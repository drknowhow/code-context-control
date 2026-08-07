"""Mobile gateway API for the C3 companion app (``/api/mobile/*``).

One authenticated surface for a remote (non-loopback, non-browser) client:
the Discovery Bearer token is REQUIRED on every method, GETs included —
unlike the legacy Oracle GETs, which stay open to loopback browsers. The
phone is inherently remote, so this surface is never open.

Reads follow the ``ActivityReporter`` pattern: per-project ``.c3`` files are
read directly off disk, no per-project server involved. Mutations (PM board,
notification ack) mirror the hub's handlers over ``services.task_store`` /
``services.notifications`` — the same cross-process stores the hub and the
per-project UI already share.

Path safety: every project path in a request must resolve to a scanner-
registered project with a ``.c3`` dir (``_resolve_registered``). Stores are
never constructed on an unvalidated path — several of them mkdir on init,
and a leaked token must not be able to scatter ``.c3/`` dirs across disk.

Security surface (credentials + Access Guard), and what is deliberately absent
-----------------------------------------------------------------------------
This is the FIRST network-reachable surface for either subsystem; every other
one (``cli/server.py``, ``cli/hub_server.py``) is loopback-only, and their
confidentiality model is "only localhost can ask". That does not transfer, so
three invariants are enforced structurally and asserted by tests:

1. **No route ever returns a credential value.** Entries cross the wire only
   through ``credential_store.public_entry`` — an allowlist that cannot emit
   one — and this module never imports ``get_value``/``expand_templates``.
   ``credential_store.is_resolvable`` exists so it never needs to.
2. **No ``set_builtin_disabled`` route.** Disabling a builtin guard needs a
   keyring attestation and a typed confirmation; it stays a local, human
   operation on a machine someone is physically at.
3. **No ``/access/preview`` equivalent.** That route (``cli/server.py``)
   returns RAW file content and is explicitly human-only.

Also absent on purpose: bulk ``.env`` import (largest blast radius in the
vault, no phone affordance), denial-log clearing (a leaked token's first move
after being denied is to erase the evidence), and global-scope enforcement
(machine-wide, with a documented config-shadowing footgun). Adding any of
these back should be a deliberate decision, not an oversight.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from oracle.config import ORACLE_DIR, load_config
from oracle.services import api_auth, discovery_audit
from oracle.services.api_auth import extract_bearer
from oracle.services.tool_registry import _c3_version

# 2: /info reports EFFECTIVE capabilities (filtered by the config switches)
# rather than a static list, so a client can gate its UI instead of probing.
API_VERSION = 2

CAPABILITIES = [
    "feed", "projects", "health", "pm", "pm_events", "digest",
    "notifications_ack",
    "credentials", "credentials_write",
    "access", "access_write",
    "enforcement", "enforcement_write",
]

# capability -> config key that gates it. Absent from this map = always on.
_CAPABILITY_SWITCHES = {
    "credentials": "mobile_credentials_enabled",
    "credentials_write": "mobile_credentials_write",
    "access": "mobile_access_enabled",
    "access_write": "mobile_access_write",
    "enforcement": "mobile_access_enabled",
    "enforcement_write": "mobile_access_write",
}

FEED_TYPES = ("notification", "activity", "edit", "session_stat")

# Per-source, per-project scan cap for one feed page. Bounded tail reads:
# NotificationStore rotates, ActivityLog tail-reads a deque, EditLedger and
# session stats are line-capped here.
_SOURCE_CAP = 1000
_MAX_LIMIT = 200
_DEFAULT_LIMIT = 50

bp = Blueprint("mobile", __name__, url_prefix="/api/mobile")

# Wired by oracle_server at import time (configure) and startup (init_services).
_get_cfg = None       # () -> dict — live server config
_get_limiter = None   # () -> discovery_audit.RateLimiter
_scanner = None       # ProjectScanner
_checker = None       # HealthChecker
_reporter = None      # ActivityReporter


def configure(get_cfg, get_limiter) -> None:
    """Late-bind config + rate-limiter accessors (avoids a circular import)."""
    global _get_cfg, _get_limiter
    _get_cfg = get_cfg
    _get_limiter = get_limiter


def init_services(scanner, checker=None, reporter=None) -> None:
    """Attach the shared Oracle services. Called from ``_init_services``."""
    global _scanner, _checker, _reporter
    _scanner = scanner
    _checker = checker
    _reporter = reporter


def _cfg() -> dict:
    if _get_cfg is not None:
        return _get_cfg() or {}
    return load_config()


# ── Auth guard ────────────────────────────────────────────

@bp.before_request
def _mobile_auth_guard():
    """Bearer gate for the whole mobile surface — every method, GETs included.

    Deliberately stricter than ``_discovery_auth_guard``: there is no
    ``require_auth`` opt-out and no session-cookie fallback. Mutating methods
    additionally consume from the shared Discovery rate-limit bucket.
    """
    if request.method == "OPTIONS":
        return None  # CORS preflight
    if not _cfg().get("mobile_api_enabled", True):
        return jsonify({"error": "mobile API disabled"}), 404
    token = extract_bearer(request.headers.get("Authorization"))
    if not api_auth.verify(token):
        return jsonify({"error": "unauthorized"}), 401
    if request.method in ("POST", "PUT", "DELETE") and _get_limiter is not None:
        limiter = _get_limiter()
        allowed, retry_after = limiter.check(
            discovery_audit.caller_id(token, request.remote_addr))
        if not allowed:
            resp = jsonify({
                "error": "rate limited",
                "detail": f"exceeded {limiter.per_minute} calls/min",
                "retry_after": round(retry_after, 2),
            })
            resp.headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
            return resp, 429
    return None


# ── Capability + feature gating ───────────────────────────

def _capabilities() -> list:
    """Capabilities this server will actually serve, given its config.

    A disabled subsystem disappears from /info AND 404s, so a client gates its
    UI on the list instead of discovering the truth by collecting 404s."""
    cfg = _cfg()
    return [c for c in CAPABILITIES
            if cfg.get(_CAPABILITY_SWITCHES.get(c, ""), True)]


def _feature_or_404(cap: str):
    """Refusal response when the capability is switched off, else None.

    404 (not 403) deliberately: a disabled subsystem should be indistinguishable
    from a server too old to have it, so one client code path handles both."""
    if not _cfg().get(_CAPABILITY_SWITCHES.get(cap, ""), True):
        return jsonify({"error": f"{cap} not available on this Oracle"}), 404
    return None


# ── Security-mutation rate limiting ───────────────────────
# A second, tighter bucket stacked on the shared Discovery one. 60 rule
# deletions a minute is not a rate limit for this subsystem.

_sec_limiter = None
_sec_limiter_key: int | None = None


def _security_limiter():
    """Rebuild only when the configured budget changes, mirroring
    ``oracle_server._rate_limiter`` — a config reload must not silently reset
    every caller's usage."""
    global _sec_limiter, _sec_limiter_key
    key = int(_cfg().get("mobile_security_rate_limit_per_min", 12) or 0)
    if _sec_limiter is None or key != _sec_limiter_key:
        _sec_limiter = discovery_audit.RateLimiter(per_minute=key)
        _sec_limiter_key = key
    return _sec_limiter


