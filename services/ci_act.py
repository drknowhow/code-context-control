"""AgentCI — the `act` execution engine (Linux jobs in real containers).

The native engine runs a job's shell steps on this machine. That is faithful
when `runs-on` matches the host and an approximation otherwise, and it cannot
run `uses:` actions at all. This engine hands a Linux job to `nektos/act`,
which runs it in a container using GitHub's own workflow semantics — real
actions included.

What that buys, measured on this repository from Windows: 3 of 15 jobs
runnable natively, 11 with containers. The remaining four are macOS cells, and
no amount of Docker fixes those — **there are no macOS containers**, so a full
local pass on a matrix containing them stays out of reach. Saying so is part of
the job.

Three things learned by probing rather than by reading, all load-bearing:

1. **`--bind` is required on Windows.** act's default copies the workspace into
   the container; against a Windows host path that silently produced an empty
   directory and every step failed on missing files. `--bind` mounts the real
   working tree and works even on a mapped drive whose path contains spaces and
   parentheses. The trade-off is that the container writes into the real tree —
   the same thing the native engine already does, so behaviour is consistent.

2. **`-P <label>=<image>` must always be passed.** Without it act prompts
   interactively on first use to choose an image size, which would hang any
   automated run.

3. **`-W <file>` is mandatory when two workflows share a job name.** act says so
   itself on this repo, where `CI` and `Release` both define `build` — the same
   collision the DAG scopes around.

Safety: this engine never passes real secrets or tokens. act reads `.secrets`
and `.env` from the repository by default, so both are explicitly pointed at
empty files. A publish step therefore executes and fails at authentication
instead of publishing, which is the intended outcome for a local run.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

# act's community runner images. These mirror a good deal of the GitHub
# runner's preinstalled toolchain; a bare `ubuntu:24.04` does not, and a job
# that assumes a preinstalled tool then fails locally while passing in CI. A
# false red is the safe direction, but it is still noise, so default to the
# image built for the purpose.
DEFAULT_IMAGE = "catthehacker/ubuntu:act-latest"
RUNNER_IMAGES = {
    "ubuntu-latest": DEFAULT_IMAGE,
    "ubuntu-24.04": "catthehacker/ubuntu:act-24.04",
    "ubuntu-22.04": "catthehacker/ubuntu:act-22.04",
    "ubuntu-20.04": "catthehacker/ubuntu:act-20.04",
}

# Pulling a runner image is ~1 GB on first use, so the act engine gets its own,
# larger default ceiling than a native shell step.
DEFAULT_ACT_TIMEOUT = 3600


def image_for(runs_on: str) -> str:
    label = str(runs_on or "").strip().lower()
    return RUNNER_IMAGES.get(label, DEFAULT_IMAGE)


def find_act() -> str:
    """Path to the act binary. C3_ACT_PATH wins, then PATH."""
    override = (os.environ.get("C3_ACT_PATH") or "").strip()
    if override and Path(override).is_file():
        return override
    return shutil.which("act") or ""


def _probe(cmd: list, timeout: int = 30) -> tuple:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout, stdin=subprocess.DEVNULL,
                             **({"creationflags": subprocess.CREATE_NO_WINDOW}
                                if os.name == "nt" else {}))
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except Exception as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def availability() -> dict:
    """Can this engine run at all? Reports WHY not, never just False.

    Both halves are checked because they fail independently and a user with
    act installed but Docker stopped deserves to be told which one to fix.
    """
    act = find_act()
    info: dict = {"ok": False, "act": act, "act_version": "",
                  "docker": False, "docker_version": "", "reason": ""}
    if not act:
        info["reason"] = ("`act` is not installed. Install it with "
                          "`winget install nektos.act` (Windows), "
                          "`brew install act` (macOS), or see "
                          "https://nektosact.com — then re-run.")
        return info
    rc, out = _probe([act, "--version"])
    if rc != 0:
        info["reason"] = f"`act --version` failed: {out.strip()[:200]}"
        return info
    info["act_version"] = out.strip().splitlines()[0] if out.strip() else ""

    rc, out = _probe(["docker", "version", "--format", "{{.Server.Version}}"])
    if rc != 0:
        info["reason"] = ("Docker is not reachable — the act engine needs a "
                          "running daemon. Start Docker Desktop (or dockerd) "
                          f"and retry. Probe said: {out.strip()[:160]}")
        return info
    info["docker"] = True
    info["docker_version"] = out.strip().splitlines()[-1] if out.strip() else ""
    info["ok"] = True

    # Is the default runner image already local? Absent, the first act run
    # pulls ~1 GB, which is worth warning about but is not unreadiness. This
    # probe deliberately does NOT pull: it cannot, in any case, predict the
    # failure in issue #173 — there the image is present and `docker pull`
    # succeeds, while act's own pull fails because act sends the stored
    # registry credential and the CLI does not. Only a real act run shows
    # that, which is why the runner classifies it (see setup_failure).
    rc, _ = _probe(["docker", "image", "inspect", DEFAULT_IMAGE], timeout=20)
    info["image_present"] = rc == 0
    if rc != 0:
        info["warning"] = (f"runner image {DEFAULT_IMAGE} is not present "
                           "locally; the first act run pulls ~1 GB.")
    return info


def image_present(image: str) -> bool:
    """Is *image* already in the local Docker store?

    Decides `--pull` for a run: an image that is here needs no registry
    round-trip, and skipping it also skips the issue-#173 trap — act sends
    the stored Docker Hub credential on every pull, so an expired login
    turns a public image that is already on disk into an auth failure.
    """
    rc, _ = _probe(["docker", "image", "inspect", image], timeout=20)
    return rc == 0


# ── Side-effect gate ────────────────────────────────────────────────────────
# With a real action runner, jobs that were previously unrunnable become
# runnable — including ones whose entire purpose is to publish. Capability no
# longer protects anybody, so policy has to.
PUBLISHING_ACTIONS = (
    "pypa/gh-action-pypi-publish",
    "softprops/action-gh-release",
    "ncipollo/release-action",
    "actions/create-release",
    "docker/build-push-action",
    "peaceiris/actions-gh-pages",
    "actions/deploy-pages",
    "js-devtools/npm-publish",
    "gradle/gradle-publish",
    "cycjimmy/semantic-release-action",
)
_PUBLISH_CMD = re.compile(
    r"\b(twine\s+upload|npm\s+publish|cargo\s+publish|gh\s+release\s+create"
    r"|docker\s+push|poetry\s+publish|aws\s+s3\s+(cp|sync)|kubectl\s+apply"
    r"|terraform\s+apply|gcloud\s+.*deploy)\b", re.IGNORECASE)


def side_effects(inst) -> list:
    """Reasons this job looks like it publishes or deploys something."""
    found: list = []
    for step in getattr(inst, "steps", []):
        base = str(getattr(step, "uses", "") or "").split("@", 1)[0].lower()
        if base in PUBLISHING_ACTIONS:
            found.append(f"step {step.index} uses `{step.uses}`")
        match = _PUBLISH_CMD.search(str(getattr(step, "run", "") or ""))
        if match:
            found.append(f"step {step.index} runs `{match.group(0)}`")
    return found


# ── Host-mutation gate (native engine only) ─────────────────────────────────
# Learned the hard way, on this repository. A required-mode run selected every
# job and the native engine executed C3's own `test` job, whose steps are
# `python -m pip install -e ".[dev]"`. That uninstalled the copied C3 from
# site-packages and replaced it with an editable install pointing at the repo;
# the run then timed out mid-write and left the package — and every project's
# hooks — broken.
#
# The native engine has no isolation: a `run:` step executes as the user, on
# the user's machine, against the user's interpreter. Most CI jobs begin by
# installing dependencies, so "run the repo's real CI natively" means "let a
# YAML file reconfigure this machine". Under act the same step is contained and
# harmless, which is why the refusal points there rather than just saying no.
_HOST_MUTATION = re.compile(
    r"(?:^|[\s;&|])(?:"
    r"pip\s+(?:install|uninstall)|pip3\s+(?:install|uninstall)"
    r"|python\s+-m\s+pip\s+(?:install|uninstall)"
    r"|npm\s+(?:i|install)\s+(?:-g|--global)|npm\s+uninstall\s+(?:-g|--global)"
    r"|yarn\s+global\s+add|pnpm\s+add\s+(?:-g|--global)"
    r"|apt(?:-get)?\s+(?:install|remove|purge)|yum\s+install|dnf\s+install"
    r"|brew\s+(?:install|uninstall)|choco\s+install|winget\s+install"
    r"|cargo\s+install|go\s+install|gem\s+install"
    r"|rustup\s|nvm\s+install|conda\s+(?:install|remove)"
    r")\b", re.IGNORECASE)


def host_mutations(inst) -> list:
    """Steps that would reconfigure THIS machine if run without a container."""
    found: list = []
    for step in getattr(inst, "steps", []):
        match = _HOST_MUTATION.search(str(getattr(step, "run", "") or ""))
        if match:
            found.append(f"step {step.index} runs `{match.group(0).strip()}`")
    return found


# ── Command construction ────────────────────────────────────────────────────

def build_command(inst, project, act_path: str, event: str = "",
                  image: str = "", network: str = "",
                  secret_file: str = "", env_file: str = "",
                  artifact_dir: str = "", pull: bool = True,
                  workflow_file: str = "", artifact_port: int = 0) -> list:
    """The exact argv handed to act. Pure, so it is unit-testable.

    *workflow_file* overrides the job's own workflow path — the derived copy
    from derive_workflow(). act accepts `-W` outside the working tree, so it
    lives in a temp dir and the repository stays untouched.

    *artifact_port*: act's artifact server binds ONE fixed port (34567) by
    default, so two act runs on the same box — two sessions, two projects,
    a test suite beside a real run — collide and the second dies with
    "bind: Only one usage of each socket address" before any step runs.
    run_job() picks a free port per run.
    """
    cmd: list = [act_path]
    if event:
        cmd.append(event)

    workflow_path = workflow_file or getattr(inst, "workflow_path", "") or ""
    if workflow_file:
        cmd += ["-W", str(workflow_file).replace("\\", "/")]
    elif workflow_path:
        try:
            rel = Path(workflow_path).resolve().relative_to(Path(project).resolve())
            cmd += ["-W", str(rel).replace("\\", "/")]
        except ValueError:
            cmd += ["-W", str(workflow_path)]

    cmd += ["-j", inst.job_id]

    for key, value in sorted((inst.matrix or {}).items()):
        cmd += ["--matrix", f"{key}:{value}"]

    cmd += ["-P", f"{inst.runs_on}={image or image_for(inst.runs_on)}"]

    # See the module docstring: without --bind the workspace arrives empty on
    # Windows and every step fails on missing files.
    cmd.append("--bind")
    cmd.append(f"--pull={'true' if pull else 'false'}")

    if network:
        cmd += ["--network", network]
    # Never let act source the repository's real .secrets / .env.
    cmd += ["--secret-file", secret_file or os.devnull]
    cmd += ["--env-file", env_file or os.devnull]
    if artifact_dir:
        cmd += ["--artifact-server-path", artifact_dir]
        if artifact_port:
            cmd += ["--artifact-server-port", str(int(artifact_port))]
    return cmd


def free_port() -> int:
    """A TCP port nothing is listening on right now, for act's artifact server."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("", 0))
            return int(sock.getsockname()[1])
    except OSError:
        return 0


