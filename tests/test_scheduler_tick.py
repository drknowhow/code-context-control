import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from oracle.services import scheduler as sch
from services import schedule_approvals as sa
from services import schedule_store as ss

REPO = Path(__file__).resolve().parents[1]
CFG = {"scheduler_enabled": True, "scheduler_run_retention_days": 30, "scheduler_approval_ttl_s": 600}


class FakeJob(SimpleNamespace):
    @property
    def terminal(self):
        return self.status in ("done", "failed", "timeout", "cancelled", "lost")


class FakeJobStore:
    """Jobs finish only when the test says so; start() returns immediately."""

    def __init__(self):
        self.jobs: dict[str, FakeJob] = {}
        self.started: list[dict] = []
        self.output_store = self
        self.block_start = False

    def start(self, **kw):
        if self.block_start:
            time.sleep(5)
        jid = f"j-{len(self.jobs) + 1}"
        job = FakeJob(id=jid, status="running", exit_code=None, error="", created_at="2026-10-13T09:00:00+00:00",
                      output_id="", duration_ms=None, project_path=kw["project_path"], session_id=kw["session_id"],
                      payload=kw["payload"])
        self.jobs[jid] = job
        self.started.append(kw)
        return job

    def finish(self, jid, status="done", exit_code=0):
        job = self.jobs[jid]
        job.status, job.exit_code, job.duration_ms = status, exit_code, 42

    def find(self, job_id):
        return self.jobs.get(job_id)

    def reap(self, **kw):
        return []


def fields(**over):
    base = {"name": "echo", "kind": "command", "target": {"cmd": "echo hi"}, "project_path": str(REPO),
            "when": {"type": "every", "seconds": 3600}, "autonomy": "autonomous"}
    base.update(over)
    return base


class SchedulerBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = ss.ScheduleStore(self.root)
        self.approvals = sa.ApprovalStore(self.root)
        self.jobs = FakeJobStore()
        self.sched = sch.Scheduler(store=self.store, approvals=self.approvals, jobstore=self.jobs,
                                   config=lambda: dict(CFG), interval=30)
        self._patches = [patch("oracle.services.scheduler.notify"), patch("services.schedule_approvals.notify"),
                         patch("oracle.services.scheduler.ActivityLog")]
        self.notify, _, _ = [p.start() for p in self._patches]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.sched.stop()
        self._tmp.cleanup()

    def due(self, **over):
        row = self.store.create(fields(**over))
        with self.store._txn() as doc:
            ss._find(doc, row["id"])["next_run_at"] = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        return self.store.get(row["id"])

    def only_run(self):
        runs = self.store.runs(limit=0)
        self.assertEqual(len(runs), 1)
        return runs[0]


