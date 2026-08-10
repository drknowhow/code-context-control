"""c3_ci — run the repository's real CI locally instead of pushing for feedback.

The agent-facing surface for AgentCI (docs/agent-ci.md). Output is written for
an agent's context budget: a job list rather than a log, structured failures
rather than a traceback dump, and a verdict line that can be acted on without
reading anything else.

The one thing this surface must never do is let a partial run read as a full
one. `FULL_CI_PASS` appears only when every job in the repository ran on this
host and passed; everything else is `PARTIAL_PASS` with the reason attached.
That distinction is the product.
"""
from __future__ import annotations

from services import ci_runner as cr
from services.ci_workflow import inspect_project

_MARK = {
    cr.PASSED: "PASS", cr.FAILED: "FAIL", cr.SKIPPED: "SKIP",
    cr.UNSUPPORTED: "UNSUP", cr.FOREIGN: "OTHER-OS", cr.TIMEOUT: "TIMEOUT",
    cr.DESELECTED: "-",
}

# Verdicts an agent may treat as "safe to push". Deliberately one item long.
_GREEN = (cr.FULL_PASS,)


def _verdict_line(run: dict) -> str:
    verdict = run.get("verdict", cr.FAIL)
    note = run.get("note", "")
    flag = "OK" if verdict in _GREEN else "!!"
    return f"[{flag}] {verdict} — {note}"


def _job_rows(jobs: list, include_deselected: bool = False) -> list:
    rows: list = []
    for job in jobs:
        status = job.get("status", "")
        if status == cr.DESELECTED and not include_deselected:
            continue
        mark = _MARK.get(status, status.upper())
        cross = " [cross-OS]" if job.get("cross_os") and status == cr.PASSED else ""
        ms = job.get("duration_ms") or 0
        timing = f"  {ms / 1000:.1f}s" if ms else ""
        line = f"  {mark:<9} {job.get('key', '?')}{cross}{timing}"
        reason = (job.get("reason") or "").strip()
        if reason and status != cr.PASSED:
            line += f"\n            {reason}"
        rows.append(line)
    return rows


def _failure_rows(jobs: list, limit: int = 25) -> list:
    rows: list = []
    for job in jobs:
        failures = job.get("failures") or []
        if not failures:
            continue
        rows.append(f"\n{job.get('key')}  ({job.get('parser') or 'unparsed'}):")
        for fail in failures[:limit]:
            where = fail.get("file", "")
            if where and fail.get("line"):
                where += f":{fail['line']}"
            bits = [b for b in (fail.get("rule"), where or fail.get("test", ""))
                    if b]
            head = "  " + (" ".join(bits) if bits else "")
            rows.append(f"{head}\n      {fail.get('message', '')}".rstrip())
            if fail.get("excerpt"):
                excerpt = "\n".join(
                    "      | " + ln
                    for ln in str(fail["excerpt"]).splitlines()[-12:])
                rows.append(excerpt)
    return rows


