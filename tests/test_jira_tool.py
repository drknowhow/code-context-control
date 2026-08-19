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
        self.issue_extra: dict = {}

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
        full.update(self.issue_extra)
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

    def update_issue(self, key, **kw):
        self._record("update_issue", key, **kw)
        return {}

    def add_comment(self, key, body, **kw):
        self._record("add_comment", key, body, **kw)
        return {"id": "5", "author": "Alice", "created": "", "body": str(body)}

    def transition_issue(self, key, transition_id, **kw):
        self._record("transition_issue", key, transition_id, **kw)
        return {}

    def assign_issue(self, key, user_id):
        self._record("assign_issue", key, user_id)
        return {}

    def list_link_types(self):
        self._record("list_link_types")
        return [
            {"id": "1", "name": "Blocks", "inward": "is blocked by",
             "outward": "blocks"},
            {"id": "2", "name": "Relates", "inward": "relates to",
             "outward": "relates to"},
        ]

    def link_issues(self, link_type, inward_key, outward_key):
        self._record("link_issues", link_type, inward_key, outward_key)
        return {}

    def set_parent(self, key, parent):
        self._record("set_parent", key, parent)
        return {}

    def unlink_issues(self, link_id):
        self._record("unlink_issues", link_id)
        return {}

    def delete_issue(self, key, **kw):
        self._record("delete_issue", key, **kw)
        return {}

    def list_boards(self, **kw):
        self._record("list_boards", **kw)
        return [{"id": 7, "name": "PROJ board", "type": "scrum",
                 "project": "PROJ"}]

    def list_sprints(self, board_id, **kw):
        self._record("list_sprints", board_id, **kw)
        return [{"id": 42, "name": "Sprint 9", "state": "active",
                 "start": "2026-08-10T00:00:00.000Z",
                 "end": "2026-08-24T00:00:00.000Z", "goal": "Ship it"}]

    def move_to_sprint(self, sprint_id, issue_keys):
        self._record("move_to_sprint", sprint_id, issue_keys)
        return {}

    def move_to_backlog(self, issue_keys):
        self._record("move_to_backlog", issue_keys)
        return {}

    def add_worklog(self, key, time_spent, **kw):
        self._record("add_worklog", key, time_spent, **kw)
        return {"id": "301", "time_spent": time_spent, "author": "Alice"}

    def list_worklogs(self, key):
        self._record("list_worklogs", key)
        return [{"id": "301", "author": "Alice", "time_spent": "2h",
                 "started": "2026-08-18T09:00:00.000Z", "comment": "pairing"}]

    def attach_file(self, key, filename, content):
        self._record("attach_file", key, filename, len(content))
        return [{"id": "501", "filename": filename, "size": len(content),
                 "author": "Alice", "created": ""}]


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

    def test_create_metadata_response_carries_screen_caveat(self):
        resp = self._run("get_create_metadata", issue_type="Bug")
        self.assertIn("[jira:create_metadata]", resp)
        self.assertIn("create screen may accept fewer", resp)
        self.assertIn("update_issue", resp)

    def test_create_issue_screen_rejection_hints_at_update_issue(self):
        self.client.fail_with = JiraError(
            "HTTP 400: customfield_10014: Field 'customfield_10014' cannot be "
            "set. It is not on the appropriate screen, or unknown.",
            status=400,
        )
        resp = self._run("create_issue", issue_type="Bug", summary="Boom")
        self.assertIn("[jira:api-error]", resp)
        self.assertIn("update_issue", resp)
        self.assertIn("not the create screen", resp)

    def test_update_issue_requires_key(self):
        resp = self._run("update_issue", summary="New")
        self.assertIn("issue key is required", resp)

    def test_update_issue_requires_a_change(self):
        resp = self._run("update_issue", issue="PROJ-1")
        self.assertIn("at least one of summary", resp)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_update_issue_rejects_bad_fields_json(self):
        resp = self._run("update_issue", issue="PROJ-1", fields="{not json")
        self.assertIn("fields must be a JSON object", resp)

    def test_update_issue_passes_changes_and_logs(self):
        resp = self._run(
            "update_issue", issue="PROJ-1", summary="New",
            fields='{"customfield_10014": "PROJ-42"}',
        )
        self.assertIn("[jira:updated] PROJ-1", resp)
        self.assertIn("customfield_10014", resp)
        call = [c for c in self.client.calls if c[0] == "update_issue"][0]
        self.assertEqual(call[1], ("PROJ-1",))
        self.assertEqual(call[2]["summary"], "New")
        self.assertEqual(call[2]["fields"], {"customfield_10014": "PROJ-42"})
        entry = self.svc.edit_ledger.entries[0]
        self.assertEqual(entry["change_type"], "update_issue")

    def test_update_issue_ledger_never_contains_field_values(self):
        self._run("update_issue", issue="PROJ-1",
                  fields='{"customfield_10014": "SECRET-EPIC-KEY"}')
        entry = self.svc.edit_ledger.entries[0]
        self.assertNotIn("SECRET-EPIC-KEY", str(entry["detail"]))

    def test_update_issue_screen_rejection_hints_at_admin(self):
        self.client.fail_with = JiraError(
            "HTTP 400: Field 'customfield_10014' cannot be set. It is not on "
            "the appropriate screen, or unknown.",
            status=400,
        )
        resp = self._run("update_issue", issue="PROJ-1",
                         fields='{"customfield_10014": "PROJ-42"}')
        self.assertIn("[jira:api-error]", resp)
        self.assertIn("not on the edit screen either", resp)

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

    def test_get_issue_renders_parent_and_links(self):
        self.client.issue_extra = {
            "parent": "PROJ-42",
            "links": [{"id": "77", "description": "blocks", "issue": "PROJ-9",
                       "status": "Open"}],
        }
        resp = self._run("get_issue", issue="PROJ-1")
        self.assertIn("parent: PROJ-42", resp)
        self.assertIn("link: blocks PROJ-9 [Open] (id=77)", resp)

    def test_create_issue_passes_parent(self):
        self._run("create_issue", project="PROJ", issue_type="Task",
                  summary="Child", parent="PROJ-42")
        call = [c for c in self.client.calls if c[0] == "create_issue"][0]
        self.assertEqual(call[2]["parent"], "PROJ-42")

    def test_update_issue_parent_only_calls_set_parent(self):
        resp = self._run("update_issue", issue="PROJ-1", parent="PROJ-42")
        self.assertIn("[jira:updated] PROJ-1 — parent", resp)
        kinds = [c[0] for c in self.client.calls]
        self.assertEqual(kinds, ["set_parent"])  # no field PUT for parent-only
        call = self.client.calls[0]
        self.assertEqual(call[1], ("PROJ-1", "PROJ-42"))
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "update_issue"
        )

    def test_update_issue_parent_none_reports_cleared(self):
        resp = self._run("update_issue", issue="PROJ-1", parent="none")
        self.assertIn("parent (cleared)", resp)
        self.assertEqual(self.client.calls[0][1], ("PROJ-1", "none"))

    def test_list_link_types_renders_catalog(self):
        resp = self._run("list_link_types")
        self.assertIn("Blocks", resp)
        self.assertIn("is blocked by", resp)
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_link_issues_requires_type_and_target(self):
        resp = self._run("link_issues", issue="PROJ-1")
        self.assertIn("link_type and target are required", resp)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_link_issues_outward_phrase_and_logs(self):
        resp = self._run("link_issues", issue="PROJ-1", link_type="blocks",
                         target="PROJ-2")
        self.assertIn("[jira:linked] PROJ-1 blocks PROJ-2", resp)
        call = [c for c in self.client.calls if c[0] == "link_issues"][0]
        self.assertEqual(call[1], ("Blocks", "PROJ-1", "PROJ-2"))
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "link_issues"
        )

    def test_link_issues_inward_phrase_flips_pair(self):
        resp = self._run("link_issues", issue="PROJ-2",
                         link_type="is blocked by", target="PROJ-1")
        self.assertIn("[jira:linked] PROJ-1 blocks PROJ-2", resp)
        call = [c for c in self.client.calls if c[0] == "link_issues"][0]
        self.assertEqual(call[1], ("Blocks", "PROJ-1", "PROJ-2"))

    def test_link_issues_unknown_type_lists_catalog(self):
        resp = self._run("link_issues", issue="PROJ-1", link_type="mystery",
                         target="PROJ-2")
        self.assertIn("unknown link type", resp)
        self.assertIn("Blocks", resp)
        self.assertNotIn("link_issues",
                         [c[0] for c in self.client.calls])
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_unlink_issues_requires_link_id_then_logs(self):
        resp = self._run("unlink_issues")
        self.assertIn("link_id is required", resp)
        self.assertEqual(self.svc.edit_ledger.entries, [])
        resp = self._run("unlink_issues", link_id="77")
        self.assertIn("[jira:unlinked] link 77", resp)
        self.assertEqual(self.client.calls[0][1], ("77",))
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "unlink_issues"
        )

    def test_delete_issue_requires_key_and_says_permanent(self):
        resp = self._run("delete_issue")
        self.assertIn("issue key is required", resp)
        resp = self._run("delete_issue", issue="PROJ-1", delete_subtasks=True)
        self.assertIn("[jira:deleted] PROJ-1 (with subtasks)", resp)
        self.assertIn("permanent", resp)
        call = self.client.calls[0]
        self.assertEqual(call[1], ("PROJ-1",))
        self.assertTrue(call[2]["delete_subtasks"])
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "delete_issue"
        )

    def test_list_boards_uses_default_project(self):
        resp = self._run("list_boards")
        self.assertIn("7: PROJ board (scrum) PROJ", resp)
        self.assertEqual(self.client.calls[0][2]["project"], "PROJ")
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_list_sprints_requires_board_id(self):
        resp = self._run("list_sprints")
        self.assertIn("board_id is required", resp)
        resp = self._run("list_sprints", board_id=7, sprint_state="active")
        self.assertIn("42: Sprint 9 [active]", resp)
        self.assertIn("Ship it", resp)
        self.assertEqual(self.client.calls[0][2]["state"], "active")

    def test_move_to_sprint_requires_sprint_id_and_splits_keys(self):
        resp = self._run("move_to_sprint", issue="PROJ-1")
        self.assertIn("sprint_id is required", resp)
        resp = self._run("move_to_sprint", issue="PROJ-1, PROJ-2", sprint_id=42)
        self.assertIn("[jira:moved] PROJ-1, PROJ-2 -> sprint 42", resp)
        call = [c for c in self.client.calls if c[0] == "move_to_sprint"][0]
        self.assertEqual(call[1], (42, ["PROJ-1", "PROJ-2"]))
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "move_to_sprint"
        )

    def test_move_to_backlog_logs(self):
        resp = self._run("move_to_backlog", issue="PROJ-1")
        self.assertIn("[jira:moved] PROJ-1 -> backlog", resp)
        self.assertEqual(self.client.calls[0][1], (["PROJ-1"],))
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "move_to_backlog"
        )

    def test_add_worklog_requires_time_spent_then_logs(self):
        resp = self._run("add_worklog", issue="PROJ-1")
        self.assertIn("time_spent is required", resp)
        resp = self._run("add_worklog", issue="PROJ-1", time_spent="2h 30m",
                         body="pairing")
        self.assertIn("[jira:worklog-added] PROJ-1 2h 30m", resp)
        call = self.client.calls[0]
        self.assertEqual(call[1], ("PROJ-1", "2h 30m"))
        self.assertEqual(call[2]["comment"], "pairing")
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "add_worklog"
        )

    def test_list_worklogs_renders_and_stays_out_of_ledger(self):
        resp = self._run("list_worklogs", issue="PROJ-1")
        self.assertIn("301: 2h by Alice", resp)
        self.assertIn("pairing", resp)
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_attach_file_validates_path_then_uploads(self):
        resp = self._run("attach_file", issue="PROJ-1")
        self.assertIn("file_path is required", resp)
        resp = self._run("attach_file", issue="PROJ-1",
                         file_path=str(Path(self._tmp.name) / "missing.log"))
        self.assertIn("file not found", resp)
        self.assertEqual(self.svc.edit_ledger.entries, [])
        real = Path(self._tmp.name) / "build.log"
        real.write_bytes(b"boom trace")
        resp = self._run("attach_file", issue="PROJ-1", file_path=str(real))
        self.assertIn("[jira:attached] PROJ-1 <- build.log (10 bytes)", resp)
        call = [c for c in self.client.calls if c[0] == "attach_file"][0]
        self.assertEqual(call[1], ("PROJ-1", "build.log", 10))
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "attach_file"
        )

    def test_attach_file_rejects_oversize(self):
        big = Path(self._tmp.name) / "big.bin"
        big.write_bytes(b"x")
        with mock.patch.object(tool, "_ATTACH_MAX_BYTES", 0):
            resp = self._run("attach_file", issue="PROJ-1", file_path=str(big))
        self.assertIn("caps at", resp)
        self.assertEqual(self.client.calls, [])

    def test_get_issue_renders_attachments(self):
        self.client.issue_extra = {
            "attachments": [{"id": "501", "filename": "build.log",
                             "size": 10, "author": "Alice", "created": ""}],
        }
        resp = self._run("get_issue", issue="PROJ-1")
        self.assertIn("attachment: build.log (10 bytes, Alice) id=501", resp)

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
