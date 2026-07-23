"""Tests for cli/tools/jira.py — stubbed client/ledger/svc, no network."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli.tools import jira as tool
from services.jira_client import JiraError

_DTO = {
    "key": "PROJ-1", "id": "10001", "summary": "Fix flux capacitor",
    "status": "In Progress", "status_category": "indeterminate",
    "issue_type": "Bug", "priority": "High",
    "assignee": "Alice", "assignee_id": "acc-1", "reporter": "Bob",
    "project": "PROJ", "created": "2026-07-01", "updated": "2026-07-02",
    "labels": ["infra"],
}


class _StubClient:
    """In-memory stand-in for JiraClient."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.meta_required: list[dict] = []
        self.fail_with: JiraError | None = None

    def _record(self, name, *a, **kw):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append((name, a, kw))

    def server_info(self):
        self._record("server_info")
        return {"version": "9.4.0"}

    def myself(self):
        self._record("myself")
        return {"displayName": "Alice", "accountId": "acc-1",
                "emailAddress": "a@x.com"}

    def search(self, jql, **kw):
        self._record("search", jql, **kw)
        return {"issues": [_DTO], "next_cursor": "tok-2"}

    def get_issue(self, key, **kw):
        self._record("get_issue", key)
        full = dict(_DTO)
        full["description"] = "the description"
        full["comments"] = [
            {"id": "9", "author": "Carol", "created": "2026-07-03", "body": "hi"}
        ]
        return full

    def list_projects(self, **kw):
        self._record("list_projects", **kw)
        return [{"key": "PROJ", "name": "Project", "id": "1"}]

    def list_transitions(self, key):
        self._record("list_transitions", key)
        return [
            {"id": "21", "name": "Start", "to_status": "In Progress"},
            {"id": "31", "name": "Done", "to_status": "Done"},
        ]

    def get_create_metadata(self, project, issue_type):
        self._record("get_create_metadata", project, issue_type)
        return {
            "project": project, "issue_type": issue_type,
            "required_fields": list(self.meta_required),
            "optional_fields": [],
        }

    def search_users(self, query, **kw):
        self._record("search_users", query, **kw)
        return [{"id": "acc-1", "display_name": "Alice", "email": "", "active": True}]

    def create_issue(self, project, issue_type, summary, **kw):
        self._record("create_issue", project, issue_type, summary, **kw)
        return {"key": "PROJ-9"}

    def add_comment(self, key, body, **kw):
        self._record("add_comment", key, body, **kw)
        return {"id": "5", "author": "Alice", "created": "", "body": str(body)}

    def transition_issue(self, key, transition_id, **kw):
        self._record("transition_issue", key, transition_id, **kw)
        return {}

    def assign_issue(self, key, user_id):
        self._record("assign_issue", key, user_id)
        return {}


class _StubLedger:
    def __init__(self):
        self.entries: list[dict] = []

    def log_edit(self, **kw):
        self.entries.append(kw)


class _StubActivity:
    def __init__(self):
        self.events: list[tuple] = []

    def log(self, event_type, data):
        self.events.append((event_type, data))


class _StubSvc:
    def __init__(self, project_path: str):
        self.project_path = project_path
        self.edit_ledger = _StubLedger()
        self.activity_log = _StubActivity()


def _captured_finalize():
    captured = {}

    def finalize(tool_name, args, response, summary, **kw):
        captured.update(tool=tool_name, args=args, response=response,
                        summary=summary)
        return response

    return captured, finalize


_ENTRY = {"base_url": "https://x.atlassian.net", "username": "a@x.com",
          "deployment": "cloud", "default_project": "PROJ",
          "verify_tls": True, "ca_bundle": "", "name": "work"}


