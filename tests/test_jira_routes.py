"""Tests for the /api/jira/* Flask routes in cli/server.py.

Uses the Flask test client with PROJECT_PATH pointed at a temp dir, a
patched Path.home (hermetic from the real ~/.c3), a stubbed keyring token,
and a stub JiraClient — fully offline.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.server as srv
from services import jira_client as jc_mod
from services import jira_credentials as jr_creds
from services.jira_client import JiraError

_DTO_TODO = {"key": "PROJ-1", "summary": "One", "status": "To Do",
             "status_category": "new", "assignee": "Alice"}
_DTO_DOING = {"key": "PROJ-2", "summary": "Two", "status": "In Progress",
              "status_category": "indeterminate", "assignee": "Alice"}


class _StubJiraClient:
    fail_with: JiraError | None = None

    def __init__(self, base_url, username, token, *, deployment,
                 verify_tls=True, ca_bundle="", timeout=30):
        self.base_url = base_url
        self.deployment = deployment

    def _maybe_fail(self):
        if _StubJiraClient.fail_with is not None:
            raise _StubJiraClient.fail_with

    def server_info(self):
        self._maybe_fail()
        return {"version": "9.4.0"}

    def myself(self):
        return {"displayName": "Alice"}

    def search(self, jql, **kw):
        self._maybe_fail()
        self.last_jql = jql
        _StubJiraClient.last_search_jql = jql
        return {"issues": [_DTO_TODO, _DTO_DOING], "next_cursor": ""}

    def get_issue(self, key, **kw):
        self._maybe_fail()
        return {**_DTO_TODO, "key": key, "description": "d", "comments": []}

    def list_transitions(self, key):
        return [{"id": "31", "name": "Done", "to_status": "Done"}]

    def create_issue(self, project, issue_type, summary, **kw):
        self._maybe_fail()
        return {"key": "PROJ-9"}

    def add_comment(self, key, body, **kw):
        self._maybe_fail()
        return {"id": "5", "author": "Alice", "created": "", "body": str(body)}

    def transition_issue(self, key, transition_id, **kw):
        self._maybe_fail()
        return {}

    def assign_issue(self, key, user_id):
        self._maybe_fail()
        return {}


class TestJiraRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        self._old_project_path = srv.PROJECT_PATH
        srv.PROJECT_PATH = self.proj
        self._home_patcher = mock.patch.object(
            Path, "home", return_value=Path(self._home.name)
        )
        self._home_patcher.start()
        self._token_patcher = mock.patch.object(
            jr_creds, "load_token", return_value="tok"
        )
        self._token_patcher.start()
        self._client_patcher = mock.patch.object(
            jc_mod, "JiraClient", _StubJiraClient
        )
        self._client_patcher.start()
        _StubJiraClient.fail_with = None
        self.client = srv.app.test_client()

    def tearDown(self):
        self._client_patcher.stop()
        self._token_patcher.stop()
        self._home_patcher.stop()
        srv.PROJECT_PATH = self._old_project_path
        self._tmp.cleanup()
        self._home.cleanup()

    def _write_account(self):
        c3 = self.proj / ".c3"
        c3.mkdir(parents=True, exist_ok=True)
        (c3 / "config.json").write_text(json.dumps({"jira": {
            "default_account": "work",
            "accounts": {"work": {
                "base_url": "https://x.atlassian.net", "username": "a@x.com",
                "deployment": "cloud", "default_project": "PROJ",
                "verify_tls": True, "ca_bundle": "",
            }},
        }}), encoding="utf-8")

    # ── unconfigured states ───────────────────────────────

    def test_status_without_account_reports_gracefully(self):
        resp = self.client.get("/api/jira/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["connection"]["ok"])
        self.assertIn("no default account", data["connection"]["error"])

    def test_board_without_account_is_400(self):
        resp = self.client.get("/api/jira/board")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("c3 jira login", resp.get_json()["error"])

    # ── configured states ─────────────────────────────────

    def test_status_probes_connection(self):
        self._write_account()
        data = self.client.get("/api/jira/status").get_json()
        self.assertTrue(data["connection"]["ok"])
        self.assertEqual(data["connection"]["version"], "9.4.0")
        self.assertEqual(data["connection"]["user"], "Alice")

    def test_board_groups_by_status_category(self):
        self._write_account()
        data = self.client.get("/api/jira/board").get_json()
        self.assertEqual([i["key"] for i in data["columns"]["new"]], ["PROJ-1"])
        self.assertEqual(
            [i["key"] for i in data["columns"]["indeterminate"]], ["PROJ-2"]
        )
        self.assertIn('project = "PROJ"', _StubJiraClient.last_search_jql)
        self.assertIn("assignee = currentUser()", _StubJiraClient.last_search_jql)

    def test_search_requires_jql(self):
        self._write_account()
        resp = self.client.get("/api/jira/search")
        self.assertEqual(resp.status_code, 400)

    def test_search_returns_issues(self):
        self._write_account()
        data = self.client.get("/api/jira/search?jql=project%20%3D%20PROJ").get_json()
        self.assertEqual(len(data["issues"]), 2)

    def test_get_issue_includes_transitions(self):
        self._write_account()
        data = self.client.get("/api/jira/issue/PROJ-7").get_json()
        self.assertEqual(data["key"], "PROJ-7")
        self.assertEqual(data["transitions"][0]["id"], "31")

    def test_create_issue_validates_input(self):
        self._write_account()
        resp = self.client.post("/api/jira/issue", json={"summary": "x"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/jira/issue", json={
            "issue_type": "Bug", "summary": "Boom",
        })  # project falls back to account default
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["key"], "PROJ-9")

    def test_transition_and_comment_and_assign(self):
        self._write_account()
        resp = self.client.post("/api/jira/issue/PROJ-1/transition",
                                json={"transition": "31"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post("/api/jira/issue/PROJ-1/comment", json={})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/jira/issue/PROJ-1/comment",
                                json={"body": "hi"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post("/api/jira/issue/PROJ-1/assign",
                                json={"user": "acc-1"})
        self.assertEqual(resp.status_code, 200)

    def test_jira_error_maps_to_http_status(self):
        self._write_account()
        _StubJiraClient.fail_with = JiraError("HTTP 404 nope", status=404)
        resp = self.client.get("/api/jira/issue/PROJ-404")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("nope", resp.get_json()["error"])

    def test_jira_bundle_entry_present(self):
        self.assertIn("ui/components/jira.js", srv._UI_JS_FILES)


if __name__ == "__main__":
    unittest.main()
