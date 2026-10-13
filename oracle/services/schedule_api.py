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

from services.cron_expr import preview, validate_when
from services.schedule_approvals import ApprovalError
from services.schedule_store import RUN_STATUSES, RevConflict, ScheduleError

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
    if _get_store().get(schedule_id) is None:
        raise ApiError(f"no schedule {schedule_id}", 404)
    bypass = bool((body or {}).get("bypass_approval", False))
    scheduler = _get_scheduler()
    if scheduler is None:
        raise ApiError("scheduler not running", 503)
    return scheduler.run_now(schedule_id, bypass_approval=bypass, by=by)


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
