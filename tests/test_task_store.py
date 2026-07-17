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


class TestConcurrencyAndRev(TaskStoreBase):
    def test_interleaved_writers_lose_no_tasks(self):
        # Two independent instances (separate threading.Locks, same pm.json)
        # simulate the hub/MCP/server multi-process topology: the pm.lock
        # file lock must serialize load->mutate->save so no create is lost.
        import threading
        stores = [TaskStore(str(self.root)) for _ in range(2)]
        errors = []

        def hammer(store, tag):
            for i in range(15):
                res = store.create_task(f"{tag}-{i}")
                if "error" in res:
                    errors.append(res)

        threads = [threading.Thread(target=hammer, args=(s, n))
                   for n, s in enumerate(stores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.list_tasks(limit=500)), 30)

    def test_rev_increments_per_mutation(self):
        t = self.store.create_task("x")
        rev1 = self.store.board()["rev"]
        self.store.update_task(t["id"], priority="p1")
        self.assertEqual(self.store.board()["rev"], rev1 + 1)

    def test_expected_rev_conflict(self):
        t = self.store.create_task("x")
        rev = self.store.board()["rev"]
        ok = self.store.update_task(t["id"], expected_rev=rev, priority="p0")
        self.assertNotIn("error", ok)
        stale = self.store.update_task(t["id"], expected_rev=rev, priority="p3")
        self.assertIn("error", stale)
        self.assertEqual(stale.get("code"), "rev_conflict")
        self.assertEqual(stale["current_rev"], rev + 1)

    def test_mutate_task_fields_and_move_single_transaction(self):
        t = self.store.create_task("x")
        rev = self.store.board()["rev"]
        res = self.store.mutate_task(t["id"],
                                     fields={"description": "d"},
                                     move={"status": "in_progress"})
        self.assertEqual(res["description"], "d")
        self.assertEqual(res["status"], "in_progress")
        self.assertEqual(self.store.board()["rev"], rev + 1)

    def test_mutate_task_requires_fields_or_move(self):
        t = self.store.create_task("x")
        self.assertIn("error", self.store.mutate_task(t["id"]))

    def test_unique_temp_no_fixed_tmp_left_behind(self):
        self.store.create_task("x")
        self.assertFalse((self.store.data_dir / "pm.json.tmp").exists())
        self.assertEqual(list(self.store.data_dir.glob("pm.json.tmp-*")), [])


class TestBackupRecovery(TaskStoreBase):
    def test_corrupt_restores_from_backup(self):
        self.store.create_task("keep me")
        self.store.create_task("second")  # this save writes pm.json.bak
        self.store.pm_file.write_text("{ not json", encoding="utf-8")
        titles = [r["title"] for r in self.store.list_tasks()]
        self.assertIn("keep me", titles)  # backup is one op behind, not empty
        self.assertTrue((self.store.data_dir / "pm.json.corrupt-1").exists())
        rec = self.store.last_recovery
        self.assertIsNotNone(rec)
        self.assertTrue(rec["restored_from_backup"])
        self.assertEqual(rec["quarantined"], "pm.json.corrupt-1")

    def test_recovery_survives_reload_and_next_mutation_persists(self):
        self.store.create_task("keep me")
        self.store.create_task("second")
        self.store.pm_file.write_text("{ not json", encoding="utf-8")
        self.store.list_tasks()  # triggers quarantine; pm.json now missing
        fresh = TaskStore(str(self.root))  # new instance, e.g. next hub request
        titles = [r["title"] for r in fresh.list_tasks()]
        self.assertIn("keep me", titles)  # served from backup, not empty
        fresh.create_task("after recovery")  # first mutation persists backup
        self.assertTrue(fresh.pm_file.exists())
        titles = [r["title"] for r in TaskStore(str(self.root)).list_tasks()]
        self.assertIn("keep me", titles)
        self.assertIn("after recovery", titles)

    def test_board_surfaces_recovery(self):
        self.store.create_task("a")
        self.store.create_task("b")
        self.store.pm_file.write_text("garbage", encoding="utf-8")
        board = self.store.board()
        self.assertIn("recovery", board)
        self.assertTrue(board["recovery"]["restored_from_backup"])

    def test_board_rev_present(self):
        self.store.create_task("a")
        self.assertGreaterEqual(self.store.board()["rev"], 1)


if __name__ == "__main__":
    unittest.main()
