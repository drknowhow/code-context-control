"""The Oracle's loopback listener (``oracle/listeners.py``, v2.125.0).

With ``bind_host`` set to a Tailscale address the Oracle used to answer
ONLY there: ``http://127.0.0.1:3331`` was refused on the very machine it ran
on. Now a specific non-loopback bind gets a second socket on ``127.0.0.1``
(config ``loopback_listener``, default true), same Flask app, same guards.

Three layers: the pure host-list decision (no sockets), the real
two-socket server on an OS-chosen port against a tiny app, and the real
Oracle app's Host allowlist admitting loopback names under a remote bind.
"""
from __future__ import annotations

import os
import socket
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("C3_ORACLE_API_KEY", "loopback-test-key")

from flask import Flask, jsonify  # noqa: E402

import oracle.oracle_server as srv  # noqa: E402
from oracle.config import DEFAULTS  # noqa: E402
from oracle.listeners import (  # noqa: E402
    OracleListeners,
    is_loopback_host,
    listener_hosts,
)


def _lan_ipv4() -> str | None:
    """A non-loopback IPv4 address of this host, or None when there is none
    (an offline CI box). Nothing is sent: UDP connect only picks a route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        addr = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    if not addr or addr.startswith("127."):
        return None
    return addr


def _get(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — loopback test URL
        return r.status, r.read()


# ── Host-list decision (no sockets) ──────────────────────


class TestListenerHosts(unittest.TestCase):

    def test_remote_bind_adds_loopback(self):
        self.assertEqual(listener_hosts("100.77.40.101"),
                         ["100.77.40.101", "127.0.0.1"])
        self.assertEqual(listener_hosts("192.168.1.20"),
                         ["192.168.1.20", "127.0.0.1"])
        self.assertEqual(listener_hosts("myhost.tailnet.ts.net"),
                         ["myhost.tailnet.ts.net", "127.0.0.1"])

    def test_loopback_bind_needs_no_second_socket(self):
        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.5"):
            with self.subTest(host=host):
                self.assertEqual(listener_hosts(host), [host])

    def test_wildcard_bind_already_answers_on_loopback(self):
        for host in ("0.0.0.0", "::", "*"):
            with self.subTest(host=host):
                self.assertEqual(listener_hosts(host), [host])

    def test_empty_bind_is_loopback(self):
        self.assertEqual(listener_hosts(""), ["127.0.0.1"])
        self.assertEqual(listener_hosts(None), ["127.0.0.1"])

    def test_switch_off_restores_one_socket(self):
        self.assertEqual(listener_hosts("100.77.40.101", loopback_listener=False),
                         ["100.77.40.101"])

    def test_is_loopback_host(self):
        for host in ("127.0.0.1", "127.1.2.3", "::1", "localhost", "LOCALHOST"):
            self.assertTrue(is_loopback_host(host), host)
        for host in ("100.77.40.101", "0.0.0.0", "", None, "example.com"):
            self.assertFalse(is_loopback_host(host), host)

    def test_config_default_is_on(self):
        self.assertIs(DEFAULTS["loopback_listener"], True)


# ── Real sockets ─────────────────────────────────────────


def _tiny_app() -> Flask:
    app = Flask("loopback-test")

    @app.route("/api/health")
    def health():
        return jsonify({"service": "tiny", "ok": True})

    return app


class TestTwoListeners(unittest.TestCase):

    def setUp(self):
        self.lan = _lan_ipv4()
        if self.lan is None:
            self.skipTest("no non-loopback IPv4 address on this host")
        self.listeners = None

    def tearDown(self):
        if self.listeners is not None:
            self.listeners.shutdown()

    def test_health_answers_on_both_sockets_and_shutdown_stops_both(self):
        hosts = listener_hosts(self.lan)
        self.assertEqual(hosts, [self.lan, "127.0.0.1"])
        self.listeners = OracleListeners(_tiny_app(), hosts, 0).serve_in_background()
        addrs = self.listeners.addresses
        self.assertEqual([h for h, _ in addrs], hosts)
        # One port, two sockets — a client on either address dials the same
        # number the config names.
        ports = {p for _, p in addrs}
        self.assertEqual(len(ports), 1)
        port = ports.pop()
        self.assertEqual(self.listeners.port, port)
        for url in self.listeners.urls:
            with self.subTest(url=url):
                status, body = _get(f"{url}/api/health")
                self.assertEqual(status, 200)
                self.assertIn(b'"ok":true', body)

        self.listeners.shutdown()
        for host in hosts:
            with self.subTest(host=host):
                with self.assertRaises((urllib.error.URLError, OSError)):
                    _get(f"http://{host}:{port}/api/health", timeout=1.5)

    def test_secondary_bind_failure_is_survivable(self):
        # Something else already holds loopback:port — the primary still
        # serves and the loopback socket is simply absent, not fatal.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            self.listeners = OracleListeners(_tiny_app(), [self.lan, "127.0.0.1"], port)
            self.listeners.start()
            self.listeners.serve_in_background()
            self.assertEqual([h for h, _ in self.listeners.addresses], [self.lan])
            status, _ = _get(f"http://{self.lan}:{port}/api/health")
            self.assertEqual(status, 200)
        finally:
            blocker.close()

    def test_shutdown_is_idempotent_and_safe_before_serving(self):
        self.listeners = OracleListeners(_tiny_app(), [self.lan, "127.0.0.1"], 0).start()
        # Never served: shutdown must not block on socketserver's Event.
        self.listeners.shutdown()
        self.listeners.shutdown()


class TestRunOracleWiring(unittest.TestCase):
    """``run_oracle`` reaches the listeners through ``_serve``; pin what it
    passes without binding anything."""

    def _capture(self, bind_host, **cfg_over):
        seen = {}

        class _Fake:
            def __init__(self, app, hosts, port, **kw):
                seen["app"] = app
                seen["hosts"] = list(hosts)
                seen["port"] = port
                self.servers = [object()] * len(hosts)
                self.urls = [f"http://{h}:{port}" for h in hosts]

            def start(self):
                return self

            def serve_forever(self):
                seen["served"] = True

            def shutdown(self):
                seen["shutdown"] = True

        with mock.patch.object(srv, "OracleListeners", _Fake), \
                mock.patch.object(srv.atexit, "register", lambda fn: seen.setdefault("atexit", fn)):
            srv._serve(bind_host, 3331, **cfg_over)
        return seen

    def test_remote_bind_serves_loopback_too(self):
        seen = self._capture("100.77.40.101")
        self.assertIs(seen["app"], srv.app)
        self.assertEqual(seen["hosts"], ["100.77.40.101", "127.0.0.1"])
        self.assertEqual(seen["port"], 3331)
        self.assertTrue(seen["served"])
        # Shutdown is registered for interpreter exit (service stop).
        self.assertTrue(callable(seen["atexit"]))

    def test_switch_off_serves_one_socket(self):
        seen = self._capture("100.77.40.101", loopback_listener=False)
        self.assertEqual(seen["hosts"], ["100.77.40.101"])

    def test_loopback_bind_is_unchanged(self):
        seen = self._capture("127.0.0.1")
        self.assertEqual(seen["hosts"], ["127.0.0.1"])


# ── Host allowlist on the real app ───────────────────────


class TestHostAllowlistAdmitsLoopback(unittest.TestCase):
    """A request that arrives on the loopback socket carries ``Host:
    127.0.0.1:3331`` (or ``localhost``) while ``bind_host`` names the
    Tailscale address. ``core.web_security`` must admit it, and must still
    refuse a rebound hostname."""

    @classmethod
    def setUpClass(cls):
        os.environ["C3_ORACLE_API_KEY"] = "loopback-test-key"
        cls._prior_cfg = srv._cfg
        srv._cfg = {"bind_host": "100.77.40.101", "allowed_hosts": [],
                    "mobile_api_enabled": True, "api_rate_limit_per_min": 0}
        srv.app.config["TESTING"] = True
        cls.client = srv.app.test_client()

    @classmethod
    def tearDownClass(cls):
        srv._cfg = cls._prior_cfg

    def _health(self, host):
        return self.client.get("/api/health?probe=1", headers={"Host": host})

    def test_loopback_hosts_are_admitted_under_a_remote_bind(self):
        for host in ("127.0.0.1:3331", "127.0.0.1", "localhost:3331", "[::1]:3331"):
            with self.subTest(host=host):
                r = self._health(host)
                self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
                self.assertEqual(r.get_json()["service"], "c3-oracle")

    def test_the_bound_address_is_admitted(self):
        r = self._health("100.77.40.101:3331")
        self.assertEqual(r.status_code, 200)

    def test_a_rebound_hostname_is_still_refused(self):
        r = self._health("evil.example:3331")
        self.assertEqual(r.status_code, 403)

    def test_mobile_gateway_answers_on_loopback_host(self):
        r = self.client.get("/api/mobile/info",
                            headers={"Host": "127.0.0.1:3331",
                                     "Authorization": "Bearer loopback-test-key"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
