import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services import schedule_store as ss

REPO = Path(__file__).resolve().parents[1]


def fields(**over):
    base = {
        "name": "daily report", "kind": "skill", "target": {"skill": "/daily_report"},
        "project_path": str(REPO), "when": {"type": "every", "seconds": 3600},
        "autonomy": "autonomous",
    }
    base.update(over)
    return base


class StoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "oracle"
        self.store = ss.ScheduleStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()


class TestNormalize(StoreBase):
    def test_defaults_and_shape(self):
        row = self.store.create(fields(), created_by="test")
        self.assertTrue(row["id"].startswith("sch-"))
        self.assertEqual(row["provider"], "claude")
        self.assertEqual(row["permission_mode"], "acceptEdits")
        self.assertEqual(row["timeout_s"], ss.DEFAULT_TIMEOUT_S)
        self.assertEqual(row["misfire"], {"policy": "grace", "grace_s": ss.DEFAULT_MISFIRE_GRACE_S})
        self.assertEqual(row["cwd"], str(REPO))
        self.assertIsNotNone(row["next_run_at"])
        self.assertEqual(row["run_count"], 0)
        self.assertEqual(self.store.rev(), 1)

    def test_agent_default_is_approval(self):
        row = self.store.create(fields(autonomy=None))
        self.assertEqual(row["autonomy"], "approval")

    def test_rejections(self):
        cases = [
            fields(name=""), fields(kind="cron"), fields(target={"skill": "daily"}),
            fields(target={"skill": "/a b"}), fields(kind="prompt", target={}),
            fields(kind="command", target={"cmd": ""}), fields(provider="gpt"),
            fields(effort="ultra"), fields(permission_mode="yolo"),
            fields(permission_mode="bypassPermissions"), fields(autonomy="maybe"),
            fields(overlap="queue"), fields(project_path=""), fields(timeout_s=5),
            fields(misfire={"policy": "retry"}), fields(max_budget_usd=-1),
            fields(when={"type": "cron", "expr": "bad"}), fields(nonsense=1),
        ]
        for case in cases:
            with self.assertRaises(ss.ScheduleError, msg=json.dumps(case)):
                self.store.create(case)

    def test_bypass_needs_the_switch(self):
        row = self.store.create(fields(permission_mode="bypassPermissions"),
                                defaults={"allow_bypass_permissions": True})
        self.assertEqual(row["permission_mode"], "bypassPermissions")

    def test_defaults_argument_fills_omitted_fields(self):
        row = self.store.create(fields(), defaults={"timeout_s": 300, "misfire_grace_s": 60,
                                                    "approval_ttl_s": 120, "max_budget_usd": 1.5})
        self.assertEqual((row["timeout_s"], row["misfire"]["grace_s"], row["approval_ttl_s"],
                          row["max_budget_usd"]), (300, 60, 120, 1.5))

    def test_prompt_text(self):
        self.assertEqual(ss.prompt_text({"kind": "skill", "target": {"skill": "/x", "args": "a b"}}), "/x a b")
        self.assertEqual(ss.prompt_text({"kind": "prompt", "target": {"prompt": "hi"}}), "hi")


class TestCrud(StoreBase):
    def test_update_recomputes_next_run_only_when_needed(self):
        row = self.store.create(fields())
        first = row["next_run_at"]
        same = self.store.update(row["id"], {"name": "renamed"})
        self.assertEqual(same["next_run_at"], first)
        moved = self.store.update(row["id"], {"when": {"type": "every", "seconds": 120}})
        self.assertNotEqual(moved["next_run_at"], first)
        off = self.store.set_enabled(row["id"], False)
        self.assertIsNone(off["next_run_at"])
        on = self.store.set_enabled(row["id"], True)
        self.assertIsNotNone(on["next_run_at"])

    def test_rev_conflict(self):
        row = self.store.create(fields())
        rev = self.store.rev()
        self.store.update(row["id"], {"name": "a"})
        with self.assertRaises(ss.RevConflict):
            self.store.update(row["id"], {"name": "b"}, expected_rev=rev)
        self.assertEqual(self.store.get(row["id"])["name"], "a")

    def test_delete_and_list_filters(self):
        a = self.store.create(fields(name="a"))
        self.store.create(fields(name="b", enabled=False, project_path=str(REPO / "tests")))
        self.assertEqual(len(self.store.list()), 2)
        self.assertEqual([r["name"] for r in self.store.list(enabled=True)], ["a"])
        self.assertEqual([r["name"] for r in self.store.list(project_path=str(REPO / "tests"))], ["b"])
        self.assertTrue(self.store.delete(a["id"]))
        self.assertFalse(self.store.delete(a["id"]))
        self.assertIsNone(self.store.get(a["id"]))

    def test_corrupt_document_reads_as_empty(self):
        self.store.create(fields())
        self.store.schedules_file.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.list(), [])

    def test_events_are_appended(self):
        row = self.store.create(fields())
        self.store.delete(row["id"])
        lines = self.store.events_file.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(l)["event"] for l in lines], ["schedule.created", "schedule.deleted"])


