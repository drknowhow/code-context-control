import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services import schedule_approvals as sa
from services.notifications import EVENT_KINDS

REPO = Path(__file__).resolve().parents[1]
SCHED = {"id": "sch-1", "name": "report", "kind": "skill", "target": {"skill": "/daily_report", "args": ""},
         "provider": "claude", "model": "sonnet", "effort": "medium", "project_path": str(REPO), "cwd": str(REPO)}
RUN = {"run_id": "sr-1"}


class TestApprovals(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = sa.ApprovalStore(Path(self._tmp.name))
        self.notify = patch("services.schedule_approvals.notify")
        self.notified = self.notify.start()

    def tearDown(self):
        self.notify.stop()
        self._tmp.cleanup()

    def test_kind_is_registered(self):
        self.assertIn("schedule", EVENT_KINDS)

    def test_create_notifies_and_dedupes(self):
        row = self.store.create(SCHED, RUN, ttl_s=600)
        self.assertEqual(row["status"], "pending")
        self.assertIn("/daily_report", row["summary"])
        self.assertIn("claude/sonnet", row["summary"])
        kw = self.notified.call_args.kwargs
        self.assertEqual((kw["kind"], kw["ref_id"], kw["severity"]), ("schedule", row["id"], "warning"))
        again = self.store.create(SCHED, {"run_id": "sr-2"}, ttl_s=600)
        self.assertEqual(again["id"], row["id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.pending_for("sch-1")["id"], row["id"])

    def test_decide_approve_and_deny(self):
        row = self.store.create(SCHED, RUN, ttl_s=600)
        done = self.store.decide(row["id"], "approve", decided_by="desk", note="go")
        self.assertEqual((done["status"], done["decided_by"], done["note"]), ("approved", "desk", "go"))
        with self.assertRaises(sa.ApprovalError):
            self.store.decide(row["id"], "deny", decided_by="desk")
        other = self.store.create(dict(SCHED, id="sch-2"), {"run_id": "sr-3"}, ttl_s=600)
        self.assertEqual(self.store.decide(other["id"], "deny", decided_by="hub")["status"], "denied")
        with self.assertRaises(sa.ApprovalError):
            self.store.decide(other["id"], "maybe", decided_by="hub")
        with self.assertRaises(sa.ApprovalError):
            self.store.decide("sa-missing", "approve", decided_by="hub")
        self.assertEqual(self.notified.call_args.kwargs["severity"], "info")

    def test_decide_on_lapsed_row_expires_it(self):
        row = self.store.create(SCHED, RUN, ttl_s=60)
        rows = json.loads(self.store.file.read_text(encoding="utf-8"))
        rows[0]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.file.write_text(json.dumps(rows), encoding="utf-8")
        with self.assertRaises(sa.ApprovalError):
            self.store.decide(row["id"], "approve", decided_by="desk")
        self.assertEqual(self.store.get(row["id"])["status"], "expired")

    def test_sweep_expires_and_prunes(self):
        row = self.store.create(SCHED, RUN, ttl_s=60)
        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        expired = self.store.sweep_expired(later)
        self.assertEqual([r["id"] for r in expired], [row["id"]])
        self.assertEqual(self.store.get(row["id"])["status"], "expired")
        self.assertEqual(self.store.list(status="pending"), [])
        self.assertEqual(self.store.sweep_expired(later + timedelta(days=sa.RETAIN_DECIDED_DAYS + 1)), [])
        self.assertIsNone(self.store.get(row["id"]))

    def test_list_filters(self):
        self.store.create(SCHED, RUN, ttl_s=600)
        self.store.create(dict(SCHED, id="sch-2", project_path=str(REPO / "tests")), {"run_id": "sr-9"}, ttl_s=600)
        self.assertEqual(len(self.store.list(project_path=str(REPO / "tests"))), 1)
        self.assertEqual(len(self.store.list(status="pending")), 2)
        self.assertEqual(len(self.store.list(limit=1)), 1)


if __name__ == "__main__":
    unittest.main()
