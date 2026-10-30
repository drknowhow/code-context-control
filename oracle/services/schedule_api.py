"""``/api/schedules/*`` — the Oracle's local surface over the scheduler.

GETs are open like the rest of the local Oracle API; mutations go through
``_local_write_guard`` (dashboard cookie or Discovery Bearer). The plain
functions below do the work on dicts so the mobile gateway and the Hub can
serve the same operations with their own auth and ``decided_by``.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request

from services import schedule_runner as runner
from services.cron_expr import preview, validate_when
from services.schedule_approvals import ApprovalError
from services.schedule_store import OPEN_RUN_STATUSES, RUN_STATUSES, RevConflict, ScheduleError

OUTPUT_DEFAULT_LIMIT = 500
OUTPUT_MAX_LIMIT = 2000

bp = Blueprint("schedule_api", __name__, url_prefix="/api/schedules")

_get_scheduler = lambda: None  # noqa: E731
_get_store = lambda: None  # noqa: E731
_get_approvals = lambda: None  # noqa: E731
_get_scanner = lambda: None  # noqa: E731
_get_cfg = lambda: {}  # noqa: E731


def configure(*, get_scheduler, get_store, get_approvals, get_scanner, get_cfg) -> None:
    global _get_scheduler, _get_store, _get_approvals, _get_scanner, _get_cfg
    _get_scheduler, _get_store, _get_approvals = get_scheduler, get_store, get_approvals
    _get_scanner, _get_cfg = get_scanner, get_cfg


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def registered_project(raw: str, scanner) -> str:
    """The registered project path matching ``raw``; ``ApiError`` 404 otherwise."""
    raw = (raw or "").strip()
    if not raw:
        raise ApiError("project_path is required", 400)
    if scanner is None:
        return raw
    try:
        target = os.path.normcase(str(Path(raw).resolve()))
    except (OSError, ValueError) as exc:
        raise ApiError("unknown project", 404) from exc
    for proj in scanner.discover():
        if not proj.get("has_c3"):
            continue
        try:
            if os.path.normcase(str(Path(proj["path"]).resolve())) == target:
                return str(Path(proj["path"]).resolve())
        except (OSError, ValueError):
            continue
    raise ApiError("unknown project", 404)


def defaults() -> dict:
    from oracle.services.scheduler import defaults_from_config
    return defaults_from_config(_get_cfg() or {})


def create_schedule(body: dict, *, scanner, by: str) -> dict:
    fields = dict(body or {})
    fields.pop("expected_rev", None)
    fields["project_path"] = registered_project(str(fields.get("project_path") or fields.pop("project", "")), scanner)
    try:
        return _get_store().create(fields, defaults=defaults(), created_by=by)
    except ScheduleError as exc:
        raise ApiError(str(exc), 400) from exc


def update_schedule(schedule_id: str, body: dict, *, scanner, by: str) -> dict:
    patch = dict(body or {})
    expected_rev = patch.pop("expected_rev", None)
    if "project_path" in patch or "project" in patch:
        patch["project_path"] = registered_project(str(patch.get("project_path") or patch.pop("project", "")), scanner)
    store = _get_store()
    if store.get(schedule_id) is None:
        raise ApiError(f"no schedule {schedule_id}", 404)
    try:
        return store.update(schedule_id, patch, expected_rev=expected_rev, defaults=defaults(), by=by)
    except RevConflict as exc:
        raise ApiError(str(exc), 409) from exc
    except ScheduleError as exc:
        raise ApiError(str(exc), 400) from exc


def run_schedule(schedule_id: str, body: dict, *, by: str) -> dict:
    """Queue a manual run; the next tick (whichever process holds the lock) dispatches it."""
    store = _get_store()
    if store.get(schedule_id) is None:
        raise ApiError(f"no schedule {schedule_id}", 404)
    bypass = bool((body or {}).get("bypass_approval", False))
    run = store.request_run(schedule_id, trigger="manual", by=by, approved=bypass)
    scheduler = _get_scheduler()
    if scheduler is not None:
        scheduler.kick()
    return run


def decide_approval(approval_id: str, body: dict, *, by: str) -> dict:
    decision = str((body or {}).get("decision") or "").strip().lower()
    note = str((body or {}).get("note") or "")
    approvals = _get_approvals()
    if approvals.get(approval_id) is None:
        raise ApiError(f"no approval {approval_id}", 404)
    try:
        row = approvals.decide(approval_id, decision, decided_by=by, note=note)
    except ApprovalError as exc:
        status = 400 if "decision must be" in str(exc) else 409
        raise ApiError(str(exc), status) from exc
    scheduler = _get_scheduler()
    if scheduler is not None:
        scheduler.kick()
    return row


def preview_when(body: dict) -> dict:
    try:
        when = validate_when((body or {}).get("when"))
    except ValueError as exc:
        raise ApiError(str(exc), 400) from exc
    count = max(1, min(int((body or {}).get("count") or 5), 20))
    fires = preview(when, datetime.now(timezone.utc), count)
    return {"when": when, "next": [f.isoformat() for f in fires]}


def run_output(run_id: str, after=0, limit=OUTPUT_DEFAULT_LIMIT, *, jobstore=None) -> dict:
    """One page of a run's stdout: the lines numbered ``n > after``.

    Reads ``schedule_runs/<run_id>.out`` once the run has one; before that the
    job's spool while it runs (``live``, complete lines only) or its kept
    output once it ended (``output_store``). A line keeps its number across
    the three. ``after`` and ``limit`` may be strings from a query;
    ``limit`` is clamped to 1..2000. Raises ``ApiError`` 404 for an unknown
    run and 400 for a non-integer ``after``/``limit``.
    """
    store = _get_store()
    run = store.get_run(str(run_id or ""))
    if run is None:
        raise ApiError(f"no run {run_id}", 404)
    after = _int_arg(after, 0, "after")
    limit = min(max(_int_arg(limit, OUTPUT_DEFAULT_LIMIT, "limit"), 1), OUTPUT_MAX_LIMIT)
    status = run.get("status")
    out = {"run_id": run["run_id"], "status": status, "open": status in OPEN_RUN_STATUSES,
           "job_id": run.get("job_id") or None, "format": _run_format(store, run),
           "source": "none", "lines": [], "next": after, "total": 0, "dropped": 0,
           "stderr_tail": str(run.get("stderr_tail") or "")}

    def page(path, source: str, first_n: int = 1) -> bool:
        try:
            lines, total = runner.page_lines(path, first_n=first_n, after=after, limit=limit,
                                             complete_only=source == "live")
        except OSError:
            return False
        out.update(source=source, lines=lines, total=total, next=lines[-1]["n"] if lines else after)
        return True

    archive = store.runs_dir / f"{run['run_id']}.out"
    if run.get("output_lines") is not None and archive.is_file():
        dropped = int(run.get("output_dropped") or 0)
        if page(archive, "archive", dropped + 1):
            out["dropped"] = dropped
            return out
    job_id = run.get("job_id")
    if not job_id:
        return out
    jobs = jobstore if jobstore is not None else _jobstore()
    job = jobs.find(job_id)
    if job is not None and not job.terminal and job.spool.get("stdout"):
        if page(job.spool["stdout"], "live"):
            return out
        job = jobs.find(job_id)          # the spool was promoted between the two reads
    if job is not None and job.terminal:
        path = runner.job_stream_path(jobs, job, "stdout")
        if path is not None:
            page(path, "output_store")
    return out


def _int_arg(value, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{name} must be an integer", 400) from exc


def _run_format(store, run: dict) -> str:
    if run.get("output_format"):
        return str(run["output_format"])
    if run.get("status") in ("check_running", "check_skipped"):
        return "text"
    schedule = store.get(run.get("schedule_id") or "")
    return runner.output_format(schedule) if schedule else "text"


def _jobstore():
    jobstore = getattr(_get_scheduler(), "jobstore", None)
    if jobstore is not None:
        return jobstore
    from services.shell_jobs import JobStore
    return JobStore()


def _limit(default: int = 50, cap: int = 500) -> int:
    try:
        return max(1, min(int(request.args.get("limit") or default), cap))
    except ValueError:
        return default


def _status_arg():
    status = (request.args.get("status") or "").strip()
    if status and status not in RUN_STATUSES:
        raise ApiError(f"unknown status {status}", 400)
    return status or None


@bp.errorhandler(ApiError)
def _api_error(exc: ApiError):
    return jsonify({"error": str(exc)}), exc.status


@bp.route("", methods=["GET"])
def list_schedules():
    project = (request.args.get("project") or "").strip() or None
    rows = _get_store().list(project_path=project)
    return jsonify({"schedules": rows, "count": len(rows), "rev": _get_store().rev()})


@bp.route("", methods=["POST"])
def create():
    row = create_schedule(request.get_json(silent=True) or {}, scanner=_get_scanner(), by="oracle")
    return jsonify(row), 201


@bp.route("/status", methods=["GET"])
def status():
    scheduler = _get_scheduler()
    return jsonify(scheduler.status if scheduler else {"running": False})


@bp.route("/kick", methods=["POST"])
def kick():
    scheduler = _get_scheduler()
    if scheduler is None:
        raise ApiError("scheduler not running", 503)
    scheduler.kick()
    return jsonify({"kicked": True})


@bp.route("/preview", methods=["POST"])
def preview_route():
    return jsonify(preview_when(request.get_json(silent=True) or {}))


@bp.route("/runs", methods=["GET"])
def runs():
    rows = _get_store().runs(schedule_id=(request.args.get("schedule_id") or None), status=_status_arg(),
                             project_path=(request.args.get("project") or None), limit=_limit())
    return jsonify({"runs": rows, "count": len(rows)})


@bp.route("/runs/<run_id>", methods=["GET"])
def one_run(run_id: str):
    run = _get_store().get_run(run_id)
    if run is None:
        raise ApiError(f"no run {run_id}", 404)
    return jsonify(run)


@bp.route("/runs/<run_id>/output", methods=["GET"])
def one_run_output(run_id: str):
    return jsonify(run_output(run_id, after=request.args.get("after"), limit=request.args.get("limit")))


@bp.route("/approvals", methods=["GET"])
def approvals():
    status = (request.args.get("status") or "").strip() or None
    rows = _get_approvals().list(status=status, project_path=(request.args.get("project") or None),
                                 limit=_limit(100))
    return jsonify({"approvals": rows, "count": len(rows)})


@bp.route("/approvals/<approval_id>", methods=["POST"])
def decide(approval_id: str):
    return jsonify(decide_approval(approval_id, request.get_json(silent=True) or {}, by="oracle"))


@bp.route("/<schedule_id>", methods=["GET"])
def one(schedule_id: str):
    row = _get_store().get(schedule_id)
    if row is None:
        raise ApiError(f"no schedule {schedule_id}", 404)
    return jsonify(dict(row, rev=_get_store().rev()))


@bp.route("/<schedule_id>", methods=["PATCH"])
def update(schedule_id: str):
    return jsonify(update_schedule(schedule_id, request.get_json(silent=True) or {}, scanner=_get_scanner(),
                                   by="oracle"))


@bp.route("/<schedule_id>", methods=["DELETE"])
def delete(schedule_id: str):
    if not _get_store().delete(schedule_id, by="oracle"):
        raise ApiError(f"no schedule {schedule_id}", 404)
    return jsonify({"deleted": schedule_id})


@bp.route("/<schedule_id>/run", methods=["POST"])
def run_now(schedule_id: str):
    return jsonify(run_schedule(schedule_id, request.get_json(silent=True) or {}, by="oracle")), 202


@bp.route("/<schedule_id>/enable", methods=["POST"])
def enable(schedule_id: str):
    return jsonify(update_schedule(schedule_id, {"enabled": True}, scanner=_get_scanner(), by="oracle"))


@bp.route("/<schedule_id>/disable", methods=["POST"])
def disable(schedule_id: str):
    return jsonify(update_schedule(schedule_id, {"enabled": False}, scanner=_get_scanner(), by="oracle"))


@bp.route("/<schedule_id>/runs", methods=["GET"])
def schedule_runs(schedule_id: str):
    if _get_store().get(schedule_id) is None:
        raise ApiError(f"no schedule {schedule_id}", 404)
    rows = _get_store().runs(schedule_id=schedule_id, status=_status_arg(), limit=_limit())
    return jsonify({"runs": rows, "count": len(rows)})
