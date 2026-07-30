"""Per-boot local dashboard session for the Oracle server.

The Oracle dashboard (oracle_ui.html bundle) runs in a local browser and calls mutating
``/api/*`` endpoints without a Bearer token. Rather than injecting the durable
Discovery token into page JS (readable by any XSS) or trusting every loopback
process (exactly the attacker the write gate exists to stop), the server
issues a per-boot HttpOnly cookie. The secret lives in process memory, is
never persisted, and rotates on restart.

Bootstrap (#31). ``GET /`` alone does NOT issue a cookie: a local process
running as a *different* OS user could otherwise fetch the page and obtain
one. Instead the cookie is exchanged for a **single-use** code:

* on boot the server writes a bootstrap KEY to ``~/.c3/oracle/bootstrap.key``,
  readable only by the owning OS user (home-directory ACLs are the gate, the
  same assumption ``~/.c3/secrets.enc`` already makes);
* ``c3 oracle open`` reads that key and asks the server to mint a one-time
  code (``POST /api/session/bootstrap``);
* the browser presents the code once at ``GET /?bootstrap=<code>``, which
  consumes it, sets the cookie, and redirects to a clean ``/``.

Codes are single-use and short-lived, so a code leaked via shell history,
terminal scrollback, or a browser-history sync is not a durable credential.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path

COOKIE_NAME = "c3_oracle_session"
BOOTSTRAP_PARAM = "bootstrap"
BOOTSTRAP_KEY_FILENAME = "bootstrap.key"

# Regenerated on every server start; never written to disk.
_BOOT_SECRET = secrets.token_urlsafe(32)

# Authorizes minting a one-time code. Written to disk (owner-only) so a
# same-user CLI can read it; a different OS user cannot.
_BOOTSTRAP_KEY = secrets.token_urlsafe(32)

# Minted codes: code -> expiry timestamp. Single-use, short TTL, capped so a
# caller that mints in a loop cannot grow this without bound.
CODE_TTL_SECONDS = 120
_MAX_OUTSTANDING_CODES = 32
_codes: dict[str, float] = {}
_codes_lock = threading.Lock()

_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def write_bootstrap_key(oracle_dir: Path) -> Path:
    """Persist the per-boot bootstrap key with owner-only permissions.

    ``chmod`` is a near no-op for ACLs on Windows, but a file under the user's
    profile already inherits an owner+Administrators ACL there, so the
    protection holds on both platforms for the threat this addresses (a
    different, non-admin OS user).
    """
    oracle_dir.mkdir(parents=True, exist_ok=True)
    path = oracle_dir / BOOTSTRAP_KEY_FILENAME
    path.write_text(_BOOTSTRAP_KEY, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def read_bootstrap_key(oracle_dir: Path) -> str:
    """Read the key a running server wrote. Empty string when absent."""
    try:
        return (oracle_dir / BOOTSTRAP_KEY_FILENAME).read_text("utf-8").strip()
    except OSError:
        return ""


def verify_bootstrap_key(presented: str | None) -> bool:
    if not presented:
        return False
    return secrets.compare_digest(presented, _BOOTSTRAP_KEY)


def _purge_expired(now: float) -> None:
    for code in [c for c, exp in _codes.items() if exp <= now]:
        _codes.pop(code, None)


def mint_code() -> str:
    """Issue a single-use bootstrap code."""
    now = time.monotonic()
    code = secrets.token_urlsafe(24)
    with _codes_lock:
        _purge_expired(now)
        if len(_codes) >= _MAX_OUTSTANDING_CODES:
            # Drop the oldest rather than refuse: the caller already proved
            # possession of the bootstrap key, so this is hygiene, not a gate.
            oldest = min(_codes, key=_codes.get)
            _codes.pop(oldest, None)
        _codes[code] = now + CODE_TTL_SECONDS
    return code


def consume_code(code: str | None) -> bool:
    """Redeem a code exactly once. False when unknown, expired, or reused."""
    if not code:
        return False
    now = time.monotonic()
    with _codes_lock:
        _purge_expired(now)
        # Compare against stored keys in constant time to avoid leaking which
        # prefix matched; the dict lookup itself is not constant-time.
        for candidate in list(_codes):
            if secrets.compare_digest(candidate, code):
                del _codes[candidate]
                return True
    return False


def is_loopback(remote_addr: str | None) -> bool:
    """True when the request came from this machine's loopback interface."""
    return (remote_addr or "") in _LOOPBACK


def attach_cookie(response):
    """Set the session cookie on *response*.

    ``HttpOnly`` keeps it out of page JS; ``SameSite=Strict`` plus the
    existing Origin/Referer guard covers CSRF. No ``Secure`` flag: the
    dashboard is plain HTTP on loopback by design.
    """
    response.set_cookie(
        COOKIE_NAME,
        _BOOT_SECRET,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


def verify(cookie_value: str | None) -> bool:
    """Constant-time check of a presented cookie against the boot secret."""
    if not cookie_value:
        return False
    return secrets.compare_digest(cookie_value, _BOOT_SECRET)
