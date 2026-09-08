"""ActivityLog.find_last — rare events behind heavy traffic (v2.128.1).

`get_recent` scans only the last `limit * 100` lines. A session's own
`session_start` row falls out of that window within minutes of ordinary tool
use — measured at 319 lines from the end of C3's own 20,825-line log, where
`get_recent(limit=1, event_type="session_start")` returned nothing while the
session was running, so the hub reported the project idle.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.activity_log import ActivityLog  # noqa: E402


class TestFindLast(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        self.log = self.project / ".c3" / "activity_log.jsonl"
        self.log.parent.mkdir(parents=True)
        self.activity = ActivityLog(str(self.project))

    def _write(self, rows):
        self.log.write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def _buried(self, noise: int):
        rows = [{"type": "session_start", "session_id": "s1", "timestamp": "t0"}]
        rows += [{"type": "tool_call", "timestamp": f"t{i}"} for i in range(noise)]
        self._write(rows)

    def test_finds_a_row_get_recent_cannot_see(self):
        self._buried(5000)
        self.assertEqual(self.activity.get_recent(limit=1, event_type="session_start"), [])
        found = self.activity.find_last("session_start")
        self.assertEqual([r["session_id"] for r in found], ["s1"])

    def test_row_near_the_end_still_works(self):
        self._buried(3)
        self.assertEqual(len(self.activity.find_last("session_start")), 1)

    def test_newest_first_and_limited(self):
        self._write([
            {"type": "session_start", "session_id": "old", "timestamp": "t0"},
            {"type": "tool_call", "timestamp": "t1"},
            {"type": "session_start", "session_id": "mid", "timestamp": "t2"},
            {"type": "session_start", "session_id": "new", "timestamp": "t3"},
        ])
        self.assertEqual(
            [r["session_id"] for r in self.activity.find_last("session_start", limit=2)],
            ["new", "mid"])

    def test_absent_type_and_missing_file(self):
        self._buried(10)
        self.assertEqual(self.activity.find_last("session_end"), [])
        self.log.unlink()
        self.assertEqual(self.activity.find_last("session_start"), [])

    def test_survives_a_corrupt_line(self):
        self.log.write_text(
            "{not json\n"
            + json.dumps({"type": "session_start", "session_id": "s1"}) + "\n"
            + "\n"
            + json.dumps({"type": "tool_call"}) + "\n",
            encoding="utf-8")
        self.assertEqual(
            [r["session_id"] for r in self.activity.find_last("session_start")], ["s1"])

    def test_row_split_across_a_chunk_boundary(self):
        """The backwards reader stitches partial lines between chunks."""
        rows = [{"type": "session_start", "session_id": "s1", "timestamp": "t0"}]
        rows += [{"type": "tool_call", "payload": "x" * 200} for _ in range(2000)]
        self._write(rows)
        for chunk in (64, 128, 4096):
            with self.subTest(chunk=chunk):
                found = self.activity.find_last("session_start", chunk_bytes=chunk)
                self.assertEqual([r["session_id"] for r in found], ["s1"])

    def test_matches_get_recent_when_both_can_see_the_row(self):
        self._buried(20)
        self.assertEqual(
            self.activity.find_last("session_start"),
            self.activity.get_recent(limit=1, event_type="session_start"))


if __name__ == "__main__":
    unittest.main()
