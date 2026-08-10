"""AgentCI — execute a normalized job DAG locally and record what happened.

Design commitments, in the order they matter:

1. **A local pass is only a full pass when everything actually ran here.**
   C3's own CI has nine test cells across three operating systems; on Windows
   three of them are reproducible and six are not. A tool that printed "PASS"
   after running a quarter of the matrix would be actively dangerous at the
   moment it is used — right before a push. So the verdict is computed from
   coverage as well as from results, and `FULL_PASS` is unreachable whenever a
   job was foreign, unsupported, or deselected. (AgentCI spec §13, §34.)

2. **Downstream of a failure is `skipped`, not `passed`.** `needs:` is a real
   edge; a job whose dependency failed did not succeed, it never ran.

3. **A timeout is its own status.** It is neither a pass nor a clean failure —
   the job's effect is unknown, which is exactly the distinction the run
   report has to preserve.

Execution reuses `cli.tools.shell`'s hardened primitives rather than
re-deriving them: Popen + `taskkill /F /T` + `stdin=DEVNULL`, because
`subprocess.run(shell=True, timeout=...)` hangs forever on Windows.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from services import ci_act, ci_cache, ci_expr, ci_failures, ci_impact
from services.ci_workflow import (
    CycleError,
    build_dag,
    discover_workflows,
    git_context,
    host_os,
    parse_workflow,
    runner_os,
)

CI_DIR = ".c3/ci"
INDEX_FILE = "index.jsonl"

DEFAULT_STEP_TIMEOUT = 900          # 15 min — a build step can legitimately be slow
MAX_LOG_CHARS = 200_000             # per job, on disk

# Job statuses
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"                 # a dependency failed
SKIPPED_IF = "skipped_if"           # its own `if:` said no — faithful, not a gap
UNSUPPORTED = "unsupported"         # blockers from the parser
FOREIGN = "foreign"                 # different OS; not attempted
TIMEOUT = "timeout"
DESELECTED = "deselected"           # not part of this run's selection
CACHED = "cached"                   # passed before for these exact inputs

# Run verdicts
FULL_PASS = "FULL_CI_PASS"
PARTIAL_PASS = "PARTIAL_PASS"
FAIL = "FAIL"

# Statuses that mean coverage was LOST — they block FULL_CI_PASS.
# SKIPPED_IF is deliberately absent: a job whose own `if:` excluded it was
# faithfully reproduced by not running, exactly as CI would not run it.
# Counting it as a gap would make any workflow with a conditional job unable to
# reach a full pass, which would be wrong rather than merely strict.
_NOT_RUN = (SKIPPED, UNSUPPORTED, FOREIGN, DESELECTED)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:120] or "job"


# ── Result types ────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    index: int
    name: str
    status: str
    exit_code: int = 0
    duration_ms: int = 0
    shim: str = ""
    shell: str = ""

    def to_dict(self) -> dict:
        return {"index": self.index, "name": self.name, "status": self.status,
                "exit_code": self.exit_code, "duration_ms": self.duration_ms,
                "shim": self.shim, "shell": self.shell}


# How faithfully a job was reproduced. This is not cosmetic: FULL_CI_PASS
# requires every job at `native` or `container`, so a cross-OS approximation
# can never be mistaken for the real thing.
FIDELITY_NATIVE = "native"        # ran on the OS it targets
FIDELITY_CONTAINER = "container"  # ran in a container of the OS it targets
FIDELITY_CROSS_OS = "cross-os"    # ran on a different OS — indicative only


@dataclass
class JobResult:
    key: str
    job_id: str
    name: str
    workflow: str
    runs_on: str
    status: str
    reason: str = ""
    engine: str = "native"          # native | act
    fidelity: str = FIDELITY_NATIVE
    fingerprint: str = ""
    cross_os: bool = False          # ran here despite a foreign `runs-on`
    duration_ms: int = 0
    steps: list = field(default_factory=list)
    failures: list = field(default_factory=list)
    parser: str = ""
    log_path: str = ""

    @property
    def ran(self) -> bool:
        return self.status not in _NOT_RUN

    def to_dict(self) -> dict:
        return {
            "key": self.key, "job_id": self.job_id, "name": self.name,
            "workflow": self.workflow, "runs_on": self.runs_on,
            "status": self.status, "reason": self.reason,
            "engine": self.engine, "fidelity": self.fidelity,
            "fingerprint": self.fingerprint,
            "cross_os": self.cross_os, "duration_ms": self.duration_ms,
            "steps": [s.to_dict() for s in self.steps],
            "failures": self.failures, "parser": self.parser,
            "log_path": self.log_path,
        }


@dataclass
class RunResult:
    run_id: str
    project: str
    started_at: str
    finished_at: str = ""
    verdict: str = FAIL
    host_os: str = ""
    fingerprint: dict = field(default_factory=dict)
    jobs: list = field(default_factory=list)
    selection: str = ""
    allow_foreign: bool = False
    event: str = ""
    engine: str = "auto"
    mode: str = "full"
    plan: dict = field(default_factory=dict)
    note: str = ""

    # ── Roll-ups the report and the UI both read ──
    @property
    def counts(self) -> dict:
        out: dict = {}
        for job in self.jobs:
            out[job.status] = out.get(job.status, 0) + 1
        return out

    @property
    def failed_keys(self) -> list:
        return [j.key for j in self.jobs if j.status in (FAILED, TIMEOUT)]

    @property
    def not_run_keys(self) -> list:
        return [j.key for j in self.jobs if j.status in _NOT_RUN]

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "project": self.project,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "verdict": self.verdict, "host_os": self.host_os,
            "fingerprint": self.fingerprint, "selection": self.selection,
            "allow_foreign": self.allow_foreign, "event": self.event,
            "engine": self.engine, "mode": self.mode,
            "plan": self.plan,
            "note": self.note,
            "counts": self.counts,
            "failed": self.failed_keys, "not_run": self.not_run_keys,
            "jobs": [j.to_dict() for j in self.jobs],
        }


# ── Fingerprint (spec §16) ──────────────────────────────────────────────────

def fingerprint(project_path) -> dict:
    """What the tree looked like, so a stale result can be recognised as stale.

    A dirty tree is the normal case for an agent mid-edit — that is the whole
    point of running CI before committing — so `dirty` is recorded, never
    treated as an error.
    """
    def _git(*args) -> str:
        try:
            out = subprocess.run(
                ["git", *args], cwd=str(project_path), capture_output=True,
                text=True, timeout=15, stdin=subprocess.DEVNULL,
                **({"creationflags": subprocess.CREATE_NO_WINDOW}
                   if os.name == "nt" else {}),
            )
            return (out.stdout or "").strip()
        except Exception:
            return ""

    status = _git("status", "--porcelain")
    return {
        "sha": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status),
        "dirty_files": len([ln for ln in status.splitlines() if ln.strip()]),
    }


# ── Step execution ──────────────────────────────────────────────────────────

def _run_step(step, cwd: Path, env: dict, timeout: int) -> tuple:
    """Execute one step. Returns (StepResult, combined_output)."""
    from cli.tools.shell import _run_sync

    # A shimmed `uses:` produces no output and cannot fail — it stands in for
    # setup whose local equivalent is "the condition already holds".
    if step.uses:
        from services.ci_workflow import action_name
        if action_name(step.uses) == "actions/cache":
            # The one cache nothing else provides locally: act caches images
            # and actions, Docker caches layers, but a workflow's declared
            # dependency paths are ours to keep.
            key = str((step.with_ or {}).get("key") or "").strip()
            paths = [ln.strip() for ln in
                     str((step.with_ or {}).get("path") or "").splitlines()
                     if ln.strip()]
            if key and paths:
                hit = ci_cache.restore_dependency(cwd, key, paths)
                note = "hit" if hit else "miss"
                return (StepResult(index=step.index, name=step.name,
                                   status="shim",
                                   shim=f"dependency cache {note}"),
                        f"[c3:ci] actions/cache {note} key={key}\n")
        return (StepResult(index=step.index, name=step.name, status="shim",
                           shim=step.shim),
                f"[c3:ci] shim {step.uses} — {step.shim}\n")

    workdir = cwd
    if step.working_directory:
        candidate = (cwd / step.working_directory).resolve()
        if candidate.is_dir():
            workdir = candidate

    res = _run_sync(step.run, str(workdir), timeout, extra_env=env)
    status = TIMEOUT if res["timed_out"] else (
        PASSED if res["exit_code"] == 0 else FAILED)
    header = f"\n[c3:ci] $ {step.run.strip()}\n"
    body = (res["stdout"] or "") + (res["stderr"] or "")
    if res["timed_out"]:
        body += f"\n[c3:ci] step timed out after {timeout}s and was killed\n"
    return (StepResult(index=step.index, name=step.name, status=status,
                       exit_code=res["exit_code"],
                       duration_ms=res["duration_ms"], shell=res["shell"]),
            header + body)


def _save_dependency_caches(inst, project: Path) -> None:
    """Persist `actions/cache` paths after a passing job, as GitHub does.

    A failed job must not write the cache: the next run would restore whatever
    half-built state caused the failure and then blame the code.
    """
    from services.ci_workflow import action_name
    for step in inst.steps:
        if action_name(step.uses) != "actions/cache":
            continue
        key = str((step.with_ or {}).get("key") or "").strip()
        paths = [ln.strip() for ln in
                 str((step.with_ or {}).get("path") or "").splitlines()
                 if ln.strip()]
        if key and paths:
            ci_cache.save_dependency(project, key, paths)


def _collect_artifacts(inst, run_dir: Path, project: Path) -> None:
    """Honour `actions/upload-artifact` against a local directory (spec §32)."""
    for step in inst.steps:
        from services.ci_workflow import action_name
        if action_name(step.uses) != "actions/upload-artifact":
            continue
        src = str((step.with_ or {}).get("path") or "").strip()
        if not src:
            continue
        dest_root = run_dir / "artifacts" / _safe(
            str((step.with_ or {}).get("name") or "artifact"))
        for pattern in [p.strip() for p in src.splitlines() if p.strip()]:
            for match in project.glob(pattern):
                try:
                    target = dest_root / match.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if match.is_dir():
                        shutil.copytree(match, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(match, target)
                except Exception:
                    # Artifact capture is a convenience; failing to copy one
                    # must never change the job's verdict.
                    pass


# ── Job execution ───────────────────────────────────────────────────────────

def _job_env(inst, project: Path) -> dict:
    env = dict(inst.env or {})
    # `CI=true` is honest — this IS continuous integration. `GITHUB_ACTIONS` is
    # deliberately NOT set: we are not GitHub Actions, and tools that branch on
    # it would take a path this runner cannot reproduce.
    env.setdefault("CI", "true")
    env["C3_AGENTCI"] = "1"
    for key, value in (inst.matrix or {}).items():
        env[f"MATRIX_{str(key).upper().replace('-', '_')}"] = str(value)
    return {str(k): str(v) for k, v in env.items()}


def _pick_engine(inst, engine: str, act_state: dict, allow_foreign: bool):
    """(engine|None, fidelity, reason_if_refused) for one job.

    The ladder, most faithful first: run it on the OS it targets; failing that,
    run it in a container of that OS; failing that, refuse — unless the caller
    explicitly accepts a cross-OS approximation.
    """
    target = runner_os(inst.runs_on)
    act_ok = bool(act_state.get("ok"))

    if not inst.foreign_runner:
        # `engine=act` is honoured even for a matching runner, because running
        # a Linux job in a container on Linux is closer to CI than the host is.
        if engine == "act" and act_ok and target == "Linux":
            return "act", FIDELITY_CONTAINER, ""
        return "native", FIDELITY_NATIVE, ""

    if target == "Linux" and engine in ("auto", "act") and act_ok:
        return "act", FIDELITY_CONTAINER, ""


    if allow_foreign:
        return "native", FIDELITY_CROSS_OS, ""

    hint = f"targets {inst.runs_on}; host is {host_os()}. "
    if target == "Linux" and not act_ok:
        hint += ("It could run in a container, but the act engine is "
                 f"unavailable: {act_state.get('reason', 'unknown')} ")
    elif target == "Darwin":
        hint += "There are no macOS containers, so this can never run here. "
    hint += "Pass allow_foreign to attempt it on this OS anyway (cross-OS)."
    return None, FIDELITY_CROSS_OS, hint


def _run_job_act(inst, project: Path, run_dir: Path, event: str,
                 network: str, fidelity: str) -> JobResult:
    """Delegate one job to act, then read its result the same way as any other."""
    result = JobResult(key=inst.key, job_id=inst.job_id, name=inst.name,
                       workflow=inst.workflow, runs_on=inst.runs_on,
                       status=PASSED, engine="act", fidelity=fidelity,
                       cross_os=False)
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)

    outcome = ci_act.run_job(inst, project, event=event, network=network,
                             artifact_dir=str(artifacts))
    header = (f"[c3:ci] job {inst.key} via act "
              f"(image {ci_act.image_for(inst.runs_on)}, host {host_os()})\n"
              f"[c3:ci] $ {outcome.get('command', '')}\n\n")
    log = header + (outcome.get("output") or "")

    result.duration_ms = outcome.get("duration_ms", 0)
    if outcome.get("timed_out"):
        result.status = TIMEOUT
        result.reason = "act exceeded its timeout; the container tree was killed"
    elif outcome.get("exit_code"):
        result.status = FAILED
        result.reason = f"act exited {outcome['exit_code']}"

    # act reports per-step results in its own log format rather than as data,
    # so the job is recorded as one unit. The failure parsers read the log the
    # same way they read a native one, which is why pytest/ruff output inside a
    # container still yields {file,line,message}.
    result.steps = [StepResult(index=0, name=f"act {inst.job_id}",
                               status=result.status,
                               exit_code=outcome.get("exit_code", 0),
                               duration_ms=result.duration_ms, shell="act")]

    if len(log) > MAX_LOG_CHARS:
        log = (log[: MAX_LOG_CHARS // 2]
               + f"\n\n[c3:ci] ... {len(log) - MAX_LOG_CHARS} chars elided ...\n\n"
               + log[-MAX_LOG_CHARS // 2:])
    log_path = run_dir / f"{_safe(inst.key)}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(log, encoding="utf-8")
        result.log_path = str(log_path)
    except OSError:
        pass

    if result.status in (FAILED, TIMEOUT):
        # Parse what the commands printed, not act's narration around it —
        # otherwise every file path arrives wearing a `[CI/lint] |` prefix.
        parsed = ci_failures.parse(
            ci_act.program_output(outcome.get("output") or ""), exit_code=1)
        result.failures = [f.to_dict() for f in parsed.failures]
        result.parser = parsed.parser
    return result


def _eval_values(inst, needs_results: dict, github: dict) -> dict:
    """The contexts an `if:` may read, built from facts we actually have."""
    return {
        "github": dict(github or {}),
        "env": dict(inst.env or {}),
        "matrix": dict(inst.matrix or {}),
        "runner": {"os": host_os()},
        "job": {"status": "success"},
        "needs": needs_results,
        "steps": {},
        "strategy": {"job-index": 0},
    }


def _run_job(inst, project: Path, run_dir: Path, timeout: int,
             eval_values: dict = None) -> JobResult:
    eval_values = eval_values or _eval_values(inst, {}, {})
    result = JobResult(key=inst.key, job_id=inst.job_id, name=inst.name,
                       workflow=inst.workflow, runs_on=inst.runs_on,
                       status=PASSED, cross_os=inst.foreign_runner)
    log_path = run_dir / f"{_safe(inst.key)}.log"
    chunks: list = [f"[c3:ci] job {inst.key}  runs-on={inst.runs_on}  "
                    f"host={host_os()}\n"]
    if inst.foreign_runner:
        chunks.append(
            f"[c3:ci] NOTE cross-OS: this job targets {inst.runs_on} and is "
            f"running on {host_os()}. Results are indicative, not equivalent.\n")

    started = time.time()
    env = _job_env(inst, project)

    # A failed step no longer ends the job outright: `if: always()` exists
    # precisely so cleanup and reporting steps run afterwards. The first
    # failure fixes the job's verdict; later steps run only if their own
    # condition permits it.
    job_failed = False
    for step in inst.steps:
        # A step with no `if:` carries an implicit success() gate, so once the
        # job has failed it is skipped. Only an explicit condition can opt out
        # of that, which is the whole reason `if: always()` exists.
        if not step.if_:
            if job_failed:
                result.steps.append(StepResult(
                    index=step.index, name=step.name, status="skipped",
                    shim="implicit success() — a previous step failed"))
                chunks.append(
                    f"\n[c3:ci] step {step.index} skipped — a previous step failed\n")
                continue
        else:
            try:
                should_run = ci_expr.evaluate(
                    step.if_, ci_expr.EvalContext(values=eval_values,
                                                  failed=job_failed))
            except (ci_expr.ExprError, ci_expr.UnknownRef) as exc:
                # Validation passed but the value is unavailable after all.
                # Refusing beats guessing in either direction.
                result.status = UNSUPPORTED
                result.reason = f"step {step.index} `if:` could not be evaluated: {exc}"
                chunks.append(f"\n[c3:ci] step {step.index} if: {exc}\n")
                break
            if not should_run:
                result.steps.append(StepResult(
                    index=step.index, name=step.name, status="skipped",
                    shim=f"if: {step.if_}"))
                chunks.append(
                    f"\n[c3:ci] step {step.index} skipped by `if: {step.if_}`\n")
                continue

        sres, output = _run_step(step, project, env, timeout)
        result.steps.append(sres)
        chunks.append(output)
        if sres.status in (FAILED, TIMEOUT) and not job_failed:
            job_failed = True
            result.status = sres.status
            result.reason = f"step {sres.index} ({sres.name}) exited {sres.exit_code}"

    result.duration_ms = round((time.time() - started) * 1000)
    log = "".join(chunks)
    if len(log) > MAX_LOG_CHARS:
        log = (log[: MAX_LOG_CHARS // 2]
               + f"\n\n[c3:ci] ... {len(log) - MAX_LOG_CHARS} chars elided ...\n\n"
               + log[-MAX_LOG_CHARS // 2:])
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(log, encoding="utf-8")
        result.log_path = str(log_path)
    except OSError:
        pass

    if result.status in (FAILED, TIMEOUT):
        parsed = ci_failures.parse(log, exit_code=1)
        result.failures = [f.to_dict() for f in parsed.failures]
        result.parser = parsed.parser
    else:
        _collect_artifacts(inst, run_dir, project)
        _save_dependency_caches(inst, project)

    return result


# ── Verdict ─────────────────────────────────────────────────────────────────

def compute_verdict(jobs: list) -> tuple:
    """(verdict, note). Coverage is part of the verdict, not a footnote."""
    if not jobs:
        return FAIL, "no jobs were selected — nothing was verified"

    failed = [j for j in jobs if j.status in (FAILED, TIMEOUT)]
    if failed:
        return FAIL, f"{len(failed)} job(s) failed"

    not_run = [j for j in jobs if j.status in _NOT_RUN]
    cross = [j for j in jobs if j.ran and j.fidelity == FIDELITY_CROSS_OS]
    if_skipped = [j for j in jobs if j.status == SKIPPED_IF]
    cached = [j for j in jobs if j.status == CACHED]
    # Mentioned, never counted against coverage: not running these IS the
    # faithful reproduction. But the reader should know how many, because the
    # answer depends on which event was declared.
    if_note = (f" ({len(if_skipped)} job(s) skipped by their own `if:`)"
               if if_skipped else "")

    if not_run:
        why: dict = {}
        for job in not_run:
            why[job.status] = why.get(job.status, 0) + 1
        detail = ", ".join(f"{n} {status}" for status, n in sorted(why.items()))
        return PARTIAL_PASS, (
            f"everything that ran passed, but {len(not_run)} of {len(jobs)} "
            f"job(s) did not run here ({detail}) — this is NOT a full CI pass")
    if cross:
        return PARTIAL_PASS, (
            f"all {len(jobs)} job(s) passed, but {len(cross)} ran on "
            f"{host_os()} against a different target OS — indicative, not "
            "equivalent. Install act + Docker to run them in a container instead.")
    ran = len(jobs) - len(if_skipped)
    cache_note = ""
    if cached:
        # A fully-cached run executed nothing. It is still a pass for these
        # exact inputs, but the reader has to know it was not re-checked.
        cache_note = (f" ({len(cached)} reused from cache -- identical inputs "
                      "to a previous pass; --no-cache forces execution)")
    return FULL_PASS, (f"all {ran} applicable job(s) passed{if_note}"
                       f"{cache_note}")


# ── Orchestration ───────────────────────────────────────────────────────────

def run_ci(project_path, selector: str = "", allow_foreign: bool = False,
           only: list = None, timeout: int = DEFAULT_STEP_TIMEOUT,
           workflow: str = "", event: str = "", engine: str = "auto",
           allow_side_effects: bool = False, network: str = "",
           mode: str = ci_impact.MODE_FULL, base: str = "",
           allow_host_mutation: bool = False,
           no_cache: bool = False) -> RunResult:
    """Plan and execute. `selector` picks jobs; `only` is an explicit key list.

    `only` exists for `rerun --failed`: it names exact keys from a prior run,
    and anything not named is `DESELECTED` — which keeps the verdict honest
    (a rerun of one job can never be a FULL CI PASS).
    """
    project = Path(project_path).resolve()
    run_id = uuid.uuid4().hex[:12]
    run_dir = project / CI_DIR / "runs" / run_id

    result = RunResult(
        run_id=run_id, project=str(project), started_at=_now(),
        host_os=host_os(), fingerprint=fingerprint(project),
        selection=selector or ("failed:" + ",".join(only) if only else "all"),
        allow_foreign=allow_foreign,
    )

    github_ctx = git_context(project)
    if event:
        github_ctx["event_name"] = event
    result.event = event
    result.engine = engine
    act_state = ci_act.availability() if engine in ('auto', 'act') else {}
    if engine == 'act' and not act_state.get('ok'):
        result.finished_at = _now()
        result.verdict = FAIL
        result.note = f"engine='act' requested but unavailable: {act_state.get('reason')}"
        _persist(project, result, run_dir)
        return result

    workflows = [parse_workflow(p) for p in discover_workflows(project)]
    if workflow:
        workflows = [w for w in workflows if w.name == workflow]
    dag = build_dag([w for w in workflows if not w.error],
                    event=event, git=github_ctx)

    if not dag.instances:
        result.finished_at = _now()
        result.verdict = FAIL
        result.note = ("no runnable workflow jobs found — "
                       f"looked in {project / '.github/workflows'}")
        _persist(project, result, run_dir)
        return result

    try:
        ordered = dag.topo_order()
    except CycleError as exc:
        result.finished_at = _now()
        result.verdict = FAIL
        result.note = str(exc)
        _persist(project, result, run_dir)
        return result

    # Selection
    skip_reasons: dict = {}   # job key -> why the planner dropped it
    cache_rules = ci_impact.load_rules(project)
    if only:
        wanted = set(only)
        chosen = {i.key for i in ordered if i.key in wanted}
    elif selector:
        matched = dag.resolve(selector)
        chosen = {i.key for i in matched}
        if not chosen:
            result.finished_at = _now()
            result.verdict = FAIL
            result.note = f"no job matches selector '{selector}'"
            _persist(project, result, run_dir)
            return result
    elif mode == ci_impact.MODE_REQUIRED:
        # Required mode narrows the run to what a change could plausibly have
        # broken. Everything it drops becomes DESELECTED, which already caps
        # the verdict at PARTIAL_PASS — a narrowed run must never read as a
        # full one (PRD 3).
        plan = ci_impact.plan_required(
            project, ordered, [w for w in workflows if not w.error],
            base=base, event=event)
        result.mode = ci_impact.MODE_REQUIRED
        result.plan = plan.to_dict()
        result.selection = f"required(base={base or 'working tree'})"
        chosen = set(plan.selected)
        skip_reasons.update({d.job: d.reason for d in plan.decisions
                             if d.decision == ci_impact.SKIP})
        if not chosen:
            result.finished_at = _now()
            result.verdict = PARTIAL_PASS
            result.note = (plan.note or "required mode selected no jobs") + \
                          " — nothing was verified"
            result.jobs = [JobResult(
                key=i.key, job_id=i.job_id, name=i.name, workflow=i.workflow,
                runs_on=i.runs_on, status=DESELECTED,
                reason=skip_reasons.get(i.key, "not selected")) for i in ordered]
            _persist(project, result, run_dir)
            return result
    else:
        chosen = {i.key for i in ordered}

    failed_jobs: set = set()        # job_ids whose failure blocks dependents
    results: list = []
    # (workflow, job_id) -> {"result": "success"|"failure"|"skipped"} so a
    # downstream `if: needs.build.result == 'success'` reads real outcomes.
    needs_results: dict = {}

    def _needs_ctx(inst) -> dict:
        return {jid: needs_results.get((inst.workflow, jid),
                                       {"result": "skipped"})
                for jid in inst.needs}

    def _record(inst, status: str) -> None:
        outcome = {PASSED: "success", SKIPPED_IF: "skipped"}.get(
            status, "failure" if status in (FAILED, TIMEOUT, UNSUPPORTED)
            else "skipped")
        needs_results[(inst.workflow, inst.job_id)] = {"result": outcome}

    for inst in ordered:
        if inst.key not in chosen:
            results.append(JobResult(
                key=inst.key, job_id=inst.job_id, name=inst.name,
                workflow=inst.workflow, runs_on=inst.runs_on,
                status=DESELECTED,
                reason=skip_reasons.get(
                    inst.key, "not part of this run's selection")))
            _record(inst, DESELECTED)
            continue

        # Engine first, THEN supportability: "can this job run?" has no answer
        # until you know what is going to run it. A step using a third-party
        # action is fatal to the native shell and unremarkable to act, so
        # asking in the other order permanently hid act's main advantage.
        chosen_engine, fidelity, why_not = _pick_engine(
            inst, engine, act_state, allow_foreign)

        if chosen_engine is not None and not inst.supported_by(chosen_engine):
            reasons = inst.blockers_for(chosen_engine)
            results.append(JobResult(
                key=inst.key, job_id=inst.job_id, name=inst.name,
                workflow=inst.workflow, runs_on=inst.runs_on,
                status=UNSUPPORTED, engine=chosen_engine, fidelity=fidelity,
                reason="; ".join(reasons)))
            failed_jobs.add((inst.workflow, inst.job_id))
            _record(inst, UNSUPPORTED)
            continue
        if chosen_engine is None:
            results.append(JobResult(
                key=inst.key, job_id=inst.job_id, name=inst.name,
                workflow=inst.workflow, runs_on=inst.runs_on,
                status=FOREIGN, cross_os=True, fidelity=FIDELITY_CROSS_OS,
                reason=why_not))
            failed_jobs.add((inst.workflow, inst.job_id))
            _record(inst, FOREIGN)
            continue

        # The native engine has no isolation: a `run:` step executes as the
        # user, against the user's interpreter. Running this repository's own
        # CI natively once uninstalled C3 mid-run (`pip install -e .`), taking
        # every project's hooks with it. Under act the same step is contained,
        # so the refusal points there instead of merely saying no.
        if chosen_engine == "native" and not allow_host_mutation:
            mutations = ci_act.host_mutations(inst)
            if mutations:
                hint = ("Run it in a container instead (engine='act')"
                        if act_state.get("ok") else
                        "Install act + Docker to run it in a container")
                results.append(JobResult(
                    key=inst.key, job_id=inst.job_id, name=inst.name,
                    workflow=inst.workflow, runs_on=inst.runs_on,
                    status=UNSUPPORTED, engine="native", fidelity=fidelity,
                    reason=("would modify THIS machine ("
                            + "; ".join(mutations[:2]) +
                            f"). The native engine has no isolation. {hint}, "
                            "or pass allow_host_mutation to accept the change.")))
                failed_jobs.add((inst.workflow, inst.job_id))
                _record(inst, UNSUPPORTED)
                continue

        # With a real action runner, a publishing job stops being unrunnable
        # and starts being one command away from actually publishing. No
        # secret is ever passed, so it should fail at auth — but "should" is
        # not a safety model, so it also needs saying yes on purpose.
        if chosen_engine == "act" and not allow_side_effects:
            risks = ci_act.side_effects(inst)
            if risks:
                results.append(JobResult(
                    key=inst.key, job_id=inst.job_id, name=inst.name,
                    workflow=inst.workflow, runs_on=inst.runs_on,
                    status=UNSUPPORTED, engine="act", fidelity=fidelity,
                    reason=("looks like it publishes or deploys ("
                            + "; ".join(risks[:2]) +
                            "). Refused by default — C3 passes no secrets, so "
                            "it would most likely fail at auth, but that is "
                            "not a guarantee. Pass allow_side_effects to run "
                            "it anyway.")))
                failed_jobs.add((inst.workflow, inst.job_id))
                _record(inst, UNSUPPORTED)
                continue

        blocked = [n for n in inst.needs if (inst.workflow, n) in failed_jobs]
        eval_values = _eval_values(inst, _needs_ctx(inst), github_ctx)

        # A job-level `if:` REPLACES the implicit success() gate on `needs` —
        # that is exactly what `if: always()` is for. So when a condition is
        # present it alone decides, and the `blocked` check below applies only
        # to jobs that declared none. (normalize() injects `success() &&` when
        # the condition names no status function, so an ordinary `if:` still
        # skips on a failed dependency — the gate is preserved, not lost.)
        if inst.if_:
            try:
                should_run = ci_expr.evaluate(
                    inst.if_, ci_expr.EvalContext(values=eval_values,
                                                  failed=bool(blocked)))
            except (ci_expr.ExprError, ci_expr.UnknownRef) as exc:
                results.append(JobResult(
                    key=inst.key, job_id=inst.job_id, name=inst.name,
                    workflow=inst.workflow, runs_on=inst.runs_on,
                    status=UNSUPPORTED,
                    reason=f"job `if:` could not be evaluated: {exc}"))
                failed_jobs.add((inst.workflow, inst.job_id))
                _record(inst, UNSUPPORTED)
                continue
            if not should_run:
                why = (f"dependency did not pass ({', '.join(sorted(set(blocked)))}) "
                       f"and `if: {inst.if_}` does not override it"
                       if blocked else
                       f"`if: {inst.if_}` is false — CI would skip it too")
                results.append(JobResult(
                    key=inst.key, job_id=inst.job_id, name=inst.name,
                    workflow=inst.workflow, runs_on=inst.runs_on,
                    status=SKIPPED_IF, reason=why))
                _record(inst, SKIPPED_IF)
                continue
            blocked = []        # the condition authorised this run

        if blocked:
            results.append(JobResult(
                key=inst.key, job_id=inst.job_id, name=inst.name,
                workflow=inst.workflow, runs_on=inst.runs_on,
                status=SKIPPED,
                reason=f"dependency did not pass: {', '.join(sorted(set(blocked)))}"))
            failed_jobs.add((inst.workflow, inst.job_id))
            _record(inst, SKIPPED)
            continue

        # Cached reuse (Phase 4). The fingerprint covers the job definition,
        # the engine, and the CONTENT of the inputs it declares — or the whole
        # tree when it declares none, which is the conservative default.
        job_fp = ""
        try:
            job_fp = ci_cache.job_fingerprint(
                project, inst, chosen_engine,
                ci_act.image_for(inst.runs_on) if chosen_engine == "act" else "",
                scope=ci_cache.scope_for(inst, cache_rules))
        except Exception:
            job_fp = ""             # never let caching break a run

        if job_fp and not no_cache:
            hit = ci_cache.lookup(project, job_fp)
            if hit:
                results.append(JobResult(
                    key=inst.key, job_id=inst.job_id, name=inst.name,
                    workflow=inst.workflow, runs_on=inst.runs_on,
                    status=CACHED, engine=chosen_engine, fidelity=fidelity,
                    fingerprint=job_fp, duration_ms=0,
                    reason=(f"identical inputs passed in run {hit.run_id} "
                            f"at {hit.at[:19]} — reused, nothing executed")))
                _record(inst, PASSED)   # downstream may proceed: it did pass
                continue

        if chosen_engine == "act":
            job_result = _run_job_act(inst, project, run_dir, event, network,
                                      fidelity)
        else:
            job_result = _run_job(inst, project, run_dir, timeout,
                                  eval_values=eval_values)
            job_result.fidelity = fidelity
        job_result.fingerprint = job_fp
        if job_fp and job_result.status == PASSED:
            ci_cache.record(project, job_fp, result.run_id, _now(),
                            job_result.duration_ms)
        results.append(job_result)
        if job_result.status in (FAILED, TIMEOUT, UNSUPPORTED):
            failed_jobs.add((inst.workflow, inst.job_id))
        _record(inst, job_result.status)

    result.jobs = results
    result.finished_at = _now()
    result.verdict, result.note = compute_verdict(results)
    _persist(project, result, run_dir)
    return result


# ── Persistence ─────────────────────────────────────────────────────────────

def _persist(project: Path, result: RunResult, run_dir: Path) -> None:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        index = project / CI_DIR / INDEX_FILE
        index.parent.mkdir(parents=True, exist_ok=True)
        summary = {
            "run_id": result.run_id, "started_at": result.started_at,
            "finished_at": result.finished_at, "verdict": result.verdict,
            "counts": result.counts, "selection": result.selection,
            "note": result.note, "fingerprint": result.fingerprint,
        }
        with open(index, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary) + "\n")
    except OSError:
        # Losing the record must not lose the result the caller already has.
        pass


def list_runs(project_path, limit: int = 20) -> list:
    index = Path(project_path) / CI_DIR / INDEX_FILE
    if not index.is_file():
        return []
    rows: list = []
    try:
        for line in index.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return list(reversed(rows))[:limit]


def load_run(project_path, run_id: str = "") -> dict:
    """One run's full record. Empty run_id loads the most recent."""
    base = Path(project_path) / CI_DIR / "runs"
    if not run_id:
        recent = list_runs(project_path, limit=1)
        if not recent:
            return {}
        run_id = recent[0].get("run_id", "")
    path = base / run_id / "run.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def read_log(project_path, run_id: str, job_key: str, tail: int = 200) -> str:
    path = (Path(project_path) / CI_DIR / "runs" / run_id
            / f"{_safe(job_key)}.log")
    if not path.is_file():
        return ""
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return "\n".join(rows[-tail:]) if tail else "\n".join(rows)