def _security_gate():
    """Consume a security-budget token. Returns a 429 response, or None."""
    limiter = _security_limiter()
    if not limiter.enabled:
        return None
    token = extract_bearer(request.headers.get("Authorization"))
    allowed, retry_after = limiter.check(
        discovery_audit.caller_id(token, request.remote_addr))
    if allowed:
        return None
    resp = jsonify({
        "error": "rate limited",
        # Name the budget so a client backs off against the right one.
        "detail": f"exceeded {limiter.per_minute} security calls/min",
        "budget": "security",
        "retry_after": round(retry_after, 2),
    })
    resp.headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
    return resp, 429


# ── Request helpers ───────────────────────────────────────

def _confirmed(data: dict, expected: str) -> bool:
    """Typed-confirmation check, the wire analogue of the CLI's
    "Type the glob again to confirm".

    Honest about its scope: this stops a fat-finger and a blind replay, and it
    forces a client into a deliberate two-step UI. It is NOT a defense against
    a leaked token — an attacker holding the Bearer constructs this field
    trivially. The config switches are what resist that."""
    return str(data.get("confirm") or "") == expected


_TOO_BROAD = {"**", "*", "/**", "**/*", "**/**", "/*"}


def _too_broad(glob: str) -> bool:
    """Whether a glob is too broad to accept from a phone.

    ``{"glob": "**", "kind": "deny", "scope": "global"}`` deny-alls every
    project on the machine in one request. The CLI and desktop UI stay
    unrestricted — the difference between the surfaces is the point."""
    return str(glob or "").strip().replace("\\", "/") in _TOO_BROAD


def _svc_error(exc: Exception):
    """Service-layer exception -> response. Sibling of ``_pm_error``.

    ValueError carries a human-readable message from the guard/store (e.g. the
    corrupt-config refusal), so it is surfaced rather than swallowed."""
    if isinstance(exc, ValueError):
        return jsonify({"error": str(exc)}), 400
    return jsonify({"error": str(exc) or exc.__class__.__name__}), 500


def _gw_audit(tool: str, args: dict, status: str = "ok") -> None:
    """One line per security mutation in the GATEWAY's own audit log.

    The per-project logs answer "what happened to this project"; this is the
    only per-gateway trail, so "my token leaked — what did it touch?" does not
    require grepping every project on disk. ``args`` is fingerprinted by
    ``discovery_audit.record``, so nothing sensitive is written."""
    if not _cfg().get("api_audit_enabled", True):
        return
    try:
        token = extract_bearer(request.headers.get("Authorization"))
        discovery_audit.record(
            tool,
            caller=discovery_audit.caller_id(token, request.remote_addr),
            args=args,
            status=status,
        )
    except Exception:
        pass


def _caller() -> str:
    """Token fingerprint for audit lines — never the token itself.

    Mobile is the first surface where "which client did this" is a real
    question; the hub and per-project UI have exactly one caller."""
    try:
        return discovery_audit.caller_id(
            extract_bearer(request.headers.get("Authorization")),
            request.remote_addr)
    except Exception:
        return "unknown"


# ── Path validation ───────────────────────────────────────

def _resolve_registered(raw: str) -> Path | None:
    """Resolve a request path to a scanner-registered project with a .c3 dir.

    Case-insensitive on Windows (normcase both sides). Returns None for
    anything unregistered — callers turn that into a 404 without ever
    touching the filesystem at the requested location.
    """
    raw = (raw or "").strip()
    if not raw or _scanner is None:
        return None
    try:
        target = os.path.normcase(str(Path(raw).resolve()))
    except (OSError, ValueError):
        return None
    for proj in _scanner.discover():
        if not proj.get("has_c3"):
            continue
        try:
            candidate = os.path.normcase(str(Path(proj["path"]).resolve()))
        except (OSError, ValueError):
            continue
        if candidate == target:
            return Path(proj["path"]).resolve()
    return None


def _project_or_404(raw: str):
    """Returns (Path, None) or (None, error_response)."""
    if not (raw or "").strip():
        return None, (jsonify({"error": "project is required"}), 400)
    resolved = _resolve_registered(raw)
    if resolved is None:
        return None, (jsonify({"error": "unknown project"}), 404)
    return resolved, None


# ── Info ──────────────────────────────────────────────────

@bp.route("/info")
def mobile_info():
    # capabilities is the EFFECTIVE list (api_version 2+): a subsystem switched
    # off in config disappears from here and 404s, so a client hides that UI
    # rather than learning the truth from a pile of 404s.
    return jsonify({
        "api_version": API_VERSION,
        "c3_version": _c3_version(),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "capabilities": _capabilities(),
    })


# ── Projects overview ─────────────────────────────────────

def _last_activity(path: Path) -> str | None:
    """Max mtime across the feed-bearing .c3 files, as ISO-8601 UTC."""
    c3 = path / ".c3"
    latest = 0.0
    for rel in ("activity_log.jsonl", "edit_ledger.jsonl",
                "notifications.jsonl", "pm/pm.json"):
        try:
            latest = max(latest, (c3 / rel).stat().st_mtime)
        except OSError:
            continue
    if not latest:
        return None
    return datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()


@bp.route("/projects")
def mobile_projects():
    """Home-screen project list. Cheap by construction: counts come from
    ``open_task_count`` (no store) and ``get_pending_count``; health is a
    separate on-demand endpoint because ``HealthChecker`` parses every fact."""
    if _scanner is None:
        return jsonify({"error": "not initialized"}), 500
    from services.notifications import NotificationStore
    from services.task_store import open_task_count

    out = []
    for proj in _scanner.discover():
        row = {
            "path": proj.get("path", ""),
            "name": proj.get("name", ""),
            "tags": proj.get("tags", []),
            "active": bool(proj.get("active")),
            "has_c3": bool(proj.get("has_c3")),
            "fact_count": proj.get("fact_count", 0),
            "open_tasks": 0,
            "pending_notifications": 0,
            "last_activity": None,
        }
        if row["has_c3"] and row["path"]:
            p = Path(row["path"])
            row["open_tasks"] = open_task_count(row["path"])
            try:
                row["pending_notifications"] = \
                    NotificationStore(row["path"]).get_pending_count()
            except Exception:
                pass
            row["last_activity"] = _last_activity(p)
        out.append(row)
    return jsonify({"projects": out})


@bp.route("/projects/health")
def mobile_project_health():
    if _checker is None:
        return jsonify({"error": "not initialized"}), 500
    resolved, err = _project_or_404(request.args.get("project", ""))
    if err:
        return err
    return jsonify(_checker.check(str(resolved)))


# ── Merged feed ───────────────────────────────────────────

def _within(ts: str, since: str, before: str) -> bool:
    """Strict window: since-exclusive (watermark), before-exclusive (cursor).
    Lexicographic ISO compare — the date/time prefix dominates, so naive and
    ``+00:00`` timestamps window correctly against each other."""
    if not ts:
        return False
    if since and ts <= since:
        return False
    if before and ts >= before:
        return False
    return True


def _hash_id(*parts) -> str:
    canonical = "|".join(str(p) for p in parts)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()[:12]


