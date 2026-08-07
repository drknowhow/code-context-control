"""Override requests — the *message* half of Override Requests.

Frozen spec: docs/override-requests.md §3.3 (store), §7 (agent surface), §8
(what the Oracle will call), §12 (failure modes).

The distinction this module exists to preserve: **a Request is a message and
an agent may create one; a Grant is a capability and only a human may create
one.** Everything an agent can reach lives above `decide()`; `decide()` itself
is reachable only from `c3 override approve|deny` today and the Oracle's
Bearer-authenticated route later. There is no `approve` action on the agent
tool and there never will be.

`justification` is agent-supplied and therefore **untrusted input**. It is
stored verbatim, capped, never parsed, never used for matching, and never
interpolated into a path or a shell. Human surfaces render it quoted, under a
label saying the agent wrote it and may be repeating text it read from a file.

Store: ``~/.c3/oracle/override_requests.json`` — Oracle-owned, one file across
projects (each row carries its ``project_path``), load-all / mutate / save-all
in the house style of ``oracle/services/memory_writer.py``.
"""
from __future__ import annotations

import json
import secrets
from datetime import timedelta
from pathlib import Path

from services import override_grants as og
from services import override_policy as op_policy

STORE_REL = ".c3/oracle/override_requests.json"

#: Mute store (P3, spec §8 "deny + suppress identical requests for this
#: session"). A SIBLING file rather than a key inside the request store,
#: because §3.3 freezes that file's shape as a JSON array of request rows.
MUTES_REL = ".c3/oracle/override_mutes.json"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_WITHDRAWN = "withdrawn"
OPEN_STATUSES = (STATUS_PENDING,)

#: Untrusted free text is capped, not sanitised — sanitising would imply it is
#: safe to interpolate somewhere, and it never is.
JUSTIFICATION_CAP = 400

DECISION_APPROVE = "approve"
DECISION_DENY = "deny"

#: Grant shape on approval (§8). ``once`` is the default and the only one that
#: needs no extra switch; ``session`` is an unlimited-uses grant for the rest
#: of the request TTL window and is gated on ``allow_session_grants`` plus a
#: typed confirmation, because it is the closest thing here to a policy change.
MODE_ONCE = "once"
MODE_SESSION = "session"
MODES = (MODE_ONCE, MODE_SESSION)

#: What the client must retype to get a session grant. Not the rule glob —
#: that challenge already means "I accept this access layer"; this one is a
#: separate question ("I accept an unlimited-uses grant") and must not be
#: answerable by the same keystrokes.
CONFIRM_SESSION = "session"


class OverrideError(ValueError):
    """A refusal the agent (or CLI) should see verbatim."""


def store_path() -> Path:
    return Path.home() / STORE_REL


def new_id() -> str:
    return f"ovr_{secrets.token_hex(6)}"


# ── Store ──────────────────────────────────────────────────────────────────

def load() -> list:
    """Every request row. A corrupt store reads as empty — fail closed.

    Empty is the safe direction here: a request that cannot be read cannot be
    approved, and the agent simply asks again.
    """
    path = store_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _save(rows: list) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


def _expired(row: dict, at=None) -> bool:
    exp = og.parse_ts(row.get("expires_at"))
    if exp is None:
        return True
    return (at or og.now()) >= exp


def _refresh(rows: list) -> bool:
    """Flip lapsed pendings to ``expired``. True when anything changed."""
    changed = False
    at = og.now()
    for row in rows:
        if row.get("status") == STATUS_PENDING and _expired(row, at):
            row["status"] = STATUS_EXPIRED
            row["resolved_at"] = og.iso(at)
            changed = True
            og.audit(row.get("project_path", "."), og.EV_EXPIRED,
                     {"request_id": row.get("id", ""),
                      "session_id": row.get("session_id", ""),
                      "rule": row.get("rule", ""), "path": row.get("path", "")})
    return changed


def sweep_expired() -> int:
    rows = load()
    before = sum(1 for r in rows if r.get("status") == STATUS_PENDING)
    if _refresh(rows):
        _save(rows)
    after = sum(1 for r in rows if r.get("status") == STATUS_PENDING)
    return before - after


