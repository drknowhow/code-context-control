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
from functools import lru_cache
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

#: Grant scope — how far one approval reaches (spec §4.1).
#: ``call``: the historical shape, and still the default. One exact
#:   (session, layer, rule, tool, op, path) tuple.
#: ``rule``: same session/layer/rule/op, ANY path the rule glob covers and
#:   any tool in the same op class. Unlimited uses, bounded by a wall-clock
#:   backstop AND an idle window. Never mintable for a synthetic rule (see
#:   ``rule_is_globbable``) because those have no glob to widen along.
SCOPE_CALL = "call"
SCOPE_RULE = "rule"
SCOPES = (SCOPE_CALL, SCOPE_RULE)

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
    """Wall-clock expiry, plus the idle window a rule grant also carries."""
    at = at or now()
    exp = parse_ts(grant.get("expires_at"))
    if exp is None:
        return True  # unreadable expiry ⇒ treat as expired, never as eternal
    if at >= exp:
        return True
    return _idle_expired(grant, at)


def _idle_expired(grant: dict, at: datetime) -> bool:
    """True when a rule grant has sat unused past its idle window.

    This is what makes "until the session ends" safe: there is no session-END
    signal to hang a long grant on (the MCP surface's host session id is
    snapshotted at boot and goes stale across ``/clear``), so a grant the
    conversation stops exercising must die on its own. Only rule grants carry
    an idle window; a ``call`` grant is already capped at 15 minutes.
    An unreadable idle window is treated as expired, never as eternal.
    """
    idle = grant.get("idle_s")
    if idle is None:
        return False
    try:
        idle = int(idle)
    except (TypeError, ValueError):
        return True
    if idle < 1:
        return True
    since = parse_ts(grant.get("last_used_at")) or parse_ts(grant.get("granted_at"))
    if since is None:
        return True
    return at >= since + timedelta(seconds=idle)


def _uses_left(grant: dict) -> int | None:
    """Remaining uses, or ``None`` for a rule grant's unlimited budget."""
    raw = grant.get("uses_remaining")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _has_uses(grant: dict) -> bool:
    left = _uses_left(grant)
    return left is None or left > 0


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
    return path_key_pair(path, project_path)[0]


def path_key_pair(path, project_path=".") -> tuple:
    """``(canon, rel)`` — the two forms a rule glob is evaluated against.

    ``canon`` alone is the grant identity, but it is NOT enough to decide
    whether a rule COVERS a path: a project-scoped glob like ``secrets/**`` is
    written relative to the project root and only ever matches the ``rel``
    form. Rule-scoped grants need both, and they must be the same two strings
    ``access_guard.verdict`` used to produce the denial — anything else would
    let a grant and the rule that filed it disagree about the same file.
    """
    try:
        from services import access_guard as ag  # noqa: PLC0415 — lazy
        canon, rel, denial = ag.canonicalize(path, project_path)
        return ("", "") if denial else (canon, rel)
    except Exception:
        return "", ""


def rule_is_globbable(rule: str) -> bool:
    """False for the synthetic rule tokens that have no glob to widen along.

    ``<discipline:native-write-block>`` and ``<shell:soft-warn>`` name a
    behaviour, not a path set (``override_policy.RULE_DISCIPLINE`` /
    ``RULE_SHELL_WARN``), so a rule-scoped grant over one would either match
    nothing or — if the token were ever treated as a glob — everything.
    Refused at mint rather than left to fail confusingly at match time.
    """
    text = str(rule or "").strip()
    return bool(text) and not text.startswith("<")


@lru_cache(maxsize=256)
def _compiled_rule(glob: str):
    """The access-guard matcher for one rule glob. ``None`` when it will not
    compile — an uncompilable rule matches nothing, which is fail-closed."""
    try:
        from services import access_guard as ag  # noqa: PLC0415 — lazy
        return ag._compile(glob, "deny", "grant")
    except Exception:
        return None