def _feed_items_for(path: str, name: str, types: set, severities: set,
                    since: str, before: str) -> list[dict]:
    """One project's feed items. The caller guarantees ``.c3`` exists."""
    items: list[dict] = []

    def emit(item_id, ts, kind, data):
        items.append({"id": item_id, "ts": ts, "type": kind,
                      "project": path, "project_name": name, "data": data})

    if "notification" in types:
        try:
            from services.notifications import NotificationStore
            for e in NotificationStore(path).get_history(limit=_SOURCE_CAP):
                ts = e.get("last_seen") or e.get("timestamp", "")
                if severities and e.get("severity") not in severities:
                    continue
                if not _within(ts, since, before):
                    continue
                emit(f"ntf:{_hash_id(path, e.get('id'))}", ts,
                     "notification", e)
        except Exception:
            pass

    if "activity" in types:
        try:
            from services.activity_log import ActivityLog
            rows = ActivityLog(path).get_recent(
                limit=_SOURCE_CAP, since=since or None, until=before or None)
            for e in rows:
                ts = e.get("timestamp", "")
                if not _within(ts, since, before):
                    continue
                # Activity entries are flat ({timestamp, type, **data}) and
                # carry no id — hash the whole entry for a stable one.
                emit(f"act:{_hash_id(path, json.dumps(e, sort_keys=True, default=str))}",
                     ts, "activity", e)
        except Exception:
            pass

    if "edit" in types:
        try:
            from services.edit_ledger import EditLedger
            rows = EditLedger(path).get_history(
                since=since or None, limit=_SOURCE_CAP)
            for e in rows:
                ts = e.get("timestamp", "")
                if not _within(ts, since, before):
                    continue
                eid = e.get("id") or _hash_id(path, ts, e.get("file"))
                emit(f"edit:{_hash_id(path, eid)}", ts, "edit", e)
        except Exception:
            pass

    if "session_stat" in types:
        try:
            from services.session_manager import SessionManager
            for e in SessionManager(path).get_session_stats(_SOURCE_CAP):
                ts = e.get("ts", "")
                if not _within(ts, since, before):
                    continue
                emit(f"ses:{_hash_id(path, e.get('session_id'), ts)}",
                     ts, "session_stat", e)
        except Exception:
            pass

    return items


@bp.route("/feed")
def mobile_feed():
    """Merged cross-project feed, newest first.

    Query: types (csv of notification|activity|edit|session_stat, default
    all), project (optional single project), severity (csv, notifications
    only), since (exclusive watermark — "only newer than"), before
    (exclusive pagination cursor), limit (default 50, max 200).

    Response: {items, next_cursor, truncated}. Cursor is the ts of the last
    returned item; identical-ts items can repeat across pages, so clients
    dedup by item id.
    """
    if _scanner is None:
        return jsonify({"error": "not initialized"}), 500

    raw_types = (request.args.get("types") or "").strip()
    types = {t.strip() for t in raw_types.split(",") if t.strip()} \
        if raw_types else set(FEED_TYPES)
    unknown = types - set(FEED_TYPES)
    if unknown:
        return jsonify({"error": f"unknown types: {sorted(unknown)}"}), 400

    raw_sev = (request.args.get("severity") or "").strip()
    severities = {s.strip() for s in raw_sev.split(",") if s.strip()}

    since = (request.args.get("since") or "").strip()
    before = (request.args.get("before") or "").strip()
    try:
        limit = max(1, min(_MAX_LIMIT,
                           int(request.args.get("limit", _DEFAULT_LIMIT))))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT

    project_arg = (request.args.get("project") or "").strip()
    if project_arg:
        resolved, err = _project_or_404(project_arg)
        if err:
            return err
        targets = [{"path": str(resolved), "name": resolved.name}]
    else:
        targets = [{"path": p["path"], "name": p.get("name") or
                    Path(p["path"]).name}
                   for p in _scanner.discover()
                   if p.get("has_c3") and p.get("path")
                   and (Path(p["path"]) / ".c3").is_dir()]

    items: list[dict] = []
    for t in targets:
        items.extend(_feed_items_for(t["path"], t["name"], types, severities,
                                     since, before))

    items.sort(key=lambda i: i["ts"], reverse=True)
    truncated = len(items) > limit
    page = items[:limit]
    return jsonify({
        "items": page,
        "next_cursor": page[-1]["ts"] if truncated and page else None,
        "truncated": truncated,
    })


# ── Notifications ─────────────────────────────────────────

@bp.route("/notifications/ack", methods=["POST"])
def mobile_notifications_ack():
    """Body: {project, id} for one, or {project, all: true} for all."""
    data = request.get_json(silent=True) or {}
    resolved, err = _project_or_404(data.get("project", ""))
    if err:
        return err
    from services.notifications import NotificationStore
    store = NotificationStore(str(resolved))
    if data.get("all"):
        return jsonify({"acked": store.acknowledge_all()})
    nid = (data.get("id") or "").strip()
    if not nid:
        return jsonify({"error": "id or all is required"}), 400
    return jsonify({"acked": 1 if store.acknowledge(nid) else 0})


# ── PM board (mirrors the hub's /api/projects/pm handlers) ─

def _pm_store(path: Path):
    from services.task_store import TaskStore
    return TaskStore(str(path))


def _pm_audit(path: Path, entity: str, op: str, item_id: str = "") -> None:
    try:
        from services.activity_log import ActivityLog
        ActivityLog(str(path)).log("pm_write", {
            "entity": entity, "op": op, "id": item_id,
            "source": "oracle-mobile"})
    except Exception:
        pass


def _pm_error(res: dict):
    status = 409 if res.get("code") == "rev_conflict" else 400
    return jsonify(res), status


@bp.route("/pm")
def mobile_pm_get():
    """Board + notes for one project. Query: project, milestone?, tag?,
    include_archived? Response shape matches the hub so the two UIs stay
    aligned."""
    resolved, err = _project_or_404(request.args.get("project", ""))
    if err:
        return err
    store = _pm_store(resolved)
    board = store.board(
        milestone_id=(request.args.get("milestone") or None),
        tag=(request.args.get("tag") or None),
        include_archived=request.args.get("include_archived") == "1",
    )
    return jsonify({"path": str(resolved), "board": board,
                    "notes": store.list_notes(limit=100)})