def get(request_id: str) -> dict | None:
    rows = load()
    if _refresh(rows):
        _save(rows)
    for row in rows:
        if row.get("id") == request_id:
            return row
    return None


def list_requests(*, project_path: str = "", session_id: str = "",
                  status: str = "", limit: int = 50) -> list:
    """Newest first. Empty filters mean 'all'."""
    rows = load()
    if _refresh(rows):
        _save(rows)
    key = og.path_key(project_path) if project_path else ""
    out = []
    for row in rows:
        if key and og.path_key(row.get("project_path", "")) != key:
            continue
        if session_id and row.get("session_id") != session_id:
            continue
        if status and row.get("status") != status:
            continue
        out.append(row)
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return out[:limit] if limit else out


# ── Mutes — "deny and stop asking" (§8, P3) ────────────────────────────────
# A mute is the answer to a retry loop: the user denied this exact call once
# and does not want to be asked again by this session. It is NOT a policy
# change and NOT a grant — it suppresses a *message*, so its worst case is an
# agent that stays blocked, which is the safe direction.
#
# Scope is deliberately (project, session, layer, rule, tool, op, path_key):
# byte-identical to the duplicate-suppression key `create()` already uses, so
# a mute is exactly "duplicate suppression that outlives the pending row".
# Session-bound because §8 says "for this session" — a new Claude Code session
# has a new problem and has earned the right to ask once.

def mutes_path() -> Path:
    return Path.home() / MUTES_REL


def mute_key(project_path: str, *, session_id: str, layer: str, rule: str,
             tool: str, op: str, path_key: str) -> str:
    """The suppression identity. Same tuple as duplicate detection."""
    return "\x1f".join([
        og.path_key(project_path), str(session_id or ""), str(layer or ""),
        str(rule or ""), str(tool or ""), str(op or ""), str(path_key or ""),
    ])


def load_mutes() -> list:
    """Every mute row. Corrupt reads as empty — a lost mute only means the
    agent gets to ask once more, so failing open here is the safe direction
    (the opposite call from grants, where failing open would be a capability).
    """
    path = mutes_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _save_mutes(rows: list) -> None:
    path = mutes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)


def is_muted(project_path: str, *, session_id: str, layer: str, rule: str,
             tool: str, op: str, path_key: str) -> bool:
    key = mute_key(project_path, session_id=session_id, layer=layer, rule=rule,
                   tool=tool, op=op, path_key=path_key)
    return any(r.get("key") == key for r in load_mutes())


def add_mute(row: dict) -> dict:
    """Suppress future requests identical to *row*. Idempotent."""
    project_path = row.get("project_path", ".")
    key = mute_key(project_path, session_id=row.get("session_id", ""),
                   layer=row.get("layer", ""), rule=row.get("rule", ""),
                   tool=row.get("tool", ""), op=row.get("op", ""),
                   path_key=row.get("path_key", ""))
    rows = load_mutes()
    for existing in rows:
        if existing.get("key") == key:
            return existing
    mute = {
        "key": key,
        "project_path": project_path,
        "session_id": row.get("session_id", ""),
        "layer": row.get("layer", ""),
        "rule": row.get("rule", ""),
        "tool": row.get("tool", ""),
        "op": row.get("op", ""),
        "path_key": row.get("path_key", ""),
        "path": row.get("path", ""),
        "request_id": row.get("id", ""),
        "muted_at": og.iso(og.now()),
    }
    rows.append(mute)
    _save_mutes(rows)
    return mute


def clear_mutes(project_path: str = "", session_id: str = "") -> int:
    """Drop mutes, optionally narrowed. Human surfaces only. Returns count."""
    rows = load_mutes()
    key = og.path_key(project_path) if project_path else ""
    keep = [r for r in rows
            if (key and og.path_key(r.get("project_path", "")) != key)
            or (session_id and r.get("session_id") != session_id)]
    if not key and not session_id:
        keep = []
    removed = len(rows) - len(keep)
    if removed:
        _save_mutes(keep)
    return removed


# ── Creation — the agent surface (§7) ──────────────────────────────────────

