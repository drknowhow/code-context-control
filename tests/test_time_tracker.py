"""Tests for services/time_tracker.py — activity pings + manual entries."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.time_tracker import TimeTracker


class TimeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self.tr = TimeTracker(str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def _write_pings(self, offsets_min):
        """Write pings at now-minus-offset minutes into the current month file."""
        now = datetime.now(timezone.utc)
        self.tr.data_dir.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"ts": (now - timedelta(minutes=o)).isoformat(),
                             "source": "tool", "session": "", "pid": 1})
                 for o in sorted(offsets_min, reverse=True)]
        self.tr._ping_file().write_text("\n".join(lines) + "\n",
                                        encoding="utf-8")


class TestPings(TimeBase):
    def test_ping_throttle_and_startup_bypass(self):
        self.assertTrue(self.tr.ping("tool"))
        self.assertFalse(self.tr.ping("tool"))     # throttled
        self.assertTrue(self.tr.ping("startup"))   # bypasses throttle
        self.assertTrue(self.tr._ping_file().exists())

    def test_sessions_coalesce_by_idle_gap(self):
        # Two clusters separated by >15min idle: [70..60] and [5..0]
        self._write_pings([70, 65, 60, 5, 3, 0])
        sessions = self.tr.sessions()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["minutes"], 5)   # newest first
        self.assertEqual(sessions[1]["minutes"], 10)

    def test_isolated_ping_counts_minimum(self):
        self._write_pings([120])
        sessions = self.tr.sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["minutes"], 1)

    def test_malformed_ping_lines_skipped(self):
        self._write_pings([10, 0])
        with open(self.tr._ping_file(), "a", encoding="utf-8") as f:
            f.write("{not json}\n")
        self.assertEqual(len(self.tr.sessions()), 1)


class TestEntries(TimeBase):
    def test_crud_roundtrip(self):
        e = self.tr.add_entry(90, note="pairing", created_by="test")
        self.assertNotIn("error", e)
        self.assertEqual(e["minutes"], 90)
        upd = self.tr.update_entry(e["id"], minutes=45, note="solo")
        self.assertEqual(upd["minutes"], 45)
        self.assertEqual(upd["note"], "solo")
        self.assertEqual(len(self.tr.list_entries()), 1)
        res = self.tr.delete_entry(e["id"][:6])   # unique prefix resolves
        self.assertTrue(res["deleted"])
        self.assertEqual(self.tr.list_entries(), [])

    def test_validation(self):
        self.assertIn("error", self.tr.add_entry(0))
        self.assertIn("error", self.tr.add_entry(2000))
        self.assertIn("error", self.tr.add_entry("nope"))
        self.assertIn("error", self.tr.add_entry(30, date="tomorrow"))
        e = self.tr.add_entry(30)
        self.assertIn("error", self.tr.update_entry(e["id"], bogus=1))
        self.assertIn("error", self.tr.update_entry("zzzz9999"))
        self.assertIn("error", self.tr.delete_entry("zzzz9999"))

    def test_corrupt_entries_quarantined(self):
        self.tr.add_entry(30)
        self.tr.entries_file.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.tr.list_entries(), [])
        self.tr.add_entry(15)
        self.assertTrue(
            (self.tr.data_dir / "entries.json.corrupt-1").exists())


class TestSummary(TimeBase):
    def test_windows_and_by_day(self):
        self._write_pings([10, 5, 0])            # one ~10min session today
        self.tr.add_entry(60)                    # manual today
        old = (datetime.now(timezone.utc)
               - timedelta(days=10)).strftime("%Y-%m-%d")
        self.tr.add_entry(30, date=old)          # manual 10 days ago
        s = self.tr.summary()
        self.assertEqual(s["today"]["auto_min"], 10)
        self.assertEqual(s["today"]["manual_min"], 60)
        self.assertEqual(s["today"]["total_min"], 70)
        self.assertEqual(s["last_7d"]["total_min"], 70)
        self.assertEqual(s["last_30d"]["manual_min"], 90)
        self.assertEqual(len(s["by_day"]), 14)
        self.assertEqual(s["by_day"][-1]["manual_min"], 60)

    def test_empty_summary(self):
        s = self.tr.summary()
        self.assertEqual(s["today"]["total_min"], 0)
        self.assertEqual(s["sessions_count"], 0)


if __name__ == "__main__":
    unittest.main()