class TestTick(SchedulerBase):
    def test_autonomous_claim_dispatches_without_waiting(self):
        self.due()
        self.jobs.block_start = False
        t0 = time.monotonic()
        summary = self.sched.tick()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual((summary["claimed"], summary["dispatched"]), (1, 1))
        run = self.only_run()
        self.assertEqual((run["status"], run["job_id"]), ("dispatched", "j-1"))
        self.assertEqual(self.jobs.started[0]["payload"]["cmd"], "echo hi")
        self.assertEqual(self.sched.tick()["claimed"], 0)

    def test_finished_job_is_folded_and_announced(self):
        self.due()
        self.sched.tick()
        self.jobs.finish("j-1")
        summary = self.sched.tick()
        self.assertEqual(summary["finished"], 1)
        run = self.only_run()
        self.assertEqual((run["status"], run["exit_code"], run["duration_ms"]), ("done", 0, 42))
        kw = self.notify.call_args.kwargs
        self.assertEqual((kw["kind"], kw["ref_id"], kw["severity"]), ("schedule", run["run_id"], "info"))

    def test_running_job_updates_status_and_missing_job_is_lost(self):
        self.due()
        self.sched.tick()
        self.sched.tick()
        self.assertEqual(self.only_run()["status"], "running")
        self.jobs.jobs.clear()
        self.sched.tick()
        self.assertEqual(self.only_run()["status"], "lost")

    def test_overlap_skip(self):
        row = self.due()
        self.sched.tick()
        self.store.request_run(row["id"])
        self.sched.tick()
        statuses = sorted(r["status"] for r in self.store.runs(limit=0))
        self.assertEqual(statuses, ["running", "skipped_overlap"])

    def test_overlap_allow_dispatches_both(self):
        row = self.due(overlap="allow")
        self.sched.tick()
        self.store.request_run(row["id"])
        self.sched.tick()
        self.assertEqual(sorted(r["status"] for r in self.store.runs(limit=0)), ["dispatched", "running"])

    def test_misfire_grace_and_skip(self):
        late = self.due(misfire={"policy": "grace", "grace_s": 60})
        with self.store._txn() as doc:
            ss._find(doc, late["id"])["next_run_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.sched.tick()
        self.assertEqual(self.only_run()["status"], "missed")
        self.assertEqual(self.store.get(late["id"])["run_count"], 0)
        within = self.due(name="fresh", misfire={"policy": "skip"})
        self.sched.tick()
        fresh = [r for r in self.store.runs(limit=0) if r["schedule_id"] == within["id"]][0]
        self.assertEqual(fresh["status"], "dispatched")

    def test_check_command_gates_the_run(self):
        self.due(check_command="test -s inbox")
        self.sched.tick()
        run = self.only_run()
        self.assertEqual(run["status"], "check_running")
        self.assertEqual(self.jobs.started[0]["payload"]["cmd"], "test -s inbox")
        self.jobs.finish("j-1", exit_code=0)
        self.sched.tick()
        self.assertEqual(self.only_run()["status"], "check_skipped")
        self.due(name="armed", check_command="test -s inbox")
        self.sched.tick()
        armed = [r for r in self.store.runs(limit=0) if r["schedule_name"] == "armed"][0]
        self.jobs.finish(armed["job_id"], status="failed", exit_code=1)
        self.sched.tick()
        armed = self.store.get_run(armed["run_id"])
        self.assertEqual((armed["status"], armed["job_id"]), ("dispatched", "j-3"))
        self.assertEqual(self.jobs.started[-1]["payload"]["cmd"], "echo hi")

    def test_check_command_failure(self):
        self.due(check_command="boom")
        self.sched.tick()
        self.jobs.finish("j-1", status="failed", exit_code=2)
        self.sched.tick()
        run = self.only_run()
        self.assertEqual(run["status"], "failed")
        self.assertIn("check_command", run["error"])

    def test_disabled_config_does_nothing(self):
        self.due()
        self.sched.config = lambda: {"scheduler_enabled": False}
        self.assertTrue(self.sched.tick().get("disabled"))
        self.assertEqual(self.store.runs(limit=0), [])


class TestApprovalFlow(SchedulerBase):
    def test_approval_then_approve_dispatches(self):
        row = self.due(autonomy="approval", approval_ttl_s=600)
        summary = self.sched.tick()
        self.assertEqual(summary["awaiting_approval"], 1)
        run = self.only_run()
        self.assertEqual(run["status"], "awaiting_approval")
        pending = self.approvals.pending_for(row["id"])
        self.assertEqual(pending["run_id"], run["run_id"])
        self.sched.tick()
        self.assertEqual(self.only_run()["status"], "awaiting_approval")
        self.approvals.decide(pending["id"], "approve", decided_by="desk")
        self.sched.tick()
        run = self.only_run()
        self.assertEqual((run["status"], run["job_id"]), ("dispatched", "j-1"))

    def test_deny_and_expire(self):
        row = self.due(autonomy="approval")
        self.sched.tick()
        pending = self.approvals.pending_for(row["id"])
        self.approvals.decide(pending["id"], "deny", decided_by="hub")
        self.sched.tick()
        self.assertEqual(self.only_run()["status"], "denied")
        self.assertEqual(self.jobs.started, [])
        again = self.store.request_run(row["id"])
        self.sched.tick()
        self.assertEqual(self.store.get_run(again["run_id"])["status"], "awaiting_approval")
        later = datetime.now(timezone.utc) + timedelta(days=1)
        summary = self.sched.tick(now=later)
        self.assertEqual(summary["expired"], 1)
        self.assertEqual(self.store.get_run(again["run_id"])["status"], "expired")

    def test_second_pending_for_same_schedule_is_not_queued(self):
        row = self.due(autonomy="approval", overlap="allow")
        self.sched.tick()
        self.store.request_run(row["id"])
        self.sched.tick()
        self.assertEqual(sorted(r["status"] for r in self.store.runs(limit=0)),
                         ["awaiting_approval", "skipped_overlap"])

    def test_run_now_bypass(self):
        row = self.store.create(fields(autonomy="approval"))
        self.sched.run_now(row["id"], bypass_approval=True)
        self.sched.tick()
        self.assertEqual(self.only_run()["status"], "dispatched")
        self.assertEqual(self.approvals.list(), [])


class TestSingleInstance(SchedulerBase):
    def test_second_scheduler_stays_in_standby(self):
        self.due()
        other = sch.Scheduler(store=ss.ScheduleStore(self.root), approvals=sa.ApprovalStore(self.root),
                              jobstore=FakeJobStore(), config=lambda: dict(CFG))
        self.assertFalse(self.sched.tick()["standby"])
        summary = other.tick()
        self.assertTrue(summary["standby"])
        self.assertEqual(len(self.store.runs(limit=0)), 1)
        self.assertTrue(self.sched.status["lock_held"])
        self.sched.stop()
        self.assertFalse(other.tick()["standby"])
        other.stop()

    def test_status_shape(self):
        self.due()
        status = self.sched.status
        self.assertEqual(status["enabled_schedules"], 1)
        self.assertIsNotNone(status["next_due"])
        self.assertFalse(status["running"])


if __name__ == "__main__":
    unittest.main()
