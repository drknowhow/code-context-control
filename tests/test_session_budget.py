import json
import tempfile
import unittest
from pathlib import Path

from services.session_manager import SessionManager


class TestSessionBudget(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.project_path = Path(self.test_dir.name)
        (self.project_path / ".c3").mkdir()
        config = {
            "context_budget": {
                "threshold": 200
            }
        }
        with open(self.project_path / ".c3" / "config.json", "w", encoding='utf-8') as f:
            json.dump(config, f)
        self.sm = SessionManager(str(self.project_path))
        self.sm.start_session("Test Session")

    def tearDown(self):
        self.test_dir.cleanup()

    def test_budget_tracking(self):
        """Token counter increments on track_response."""
        self.sm.track_response("test_tool", "word " * 50)
        snap = self.sm.get_budget_snapshot()
        self.assertGreater(snap["response_tokens"], 0)
        self.assertEqual(snap["call_count"], 1)

    def test_budget_over_threshold(self):
        """is_over_budget returns True when tokens exceed threshold."""
        self.sm.track_response("test_tool", "word " * 300)
        self.assertTrue(self.sm.is_over_budget())

    def test_budget_under_threshold(self):
        """is_over_budget returns False when tokens below threshold."""
        self.sm.track_response("test_tool", "hello")
        self.assertFalse(self.sm.is_over_budget())

    def test_budget_reset(self):
        """reset_budget zeroes counters."""
        self.sm.track_response("test_tool", "word " * 300)
        self.sm.reset_budget()
        snap = self.sm.get_budget_snapshot()
        self.assertEqual(snap["response_tokens"], 0)
        self.assertEqual(snap["call_count"], 0)
        self.assertFalse(self.sm.is_over_budget())

    def test_nudge_when_over(self):
        """get_context_nudge returns nudge text when over threshold."""
        self.sm.track_response("test_tool", "word " * 300)
        nudge = self.sm.get_context_nudge()
        self.assertIn("ctx:", nudge)
        self.assertIn("compact", nudge)

    def test_no_nudge_when_under(self):
        """get_context_nudge returns empty when under threshold."""
        self.sm.track_response("test_tool", "hello")
        nudge = self.sm.get_context_nudge()
        self.assertEqual(nudge, "")

    def test_by_tool_tracking(self):
        """Per-tool token breakdown is tracked."""
        self.sm.track_response("c3_search", "word " * 50)
        self.sm.track_response("c3_compress", "word " * 30)
        snap = self.sm.get_budget_snapshot()
        self.assertIn("c3_search", snap["by_tool"])
        self.assertIn("c3_compress", snap["by_tool"])

if __name__ == "__main__":
    unittest.main()
