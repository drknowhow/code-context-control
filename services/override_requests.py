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
import os
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
#: ``rule`` (§4.1) is the largest shape: one approval covers every path the
#: rule glob matches, for any tool in the same op class, until its TTL or its
#: idle window runs out. It is gated on ``allow_rule_grants`` AND the rule
#: glob retyped by hand — a strictly harder challenge than ``session``,
#: because the thing being accepted is a standing capability, not a repeat.
MODE_ONCE = "once"
MODE_SESSION = "session"
MODE_RULE = "rule"
MODES = (MODE_ONCE, MODE_SESSION, MODE_RULE)

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


#: Lock file for the request store's read-modify-write. The store is ONE file
#: for every project and session on the box (`STORE_REL` under ``~``), so two
#: concurrent decides — or a decide racing an auto-file — used to be able to
#: lose a row outright. `_store_lock()` closes that window; the tmp name is
#: per-process besides, because a fixed `.tmp` is itself a collision.
_LOCK_REL = ".c3/oracle/override_requests.lock"


def _store_lock():
    """Cross-process advisory lock around a load→mutate→_save sequence.

    Reuses ``override_grants._Lock`` rather than growing a second
    implementation: same O_EXCL primitive, same stale-break, same
    non-fatal-on-failure contract (worst case is the pre-lock behaviour).
    """
    return og._Lock(Path.home(), _LOCK_REL)


def _save(rows: list) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _save_row(row: dict) -> None:
    """Persist ONE mutated row without clobbering rows written meanwhile.

    Every mutator here (create, withdraw, decide) touches exactly one row, but
    used to write back the whole snapshot it had loaded — so a decide and a
    concurrent auto-file, over a store shared by every project on the box,
    could each save a list that was missing the other's row. Re-reading under
    the lock and replacing only this row is the smallest fix that is actually
    correct; the caller's other in-memory rows are deliberately discarded.
    """
    with _store_lock():
        rows = load()
        for i, existing in enumerate(rows):
            if existing.get("id") == row.get("id"):
                rows[i] = row
                break
        else:
            rows.append(row)
        _save(rows)


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


def _refresh_writeback() -> list:
    """Load, lazily expire lapsed pendings, persist if anything moved.

    The writeback is the only reason a read path touches the file at all, so
    it takes the lock; a lost expiry flip is harmless on its own but would
    drop whatever else was being written at the same moment.
    """
    rows = load()
    if not _refresh(rows):
        return rows
    with _store_lock():
        fresh = load()
        if _refresh(fresh):
            _save(fresh)
        return fresh


def sweep_expired() -> int:
    rows = load()
    before = sum(1 for r in rows if r.get("status") == STATUS_PENDING)
    rows = _refresh_writeback()
    after = sum(1 for r in rows if r.get("status") == STATUS_PENDING)
    return before - after


def get(request_id: str) -> dict | None:
    rows = _refresh_writeback()
    for row in rows:
        if row.get("id") == request_id:
            return row
    return None


def list_requests(*, project_path: str = "", session_id: str = "",
                  status: str = "", limit: int = 50) -> list:
    """Newest first. Empty filters mean 'all'."""
    rows = _refresh_writeback()
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
    # Classify BEFORE the enabled check: the `access_confirm` layer does not
    # require `override.enabled` (the confirm rule a human wrote is the
    # opt-in — docs/confirm-guard.md), so which layer this is must be known
    # before deciding whether the master switch matters.
    gate, layers_key, rule = classify(project_path, denial=denial, layer=layer)
    if layers_key != op_policy.LAYER_ACCESS_CONFIRM and not policy.enabled:
        raise OverrideError(
            f"{op_policy.TAG_NOT_ESCALATABLE} overrides are switched off for "
            "this project. Ask the user in chat instead.")
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
    _save_row(row)

    og.audit(project_path, og.EV_REQUESTED, {
        "request_id": row["id"], "session_id": row["session_id"],
        "layer": gate, "rule": rule, "tool": tool, "op": op, "path": key,
    })
    _notify(project_path, row, policy)
    return row


