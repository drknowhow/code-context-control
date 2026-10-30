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
from oracle.services import mobile_api, schedule_api  # noqa: E402
from services import schedule_approvals as sa  # noqa: E402
from services import schedule_store as ss  # noqa: E402

AUTH = {"Authorization": "Bearer mobile-sched-key"}


class _StubScanner:
    def __init__(self, paths):
        self.paths = paths

    def discover(self, force=False):
        return [{"path": str(p), "name": p.name, "tags": [], "active": False, "has_c3": True, "fact_count": 0}
                for p in self.paths]


class TestMobileSchedules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="c3-mobile-sched-"))
        cls.alpha = cls.tmp / "alpha"
        cls.outsider = cls.tmp / "outsider"
        for p in (cls.alpha, cls.outsider):
            (p / ".c3").mkdir(parents=True)
        cls._env = mock.patch.dict(os.environ, {"C3_ORACLE_API_KEY": "mobile-sched-key"})
        cls._env.start()
        cls._prior_cfg = srv._cfg
        srv._cfg = {"mobile_api_enabled": True, "api_rate_limit_per_min": 0,
                    "mobile_security_rate_limit_per_min": 0}
        mobile_api.init_services(scanner=_StubScanner([cls.alpha]))
        cls.store = ss.ScheduleStore(cls.tmp / "oracle")
        cls.approvals = sa.ApprovalStore(cls.tmp / "oracle")
        cls._prior_api = dict(get_scheduler=schedule_api._get_scheduler, get_store=schedule_api._get_store,
                              get_approvals=schedule_api._get_approvals, get_scanner=schedule_api._get_scanner,
                              get_cfg=schedule_api._get_cfg)
        schedule_api.configure(get_scheduler=lambda: None, get_store=lambda: cls.store,
                               get_approvals=lambda: cls.approvals, get_scanner=lambda: mobile_api._scanner,
                               get_cfg=lambda: srv._cfg)
        cls.client = srv.app.test_client()
        cls.notify = mock.patch("services.schedule_approvals.notify")
        cls.notify.start()

    @classmethod
    def tearDownClass(cls):
        cls.notify.stop()
        schedule_api.configure(**cls._prior_api)
        srv._cfg = cls._prior_cfg
        cls._env.stop()

    def body(self, **over):
        base = {"name": "report", "kind": "skill", "target": {"skill": "/daily_report"},
                "project_path": str(self.alpha), "when": {"type": "every", "seconds": 3600},
                "autonomy": "autonomous"}
        base.update(over)
        return base

    def test_info_advertises_and_switch_hides(self):
        info = self.client.get("/api/mobile/info", headers=AUTH).get_json()
        self.assertGreaterEqual(info["api_version"], 6)
        self.assertIn("schedules", info["capabilities"])
        self.assertIn("schedules_write", info["capabilities"])
        with mock.patch.dict(srv._cfg, {"mobile_schedules_enabled": False}):
            info = self.client.get("/api/mobile/info", headers=AUTH).get_json()
            self.assertNotIn("schedules", info["capabilities"])
            self.assertEqual(self.client.get("/api/mobile/schedules", headers=AUTH).status_code, 404)
        with mock.patch.dict(srv._cfg, {"mobile_schedules_write": False}):
            self.assertEqual(self.client.post("/api/mobile/schedules", json=self.body(), headers=AUTH).status_code, 404)
            self.assertEqual(self.client.get("/api/mobile/schedules", headers=AUTH).status_code, 200)

    def test_bearer_required_on_get(self):
        self.assertEqual(self.client.get("/api/mobile/schedules").status_code, 401)

    def test_create_list_update_run_delete(self):
        resp = self.client.post("/api/mobile/schedules", json=self.body(name="m1"), headers=AUTH)
        self.assertEqual(resp.status_code, 201, resp.get_json())
        row = resp.get_json()
        listed = self.client.get("/api/mobile/schedules", headers=AUTH).get_json()
        self.assertIn(row["id"], [r["id"] for r in listed["schedules"]])
        one = self.client.get(f"/api/mobile/schedules/{row['id']}", headers=AUTH).get_json()
        self.assertEqual(one["name"], "m1")
        patched = self.client.patch(f"/api/mobile/schedules/{row['id']}", json={"name": "m2"}, headers=AUTH)
        self.assertEqual(patched.get_json()["name"], "m2")
        off = self.client.post(f"/api/mobile/schedules/{row['id']}/disable", headers=AUTH).get_json()
        self.assertFalse(off["enabled"])
        self.assertEqual(self.client.post(f"/api/mobile/schedules/{row['id']}/explode", headers=AUTH).status_code, 404)
        run = self.client.post(f"/api/mobile/schedules/{row['id']}/run", json={}, headers=AUTH)
        self.assertEqual(run.status_code, 202)
        runs = self.client.get(f"/api/mobile/schedules/runs?schedule_id={row['id']}", headers=AUTH).get_json()
        self.assertEqual([r["run_id"] for r in runs["runs"]], [run.get_json()["run_id"]])
        self.assertEqual(self.client.delete(f"/api/mobile/schedules/{row['id']}", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.get(f"/api/mobile/schedules/{row['id']}", headers=AUTH).status_code, 404)

    def test_unregistered_project_rows_are_hidden(self):
        resp = self.client.post("/api/mobile/schedules", json=self.body(project_path=str(self.outsider)), headers=AUTH)
        self.assertEqual(resp.status_code, 404)
        hidden = self.store.create(self.body(name="hidden", project_path=str(self.outsider)))
        ids = [r["id"] for r in self.client.get("/api/mobile/schedules", headers=AUTH).get_json()["schedules"]]
        self.assertNotIn(hidden["id"], ids)
        self.assertEqual(self.client.get(f"/api/mobile/schedules/{hidden['id']}", headers=AUTH).status_code, 404)
        self.assertEqual(self.client.post(f"/api/mobile/schedules/{hidden['id']}/run", json={},
                                          headers=AUTH).status_code, 404)
        run = self.store.request_run(hidden["id"])
        runs = self.client.get("/api/mobile/schedules/runs", headers=AUTH).get_json()
        self.assertNotIn(run["run_id"], [r["run_id"] for r in runs["runs"]])
        self.assertEqual(self.client.get("/api/mobile/schedules?project=" + str(self.outsider),
                                         headers=AUTH).status_code, 404)

    def archived_run(self, **over):
        run = self.store.request_run(self.store.create(self.body(**over))["id"])
        (self.store.runs_dir / f"{run['run_id']}.out").write_bytes(b"one\ntwo\n")
        self.store.update_run(run["run_id"], output_lines=2, output_dropped=0)
        return run

    def test_run_output_needs_bearer_and_pages_the_archive(self):
        run = self.archived_run(name="out")
        url = f"/api/mobile/schedules/runs/{run['run_id']}/output"
        self.assertEqual(self.client.get(url).status_code, 401)
        resp = self.client.get(url + "?after=1", headers=AUTH)
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertEqual((body["run_id"], body["source"], body["format"], body["open"]),
                         (run["run_id"], "archive", "claude-stream", True))
        self.assertEqual((body["lines"], body["next"], body["total"], body["dropped"]),
                         ([{"n": 2, "text": "two"}], 2, 2, 0))
        self.assertEqual(self.client.get(url + "?limit=x", headers=AUTH).status_code, 400)
        self.assertEqual(self.client.get("/api/mobile/schedules/runs/sr-0000000000/output",
                                         headers=AUTH).status_code, 404)

    def test_run_output_capability_follows_the_schedules_switch(self):
        run = self.archived_run(name="switched")
        url = f"/api/mobile/schedules/runs/{run['run_id']}/output"
        self.assertIn("schedule_output", self.client.get("/api/mobile/info", headers=AUTH).get_json()["capabilities"])
        with mock.patch.dict(srv._cfg, {"mobile_schedules_enabled": False}):
            caps = self.client.get("/api/mobile/info", headers=AUTH).get_json()["capabilities"]
            self.assertNotIn("schedule_output", caps)
            self.assertEqual(self.client.get(url, headers=AUTH).status_code, 404)
        with mock.patch.dict(srv._cfg, {"mobile_schedules_write": False}):
            self.assertEqual(self.client.get(url, headers=AUTH).status_code, 200)

    def test_run_output_of_an_unserved_project_is_404(self):
        run = self.archived_run(name="hidden-out", project_path=str(self.outsider))
        resp = self.client.get(f"/api/mobile/schedules/runs/{run['run_id']}/output", headers=AUTH)
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("two", resp.get_data(as_text=True))

    def test_approvals_decide_and_409(self):
        row = self.store.create(self.body(name="gated", autonomy="approval"))
        run = self.store.request_run(row["id"])
        pending = self.approvals.create(row, run, ttl_s=600)
        listed = self.client.get("/api/mobile/schedules/approvals?status=pending", headers=AUTH).get_json()
        self.assertIn(pending["id"], [a["id"] for a in listed["approvals"]])
        ok = self.client.post(f"/api/mobile/schedules/approvals/{pending['id']}/decide",
                              json={"decision": "approve"}, headers=AUTH)
        self.assertEqual(ok.status_code, 200, ok.get_json())
        self.assertEqual((ok.get_json()["status"], ok.get_json()["decided_by"]), ("approved", "mobile"))
        again = self.client.post(f"/api/mobile/schedules/approvals/{pending['id']}/decide",
                                 json={"decision": "deny"}, headers=AUTH)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.client.post("/api/mobile/schedules/approvals/sa-none/decide",
                                          json={"decision": "approve"}, headers=AUTH).status_code, 404)
        foreign = self.store.create(self.body(name="foreign", autonomy="approval", project_path=str(self.outsider)))
        frun = self.store.request_run(foreign["id"])
        fpending = self.approvals.create(foreign, frun, ttl_s=600)
        self.assertEqual(self.client.post(f"/api/mobile/schedules/approvals/{fpending['id']}/decide",
                                          json={"decision": "approve"}, headers=AUTH).status_code, 404)
        self.assertEqual(json.loads(json.dumps(self.approvals.get(fpending["id"])))["status"], "pending")


if __name__ == "__main__":
    unittest.main()
