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

    def test_complete_keeps_task_links(self):
        ms = self.store.create_milestone("M1")
        t = self.store.create_task("a", milestone_id=ms["id"])
        self.store.update_task(t["id"], status="done")
        res = self.store.complete_milestone(ms["id"])
        self.assertEqual(res["lifecycle"], "completed")
        self.assertTrue(res["completed_at"])
        # The link survives — the whole point vs archive.
        self.assertEqual(self.store.get_task(t["id"])["milestone_id"], ms["id"])
        # Hidden from default listings, board, and report; visible on request.
        self.assertEqual(self.store.list_milestones(), [])
        self.assertEqual(len(self.store.list_milestones(include_archived=True)), 1)
        self.assertEqual(self.store.board()["milestones"], [])
        self.assertEqual(self.store.report()["milestones"], [])

    def test_complete_refuses_open_tasks(self):
        ms = self.store.create_milestone("M1")
        self.store.create_task("open one", milestone_id=ms["id"])
        res = self.store.complete_milestone(ms["id"])
        self.assertIn("error", res)
        self.assertIn("open task", res["error"])

    def test_complete_only_from_active(self):
        ms = self.store.create_milestone("M1")
        self.store.archive_milestone(ms["id"])
        self.assertIn("error", self.store.complete_milestone(ms["id"]))

    def test_complete_is_not_purged(self):
        ms = self.store.create_milestone("M1")
        self.store.complete_milestone(ms["id"])
        self.assertEqual(self.store.purge_archived("milestone")["purged"], 0)
        self.assertEqual(len(self.store.list_milestones(include_archived=True)), 1)

    def test_reopen_by_id_and_by_name(self):
        ms = self.store.create_milestone("Big Ship")
        self.store.complete_milestone(ms["id"])
        # resolve_milestone name-matching covers active only — reopen has
        # its own completed-name fallback.
        self.assertIsNone(self.store.resolve_milestone("big ship"))
        res = self.store.reopen_milestone("big ship")
        self.assertEqual(res["lifecycle"], "active")
        self.assertIsNone(res["completed_at"])
        self.assertEqual(len(self.store.list_milestones()), 1)
        self.assertIn("error", self.store.reopen_milestone(ms["id"]))  # active now


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


class TestHistory(TaskStoreBase):
    def _events(self, **kw):
        return self.store.history(**kw)

    def test_create_update_move_archive_recorded(self):
        t = self.store.create_task("x", created_by="mcp")
        self.store.update_task(t["id"], actor="ui", priority="p1")
        self.store.move_task(t["id"], status="in_progress", actor="hub")
        self.store.archive_task(t["id"], actor="mcp")
        ops = [(e["entity"], e["op"]) for e in self._events(limit=10)]
        self.assertEqual(ops, [("task", "archive"), ("task", "move"),
                               ("task", "update"), ("task", "create")])

    def test_update_patch_before_after(self):
        t = self.store.create_task("x")
        self.store.update_task(t["id"], priority="p0")
        ev = self._events(op="update", limit=1)[0]
        self.assertEqual(ev["patch"]["priority"], ["p2", "p0"])
        self.assertEqual(ev["id"], t["id"])
        self.assertGreaterEqual(ev["rev"], 2)

    def test_actor_and_item_filter(self):
        a = self.store.create_task("a", created_by="mcp")
        self.store.create_task("b")
        self.store.update_task(a["id"], actor="ui", status="done")
        rows = self._events(item_id=a["id"], limit=10)
        self.assertEqual({e["id"] for e in rows}, {a["id"]})
        create = [e for e in rows if e["op"] == "create"][0]
        self.assertEqual(create["actor"], "mcp")
        update = [e for e in rows if e["op"] == "update"][0]
        self.assertEqual(update["actor"], "ui")

    def test_failed_mutation_writes_no_event(self):
        t = self.store.create_task("x")
        n = len(self._events(limit=100))
        self.store.update_task(t["id"], status="bogus")  # validation error
        self.store.update_task("zzzzzz", priority="p1")  # unknown id
        self.assertEqual(len(self._events(limit=100)), n)

    def test_link_and_milestone_events(self):
        t = self.store.create_task("x")
        self.store.add_link(t["id"], "file", "cli/server.py")
        ms = self.store.create_milestone("M1")
        self.store.archive_milestone(ms["id"])
        ops = [e["op"] for e in self._events(limit=3)]
        self.assertEqual(ops, ["archive", "create", "link"])
        arch = self._events(op="archive", limit=1)[0]
        self.assertEqual(arch["data"]["detached_tasks"], 0)

    def test_rotation(self):
        from services import task_store as ts_mod
        old = ts_mod._EVENTS_ROTATE_BYTES
        ts_mod._EVENTS_ROTATE_BYTES = 200
        try:
            for i in range(20):
                self.store.create_task(f"t{i}")
            self.assertTrue(
                self.store.events_file.with_name("events.jsonl.1").exists())
        finally:
            ts_mod._EVENTS_ROTATE_BYTES = old

    def test_migration_scaffold_applies_registered_upgrades(self):
        from services import task_store as ts_mod
        self.store.create_task("x")
        doc = json.loads(self.store.pm_file.read_text(encoding="utf-8"))
        doc["schema_version"] = 0
        self.store.pm_file.write_text(json.dumps(doc), encoding="utf-8")
        ts_mod._MIGRATIONS[0] = lambda d: {**d, "migrated_marker": True}
        try:
            loaded = self.store._load()
            self.assertTrue(loaded.get("migrated_marker"))
            self.assertEqual(loaded["schema_version"], 1)
        finally:
            ts_mod._MIGRATIONS.pop(0, None)