def auto_file(project_path: str, *, denial, tool: str, op: str, path,
              session_id: str, refusal: str = "") -> tuple:
    """File a request on the agent's behalf at a ``confirm`` denial site.

    ``(row, "")`` on success — including the duplicate-suppressed case, where
    the existing pending row comes back — or ``(None, reason)`` when filing
    was refused (muted, rate-limited, layer forced off, unrepresentable path).
    Never raises: this runs on refusal paths inside hooks and tool handlers,
    and a broken request store must degrade to an ordinary refusal, never to a
    crash that fails the tool call open or closed for the wrong reason.

    ``justification`` is deliberately empty: an auto-filed row carries the
    denial's identity (tool, op, path, rule) and nothing the agent composed —
    the card renders from trusted fields only (docs/confirm-guard.md §3).
    """
    try:
        row = create(project_path, session_id=session_id, tool=tool, op=op,
                     path=path, denial=denial, justification="",
                     refusal=refusal)
        return row, ""
    except OverrideError as exc:
        reason = str(exc)
        if reason.startswith(op_policy.TAG_NOT_ESCALATABLE):
            reason = reason[len(op_policy.TAG_NOT_ESCALATABLE):].strip()
        return None, reason[:200]
    except Exception as exc:  # noqa: BLE001 — refusal path must not crash
        return None, f"request store unavailable ({exc})"[:200]


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
            # A phone routes on these, not on the title. Without them a tap
            # opens the app and drops the user wherever they were last —
            # which, for the one notification that exists to be answered, is
            # the same as not delivering it (§9).
            kind="override",
            ref_id=row["id"],
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
            detail = {
                MODE_SESSION: "for this session",
                MODE_RULE: f"for every path {row.get('rule')} matches, "
                           "this session",
            }.get(str(row.get("grant_mode") or ""), "once")
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


def _wake(project_path: str, row: dict, grant=None, policy=None) -> dict:
    """Tell the asking session a human answered (§7.1).

    The notification feed tells the *user* what they decided. Nothing told the
    *agent*, which is how an approved grant expired unused on 2026-08-08 with
    everyone believing the tap had worked. Failure here is swallowed on
    purpose: the decision is already on disk and the agent's ``action='status'``
    path still works — a wake is a shortcut past waiting, not the mechanism.
    """
    try:
        from services import override_wake as owake  # noqa: PLC0415
        return owake.fire(project_path, row, grant, policy=policy)
    except Exception as exc:
        return {"fired": False, "reason": f"wake unavailable: {exc}"}


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
        _save_row(row)
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
        _save_row(row)
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
        # A denial is news too: without it the agent sits in `wait` until the
        # request lapses, then reports a timeout for a question that was
        # answered in two seconds.
        row["wake"] = _wake(project_path, row)
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

    scope = og.SCOPE_CALL
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
    elif mode == MODE_RULE:
        # Same two-gate construction, one notch harder on both. The switch is
        # its own (`allow_rule_grants`, not the session one), and the
        # challenge is the RULE GLOB — for every layer, including the ones
        # that are one-tap for `once`. What is being accepted here is not
        # "ask me less about this file", it is "stop asking me about this
        # rule", and the keystrokes should say so.
        if not policy.allow_rule_grants:
            raise OverrideError(
                "rule-scoped grants are disabled for this project "
                "(`override.allow_rule_grants` is false). Approve once or "
                "for this session instead, or change the policy on the "
                "desktop.")
        if not og.rule_is_globbable(row.get("rule", "")):
            raise OverrideError(
                f"'{row.get('rule')}' names a behaviour, not a path set — "
                "there is nothing to widen along. Approve this call instead.")
        if confirm != row.get("rule"):
            raise OverrideError(
                "a rule grant covers every path the rule matches, for any "
                "tool in the same op class, until it expires: "
                f"confirm='{row.get('rule')}'")
        scope = og.SCOPE_RULE
        uses = None  # unlimited; mint() owns the TTL + idle window

    grant = og.mint(project_path, session_id=row.get("session_id", ""),
                    layer=row.get("layer", ""), rule=row.get("rule", ""),
                    tool=row.get("tool", ""), op=row.get("op", ""),
                    path=row.get("path", ""), ttl_s=ttl_s, uses=uses,
                    granted_by=decided_by, request_id=request_id,
                    policy=policy, layers_key=layers_key, scope=scope)

    row["status"] = STATUS_APPROVED
    row["resolved_at"] = og.iso(now)
    row["decided_by"] = decided_by
    row["decision_note"] = str(note or "")[:JUSTIFICATION_CAP] or None
    row["grant_id"] = grant["id"]
    row["grant_mode"] = mode
    row["grant_scope"] = scope
    _save_row(row)
    _notify_decision(project_path, row, grant=grant)
    # AFTER _save: the woken agent's very first move is to retry the blocked
    # call, which reads this store. Waking before persisting would race the
    # agent against the decision it was told about.
    row["wake"] = _wake(project_path, row, grant, policy=policy)
    return row
