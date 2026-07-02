"""Tests for services/task_store.py — the PM store (tasks/milestones/notes)."""
import json
import tempfile
import unittest
from pathlib import Path

from services.task_store import TaskStore, open_task_count


class TaskStoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self.store = TaskStore(str(self.root))

    def tearDown(self):
        self._tmp.cleanup()


class TestTasks(TaskStoreBase):
    def test_create_defaults_and_shape(self):
        t = self.store.create_task("Ship board", created_by="mcp")
        self.assertEqual(len(t["id"]), 12)
        self.assertEqual(t["status"], "backlog")
        self.assertEqual(t["priority"], "p2")
        self.assertEqual(t["lifecycle"], "active")
        self.assertIsNone(t["completed_at"])
        self.assertEqual(t["created_by"], "mcp")
        self.assertEqual(t["sort_key"], 1024.0)

    def test_no_write_until_first_mutation(self):
        store = TaskStore(str(self.root / "sub"))
        self.assertEqual(store.list_tasks(), [])
        self.assertFalse(store.pm_file.exists())

    def test_validation(self):
        self.assertIn("error", self.store.create_task(""))
        self.assertIn("error", self.store.create_task("x", status="doing"))
        self.assertIn("error", self.store.create_task("x", priority="urgent"))
        self.assertIn("error", self.store.create_task("x", due_date="tomorrow"))
        t = self.store.create_task("x")
        self.assertIn("error", self.store.update_task(t["id"], nonsense=1))
        self.assertIn("error", self.store.update_task(t["id"], title=""))

    def test_done_stamps_and_clears_completed_at(self):
        t = self.store.create_task("x")
        done = self.store.update_task(t["id"], status="done")
        self.assertIsNotNone(done["completed_at"])
        back = self.store.update_task(t["id"], status="in_progress")
        self.assertIsNone(back["completed_at"])

    def test_move_rank_between_neighbors(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        c = self.store.create_task("c")
        moved = self.store.move_task(c["id"], before_id=a["id"])
        col = self.store.board()["columns"]["backlog"]
        self.assertEqual([t["title"] for t in col], ["c", "a", "b"])
        self.assertLess(moved["sort_key"], 1024.0)

    def test_move_column_lands_at_bottom(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b", status="in_progress")
        moved = self.store.move_task(a["id"], status="in_progress")
        col = self.store.board()["columns"]["in_progress"]
        self.assertEqual([t["title"] for t in col], ["b", "a"])
        self.assertGreater(moved["sort_key"], b["sort_key"])

    def test_rebalance_on_gap_collapse(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        # squeeze repeatedly into the same gap until rebalance triggers
        mover = self.store.create_task("m")
        for _ in range(60):
            self.store.move_task(mover["id"], before_id=b["id"])
            self.store.move_task(mover["id"], after_id=a["id"])
        col = self.store.board()["columns"]["backlog"]
        keys = [t["sort_key"] for t in col]
        self.assertEqual(keys, sorted(keys))
        self.assertGreater(min(b - a for a, b in zip(keys, keys[1:])), 1e-6)

    def test_archive_restore_purge(self):
        t = self.store.create_task("x")
        self.assertEqual(self.store.archive_task(t["id"])["lifecycle"], "archived")
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(len(self.store.list_tasks(include_archived=True)), 1)
        self.assertEqual(self.store.restore_task(t["id"])["lifecycle"], "active")
        self.store.archive_task(t["id"])
        self.assertEqual(self.store.purge_archived("task")["purged"], 1)
        self.assertEqual(self.store.list_tasks(include_archived=True), [])

    def test_id_prefix_resolution(self):
        t = self.store.create_task("x")
        self.assertIsNotNone(self.store.get_task(t["id"][:6]))
        self.assertIsNone(self.store.get_task(t["id"][:3]))  # too short
        self.assertIsNone(self.store.get_task("zzzz"))

    def test_list_filters_and_query(self):
        self.store.create_task("fix parser bug", tags=["bug"], priority="p0")
        self.store.create_task("write docs", tags=["docs"])
        self.assertEqual(len(self.store.list_tasks(tag="bug")), 1)
        self.assertEqual(len(self.store.list_tasks(priority="p0")), 1)
        self.assertEqual(len(self.store.list_tasks(query="parser")), 1)
        self.assertEqual(self.store.list_tasks(query="parser")[0]["title"], "fix parser bug")

    def test_cross_instance_visibility(self):
        # Two store objects on the same path — proves reload-per-op.
        other = TaskStore(str(self.root))
        t = self.store.create_task("visible everywhere")
        self.assertIsNotNone(other.get_task(t["id"]))
        other.update_task(t["id"], status="done")
        self.assertEqual(self.store.get_task(t["id"])["status"], "done")


class TestLinks(TaskStoreBase):
    def test_link_add_dedup_remove(self):
        t = self.store.create_task("x")
        self.store.add_link(t["id"], "file", "cli/hub_server.py")
        self.store.add_link(t["id"], "file", "cli/hub_server.py")  # dedup
        got = self.store.get_task(t["id"])
        self.assertEqual(len(got["links"]), 1)
        self.store.remove_link(t["id"], "file", "cli/hub_server.py")
        self.assertEqual(self.store.get_task(t["id"])["links"], [])

    def test_link_validation(self):
        t = self.store.create_task("x")
        self.assertIn("error", self.store.add_link(t["id"], "url", "http://x"))
        self.assertIn("error", self.store.add_link(t["id"], "file", ""))


class TestMilestones(TaskStoreBase):
    def test_progress_computed(self):
        ms = self.store.create_milestone("M1", target_date="2026-08-01")
        a = self.store.create_task("a", milestone_id=ms["id"])
        self.store.create_task("b", milestone_id=ms["id"])
        self.store.update_task(a["id"], status="done")
        listed = self.store.list_milestones()[0]
        self.assertEqual(listed["progress"], {"total": 2, "done": 1, "pct": 50})

    def test_archive_detaches_tasks(self):
        ms = self.store.create_milestone("M1")
        t = self.store.create_task("a", milestone_id=ms["id"])
        res = self.store.archive_milestone(ms["id"])
        self.assertEqual(res["detached_tasks"], 1)
        self.assertIsNone(self.store.get_task(t["id"])["milestone_id"])
        self.assertEqual(self.store.list_milestones(), [])

    def test_resolve_by_unique_name(self):
        ms = self.store.create_milestone("Release v2.45")
        self.assertEqual(self.store.resolve_milestone("release v2.45")["id"], ms["id"])
        self.assertIsNone(self.store.resolve_milestone("nope"))

    def test_create_task_with_unknown_milestone_rejected(self):
        self.assertIn("error", self.store.create_task("x", milestone_id="zzzzzz"))


class TestNotes(TaskStoreBase):
    def test_note_crud(self):
        n = self.store.add_note("Chose float sort keys", kind="decision", author="hub")
        self.assertEqual(n["kind"], "decision")
        self.store.update_note(n["id"], text="updated")
        notes = self.store.list_notes(kind="decision")
        self.assertEqual(notes[0]["text"], "updated")
        self.store.archive_note(n["id"])
        self.assertEqual(self.store.list_notes(), [])

    def test_note_validation(self):
        self.assertIn("error", self.store.add_note(""))
        self.assertIn("error", self.store.add_note("x", kind="rant"))
        self.assertIn("error", self.store.add_note("x", task_id="zzzzzz"))


class TestAggregates(TaskStoreBase):
    def test_stats_shape(self):
        self.store.create_task("a", priority="p0", due_date="2001-01-01")  # overdue
        self.store.create_task("b", status="in_progress")
        done = self.store.create_task("c")
        self.store.update_task(done["id"], status="done")
        self.store.create_milestone("M1")
        self.store.add_note("n")
        s = self.store.stats()
        self.assertEqual(s["open"], 2)
        self.assertEqual(s["by_status"]["done"], 1)
        self.assertEqual(s["overdue"], 1)
        self.assertEqual(s["milestones_active"], 1)
        self.assertEqual(s["notes"], 1)

    def test_board_columns_sorted(self):
        self.store.create_task("a")
        self.store.create_task("b")
        board = self.store.board()
        self.assertEqual(set(board["columns"].keys()),
                         {"backlog", "in_progress", "blocked", "done"})
        keys = [t["sort_key"] for t in board["columns"]["backlog"]]
        self.assertEqual(keys, sorted(keys))
        self.assertIn("stats", board)

    def test_board_milestone_filter(self):
        ms = self.store.create_milestone("M1")
        self.store.create_task("in", milestone_id=ms["id"])
        self.store.create_task("out")
        board = self.store.board(milestone_id=ms["id"])
        self.assertEqual([t["title"] for t in board["columns"]["backlog"]], ["in"])


class TestDurability(TaskStoreBase):
    def test_corrupt_file_recovered(self):
        self.store.create_task("x")
        self.store.pm_file.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.list_tasks(), [])
        self.store.create_task("fresh")  # triggers save of clean doc
        self.assertTrue((self.store.data_dir / "pm.json.corrupt-1").exists())
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_schema_version_stamped(self):
        self.store.create_task("x")
        doc = json.loads(self.store.pm_file.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema_version"], 1)

    def test_open_task_count_helper(self):
        self.assertEqual(open_task_count(self.root), 0)
        self.store.create_task("a")
        done = self.store.create_task("b")
        self.store.update_task(done["id"], status="done")
        archived = self.store.create_task("c")
        self.store.archive_task(archived["id"])
        self.assertEqual(open_task_count(self.root), 1)


if __name__ == "__main__":
    unittest.main()
