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

from services import ci_failures
from services.ci_workflow import (
    CycleError,
    build_dag,
    discover_workflows,
    host_os,
    parse_workflow,
)

CI_DIR = ".c3/ci"
INDEX_FILE = "index.jsonl"

DEFAULT_STEP_TIMEOUT = 900          # 15 min — a build step can legitimately be slow
MAX_LOG_CHARS = 200_000             # per job, on disk

# Job statuses
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"                 # a dependency failed
UNSUPPORTED = "unsupported"         # blockers from the parser
FOREIGN = "foreign"                 # different OS; not attempted
TIMEOUT = "timeout"
DESELECTED = "deselected"           # not part of this run's selection

# Run verdicts
FULL_PASS = "FULL_CI_PASS"
PARTIAL_PASS = "PARTIAL_PASS"
FAIL = "FAIL"

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


@dataclass
class JobResult:
    key: str
    job_id: str
    name: str
    workflow: str
    runs_on: str
    status: str
    reason: str = ""
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
            "allow_foreign": self.allow_foreign, "note": self.note,
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


def _run_job(inst, project: Path, run_dir: Path, timeout: int) -> JobResult:
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

    for step in inst.steps:
        sres, output = _run_step(step, project, env, timeout)
        result.steps.append(sres)
        chunks.append(output)
        if sres.status in (FAILED, TIMEOUT):
            result.status = sres.status
            result.reason = f"step {sres.index} ({sres.name}) exited {sres.exit_code}"
            break

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
    cross = [j for j in jobs if j.cross_os and j.ran]

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
            f"{host_os()} against a different target OS — indicative, not equivalent")
    return FULL_PASS, f"all {len(jobs)} job(s) ran here and passed"


# ── Orchestration ───────────────────────────────────────────────────────────

def run_ci(project_path, selector: str = "", allow_foreign: bool = False,
           only: list = None, timeout: int = DEFAULT_STEP_TIMEOUT,
           workflow: str = "") -> RunResult:
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

    workflows = [parse_workflow(p) for p in discover_workflows(project)]
    if workflow:
        workflows = [w for w in workflows if w.name == workflow]
    dag = build_dag([w for w in workflows if not w.error])

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
    else:
        chosen = {i.key for i in ordered}

    failed_jobs: set = set()        # job_ids whose failure blocks dependents
    results: list = []

    for inst in ordered:
        if inst.key not in chosen:
            results.append(JobResult(
                key=inst.key, job_id=inst.job_id, name=inst.name,
                workflow=inst.workflow, runs_on=inst.runs_on,
                status=DESELECTED, reason="not part of this run's selection"))
            continue

        if not inst.supported:
            results.append(JobResult(
                key=inst.key, job_id=inst.job_id, name=inst.name,
                workflow=inst.workflow, runs_on=inst.runs_on,
                status=UNSUPPORTED, reason="; ".join(inst.blockers)))
            failed_jobs.add((inst.workflow, inst.job_id))
            continue

        if inst.foreign_runner and not allow_foreign:
            results.append(JobResult(
                key=inst.key, job_id=inst.job_id, name=inst.name,
                workflow=inst.workflow, runs_on=inst.runs_on,
                status=FOREIGN, cross_os=True,
                reason=f"targets {inst.runs_on}; host is {host_os()}. "
                       "Re-run with allow_foreign to attempt it anyway."))
            failed_jobs.add((inst.workflow, inst.job_id))
            continue

        blocked = [n for n in inst.needs if (inst.workflow, n) in failed_jobs]
        if blocked:
            results.append(JobResult(
                key=inst.key, job_id=inst.job_id, name=inst.name,
                workflow=inst.workflow, runs_on=inst.runs_on,
                status=SKIPPED,
                reason=f"dependency did not pass: {', '.join(sorted(set(blocked)))}"))
            failed_jobs.add((inst.workflow, inst.job_id))
            continue

        job_result = _run_job(inst, project, run_dir, timeout)
        results.append(job_result)
        if job_result.status in (FAILED, TIMEOUT):
            failed_jobs.add((inst.workflow, inst.job_id))

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
