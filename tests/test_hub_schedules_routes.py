import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.hub_server as hub_server  # noqa: E402
from oracle.services import schedule_api  # noqa: E402
from services import schedule_approvals as sa  # noqa: E402
from services import schedule_store as ss  # noqa: E402


class TestHubScheduleRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        self.store = ss.ScheduleStore(Path(self._tmp.name) / "oracle")
        self.approvals = sa.ApprovalStore(Path(self._tmp.name) / "oracle")
        self._prior_api = dict(get_scheduler=schedule_api._get_scheduler, get_store=schedule_api._get_store,
                               get_approvals=schedule_api._get_approvals, get_scanner=schedule_api._get_scanner,
                               get_cfg=schedule_api._get_cfg)
        schedule_api.configure(get_scheduler=lambda: None, get_store=lambda: self.store,
                               get_approvals=lambda: self.approvals, get_scanner=lambda: None,
                               get_cfg=lambda: {})
        hub_server.app.config["TESTING"] = True
        self.client = hub_server.app.test_client()

        def resolve(raw):
            if Path(raw) == self.proj:
                return str(self.proj)
            raise ValueError(f"unknown project {raw}")

        self._resolve = mock.patch.object(hub_server, "_resolve_project_path", side_effect=resolve)
        self._resolve.start()
        self._notify = mock.patch("services.schedule_approvals.notify")
        self._notify.start()

    def tearDown(self):
        self._notify.stop()
        self._resolve.stop()
        schedule_api.configure(**self._prior_api)
        self._tmp.cleanup()

    def body(self, **over):
        base = {"name": "report", "kind": "command", "target": {"cmd": "echo hi"}, "project": str(self.proj),
                "when": {"type": "preset", "preset": "weekdays", "at": "09:00", "tz": "UTC"},
                "autonomy": "approval"}
        base.update(over)
        return base

    def test_create_list_and_names(self):
        resp = self.client.post("/api/hub/schedules", json=self.body())
        self.assertEqual(resp.status_code, 201, resp.get_json())
        row = resp.get_json()
        self.assertEqual(row["when"]["expr"], "0 9 * * 1-5")
        listed = self.client.get("/api/hub/schedules").get_json()
        self.assertEqual(listed["schedules"][0]["project_name"], "proj")
        self.assertEqual(self.client.get(f"/api/hub/schedules?path={self.proj}").get_json()["count"], 1)
        self.assertEqual(self.client.get("/api/hub/schedules?path=/nowhere").status_code, 404)
        self.assertEqual(self.client.post("/api/hub/schedules", json=self.body(project="/nowhere")).status_code, 404)
        self.assertEqual(self.client.post("/api/hub/schedules", json=self.body(effort="ultra")).status_code, 400)

    def test_update_toggle_run_delete(self):
        row = self.client.post("/api/hub/schedules", json=self.body()).get_json()
        one = self.client.get(f"/api/hub/schedules/{row['id']}").get_json()
        patched = self.client.patch(f"/api/hub/schedules/{row['id']}",
                                    json={"name": "renamed", "expected_rev": one["rev"]})
        self.assertEqual(patched.get_json()["name"], "renamed")
        stale = self.client.patch(f"/api/hub/schedules/{row['id']}", json={"name": "x", "expected_rev": one["rev"]})
        self.assertEqual(stale.status_code, 409)
        self.assertFalse(self.client.post(f"/api/hub/schedules/{row['id']}/disable").get_json()["enabled"])
        self.assertTrue(self.client.post(f"/api/hub/schedules/{row['id']}/enable").get_json()["enabled"])
        self.assertEqual(self.client.post(f"/api/hub/schedules/{row['id']}/bogus").status_code, 404)
        run = self.client.post(f"/api/hub/schedules/{row['id']}/run", json={"bypass_approval": True})
        self.assertEqual(run.status_code, 202)
        self.assertEqual(run.get_json()["status"], "approved")
        runs = self.client.get(f"/api/hub/schedules/{row['id']}/runs").get_json()
        self.assertEqual(runs["count"], 1)
        self.assertEqual(self.client.get("/api/hub/schedules/runs?status=approved").get_json()["count"], 1)
        self.assertEqual(self.client.delete(f"/api/hub/schedules/{row['id']}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/hub/schedules/{row['id']}").status_code, 404)
        self.assertEqual(self.client.post("/api/hub/schedules/sch-none/run", json={}).status_code, 404)

    def test_approvals_decide_as_desktop(self):
        row = self.client.post("/api/hub/schedules", json=self.body()).get_json()
        run = self.store.request_run(row["id"])
        pending = self.approvals.create(row, run, ttl_s=600)
        listed = self.client.get("/api/hub/schedules/approvals?status=pending").get_json()
        self.assertEqual(listed["approvals"][0]["project_name"], "proj")
        ok = self.client.post(f"/api/hub/schedules/approvals/{pending['id']}", json={"decision": "deny", "note": "no"})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual((ok.get_json()["status"], ok.get_json()["decided_by"]), ("denied", "desktop"))
        self.assertEqual(self.client.post(f"/api/hub/schedules/approvals/{pending['id']}",
                                          json={"decision": "approve"}).status_code, 409)

    def test_run_output(self):
        row = self.client.post("/api/hub/schedules", json=self.body()).get_json()
        run = self.store.request_run(row["id"])
        url = f"/api/hub/schedules/runs/{run['run_id']}/output"
        body = self.client.get(url).get_json()
        self.assertEqual((body["source"], body["lines"], body["open"], body["format"]), ("none", [], True, "text"))
        (self.store.runs_dir / f"{run['run_id']}.out").write_bytes(b"built\n")
        self.store.update_run(run["run_id"], output_lines=1, output_dropped=0)
        body = self.client.get(url).get_json()
        self.assertEqual((body["source"], body["lines"]), ("archive", [{"n": 1, "text": "built"}]))
        self.assertEqual(self.client.get("/api/hub/schedules/runs/sr-0000000000/output").status_code, 404)
        self.assertEqual(self.client.get(url + "?after=-").status_code, 400)

    def test_preview_and_status_unreachable(self):
        resp = self.client.post("/api/hub/schedules/preview",
                                json={"when": {"type": "cron", "expr": "*/30 * * * *", "tz": "UTC"}, "count": 2})
        self.assertEqual(len(resp.get_json()["next"]), 2)
        with mock.patch.object(hub_server, "_read_hub_config", return_value={"oracle_url": "http://127.0.0.1:9"}):
            status = self.client.get("/api/hub/schedules/status").get_json()
        self.assertFalse(status["reachable"])
        self.assertFalse(status["running"])


if __name__ == "__main__":
    unittest.main()
