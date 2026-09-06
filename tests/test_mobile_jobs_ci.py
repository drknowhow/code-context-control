"""D0b (v2.126.0): the gateway's jobs + CI routes, and the feed changes.

- ``GET /api/mobile/jobs`` reads ``JobStore.list_all`` — a READ-ONLY
  enumerator. The proof is on mtimes: a ``running`` job whose supervisor is
  dead WOULD be marked ``lost`` by the mutating ``reap``/``list``/``resolve``
  paths; after the route runs, every job file's mtime and content is exactly
  what it was.
- ``GET /api/mobile/ci/runs`` / ``/ci/run`` serve the index and one run.
- ``/info`` advertises ``jobs`` and ``ci``.
- Feed: an ``info`` notification is retrievable when the client names
  ``severity=info`` (and excluded when it names ``warning``); notification
  items always carry ``kind`` and ``ref_id`` (empty for pre-2.126 lines);
  and a finished job's notification WAKES a ``/feed?wait=`` long-poll.

Rows never carry creds, env, pids or guard paths — asserted, not assumed.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ["C3_ORACLE_API_KEY"] = "mobile-jobs-key"

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import mobile_api  # noqa: E402
from services import ci_runner  # noqa: E402
from services import shell_jobs as sj  # noqa: E402

_DEAD_PID = 2 ** 22 + 7
_FORBIDDEN_JOB_FIELDS = ("creds", "guard", "spool", "supervisor_pid", "child_pid",
                         "supervisor_argv", "env", "secrets", "cmd_sha256")


class _StubScanner:
    def __init__(self, projects):
        self.projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self.projects]


def _entry(path, name):
    return {"path": str(path), "name": name, "tags": [], "active": False,
            "has_c3": True, "fact_count": 0}


def _snapshot(root: Path) -> dict:
    """``{path: (mtime_ns, bytes)}`` for every file under ``root``."""
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_mtime_ns, p.read_bytes())
    return out


class TestMobileJobsCi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="c3-mobile-jobs-"))
        cls.alpha = cls.tmp / "alpha"
        cls.beta = cls.tmp / "beta"
        cls.delta = cls.tmp / "delta"        # feed wake target: no fixture rows
        cls.outsider = cls.tmp / "outsider"  # has jobs, never registered
        for p in (cls.alpha, cls.beta, cls.delta, cls.outsider):
            (p / ".c3").mkdir(parents=True)
            (p / ".c3" / "config.json").write_text(
                json.dumps({"hybrid": {"ollama_base_url": "http://127.0.0.1:9"}}),
                encoding="utf-8")
        cls.store_root = cls.tmp / "store"
        cls._env = mock.patch.dict(os.environ, {"C3_SHELL_OUT_DIR": str(cls.store_root)})
        cls._env.start()

        store = sj.JobStore(apply_acl=False)

        def job(project, session, cmd, status, **fields):
            j = store.create(project_path=str(project), session_id=session, cmd=cmd,
                             cwd=str(project), timeout_s=60, guard_paths=[str(project / "x")],
                             cred_names=["SECRET_NAME"])
            j.status = status
            for k, v in fields.items():
                setattr(j, k, v)
            store.save(j)
            return j

        cls.j_done = job(cls.alpha, "host-A", "pytest -q", "done", exit_code=0,
                         duration_ms=1500, finished_at="2026-09-06T10:00:05+00:00",
                         started_at="2026-09-06T10:00:03+00:00", output_id="o-aaaaaaaaaaaa")
        # A running job with a dead supervisor: the mutating paths would mark
        # it lost; the read-only route must not.
        cls.j_stale = job(cls.alpha, "host-A", "sleep 999", "running",
                          supervisor_pid=_DEAD_PID, supervisor_start_time="1",
                          supervisor_start_source="test", child_pid=None,
                          started_at="2026-09-06T10:01:00+00:00")
        cls.j_failed = job(cls.beta, "host-B", "make", "failed", exit_code=2,
                           duration_ms=300, finished_at="2026-09-06T09:00:00+00:00",
                           error="boom")
        cls.j_outside = job(cls.outsider, "host-X", "rm -rf build", "done", exit_code=0)

        # CI runs in alpha through the real persist path.
        def run(run_id, verdict, statuses, note=""):
            jobs = [ci_runner.JobResult(key=f"ci/j{i}", job_id=f"j{i}", name=f"j {i}",
                                        workflow="ci", runs_on="windows-latest", status=s)
                    for i, s in enumerate(statuses)]
            res = ci_runner.RunResult(run_id=run_id,
                                      project=str(cls.alpha), started_at=ci_runner._now(),
                                      finished_at=ci_runner._now(), verdict=verdict, jobs=jobs,
                                      note=note)
            ci_runner._persist(cls.alpha, res,
                               cls.alpha / ci_runner.CI_DIR / "runs" / res.run_id)
            return res

        cls.run_pass = run("aaaa11112222", ci_runner.FULL_PASS, ["passed", "passed"])
        cls.run_fail = run("bbbb33334444", ci_runner.FAIL, ["passed", "failed"], note="1 failed")

        # A pre-2.126 notification line (no kind/ref_id) in beta.
        (cls.beta / ".c3" / "notifications.jsonl").write_text(json.dumps({
            "id": "old-line", "agent": "legacy", "severity": "info", "title": "old",
            "message": "m", "message_hash": "x", "timestamp": "2026-08-01T00:00:00+00:00",
            "last_seen": "2026-08-01T00:00:00+00:00", "count": 1, "acknowledged": False,
        }) + "\n", encoding="utf-8")

        cls._prior_key = os.environ.get("C3_ORACLE_API_KEY")
        os.environ["C3_ORACLE_API_KEY"] = "mobile-jobs-key"
        cls._prior_cfg = srv._cfg
        srv._cfg = {"mobile_api_enabled": True, "api_rate_limit_per_min": 0}
        mobile_api.init_services(scanner=_StubScanner([
            _entry(cls.alpha, "alpha"), _entry(cls.beta, "beta"), _entry(cls.delta, "delta")]))
        srv.app.config["TESTING"] = True
        cls.client = srv.app.test_client()
        cls.auth = {"Authorization": "Bearer mobile-jobs-key"}

    @classmethod
    def tearDownClass(cls):
        srv._cfg = cls._prior_cfg
        if cls._prior_key is None:
            os.environ.pop("C3_ORACLE_API_KEY", None)
        else:
            os.environ["C3_ORACLE_API_KEY"] = cls._prior_key
        cls._env.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, **params):
        return self.client.get(path, query_string=params, headers=self.auth)

    # ── /info ─────────────────────────────────────────────

    def test_info_advertises_jobs_and_ci(self):
        caps = self.get("/api/mobile/info").get_json()["capabilities"]
        self.assertIn("jobs", caps)
        self.assertIn("ci", caps)

    # ── /jobs ─────────────────────────────────────────────

    def test_jobs_requires_auth(self):
        self.assertEqual(self.client.get("/api/mobile/jobs").status_code, 401)

    def test_jobs_for_one_project_newest_first_and_untouched(self):
        before = _snapshot(self.store_root)
        r = self.get("/api/mobile/jobs", project=str(self.alpha))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        ids = [j["id"] for j in body["jobs"]]
        self.assertEqual(set(ids), {self.j_done.id, self.j_stale.id})
        self.assertEqual(ids, sorted(ids, key=lambda i: {self.j_done.id: self.j_done.created_at,
                                                         self.j_stale.id: self.j_stale.created_at}[i],
                                     reverse=True))
        stale = next(j for j in body["jobs"] if j["id"] == self.j_stale.id)
        self.assertEqual(stale["status"], "running")        # NOT marked lost
        done = next(j for j in body["jobs"] if j["id"] == self.j_done.id)
        self.assertEqual(done["exit_code"], 0)
        self.assertEqual(done["duration_ms"], 1500)
        self.assertEqual(done["output_id"], "o-aaaaaaaaaaaa")
        self.assertEqual(done["session_id"], "host-A")
        self.assertEqual(done["cmd_display"], "pytest -q")
        self.assertEqual(done["project_path"], str(self.alpha))
        for key in ("id", "project_path", "session_id", "status", "cmd_display", "cwd",
                    "created_at", "started_at", "finished_at", "exit_code", "duration_ms",
                    "output_id"):
            self.assertIn(key, done)
        for key in _FORBIDDEN_JOB_FIELDS:
            self.assertNotIn(key, done)
        self.assertNotIn("SECRET_NAME", r.get_data(as_text=True))
        self.assertEqual(_snapshot(self.store_root), before, "the jobs route mutated the store")

    def test_jobs_without_project_covers_registered_projects_only(self):
        body = self.get("/api/mobile/jobs").get_json()
        ids = {j["id"] for j in body["jobs"]}
        self.assertEqual(ids, {self.j_done.id, self.j_stale.id, self.j_failed.id})
        self.assertNotIn(self.j_outside.id, ids)

    def test_jobs_status_filter_and_limit(self):
        body = self.get("/api/mobile/jobs", status="failed").get_json()
        self.assertEqual([j["id"] for j in body["jobs"]], [self.j_failed.id])
        self.assertEqual(body["jobs"][0]["error"], "boom")
        body = self.get("/api/mobile/jobs", limit=1).get_json()
        self.assertEqual(len(body["jobs"]), 1)
        self.assertEqual(body["limit"], 1)

    def test_jobs_rejects_unknown_status_and_project(self):
        self.assertEqual(self.get("/api/mobile/jobs", status="bogus").status_code, 400)
        self.assertEqual(self.get("/api/mobile/jobs", project=str(self.outsider)).status_code, 404)

    def test_list_all_leaves_files_untouched_directly(self):
        before = _snapshot(self.store_root)
        rows = sj.JobStore(apply_acl=False).list_all()
        self.assertEqual({r["id"] for r in rows},
                         {self.j_done.id, self.j_stale.id, self.j_failed.id, self.j_outside.id})
        rows = sj.JobStore(apply_acl=False).list_all(project_path=str(self.beta))
        self.assertEqual([r["id"] for r in rows], [self.j_failed.id])
        self.assertEqual(_snapshot(self.store_root), before)
        # And the mutating path proves the fixture is the one it claims to be:
        # the same stale job IS reaped by reap(). Done last, on a copy.
        copy_root = self.tmp / "store-copy"
        shutil.copytree(self.store_root, copy_root)
        lost = sj.JobStore(copy_root, apply_acl=False).reap()
        self.assertIn(self.j_stale.id, [j.id for j in lost])

    # ── /ci/runs + /ci/run ────────────────────────────────

    def test_ci_runs_newest_first(self):
        r = self.get("/api/mobile/ci/runs", project=str(self.alpha))
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual([x["run_id"] for x in body["runs"]],
                         [self.run_fail.run_id, self.run_pass.run_id])
        self.assertEqual(body["runs"][0]["verdict"], "FAIL")
        self.assertEqual(body["runs"][0]["counts"], {"passed": 1, "failed": 1})
        self.assertEqual(body["count"], 2)
        body = self.get("/api/mobile/ci/runs", project=str(self.alpha), limit=1).get_json()
        self.assertEqual(len(body["runs"]), 1)

    def test_ci_runs_requires_project(self):
        self.assertEqual(self.get("/api/mobile/ci/runs").status_code, 400)
        self.assertEqual(self.get("/api/mobile/ci/runs", project=str(self.outsider)).status_code, 404)

    def test_ci_run_by_id_and_latest(self):
        r = self.get("/api/mobile/ci/run", project=str(self.alpha), run_id=self.run_pass.run_id)
        self.assertEqual(r.status_code, 200)
        run = r.get_json()["run"]
        self.assertEqual(run["run_id"], self.run_pass.run_id)
        self.assertEqual(run["verdict"], "FULL_CI_PASS")
        self.assertEqual([j["key"] for j in run["jobs"]], ["ci/j0", "ci/j1"])
        latest = self.get("/api/mobile/ci/run", project=str(self.alpha)).get_json()["run"]
        self.assertEqual(latest["run_id"], self.run_fail.run_id)

    def test_ci_run_unknown_is_404(self):
        self.assertEqual(self.get("/api/mobile/ci/run", project=str(self.alpha),
                                  run_id="nope0000").status_code, 404)
        self.assertEqual(self.get("/api/mobile/ci/run", project=str(self.beta)).status_code, 404)
        self.assertEqual(self.get("/api/mobile/ci/run", project=str(self.alpha),
                                  run_id="../../x").status_code, 400)

    # ── Feed ──────────────────────────────────────────────

    def test_feed_info_notifications_are_retrievable_by_explicit_severity(self):
        # The CI producer wrote an info (FULL_CI_PASS) and a warning (FAIL).
        body = self.get("/api/mobile/feed", types="notification", severity="info",
                        project=str(self.alpha)).get_json()
        kinds = {(i["data"]["kind"], i["data"]["ref_id"]) for i in body["items"]}
        self.assertIn(("ci", self.run_pass.run_id), kinds)
        self.assertTrue(all(i["data"]["severity"] == "info" for i in body["items"]))
        body = self.get("/api/mobile/feed", types="notification", severity="warning",
                        project=str(self.alpha)).get_json()
        refs = {i["data"]["ref_id"] for i in body["items"]}
        self.assertIn(self.run_fail.run_id, refs)
        self.assertNotIn(self.run_pass.run_id, refs)
        # No filter: both severities.
        body = self.get("/api/mobile/feed", types="notification",
                        project=str(self.alpha)).get_json()
        refs = {i["data"]["ref_id"] for i in body["items"]}
        self.assertTrue({self.run_pass.run_id, self.run_fail.run_id} <= refs)

    def test_feed_notification_items_always_carry_kind_and_ref_id(self):
        body = self.get("/api/mobile/feed", types="notification",
                        project=str(self.beta)).get_json()
        old = next(i for i in body["items"] if i["data"]["id"] == "old-line")
        self.assertEqual(old["data"]["kind"], "")
        self.assertEqual(old["data"]["ref_id"], "")

    def test_feed_wait_wakes_on_a_finished_job(self):
        # The whole reason D0b exists: a job finishing in the detached
        # supervisor lands a `shell_job` notification, and the notifications
        # file is one the long-poll watches — so the waiting client is
        # answered on THIS request, not on its next poll. Its own store root,
        # so the class-level job fixtures stay exactly as enumerated above.
        store = sj.JobStore(self.tmp / "store-wake", apply_acl=False)
        job = store.create(project_path=str(self.delta), session_id="host-D",
                           cmd="python build.py", cwd=str(self.delta), timeout_s=60)
        since = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()

        def finish():
            time.sleep(0.4)
            sj._finish(store, job, "done", exit_code=0, duration_ms=42)

        writer = threading.Thread(target=finish, daemon=True)
        started = time.monotonic()
        writer.start()
        try:
            r = self.get("/api/mobile/feed", wait=20, since=since, types="notification",
                         severity="info", project=str(self.delta))
        finally:
            writer.join(timeout=5)
        elapsed = time.monotonic() - started
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual([(i["data"]["kind"], i["data"]["ref_id"]) for i in body["items"]],
                         [("shell_job", job.id)])
        self.assertEqual(body["items"][0]["data"]["title"], f"Job done: {job.id}")
        self.assertLess(elapsed, 15)
        self.assertIn("waited_s", body)


if __name__ == "__main__":
    unittest.main()
