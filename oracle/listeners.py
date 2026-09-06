"""Serve the Oracle on its configured bind host AND on loopback.

Why
---
``bind_host`` exists so a phone on the tailnet can reach the Oracle: set it
to the Tailscale address and the server listens there — and ONLY there. A
process on the same machine dialling ``http://127.0.0.1:3331`` was refused,
which broke every same-box client that reasonably assumes loopback: the
desk tray client, ``c3 oracle open``'s health probe, the single-instance
check in ``run_oracle``, curl in a terminal.

So when the bind host is a specific non-loopback address, the same Flask app
is served a second time on ``127.0.0.1``, same port. Same app object means
the same guards: the Host allowlist (``core.web_security``) always admits
``localhost`` / ``127.0.0.1``, the Bearer gates do not care which socket a
request arrived on, and ``local_session.is_local`` already treats loopback
as same-machine. A wildcard bind (``0.0.0.0`` / ``::``) already answers on
loopback, and a loopback bind needs no second socket, so neither gets one.

Config: ``loopback_listener`` in ``~/.c3/oracle/config.json`` (default
true). Off = the pre-v2.125.0 behaviour, one socket on ``bind_host``.

Shape
-----
``werkzeug.serving.make_server`` builds one threaded WSGI server per host;
every listener but the first runs ``serve_forever`` in a daemon thread, the
first blocks the calling thread exactly as ``app.run`` did. ``shutdown()``
stops every listener, and ``serve_forever`` calls it on the way out
(Ctrl-C included) so a secondary socket never outlives the primary.
"""

from __future__ import annotations

import ipaddress
import logging
import threading

LOOPBACK_HOST = "127.0.0.1"

# Binds that already answer on loopback, so a second socket would only
# collide with the first.
_WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "*"})

_log = logging.getLogger("oracle.listeners")


def is_loopback_host(host: str | None) -> bool:
    """True for ``localhost`` and any loopback IP literal (v4 or v6)."""
    h = (host or "").strip().lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h.strip("[]")).is_loopback
    except ValueError:
        return False


def listener_hosts(bind_host: str | None, loopback_listener: bool = True) -> list[str]:
    """The hosts to listen on, primary first.

    >>> listener_hosts("100.77.40.101")
    ['100.77.40.101', '127.0.0.1']
    >>> listener_hosts("127.0.0.1")
    ['127.0.0.1']
    >>> listener_hosts("0.0.0.0")
    ['0.0.0.0']
    >>> listener_hosts("100.77.40.101", loopback_listener=False)
    ['100.77.40.101']
    """
    primary = (bind_host or "").strip() or LOOPBACK_HOST
    hosts = [primary]
    if not loopback_listener:
        return hosts
    if primary.lower() in _WILDCARD_HOSTS or is_loopback_host(primary):
        return hosts
    hosts.append(LOOPBACK_HOST)
    return hosts


class OracleListeners:
    """One Flask app, N sockets. See the module docstring."""

    def __init__(self, app, hosts: list[str], port: int, *, threaded: bool = True):
        if not hosts:
            raise ValueError("at least one host is required")
        self.app = app
        self.hosts = list(hosts)
        self.port = int(port)
        self.threaded = threaded
        self.servers: list = []
        # Servers whose serve_forever loop has been entered. socketserver's
        # shutdown() waits on an Event that only serve_forever sets, so
        # calling it on a server that was never served blocks forever.
        self._serving: list = []
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._started = False
        self._stopped = False

    # ── lifecycle ──────────────────────────────────────────

    def start(self) -> OracleListeners:
        """Bind every socket. The primary is NOT served yet — call
        :meth:`serve_forever` (blocking) or :meth:`serve_in_background`.

        A secondary bind that fails (something else already holds the port on
        loopback, say) is logged and skipped; the primary failing raises, as
        ``app.run`` would have.
        """
        from werkzeug.serving import make_server

        with self._lock:
            if self._started:
                return self
            self._started = True
        primary_host = self.hosts[0]
        primary = make_server(primary_host, self.port, self.app, threaded=self.threaded)
        self.servers.append(primary)
        # A port of 0 asks the OS for one; every secondary must then share it.
        if self.port == 0:
            self.port = int(primary.socket.getsockname()[1])
        for host in self.hosts[1:]:
            try:
                srv = make_server(host, self.port, self.app, threaded=self.threaded)
            except (OSError, SystemExit) as exc:
                # werkzeug turns a failed bind into sys.exit(1) after printing
                # the reason; for a SECONDARY socket that is a warning, not
                # the end of the server.
                _log.warning("loopback listener on %s:%s not started: %s",
                             host, self.port, exc)
                continue
            self.servers.append(srv)
            self._serving.append(srv)
            t = threading.Thread(target=srv.serve_forever, daemon=True,
                                 name=f"oracle-listener-{host}")
            t.start()
            self._threads.append(t)
        return self

    def serve_forever(self) -> None:
        """Serve the primary listener on the calling thread until shutdown.

        Every other listener is stopped on the way out, whichever way the
        primary exits (``shutdown()`` from another thread, Ctrl-C, an error).
        """
        if not self._started:
            self.start()
        primary = self.servers[0]
        self._serving.append(primary)
        try:
            primary.serve_forever()
        finally:
            self.shutdown()

    def serve_in_background(self) -> OracleListeners:
        """Serve the primary on a daemon thread too (tests, embedders)."""
        if not self._started:
            self.start()
        primary = self.servers[0]
        self._serving.append(primary)
        t = threading.Thread(target=primary.serve_forever, daemon=True,
                             name=f"oracle-listener-{self.hosts[0]}")
        t.start()
        self._threads.insert(0, t)
        return self

    def shutdown(self) -> None:
        """Stop every listener and close its socket. Idempotent, and safe to
        call from any thread that is not itself inside ``serve_forever``."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        for srv in self.servers:
            if srv in self._serving:
                try:
                    srv.shutdown()
                except Exception:
                    pass
            try:
                srv.server_close()
            except Exception:
                pass
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(timeout=5)

    # ── introspection ──────────────────────────────────────

    @property
    def addresses(self) -> list[tuple[str, int]]:
        """``(host, port)`` per live listener, primary first."""
        out = []
        for srv in self.servers:
            try:
                name = srv.socket.getsockname()
                out.append((str(name[0]), int(name[1])))
            except OSError:
                continue
        return out

    @property
    def urls(self) -> list[str]:
        out = []
        for host, port in self.addresses:
            shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
            if ":" in shown and not shown.startswith("["):
                shown = f"[{shown}]"
            out.append(f"http://{shown}:{port}")
        return out
