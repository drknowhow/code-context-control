"""Tests for cli/tools/bitbucket.py — handle_bitbucket dispatch."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli.tools import bitbucket as tool


class _StubClient:
    """In-memory stand-in for BitbucketDataCenterClient."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name, *a, **kw):
        self.calls.append((name, a, kw))

    def application_properties(self):
        self._record("application_properties")
        return {"version": "8.5.0"}

    def whoami(self):
        self._record("whoami")
        return {"name": "alice", "displayName": "Alice", "emailAddress": "a@x", "active": True}

    def list_projects(self, *, name=""):
        self._record("list_projects", name=name)
        return [{"key": "PROJ", "name": "Project"}]

    def list_repos(self, project_key):
        self._record("list_repos", project_key)
        return [{"slug": "repo", "name": "Repo"}]

    def list_pull_requests(self, project, repo, *, state="OPEN", **kw):
        self._record("list_prs", project, repo, state=state, **kw)
        return [{"id": 1, "title": "PR1", "state": state, "fromRef": {"displayId": "f"},
                 "toRef": {"displayId": "main"}, "author": {"user": {"name": "a"}}}]

    def get_pull_request(self, project, repo, pr_id):
        self._record("get_pr", project, repo, pr_id)
        return {"id": pr_id, "version": 7, "state": "OPEN", "title": "T",
                "fromRef": {"displayId": "f"}, "toRef": {"displayId": "main"},
                "author": {"user": {"displayName": "Alice"}}}

    def update_pull_request(self, project, repo, pr_id, *, version, title,
                            description="", to_branch="", reviewers=None):
        self._record("update_pr", project, repo, pr_id, version=version,
                     title=title, description=description,
                     to_branch=to_branch, reviewers=reviewers)
        return {"id": pr_id, "version": version + 1, "state": "OPEN",
                "title": title, "description": description,
                "fromRef": {"displayId": "f"},
                "toRef": {"displayId": to_branch or "main"},
                "author": {"user": {"displayName": "Alice"}}}

    def set_pr_reviewer_status(self, project, repo, pr_id, *, user_slug, status):
        self._record("set_reviewer_status", project, repo, pr_id,
                     user_slug=user_slug, status=status)
        return {"status": status}

    def get_pr_commits(self, project, repo, pr_id):
        self._record("get_pr_commits", project, repo, pr_id)
        return [{"id": "abc1234567", "displayId": "abc1234",
                 "message": "fix: the thing",
                 "author": {"name": "alice"}}]

    def get_pr_comment(self, project, repo, pr_id, comment_id):
        self._record("get_pr_comment", project, repo, pr_id, comment_id)
        return {"id": comment_id, "version": 3, "text": "old text",
                "state": "OPEN", "severity": "BLOCKER"}

    def update_pr_comment(self, project, repo, pr_id, comment_id, *,
                          version, text):
        self._record("update_pr_comment", project, repo, pr_id, comment_id,
                     version=version, text=text)
        return {"id": comment_id, "version": version + 1, "text": text}

    def delete_pr_comment(self, project, repo, pr_id, comment_id, *, version):
        self._record("delete_pr_comment", project, repo, pr_id, comment_id,
                     version=version)
        return {}

    def list_pr_tasks(self, project, repo, pr_id, *, state=""):
        self._record("list_pr_tasks", project, repo, pr_id, state=state)
        return [{"id": 91, "state": "OPEN", "severity": "BLOCKER",
                 "text": "Add a regression test",
                 "author": {"displayName": "Alice"}}]

    def create_pr_task(self, project, repo, pr_id, *, text):
        self._record("create_pr_task", project, repo, pr_id, text=text)
        return {"id": 92, "severity": "BLOCKER", "state": "OPEN", "text": text}

    def set_pr_task_state(self, project, repo, pr_id, comment_id, *,
                          version, state):
        self._record("set_pr_task_state", project, repo, pr_id, comment_id,
                     version=version, state=state)
        return {"id": comment_id, "state": state}

    def merge_pr(self, project, repo, pr_id, *, version, message=""):
        self._record("merge_pr", project, repo, pr_id, version=version, message=message)
        return {"state": "MERGED", "toRef": {"displayId": "main"}}

    def decline_pr(self, project, repo, pr_id, *, version):
        self._record("decline_pr", project, repo, pr_id, version=version)
        return {"state": "DECLINED"}

    def list_branches(self, project, repo, *, filter_text=""):
        self._record("list_branches", project, repo, filter_text=filter_text)
        return [{"id": "refs/heads/main", "displayId": "main", "isDefault": True,
                 "latestCommit": "abc1234567"}]


