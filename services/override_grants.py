"""Override grants — the capability half of Override Requests.

Frozen spec: docs/override-requests.md §3.4 (store), §3.5 (audit), §4 (the
whole security surface: what a grant does and does not authorise).

A *Request* is a message and an agent can make one. A **Grant** is a
capability and only a human can mint one — there is no agent-facing call in
this module that creates or widens a grant, and there never will be. Writers
here are privileged internal surfaces (the ``c3 override`` CLI, later the
Oracle decide route) using the same tool-layer bypass as ``credential_store``.

Everything is deliberately boring on disk: ``.c3/override_grants.json`` is
runtime state, not config — ephemeral, gitignored with the rest of ``.c3/``,
and fail-closed. An unparseable file means **zero grants**, not "skip the
check".
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services import override_policy as op_policy

GRANTS_FILE = ".c3/override_grants.json"
AUDIT_FILE = ".c3/overrides.jsonl"
_LOCK_FILE = ".c3/override_grants.lock"

_AUDIT_MAX_BYTES = 512 * 1024

#: Lifecycle events written to .c3/overrides.jsonl (spec §3.5).
EV_REQUESTED = "requested"
EV_APPROVED = "approved"
EV_DENIED = "denied"
EV_EXPIRED = "expired"
EV_CONSUMED = "consumed"
EV_REVOKED = "revoked"
EV_NEAR_MISS = "near_miss"
EV_CONSUMED_AFTER_EXPIRY = "consumed_after_expiry_attempt"

#: How long a stale lock is honoured before it is broken. Long enough that a
#: real read-modify-write (two small JSON files) always finishes first; short
#: enough that a killed hook subprocess cannot wedge enforcement.
_LOCK_STALE_S = 5.0
_LOCK_POLL_S = 0.01
_LOCK_TIMEOUT_S = 2.0


# ── Time helpers ───────────────────────────────────────────────────────────

def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_ts(value) -> datetime | None:
    try:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _expired(grant: dict, at: datetime | None = None) -> bool:
    exp = parse_ts(grant.get("expires_at"))
    if exp is None:
        return True  # unreadable expiry ⇒ treat as expired, never as eternal
    return (at or now()) >= exp


# ── Paths ──────────────────────────────────────────────────────────────────

def grants_path(project_path) -> Path:
    return Path(project_path) / GRANTS_FILE


def audit_path(project_path) -> Path:
    return Path(project_path) / AUDIT_FILE


def path_key(path, project_path=".") -> str:
    """Canonical identity of a target path — §4 condition 7.

    Delegates to ``access_guard.canonicalize`` so ``.\\src\\..\\.env`` and
    ``.env`` are the same grant and neither is a new one. Returns '' when the
    path is not representable (UNC, ADS, 8.3 alias); '' never matches a grant.
    """
    try:
        from services import access_guard as ag  # noqa: PLC0415 — lazy
        canon, _rel, denial = ag.canonicalize(path, project_path)
        return "" if denial else canon
    except Exception:
        return ""


# ── Store ──────────────────────────────────────────────────────────────────

class _Lock:
    """Cross-process advisory lock for the grants read-modify-write.

    ``_atomic_write_json`` guarantees a reader never sees a torn file; it does
    NOT make read-modify-write atomic, and two PreToolUse hook subprocesses
    racing the last use of a single-use grant is exactly that. O_EXCL create is
    the portable primitive available in a hook subprocess with no dependencies.
    Failure to acquire is not fatal — the caller proceeds and the worst case is
    the pre-lock behaviour, never a wrongly-allowed call, because the consume
    re-reads under the lock and decrements what it actually found.
    """

    def __init__(self, project_path):
        self.path = Path(project_path) / _LOCK_FILE
        self.held = False

    def __enter__(self):
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > _LOCK_STALE_S:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
            except OSError:
                return self  # unwritable .c3 — proceed unlocked
            if time.monotonic() >= deadline:
                return self
            time.sleep(_LOCK_POLL_S)

    def __exit__(self, *exc):
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


def _atomic_write(path: Path, data: dict) -> None:
    """Reuse the hook layer's durable writer; fall back to a plain write."""
    try:
        import sys  # noqa: PLC0415
        cli_dir = Path(__file__).resolve().parent.parent / "cli"
        if str(cli_dir) not in sys.path:
            sys.path.insert(0, str(cli_dir))
        from _hook_utils import _atomic_write_json  # noqa: PLC0415
        _atomic_write_json(path, data)
    except Exception:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(project_path=".") -> tuple:
    """``(grants, corrupt)``. Corrupt ⇒ ``([], True)`` — fail closed (§12.1)."""
    path = grants_path(project_path)
    if not path.is_file():
        return [], False
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return [], True
    if not isinstance(data, dict):
        return [], True
    grants = data.get("grants")
    if grants is None:
        return [], False
    if not isinstance(grants, list) or any(not isinstance(g, dict) for g in grants):
        return [], True
    return grants, False