# act prefixes every line it emits with `[Workflow/job]`, and marks actual
# program output with a `|` gutter:
#
#     [CI/lint] ⭐ Run Main ruff check .        <- act's own narration
#     [CI/lint]   | app/x.py:3:1: F401 ...      <- what the command printed
#
# Handing the raw log to the failure parsers captures the prefix as part of the
# filename — `file=[CI/lint] ⭐ Run Main echo "app/x.py` — which makes the
# structured failure useless. Parse the gutter lines only.
_ACT_OUTPUT_LINE = re.compile(r"^\[[^\]]*\]\s{0,3}\|\s?(.*)$")


# ── Setup-phase failures ────────────────────────────────────────────────────
# act can fail BEFORE any workflow step runs: the runner image cannot be
# pulled, the daemon refuses, the container never starts. Nothing the failure
# parsers look for was ever printed, so parsing that log yields `unparsed` and
# `c3_ci(action='failures')` answers "0 parsed failures" — for what is really
# "the runner image could not be obtained" (issue #173). Classify it instead.
SETUP_PARSER = "container-setup"

_SETUP_FAILED = re.compile(r"(?:❌\s*)?Failure\s+-\s+Set up job", re.I)
_DAEMON_ERROR = re.compile(r"Error response from daemon:\s*(.+)", re.I)
_SETUP_REASONS = (
    (re.compile(r"authentication required|incorrect username or password", re.I),
     "the Docker registry rejected the runner image pull with an "
     "authentication error. act sends the stored Docker credential where the "
     "CLI sends none for a public image, so an expired token turns a public "
     "pull into an auth failure — `docker logout` clears it."),
    (re.compile(r"pull access denied|repository does not exist|manifest unknown", re.I),
     "the runner image could not be found in the registry."),
    (re.compile(r"Cannot connect to the Docker daemon|daemon is not running|"
                r"docker daemon is not running", re.I),
     "the Docker daemon is not reachable — start Docker and retry."),
    (re.compile(r"no space left on device", re.I),
     "the Docker host ran out of disk space pulling the runner image."),
)


