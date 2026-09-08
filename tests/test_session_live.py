"""Session heartbeat files (v2.128.0) — .c3/live/<session>.json.

Liveness used to be inferred from activity rows: a closed IDE read "live" for
up to 20 minutes (nothing consumed the session_end row), and an open-but-quiet
session read dead after 20 minutes of no tool calls. These pin the file
contract the MCP server writes and the hub reads.
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import session_live  # noqa: E402


class TestSessionLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _age(self, session_id: str, seconds: float):
        """Backdate one heartbeat without waiting for the clock."""
        path = session_live.heartbeat_path(self.project, session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["ts"] = time.time() - seconds
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_beat_then_read_back(self):
        self.assertTrue(session_live.beat(
            self.project, "s1", host_session_id="host-1", ide="claude-code"))
        live = session_live.live_sessions(self.project)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["session_id"], "s1")
        self.assertEqual(live[0]["host_session_id"], "host-1")
        self.assertEqual(live[0]["ide"], "claude-code")
        self.assertIsInstance(live[0]["pid"], int)
        self.assertLess(live[0]["age_seconds"], 5)

    def test_empty_session_id_is_refused(self):
        self.assertFalse(session_live.beat(self.project, ""))
        self.assertEqual(session_live.live_sessions(self.project), [])

    def test_no_live_dir_is_not_an_error(self):
        self.assertEqual(session_live.live_sessions(self.project), [])
        self.assertFalse(session_live.is_live(self.project, "s1"))

    def test_clear_removes_the_file(self):
        session_live.beat(self.project, "s1")
        self.assertTrue(session_live.clear(self.project, "s1"))
        self.assertEqual(session_live.live_sessions(self.project), [])
        # Second clear is a no-op, not a crash (teardown can run twice).
        self.assertFalse(session_live.clear(self.project, "s1"))

    def test_stale_heartbeat_is_not_live_and_is_pruned(self):
        session_live.beat(self.project, "s1")
        self._age("s1", session_live.HEARTBEAT_TTL_S + 60)
        self.assertEqual(session_live.live_sessions(self.project), [])
        self.assertFalse(session_live.heartbeat_path(self.project, "s1").exists())

    def test_stale_heartbeat_survives_prune_false(self):
        session_live.beat(self.project, "s1")
        self._age("s1", session_live.HEARTBEAT_TTL_S + 60)
        self.assertEqual(session_live.live_sessions(self.project, prune=False), [])
        self.assertTrue(session_live.heartbeat_path(self.project, "s1").exists())

    def test_just_inside_the_ttl_is_live(self):
        session_live.beat(self.project, "s1")
        self._age("s1", session_live.HEARTBEAT_TTL_S - 5)
        self.assertEqual(len(session_live.live_sessions(self.project)), 1)

    def test_idle_session_stays_live(self):
        """The whole point: no tool call for hours, still live."""
        session_live.beat(self.project, "s1")
        self._age("s1", 30)  # last beat 30 s ago, last tool call irrelevant
        self.assertTrue(session_live.is_live(self.project, "s1"))

    def test_two_sessions_are_two_entries_newest_first(self):
        session_live.beat(self.project, "older")
        session_live.beat(self.project, "newer")
        self._age("older", 100)
        live = session_live.live_sessions(self.project)
        self.assertEqual([s["session_id"] for s in live], ["newer", "older"])

    def test_is_live_matches_either_id(self):
        session_live.beat(self.project, "c3-id", host_session_id="host-id")
        self.assertTrue(session_live.is_live(self.project, "c3-id"))
        self.assertTrue(session_live.is_live(self.project, "host-id"))
        self.assertFalse(session_live.is_live(self.project, "someone-else"))
        self.assertFalse(session_live.is_live(self.project, ""))

    def test_unsafe_session_id_stays_inside_live_dir(self):
        session_live.beat(self.project, "../../escape")
        files = list(session_live.live_dir(self.project).iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].parent, session_live.live_dir(self.project))

    def test_unreadable_file_is_ignored_not_fatal(self):
        session_live.beat(self.project, "s1")
        session_live.heartbeat_path(self.project, "s1").write_text(
            "{half-written", encoding="utf-8")
        self.assertEqual(session_live.live_sessions(self.project), [])

    def test_concurrent_beats_never_corrupt(self):
        """Six threads on one path: the Windows failure mode atomic_json exists for."""
        errors: list = []

        def hammer():
            try:
                for _ in range(15):
                    session_live.beat(self.project, "shared")
                    session_live.live_sessions(self.project, prune=False)
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        live = session_live.live_sessions(self.project)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["session_id"], "shared")
        # No abandoned temp files left beside it.
        self.assertEqual(
            [p.name for p in session_live.live_dir(self.project).iterdir()],
            ["shared.json"])


if __name__ == "__main__":
    unittest.main()
