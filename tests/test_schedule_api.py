import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import scheduler as sch  # noqa: E402
from services import schedule_approvals as sa  # noqa: E402
from services import schedule_store as ss  # noqa: E402

AUTH = {"Authorization": "Bearer schedule-api-key"}


class _StubScanner:
    def __init__(self, paths):
        self.paths = paths

    def discover(self, force=False):
        return [{"path": str(p), "name": p.name, "has_c3": True} for p in self.paths]


class _NoJobs:
    def start(self, **kw):
        raise AssertionError("tick must not run inside a request")

    def find(self, job_id):
        return None

    def reap(self, **kw):
        return []


class TestScheduleApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="c3-sched-api-"))
        cls.alpha = cls.tmp / "alpha"
        (cls.alpha / ".c3").mkdir(parents=True)
        cls.outsider = cls.tmp / "outsider"
        (cls.outsider / ".c3").mkdir(parents=True)
        cls._env = mock.patch.dict(os.environ, {"C3_ORACLE_API_KEY": "schedule-api-key"})
        cls._env.start()
        cls._prior = (srv._cfg, srv._scanner, srv._schedule_store, srv._approval_store, srv._scheduler)
        srv._cfg = {"scheduler_approval_ttl_s": 600}
        srv._scanner = _StubScanner([cls.alpha])
        srv._schedule_store = ss.ScheduleStore(cls.tmp / "oracle")
        srv._approval_store = sa.ApprovalStore(cls.tmp / "oracle")
        srv._scheduler = sch.Scheduler(store=srv._schedule_store, approvals=srv._approval_store, jobstore=_NoJobs(),
                                       config=lambda: srv._cfg)
        cls.client = srv.app.test_client()

    @classmethod
    def tearDownClass(cls):
        srv._cfg, srv._scanner, srv._schedule_store, srv._approval_store, srv._scheduler = cls._prior
        cls._env.stop()

    def body(self, **over):
        base = {"name": "report", "kind": "skill", "target": {"skill": "/daily_report"},
                "project_path": str(self.alpha), "when": {"type": "every", "seconds": 3600},
                "autonomy": "autonomous"}
        base.update(over)
        return base

    def create(self, **over):
        resp = self.client.post("/api/schedules", json=self.body(**over), headers=AUTH)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        return resp.get_json()

    def test_mutations_need_auth_and_reads_do_not(self):
        self.assertEqual(self.client.post("/api/schedules", json=self.body()).status_code, 401)
        self.assertEqual(self.client.get("/api/schedules").status_code, 200)
        self.assertEqual(self.client.get("/api/schedules/status").status_code, 200)

    def test_crud_roundtrip(self):
        row = self.create(name="crud")
        self.assertEqual(row["project_path"], str(self.alpha.resolve()))
        listed = self.client.get(f"/api/schedules?project={self.alpha}").get_json()
        self.assertIn(row["id"], [r["id"] for r in listed["schedules"]])
        one = self.client.get(f"/api/schedules/{row['id']}").get_json()
        self.assertEqual(one["name"], "crud")
        rev = one["rev"]
        patched = self.client.patch(f"/api/schedules/{row['id']}", json={"name": "crud2", "expected_rev": rev},
                                    headers=AUTH)
        self.assertEqual(patched.status_code, 200)
        stale = self.client.patch(f"/api/schedules/{row['id']}", json={"name": "x", "expected_rev": rev}, headers=AUTH)
        self.assertEqual(stale.status_code, 409)
        bad = self.client.patch(f"/api/schedules/{row['id']}", json={"effort": "ultra"}, headers=AUTH)
        self.assertEqual(bad.status_code, 400)
        off = self.client.post(f"/api/schedules/{row['id']}/disable", headers=AUTH).get_json()
        self.assertFalse(off["enabled"])
        self.assertIsNone(off["next_run_at"])
        on = self.client.post(f"/api/schedules/{row['id']}/enable", headers=AUTH).get_json()
        self.assertIsNotNone(on["next_run_at"])
        self.assertEqual(self.client.delete(f"/api/schedules/{row['id']}", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.get(f"/api/schedules/{row['id']}").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/schedules/{row['id']}", headers=AUTH).status_code, 404)

    def test_unregistered_project_is_404_and_validation_400(self):
        resp = self.client.post("/api/schedules", json=self.body(project_path=str(self.outsider)), headers=AUTH)
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post("/api/schedules", json=self.body(when={"type": "cron", "expr": "nope"}), headers=AUTH)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("cron", resp.get_json()["error"])

    def test_run_now_and_runs(self):
        row = self.create(name="runme")
        resp = self.client.post(f"/api/schedules/{row['id']}/run", json={}, headers=AUTH)
        self.assertEqual(resp.status_code, 202)
        run = resp.get_json()
        self.assertEqual((run["status"], run["trigger"]), ("claimed", "manual"))
        runs = self.client.get(f"/api/schedules/{row['id']}/runs").get_json()
        self.assertEqual([r["run_id"] for r in runs["runs"]], [run["run_id"]])
        self.assertEqual(self.client.get(f"/api/schedules/runs/{run['run_id']}").get_json()["schedule_id"], row["id"])
        self.assertEqual(self.client.get("/api/schedules/runs?status=bogus").status_code, 400)
        self.assertEqual(self.client.post("/api/schedules/sch-none/run", json={}, headers=AUTH).status_code, 404)
        bypass = self.client.post(f"/api/schedules/{row['id']}/run", json={"bypass_approval": True}, headers=AUTH)
        self.assertEqual(bypass.get_json()["status"], "approved")

    def test_approvals_decide(self):
        row = self.create(name="gated", autonomy="approval")
        run = srv._schedule_store.request_run(row["id"])
        with mock.patch("services.schedule_approvals.notify"):
            pending = srv._approval_store.create(row, run, ttl_s=600)
            listed = self.client.get("/api/schedules/approvals?status=pending").get_json()
            self.assertIn(pending["id"], [a["id"] for a in listed["approvals"]])
            self.assertEqual(self.client.post(f"/api/schedules/approvals/{pending['id']}", json={"decision": "later"},
                                              headers=AUTH).status_code, 400)
            ok = self.client.post(f"/api/schedules/approvals/{pending['id']}", json={"decision": "approve"},
                                  headers=AUTH)
            self.assertEqual(ok.status_code, 200)
            self.assertEqual((ok.get_json()["status"], ok.get_json()["decided_by"]), ("approved", "oracle"))
            again = self.client.post(f"/api/schedules/approvals/{pending['id']}", json={"decision": "deny"},
                                     headers=AUTH)
            self.assertEqual(again.status_code, 409)
        self.assertEqual(self.client.post("/api/schedules/approvals/sa-none", json={"decision": "approve"},
                                          headers=AUTH).status_code, 404)

    def test_preview(self):
        resp = self.client.post("/api/schedules/preview",
                                json={"when": {"type": "cron", "expr": "0 9 * * *", "tz": "UTC"}, "count": 3},
                                headers=AUTH)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["next"]), 3)
        self.assertTrue(all(t.endswith("09:00:00+00:00") for t in data["next"]))
        self.assertEqual(self.client.post("/api/schedules/preview", json={"when": {"type": "x"}},
                                          headers=AUTH).status_code, 400)

    def test_status_and_kick(self):
        status = self.client.get("/api/schedules/status").get_json()
        self.assertIn("pending_approvals", status)
        self.assertEqual(self.client.post("/api/schedules/kick", headers=AUTH).get_json(), {"kicked": True})
        self.assertEqual(json.loads(json.dumps(status))["running"], False)


if __name__ == "__main__":
    unittest.main()
