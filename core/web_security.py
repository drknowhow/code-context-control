"""Localhost-only security guard for C3's Flask dashboards (UI, Hub, Oracle).

Why this exists
---------------
C3's web servers bind to loopback (``127.0.0.1``) by default. A loopback bind
keeps the server off the LAN, but it does **not** protect against requests made
by a web page running in the user's own browser. Two classic attacks defeat a
"loopback is safe" assumption:

* **Cross-origin / CSRF** — any website the user visits can ``fetch()`` against
  ``http://localhost:<port>/...``. With no auth and a permissive CORS policy,
  that page could drive state-changing endpoints (launch an IDE command, add a
  malicious MCP server, downgrade permissions, wipe data).
* **DNS rebinding** — an attacker domain re-resolves to ``127.0.0.1`` after the
  page loads, so the browser sends requests to the local server with the
  *attacker's* hostname in the ``Host`` header.

This module adds two cheap, standard defenses that together close that gap
without requiring the dashboard JavaScript to change (same-origin requests pass
naturally):

1. **Host-header allowlist** — defeats DNS rebinding. The rebound request
   carries the attacker's hostname in ``Host``; anything not in the allowlist is
   rejected.
2. **Origin/Referer check on state-changing requests** — defeats cross-origin
   CSRF. Browsers always attach ``Origin`` to ``POST``/``PUT``/``DELETE``/
   ``PATCH`` (cross-origin *and* same-origin), so a mismatched origin is a
   reliable CSRF signal.

Non-browser clients (curl, the Oracle Discovery REST/MCP consumers) send no
``Origin`` and a loopback ``Host``, so they are unaffected. Bearer-token auth
(Oracle discovery) still applies on top of this guard.

For an intentional non-loopback bind (an explicit, already-warned opt-in), pass
the configured bind host so the user can still reach their own dashboard; remote
hosts that need access can be added via the optional ``extra`` argument
(wired from an ``allowed_hosts`` config list by the caller).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

# Hostnames that always denote "this machine". Note: a literal ``0.0.0.0`` never
# appears as a browser Host header, so it is intentionally excluded — binding to
# 0.0.0.0 means the client connects by some concrete IP/name, which must be added
# via ``extra`` (``allowed_hosts`` config) by the operator.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def _hostname(value: str | None) -> str:
    """Extract a bare, lowercased hostname from a Host header or Origin/Referer URL.

    Handles values with or without a scheme and with or without a port, including
    IPv6 literals like ``[::1]:3333``.
    """
    if not value:
        return ""
    value = value.strip()
    # A bare Host header ("localhost:3333") has no scheme; prefix "//" so urlsplit
    # parses it as a netloc rather than a path.
    if "://" not in value:
        value = "//" + value
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return ""
    return host.lower()


def allowed_hostnames(bind_host: str | None = None,
                      extra: Iterable[str] | None = None) -> set[str]:
    """Build the set of acceptable hostnames for this server.

    Always includes loopback names. ``bind_host`` (unless it is the wildcard
    ``0.0.0.0`` or empty) and any ``extra`` hosts are added so an intentional
    non-loopback deployment remains reachable.
    """
    hosts = set(_LOOPBACK_HOSTS)
    bh = (bind_host or "").strip().lower()
    if bh and bh not in ("0.0.0.0", "::", "*"):
        hosts.add(bh)
    if extra:
        for h in extra:
            h = (h or "").strip().lower()
            if h:
                hosts.add(h)
    return hosts


def check_request(request, allowed: set[str]) -> tuple[bool, str]:
    """Return ``(ok, reason)``. ``ok == False`` means the request must be 403'd.

    ``request`` is a Flask request (anything exposing ``.host``, ``.method`` and
    ``.headers.get``).
    """
    # 1) Host-header allowlist — anti DNS-rebinding.
    host = _hostname(getattr(request, "host", "") or "")
    if host and host not in allowed:
        return False, f"host '{host}' is not allowlisted"

    # 2) Origin check — anti cross-origin CSRF. Browsers always send Origin on
    #    state-changing requests, so a mismatch is a reliable CSRF signal. When
    #    Origin is absent (typical for curl / API clients), fall back to Referer
    #    only for mutating methods; a fully header-less request is treated as a
    #    non-browser caller and allowed (it cannot be a CSRF from a page).
    origin = request.headers.get("Origin")
    if origin:
        if _hostname(origin) not in allowed:
            return False, "cross-origin request blocked (Origin)"
    elif request.method in _MUTATING_METHODS:
        referer = request.headers.get("Referer")
        if referer and _hostname(referer) not in allowed:
            return False, "cross-origin request blocked (Referer)"
    return True, ""


def cors_origin(request, allowed: set[str]) -> str | None:
    """Echo the request Origin in Access-Control-Allow-Origin only if it is
    same-origin/allowlisted. The wildcard ``*`` is never used.
    """
    origin = request.headers.get("Origin")
    if origin and _hostname(origin) in allowed:
        return origin
    return None


def guard_summary() -> dict:
    """Compact, serializable status for health endpoints — confirms to operators
    that the localhost guard is active (it otherwise enforces silently)."""
    return {
        "active": True,
        "host_allowlist": True,
        "csrf": "origin+referer",
        "cors": "scoped",
    }


def install_guard(app, get_allowed: Callable[[], set[str]]) -> None:
    """Register the Host/Origin guard and a tightened CORS policy on a Flask app.

    ``get_allowed`` is called per-request so live config changes (e.g. a hub
    ``host`` edit or an ``allowed_hosts`` list) are honoured without a restart.
    Registering this BEFORE any other ``before_request`` (e.g. a bearer-token
    guard) ensures cross-origin requests are rejected first.
    """
    from flask import jsonify, request

    @app.before_request
    def _c3_security_guard():  # noqa: ANN202 - Flask hook
        # Let CORS preflight through; the after_request handler answers it and
        # only reflects an allowlisted Origin, so disallowed origins still fail.
        if request.method == "OPTIONS":
            return None
        ok, reason = check_request(request, get_allowed())
        if not ok:
            return jsonify({"error": f"blocked: {reason}"}), 403
        return None

    @app.after_request
    def _c3_cors(response):  # noqa: ANN202 - Flask hook
        origin = cors_origin(request, get_allowed())
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
        return response

    import logging
    logging.getLogger("c3.web_security").info(
        "localhost web guard active — Host allowlist + Origin/Referer CSRF + scoped CORS"
    )