def handle_ci(action: str, job: str, run_id: str, allow_foreign: bool,
              workflow: str, tail: int, timeout: int, svc, finalize) -> str:
    """Route c3_ci actions."""
    project = str(svc.project_path)
    action = (action or "inspect").strip().lower()
    args = {"action": action}
    if job:
        args["job"] = job

    # ── inspect ──────────────────────────────────────────────────────────
    if action in ("", "inspect", "jobs"):
        data = inspect_project(project)
        if not data["workflows"]:
            return finalize(
                "c3_ci", args,
                "No GitHub workflows found (.github/workflows/*.yml). "
                "AgentCI reads existing workflow files; it does not define a "
                "second CI config.", "no workflows")

        lines = [f"Workflows in {project} (host {data['host_os']}):"]
        for wf in data["workflows"]:
            err = f"  ERROR: {wf['error']}" if wf["error"] else ""
            lines.append(f"  {wf['name']}  [{', '.join(wf['triggers']) or 'no triggers'}]"
                         f"  jobs={len(wf['jobs'])}{err}")

        lines.append("\nJob graph (dependency order):")
        by_key = {j["key"]: j for j in data["jobs"]}
        for key in data["order"]:
            j = by_key[key]
            needs = f"  needs={','.join(j['needs'])}" if j["needs"] else ""
            if not j["supported"]:
                state = "UNSUPPORTED"
            elif j["foreign_runner"]:
                state = f"other-OS ({j['runs_on']})"
            else:
                state = "runnable here"
            lines.append(f"  {key:<42} {state}{needs}")

        lines.append(
            f"\nRunnable on this host: {len(data['runnable'])} of {len(data['jobs'])}")
        if data["foreign"]:
            lines.append(
                f"  other-OS ({len(data['foreign'])}): these target a different "
                "runner. c3_ci(action='run', allow_foreign=true) attempts them "
                "anyway and labels the result cross-OS.")
        if data["unsupported"]:
            lines.append(f"  unsupported ({len(data['unsupported'])}):")
            for item in data["unsupported"][:8]:
                lines.append(f"    {item['key']}: {item['blockers'][0]}")
        if data["cycle"]:
            lines.append(f"  CYCLE: {data['cycle']}")
        if data["unknown_needs"]:
            for item in data["unknown_needs"][:5]:
                lines.append(
                    f"  BROKEN needs: {item['job']} -> '{item['missing_need']}' "
                    "does not exist")
        return finalize("c3_ci", args, "\n".join(lines),
                        f"{len(data['jobs'])} jobs, {len(data['runnable'])} runnable")

    # ── run / rerun ──────────────────────────────────────────────────────
    if action in ("run", "rerun"):
        only = None
        if action == "rerun":
            prior = cr.load_run(project, run_id)
            if not prior:
                return finalize("c3_ci", args,
                                "No previous run to rerun. Use action='run' first.",
                                "no prior run")
            only = prior.get("failed") or []
            if not only:
                return finalize(
                    "c3_ci", args,
                    f"Previous run {prior.get('run_id')} had no failed jobs "
                    f"({prior.get('verdict')}). Nothing to rerun.", "nothing failed")

        result = cr.run_ci(project, selector=job, allow_foreign=allow_foreign,
                           only=only, timeout=timeout or cr.DEFAULT_STEP_TIMEOUT,
                           workflow=workflow)
        run = result.to_dict()
        lines = [_verdict_line(run), f"run {run['run_id']}  host={run['host_os']}"]
        fp = run.get("fingerprint") or {}
        if fp.get("dirty"):
            lines.append(f"tree: {fp.get('branch', '?')} @ {(fp.get('sha') or '')[:8]} "
                         f"+{fp.get('dirty_files')} uncommitted file(s)")
        lines.append("")
        lines.extend(_job_rows(run["jobs"]))
        fails = _failure_rows(run["jobs"])
        if fails:
            lines.append("\nStructured failures:")
            lines.extend(fails)
            lines.append("\nFix, then: c3_ci(action='rerun') — reruns only what failed.")
        return finalize("c3_ci", args, "\n".join(lines),
                        f"{run['verdict']} ({run['run_id']})")

    # ── status ───────────────────────────────────────────────────────────
    if action == "status":
        run = cr.load_run(project, run_id)
        if not run:
            return finalize("c3_ci", args,
                            "No CI runs recorded for this project yet.", "no runs")
        lines = [_verdict_line(run),
                 f"run {run['run_id']}  started {run['started_at']}", ""]
        lines.extend(_job_rows(run["jobs"]))
        return finalize("c3_ci", args, "\n".join(lines), run["verdict"])

    # ── failures ─────────────────────────────────────────────────────────
    if action == "failures":
        run = cr.load_run(project, run_id)
        if not run:
            return finalize("c3_ci", args, "No CI runs recorded yet.", "no runs")
        rows = _failure_rows(run["jobs"])
        if not rows:
            return finalize(
                "c3_ci", args,
                f"No failures in run {run['run_id']} ({run['verdict']}).",
                "no failures")
        return finalize("c3_ci", args,
                        f"Failures in run {run['run_id']}:" + "\n".join(rows),
                        f"{len(rows)} failure block(s)")

    # ── logs ─────────────────────────────────────────────────────────────
    if action == "logs":
        run = cr.load_run(project, run_id)
        if not run:
            return finalize("c3_ci", args, "No CI runs recorded yet.", "no runs")
        if not job:
            keys = [j["key"] for j in run["jobs"] if j.get("log_path")]
            return finalize("c3_ci", args,
                            "Pass job=<key>. Jobs with logs:\n  "
                            + "\n  ".join(keys), "need a job")
        text = cr.read_log(project, run["run_id"], job, tail=tail or 200)
        if not text:
            return finalize("c3_ci", args,
                            f"No log for '{job}' in run {run['run_id']}.", "no log")
        return finalize("c3_ci", args,
                        f"{job} (last {tail or 200} lines):\n{text}", "log")

    # ── runs ─────────────────────────────────────────────────────────────
    if action == "runs":
        rows = cr.list_runs(project, limit=20)
        if not rows:
            return finalize("c3_ci", args, "No CI runs recorded yet.", "no runs")
        lines = ["Recent local CI runs:"]
        for row in rows:
            counts = row.get("counts") or {}
            tally = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            lines.append(f"  {row['run_id']}  {row['verdict']:<14} "
                         f"{row.get('started_at', '')[:19]}  {tally}")
        return finalize("c3_ci", args, "\n".join(lines), f"{len(rows)} runs")

    return finalize("c3_ci", args,
                    f"Unknown action '{action}'. Use: inspect, run, rerun, "
                    "status, failures, logs, runs.", "unknown action")
