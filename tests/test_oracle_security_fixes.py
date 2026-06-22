"""Security-fix tests for the Oracle server (PyPI release hardening).

Covers:
  #1  POST /api/config requires the Bearer token + rejects unknown keys.
  #2  GET /api/apikey masks the raw token unless a valid Bearer token is given.
  #3  project_path tool args are validated against discovered projects.

Uses Flask's test client with a stub registry/executor and an in-memory config
so no Ollama/keyring/real config file is touched. The API key is supplied via
the ``C3_ORACLE_API_KEY`` env override (read by api_auth at verify time).
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
from oracle.services.tool_registry import TIER_ACTION, ToolRegistry  # noqa: E402


class _StubExecutor:
    def execute(self, name, args=None):
        return {"dispatched": name, "args": args or {}}


# ── #1 + #2: config / apikey HTTP gates ──────────────────────────────


class TestConfigAuthGate(unittest.TestCase):
    """POST /api/config must require the Bearer token and allowlist keys."""

    @classmethod
    def setUpClass(cls):
        # Re-assert the env override here (not just at module import): another
        # oracle test module pops it in its tearDownClass, and class run order is
        # not guaranteed, so set it idempotently per class.
        os.environ["C3_ORACLE_API_KEY"] = "test-secret-key"
        srv._cfg = {
            "api_enabled": True,
            "api_require_auth": True,
            "api_max_tier": "action",
            "bind_host": "127.0.0.1",
            "mcp_port": 3332,
            "mcp_enabled": True,
            "ollama_base_url": "https://ollama.com",
            "model": "gemma4:31b-cloud",
        }
        srv._tool_registry = ToolRegistry(_StubExecutor(), max_tier=TIER_ACTION)
        srv._bridge = None  # skip bridge re-verify branch
        srv.app.config["TESTING"] = True
        cls.client = srv.app.test_client()
        cls.auth = {"Authorization": "Bearer test-secret-key"}

    def setUp(self):
        # Never write the real config; serve a copy of the in-memory cfg.
        self._saved = {}
        self._lc = mock.patch.object(srv, "load_config", lambda: dict(srv._cfg))
        self._sc = mock.patch.object(srv, "save_config", self._saved.update)
        self._lc.start()
        self._sc.start()
        self.addCleanup(self._lc.stop)
        self.addCleanup(self._sc.stop)

    def test_post_config_unauthenticated_rejected(self):
        # The original vuln: strip Discovery auth with no credentials.
        r = self.client.post("/api/config", json={"api_require_auth": False})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(self._saved)  # nothing persisted

    def test_post_config_bad_token_rejected(self):
        r = self.client.post("/api/config", json={"model": "x"},
                             headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    def test_post_config_unknown_key_rejected(self):
        r = self.client.post("/api/config", json={"evil_key": 1, "model": "ok"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 400)
        self.assertIn("evil_key", r.get_json().get("keys", []))
        self.assertFalse(self._saved)  # rejected wholesale, nothing persisted

    def test_post_config_known_key_accepted(self):
        r = self.client.post("/api/config", json={"model": "new-model"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["saved"])
        self.assertEqual(self._saved.get("model"), "new-model")


class TestApikeyMasking(unittest.TestCase):
    """GET /api/apikey must not leak the raw token without a Bearer token."""

    @classmethod
    def setUpClass(cls):
        os.environ["C3_ORACLE_API_KEY"] = "test-secret-key"  # see note above
        srv._cfg = {
            "api_enabled": True, "api_require_auth": True,
            "bind_host": "127.0.0.1", "mcp_port": 3332, "mcp_enabled": True,
        }
        srv.app.config["TESTING"] = True
        cls.client = srv.app.test_client()
        cls.auth = {"Authorization": "Bearer test-secret-key"}

    def test_get_apikey_unauthenticated_masks_key(self):
        body = self.client.get("/api/apikey").get_json()
        self.assertTrue(body["exists"])
        self.assertEqual(body["key"], "")  # raw key never returned
        self.assertTrue(body["masked"])    # masked form still available for status
        snippet_auth = body["snippet"]["mcpServers"]["c3-oracle"]["headers"]["Authorization"]
        self.assertEqual(snippet_auth, "Bearer <token>")  # placeholder, not raw

    def test_get_apikey_authenticated_reveals_key(self):
        body = self.client.get("/api/apikey", headers=self.auth).get_json()
        self.assertEqual(body["key"], "test-secret-key")
        snippet_auth = body["snippet"]["mcpServers"]["c3-oracle"]["headers"]["Authorization"]
        self.assertEqual(snippet_auth, "Bearer test-secret-key")


# ── #3: project_path membership validation ───────────────────────────


class _StubScanner:
    def __init__(self, projects):
        self._projects = projects

    def discover(self):
        return list(self._projects)


class TestProjectPathValidation(unittest.TestCase):
    def test_validate_rejects_unknown_path(self):
        from oracle.services.c3_bridge import validate_project_path
        scanner = _StubScanner([{"path": str(REPO_ROOT)}])
        with self.assertRaises(ValueError):
            validate_project_path(scanner, str(REPO_ROOT.parent))

    def test_validate_accepts_known_path(self):
        from oracle.services.c3_bridge import validate_project_path
        scanner = _StubScanner([{"path": str(REPO_ROOT)}])
        resolved = validate_project_path(scanner, str(REPO_ROOT))
        self.assertEqual(resolved, str(REPO_ROOT.resolve()))

    def test_validate_rejects_empty_path(self):
        from oracle.services.c3_bridge import validate_project_path
        with self.assertRaises(ValueError):
            validate_project_path(_StubScanner([]), "")


if __name__ == "__main__":
    unittest.main()