class TestJiraTool(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _StubSvc(self._tmp.name)
        self.client = _StubClient()
        self._patcher = mock.patch.object(
            tool, "_build_client", return_value=(self.client, dict(_ENTRY), "")
        )
        self._patcher.start()
        self.captured, self.finalize = _captured_finalize()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _run(self, action, **kw):
        return tool.handle_jira(action, self.svc, self.finalize, **kw)

    # ── plumbing ──────────────────────────────────────────

    def test_status_works_without_client(self):
        self._patcher.stop()
        try:
            with mock.patch.object(
                tool, "_build_client",
                return_value=(None, {}, "[jira:no-account] nope"),
            ):
                resp = self._run("status")
        finally:
            self._patcher.start()
        self.assertIn("no-account", resp)
        self.assertIn("account(s) configured", resp)

    def test_unknown_action_lists_valid(self):
        resp = self._run("frobnicate")
        self.assertIn("unknown-action", resp)
        self.assertIn("create_issue", resp)

    def test_api_error_is_translated(self):
        self.client.fail_with = JiraError("HTTP 403 nope", status=403)
        resp = self._run("whoami")
        self.assertIn("[jira:api-error]", resp)
        self.assertEqual(self.captured["summary"], "http-403")

    # ── reads ─────────────────────────────────────────────

    def test_whoami(self):
        resp = self._run("whoami")
        self.assertIn("Alice", resp)
        self.assertIn("acc-1", resp)

    def test_search_requires_jql(self):
        resp = self._run("search")
        self.assertIn("jql is required", resp)

    def test_search_formats_results_and_cursor(self):
        resp = self._run("search", jql="project = PROJ")
        self.assertIn("PROJ-1", resp)
        self.assertIn("cursor=tok-2", resp)

    def test_my_issues_builds_quoted_jql(self):
        self._run("my_issues", status_category='To Do" OR project = SECRET')
        jql = self.client.calls[0][1][0]
        self.assertIn("assignee = currentUser()", jql)
        self.assertIn('project = "PROJ"', jql)  # from account default_project
        self.assertIn('statusCategory = "To Do\\" OR project = SECRET"', jql)
        self.assertIn("ORDER BY updated DESC", jql)

    def test_get_issue_requires_key(self):
        resp = self._run("get_issue")
        self.assertIn("issue key is required", resp)

    def test_get_issue_renders_full(self):
        resp = self._run("get_issue", issue="PROJ-1")
        self.assertIn("the description", resp)
        self.assertIn("comment [Carol]", resp)

    # ── mutations + ledger ────────────────────────────────

    def test_create_issue_logs_to_ledger(self):
        resp = self._run("create_issue", issue_type="Bug", summary="Boom")
        self.assertIn("PROJ-9", resp)
        self.assertEqual(len(self.svc.edit_ledger.entries), 1)
        entry = self.svc.edit_ledger.entries[0]
        self.assertEqual(entry["change_type"], "create_issue")
        self.assertEqual(entry["detail"]["kind"], "jira")
        self.assertEqual(self.svc.activity_log.events[0][0], "jira_action")

    def test_create_issue_reports_missing_required_fields(self):
        self.client.meta_required = [
            {"id": "customfield_10010", "name": "Severity", "type": "option"},
        ]
        resp = self._run("create_issue", issue_type="Bug", summary="Boom")
        self.assertIn("[jira:missing-fields]", resp)
        self.assertIn("customfield_10010", resp)
        self.assertFalse(
            [c for c in self.client.calls if c[0] == "create_issue"]
        )

    def test_create_issue_rejects_bad_fields_json(self):
        resp = self._run("create_issue", issue_type="Bug", summary="x",
                         fields="{not json")
        self.assertIn("fields must be a JSON object", resp)

    def test_comment_requires_body_and_logs(self):
        resp = self._run("comment", issue="PROJ-1")
        self.assertIn("body is required", resp)
        resp = self._run("comment", issue="PROJ-1", body="hello")
        self.assertIn("[jira:commented]", resp)
        self.assertEqual(len(self.svc.edit_ledger.entries), 1)

    def test_ledger_never_contains_comment_body(self):
        self._run("comment", issue="PROJ-1", body="SECRET-BODY-TEXT")
        entry = self.svc.edit_ledger.entries[0]
        self.assertNotIn("SECRET-BODY-TEXT", str(entry["detail"]))

    def test_transition_by_id(self):
        self._run("transition", issue="PROJ-1", transition="31")
        call = [c for c in self.client.calls if c[0] == "transition_issue"][0]
        self.assertEqual(call[1], ("PROJ-1", "31"))

    def test_transition_by_name_resolves_id(self):
        resp = self._run("transition", issue="PROJ-1", transition="done")
        self.assertIn("[jira:transitioned]", resp)
        call = [c for c in self.client.calls if c[0] == "transition_issue"][0]
        self.assertEqual(call[1], ("PROJ-1", "31"))

    def test_transition_unknown_name_lists_available(self):
        resp = self._run("transition", issue="PROJ-1", transition="Reopen")
        self.assertIn("no transition named", resp)
        self.assertIn("Done (31)", resp)

    def test_assign_requires_user(self):
        resp = self._run("assign", issue="PROJ-1")
        self.assertIn("user is required", resp)

    def test_assign_passes_native_id(self):
        self._run("assign", issue="PROJ-1", user="acc-42")
        call = [c for c in self.client.calls if c[0] == "assign_issue"][0]
        self.assertEqual(call[1], ("PROJ-1", "acc-42"))

    def test_read_actions_do_not_touch_ledger(self):
        self._run("search", jql="project = PROJ")
        self._run("get_issue", issue="PROJ-1")
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_cap_clamps_single_overlong_line(self):
        resp = tool._cap("x" * 100_000)
        self.assertLess(len(resp), 100_000)
        self.assertIn("[truncated]", resp)


if __name__ == "__main__":
    unittest.main()
