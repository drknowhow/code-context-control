"""Tests for cli/tools/artifacts.py — the c3_artifacts MCP action router."""
import tempfile
import unittest
from pathlib import Path

from cli.tools.artifacts import READ_ACTIONS, handle_artifacts
from services.artifact_store import ArtifactStore
from services.edit_ledger import EditLedger


def _finalize(name, args, resp, summ, **kw):
    return resp


class _StubSessionMgr:
    current_session = {"id": "sess-1"}


class _StubSvc:
    def __init__(self, project_path, enabled=True, with_ledger=False):
        self.project_path = str(project_path)
        self.artifact_store = ArtifactStore(self.project_path)
        self.hybrid_config = {"agent_artifacts": {"enabled": enabled}}
        self.session_mgr = _StubSessionMgr()
        self.edit_ledger = EditLedger(self.project_path) if with_ledger else None


class ArtifactToolBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self.svc = _StubSvc(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel, content):
        fp = self.root / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")

    def _run(self, action, **kw):
        return handle_artifacts(action, self.svc, _finalize, **kw)


class TestRouting(ArtifactToolBase):
    def test_scan_reports_changes(self):
        self.write("CLAUDE.md", "# hi")
        out = self._run("scan")
        self.assertIn("[artifacts:scan] 1 tracked — 1 added", out)
        self.assertIn("instructions:CLAUDE.md", out)
        self.assertIn("[artifacts:scan] 1 tracked — 0 added, 0 modified, 0 deleted",
                      self._run("scan"))

    def test_list_and_filters(self):
        self.write("CLAUDE.md", "# hi")
        self.write(".mcp.json", "{}")
        self._run("scan")
        out = self._run("list")
        self.assertIn("[artifacts:list] 2 artifact(s)", out)
        only_mcp = self._run("list", cls="mcp")
        self.assertIn("1 artifact(s)", only_mcp)
        self.assertNotIn("CLAUDE.md", only_mcp)

    def test_list_before_scan_hints(self):
        self.assertIn("run c3_artifacts(action='scan')", self._run("list"))

    def test_history_show_diff(self):
        self.write("CLAUDE.md", "alpha\n")
        self._run("scan")
        self.write("CLAUDE.md", "beta\n")
        self._run("scan")
        hist = self._run("history", artifact="CLAUDE.md")
        self.assertIn("2 event(s)", hist)
        show = self._run("show", artifact="CLAUDE.md", version=1)
        self.assertIn("alpha", show)
        diff = self._run("diff", artifact="CLAUDE.md", version=1, against=2)
        self.assertIn("-alpha", diff)
        self.assertIn("+beta", diff)

    def test_diff_requires_version(self):
        self.write("CLAUDE.md", "x")
        self._run("scan")
        self.assertIn("[artifacts:error]", self._run("diff", artifact="CLAUDE.md"))

    def test_restore_round_trip_and_ledger_log(self):
        self.svc = _StubSvc(self.root, with_ledger=True)
        self.write("CLAUDE.md", "original")
        self._run("scan")
        self.write("CLAUDE.md", "changed")
        self._run("scan")
        out = self._run("restore", artifact="CLAUDE.md", version=1)
        self.assertIn("[artifacts:restored]", out)
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8"),
                         "original")
        entries = self.svc.edit_ledger.get_history(limit=5)
        self.assertTrue(any(e.get("change_type") == "restored" for e in entries))

    def test_restore_surfaces_warnings(self):
        self.write(".claude/settings.local.json", "{}")
        self._run("scan")
        self.write(".claude/settings.local.json", '{"a":1}')
        self._run("scan")
        out = self._run("restore", artifact="settings:.claude/settings.local.json",
                        version=1)
        self.assertIn("warning:", out)

    def test_status(self):
        self.write("CLAUDE.md", "x")
        self._run("scan")
        out = self._run("status")
        self.assertIn("[artifacts:status] 1 tracked", out)
        self.assertIn("instructions:1", out)

    def test_artifact_required_for_single_target_actions(self):
        for action in ("show", "diff", "restore"):
            self.assertIn("requires artifact", self._run(action))

    def test_unknown_and_missing_action(self):
        self.assertIn("unknown action", self._run("bogus"))
        self.assertIn("action required", self._run(""))

    def test_bad_int_params(self):
        self.assertIn("must be integers",
                      self._run("show", artifact="x", version="one"))


class TestGates(ArtifactToolBase):
    def test_disabled_gate(self):
        svc = _StubSvc(self.root, enabled=False)
        out = handle_artifacts("list", svc, _finalize)
        self.assertIn("[artifacts:disabled]", out)

    def test_store_unavailable(self):
        svc = _StubSvc(self.root)
        svc.artifact_store = None
        self.assertIn("[artifacts:error]", handle_artifacts("list", svc, _finalize))

    def test_read_actions_membership(self):
        self.assertEqual(READ_ACTIONS,
                         {"scan", "list", "history", "show", "diff", "status"})
        self.assertNotIn("restore", READ_ACTIONS)


if __name__ == "__main__":
    unittest.main()
