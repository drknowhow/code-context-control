"""Tests for the localhost web security guard (core/web_security.py).

Covers Host-header allowlisting (anti DNS-rebinding), Origin/Referer CSRF
rejection, and the tightened (non-wildcard) CORS reflection.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask  # noqa: E402

from core import web_security as ws  # noqa: E402


def _app(bind_host=None):
    app = Flask(__name__)
    ws.install_guard(app, lambda: ws.allowed_hostnames(bind_host))

    @app.route("/api/ping", methods=["GET", "POST"])
    def _ping():
        return {"ok": True}

    return app


class TestAllowedHostnames(unittest.TestCase):
    def test_loopback_always_present(self):
        a = ws.allowed_hostnames(None)
        self.assertIn("localhost", a)
        self.assertIn("127.0.0.1", a)
        self.assertIn("::1", a)

    def test_bind_host_added(self):
        self.assertIn("192.168.1.5", ws.allowed_hostnames("192.168.1.5"))

    def test_wildcard_bind_not_added(self):
        self.assertNotIn("0.0.0.0", ws.allowed_hostnames("0.0.0.0"))

    def test_extra_hosts_added(self):
        self.assertIn("box.local", ws.allowed_hostnames(None, ["box.local"]))


class TestHostnameParsing(unittest.TestCase):
    def test_host_with_port(self):
        self.assertEqual(ws._hostname("localhost:3333"), "localhost")

    def test_origin_url(self):
        self.assertEqual(ws._hostname("https://evil.com"), "evil.com")

    def test_ipv6_literal(self):
        self.assertEqual(ws._hostname("[::1]:3333"), "::1")

    def test_empty(self):
        self.assertEqual(ws._hostname(""), "")


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.client = _app().test_client()

    def test_same_origin_post_allowed(self):
        r = self.client.post("/api/ping", headers={
            "Host": "localhost:3333", "Origin": "http://localhost:3333"})
        self.assertEqual(r.status_code, 200)

    def test_no_origin_loopback_allowed(self):
        # Non-browser client (curl/API) — no Origin, loopback Host — passes.
        r = self.client.post("/api/ping", headers={"Host": "127.0.0.1:3333"})
        self.assertEqual(r.status_code, 200)

    def test_cross_origin_post_blocked(self):
        r = self.client.post("/api/ping", headers={
            "Host": "localhost:3333", "Origin": "http://evil.com"})
        self.assertEqual(r.status_code, 403)

    def test_cross_origin_get_blocked(self):
        r = self.client.get("/api/ping", headers={
            "Host": "localhost:3333", "Origin": "http://evil.com"})
        self.assertEqual(r.status_code, 403)

    def test_dns_rebinding_host_blocked(self):
        r = self.client.post("/api/ping", headers={"Host": "evil.com"})
        self.assertEqual(r.status_code, 403)

    def test_mutating_cross_referer_blocked(self):
        r = self.client.post("/api/ping", headers={
            "Host": "localhost:3333", "Referer": "http://evil.com/x"})
        self.assertEqual(r.status_code, 403)

    def test_cors_reflects_allowed_origin_not_wildcard(self):
        r = self.client.get("/api/ping", headers={
            "Host": "localhost:3333", "Origin": "http://localhost:3333"})
        acao = r.headers.get("Access-Control-Allow-Origin")
        self.assertEqual(acao, "http://localhost:3333")
        self.assertNotEqual(acao, "*")

    def test_preflight_from_evil_origin_gets_no_cors(self):
        r = self.client.options("/api/ping", headers={
            "Host": "localhost:3333", "Origin": "http://evil.com"})
        self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))


if __name__ == "__main__":
    unittest.main()