def setup_failure(text: str) -> str:
    """Why act failed before any workflow step ran, or '' if it got that far.

    The verdict marker is act's own `Failure - Set up job`; the patterns after
    it only supply a reason a human can act on. A job that reached its steps
    and failed there is NOT a setup failure and must still go to the parsers.
    """
    text = text or ""
    if not _SETUP_FAILED.search(text):
        return ""
    for pattern, reason in _SETUP_REASONS:
        if pattern.search(text):
            return f"act could not start the runner container: {reason}"
    daemon = _DAEMON_ERROR.search(text)
    if daemon:
        return ("act could not start the runner container — the Docker daemon "
                f"said: {daemon.group(1).strip()[:200]}")
    return ("act failed during 'Set up job': the runner container never "
            "started, so no workflow step ran.")


def program_output(text: str) -> str:
    """Just what the commands printed, with act's prefixes removed.

    Falls back to the raw text when nothing matches, so an act failure that
    never reached a command (a bad image, a daemon error) still reaches the
    parsers and the reader rather than becoming an empty log.
    """
    lines = [m.group(1) for m in
             (_ACT_OUTPUT_LINE.match(ln) for ln in (text or "").splitlines())
             if m]
    return "\n".join(lines) if lines else (text or "")


# ── Owning the DAG ──────────────────────────────────────────────────────────
# `act -j <job>` runs the job's `needs` first — measured 2026-09-12 with a
# two-job probe: `-j b` ran `a`, then `b`. C3 already ran `a`, ordered the DAG
# and recorded the result, so under the old per-job invocation every
# dependency executed twice and an aggregator job replayed the whole workflow
# inside its container. The derived copy below holds one job, no `needs`, no
# `if:` (C3 evaluated it before dispatch), and `needs.*` spelled out.