def classify(project_path: str, *, denial=None, layer: str = "",
             rule: str = "") -> tuple:
    """``(gate_layer, layers_key, rule)`` for a request, or raise.

    Derived from the *denial* wherever there is one: a request whose rule
    string does not match the rule that denies could never be satisfied by a
    grant, and a silently unsatisfiable request is worse than a refusal.
    """
    if denial is not None:
        layers_key = op_policy.rule_class_for_denial(denial)
        if layers_key is None:
            raise OverrideError(
                f"{op_policy.TAG_NOT_ESCALATABLE} this denial can never be "
                "overridden. Do not ask again and do not look for another "
                "route — mark the step blocked and tell the user.")
        return (op_policy.GATE_FOR_LAYER_KEY[layers_key], layers_key,
                str(getattr(denial, "rule", "")))
    if layer == op_policy.GATE_DISCIPLINE:
        return (op_policy.GATE_DISCIPLINE, op_policy.LAYER_DISCIPLINE,
                op_policy.RULE_DISCIPLINE)
    if layer == op_policy.GATE_SHELL:
        return (op_policy.GATE_SHELL, op_policy.LAYER_SHELL_WARN,
                op_policy.RULE_SHELL_WARN)
    raise OverrideError(
        f"{op_policy.TAG_NOT_ESCALATABLE} could not identify which rule "
        f"denied this call (layer={layer!r}, rule={rule!r}).")


def create(project_path: str, *, session_id: str, tool: str, op: str, path,
           denial=None, layer: str = "", justification: str = "",
           refusal: str = "", policy=None) -> dict:
    """Create (or re-surface) a pending request. Agent-reachable.

    Returns the row. A duplicate of a live pending request returns that row
    with ``duplicate: True`` rather than minting a second card — an agent in a
    retry loop must not be able to fill the user's phone.
    """
    policy = policy or op_policy.resolve(project_path)
    if not policy.enabled:
        raise OverrideError(
            f"{op_policy.TAG_NOT_ESCALATABLE} overrides are switched off for "
            "this project. Ask the user in chat instead.")

    gate, layers_key, rule = classify(project_path, denial=denial, layer=layer)
    if not policy.escalatable(layers_key):
        raise OverrideError(
            f"{op_policy.TAG_NOT_ESCALATABLE} the '{layers_key}' layer is not "
            "escalatable in this project. Ask the user in chat instead.")

    key = og.path_key(path, project_path)
    if not key:
        raise OverrideError(
            f"{op_policy.TAG_NOT_ESCALATABLE} that path is not representable "
            "(UNC / alternate data stream / 8.3 alias).")
    if op_policy.forbidden_target(key):
        raise OverrideError(
            f"{op_policy.TAG_NOT_ESCALATABLE} that file is part of the "
            "credential vault or the override policy itself.")

    # A mute is a standing "no, and stop asking" from the user (§8). It is
    # checked BEFORE the rate limits so a muted retry loop costs the agent's
    # hourly budget nothing — the user already answered, and the honest
    # outcome is a refusal that names the reason.
    if is_muted(project_path, session_id=session_id, layer=gate, rule=rule,
                tool=tool, op=op, path_key=key):
        raise OverrideError(
            f"{op_policy.TAG_NOT_ESCALATABLE} the user denied this exact "
            "request and muted it for this session. Do not ask again — mark "
            "the step blocked and tell the user what you needed.")

    rows = load()
    _refresh(rows)
    proj_key = og.path_key(project_path)
    now = og.now()

    for row in rows:
        if (row.get("status") == STATUS_PENDING
                and og.path_key(row.get("project_path", "")) == proj_key
                and row.get("session_id") == session_id
                and row.get("layer") == gate
                and row.get("rule") == rule
                and row.get("tool") == tool
                and row.get("op") == op
                and row.get("path_key") == key):
            out = dict(row)
            out["duplicate"] = True
            return out

    pending = sum(1 for r in rows if r.get("status") == STATUS_PENDING
                  and r.get("session_id") == session_id)
    if pending >= policy.max_pending_per_session:
        raise OverrideError(
            f"rate limit: this session already has {pending} pending "
            f"request(s), the cap is {policy.max_pending_per_session}. "
            "Withdraw one or wait for a decision.")

    hour_ago = now - timedelta(hours=1)
    recent = sum(1 for r in rows
                 if og.path_key(r.get("project_path", "")) == proj_key
                 and (og.parse_ts(r.get("created_at")) or hour_ago) > hour_ago)
    if recent >= policy.max_requests_per_hour:
        raise OverrideError(
            f"rate limit: {recent} requests in this project in the last hour, "
            f"the cap is {policy.max_requests_per_hour}.")

    row = {
        "id": new_id(),
        "project_path": str(Path(project_path).resolve()).replace("\\", "/"),
        "session_id": str(session_id or ""),
        "created_at": og.iso(now),
        "expires_at": og.iso(now + timedelta(seconds=policy.request_ttl_s)),
        "status": STATUS_PENDING,
        "layer": gate,
        "rule": rule,
        "rule_class": layers_key,
        "scope": str(getattr(denial, "scope", "") or ""),
        "tool": str(tool),
        "op": str(op),
        "path": str(path),
        "path_key": key,
        "refusal": str(refusal or "")[:1000],
        "justification": str(justification or "")[:JUSTIFICATION_CAP],
        "resolved_at": None,
        "decided_by": None,
        "decision_note": None,
    }
    rows.append(row)
    _save(rows)

    og.audit(project_path, og.EV_REQUESTED, {
        "request_id": row["id"], "session_id": row["session_id"],
        "layer": gate, "rule": rule, "tool": tool, "op": op, "path": key,
    })
    _notify(project_path, row, policy)
    return row


