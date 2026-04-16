"""Notification signal discipline.

Regression: get_unacknowledged() once returned all severities, so 18 'info'
events ('File maps updated') drowned 2 real warnings. This locks in that the
default filter is actionable-only; info events go through get_history() or
an explicit severities=() override.
"""
import tempfile
import unittest
from pathlib import Path

from services.notifications import NotificationStore


class TestNotificationDiscipline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir()
        self.store = NotificationStore(str(self.project))

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_returns_actionable_only(self):
        self.store.add("FileMemory", "info", "maps updated", "5 files scanned")
        self.store.add("FileMemory", "info", "File maps updated 2", "5 files")
        self.store.add("IndexStaleness", "warning", "Index is stale", "rebuild")
        self.store.add("ClaudeMdDrift", "critical", "severe drift", "fix now")

        pending = self.store.get_unacknowledged()
        severities = {e["severity"] for e in pending}
        self.assertEqual(severities, {"warning", "critical"},
                         "info events must not leak into default pending view")
        self.assertEqual(len(pending), 2)

    def test_info_count_reported_separately(self):
        self.store.add("FileMemory", "info", "one", "a")
        self.store.add("FileMemory", "info", "two", "b")
        self.store.add("IndexStaleness", "warning", "stale", "x")
        self.assertEqual(self.store.get_suppressed_info_count(), 2)

    def test_explicit_severities_override(self):
        self.store.add("FileMemory", "info", "chatter", "b")
        self.store.add("IndexStaleness", "warning", "real", "x")
        # Empty severities tuple = no filter → all
        all_pending = self.store.get_unacknowledged(severities=())
        self.assertEqual(len(all_pending), 2)
        # Explicit info-only
        info_only = self.store.get_unacknowledged(severities=("info",))
        self.assertEqual(len(info_only), 1)
        self.assertEqual(info_only[0]["severity"], "info")

    def test_history_is_unfiltered(self):
        self.store.add("FileMemory", "info", "chatter", "b")
        self.store.add("IndexStaleness", "warning", "real", "x")
        history = self.store.get_history()
        self.assertEqual(len(history), 2)


if __name__ == "__main__":
    unittest.main()
