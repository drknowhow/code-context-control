"""Tests for oracle/services/activity_reporter.py — cross-project digest.

Builds temp projects with synthetic .c3 JSONL artifacts and a stub scanner;
no Ollama/keyring needed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from oracle.services.activity_reporter import ActivityReporter


class _StubScanner:
    def __init__(self, projects):
        self._projects = projects

    def discover(self):
        return self._projects


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class TestActivityReporter(unittest.TestCase):
    DAY = "2026-06-14"
    PREV = "2026-06-13"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proj = Path(self.tmp.name) / "projA"
        c3 = self.proj / ".c3"
        c3.mkdir(parents=True)

        _write_jsonl(c3 / "activity_log.jsonl", [
            {"timestamp": f"{self.DAY}T09:00:00+00:00", "type": "tool_call"},
            {"timestamp": f"{self.DAY}T10:00:00+00:00", "type": "tool_call"},
            {"timestamp": f"{self.DAY}T11:00:00+00:00", "type": "tool_call"},
            {"timestamp": f"{self.DAY}T11:30:00+00:00", "type": "decision"},
            {"timestamp": f"{self.PREV}T23:00:00+00:00", "type": "tool_call"},
        ])
        _write_jsonl(c3 / "session_stats.jsonl", [
            {"ts": f"{self.DAY}T09:30:00+00:00", "session_id": "s1",
             "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.12},
            {"ts": f"{self.PREV}T09:30:00+00:00", "session_id": "s0",
             "input_tokens": 999, "output_tokens": 999, "cost_usd": 9.99},
        ])
        sess_dir = c3 / "sessions"
        sess_dir.mkdir(parents=True)
        with open(sess_dir / "session_001.json", "w", encoding="utf-8") as f:
            json.dump({"id": "s1", "started": f"{self.DAY}T09:00:00+00:00",
                       "ended": f"{self.DAY}T12:00:00+00:00", "description": "work",
                       "tool_calls": [1, 2, 3]}, f)
        _write_jsonl(c3 / "edit_ledger.jsonl", [
            {"id": "e1", "timestamp": f"{self.DAY}T09:10:00+00:00", "change_type": "modified", "file": "a.py"},
            {"id": "e2", "timestamp": f"{self.DAY}T09:20:00+00:00", "change_type": "modified", "file": "b.py"},
            {"id": "e3", "timestamp": f"{self.DAY}T09:25:00+00:00", "change_type": "shell_git", "file": "a.py"},
            {"id": "e0", "timestamp": f"{self.PREV}T09:00:00+00:00", "change_type": "modified", "file": "a.py"},
        ])
        self.scanner = _StubScanner([
            {"path": str(self.proj), "name": "projA", "has_c3": True},
        ])
        self.reporter = ActivityReporter(scanner=self.scanner)

    def test_digest_windows_to_the_day(self):
        d = self.reporter.report(date=self.DAY)
        t = d["totals"]
        self.assertEqual(t["projects_active"], 1)
        self.assertEqual(t["tool_calls"], 3)
        self.assertEqual(t["decisions"], 1)
        self.assertEqual(t["edits"], 2)          # modified only, not shell_git
        self.assertEqual(t["git_mutations"], 1)
        self.assertEqual(t["sessions"], 1)
        self.assertEqual(t["input_tokens"], 100)
        self.assertEqual(t["output_tokens"], 50)
        self.assertAlmostEqual(t["cost_usd"], 0.12, places=4)

    def test_prev_day_does_not_leak(self):
        t = self.reporter.report(date=self.PREV)["totals"]
        self.assertEqual(t["tool_calls"], 1)
        self.assertEqual(t["edits"], 1)
        self.assertEqual(t["git_mutations"], 0)
        self.assertAlmostEqual(t["cost_usd"], 9.99, places=4)

    def test_empty_day_has_no_projects(self):
        d = self.reporter.report(date="2025-01-01")
        self.assertEqual(d["totals"]["projects_active"], 0)
        self.assertEqual(d["projects"], [])

    def test_single_project_filter(self):
        d = self.reporter.report(date=self.DAY, project_path=str(self.proj))
        self.assertEqual(d["totals"]["projects_active"], 1)
        self.assertEqual(d["projects"][0]["name"], "projA")

    def test_non_c3_project_skipped_without_side_effect(self):
        ghost = Path(self.tmp.name) / "ghost"
        ghost.mkdir()
        scanner = _StubScanner([
            {"path": str(self.proj), "name": "projA", "has_c3": True},
            {"path": str(ghost), "name": "ghost", "has_c3": False},
        ])
        d = ActivityReporter(scanner=scanner).report(date=self.DAY)
        self.assertFalse((ghost / ".c3").exists())  # no mkdir side effect
        self.assertNotIn("ghost", {p["name"] for p in d["projects"]})

    def test_narrate_without_bridge_records_error(self):
        d = self.reporter.report(date=self.DAY, narrate=True)
        self.assertIsNone(d["narrative"])
        self.assertIn("narrative_error", d)

    def test_window_label_marks_today(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        d = self.reporter.report(date=today)
        self.assertIn("today", d["window"]["label"])

    def test_not_truncated_under_cap(self):
        d = self.reporter.report(date=self.DAY)
        self.assertFalse(d["truncated"])
        self.assertFalse(d["projects"][0]["truncated"])

    def test_truncated_flag_when_activity_cap_hit(self):
        # Drop the activity-log cap below the row count so the scan caps out.
        from unittest import mock
        from oracle.services import activity_reporter as ar
        with mock.patch.object(ar, "_CAP_ACTIVITY", 3):
            d = self.reporter.report(date=self.DAY)
        self.assertTrue(d["truncated"])
        self.assertTrue(d["projects"][0]["truncated"])


if __name__ == "__main__":
    unittest.main()