@bp.route("/pm/task", methods=["POST", "PUT", "DELETE"])
def mobile_pm_task():
    data = request.get_json(silent=True) or {}
    resolved, err = _project_or_404(data.get("project", ""))
    if err:
        return err
    store = _pm_store(resolved)

    if request.method == "POST":
        res = store.create_task(
            data.get("title", ""), description=data.get("description", ""),
            status=data.get("status") or "backlog",
            priority=data.get("priority") or "p2",
            due_date=data.get("due_date") or None,
            tags=data.get("tags") or [], milestone_id=data.get("milestone_id"),
            links=data.get("links") or [], created_by="mobile")
        if "error" in res:
            return _pm_error(res)
        _pm_audit(resolved, "task", "create", res["id"])
        return jsonify({"created": True, "task": res}), 201

    if request.method == "PUT":
        task_id = (data.get("id") or "").strip()
        if not task_id:
            return jsonify({"error": "id is required"}), 400
        if data.get("restore"):
            res = store.restore_task(task_id, actor="mobile")
            if "error" in res:
                return _pm_error(res)
            _pm_audit(resolved, "task", "restore", res["id"])
            return jsonify({"updated": True, "task": res})
        if not (data.get("fields") or data.get("move")):
            return jsonify({"error": "fields or move required"}), 400
        res = store.mutate_task(task_id, fields=data.get("fields"),
                                move=data.get("move"),
                                expected_rev=data.get("expected_rev"),
                                actor="mobile")
        if "error" in res:
            return _pm_error(res)
        _pm_audit(resolved, "task", "update", res["id"])
        return jsonify({"updated": True, "task": res})

    # DELETE: archive by id
    task_id = (data.get("id") or "").strip()
    if not task_id:
        return jsonify({"error": "id is required"}), 400
    res = store.archive_task(task_id, actor="mobile")
    if "error" in res:
        return _pm_error(res)
    _pm_audit(resolved, "task", "archive", res["id"])
    return jsonify({"archived": True, "task": res})


@bp.route("/pm/milestone", methods=["POST", "PUT", "DELETE"])
def mobile_pm_milestone():
    data = request.get_json(silent=True) or {}
    resolved, err = _project_or_404(data.get("project", ""))
    if err:
        return err
    store = _pm_store(resolved)

    if request.method == "POST":
        res = store.create_milestone(data.get("name", ""),
                                     description=data.get("description", ""),
                                     target_date=data.get("target_date") or None)
        if "error" in res:
            return _pm_error(res)
        _pm_audit(resolved, "milestone", "create", res["id"])
        return jsonify({"created": True, "milestone": res}), 201

    ms_id = (data.get("id") or "").strip()
    if not ms_id:
        return jsonify({"error": "id is required"}), 400

    if request.method == "PUT":
        res = store.update_milestone(ms_id, expected_rev=data.get("expected_rev"),
                                     **(data.get("fields") or {}))
        if "error" in res:
            return _pm_error(res)
        _pm_audit(resolved, "milestone", "update", res["id"])
        return jsonify({"updated": True, "milestone": res})

    res = store.archive_milestone(ms_id)  # DELETE = archive + detach tasks
    if "error" in res:
        return _pm_error(res)
    _pm_audit(resolved, "milestone", "archive", res["id"])
    return jsonify({"archived": True, "milestone": res})


@bp.route("/pm/note", methods=["POST", "PUT", "DELETE"])
def mobile_pm_note():
    data = request.get_json(silent=True) or {}
    resolved, err = _project_or_404(data.get("project", ""))
    if err:
        return err
    store = _pm_store(resolved)

    if request.method == "POST":
        res = store.add_note(data.get("text", ""), kind=data.get("kind") or "note",
                             tags=data.get("tags") or [],
                             task_id=data.get("task_id"), author="mobile")
        if "error" in res:
            return _pm_error(res)
        _pm_audit(resolved, "note", "create", res["id"])
        return jsonify({"created": True, "note": res}), 201

    note_id = (data.get("id") or "").strip()
    if not note_id:
        return jsonify({"error": "id is required"}), 400

    if request.method == "PUT":
        res = store.update_note(note_id, expected_rev=data.get("expected_rev"),
                                **(data.get("fields") or {}))
        if "error" in res:
            return _pm_error(res)
        _pm_audit(resolved, "note", "update", res["id"])
        return jsonify({"updated": True, "note": res})

    res = store.archive_note(note_id, actor="mobile")
    if "error" in res:
        return _pm_error(res)
    _pm_audit(resolved, "note", "archive", res["id"])
    return jsonify({"archived": True, "note": res})


@bp.route("/pm/link", methods=["POST"])
def mobile_pm_link():
    data = request.get_json(silent=True) or {}
    resolved, err = _project_or_404(data.get("project", ""))
    if err:
        return err
    task_id = (data.get("id") or "").strip()
    link = data.get("link") or {}
    op = (data.get("op") or "add").strip()
    if not task_id or not link.get("type") or not link.get("ref"):
        return jsonify({"error": "id and link {type, ref} are required"}), 400
    if op not in ("add", "remove"):
        return jsonify({"error": "op must be add|remove"}), 400
    store = _pm_store(resolved)
    if op == "add":
        res = store.add_link(task_id, link["type"], link["ref"],
                             label=link.get("label", ""))
    else:
        res = store.remove_link(task_id, link["type"], link["ref"])
    if "error" in res:
        return _pm_error(res)
    _pm_audit(resolved, "link", op, res["id"])
    return jsonify({"task": res})


@bp.route("/pm/events")
def mobile_pm_events():
    """PM mutation history, newest first. Query: project, entity?, id?, op?,
    limit?"""
    resolved, err = _project_or_404(request.args.get("project", ""))
    if err:
        return err
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"path": str(resolved), "events": _pm_store(resolved).history(
        entity=(request.args.get("entity") or None),
        item_id=(request.args.get("id") or None),
        op=(request.args.get("op") or None),
        limit=limit)})


# ── Digest ────────────────────────────────────────────────

@bp.route("/digest")
def mobile_digest():
    """Bearer-gated digest. ``?latest=1`` serves the last scheduled digest;
    otherwise builds one on demand (date/since/until/project params)."""
    if request.args.get("latest") == "1":
        latest = ORACLE_DIR / "activity_digests" / "latest.json"
        try:
            if latest.is_file():
                return Response(latest.read_text(encoding="utf-8"),
                                mimetype="application/json")
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"digest": None, "generated_at": None})
    if _reporter is None:
        return jsonify({"error": "not initialized"}), 500
    project_arg = (request.args.get("project") or "").strip()
    if project_arg:
        resolved, err = _project_or_404(project_arg)
        if err:
            return err
        project_arg = str(resolved)
    return jsonify(_reporter.report(
        date=request.args.get("date", ""),
        since=request.args.get("since", ""),
        until=request.args.get("until", ""),
        project_path=project_arg,
    ))


# ── Credentials ───────────────────────────────────────────
# Write-only wire contract, mobile edition. Values go IN over POST and are
# never returned by any route here: entries are serialized ONLY through
# credential_store.public_entry, and this module has no access to plaintext at
# all (see the module docstring, invariant 1).

def _resolve_cred_target(raw_project: str, scope: str, *, mutation: bool):
    """Resolve a credentials/vault request to ``(project, store_path, error)``.

    The safety property the mobile surface must keep is not "only registered
    paths" — it is **the write location is never derived from the request**.
    Registered-path validation satisfies that for project scope; for global
    scope a server-side constant satisfies it more strongly, so the client's
    ``project`` field is never consulted for the location, only retained as the
    audit target.

    Global mutations additionally require ``~/.c3`` to already exist. This
    surface may EDIT the shared vault but never CREATE it, so the set of paths
    it can bring a ``.c3`` dir into being under stays exactly the set of
    registered projects — unchanged by this feature.
    """
    from services import credential_store as cred_store
    if scope not in ("project", "global"):
        return None, "", (jsonify(
            {"error": "scope must be 'project' or 'global'"}), 400)

    if scope == "global":
        home = cred_store.global_base()
        if home is None:
            return None, "", (jsonify(
                {"error": "global scope unresolvable (no home dir)"}), 500)
        project = None
        if raw_project:
            # Retained only for the audit trail; an invalid one is still a 404
            # so a bad path never silently succeeds against the global vault.
            project, err = _project_or_404(raw_project)
            if err:
                return None, "", err
        if mutation and not (home / ".c3").is_dir():
            return None, "", (jsonify(
                {"error": "not initialized", "needs_init": True}), 409)
        return project, str(home), None

    project, err = _project_or_404(raw_project)
    if err:
        return None, "", err
    # No needs_init branch here, unlike the hub: _resolve_registered already
    # requires has_c3, so an uninitialized project 404s before reaching this.
    return project, str(project), None


