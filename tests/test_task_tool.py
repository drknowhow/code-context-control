"""Tests for cli/tools/tasks.py — the c3_task MCP action router."""
import tempfile
import unittest
from pathlib import Path

from cli.tools.tasks import READ_ACTIONS, handle_task
from services.task_store import TaskStore


def _finalize(name, args, resp, summ, **kw):
    return resp


class _StubSessionMgr:
    current_session = {"id": "sess-1"}


class _StubSvc:
    def __init__(self, project_path, pm_enabled=True):
        self.project_path = str(project_path)
        self.task_store = TaskStore(self.project_path)
        self.hybrid_config = {"pm": {"enabled": pm_enabled}}
        self.session_mgr = _StubSessionMgr()


class TaskToolBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self.svc = _StubSvc(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, action, **kw):
        return handle_task(action, self.svc, _finalize, **kw)


class TestRouting(TaskToolBase):
    def test_add_and_provenance(self):
        out = self._run("add", title="Ship it", priority="p1")
        self.assertIn("[task:added]", out)
        task = self.svc.task_store.list_tasks()[0]
        self.assertEqual(task["created_by"], "mcp")
        self.assertEqual(task["origin_session"], "sess-1")

    def test_add_requires_title(self):
        self.assertIn("[task:error]", self._run("add"))

    def test_update_and_done(self):
        self._run("add", title="x")
        tid = self.svc.task_store.list_tasks()[0]["id"]
        out = self._run("update", task_id=tid, status="in_progress")
        self.assertIn("[task:updated]", out)
        out = self._run("done", task_id=tid)
        self.assertIn("[task:done]", out)
        self.assertIsNotNone(self.svc.task_store.get_task(tid)["completed_at"])

    def test_update_requires_a_field(self):
        self._run("add", title="x")
        tid = self.svc.task_store.list_tasks()[0]["id"]
        self.assertIn("[task:error]", self._run("update", task_id=tid))

    def test_id_prefix_accepted(self):
        self._run("add", title="x")
        tid = self.svc.task_store.list_tasks()[0]["id"]
        self.assertIn("[task:done]", self._run("done", task_id=tid[:6]))

    def test_list_board_get(self):
        self._run("add", title="find me", tags="bug,ui")
        out = self._run("list", query="find")
        self.assertIn("find me", out)
        self.assertIn("#bug", out)
        out = self._run("board")
        self.assertIn("[task:board]", out)
        tid = self.svc.task_store.list_tasks()[0]["id"]
        out = self._run("get", task_id=tid)
        self.assertIn("find me", out)

    def test_link_unlink(self):
        self._run("add", title="x")
        tid = self.svc.task_store.list_tasks()[0]["id"]
        out = self._run("link", task_id=tid, link_type="file", ref="cli/c3.py")
        self.assertIn("1 link(s)", out)
        out = self._run("unlink", task_id=tid, link_type="file", ref="cli/c3.py")
        self.assertIn("0 link(s)", out)

    def test_unknown_action(self):
        self.assertIn("Unknown action", self._run("explode"))

    def test_missing_store_guard(self):
        svc = _StubSvc(self.root)
        svc.task_store = None
        out = handle_task("list", svc, _finalize)
        self.assertIn("task store unavailable", out)

    def test_disabled_config(self):
        svc = _StubSvc(self.root, pm_enabled=False)
        out = handle_task("add", svc, _finalize, title="x")
        self.assertIn("[task:disabled]", out)


class TestMilestonesAndNotes(TaskToolBase):
    def test_milestone_flow_and_name_resolution(self):
        out = self._run("milestone_add", name="Release v2.45", target_date="2026-08-01")
        self.assertIn("[milestone:added]", out)
        out = self._run("add", title="in ms", milestone="release v2.45")
        self.assertIn("[task:added]", out)
        out = self._run("milestone_list")
        self.assertIn("0/1 (0%)", out)
        out = self._run("milestone_update", milestone="Release v2.45", name="R45")
        self.assertIn("[milestone:updated]", out)
        out = self._run("milestone_archive", milestone="R45")
        self.assertIn("1 task(s) detached", out)

    def test_unknown_milestone_on_add(self):
        out = self._run("add", title="x", milestone="nope")
        self.assertIn("no milestone matches", out)

    def test_notes_default_kind_and_unfiltered_list(self):
        self._run("note_add", note="plain note")
        self._run("note_add", note="big decision", kind="decision")
        out = self._run("note_list")
        self.assertIn("plain note", out)
        self.assertIn("big decision", out)
        out = self._run("note_list", kind="decision")
        self.assertNotIn("plain note", out)


class TestReadActions(unittest.TestCase):
    def test_read_action_set(self):
        self.assertEqual(READ_ACTIONS,
                         {"list", "get", "board", "milestone_list", "note_list"})


if __name__ == "__main__":
    unittest.main()