def _save(project_path, grants: list) -> None:
    _atomic_write(grants_path(project_path), {"grants": grants})


def active(project_path=".", session_id: str = "") -> list:
    """Live grants: not expired, uses left. Ordered newest first."""
    grants, corrupt = load(project_path)
    if corrupt:
        return []
    at = now()
    live = [g for g in grants
            if not _expired(g, at) and int(g.get("uses_remaining") or 0) > 0
            and (not session_id or g.get("session_id") == session_id)]
    return sorted(live, key=lambda g: str(g.get("granted_at") or ""), reverse=True)


# ── Audit (append-only, spec §3.5) ─────────────────────────────────────────

def audit(project_path, event: str, payload: dict) -> None:
    """Append one lifecycle line. Best-effort — never raises into a hook."""
    try:
        path = audit_path(project_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > _AUDIT_MAX_BYTES:
                os.replace(str(path), str(path) + ".1")
        except OSError:
            pass
        line = dict(payload)
        line["event"] = event
        line["ts"] = iso(now())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_audit(project_path=".", limit: int = 50) -> list:
    """Newest-last audit lines, for `c3 override list --audit`."""
    path = audit_path(project_path)
    if not path.is_file():
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except Exception:
                    continue
    except OSError:
        return []
    return out[-limit:] if limit else out


# ── Minting — human surfaces only ──────────────────────────────────────────

def new_id(prefix: str = "grt") -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def mint(project_path=".", *, session_id: str, layer: str, rule: str,
         tool: str, op: str, path, ttl_s: int | None = None,
         uses: int | None = None, granted_by: str = "cli",
         request_id: str = "", policy=None, layers_key: str = "") -> dict:
    """Create one grant. **Human surfaces only** — callers must be authorised.

    Raises ``ValueError`` when policy forbids it. The refusal is the point:
    a mint that policy would not allow must fail loudly at creation, not
    silently produce a grant the gate then ignores.

    ``layers_key``: the ``override.layers`` key the request was classified
    under. When given, the enabled-check is ``policy.escalatable(layers_key)``
    — strictly tighter for every layer except ``access_confirm``, which is the
    one layer that does not require ``override.enabled`` (the confirm rule the
    human wrote is the opt-in). When empty (direct CLI mints), the legacy
    ``enabled`` check applies unchanged.
    """
    policy = policy or op_policy.resolve(project_path)
    if layers_key:
        if not policy.escalatable(layers_key):
            raise ValueError(f"the '{layers_key}' layer is not escalatable for "
                             "this project — no gate would honour the grant")
    elif not policy.enabled:
        raise ValueError("overrides are disabled for this project "
                         "(`override.enabled` is false)")
    if layer not in op_policy.GATE_LAYERS:
        raise ValueError(f"unknown layer '{layer}' — expected one of: "
                         + ", ".join(op_policy.GATE_LAYERS))
    if not str(session_id or "").strip():
        # §4 condition 2 is not decoration: an unbound grant would be a
        # standing capability for every future session on this project.
        raise ValueError("a grant must name the session it belongs to "
                         "(--session); grants never cross sessions")
    key = path_key(path, project_path)
    if not key:
        raise ValueError("target path is not representable (UNC / ADS / 8.3 "
                         "alias) — it can never match a grant")
    if op_policy.forbidden_target(key):
        raise ValueError(f"{op_policy.TAG_NOT_ESCALATABLE} {Path(key).name} is "
                         "a vault or policy file — no grant may ever cover it")
    ttl = policy.clamp_ttl(ttl_s)
    grant = {
        "id": new_id(),
        "request_id": request_id or "",
        "session_id": str(session_id or ""),
        "layer": layer,
        "rule": str(rule),
        "tool": str(tool),
        "op": str(op),
        "path_key": key,
        "expires_at": iso(now() + timedelta(seconds=ttl)),
        "uses_remaining": policy.clamp_uses(uses),
        "granted_at": iso(now()),
        "granted_by": str(granted_by or "cli"),
    }
    with _Lock(project_path):
        grants, corrupt = load(project_path)
        if corrupt:
            grants = []  # a corrupt file is replaced, never merged into
        grants = [g for g in grants if not _expired(g)
                  or g.get("id") == grant["id"]]
        grants.append(grant)
        _save(project_path, grants)
    audit(project_path, EV_APPROVED, {
        "grant_id": grant["id"], "request_id": grant["request_id"],
        "session_id": grant["session_id"], "layer": layer, "rule": grant["rule"],
        "tool": grant["tool"], "op": grant["op"], "path": key,
        "ttl_s": ttl, "uses": grant["uses_remaining"],
        "granted_by": grant["granted_by"],
    })
    return grant


def revoke(project_path=".", grant_id: str = "") -> bool:
    """Drop one grant early. Human surfaces only."""
    removed = None
    with _Lock(project_path):
        grants, corrupt = load(project_path)
        if corrupt:
            return False
        keep = []
        for g in grants:
            if g.get("id") == grant_id and removed is None:
                removed = g
            else:
                keep.append(g)
        if removed is None:
            return False
        _save(project_path, keep)
    audit(project_path, EV_REVOKED, {
        "grant_id": grant_id, "session_id": removed.get("session_id", ""),
        "rule": removed.get("rule", ""), "path": removed.get("path_key", ""),
    })
    return True


def sweep_expired(project_path=".") -> int:
    """Drop expired/used-up grants, one audit line each. Returns the count."""
    dropped = []
    with _Lock(project_path):
        grants, corrupt = load(project_path)
        if corrupt:
            return 0
        keep = []
        for g in grants:
            if _expired(g) or int(g.get("uses_remaining") or 0) <= 0:
                dropped.append(g)
            else:
                keep.append(g)
        if dropped:
            _save(project_path, keep)
    for g in dropped:
        audit(project_path, EV_EXPIRED, {
            "grant_id": g.get("id", ""), "session_id": g.get("session_id", ""),
            "rule": g.get("rule", ""), "path": g.get("path_key", ""),
            "uses_remaining": g.get("uses_remaining", 0),
        })
    return len(dropped)


# ── Matching + consumption (§4) ────────────────────────────────────────────

def _matches(grant: dict, *, session_id: str, layer: str, rule: str,
             tool: str, op: str, key: str, at: datetime) -> bool:
    """All nine conditions of §4. Any mismatch ⇒ ordinary denial."""
    return (
        str(grant.get("session_id") or "") == str(session_id or "")   # 2
        and grant.get("layer") == layer                               # 3
        and grant.get("rule") == rule                                 # 4
        and grant.get("tool") == tool                                 # 5
        and grant.get("op") == op                                     # 6
        and grant.get("path_key") == key                              # 7
        and not _expired(grant, at)                                   # 8
        and int(grant.get("uses_remaining") or 0) > 0                 # 9
    )
    # Condition 1 (project_path) is structural: the store is project-local.


def find(project_path=".", *, session_id: str, layer: str, rule: str,
         tool: str, op: str, path) -> dict | None:
    """The grant that WOULD authorise this call — no consumption, no audit."""
    key = path_key(path, project_path)
    if not key or op_policy.forbidden_target(key):
        return None
    grants, corrupt = load(project_path)
    if corrupt:
        return None
    at = now()
    for g in grants:
        if _matches(g, session_id=session_id, layer=layer, rule=rule,
                    tool=tool, op=op, key=key, at=at):
            return g
    return None


def _near_miss(project_path, grants: list, *, session_id: str, layer: str,
               rule: str, tool: str, op: str, key: str) -> None:
    """Record 'you approved X, the agent then tried Y' (§4, §12.6)."""
    for g in grants:
        if g.get("session_id") != session_id or g.get("layer") != layer:
            continue
        if _expired(g) or int(g.get("uses_remaining") or 0) <= 0:
            continue
        differs = [f for f, want, got in (
            ("rule", g.get("rule"), rule),
            ("tool", g.get("tool"), tool),
            ("op", g.get("op"), op),
            ("path", g.get("path_key"), key),
        ) if want != got]
        if differs:
            audit(project_path, EV_NEAR_MISS, {
                "grant_id": g.get("id", ""), "session_id": session_id,
                "differs": differs,
                "approved": {"rule": g.get("rule"), "tool": g.get("tool"),
                             "op": g.get("op"), "path": g.get("path_key")},
                "attempted": {"rule": rule, "tool": tool, "op": op,
                              "path": key},
            })
            return


def consume(project_path=".", *, session_id: str, layer: str, rule: str,
            tool: str, op: str, path) -> dict | None:
    """Atomically burn one use and return the grant, or ``None``.

    Consumption happens at **allow** time in the PreToolUse hook. If the tool
    then fails for an unrelated reason the use is burned; re-requesting is
    cheap, and that is strictly safer than consuming in PostToolUse where a
    crash would leave a live grant behind (§4).
    """
    key = path_key(path, project_path)
    if not key or op_policy.forbidden_target(key):
        return None
    with _Lock(project_path):
        grants, corrupt = load(project_path)
        if corrupt:
            return None
        at = now()
        hit = None
        for g in grants:
            if _matches(g, session_id=session_id, layer=layer, rule=rule,
                        tool=tool, op=op, key=key, at=at):
                hit = g
                break
        if hit is None:
            _near_miss(project_path, grants, session_id=session_id, layer=layer,
                       rule=rule, tool=tool, op=op, key=key)
            return None
        hit["uses_remaining"] = int(hit.get("uses_remaining") or 0) - 1
        hit["last_used_at"] = iso(at)
        _save(project_path, grants)
        used = dict(hit)
    audit(project_path, EV_CONSUMED, {
        "grant_id": used.get("id", ""), "session_id": session_id,
        "layer": layer, "rule": rule, "tool": tool, "op": op, "path": key,
        "uses_remaining": used.get("uses_remaining", 0),
    })
    return used


def granted_context(grant: dict, rule: str) -> str:
    """The additionalContext line an allowed-by-grant call emits (§5)."""
    when = parse_ts(grant.get("granted_at"))
    stamp = when.strftime("%H:%MZ") if when else "?"
    return (
        f"{op_policy.TAG_GRANTED} Allowed by override {grant.get('id', '?')} "
        f"(approved on {grant.get('granted_by', '?')} {stamp}, "
        f"{int(grant.get('uses_remaining') or 0)} uses left). "
        f"The rule {rule} is still in force."
    )


# ── The gate the hooks call ────────────────────────────────────────────────

def gate_access(project_path, denial, *, tool: str, op: str, path,
                session_id: str) -> str | None:
    """Consult grants for an access/mask ``Denial``. ``None`` ⇒ stay denied.

    Order is load-bearing: **policy first, grants second**. A live grant is
    voided the moment `override.enabled` (or its layer) is switched off
    (§12.8), and a non-escalatable denial never reaches the store at all.
    """
    layer_key = op_policy.rule_class_for_denial(denial)
    if layer_key is None:
        return None
    policy = op_policy.resolve(project_path)
    if not policy.escalatable(layer_key):
        return None
    gate = op_policy.GATE_FOR_LAYER_KEY[layer_key]
    rule = str(getattr(denial, "rule", "") or "")
    grant = consume(project_path, session_id=session_id, layer=gate, rule=rule,
                    tool=tool, op=op, path=path)
    return granted_context(grant, rule) if grant else None


def gate_discipline(project_path, *, tool: str, path, session_id: str) -> str | None:
    """Consult grants for the tool-discipline native-write block.

    Runs **after** the vault guard, which stays unconditional.
    """
    policy = op_policy.resolve(project_path)
    if not policy.escalatable(op_policy.LAYER_DISCIPLINE):
        return None
    grant = consume(project_path, session_id=session_id,
                    layer=op_policy.GATE_DISCIPLINE,
                    rule=op_policy.RULE_DISCIPLINE, tool=tool, op="write",
                    path=path)
    if not grant:
        return None
    return granted_context(grant, "tool discipline")