def _cred_audit(action: str, name: str, scope: str, project,
                *, confirmed: bool = False) -> None:
    """Names only — never values. Failure-safe.

    Mirrors ``hub_server._hub_cred_audit`` with two mobile additions: ``caller``
    (the token fingerprint — the first surface with more than one possible
    client) and ``confirmed`` (whether a typed confirmation was supplied)."""
    detail = {"kind": "creds", "action": action, "name": name,
              "scope": scope, "via": "oracle-mobile", "caller": _caller(),
              "confirmed": bool(confirmed)}
    if project is not None:
        try:
            from services.activity_log import ActivityLog
            ActivityLog(str(project)).log("cred_action", dict(detail))
        except Exception:
            pass
        try:
            from services.edit_ledger import EditLedger
            EditLedger(str(project)).log_edit(
                file=f"cred://{name}", change_type=f"cred_{action}",
                summary=f"{action} {name} ({scope}) via mobile",
                tags=["creds", action], detail=dict(detail))
        except Exception:
            pass
    if scope == "global" or project is None:
        # A global-vault edit with no project context would otherwise leave no
        # trace anywhere.
        try:
            from services import credential_store as cred_store
            from services.activity_log import ActivityLog
            home = cred_store.global_base()
            if home is not None:
                ActivityLog(str(home)).log("cred_action", dict(detail))
        except Exception:
            pass


def _cred_scope_args(source: dict) -> tuple[str, str]:
    """(project, scope) from a request body or args, defaulting scope to the
    one implied by whether a project was named."""
    raw_project = str(source.get("project") or "").strip()
    scope = str(source.get("scope") or "").strip().lower()
    if not scope:
        scope = "project" if raw_project else "global"
    return raw_project, scope


@bp.route("/credentials", methods=["GET"])
def mobile_credentials_list():
    """Registry for one project (merged: global entries + project shadows) or
    the global vault. Metadata, usage and shadow flags only."""
    gate = _feature_or_404("credentials")
    if gate:
        return gate
    from services import credential_store as cred_store
    raw_project, scope = _cred_scope_args(request.args)
    project, store_path, err = _resolve_cred_target(
        raw_project, scope, mutation=False)
    if err:
        return err
    only = str(request.args.get("only") or "").strip().lower()
    usage = cred_store.read_usage_state(store_path)
    entries = []
    for name, entry in cred_store.list_entries(store_path).items():
        if only in ("project", "global") and entry.get("scope") != only:
            continue
        entries.append(cred_store.public_entry(name, entry, usage=usage))
    return jsonify({
        "target": scope,
        "path": store_path,
        "global_base": str(cred_store.global_base() or ""),
        "entries": entries,
    })


@bp.route("/credentials-overview", methods=["GET"])
def mobile_credentials_overview():
    """Cross-project inventory: the global vault plus each registered
    project's project-scoped entries, with shadow info BOTH ways.

    The reciprocal (``shadows_global`` on project rows, ``shadowed_in`` on
    global ones) is only derivable here, which is why a client renders from
    this rather than from repeated per-project calls.

    Hyphenated, not ``/credentials/overview``: credential names may not contain
    a hyphen (``_NAME_RE``) but MAY be the literal "overview", and a static
    sub-path would make that entry unreachable through ``/credentials/<name>``.
    """
    gate = _feature_or_404("credentials")
    if gate:
        return gate
    from services import credential_store as cred_store
    if _scanner is None:
        return jsonify({"error": "not initialized"}), 500
    home = cred_store.global_base()
    global_entries = cred_store.list_entries(str(home)) if home else {}
    shadowed_in = {name: [] for name in global_entries}
    projects_out = []
    for proj in _scanner.discover():
        if not proj.get("has_c3"):
            continue
        ppath = str(proj.get("path") or "")
        row = {"name": proj.get("name") or "", "path": ppath,
               "initialized": True, "error": None, "entries": []}
        try:
            usage = cred_store.read_usage_state(ppath)
            for name, entry in cred_store.list_entries(ppath).items():
                if entry.get("scope") != "project":
                    continue
                row["entries"].append(cred_store.public_entry(
                    name, entry, usage=usage,
                    shadows_global=name in global_entries))
                if name in shadowed_in:
                    shadowed_in[name].append(
                        {"name": row["name"], "path": ppath})
        except Exception as e:  # per-row isolation — one bad project must not
            row["error"] = str(e)  # blank the whole inventory
        projects_out.append(row)
    global_usage = cred_store.read_usage_state(str(home)) if home else {}
    global_out = [
        {**cred_store.public_entry(name, entry, usage=global_usage),
         "shadowed_in": shadowed_in.get(name, [])}
        for name, entry in global_entries.items()
    ]
    return jsonify({
        "global": {"entries": global_out, "path": str(home or "")},
        "projects": projects_out,
    })


@bp.route("/credentials/<name>", methods=["GET"])
def mobile_credentials_get(name):
    """One resolved entry, with its owning scope. Metadata only."""
    gate = _feature_or_404("credentials")
    if gate:
        return gate
    from services import credential_store as cred_store
    raw_project, scope = _cred_scope_args(request.args)
    project, store_path, err = _resolve_cred_target(
        raw_project, scope, mutation=False)
    if err:
        return err
    entry = cred_store.get_entry(name, project_path=store_path)
    if not entry:
        return jsonify({"error": f"no credential named '{name}'"}), 404
    usage = cred_store.read_usage_state(store_path)
    return jsonify({"entry": cred_store.public_entry(name, entry, usage=usage)})


@bp.route("/credentials/<name>/check", methods=["POST"])
def mobile_credentials_check(name):
    """Resolvability probe — returns a fingerprint, never the value.

    POST rather than GET on purpose: the fingerprint is computed live from the
    decoded value (it is deliberately never persisted, because a stored one
    would be an offline guessing oracle for a weak secret), so this belongs in
    the security rate-limit budget rather than being freely pollable."""
    gate = _feature_or_404("credentials")
    if gate:
        return gate
    limited = _security_gate()
    if limited:
        return limited
    from services import credential_store as cred_store
    data = request.get_json(silent=True) or {}
    raw_project, scope = _cred_scope_args(data)
    project, store_path, err = _resolve_cred_target(
        raw_project, scope, mutation=False)
    if err:
        return err
    entry = cred_store.get_entry(name, project_path=store_path)
    if not entry:
        return jsonify({"error": f"no credential named '{name}'"}), 404
    return jsonify({
        "name": name,
        "scope": entry["scope"],
        "storage": entry.get("storage", "keyring"),
        # is_resolvable, not `get_value(...) is not None` — this module must
        # stay structurally free of plaintext accessors (invariant 1).
        "resolvable": cred_store.is_resolvable(name, project_path=store_path),
        "fingerprint": cred_store.fingerprint(name, project_path=store_path),
    })


