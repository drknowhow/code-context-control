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
