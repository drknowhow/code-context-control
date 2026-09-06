"""D0b (v2.126.0): every event producer writes a routable notification.

The mobile gateway's ``/feed?wait=`` wakes only when one of four per-project
files moves, and ``.c3/notifications.jsonl`` is one of them. So a finished
background job, a local CI verdict, a session boundary and the MCP runtime
becoming ready each write ONE notification with a machine-readable ``kind``
and the id of the thing it is about in ``ref_id``. These tests pin, per
producer:

- the exact ``kind`` / ``ref_id`` / ``severity`` / title shape;
- that titles carry the id, so two events that end the same way are two
  records (``NotificationStore.add`` dedups on agent+title);
- that a failure INSIDE the store never raises out of the producer — the
  supervisor is a detached process, the hooks are short-lived subprocesses,
  and a notification failure must never fail the thing it reports on.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import ci_runner  # noqa: E402
from services import notifications as ntf  # noqa: E402
from services import shell_jobs as sj  # noqa: E402
from services.notifications import NotificationStore  # noqa: E402

# A pid no live process has (past Linux's default pid_max, and a Windows
# DWORD nobody hands out); ``process_start_time`` answers ("", "") for it.
_DEAD_PID = 2 ** 22 + 7


def _read_notifications(project: Path) -> list[dict]:
    path = project / ".c3" / "notifications.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_activity(project: Path) -> list[dict]:
    path = project / ".c3" / "activity_log.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "proj"
        (self.project / ".c3").mkdir(parents=True)
        # A live Ollama makes session-save tests flaky; keep every helper
        # that might look for one pointed at a closed port.
        (self.project / ".c3" / "config.json").write_text(
            json.dumps({"hybrid": {"ollama_base_url": "http://127.0.0.1:9"}}),
            encoding="utf-8")
        self.store_root = self.root / "store"
        self._env = patch.dict(os.environ, {"C3_SHELL_OUT_DIR": str(self.store_root)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        for _ in range(20):
            try:
                self._tmp.cleanup()
                break
            except (PermissionError, OSError):
                import time
                time.sleep(0.05)


class TestNotifyHelper(_Base):
    def test_notify_writes_kind_and_ref_id(self):
        entry = ntf.notify(self.project, "x", "info", "T 1", "m", kind="ci", ref_id="r1")
        self.assertIsNotNone(entry)
        rows = _read_notifications(self.project)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "ci")
        self.assertEqual(rows[0]["ref_id"], "r1")
        self.assertEqual(rows[0]["severity"], "info")

    def test_notify_never_raises_when_the_store_does(self):
        with patch.object(NotificationStore, "add", side_effect=RuntimeError("disk on fire")):
            self.assertIsNone(ntf.notify(self.project, "x", "info", "T", "m", kind="ci"))

    def test_notify_never_raises_on_an_unwritable_project(self):
        # ``.c3`` cannot be created under a FILE: the store's own mkdir fails.
        blocker = self.root / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        self.assertIsNone(ntf.notify(blocker, "x", "info", "T", "m"))

    def test_event_kinds_are_the_documented_set(self):
        self.assertEqual(set(ntf.EVENT_KINDS),
                         {"shell_job", "ci", "session", "mcp", "override"})


class TestShellJobProducer(_Base):
    def _job(self, cmd="echo hi") -> sj.JobState:
        store = sj.JobStore(apply_acl=False)
        job = store.create(project_path=str(self.project), session_id="host-sid-1",
                           cmd=cmd, cwd=str(self.project), timeout_s=30)
        return job

    def _only(self, kind: str) -> dict:
        rows = [r for r in _read_notifications(self.project) if r.get("kind") == kind]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_done_is_info_with_job_id_as_ref(self):
        store = sj.JobStore(apply_acl=False)
        job = self._job()
        sj._finish(store, job, "done", exit_code=0, duration_ms=1200)
        row = self._only("shell_job")
        self.assertEqual(row["severity"], "info")
        self.assertEqual(row["ref_id"], job.id)
        self.assertEqual(row["title"], f"Job done: {job.id}")
        self.assertEqual(row["agent"], "shell_job")
        self.assertIn("exit 0", row["message"])
        self.assertIn("1.2s", row["message"])

    def test_failed_timeout_cancelled_are_warnings(self):
        store = sj.JobStore(apply_acl=False)
        for status, kwargs in (("failed", {"exit_code": 1}),
                               ("timeout", {"exit_code": None, "timed_out": True}),
                               ("cancelled", {"exit_code": None})):
            job = self._job(cmd=f"cmd-{status}")
            sj._finish(store, job, status, **kwargs)
        rows = {r["ref_id"]: r for r in _read_notifications(self.project)
                if r.get("kind") == "shell_job"}
        self.assertEqual(len(rows), 3)
        for r in rows.values():
            self.assertEqual(r["severity"], "warning")
            self.assertTrue(r["title"].startswith("Job "), r["title"])
            self.assertIn(r["ref_id"], r["title"])

    def test_two_done_jobs_are_two_records(self):
        # The job id is in the title precisely so the store's agent+title
        # dedup cannot fold two finished jobs into one line (and one wake).
        store = sj.JobStore(apply_acl=False)
        a, b = self._job("same"), self._job("same")
        sj._finish(store, a, "done", exit_code=0, duration_ms=1)
        sj._finish(store, b, "done", exit_code=0, duration_ms=1)
        refs = sorted(r["ref_id"] for r in _read_notifications(self.project)
                      if r.get("kind") == "shell_job")
        self.assertEqual(refs, sorted([a.id, b.id]))

    def test_lost_transition_notifies_from_reap(self):
        store = sj.JobStore(apply_acl=False)
        job = self._job()
        job.status = "running"
        job.supervisor_pid = _DEAD_PID
        job.supervisor_start_time = "1"
        job.supervisor_start_source = "test"
        store.save(job)
        after = store._reap_one(job)
        self.assertEqual(after.status, "lost")
        row = self._only("shell_job")
        self.assertEqual(row["severity"], "warning")
        self.assertEqual(row["title"], f"Job lost: {job.id}")
        self.assertEqual(row["ref_id"], job.id)

    def test_store_failure_never_fails_the_finish(self):
        store = sj.JobStore(apply_acl=False)
        job = self._job()
        with patch.object(NotificationStore, "add", side_effect=OSError("locked")):
            sj._finish(store, job, "done", exit_code=0, duration_ms=5)
        fresh = store.load(job)
        self.assertEqual(fresh.status, "done")
        self.assertEqual(_read_notifications(self.project), [])
        # The activity row the supervisor owes is still written.
        self.assertTrue(any(e.get("type") == "shell_exec" and e.get("job_id") == job.id
                            for e in _read_activity(self.project)))


class TestCiProducer(_Base):
    def _result(self, verdict: str, statuses: list[str]) -> ci_runner.RunResult:
        jobs = [ci_runner.JobResult(key=f"wf/job{i}", job_id=f"job{i}", name=f"job {i}",
                                    workflow="wf", runs_on="windows-latest", status=s)
                for i, s in enumerate(statuses)]
        res = ci_runner.RunResult(run_id=f"run{len(statuses)}{verdict[:1]}".lower(),
                                  project=str(self.project), started_at=ci_runner._now(),
                                  finished_at=ci_runner._now(), verdict=verdict, jobs=jobs)
        return res

    def _persist(self, res):
        run_dir = self.project / ci_runner.CI_DIR / "runs" / res.run_id
        ci_runner._persist(self.project, res, run_dir)

    def test_full_pass_is_info(self):
        res = self._result(ci_runner.FULL_PASS, ["passed"] * 3)
        self._persist(res)
        rows = [r for r in _read_notifications(self.project) if r.get("kind") == "ci"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["severity"], "info")
        self.assertEqual(row["ref_id"], res.run_id)
        self.assertEqual(row["agent"], "ci")
        self.assertEqual(row["title"], f"CI FULL_CI_PASS (3 passed) run {res.run_id}")

    def test_partial_and_fail_are_warnings(self):
        partial = self._result(ci_runner.PARTIAL_PASS, ["passed", "foreign"])
        partial.note = "1 job targets another OS"
        self._persist(partial)
        failed = self._result(ci_runner.FAIL, ["passed", "failed", "skipped"])
        self._persist(failed)
        rows = {r["ref_id"]: r for r in _read_notifications(self.project)
                if r.get("kind") == "ci"}
        self.assertEqual(set(rows), {partial.run_id, failed.run_id})
        self.assertEqual(rows[partial.run_id]["severity"], "warning")
        self.assertEqual(rows[partial.run_id]["title"],
                         f"CI PARTIAL_PASS (1 passed, 1 not run) run {partial.run_id}")
        self.assertEqual(rows[partial.run_id]["message"], "1 job targets another OS")
        self.assertEqual(rows[failed.run_id]["severity"], "warning")
        self.assertEqual(rows[failed.run_id]["title"],
                         f"CI FAIL (1 passed, 1 failed, 1 not run) run {failed.run_id}")
        self.assertIn("wf/job1", rows[failed.run_id]["message"])

    def test_store_failure_never_fails_persist(self):
        res = self._result(ci_runner.FULL_PASS, ["passed"])
        with patch.object(NotificationStore, "add", side_effect=OSError("locked")):
            self._persist(res)
        # The run record and the index line still landed.
        self.assertTrue((self.project / ci_runner.CI_DIR / "runs" / res.run_id / "run.json").is_file())
        self.assertEqual(ci_runner.list_runs(str(self.project))[0]["run_id"], res.run_id)
        self.assertEqual(_read_notifications(self.project), [])


class TestMcpProducer(_Base):
    """cli.mcp_server's helpers, driven with a stub runtime (no FastMCP)."""

    def _svc(self, host_sid: str = "", c3_sid: str = "20260906_120000_abcdef012345"):
        session = {"id": c3_sid, "host_session_id": host_sid}
        mgr = SimpleNamespace(current_session=session)
        return SimpleNamespace(project_path=str(self.project), session_mgr=mgr,
                               ide_name="claude-code")

    def setUp(self):
        super().setUp()
        from cli import mcp_server
        self.mcp = mcp_server
        self.mcp._host_sid_cache.update({"value": "", "read_at": 0.0, "linked": ""})

    def test_connected_notification_is_info_with_c3_session_ref(self):
        svc = self._svc(host_sid="aaaaaaaa-1111-2222-3333-444444444444")
        self.mcp._notify_mcp_ready(svc)
        rows = [r for r in _read_notifications(self.project) if r.get("kind") == "mcp"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["severity"], "info")
        self.assertEqual(row["ref_id"], "20260906_120000_abcdef012345")
        self.assertEqual(row["title"], "C3 connected aaaaaaaa")
        self.assertIn("host aaaaaaaa-1111-2222-3333-444444444444", row["message"])
        self.assertEqual(row["agent"], "c3")

    def test_connected_writes_the_host_link_file(self):
        from services.host_sessions import linked_c3_session_id
        svc = self._svc(host_sid="bbbbbbbb-1111-2222-3333-444444444444")
        self.mcp._notify_mcp_ready(svc)
        self.assertEqual(
            linked_c3_session_id(self.project, "claude", "bbbbbbbb-1111-2222-3333-444444444444"),
            "20260906_120000_abcdef012345")

    def test_host_id_falls_back_to_enforcement_state_and_is_throttled(self):
        state = self.project / ".c3" / "enforcement_state.json"
        state.write_text(json.dumps({"session_id": "cccccccc-1", "last_c3_call": None,
                                     "unlocked_files": {}}), encoding="utf-8")
        svc = self._svc(host_sid="")
        self.assertEqual(self.mcp._host_session_id(svc), "cccccccc-1")
        # A change on disk inside the TTL is NOT seen: at most one read per 5 s.
        state.write_text(json.dumps({"session_id": "dddddddd-2", "last_c3_call": None,
                                     "unlocked_files": {}}), encoding="utf-8")
        self.assertEqual(self.mcp._host_session_id(svc), "cccccccc-1")
        self.mcp._host_sid_cache["read_at"] = 0.0          # TTL elapsed
        self.assertEqual(self.mcp._host_session_id(svc), "dddddddd-2")

    def test_store_failure_never_raises(self):
        svc = self._svc(host_sid="eeeeeeee-1")
        with patch.object(NotificationStore, "add", side_effect=OSError("locked")):
            self.mcp._notify_mcp_ready(svc)      # must not raise
        self.assertEqual(_read_notifications(self.project), [])


if __name__ == "__main__":
    unittest.main()