def _notify(project_path: str, row: dict, policy) -> None:
    """Ride the existing notification feed so the phone sees it (§9)."""
    try:
        from services.notifications import NotificationStore  # noqa: PLC0415
        NotificationStore(str(project_path)).add(
            agent="override",
            severity=policy.notify_severity,
            title=f"Approve {row['tool']} {row['op']} on "
                  f"{Path(row['path']).name}?",
            message=(f"Blocked by rule {row['rule']} ({row['rule_class']}). "
                     f"Approve once: c3 override approve {row['id']}"),
        )
    except Exception:
        pass  # a missing notification must never fail the request


def _notify_decision(project_path: str, row: dict, grant=None) -> None:
    """Append the OUTCOME to the notification feed (§8).

    The request notification says "someone should decide this"; without a
    second line the feed never records that anyone did, so a user scrolling
    back sees an eternally-open question they actually answered from the
    phone. Severity is deliberately ``info``: the decision is the calm end of
    the story, and re-alerting on your own tap trains people to ignore the
    feed.
    """
    try:
        from services.notifications import NotificationStore  # noqa: PLC0415
        status = row.get("status", "")
        name = Path(str(row.get("path", ""))).name
        if status == STATUS_APPROVED:
            detail = "once" if row.get("grant_mode") != MODE_SESSION \
                else "for this session"
            message = (f"Granted {row.get('tool')} {row.get('op')} on {name} "
                       f"{detail} (rule {row.get('rule')}). "
                       f"Grant {(grant or {}).get('id', '')} expires "
                       f"{(grant or {}).get('expires_at', '')}.")
        else:
            message = (f"Denied {row.get('tool')} {row.get('op')} on {name} "
                       f"(rule {row.get('rule')})."
                       + (" Muted for this session." if row.get("muted") else ""))
        NotificationStore(str(project_path)).add(
            agent="override",
            severity="info",
            title=f"Override {status}: {name}",
            message=message,
        )
    except Exception:
        pass  # the decision already happened; a missing feed line never undoes it


def withdraw(request_id: str, session_id: str) -> dict:
    """An agent cancelling its OWN pending request (e.g. it found another way)."""
    rows = load()
    _refresh(rows)
    for row in rows:
        if row.get("id") != request_id:
            continue
        if row.get("session_id") != session_id:
            raise OverrideError("that request belongs to another session.")
        if row.get("status") != STATUS_PENDING:
            raise OverrideError(f"request is already {row.get('status')}.")
        row["status"] = STATUS_WITHDRAWN
        row["resolved_at"] = og.iso(og.now())
        _save(rows)
        return row
    raise OverrideError(f"no request with id '{request_id}'.")