def rule_covers(rule: str, key: str, rel: str = "") -> bool:
    """True when a path is inside the set the rule glob describes — §4
    condition 7 under ``scope="rule"``.

    *key* and *rel* are the canon/project-relative pair from
    :func:`path_key_pair`. Both are needed and both are passed to the SAME
    matcher the evaluator uses, so a rule grant covers exactly the files that
    rule would have blocked — never one more, never one fewer. Passing only
    the canon silently drops every project-scoped glob, which is a grant that
    matches nothing rather than a grant that matches too much, but is still
    wrong.
    """
    if not key or not rule_is_globbable(rule):
        return False
    compiled = _compiled_rule(str(rule))
    if compiled is None:
        return False
    leaf = key.rsplit("/", 1)[-1]
    return compiled.matches(key, rel or key, leaf)


# ── Store ──────────────────────────────────────────────────────────────────

class _Lock:
    """Cross-process advisory lock for a store's read-modify-write.

    Also used by ``override_requests`` for the machine-global request store
    (pass ``lock_file``), which had no lock at all — every mutator there does
    an unsynchronised load→mutate→save over one file shared by every project
    and session on the box, so two concurrent decisions could lose a row.
    Rule-scoped grants raise the write rate, which is what made it worth
    fixing rather than noting.

    ``_atomic_write_json`` guarantees a reader never sees a torn file; it does
    NOT make read-modify-write atomic, and two PreToolUse hook subprocesses
    racing the last use of a single-use grant is exactly that. O_EXCL create is
    the portable primitive available in a hook subprocess with no dependencies.
    Failure to acquire is not fatal — the caller proceeds and the worst case is
    the pre-lock behaviour, never a wrongly-allowed call, because the consume
    re-reads under the lock and decrements what it actually found.
    """

    def __init__(self, project_path, lock_file: str = _LOCK_FILE):
        self.path = Path(project_path) / lock_file
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
            if not _expired(g, at) and _has_uses(g)
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
         request_id: str = "", policy=None, layers_key: str = "",
         scope: str = SCOPE_CALL) -> dict:
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

    ``scope``: ``call`` (default, the historical shape) or ``rule`` — see
    :data:`SCOPES`. A rule grant needs ``override.allow_rule_grants``, a
    globbable rule, and gets its own TTL ceiling plus an idle window; the
    ``path`` it is minted from is still recorded, so the audit line and the
    UI can say which refusal produced it.
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
    if scope not in SCOPES:
        raise ValueError(f"unknown grant scope '{scope}' — expected one of: "
                         + ", ".join(SCOPES))
    key, rel = path_key_pair(path, project_path)
    if not key:
        raise ValueError("target path is not representable (UNC / ADS / 8.3 "
                         "alias) — it can never match a grant")
    if op_policy.forbidden_target(key):
        raise ValueError(f"{op_policy.TAG_NOT_ESCALATABLE} {Path(key).name} is "
                         "a vault or policy file — no grant may ever cover it")
    if scope == SCOPE_RULE:
        if not policy.allow_rule_grants:
            raise ValueError(
                "rule-scoped grants are disabled for this project "
                "(`override.allow_rule_grants` is false)")
        if not rule_is_globbable(rule):
            raise ValueError(
                f"'{rule}' names a behaviour, not a path set — it cannot be "
                "widened to a rule-scoped grant; approve this call instead")
        if not rule_covers(rule, key, rel):
            # Belt and braces: if the glob the user is approving does not even
            # cover the path they were looking at, the grant would silently
            # authorise a DIFFERENT set of files than the one on screen.
            raise ValueError(
                f"rule '{rule}' does not cover {Path(key).name} — refusing to "
                "mint a rule grant whose reach does not match the refusal")
    ttl = (policy.clamp_rule_ttl(ttl_s) if scope == SCOPE_RULE
           else policy.clamp_ttl(ttl_s))
    grant = {
        "id": new_id(),
        "request_id": request_id or "",
        "session_id": str(session_id or ""),
        "scope": scope,
        "layer": layer,
        "rule": str(rule),
        "tool": str(tool),
        "op": str(op),
        "path_key": key,
        "expires_at": iso(now() + timedelta(seconds=ttl)),
        # None = unlimited within the TTL + idle window. Only a rule grant
        # gets it; every other shape keeps a countable budget.
        "uses_remaining": None if scope == SCOPE_RULE else policy.clamp_uses(uses),
        "idle_s": policy.rule_idle_s() if scope == SCOPE_RULE else None,
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
        "scope": scope, "idle_s": grant["idle_s"],
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
            if _expired(g) or not _has_uses(g):
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
             tool: str, op: str, key: str, at: datetime,
             rel: str = "") -> bool:
    """All nine conditions of §4. Any mismatch ⇒ ordinary denial.

    Conditions 2, 3, 4, 6, 8 and 9 are identical for every scope. A
    ``scope="rule"`` grant relaxes exactly two of them, and only in the
    directions §4.1 spells out:

    * **5 (tool)** — from the exact tool to a declared op-class
      (``override_policy.same_tool_class``). An unclassed tool is never
      widened.
    * **7 (path)** — from the exact canon key to "inside the path set the
      rule glob describes", matched by the SAME compiler that produced the
      denial, so a grant can never reach a file the rule would not have
      blocked.

    Everything else — the vault, the policy files, Tier-0 — is unreachable
    before this function runs: ``find``/``consume`` refuse a
    ``forbidden_target`` key on every call, not just at mint.
    """
    if (str(grant.get("session_id") or "") != str(session_id or "")   # 2
            or grant.get("layer") != layer                            # 3
            or grant.get("rule") != rule                              # 4
            or grant.get("op") != op                                  # 6
            or _expired(grant, at)                                    # 8
            or not _has_uses(grant)):                                 # 9
        return False
    if grant.get("scope") == SCOPE_RULE:
        return (op_policy.same_tool_class(grant.get("tool") or "", tool)  # 5
                and rule_covers(rule, key, rel))                         # 7
    return grant.get("tool") == tool and grant.get("path_key") == key  # 5, 7
    # Condition 1 (project_path) is structural: the store is project-local.