class TestDependencies(TaskStoreBase):
    def test_block_unblock_roundtrip_and_dedupe(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        res = self.store.add_dependency(a["id"], b["id"])
        self.assertEqual(res["blocked_by"], [b["id"]])
        res = self.store.add_dependency(a["id"], b["id"])  # idempotent
        self.assertEqual(res["blocked_by"], [b["id"]])
        res = self.store.remove_dependency(a["id"], b["id"])
        self.assertEqual(res["blocked_by"], [])

    def test_self_and_cycle_rejected(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        c = self.store.create_task("c")
        self.assertIn("error", self.store.add_dependency(a["id"], a["id"]))
        self.store.add_dependency(b["id"], a["id"])
        self.store.add_dependency(c["id"], b["id"])
        cyc = self.store.add_dependency(a["id"], c["id"])
        self.assertIn("cycle", cyc["error"])

    def test_done_blocker_auto_releases_dependent(self):
        a = self.store.create_task("a")
        b = self.store.create_task("blocker")
        self.store.add_dependency(a["id"], b["id"])
        self.store.update_task(a["id"], status="blocked")
        res = self.store.update_task(b["id"], status="done")
        self.assertEqual(res.get("released"), [a["id"]])
        self.assertEqual(self.store.get_task(a["id"])["status"], "backlog")
        ev = self.store.history(op="unblocked", limit=1)[0]
        self.assertEqual(ev["id"], a["id"])
        self.assertEqual(ev["data"]["released_by"], b["id"])

    def test_no_release_while_other_blockers_open(self):
        a = self.store.create_task("a")
        b1 = self.store.create_task("b1")
        b2 = self.store.create_task("b2")
        self.store.add_dependency(a["id"], b1["id"])
        self.store.add_dependency(a["id"], b2["id"])
        self.store.update_task(a["id"], status="blocked")
        res = self.store.update_task(b1["id"], status="done")
        self.assertNotIn("released", res)
        self.assertEqual(self.store.get_task(a["id"])["status"], "blocked")


class TestSubtasks(TaskStoreBase):
    def test_parent_roundtrip_and_one_level(self):
        p = self.store.create_task("parent")
        c = self.store.create_task("child", parent_id=p["id"])
        self.assertEqual(c["parent_id"], p["id"])
        self.assertIn("error",
                      self.store.create_task("grandchild", parent_id=c["id"]))
        self.assertIn("error",
                      self.store.update_task(p["id"], parent_id=c["id"]))

    def test_parent_must_exist_and_not_self(self):
        t = self.store.create_task("t")
        self.assertIn("error", self.store.create_task("x", parent_id="nope9999"))
        self.assertIn("error",
                      self.store.update_task(t["id"], parent_id=t["id"]))

    def test_clear_parent(self):
        p = self.store.create_task("parent")
        c = self.store.create_task("child", parent_id=p["id"])
        res = self.store.update_task(c["id"], parent_id=None)
        self.assertIsNone(res["parent_id"])


class TestReport(TaskStoreBase):
    def test_report_sections(self):
        overdue = self.store.create_task("late", due_date="2020-01-01")
        b = self.store.create_task("blocker")
        blocked = self.store.create_task("stuck")
        self.store.add_dependency(blocked["id"], b["id"])
        self.store.update_task(blocked["id"], status="blocked")
        manual = self.store.create_task("manual-block")
        self.store.update_task(manual["id"], status="blocked")
        ms = self.store.create_milestone("M1", target_date="2020-06-01")
        self.store.update_task(overdue["id"], milestone_id=ms["id"])
        d = self.store.create_task("shipped")
        self.store.update_task(d["id"], status="done")
        rep = self.store.report()
        self.assertEqual([r["id"] for r in rep["overdue"]], [overdue["id"]])
        self.assertGreater(rep["overdue"][0]["days_overdue"], 1000)
        self.assertEqual([r["id"] for r in rep["blocked"]], [blocked["id"]])
        self.assertEqual(rep["blocked"][0]["blockers"][0]["id"], b["id"])
        self.assertEqual([r["id"] for r in rep["ready"]], [manual["id"]])
        m = rep["milestones"][0]
        self.assertTrue(m["at_risk"])  # target long past with an open task
        self.assertEqual(m["overdue"], 1)
        self.assertEqual(rep["throughput"]["done_last_7d"], 1)
        self.assertIsNotNone(rep["throughput"]["avg_cycle_days"])

    def test_report_empty_store(self):
        rep = self.store.report()
        self.assertEqual(rep["overdue"], [])
        self.assertEqual(rep["blocked"], [])
        self.assertEqual(rep["milestones"], [])
        self.assertIsNone(rep["throughput"]["avg_cycle_days"])


if __name__ == "__main__":
    unittest.main()
