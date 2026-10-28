"""FileMemo reuses a value derived from a file only while the file is unchanged.

The hub and Oracle poll every registered project's notification, task and
activity files several times a minute, and between two polls almost none of
them change. These tests pin that a write shows on the next call, that a
file written within the last RACY_S seconds is never trusted to its
signature, and that the hub's per-project counters read through the memo.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from services import file_memo
from services.file_memo import FileMemo


def _backdate(path: Path, seconds: float = 60.0) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


class _Counter:
    def __init__(self):
        self.calls = 0

    def __call__(self, path, *args):
        self.calls += 1
        try:
            return (Path(path).read_text(encoding="utf-8"), args)
        except OSError:
            return None


class TestFileMemo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "data.txt"
        self.compute = _Counter()
        self.memo = FileMemo(self.compute)

    def write(self, text, age=60.0):
        self.path.write_text(text, encoding="utf-8")
        if age:
            _backdate(self.path, age)

    def test_an_unchanged_file_is_computed_once(self):
        self.write("a")
        for _ in range(3):
            self.assertEqual(self.memo.get(self.path), ("a", ()))
        self.assertEqual(self.compute.calls, 1)

    def test_a_write_shows_on_the_next_call(self):
        self.write("a")
        self.memo.get(self.path)
        self.write("bb", age=30.0)
        self.assertEqual(self.memo.get(self.path), ("bb", ()))

    def test_a_fresh_file_is_recomputed_every_call(self):
        """Two writes inside one timestamp tick can leave size and mtime equal."""
        self.write("a", age=0)
        self.memo.get(self.path)
        self.memo.get(self.path)
        self.assertEqual(self.compute.calls, 2)

    def test_arguments_are_part_of_the_key(self):
        self.write("a")
        self.assertEqual(self.memo.get(self.path, "x", 1), ("a", ("x", 1)))
        self.assertEqual(self.memo.get(self.path, "y", 1), ("a", ("y", 1)))
        self.assertEqual(self.compute.calls, 2)

    def test_a_missing_file_gets_the_computes_own_answer(self):
        self.assertIsNone(self.memo.get(self.path))
        self.assertIsNone(self.memo.get(self.path))
        self.assertEqual(self.compute.calls, 2)

    def test_the_entry_count_is_bounded(self):
        memo = FileMemo(self.compute, max_entries=2)
        self.write("a")
        for arg in range(5):
            memo.get(self.path, arg)
        self.assertLessEqual(len(memo._entries), 2)

    def test_clear(self):
        self.write("a")
        self.memo.get(self.path)
        self.memo.clear()
        self.memo.get(self.path)
        self.assertEqual(self.compute.calls, 2)

    def test_racy_window_matches_the_access_guards(self):
        from services import access_guard
        self.assertEqual(file_memo.RACY_S, access_guard._RACY_S)


class TestCountersReadThroughTheMemo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3" / "pm").mkdir(parents=True)

    def test_open_task_count(self):
        from services.task_store import open_task_count
        self.assertEqual(open_task_count(self.proj), 0)
        pm = self.proj / ".c3" / "pm" / "pm.json"
        tasks = [{"status": "backlog"}, {"status": "done"},
                 {"status": "in_progress", "lifecycle": "archived"}, "junk"]
        pm.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
        _backdate(pm)
        self.assertEqual(open_task_count(self.proj), 1)
        tasks.append({"status": "blocked"})
        pm.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
        _backdate(pm, 30.0)
        self.assertEqual(open_task_count(self.proj), 2)

    def test_notification_count(self):
        from cli.hub_server import _notification_count
        nf = self.proj / ".c3" / "notifications.jsonl"
        self.assertEqual(_notification_count(str(self.proj)), 0)
        rows = [{"id": 1}, {"id": 2, "acknowledged": True}, "[1]", {"id": 3}]
        nf.write_text("\n".join(json.dumps(r) for r in rows) + "\n{broken\n", encoding="utf-8")
        _backdate(nf)
        self.assertEqual(_notification_count(str(self.proj)), 2)

    def test_find_last_hands_out_copies(self):
        from services.activity_log import ActivityLog
        log = ActivityLog(str(self.proj))
        log.log("session_start", {"session_id": "s1"})
        _backdate(log.log_file)
        first = log.find_last("session_start")
        first[0]["type"] = "mutated"
        self.assertEqual(log.find_last("session_start")[0]["type"], "session_start")


if __name__ == "__main__":
    unittest.main()