def find(project_path=".", *, session_id: str, layer: str, rule: str,
         tool: str, op: str, path) -> dict | None:
    """The grant that WOULD authorise this call — no consumption, no audit."""
    key, rel = path_key_pair(path, project_path)
    if not key or op_policy.forbidden_target(key):
        return None
    grants, corrupt = load(project_path)
    if corrupt:
        return None
    at = now()
    for g in grants:
        if _matches(g, session_id=session_id, layer=layer, rule=rule,
                    tool=tool, op=op, key=key, at=at, rel=rel):
            return g
    return None


def _near_miss(project_path, grants: list, *, session_id: str, layer: str,
               rule: str, tool: str, op: str, key: str) -> None:
    """Record 'you approved X, the agent then tried Y' (§4, §12.6)."""
    for g in grants:
        if g.get("session_id") != session_id or g.get("layer") != layer:
            continue
        if _expired(g) or not _has_uses(g):
            continue
        wide = g.get("scope") == SCOPE_RULE
        differs = [f for f, want, got in (
            ("rule", g.get("rule"), rule),
            # A rule grant is SUPPOSED to span tools and paths, so reporting
            # those as near-misses would bury the signal ('you approved X,
            # the agent tried Y') under noise the user deliberately allowed.
            ("tool", g.get("tool"), tool if not wide else g.get("tool")),
            ("op", g.get("op"), op),
            ("path", g.get("path_key"), key if not wide else g.get("path_key")),
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
    key, rel = path_key_pair(path, project_path)
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
                        tool=tool, op=op, key=key, at=at, rel=rel):
                hit = g
                break
        if hit is None:
            _near_miss(project_path, grants, session_id=session_id, layer=layer,
                       rule=rule, tool=tool, op=op, key=key)
            return None
        left = _uses_left(hit)
        if left is not None:
            hit["uses_remaining"] = left - 1
        # last_used_at is not bookkeeping for a rule grant — it is what the
        # idle window is measured from, so it must be written even when
        # there is no counter to decrement.
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
    left = _uses_left(grant)
    if left is None:
        # A rule grant is the one shape with no countdown, so the line has to
        # say what it actually covers — otherwise a standing capability reads
        # in the transcript exactly like a single approval.
        budget = f"covers every path {rule} matches, this session"
    else:
        budget = f"{left} uses left"
    return (
        f"{op_policy.TAG_GRANTED} Allowed by override {grant.get('id', '?')} "
        f"(approved on {grant.get('granted_by', '?')} {stamp}, {budget}). "
        f"The rule {rule} is still in force."
    )


# ── The gate the hooks call ────────────────────────────────────────────────

def gate_access(project_path, denial, *, tool: str, op: str, path,
                session_id: str, peek: bool = False) -> str | None:
    """Consult grants for an access/mask ``Denial``. ``None`` ⇒ stay denied.

    Order is load-bearing: **policy first, grants second**. A live grant is
    voided the moment `override.enabled` (or its layer) is switched off
    (§12.8), and a non-escalatable denial never reaches the store at all.

    ``peek=True`` answers the same question WITHOUT burning a use, for a
    caller that must not consume until it knows the call will proceed. The
    hook dispatcher settles grants only after every PreToolUse sub-hook has
    voted (v2.102.0): before that, this gate consumed the use first and a
    strict-mode discipline deny from the next sub-hook then won the merge —
    grant spent, nothing written, user asked twice for one edit.
    """
    layer_key = op_policy.rule_class_for_denial(denial)
    if layer_key is None:
        return None
    policy = op_policy.resolve(project_path)
    if not policy.escalatable(layer_key):
        return None
    gate = op_policy.GATE_FOR_LAYER_KEY[layer_key]
    rule = str(getattr(denial, "rule", "") or "")
    lookup = find if peek else consume
    grant = lookup(project_path, session_id=session_id, layer=gate, rule=rule,
                   tool=tool, op=op, path=path)
    return granted_context(grant, rule) if grant else None


def gate_discipline(project_path, *, tool: str, path, session_id: str,
                    peek: bool = False) -> str | None:
    """Consult grants for the tool-discipline native-write block.

    Runs **after** the vault guard, which stays unconditional. ``peek`` as
    in :func:`gate_access`.
    """
    policy = op_policy.resolve(project_path)
    if not policy.escalatable(op_policy.LAYER_DISCIPLINE):
        return None
    lookup = find if peek else consume
    grant = lookup(project_path, session_id=session_id,
                   layer=op_policy.GATE_DISCIPLINE,
                   rule=op_policy.RULE_DISCIPLINE, tool=tool, op="write",
                   path=path)
    if not grant:
        return None
    return granted_context(grant, "tool discipline")


def gate_shell(project_path, *, path, session_id: str) -> str | None:
    """Consult grants for the c3_shell soft-warn (docs/confirm-guard.md §7).

    Grant identity is fixed: layer=shell, rule=<shell:soft-warn>,
    tool="c3_shell", op="run", path_key=the effective cwd. The stated
    limitation: the grant binds to the CWD, not the command text — acceptable
    because the soft-warn is a caveat on an already-executed command, never a
    block. `_BLOCKED` never consults grants and never will (spec §2).
    Normal layer semantics apply: `override.enabled` AND `layers.shell_warn`.
    """
    policy = op_policy.resolve(project_path)
    if not policy.escalatable(op_policy.LAYER_SHELL_WARN):
        return None
    grant = consume(project_path, session_id=session_id,
                    layer=op_policy.GATE_SHELL,
                    rule=op_policy.RULE_SHELL_WARN, tool="c3_shell", op="run",
                    path=path)
    if not grant:
        return None
    return granted_context(grant, "shell soft-warn")