_NEEDS_REF = re.compile(
    r"\$\{\{\s*needs\.([A-Za-z_][\w-]*)\.(result|outputs\.([A-Za-z_][\w-]*))\s*\}\}")
_SET_OUTPUT = re.compile(r"::set-output::\s*([^=\s]+)=(.*)$")


def _reads_needs(inst) -> bool:
    for step in getattr(inst, "steps", []):
        if "needs." in str(getattr(step, "run", "") or ""):
            return True
        for value in (getattr(step, "env", None) or {}).values():
            if "needs." in str(value):
                return True
    return False


def derive_workflow(inst, needs_ctx: dict, source_text: str = "") -> tuple:
    """(yaml_text, unresolved) — this one job, ready for `act -j`.

    *source_text* is the workflow file's content (read from
    ``inst.workflow_path`` when omitted). The job keeps everything else —
    matrix, env, steps, `outputs:` — so act's behaviour is unchanged.
    Returns (None, []) when the source cannot be read or parsed.

    A `needs.*` reference to a result or output C3 does not hold is returned
    in *unresolved* rather than left for act, which would resolve it to ""
    and run something CI never would.
    """
    text = source_text
    if not text:
        try:
            text = Path(getattr(inst, "workflow_path", "")).read_text(encoding="utf-8")
        except (OSError, TypeError, ValueError):
            return None, []
    try:
        raw = yaml.safe_load(text) or {}
    except Exception:
        return None, []
    if not isinstance(raw, dict):
        return None, []
    jobs = raw.get("jobs")
    job = jobs.get(inst.job_id) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        return None, []

    job = dict(job)
    job.pop("needs", None)
    job.pop("if", None)
    derived = {k: v for k, v in raw.items() if k != "jobs"}
    derived["jobs"] = {inst.job_id: job}
    # `on:` parses as the boolean True under YAML 1.1; put the key back as a
    # string so act's parser sees a trigger list.
    if True in derived:
        derived["on"] = derived.pop(True)

    unresolved: list = []

    def _one(match: re.Match) -> str:
        dep, kind, key = match.group(1), match.group(2), match.group(3)
        rec = (needs_ctx or {}).get(dep)
        if not isinstance(rec, dict):
            unresolved.append(match.group(0).strip())
            return match.group(0)
        if kind == "result":
            return str(rec.get("result", ""))
        outputs = rec.get("outputs") or {}
        if key not in outputs:
            unresolved.append(match.group(0).strip())
            return match.group(0)
        return str(outputs[key])

    dumped = yaml.safe_dump(derived, sort_keys=False, allow_unicode=True,
                            width=10_000)
    substituted = _NEEDS_REF.sub(_one, dumped)
    return substituted, sorted(set(unresolved))


