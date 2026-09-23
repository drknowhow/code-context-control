"""Temporary and per-session modes for Tier-1 builtin guards.

A lease loosens one builtin to ``confirm`` or ``allow`` until it expires, for
every session (``session_id`` empty) or for one agent session. It never
touches the permanent two-key mode: when a lease lapses the guard is back
where it was, with nothing to undo.

Leases are two-key like modes. A row in ``~/.c3/builtin_leases.json`` counts
only while the keyring (service ``c3-access``, account ``lease|<id>``) holds
the sha256 of that exact row, so a row written, copied or extended by hand
has no matching attestation and is ignored. Every failure path ignores the
lease, which leaves the guard on.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from services import access_guard as ag
from services.atomic_json import write_json_atomic

LEASE_MODES = ("confirm", "allow")
MIN_TTL_S = 60
MAX_TTL_S = 8 * 3600
_LOCK_FILE = ".c3/builtin_leases.lock"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


def _store():
    base = ag._global_base()
    return base / ".c3" / ag.LEASES_FILE if base is not None else None


def _account(lease_id: str) -> str:
    return f"lease|{lease_id}"


def _digest(row: dict) -> str:
    blob = json.dumps(row, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _attested(row: dict) -> bool:
    try:
        import keyring  # noqa: PLC0415 — lazy, as in access_guard
        return keyring.get_password(ag._ACCESS_KEYRING_SERVICE,
                                    _account(str(row.get("id")))) == _digest(row)
    except Exception:
        return False


def _load() -> list:
    path = _store()
    if path is None or not path.is_file():
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8")).get("leases")
    except (OSError, ValueError, AttributeError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _live(row: dict, at: datetime) -> bool:
    exp = _parse(row.get("expires_at"))
    return exp is not None and at < exp


def active() -> list:
    """Live, attested leases, soonest to expire first."""
    at = _now()
    return sorted((r for r in _load() if _live(r, at) and _attested(r)),
                  key=lambda r: str(r.get("expires_at")))


def live_modes(project_path: str, session_id: str) -> dict:
    """``{canonical_glob: mode}`` from the leases that reach this project and
    session. ``allow`` wins over ``confirm`` when two leases cover one glob."""
    rows = _load()
    if not rows:
        return {}
    realms = {"global", ag.realm("project", project_path)}
    at = _now()
    out = {}
    for r in rows:
        if (r.get("realm") in realms and r.get("mode") in LEASE_MODES
                and r.get("session_id", "") in ("", session_id)
                and _live(r, at) and _attested(r)):
            if out.get(r["glob"]) != "allow":
                out[r["glob"]] = r["mode"]
    return out


def _save(rows: list) -> None:
    write_json_atomic(_store(), {"leases": rows})


def _lock():
    from services.override_grants import _Lock  # noqa: PLC0415 — off the hot path
    return _Lock(ag._global_base(), _LOCK_FILE)


def mint(glob, mode: str, *, scope: str, project_path: str = ".",
         session_id: str = "", ttl_s: int, created_by: str) -> dict:
    """Create a lease and return its row.

    Raises ValueError for a glob that is not Tier 1, a mode other than
    confirm/allow, a TTL outside 60 s–8 h, a scope without a realm, or a
    keyring that will not attest (attestation is written first, so a lease
    that could never count is never stored).
    """
    if mode not in LEASE_MODES:
        raise ValueError(f"a lease mode must be one of: {', '.join(LEASE_MODES)}")
    ag.builtin_strictness(glob, mode)
    try:
        ttl = int(ttl_s)
    except (TypeError, ValueError):
        raise ValueError("ttl_s must be a whole number of seconds") from None
    if not MIN_TTL_S <= ttl <= MAX_TTL_S:
        raise ValueError(f"ttl_s must be between {MIN_TTL_S} and {MAX_TTL_S}")
    if scope not in ("global", "project"):
        raise ValueError("scope must be 'global' or 'project'")
    realm = ag.realm(scope, project_path)
    if not realm or _store() is None:
        raise ValueError("no home directory to hold the lease")
    at = _now()
    row = {
        "id": "bls_" + secrets.token_hex(6),
        "glob": ag._norm_builtin(glob),
        "mode": mode,
        "scope": scope,
        "realm": realm,
        "project_path": str(project_path) if scope == "project" else "",
        "session_id": str(session_id or ""),
        "created_at": at.isoformat(),
        "expires_at": (at + timedelta(seconds=ttl)).isoformat(),
        "created_by": str(created_by),
    }
    try:
        import keyring  # noqa: PLC0415
        keyring.set_password(ag._ACCESS_KEYRING_SERVICE, _account(row["id"]),
                             _digest(row))
    except Exception:
        raise ValueError("keyring unavailable, so the lease cannot be attested "
                         "and would not take effect") from None
    with _lock():
        rows = [r for r in _load() if _live(r, at)]
        rows.append(row)
        _save(rows)
    return row


def revoke(lease_id: str) -> dict | None:
    """Remove a lease; returns the removed row, or None if it was not live.
    Expired rows are swept on the way."""
    if _store() is None:
        return None
    at = _now()
    with _lock():
        rows = _load()
        gone = next((r for r in rows if r.get("id") == lease_id), None)
        _save([r for r in rows if r is not gone and _live(r, at)])
    if gone is None:
        return None
    try:
        import keyring  # noqa: PLC0415
        keyring.set_password(ag._ACCESS_KEYRING_SERVICE, _account(lease_id), "")
    except Exception:  # a keyring that cannot be written cannot be read either
        pass           # so a re-added copy of the row stays unattested
    return gone if _live(gone, at) else None
