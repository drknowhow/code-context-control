"""Tests for the Oracle dashboard token-management endpoints (/api/apikey/*).

These are local-dashboard endpoints (NOT under /api/discovery, so not Bearer-gated).
``api_auth`` is replaced with an in-memory fake so no keyring/env is touched.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracle.oracle_server as srv  # noqa: E402


class _FakeAuth:
    def __init__(self):
        self.key = None

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


class TestApikeyAPI(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeAuth()
        self._patch = mock.patch.object(srv, "api_auth", self.fake)
        self._patch.start()
        srv._cfg = {
            "bind_host": "127.0.0.1", "mcp_port": 3332,
            "api_require_auth": True, "api_enabled": True, "mcp_enabled": True,
        }
        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()

    def tearDown(self):
        self._patch.stop()

    def test_get_no_token_is_ungated(self):
        # No Authorization header — dashboard endpoint must not be Bearer-gated.
        r = self.client.get("/api/apikey")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["exists"])
        self.assertEqual(body["key"], "")
        self.assertTrue(body["require_auth"])
        self.assertTrue(body["mcp_url"].endswith("/mcp"))
        self.assertTrue(body["openapi_url"].endswith("/api/discovery/openapi.json"))
        self.assertIn("c3-oracle", body["snippet"]["mcpServers"])

    def test_generate(self):
        r = self.client.post("/api/apikey/generate")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["exists"])
        self.assertEqual(body["key"], "generated-key-abcdef0123456789")
        self.assertTrue(body["masked"])
        self.assertEqual(
            body["snippet"]["mcpServers"]["c3-oracle"]["headers"]["Authorization"],
            "Bearer generated-key-abcdef0123456789",
        )

    def test_rotate_changes_key(self):
        self.client.post("/api/apikey/generate")
        r = self.client.post("/api/apikey/rotate")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["key"], "rotated-key-9876543210fedcba")

    def test_clear(self):
        self.client.post("/api/apikey/generate")
        r = self.client.post("/api/apikey/clear")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["exists"])
        self.assertEqual(body["key"], "")

    def test_snippet_placeholder_when_no_key(self):
        r = self.client.get("/api/apikey")
        auth = r.get_json()["snippet"]["mcpServers"]["c3-oracle"]["headers"]["Authorization"]
        self.assertEqual(auth, "Bearer <token>")


if __name__ == "__main__":
    unittest.main()