# ── Decision — human surfaces only (§7: there is no agent path here) ───────

def decide(request_id: str, decision: str, *, uses: int | None = None,
           ttl_s: int | None = None, note: str = "", decided_by: str = "cli",
           confirm: str | None = None, mode: str = MODE_ONCE,
           mute: bool = False) -> dict:
    """Approve (minting a grant) or deny one request. **Human callers only.**

    Approving an ``access_deny`` / ``access_builtin`` request requires the rule
    glob retyped by hand, here rather than in each surface, so the CLI and the
    Oracle cannot drift apart on the one check that matters.
    """
    if decision not in (DECISION_APPROVE, DECISION_DENY):
        raise OverrideError("decision must be 'approve' or 'deny'.")
    if mode not in MODES:
        raise OverrideError(f"mode must be one of: {', '.join(MODES)}.")

    rows = load()
    _refresh(rows)
    row = next((r for r in rows if r.get("id") == request_id), None)
    if row is None:
        raise OverrideError(f"no request with id '{request_id}'.")
    if row.get("status") != STATUS_PENDING:
        # §12.7: a request that lapsed while the phone was showing it refreshes
        # to its real status instead of silently minting a grant.
        raise OverrideError(
            f"request {request_id} is {row.get('status')}, not pending.")

    project_path = row.get("project_path", ".")
    now = og.now()

    if decision == DECISION_DENY:
        row["status"] = STATUS_DENIED
        row["resolved_at"] = og.iso(now)
        row["decided_by"] = decided_by
        row["decision_note"] = str(note or "")[:JUSTIFICATION_CAP] or None
        row["muted"] = bool(mute)
        _save(rows)
        # Mute AFTER the row is persisted: a mute whose request never landed
        # as denied would suppress a question the user never actually answered.
        if mute:
            add_mute(row)
        og.audit(project_path, og.EV_DENIED, {
            "request_id": request_id, "session_id": row.get("session_id", ""),
            "rule": row.get("rule", ""), "path": row.get("path_key", ""),
            "decided_by": decided_by, "muted": bool(mute),
        })
        _notify_decision(project_path, row, grant=None)
        return row

    policy = op_policy.resolve(project_path)
    layers_key = row.get("rule_class", "")
    if not policy.escalatable(layers_key):
        raise OverrideError(
            f"the '{layers_key}' layer is no longer escalatable in this "
            "project — approving would mint a grant no gate would honour.")
    if layers_key in op_policy.TYPED_CONFIRM_LAYERS and confirm != row.get("rule"):
        raise OverrideError(
            f"approving an {layers_key} request needs the rule retyped: "
            f"confirm='{row.get('rule')}'")

    if mode == MODE_SESSION:
        # Two independent gates, both required. The switch is the user's
        # standing policy; the challenge is their answer right now.
        if not policy.allow_session_grants:
            raise OverrideError(
                "session grants are disabled for this project "
                "(`override.allow_session_grants` is false). Approve once "
                "instead, or change the policy on the desktop.")
        if confirm != CONFIRM_SESSION and layers_key not in op_policy.TYPED_CONFIRM_LAYERS:
            raise OverrideError(
                "a session grant is unlimited-use until it expires: "
                f"confirm='{CONFIRM_SESSION}'")
        # Unlimited uses within the (already clamped) TTL window.
        uses = op_policy.HARD_MAX_USES

    grant = og.mint(project_path, session_id=row.get("session_id", ""),
                    layer=row.get("layer", ""), rule=row.get("rule", ""),
                    tool=row.get("tool", ""), op=row.get("op", ""),
                    path=row.get("path", ""), ttl_s=ttl_s, uses=uses,
                    granted_by=decided_by, request_id=request_id,
                    policy=policy)

    row["status"] = STATUS_APPROVED
    row["resolved_at"] = og.iso(now)
    row["decided_by"] = decided_by
    row["decision_note"] = str(note or "")[:JUSTIFICATION_CAP] or None
    row["grant_id"] = grant["id"]
    row["grant_mode"] = mode
    _save(rows)
    _notify_decision(project_path, row, grant=grant)
    return row