@bp.route("/credentials", methods=["POST"])
def mobile_credentials_set():
    """Create/update an entry. ``value`` omitted = metadata-only update,
    touching ONLY the keys present in the payload.

    Raising ``agent_readable`` is the one operation here that makes a stored
    secret readable into model context, so it is asymmetric: lowering is free,
    raising needs a typed confirmation AND the (default-off)
    ``mobile_creds_agent_readable_raise`` switch."""
    gate = _feature_or_404("credentials") or _feature_or_404("credentials_write")
    if gate:
        return gate
    limited = _security_gate()
    if limited:
        return limited
    from services import credential_store as cred_store
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    raw_project, scope = _cred_scope_args(data)
    project, store_path, err = _resolve_cred_target(
        raw_project, scope, mutation=True)
    if err:
        return err

    value = str(data.get("value") or "")
    ctype = str(data.get("type") or data.get("ctype") or "token")
    wants_readable = bool(data.get("agent_readable"))
    existing = cred_store.get_entry(name, project_path=store_path)
    raising = wants_readable and not (existing or {}).get("agent_readable")
    if raising:
        if not _cfg().get("mobile_creds_agent_readable_raise", False):
            return jsonify({
                "error": "raising agent_readable is disabled on this gateway",
                "detail": "enable mobile_creds_agent_readable_raise, or set it "
                          "with `c3 creds set --agent-readable` on the desktop.",
            }), 403
        if not _confirmed(data, name):
            return jsonify({
                "error": "agent_readable makes this value readable by the agent",
                "needs_confirmation": True, "confirm_with": name,
            }), 400

    try:
        if value:
            entry = cred_store.set_credential(
                name, value, scope=scope, project_path=store_path, ctype=ctype,
                description=str(data.get("description") or ""),
                env_var=str(data.get("env_var") or ""),
                agent_readable=wants_readable,
                inject=bool(data.get("inject")))
        else:
            fields = {}
            for key in ("description", "env_var"):
                if key in data:
                    fields[key] = str(data[key] or "")
            for key in ("agent_readable", "inject"):
                if key in data:
                    fields[key] = bool(data[key])
            if "type" in data or "ctype" in data:
                fields["type"] = ctype
            entry = cred_store.update_metadata(
                name, scope=scope, project_path=store_path, **fields)
    except cred_store.CredentialError as exc:
        return jsonify({"error": str(exc)}), 400
    except (ValueError, RuntimeError) as exc:
        return _svc_error(exc)

    op = "set" if value else "update"
    _cred_audit(op, name, scope, project, confirmed=raising)
    _gw_audit(f"mobile.creds.{op}", {"name": name, "scope": scope})
    return jsonify({
        "op": op,
        "entry": cred_store.public_entry(name, {**entry, "scope": scope}),
    })


@bp.route("/credentials/<name>", methods=["DELETE"])
def mobile_credentials_delete(name):
    """Delete an entry (value + registry). Irreversible: the value cannot be
    read back from any surface, so a client should require typed confirmation
    before calling this."""
    gate = _feature_or_404("credentials") or _feature_or_404("credentials_write")
    if gate:
        return gate
    limited = _security_gate()
    if limited:
        return limited
    from services import credential_store as cred_store
    data = request.get_json(silent=True) or {}
    source = data if data else request.args
    raw_project, scope = _cred_scope_args(source)
    explicit_scope = bool(str(source.get("scope") or "").strip())
    project, store_path, err = _resolve_cred_target(
        raw_project, scope, mutation=True)
    if err:
        return err
    if not explicit_scope:
        entry = cred_store.get_entry(name, project_path=store_path)
        if entry:
            scope = entry.get("scope") or scope
    try:
        removed = cred_store.delete_credential(
            name, scope=scope, project_path=store_path)
    except cred_store.CredentialError as exc:
        return jsonify({"error": str(exc)}), 400
    if removed:
        _cred_audit("delete", name, scope, project)
        _gw_audit("mobile.creds.delete", {"name": name, "scope": scope})
    return jsonify({"removed": bool(removed), "scope": scope})


# ── Access Guard ──────────────────────────────────────────
# Path policy. Kept on its own routes from /enforcement below, because path
# policy is a security boundary and tool discipline is a workflow preference —
# the CLI and desktop UI deliberately keep them apart and this must not blur it.

def _access_audit(action: str, glob: str, kind: str, scope: str, project,
                  *, confirmed: bool = False) -> None:
    """Globs only — never file content. Failure-safe.

    A global-scope rule affects every project on the machine, so it is also
    recorded in ~/.c3 rather than only under whichever project the phone
    happened to have open."""
    detail = {"kind": "access", "action": action, "glob": glob,
              "rule_kind": kind, "scope": scope, "via": "oracle-mobile",
              "caller": _caller(), "confirmed": bool(confirmed)}
    if project is not None:
        try:
            from services.activity_log import ActivityLog
            ActivityLog(str(project)).log("access_action", dict(detail))
        except Exception:
            pass
        try:
            from services.edit_ledger import EditLedger
            EditLedger(str(project)).log_edit(
                file=f"access://{glob}", change_type=f"access_{action}",
                summary=f"{action} {kind} rule {glob} ({scope}) via mobile",
                tags=["access", action], detail=dict(detail))
        except Exception:
            pass
    if scope == "global":
        try:
            from services import credential_store as cred_store
            from services.activity_log import ActivityLog
            home = cred_store.global_base()
            if home is not None:
                ActivityLog(str(home)).log("access_action", dict(detail))
        except Exception:
            pass


def _access_write_gate(scope: str, data: dict, *, confirm_for: str = ""):
    """Shared refusal path for access mutations: capability, rate budget,
    global-scope switch, and typed confirmation. Returns a response or None."""
    gate = _feature_or_404("access") or _feature_or_404("access_write")
    if gate:
        return gate
    limited = _security_gate()
    if limited:
        return limited
    if scope == "global" and not _cfg().get("mobile_access_global_scope", False):
        return jsonify({
            "error": "global-scope access rules are disabled on this gateway",
            "detail": "a global rule applies to every project on this machine; "
                      "enable mobile_access_global_scope, or use `c3 access` "
                      "on the desktop.",
        }), 403
    if confirm_for and not _confirmed(data, confirm_for):
        return jsonify({
            "error": "removing a rule weakens protection",
            "needs_confirmation": True, "confirm_with": confirm_for,
        }), 400
    return None