def job_outputs_from_log(text: str) -> dict:
    """Every `::set-output:: k=v` act logged, last write wins."""
    found: dict = {}
    for line in (text or "").splitlines():
        m = _SET_OUTPUT.search(line)
        if m:
            found[m.group(1)] = m.group(2).rstrip()
    return found


def run_job(inst, project, timeout: int = DEFAULT_ACT_TIMEOUT,
            event: str = "", network: str = "", artifact_dir: str = "",
            act_path: str = "", needs_ctx: dict = None) -> dict:
    """Run one job through act. Returns {exit_code, output, timed_out, command}.

    *needs_ctx* is `{job_id: {"result": ..., "outputs": {...}}}` for the
    job's dependencies, as C3 recorded them. When the job declares `needs`
    or reads `needs.*`, act is handed a derived workflow (derive_workflow)
    so it runs this job alone. An unresolvable `needs.*` reference comes
    back as {"unresolved": [...]} without act ever starting.
    """
    act_path = act_path or find_act()
    if not act_path:
        return {"exit_code": 127, "output": "act is not installed",
                "timed_out": False, "command": ""}

    tmpdir = tempfile.mkdtemp(prefix="c3act-")
    empty = Path(tmpdir) / "empty"
    empty.write_text("", encoding="utf-8")

    workflow_file = ""
    if getattr(inst, "needs", None) or _reads_needs(inst):
        derived, unresolved = derive_workflow(inst, needs_ctx or {})
        if unresolved:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return {"exit_code": 0, "output": "", "timed_out": False,
                    "command": "", "unresolved": unresolved}
        if derived is None:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return {"exit_code": 126, "timed_out": False, "command": "",
                    "output": "could not read the workflow file to derive a "
                              "single-job copy for act"}
        workflow_file = str(Path(tmpdir) / "workflow.yml")
        Path(workflow_file).write_text(derived, encoding="utf-8")

    image = image_for(inst.runs_on)
    cmd = build_command(inst, project, act_path, event=event, network=network,
                        secret_file=str(empty), env_file=str(empty),
                        artifact_dir=artifact_dir, workflow_file=workflow_file,
                        pull=not image_present(image),
                        artifact_port=free_port() if artifact_dir else 0)

    from cli.tools.shell import _kill_tree, _popen_kwargs

    start = time.time()
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(project), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", **_popen_kwargs(),
        )
    except (OSError, ValueError) as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"exit_code": 126, "output": f"{type(exc).__name__}: {exc}",
                "timed_out": False, "command": " ".join(cmd)}

    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=5)
        except Exception:
            output = ""
        timed_out = True
        output = (output or "") + (
            f"\n[c3:ci] act exceeded {timeout}s and its container tree was killed\n")

    shutil.rmtree(tmpdir, ignore_errors=True)
    return {
        "exit_code": -1 if timed_out else (proc.returncode or 0),
        "output": output or "",
        "timed_out": timed_out,
        "duration_ms": round((time.time() - start) * 1000),
        "command": " ".join(cmd),
    }
