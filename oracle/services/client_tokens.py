"""Per-client Bearer tokens for the mobile gateway (``~/.c3/oracle/clients.json``).

Why this exists
---------------
Until v2.125.0 the only credential the ``/api/mobile/*`` gateway accepted was
the single Discovery token (``api_auth``). Every phone carried that one token,
so rotating it un-paired every device at once, and the server could not tell
one client from another: every override decision was audited as
``decided_by="mobile"`` whether a phone or a desktop tray client tapped it.

A *client token* is one credential per paired device. It is minted once (by
the dashboard's pairing QR, or by ``POST /api/mobile/clients`` with the
on-disk bootstrap key), returned exactly once, and stored here only as a
SHA-256 hash. Revoking one device revokes one device.

What is stored, and where
-------------------------
``~/.c3/oracle/clients.json`` — a JSON array of rows::

    {"client_id": "desk-3f9a2c1b0d4e", "kind": "desk", "label": "office pc",
     "token_hash": "<sha256 hex>", "created": "<iso utc>",
     "last_seen": "<iso utc>|null", "revoked_at": "<iso utc>|null"}

``kind`` is one of :data:`KINDS`. The file is written through
``services.atomic_json.write_json_atomic`` and chmod'd owner-only, the same
posture as ``bootstrap.key`` beside it (``local_session.write_bootstrap_key``
explains why chmod is enough on Windows too). The plaintext token never
touches disk.

Verification is a constant-time compare of the presented token's hash against
EVERY stored hash — no early exit, so timing does not leak which row matched.
``last_seen`` is touched at most once a minute per client so a long-poll
client does not rewrite the file on every request.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from services.atomic_json import write_json_atomic

CLIENTS_FILENAME = "clients.json"

#: The client kinds the gateway knows how to attribute. ``mobile`` is the
#: phone (and the principal the legacy Discovery token maps to); ``desk`` is
#: the C3 Desk tray client. Hub stays ``desktop`` and the CLI stays ``cli`` —
#: those are not gateway clients and never hold a client token.
KINDS = ("mobile", "desk")

#: The principal a request authenticated by the Discovery token gets. Kind
#: ``mobile`` keeps every existing phone's audit attribution byte-identical.
DISCOVERY_PRINCIPAL = {"kind": "mobile", "client_id": "discovery"}

_MAX_LABEL = 64
_DEFAULT_LABELS = {"mobile": "phone", "desk": "desk"}

#: Minimum seconds between two ``last_seen`` writes for one client.
LAST_SEEN_INTERVAL_S = 60.0

_lock = threading.RLock()
# client_id -> monotonic time of the last last_seen write this process made.
_last_touch: dict[str, float] = {}


def store_path() -> Path:
    """``~/.c3/oracle/clients.json`` — resolved per call so tests that patch
    ``Path.home`` redirect it, exactly like the override-request store."""
    return Path.home() / ".c3" / "oracle" / CLIENTS_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def _load() -> list[dict]:
    path = store_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        rows = json.loads(raw)
    except ValueError:
        # A corrupt store fails CLOSED for verification (no row matches) but
        # must not take the whole gateway down: the Discovery token still
        # authenticates, and the next mint rewrites the file whole.
        return []
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("client_id")]


def _save(rows: list[dict]) -> None:
    path = store_path()
    write_json_atomic(path, rows, indent=2)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _public(row: dict) -> dict:
    """The row for the wire: everything except the hash. Never the token."""
    return {
        "client_id": row.get("client_id"),
        "kind": row.get("kind"),
        "label": row.get("label") or "",
        "created": row.get("created"),
        "last_seen": row.get("last_seen"),
        "revoked_at": row.get("revoked_at"),
    }


def _clean_label(kind: str, label: str | None) -> str:
    text = " ".join(str(label or "").split())[:_MAX_LABEL]
    return text or _DEFAULT_LABELS.get(kind, kind)


def mint(kind: str, label: str | None = None) -> tuple[dict, str]:
    """Create a client. Returns ``(public_row, token)``.

    The token is the ONLY copy that will ever exist in plaintext: the store
    keeps its SHA-256, so a later read of ``clients.json`` (or of this
    process's memory after the request returns) cannot recover it.
    """
    kind = str(kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    token = secrets.token_urlsafe(32)
    row = {
        "client_id": f"{kind}-{secrets.token_hex(6)}",
        "kind": kind,
        "label": _clean_label(kind, label),
        "token_hash": hash_token(token),
        "created": _now_iso(),
        "last_seen": None,
        "revoked_at": None,
    }
    with _lock:
        rows = _load()
        rows.append(row)
        _save(rows)
    return _public(row), token


def verify(token: str | None) -> dict | None:
    """The live row a presented token belongs to, or ``None``.

    Compares against every stored hash in constant time — a revoked row is
    still compared (so timing does not reveal revocation either) and only
    rejected afterwards. Touches ``last_seen`` at most once per
    :data:`LAST_SEEN_INTERVAL_S` per client; that write is best-effort.
    """
    if not token:
        return None
    presented = hash_token(token)
    with _lock:
        rows = _load()
        matched: dict | None = None
        for row in rows:
            stored = str(row.get("token_hash") or "")
            # Compare against a same-length string when the row is malformed
            # so the loop cost does not depend on which rows are well-formed.
            if secrets.compare_digest(presented, stored or "0" * len(presented)):
                matched = row
        if matched is None or matched.get("revoked_at"):
            return None
        cid = str(matched["client_id"])
        now = time.monotonic()
        if now - _last_touch.get(cid, -LAST_SEEN_INTERVAL_S) >= LAST_SEEN_INTERVAL_S:
            _last_touch[cid] = now
            matched["last_seen"] = _now_iso()
            try:
                _save(rows)
            except OSError:
                pass
        return _public(matched)


def principal_for(token: str | None) -> dict | None:
    """``{"kind", "client_id"}`` for a live client token, else ``None``.

    The shape every guard stashes on ``flask.g.c3_client``; the Discovery
    token's equivalent is :data:`DISCOVERY_PRINCIPAL`.
    """
    row = verify(token)
    if row is None:
        return None
    return {"kind": row["kind"], "client_id": row["client_id"]}


def list_clients(include_revoked: bool = True) -> list[dict]:
    """Every row, hashes stripped, oldest first."""
    with _lock:
        rows = _load()
    out = [_public(r) for r in rows
           if include_revoked or not r.get("revoked_at")]
    return out


def get(client_id: str) -> dict | None:
    with _lock:
        for row in _load():
            if row.get("client_id") == client_id:
                return _public(row)
    return None


def revoke(client_id: str) -> dict | None:
    """Mark a client revoked. Its token fails :func:`verify` from the next
    call on. Returns the public row, or ``None`` when unknown. Revoking an
    already-revoked client is a no-op that returns the row."""
    with _lock:
        rows = _load()
        for row in rows:
            if row.get("client_id") == client_id:
                if not row.get("revoked_at"):
                    row["revoked_at"] = _now_iso()
                    _save(rows)
                _last_touch.pop(str(client_id), None)
                return _public(row)
    return None


def reset_touch_cache() -> None:
    """Forget the per-process ``last_seen`` throttle (tests)."""
    with _lock:
        _last_touch.clear()