@bp.route("/access", methods=["GET"])
def mobile_access_list():
    """Rule registry for a project: builtin + global + project, the coverage
    matrix, the mask preset catalog, and mask activation status."""
    gate = _feature_or_404("access")
    if gate:
        return gate
    from services import access_guard, mask_activation
    resolved, err = _project_or_404(request.args.get("project") or "")
    if err:
        return err
    path = str(resolved)
    scopes = access_guard.list_rules(path)
    corrupt = [s for s in ("global", "project")
               if (scopes.get(s) or {}).get("corrupt")]
    from services import credential_store as cred_store
    return jsonify({
        "path": path,
        "scopes": scopes,
        "corrupt": corrupt,
        "coverage": access_guard.COVERAGE_MATRIX,
        "presets": access_guard.preset_catalog(),
        "mask": mask_activation.status(path),
        "mask_summary": mask_activation.summary_line(path),
        # Which vault the global rules actually live in. enforcement_policy
        # honors C3_HOME while access_guard uses Path.home(), so a client that
        # displays this makes any mismatch visible instead of silent.
        "global_base": str(cred_store.global_base() or ""),
    })


@bp.route("/access/check", methods=["POST"])
def mobile_access_check():
    """Path probe: verdict + matched rule + the exact refusal string.

    POST rather than GET because ``canonicalize`` walks parents calling
    ``exists()`` on any absolute path the client names — no content is read,
    but it is an unbounded filesystem-probing primitive, so it belongs in the
    security rate budget."""
    gate = _feature_or_404("access")
    if gate:
        return gate
    limited = _security_gate()
    if limited:
        return limited
    from services import access_guard
    data = request.get_json(silent=True) or {}
    resolved, err = _project_or_404(data.get("project") or "")
    if err:
        return err
    path = str(data.get("path") or "").strip()
    op = str(data.get("op") or "read").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    if len(path) > 512:
        return jsonify({"error": "path too long"}), 400
    if op not in ("read", "write", "create", "delete"):
        return jsonify({"error": f"unknown op '{op}' — expected "
                                 "read|write|create|delete"}), 400
    denial = access_guard.check(path, op, str(resolved))
    if denial is None:
        # Echo the client's own string back, never the resolved canonical path.
        return jsonify({"path": path, "op": op, "verdict": "allowed",
                        "rule": "", "scope": "", "refusal": ""})
    return jsonify({
        "path": path, "op": op,
        "verdict": "read_only" if denial.kind == "read_only" else "denied",
        "rule": denial.rule, "scope": denial.scope, "reason": denial.reason,
        "refusal": access_guard.refusal(denial, path, op),
    })


@bp.route("/access/rule", methods=["POST"])
def mobile_access_rule_add():
    """Add a deny or read_only rule. Tightening — no confirmation needed."""
    from services import access_guard
    data = request.get_json(silent=True) or {}
    scope = str(data.get("scope") or "project").strip()
    gate = _access_write_gate(scope, data)
    if gate:
        return gate
    resolved, err = _project_or_404(data.get("project") or "")
    if err:
        return err
    glob = str(data.get("glob") or "")
    kind = str(data.get("kind") or "").strip()
    if _too_broad(glob):
        return jsonify({
            "error": f"'{glob}' is too broad to set from the mobile gateway",
            "detail": "a rule this sweeping should be set locally with "
                      "`c3 access add`, where its blast radius is visible.",
        }), 400
    try:
        result = access_guard.set_rule(glob, kind, scope, str(resolved))
    except ValueError as exc:
        return _svc_error(exc)
    if result["added"]:
        _access_audit("add", result["glob"], kind, scope, resolved)
        _gw_audit("mobile.access.rule_add",
                  {"glob": result["glob"], "kind": kind, "scope": scope})
    return jsonify({"rule": result})


@bp.route("/access/rule", methods=["DELETE"])
def mobile_access_rule_remove():
    """Remove a rule. Loosening — deny rules require a typed confirmation."""
    from services import access_guard
    data = request.get_json(silent=True) or {}
    scope = str(data.get("scope") or "project").strip()
    glob = str(data.get("glob") or "")
    kind = str(data.get("kind") or "").strip()
    # A read_only rule downgrades to "writable"; a deny rule downgrades to
    # "fully visible" — only the latter is worth the friction.
    gate = _access_write_gate(
        scope, data, confirm_for=glob if kind == "deny" else "")
    if gate:
        return gate
    resolved, err = _project_or_404(data.get("project") or "")
    if err:
        return err
    try:
        result = access_guard.remove_rule(glob, kind, scope, str(resolved))
    except ValueError as exc:
        return _svc_error(exc)
    if result["removed"]:
        _access_audit("remove", result["glob"], kind, scope, resolved,
                      confirmed=kind == "deny")
        _gw_audit("mobile.access.rule_remove",
                  {"glob": result["glob"], "kind": kind, "scope": scope})
    return jsonify(result)


@bp.route("/access/mask", methods=["POST"])
def mobile_access_mask_add():
    """Add/replace a mask rule. The rule lands on disk immediately but
    protection is NOT real until activation has purged derived artifacts, so
    the response carries the resulting (stale) status."""
    from services import access_guard, mask_activation
    data = request.get_json(silent=True) or {}
    scope = str(data.get("scope") or "project").strip()
    gate = _access_write_gate(scope, data)
    if gate:
        return gate
    resolved, err = _project_or_404(data.get("project") or "")
    if err:
        return err
    glob = str(data.get("glob") or "")
    if _too_broad(glob):
        return jsonify({
            "error": f"'{glob}' is too broad to mask from the mobile gateway",
        }), 400
    try:
        result = access_guard.set_mask_rule(
            glob, str(data.get("preset") or ""), data.get("params") or {},
            scope, str(resolved))
    except ValueError as exc:
        return _svc_error(exc)
    if result["added"] or result["replaced"]:
        _access_audit("mask-add", result["glob"], result["preset"],
                      result["scope"], resolved)
        _gw_audit("mobile.access.mask_add",
                  {"glob": result["glob"], "preset": result["preset"]})
    return jsonify({"rule": result, "mask": mask_activation.status(str(resolved))})


@bp.route("/access/mask", methods=["DELETE"])
def mobile_access_mask_remove():
    """Remove a mask rule — loosening, so typed confirmation."""
    from services import access_guard, mask_activation
    data = request.get_json(silent=True) or {}
    scope = str(data.get("scope") or "project").strip()
    glob = str(data.get("glob") or "")
    gate = _access_write_gate(scope, data, confirm_for=glob)
    if gate:
        return gate
    resolved, err = _project_or_404(data.get("project") or "")
    if err:
        return err
    try:
        result = access_guard.remove_mask_rule(glob, scope, str(resolved))
    except ValueError as exc:
        return _svc_error(exc)
    if result["removed"]:
        _access_audit("mask-remove", result["glob"], "mask", scope, resolved,
                      confirmed=True)
        _gw_audit("mobile.access.mask_remove", {"glob": result["glob"]})
    return jsonify({**result, "mask": mask_activation.status(str(resolved))})


# Single-flight: two concurrent activations race on the state file and the
# view mirror. The rate limiter does not prevent this — one token buys minutes
# of work — so the lock is the actual guard.
_activation_lock = threading.Lock()