class TestClaim(StoreBase):
    def _due(self, **over):
        row = self.store.create(fields(**over))
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with self.store._txn() as doc:
            ss._find(doc, row["id"])["next_run_at"] = past
        return self.store.get(row["id"])

    def test_claim_marks_next_run_before_returning(self):
        row = self._due()
        claimed = self.store.claim_due()
        self.assertEqual(len(claimed), 1)
        sched, run = claimed[0]
        self.assertEqual(run["status"], "claimed")
        self.assertEqual(run["due_at"], row["next_run_at"])
        self.assertGreater(sched["next_run_at"], datetime.now(timezone.utc).isoformat())
        self.assertEqual(self.store.claim_due(), [])
        self.assertEqual(self.store.get_run(run["run_id"])["schedule_id"], row["id"])

    def test_disabled_and_future_rows_are_not_claimed(self):
        self._due(enabled=False)
        self.store.create(fields())
        self.assertEqual(self.store.claim_due(), [])

    def test_one_shot_disables_at_claim(self):
        when = {"type": "at", "when": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}
        row = self.store.create(fields(when=when))
        with self.store._txn() as doc:
            ss._find(doc, row["id"])["next_run_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.assertEqual(len(self.store.claim_due()), 1)
        after = self.store.get(row["id"])
        self.assertFalse(after["enabled"])
        self.assertIsNone(after["next_run_at"])

    def test_concurrent_threads_claim_once(self):
        self._due()
        results = []
        barrier = threading.Barrier(4)

        def worker():
            store = ss.ScheduleStore(self.root)
            barrier.wait()
            results.append(len(store.claim_due()))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 1)
        self.assertEqual(len(self.store.runs()), 1)

    def test_concurrent_processes_claim_once(self):
        self._due()
        code = ("import sys; from services.schedule_store import ScheduleStore; "
                f"print(len(ScheduleStore({str(self.root)!r}).claim_due()))")
        env = dict(os.environ, PYTHONPATH=str(REPO))
        procs = [subprocess.Popen([sys.executable, "-c", code], cwd=str(REPO), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(3)]
        outs = [p.communicate(timeout=60) for p in procs]
        for p, (out, err) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, err)
        self.assertEqual(sum(int(out.strip()) for out, _ in outs), 1)

    def test_request_run_and_open_run_queries(self):
        row = self.store.create(fields())
        run = self.store.request_run(row["id"], by="hub")
        self.assertEqual(run["trigger"], "manual")
        self.assertEqual(self.store.open_run_for(row["id"])["run_id"], run["run_id"])
        self.assertIsNone(self.store.open_run_for(row["id"], exclude=run["run_id"]))
        with self.assertRaises(ss.ScheduleError):
            self.store.request_run("sch-nope")


class TestRuns(StoreBase):
    def test_update_and_finish(self):
        row = self.store.create(fields())
        run = self.store.request_run(row["id"])
        self.store.update_run(run["run_id"], status="dispatched", job_id="j1")
        with self.assertRaises(ss.ScheduleError):
            self.store.update_run(run["run_id"], status="exploded")
        with self.assertRaises(ss.ScheduleError):
            self.store.update_run(run["run_id"], bogus=1)
        with self.assertRaises(ss.ScheduleError):
            self.store.finish_run(run["run_id"], "running")
        done = self.store.finish_run(run["run_id"], "done", exit_code=0, cost_usd=0.12,
                                     result_excerpt="x" * 5000)
        self.assertEqual(len(done["result_excerpt"]), ss.RESULT_EXCERPT_CAP)
        sched = self.store.get(row["id"])
        self.assertEqual((sched["last_status"], sched["last_run_id"], sched["run_count"]),
                         ("done", run["run_id"], 1))
        skipped = self.store.request_run(row["id"])
        self.store.finish_run(skipped["run_id"], "missed")
        self.assertEqual(self.store.get(row["id"])["run_count"], 1)

    def test_runs_filters_and_order(self):
        a = self.store.create(fields(name="a"))
        b = self.store.create(fields(name="b", project_path=str(REPO / "tests")))
        r1 = self.store.request_run(a["id"])
        r2 = self.store.request_run(b["id"])
        self.store.finish_run(r1["run_id"], "done")
        self.assertEqual([r["run_id"] for r in self.store.runs(schedule_id=b["id"])], [r2["run_id"]])
        self.assertEqual([r["run_id"] for r in self.store.runs(status="done")], [r1["run_id"]])
        self.assertEqual(len(self.store.runs(project_path=str(REPO / "tests"))), 1)
        self.assertEqual(len(self.store.open_runs()), 1)
        self.assertEqual(len(self.store.runs(limit=1)), 1)

    def test_sweep_keeps_open_and_recent(self):
        row = self.store.create(fields())
        old = self.store.request_run(row["id"])
        self.store.finish_run(old["run_id"], "done")
        self.store.update_run(old["run_id"], finished_at=(datetime.now(timezone.utc) - timedelta(days=40)).isoformat())
        fresh = self.store.request_run(row["id"])
        self.store.finish_run(fresh["run_id"], "failed")
        stuck = self.store.request_run(row["id"])
        self.store.update_run(stuck["run_id"], claimed_at=(datetime.now(timezone.utc) - timedelta(days=90)).isoformat())
        self.assertEqual(self.store.sweep_runs(30), 1)
        left = {r["run_id"] for r in self.store.runs()}
        self.assertEqual(left, {fresh["run_id"], stuck["run_id"]})


if __name__ == "__main__":
    unittest.main()
