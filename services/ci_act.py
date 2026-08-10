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
    return info


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


# ── Command construction ────────────────────────────────────────────────────

def build_command(inst, project, act_path: str, event: str = "",
                  image: str = "", network: str = "",
                  secret_file: str = "", env_file: str = "",
                  artifact_dir: str = "", pull: bool = True) -> list:
    """The exact argv handed to act. Pure, so it is unit-testable."""
    cmd: list = [act_path]
    if event:
        cmd.append(event)

    workflow_path = getattr(inst, "workflow_path", "") or ""
    if workflow_path:
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
    return cmd


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


def run_job(inst, project, timeout: int = DEFAULT_ACT_TIMEOUT,
            event: str = "", network: str = "", artifact_dir: str = "",
            act_path: str = "") -> dict:
    """Run one job through act. Returns {exit_code, output, timed_out, command}."""
    act_path = act_path or find_act()
    if not act_path:
        return {"exit_code": 127, "output": "act is not installed",
                "timed_out": False, "command": ""}

    tmpdir = tempfile.mkdtemp(prefix="c3act-")
    empty = Path(tmpdir) / "empty"
    empty.write_text("", encoding="utf-8")

    cmd = build_command(inst, project, act_path, event=event, network=network,
                        secret_file=str(empty), env_file=str(empty),
                        artifact_dir=artifact_dir)

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