@bp.route("/access/mask/activate", methods=["POST"])
def mobile_access_mask_activate():
    """Run the mask activation transaction (purge -> build -> validate).

    The most destructive button on this surface. It wipes the compression
    cache, the search index and MAP.md, and on FIRST activation irreversibly
    purges memory facts whose provenance predates masking — so first activation
    needs a typed confirmation, ``rebuild_index`` is never accepted from the
    wire, and concurrent runs are refused rather than queued."""
    from services import mask_activation
    data = request.get_json(silent=True) or {}
    gate = _access_write_gate("project", data)
    if gate:
        return gate
    resolved, err = _project_or_404(data.get("project") or "")
    if err:
        return err
    path = str(resolved)

    status = mask_activation.status(path)
    if not status.get("activated_at") and not _confirmed(data, "activate"):
        return jsonify({
            "error": "first activation permanently purges memory facts whose "
                     "provenance predates masking, and wipes the index and "
                     "compression cache",
            "needs_confirmation": True, "confirm_with": "activate",
            "first_activation": True,
        }), 409

    if not _activation_lock.acquire(blocking=False):
        return jsonify({"error": "activation already running"}), 409
    try:
        # rebuild_index is deliberately NOT read from the body: the purge has
        # already dropped the index so protection is complete either way, and
        # a rebuild is unbounded CPU triggered by one tap. Rebuild locally with
        # `c3 access mask activate --reindex`.
        report = mask_activation.activate(path, rebuild_index=False)
    except Exception as exc:
        return _svc_error(exc)
    finally:
        _activation_lock.release()

    _access_audit("mask-activate", f"{report.get('files', 0)} file(s)",
                  "mask", "project", resolved, confirmed=True)
    _gw_audit("mobile.access.mask_activate", {"files": report.get("files", 0)})
    return jsonify({
        "report": report,
        "mask": mask_activation.status(path),
        "summary": mask_activation.summary_line(path),
    })


@bp.route("/access/denials", methods=["GET"])
def mobile_access_denials():
    """Aggregated denial counters with an actionable fix per row."""
    gate = _feature_or_404("access")
    if gate:
        return gate
    from services import access_telemetry as at
    resolved, err = _project_or_404(request.args.get("project") or "")
    if err:
        return err
    agg = at.aggregate(str(resolved),
                       session_id=request.args.get("session") or "")
    return jsonify({
        "path": str(resolved),
        "total": agg["total"],
        "by_layer": agg["by_layer"],
        "rows": [{**r, "fix": at.suggest(r)} for r in agg["rows"]],
        "sessions": agg.get("sessions", 0),
    })


@bp.route("/access/denials/search", methods=["GET"])
def mobile_access_denials_search():
    """Raw denial events, newest first. Read-only.

    There is deliberately no DELETE counterpart: clearing the counters is a
    leaked token's first move after being denied."""
    gate = _feature_or_404("access")
    if gate:
        return gate
    from services import access_telemetry as at
    resolved, err = _project_or_404(request.args.get("project") or "")
    if err:
        return err
    return jsonify(at.search_events(
        str(resolved),
        q=request.args.get("q") or "",
        layer=request.args.get("layer") or "",
        tool=request.args.get("tool") or "",
        session=request.args.get("session") or "",
        since=request.args.get("since") or "",
        limit=request.args.get("limit") or 200))


# ── Tool discipline (c3 enforce) ──────────────────────────

@bp.route("/enforcement", methods=["GET"])
def mobile_enforcement_get():
    """Effective discipline policy for a project, plus its denial evidence."""
    gate = _feature_or_404("enforcement")
    if gate:
        return gate
    from services import access_telemetry as at
    from services import enforcement_policy as ep
    resolved, err = _project_or_404(request.args.get("project") or "")
    if err:
        return err
    path = str(resolved)
    policy = ep.resolve(path)
    tier = ""
    try:
        cfg_file = resolved / ".c3" / "config.json"
        if cfg_file.is_file():
            tier = str(json.loads(cfg_file.read_text(encoding="utf-8"))
                       .get("permission_tier") or "")
    except (OSError, ValueError):
        tier = ""
    agg = at.aggregate(path)
    return jsonify({
        "path": path,
        "mode": policy.mode,
        "scope": policy.scope,
        "set_by": policy.set_by,
        "signal_ttl_s": policy.signal_ttl_s,
        "blocked_tools": sorted(policy.blocked_tools),
        "warnings": list(policy.warnings),
        "tier": tier,
        "tier_implies": ep.derive_from_tier(tier) if tier else "",
        "default_mode": ep.DEFAULT_MODE,
        "modes": [{"id": m, "help": ep.MODE_HELP[m]} for m in ep.MODES],
        "denials": {
            "total": agg["total"],
            "by_layer": agg["by_layer"],
            "rows": [{**r, "fix": at.suggest(r)} for r in agg["rows"][:12]],
        },
        # Verbatim from the desktop: without this, "off" reads as "nothing is
        # protected", which is false and would make the choice uninformed.
        "coverage_note": (
            "Tool discipline only governs whether native Edit/Write are pushed "
            "through c3_edit. At every mode — including off — Access Guard path "
            "rules, the credential-vault write guard, and agent locks still "
            "enforce. The edit ledger records native writes either way; strict "
            "additionally gets c3_edit's pre-edit snapshot."
        ),
    })


@bp.route("/enforcement", methods=["POST"])
def mobile_enforcement_set():
    """Set a project's discipline mode.

    ``scope`` is hard-coded to project and ``set_by`` to user: a machine-wide
    discipline change from a phone would silently re-govern every project, and
    there is no affordance for reviewing that here."""
    gate = _feature_or_404("enforcement") or _feature_or_404("enforcement_write")
    if gate:
        return gate
    limited = _security_gate()
    if limited:
        return limited
    from services import enforcement_policy as ep
    data = request.get_json(silent=True) or {}
    resolved, err = _project_or_404(data.get("project") or "")
    if err:
        return err
    mode = str(data.get("mode") or "").strip().lower()
    # Turning discipline off is the loosening direction, so it is the one mode
    # change that needs a deliberate second step.
    if mode == ep.MODE_OFF and not _confirmed(data, "off"):
        return jsonify({
            "error": "turning tool discipline off stops native writes being "
                     "pushed through c3_edit",
            "needs_confirmation": True, "confirm_with": "off",
        }), 400
    try:
        result = ep.set_mode(mode, str(resolved), set_by=ep.SET_BY_USER,
                             scope="project")
    except (ValueError, TypeError) as exc:
        return _svc_error(exc)

    detail = {"kind": "enforcement", "action": "set_mode",
              "mode": result["mode"], "previous": result.get("previous", ""),
              "scope": result["scope"], "via": "oracle-mobile",
              "caller": _caller()}
    try:
        from services.activity_log import ActivityLog
        ActivityLog(str(resolved)).log("access_action", dict(detail))
    except Exception:
        pass
    try:
        from services.edit_ledger import EditLedger
        EditLedger(str(resolved)).log_edit(
            file=f"enforcement://{result['scope']}",
            change_type="enforcement_set_mode",
            summary=(f"tool discipline {result.get('previous') or 'default'} "
                     f"-> {result['mode']} via mobile"),
            tags=["enforcement", "access"], detail=dict(detail))
    except Exception:
        pass
    _gw_audit("mobile.enforcement.set_mode", {"mode": result["mode"]})
    return jsonify(result)