class _StubLedger:
    def __init__(self):
        self.entries: list[dict] = []

    def log_edit(self, **kw):
        self.entries.append(kw)
        return {"id": "ledger-1"}


class _StubActivity:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def log(self, event_type, data):
        self.events.append((event_type, data))


class _StubSvc:
    def __init__(self, project_path: str):
        self.project_path = project_path
        self.edit_ledger = _StubLedger()
        self.activity_log = _StubActivity()


def _captured_finalize():
    seen: list[tuple] = []

    def finalize(name, args, resp, summary, **kw):
        seen.append((name, dict(args), resp, summary))
        return resp

    return finalize, seen


class TestBitbucketTool(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _StubSvc(self._tmp.name)
        self.client = _StubClient()
        # Make _build_client return our stub regardless of config.
        self._build_patcher = mock.patch.object(
            tool, "_build_client", return_value=(self.client, "")
        )
        self._build_patcher.start()
        # Provide minimal config (default_project + default_repo) so repo-scoped
        # actions don't need explicit args.
        from services import bitbucket_credentials as bb_creds
        bb_creds.set_default_repo("PROJ", "repo", project_path=self._tmp.name)
        bb_creds.set_active_account("https://bb", "alice", project_path=self._tmp.name)

    def tearDown(self):
        self._build_patcher.stop()
        self._tmp.cleanup()

    def test_status_works_without_client(self):
        with mock.patch.object(tool, "_build_client", return_value=(None, "no client")):
            finalize, seen = _captured_finalize()
            res = tool.handle_bitbucket("status", self.svc, finalize)
            self.assertIn("[bitbucket:status]", res)
            self.assertEqual(seen[-1][3], "status")

    def test_unknown_action_lists_valid_actions(self):
        finalize, seen = _captured_finalize()
        res = tool.handle_bitbucket("explode", self.svc, finalize)
        self.assertIn("unknown-action", res)
        self.assertIn("status", res)
        self.assertIn("merge_pr", res)

    def test_whoami_returns_user(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("whoami", self.svc, finalize)
        self.assertIn("Alice", res)

    def test_list_prs_uses_default_repo(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("list_prs", self.svc, finalize)
        self.assertIn("#1", res)
        self.assertIn("OPEN", res)

    def test_merge_pr_logs_to_ledger(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("merge_pr", self.svc, finalize, pr_id=42)
        self.assertIn("[bitbucket:merged]", res)
        # Should have called get_pr first to get version, then merge_pr.
        kinds = [c[0] for c in self.client.calls]
        self.assertEqual(kinds, ["get_pr", "merge_pr"])
        # Ledger should have one entry.
        self.assertEqual(len(self.svc.edit_ledger.entries), 1)
        self.assertEqual(self.svc.edit_ledger.entries[0]["change_type"], "merge_pr")
        # Activity log should have the bitbucket_action event.
        self.assertEqual(self.svc.activity_log.events[0][0], "bitbucket_action")

    def test_decline_pr_uses_pr_version(self):
        finalize, _ = _captured_finalize()
        tool.handle_bitbucket("decline_pr", self.svc, finalize, pr_id=5)
        # Should have called get_pr and then decline_pr with version=7.
        kinds = [(c[0], c[2].get("version")) for c in self.client.calls]
        self.assertEqual(kinds, [("get_pr", None), ("decline_pr", 7)])

    def test_update_pr_merges_unchanged_fields_from_live_pr(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket(
            "update_pr", self.svc, finalize, pr_id=9, description="New body"
        )
        self.assertIn("[bitbucket:updated]", res)
        kinds = [c[0] for c in self.client.calls]
        self.assertEqual(kinds, ["get_pr", "update_pr"])
        kw = self.client.calls[1][2]
        self.assertEqual(kw["version"], 7)          # optimistic-lock version
        self.assertEqual(kw["title"], "T")          # merged from the live PR
        self.assertEqual(kw["description"], "New body")
        self.assertEqual(kw["reviewers"], [])       # live PR has none
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "update_pr"
        )

    def test_update_pr_replaces_reviewers_when_given(self):
        finalize, _ = _captured_finalize()
        tool.handle_bitbucket(
            "update_pr", self.svc, finalize, pr_id=9, reviewers="bob, carol"
        )
        kw = self.client.calls[1][2]
        self.assertEqual(kw["reviewers"], ["bob", "carol"])

    def test_update_pr_with_nothing_to_change_errors_without_calls(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("update_pr", self.svc, finalize, pr_id=9)
        self.assertIn("nothing to change", res)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_needs_work_pr_uses_whoami_identity(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("needs_work_pr", self.svc, finalize, pr_id=3)
        self.assertIn("[bitbucket:needs-work]", res)
        kinds = [c[0] for c in self.client.calls]
        self.assertEqual(kinds, ["whoami", "set_reviewer_status"])
        kw = self.client.calls[1][2]
        self.assertEqual(kw["user_slug"], "alice")
        self.assertEqual(kw["status"], "NEEDS_WORK")
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "needs_work_pr"
        )

    def test_get_pr_commits_renders_and_stays_out_of_ledger(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("get_pr_commits", self.svc, finalize, pr_id=4)
        self.assertIn("[bitbucket:pr_commits]", res)
        self.assertIn("abc1234", res)
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_update_pr_comment_fetches_version_first(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket(
            "update_pr_comment", self.svc, finalize,
            pr_id=4, comment_id=17, body="new text",
        )
        self.assertIn("[bitbucket:comment-updated]", res)
        kinds = [c[0] for c in self.client.calls]
        self.assertEqual(kinds, ["get_pr_comment", "update_pr_comment"])
        kw = self.client.calls[1][2]
        self.assertEqual(kw["version"], 3)
        self.assertEqual(kw["text"], "new text")
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "update_pr_comment"
        )

    def test_update_pr_comment_requires_comment_id_and_body(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket(
            "update_pr_comment", self.svc, finalize, pr_id=4
        )
        self.assertIn("comment_id and body are required", res)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_delete_pr_comment_fetches_version_first(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket(
            "delete_pr_comment", self.svc, finalize, pr_id=4, comment_id=17
        )
        self.assertIn("[bitbucket:comment-deleted]", res)
        kinds = [(c[0], c[2].get("version")) for c in self.client.calls]
        self.assertEqual(kinds, [("get_pr_comment", None),
                                 ("delete_pr_comment", 3)])

    def test_list_pr_tasks_renders_state(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket(
            "list_pr_tasks", self.svc, finalize, pr_id=4, state="OPEN"
        )
        self.assertIn("[bitbucket:pr_tasks]", res)
        self.assertIn("[OPEN] id=91 (Alice) Add a regression test", res)
        self.assertEqual(self.client.calls[0][2]["state"], "OPEN")
        self.assertEqual(self.svc.edit_ledger.entries, [])

    def test_create_pr_task_requires_body_then_logs(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("create_pr_task", self.svc, finalize, pr_id=4)
        self.assertIn("body is required", res)
        res = tool.handle_bitbucket(
            "create_pr_task", self.svc, finalize, pr_id=4, body="Do the thing"
        )
        self.assertIn("task-id=92", res)
        self.assertEqual(
            self.svc.edit_ledger.entries[0]["change_type"], "create_pr_task"
        )

    def test_resolve_pr_task_sets_resolved_with_version(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket(
            "resolve_pr_task", self.svc, finalize, pr_id=4, comment_id=91
        )
        self.assertIn("[bitbucket:task-resolved]", res)
        kinds = [c[0] for c in self.client.calls]
        self.assertEqual(kinds, ["get_pr_comment", "set_pr_task_state"])
        kw = self.client.calls[1][2]
        self.assertEqual(kw["version"], 3)
        self.assertEqual(kw["state"], "RESOLVED")

    def test_missing_pr_id_for_get_pr(self):
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("get_pr", self.svc, finalize)
        self.assertIn("pr_id required", res)

    def test_repo_scoped_action_without_defaults_errors(self):
        from services import bitbucket_credentials as bb_creds
        bb_creds.set_default_repo("", "", project_path=self._tmp.name)
        finalize, _ = _captured_finalize()
        res = tool.handle_bitbucket("list_branches", self.svc, finalize)
        self.assertIn("project and repo are required", res)

    def test_cap_clamps_single_overlong_line(self):
        # Issue 7: one line with no newlines must still be clamped by chars.
        long_line = "x" * 200_000
        out = tool._cap(long_line)
        self.assertTrue(out.endswith("[truncated]"))
        self.assertLessEqual(
            len(out), tool._RESPONSE_TOKEN_CAP * 4 + len("\n[truncated]")
        )
        self.assertLess(len(out), len(long_line))


if __name__ == "__main__":
    unittest.main()
