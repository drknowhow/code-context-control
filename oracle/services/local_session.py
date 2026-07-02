"""Per-boot local dashboard session for the Oracle server.

The Oracle dashboard (oracle_ui.html bundle) runs in a local browser and calls mutating
``/api/*`` endpoints without a Bearer token. Rather than injecting the durable
Discovery token into page JS (readable by any XSS) or trusting every loopback
process (exactly the attacker the write gate exists to stop), the server
issues a per-boot HttpOnly cookie on ``GET /`` to loopback clients only. The
secret lives in process memory, is never persisted, and rotates on restart.

Residual risk, accepted and documented: a local process running as a
*different* OS user can fetch ``GET /`` and obtain a cookie. Same-user
processes can already read the keyring token directly, so the cookie does not
regress anything relative to that baseline.
"""

from __future__ import annotations

import secrets

COOKIE_NAME = "c3_oracle_session"

# Regenerated on every server start; never written to disk.
_BOOT_SECRET = secrets.token_urlsafe(32)

_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


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
