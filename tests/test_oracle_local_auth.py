"""Kill-chain regression tests for the Oracle local write gate.

The vulnerability being pinned: ``POST /api/apikey/rotate`` (and generate /
clear), ``/api/chat``, ``/api/suggestions/approve`` and ``/api/config`` were
unauthenticated. Any local process could rotate the Discovery token and read
the fresh value from the response, defeating the Bearer gates on
``/api/config`` and ``/api/discovery/*``.

Now every mutating ``/api/*`` call (except ``/api/discovery/*``, which stays
Bearer-only) requires the per-boot dashboard session cookie — issued on
``GET /`` to loopback clients only — or the Bearer token.

Uses Flask's test client with in-memory config; the Bearer key comes from the
``C3_ORACLE_API_KEY`` env override so no keyring is touched, and ``api_auth``
is swapped for a fake wherever a handler would otherwise write to the keyring.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Deterministic key via env override — read by api_auth at verify time.
os.environ["C3_ORACLE_API_KEY"] = "test-secret-key"

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import local_session  # noqa: E402
from oracle.services.tool_registry import TIER_ACTION, ToolRegistry  # noqa: E402

MUTATING_LOCAL_ENDPOINTS = [
    "/api/apikey/generate",
    "/api/apikey/rotate",
    "/api/apikey/clear",
    "/api/chat",
    "/api/suggestions/approve",
    "/api/config",
]


class _StubExecutor:
    def execute(self, name, args=None):
        return {"dispatched": name, "args": args or {}}


class _FakeAuth:
    """In-memory stand-in for api_auth so no test ever writes the keyring."""

    def __init__(self, key=None):
        self.key = key

    def peek(self):
        return self.key

    def get_or_create_key(self):
        if not self.key:
            self.key = "generated-key-abcdef0123456789"
        return self.key

    def rotate(self):
        self.key = "rotated-key-9876543210fedcba"
        return self.key

    def clear(self):
        had = self.key is not None
        self.key = None
        return had


class _GateTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Re-assert the env override (another oracle test module pops it in its
        # tearDownClass and class run order is not guaranteed).
        os.environ["C3_ORACLE_API_KEY"] = "test-secret-key"
        srv._cfg = {
            "api_enabled": True,
            "api_require_auth": True,
            "api_max_tier": "action",
            "bind_host": "127.0.0.1",
            "mcp_port": 3332,
            "mcp_enabled": True,
        }
        srv._tool_registry = ToolRegistry(_StubExecutor(), max_tier=TIER_ACTION)
        srv._bridge = None
        srv._chat_engine = None
        srv._writer = None
        srv.app.config["TESTING"] = True
        cls.auth = {"Authorization": "Bearer test-secret-key"}

    def setUp(self):
        os.environ["C3_ORACLE_API_KEY"] = "test-secret-key"
        self.client = srv.app.test_client()  # fresh cookie jar per test
        self._saved = {}
        for patcher in (
            mock.patch.object(srv, "load_config", lambda: dict(srv._cfg)),
            mock.patch.object(srv, "save_config", self._saved.update),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _login(self):
        """Prime the dashboard session cookie via GET / (loopback client)."""
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)


# ── Cookie issuance ───────────────────────────────────────────────────


class TestSessionCookieIssuance(_GateTestBase):
    def test_root_sets_httponly_strict_cookie_for_loopback(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        set_cookie = r.headers.getlist("Set-Cookie")
        ours = [c for c in set_cookie if c.startswith(local_session.COOKIE_NAME + "=")]
        self.assertEqual(len(ours), 1)
        self.assertIn("HttpOnly", ours[0])
        self.assertIn("SameSite=Strict", ours[0])

    def test_root_no_cookie_for_non_loopback(self):
        r = self.client.get("/", environ_overrides={"REMOTE_ADDR": "192.168.1.9"})
        self.assertEqual(r.status_code, 200)
        set_cookie = r.headers.getlist("Set-Cookie")
        ours = [c for c in set_cookie if c.startswith(local_session.COOKIE_NAME + "=")]
        self.assertEqual(ours, [])


# ── The write gate: default-deny without credentials ─────────────────


class TestWriteGateDeniesUnauthenticated(_GateTestBase):
    def test_all_mutating_endpoints_401_without_credentials(self):
        for path in MUTATING_LOCAL_ENDPOINTS:
            with self.subTest(path=path):
                r = self.client.post(path, json={})
                self.assertEqual(r.status_code, 401)

    def test_rotate_kill_chain_key_untouched(self):
        # The original vuln: rotate unauthenticated, read the fresh token.
        fake = _FakeAuth(key="original-key-000111222333444")
        with mock.patch.object(srv, "api_auth", fake):
            r = self.client.post("/api/apikey/rotate")
            self.assertEqual(r.status_code, 401)
            self.assertEqual(fake.key, "original-key-000111222333444")
            self.assertNotIn(b"rotated", r.data)

    def test_options_is_exempt(self):
        r = self.client.options("/api/config")
        self.assertNotEqual(r.status_code, 401)

    def test_gets_stay_open(self):
        self.assertEqual(self.client.get("/api/suggestions").status_code, 200)
        self.assertEqual(self.client.get("/api/apikey").status_code, 200)


# ── The write gate: session cookie or Bearer passes ──────────────────


class TestWriteGateAcceptsCredentials(_GateTestBase):
    def test_config_post_with_session_cookie(self):
        self._login()
        r = self.client.post("/api/config", json={"model": "m1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._saved.get("model"), "m1")

    def test_config_post_with_bearer(self):
        r = self.client.post("/api/config", json={"model": "m2"}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._saved.get("model"), "m2")

    def test_rotate_with_session_cookie_rotates_and_reveals(self):
        fake = _FakeAuth(key="original-key-000111222333444")
        with mock.patch.object(srv, "api_auth", fake):
            self._login()
            r = self.client.post("/api/apikey/rotate")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()["key"], "rotated-key-9876543210fedcba")

    def test_gate_passes_through_to_handler_validation(self):
        # Past the gate, handlers still enforce their own validation: an empty
        # approve body is a 400 (not 401), an uninitialized chat engine a 500.
        self._login()
        self.assertEqual(
            self.client.post("/api/suggestions/approve", json={}).status_code, 400
        )
        self.assertEqual(self.client.post("/api/chat", json={}).status_code, 500)


# ── Discovery stays Bearer-only ───────────────────────────────────────


class TestDiscoveryUnaffectedByCookie(_GateTestBase):
    def test_cookie_does_not_satisfy_discovery(self):
        self._login()
        r = self.client.get("/api/discovery/tools")
        self.assertEqual(r.status_code, 401)

    def test_bearer_still_satisfies_discovery(self):
        r = self.client.get("/api/discovery/tools", headers=self.auth)
        self.assertEqual(r.status_code, 200)


# ── Reveal semantics on GET /api/apikey ───────────────────────────────


class TestApikeyRevealWithSession(_GateTestBase):
    def test_session_cookie_reveals_raw_key(self):
        fake = _FakeAuth(key="original-key-000111222333444")
        with mock.patch.object(srv, "api_auth", fake):
            self._login()
            body = self.client.get("/api/apikey").get_json()
            self.assertEqual(body["key"], "original-key-000111222333444")

    def test_anonymous_get_masks_key(self):
        fake = _FakeAuth(key="original-key-000111222333444")
        with mock.patch.object(srv, "api_auth", fake):
            body = self.client.get("/api/apikey").get_json()
            self.assertEqual(body["key"], "")
            self.assertTrue(body["masked"])


if __name__ == "__main__":
    unittest.main()
