import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cli.tools import schedule as tool
from services import schedule_approvals as sa
from services import schedule_store as ss

REPO = Path(__file__).resolve().parents[1]


def _finalize(name, args, resp, summ, **kw):
    return resp


class TestScheduleTool(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = ss.ScheduleStore(root)
        self.approvals = sa.ApprovalStore(root)
        self._patches = [patch.object(tool, "_store", return_value=self.store),
                         patch.object(tool, "_approvals", return_value=self.approvals),
                         patch.object(tool, "_defaults", return_value={}),
                         patch("services.schedule_approvals.notify")]
        for p in self._patches:
            p.start()
        self.svc = SimpleNamespace(project_path=str(REPO))

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def call(self, action, **kw):
        return tool.handle_schedule(action, self.svc, _finalize, **kw)

    def test_build_when_and_target(self):
        self.assertEqual(tool.build_when(cron="0 9 * * *", tz="UTC"), {"type": "cron", "expr": "0 9 * * *", "tz": "UTC"})
        self.assertEqual(tool.build_when(preset="weekly", preset_at="08:00", days="mon, fri")["days"], ["mon", "fri"])
        self.assertEqual(tool.build_when(every=600), {"type": "every", "seconds": 600})
        self.assertEqual(tool.build_when(at="2026-10-14T09:00Z")["type"], "at")
        self.assertIsNone(tool.build_when())
        self.assertEqual(tool.build_target("skill", "/daily_report weekly x"), {"skill": "/daily_report", "args": "weekly x"})
        self.assertEqual(tool.build_target("command", "echo hi"), {"cmd": "echo hi"})
        self.assertEqual(tool.build_target("prompt", "hello"), {"prompt": "hello"})

    def test_create_defaults_to_approval_and_project(self):
        out = self.call("create", name="rep", target="/daily_report", preset="weekdays")
        self.assertIn("[schedule:created] sch-", out)
        self.assertIn("asks for approval", out)
        row = self.store.list()[0]
        self.assertEqual((row["autonomy"], row["project_path"], row["when"]["expr"]),
                         ("approval", str(REPO), "0 9 * * 1-5"))

    def test_create_requires_a_cadence_and_validates(self):
        self.assertIn("needs one of cron", self.call("create", name="x", target="/x"))
        self.assertIn("[schedule:error]", self.call("create", name="x", target="/x", every=600, effort="ultra"))
        self.assertIn("[schedule:error]", self.call("create", name="x", target="/x", every=600,
                                                    permission_mode="bypassPermissions"))

    def test_list_get_update_toggle_delete(self):
        self.call("create", name="rep", target="/daily_report", every=3600, autonomy="autonomous")
        sid = self.store.list()[0]["id"]
        self.assertIn("rep", self.call("list"))
        self.assertIn('"id"', self.call("get", schedule_id=sid))
        out = self.call("update", schedule_id=sid, model="haiku", cron="0 8 * * *")
        self.assertIn("fields=['model', 'when']", out)
        self.assertEqual(self.store.get(sid)["model"], "haiku")
        self.assertIn("nothing to change", self.call("update", schedule_id=sid))
        self.assertIn("disabled", self.call("disable", schedule_id=sid))
        self.assertIsNone(self.store.get(sid)["next_run_at"])
        self.assertIn("enabled", self.call("enable", schedule_id=sid))
        self.assertIn("[schedule:deleted]", self.call("delete", schedule_id=sid))
        self.assertIn("[schedule:error]", self.call("delete", schedule_id=sid))

    def test_run_now_runs_and_approvals(self):
        self.call("create", name="rep", target="/daily_report", every=3600)
        sid = self.store.list()[0]["id"]
        out = self.call("run_now", schedule_id=sid)
        self.assertIn("[schedule:queued] run sr-", out)
        self.assertIn("needs a human approval", out)
        self.assertIn("claimed", self.call("runs"))
        self.assertIn("[schedule:runs] no runs", self.call("runs", status="done"))
        self.assertIn("none pending", self.call("approvals"))
        run = self.store.runs()[0]
        self.approvals.create(self.store.get(sid), run, ttl_s=600)
        out = self.call("approvals")
        self.assertIn("decide from the Hub", out)
        self.assertIn("sa-", out)
        self.assertIn("pending_approvals=1", self.call("status"))

    def test_preview_and_bad_action(self):
        out = self.call("preview", cron="0 9 * * *", tz="UTC", limit=2)
        self.assertEqual(out.count("T09:00:00+00:00"), 2)
        self.assertIn("action must be one of", self.call("decide"))

    def test_read_actions_are_declared(self):
        self.assertTrue(tool.READ_ACTIONS <= set(tool.ACTIONS))
        self.assertNotIn("decide", tool.ACTIONS)


if __name__ == "__main__":
    unittest.main()
