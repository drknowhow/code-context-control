"""Smoke tests for the Oracle Discovery REST API: auth, dispatch, OpenAPI.

Uses Flask's test client with a stub executor so no Ollama/keyring is needed.
The API key is supplied via the ``C3_ORACLE_API_KEY`` env override.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Deterministic key via env override — read by api_auth at verify time.
os.environ["C3_ORACLE_API_KEY"] = "test-secret-key"

import oracle.oracle_server as srv  # noqa: E402
from oracle.services.tool_registry import TIER_ACTION, ToolRegistry  # noqa: E402


class _StubExecutor:
    def execute(self, name, args=None):
        return {"dispatched": name, "args": args or {}}


class TestDiscoveryAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv._cfg = {
            "api_enabled": True,
            "api_require_auth": True,
            "api_max_tier": "action",
            "bind_host": "127.0.0.1",
            "mcp_port": 3332,
            "mcp_enabled": True,
        }
        srv._tool_registry = ToolRegistry(_StubExecutor(), max_tier=TIER_ACTION)
        srv.app.config["TESTING"] = True
        cls.client = srv.app.test_client()
        cls.auth = {"Authorization": "Bearer test-secret-key"}

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("C3_ORACLE_API_KEY", None)

    def test_tools_requires_auth(self):
        self.assertEqual(self.client.get("/api/discovery/tools").status_code, 401)

    def test_bad_token_rejected(self):
        r = self.client.get("/api/discovery/tools", headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    def test_tools_with_auth(self):
        r = self.client.get("/api/discovery/tools", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        names = {t["name"] for t in r.get_json()["tools"]}
        self.assertIn("list_projects", names)
        self.assertIn("c3_search_cross", names)

    def test_no_edit_tool_exposed(self):
        r = self.client.get("/api/discovery/tools", headers=self.auth)
        names = {t["name"] for t in r.get_json()["tools"]}
        self.assertNotIn("c3_edit", names)
        self.assertNotIn("c3_shell", names)

    def test_activity_report_listed(self):
        r = self.client.get("/api/discovery/tools", headers=self.auth)
        names = {t["name"] for t in r.get_json()["tools"]}
        self.assertIn("activity_report", names)

    def test_activity_report_dispatches(self):
        r = self.client.post("/api/discovery/call",
                             json={"tool": "activity_report", "args": {}}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["dispatched"], "activity_report")

    def test_call_dispatches(self):
        r = self.client.post("/api/discovery/call",
                             json={"tool": "list_projects", "args": {}}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["dispatched"], "list_projects")

    def test_call_missing_tool(self):
        r = self.client.post("/api/discovery/call", json={"args": {}}, headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_call_named(self):
        r = self.client.post("/api/discovery/tools/c3_search_cross",
                             json={"query": "x"}, headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["dispatched"], "c3_search_cross")

    def test_openapi(self):
        r = self.client.get("/api/discovery/openapi.json", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        spec = r.get_json()
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("/api/discovery/tools/list_projects", spec["paths"])

    def test_mcp_info(self):
        r = self.client.get("/api/discovery/mcp-info", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        info = r.get_json()
        self.assertEqual(info["auth"], "bearer")
        self.assertTrue(info["url"].endswith("/mcp"))


if __name__ == "__main__":
    unittest.main()
