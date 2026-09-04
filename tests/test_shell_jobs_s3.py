"""S3 of the c3_shell remediation: background jobs run by a detached supervisor.

Measured 2026-09-04: 44% of c3_shell wall time was in calls over 60 s, and the
MCP client kills a tool call at 120 s — so every long build and the full test
suite left C3 for native Bash and lost the ledger, the telemetry and the spill
store. These tests pin the contract of ``c3_shell_job`` and
``services/shell_jobs.py``:

- a real job: running with the child pid + creation time recorded, the growing
  spool tails while it runs, the output is promoted ALWAYS and pages back by id
  for the same project + session only;
- cancel kills the child tree and records ``cancelled``; a creation-time
  mismatch (reused pid) makes cancel refuse and say so;
- the supervisor survives the process that started it;
- the job's own timeout produces ``timeout``;
- a job whose supervisor is gone is reaped as ``lost`` with its spool kept;
- a blocked command is refused before anything is spawned;
- an injected credential never lands in any file under the store and is
  redacted in the kept output;
- unknown / foreign ids share one refusal wording;
- ``c3_project`` refuses to proxy jobs.

Wall clock: ``TestLiveJobs`` spawns real supervisors and takes roughly 25 s in
total; the two slowest cases (``…runs_to_completion…`` ~6 s,
``…survives_the_parent`` ~5 s) are marked in their docstrings. Everything
else is sub-second.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools import _grants  # noqa: E402
from cli.tools import shell as shell_mod  # noqa: E402
from cli.tools import shell_job as job_mod  # noqa: E402
from services import access_guard  # noqa: E402
from services import shell_jobs as sj  # noqa: E402
from services.output_filter import OutputFilter  # noqa: E402
from services.shell_output import OutputAccessError, ShellOutputStore  # noqa: E402

PY = sys.executable
ROOT = Path(__file__).resolve().parent.parent


def _svc(project: str, **extra):
    base = dict(project_path=project, activity_log=None, edit_ledger=None,
                output_filter=OutputFilter({"HYBRID_DISABLE_TIER1": True}),
                session_mgr=None, hybrid_config={})
    base.update(extra)
    return SimpleNamespace(**base)


def _finalize(name, args, resp, summ, **kw):
    return resp


def _allow(_path):
    return None


def _py(code: str) -> str:
    """A one-line python command safe for Git Bash / cmd / sh quoting."""
    return f'"{PY}" -c "{code}"'


def _spawn_and_kill_a_process() -> tuple[int, str, str]:
    """(pid, creation time, source) of a process that no longer exists."""
    proc = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        start, source = sj.process_start_time(proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=10)
    for _ in range(50):
        if not sj.process_start_time(proc.pid)[0]:
            break
        time.sleep(0.1)
    return proc.pid, start, source


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.project = self.root / "proj"
        (self.project / ".c3").mkdir(parents=True)
        self.store_root = self.root / "store"
        self._env = patch.dict(os.environ, {"C3_SHELL_OUT_DIR": str(self.store_root)})
        self._env.start()
        self.svc = _svc(str(self.project))
        self.session_id = _grants.session_id(self.svc)
        self._started: list[str] = []

    def tearDown(self):
        # Never leave a child running: cancel whatever this test started.
        store = sj.JobStore()
        for jid in self._started:
            try:
                job = store.resolve(jid, project_path=str(self.project), session_id=self.session_id,
                                    guard_check=_allow)
                if not job.terminal:
                    store.cancel(job)
            except Exception:
                pass
        self._env.stop()
        for _ in range(20):                           # Windows: a handle may close a beat late
            try:
                self._tmp.cleanup()
                break
            except (PermissionError, OSError):
                time.sleep(0.25)

    def _call(self, action, **kw):
        return job_mod.handle_shell_job(action, svc=self.svc, finalize=_finalize, **kw)

    def _start(self, cmd, **kw) -> tuple[str, str]:
        out = self._call("start", cmd=cmd, **kw)
        self.assertIn("[c3_shell_job:started] j-", out, out)
        jid = out.split("started] ", 1)[1].split()[0]
        self._started.append(jid)
        return jid, out

    def _job(self, jid) -> sj.JobState:
        return sj.JobStore().resolve(jid, project_path=str(self.project), session_id=self.session_id,
                                     guard_check=_allow)

    def _wait_terminal(self, jid, timeout=30.0) -> sj.JobState:
        deadline = time.monotonic() + timeout
        while True:
            job = self._job(jid)
            if job.terminal or time.monotonic() > deadline:
                return job
            time.sleep(0.2)


class TestProcessIdentity(unittest.TestCase):
    def test_self_is_alive_with_a_creation_time(self):
        start, source = sj.process_start_time(os.getpid())
        self.assertTrue(start, "no creation time for our own pid")
        self.assertIn(source, ("GetProcessTimes", "powershell", "procfs", "ps"))
        self.assertTrue(sj.process_alive(os.getpid(), start, source))
        self.assertFalse(sj.process_alive(os.getpid(), "not-the-same", source))
        self.assertFalse(sj.process_alive(os.getpid(), start, "some-other-source"))

    def test_an_exited_pid_is_not_alive(self):
        pid, start, source = _spawn_and_kill_a_process()
        self.assertTrue(start)
        self.assertFalse(sj.process_alive(pid, start, source))

    def test_bad_pids(self):
        self.assertEqual(sj.process_start_time("x"), ("", ""))
        self.assertEqual(sj.process_start_time(0), ("", ""))
        self.assertFalse(sj.process_alive(None, "", ""))


class TestPreflight(_Base):
    def test_blocked_command_is_refused_before_any_spawn(self):
        with patch.object(sj.JobStore, "start", side_effect=AssertionError("spawned")) as spawn:
            out = self._call("start", cmd="rm -rf / ")
            spawn.assert_not_called()
        self.assertIn("blocked pattern", out)
        self.assertEqual(self._call("start", cmd="   "), "[c3_shell_job:error] empty command")

    def test_timeout_is_clamped_to_six_hours(self):
        seen = {}

        def fake_start(self_, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")

        with patch.object(sj.JobStore, "start", fake_start):
            out = self._call("start", cmd="echo hi", timeout=99_999)
        self.assertEqual(seen["timeout_s"], sj.MAX_TIMEOUT_S)
        self.assertIn("could not start the supervisor", out)

    def test_cwd_deny_refuses(self):
        denial = access_guard.Denial("<test>", "deny", "user", "test rule")
        with patch.object(access_guard, "check", return_value=denial), \
                patch.object(sj.JobStore, "start", side_effect=AssertionError("spawned")):
            out = self._call("start", cmd="echo hi")
        self.assertNotIn("started]", out)
        self.assertIn("<test>", out)

    def test_bad_action_and_missing_id(self):
        self.assertIn("action must be one of", self._call("zap"))
        self.assertIn("needs job_id", self._call("status"))
        self.assertIn("needs job_id", self._call("tail"))

    def test_unknown_and_foreign_ids_share_one_wording(self):
        self.assertIn("not found for this project and session", self._call("status", job_id="j-000000000000"))
        self.assertIn("not found for this project and session", self._call("tail", job_id="nonsense"))
        self.assertIn("not found for this project and session", self._call("cancel", job_id="o-000000000000"))
        self.assertIn("needs job_id", self._call("cancel", job_id=""))
        self.assertIn("no jobs for this project and session", self._call("list"))


class TestCancelSafety(_Base):
    def _running_record(self, child_pid, child_start, child_source) -> sj.JobState:
        store = sj.JobStore()
        job = store.create(project_path=str(self.project), session_id=self.session_id,
                           cmd="sleep 60", cwd=str(self.project), timeout_s=60)
        job.status = "running"
        job.started_at = job.created_at
        job.supervisor_pid = os.getpid()               # a live supervisor: this very process
        job.supervisor_start_time, job.supervisor_start_source = sj.process_start_time(os.getpid())
        job.child_pid = child_pid
        job.child_start_time, job.child_start_source = child_start, child_source
        store.save(job)
        return job

    def test_creation_time_mismatch_refuses_to_kill(self):
        # The recorded child pid is OUR OWN pid with a creation time that does
        # not match: a reused pid. Cancel must refuse, say so, and signal nothing.
        _, real_source = sj.process_start_time(os.getpid())
        job = self._running_record(os.getpid(), "1", real_source)
        out = self._call("cancel", job_id=job.id)
        self.assertIn("refused: pid", out)
        self.assertIn("nothing was killed", out)
        self.assertIn(f"{job.id} running", out)
        self.assertFalse(job.cancel_marker.exists())
        self.assertEqual(self._job(job.id).status, "running")   # still the supervisor's call

    def test_exited_child_is_not_signalled(self):
        pid, start, source = _spawn_and_kill_a_process()
        job = self._running_record(pid, start, source)
        with patch.object(sj, "kill_tree_by_pid", side_effect=AssertionError("must not kill")):
            fresh, note = sj.JobStore().cancel(self._job(job.id))
        self.assertIn("already exited", note)
        self.assertFalse(job.cancel_marker.exists())


class TestReap(_Base):
    def test_dead_supervisor_marks_lost_and_keeps_the_spool(self):
        store = sj.JobStore()
        job = store.create(project_path=str(self.project), session_id=self.session_id,
                           cmd="make all", cwd=str(self.project), timeout_s=600)
        pid, start, source = _spawn_and_kill_a_process()
        spool = store.output_store.spool_dir()
        oid = "o-0123456789ab"
        (spool / f"{oid}.stdout.part").write_bytes(b"built 1\nbuilt 2\n")
        (spool / f"{oid}.stderr.part").write_bytes(b"")
        job.status = "running"
        job.started_at = job.created_at
        job.supervisor_pid, job.supervisor_start_time, job.supervisor_start_source = pid, start, source
        job.output_id = oid
        job.spool = {"stdout": str(spool / f"{oid}.stdout.part"), "stderr": str(spool / f"{oid}.stderr.part")}
        store.save(job)

        lost = store.reap(project_path=str(self.project), session_id=self.session_id)
        self.assertEqual([j.id for j in lost], [job.id])
        after = self._job(job.id)
        self.assertEqual(after.status, "lost")
        self.assertIn("supervisor process is gone", after.error)
        self.assertEqual(after.output_id, oid)
        self.assertEqual(after.stdout.get("lines"), 2)
        meta = ShellOutputStore().resolve(oid, project_path=str(self.project), session_id=self.session_id,
                                          guard_check=_allow)
        self.assertIn("built 2", ShellOutputStore().read(meta, "stdout"))
        self.assertFalse((spool / f"{oid}.stdout.part").exists())
        out = self._call("status", job_id=job.id)
        self.assertIn(f"{job.id} lost", out)
        self.assertIn(f"output_id={oid}", out)
        self.assertIn("lost", self._call("list"))

    def test_live_supervisor_is_left_alone(self):
        store = sj.JobStore()
        job = store.create(project_path=str(self.project), session_id=self.session_id,
                           cmd="sleep 5", cwd=str(self.project), timeout_s=60)
        job.status = "running"
        job.supervisor_pid = os.getpid()
        job.supervisor_start_time, job.supervisor_start_source = sj.process_start_time(os.getpid())
        store.save(job)
        self.assertEqual(store.reap(project_path=str(self.project), session_id=self.session_id), [])
        self.assertEqual(self._job(job.id).status, "running")


class TestLiveJobs(_Base):
    """Real supervisors. ~25 s for the class; the slow cases say so."""

    def test_job_runs_to_completion_and_its_output_pages_back(self):
        """~6 s: 3 s of sleep inside the job plus polling."""
        cmd = _py("import time,sys; print('a'); sys.stdout.flush(); time.sleep(3); print('b'); sys.exit(4)")
        jid, out = self._start(cmd)
        self.assertIn("(timeout 1800s)", out)
        self.assertIn("poll with action='status'", out)

        job = self._job(jid)
        self.assertIn(job.status, ("queued", "running"))
        deadline = time.monotonic() + 10
        while job.status == "queued" and time.monotonic() < deadline:
            time.sleep(0.1)
            job = self._job(jid)
        self.assertEqual(job.status, "running")
        self.assertTrue(job.child_pid)
        self.assertTrue(job.child_start_time, "child creation time not recorded")
        self.assertTrue(job.child_start_source)
        self.assertTrue(job.supervisor_pid)
        self.assertTrue(job.supervisor_start_time)
        self.assertTrue(sj.process_alive(job.supervisor_pid, job.supervisor_start_time, job.supervisor_start_source))
        self.assertEqual(job.output_id[:2], "o-")
        self.assertNotIn(str(self.store_root), out)     # no path handed to the agent

        # tail shows the first line while the job is still running
        deadline = time.monotonic() + 5
        tail = ""
        while time.monotonic() < deadline:
            tail = self._call("tail", job_id=jid)
            if "\na" in tail:
                break
            time.sleep(0.1)
        self.assertIn(f"[c3_shell_job:tail] {jid} running", tail)
        self.assertIn("[stdout L1-1 of 1, running]", tail)
        status = self._call("status", job_id=jid)
        self.assertIn(f"{jid} running", status)
        self.assertIn(f"child pid {job.child_pid}", status)

        job = self._wait_terminal(jid)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.exit_code, 4)
        self.assertFalse(job.timed_out)
        self.assertGreaterEqual(job.duration_ms, 2500)
        self.assertTrue(job.finished_at)
        self.assertEqual(job.spool, {})
        self.assertEqual(job.stdout.get("lines"), 2)
        status = self._call("status", job_id=jid)
        self.assertIn(f"{jid} failed exit 4", status)
        self.assertIn(f"output_id={job.output_id}", status)
        tail = self._call("tail", job_id=jid, lines="1")
        self.assertIn("[stdout L2-2 of 2, failed]\nb", tail)

        # the kept output resolves for this project + session…
        store = ShellOutputStore()
        meta = store.resolve(job.output_id, project_path=str(self.project), session_id=self.session_id,
                             guard_check=_allow)
        self.assertEqual(meta.exit_code, 4)
        paged = asyncio.run(shell_mod.handle_shell("", "", 60, True, False, self.svc, _finalize,
                                                   output_id=job.output_id, output_action="read"))
        self.assertIn("[c3_shell:output]", paged)
        self.assertIn("\na\nb", paged)
        # …and for nobody else
        other = self.root / "other"
        (other / ".c3").mkdir(parents=True)
        with self.assertRaises(OutputAccessError):
            store.resolve(job.output_id, project_path=str(other), session_id=self.session_id, guard_check=_allow)
        with self.assertRaises(OutputAccessError):
            store.resolve(job.output_id, project_path=str(self.project), session_id="someone-else",
                          guard_check=_allow)
        foreign = job_mod.handle_shell_job("status", job_id=jid, svc=_svc(str(other)), finalize=_finalize)
        self.assertIn("not found for this project and session", foreign)
        with patch.object(_grants, "session_id", return_value="someone-else"):
            stranger = self._call("status", job_id=jid)
        self.assertIn("not found for this project and session", stranger)
        denial = access_guard.Denial("<test>", "deny", "user", "test rule")
        with patch.object(access_guard, "check", return_value=denial):
            denied = self._call("status", job_id=jid)
        self.assertIn("no longer readable", denied)

        # the records a synchronous call would have written
        activity = (self.project / ".c3" / "activity_log.jsonl").read_text(encoding="utf-8")
        rec = [json.loads(line) for line in activity.splitlines() if '"shell_exec"' in line][-1]
        self.assertEqual(rec["job_id"], jid)
        self.assertEqual(rec["exit_code"], 4)
        telemetry = (self.project / ".c3" / "tool_telemetry.jsonl").read_text(encoding="utf-8")
        trec = [json.loads(line) for line in telemetry.splitlines() if jid in line][-1]
        self.assertEqual(trec["tool"], "c3_shell")
        self.assertEqual(trec["detail"]["cmd_class"], "job")
        self.assertEqual(trec["detail"]["exit_code"], 4)
        self.assertEqual(trec["detail"]["stdout_bytes"], job.stdout["bytes"])
        self.assertIn(jid, self._call("list"))

    def test_cancel_kills_the_child_tree(self):
        """~3 s."""
        jid, _ = self._start(_py("import time; time.sleep(60)"))
        deadline = time.monotonic() + 10
        job = self._job(jid)
        while job.status == "queued" and time.monotonic() < deadline:
            time.sleep(0.1)
            job = self._job(jid)
        self.assertEqual(job.status, "running")
        child_pid, start, source = job.child_pid, job.child_start_time, job.child_start_source
        self.assertTrue(sj.process_alive(child_pid, start, source))

        out = self._call("cancel", job_id=jid)
        self.assertIn(f"[c3_shell_job:cancel] {jid} child pid {child_pid} tree killed", out)
        job = self._wait_terminal(jid, timeout=10)
        self.assertEqual(job.status, "cancelled", out)
        self.assertTrue(job.cancel_requested)
        self.assertFalse(sj.process_alive(child_pid, start, source), "child still alive after cancel")
        self.assertTrue(job.output_id)                 # promoted even though it is empty
        self.assertIn("already cancelled", self._call("cancel", job_id=jid))

    def test_timeout_at_the_jobs_own_limit(self):
        """~4 s: a 2 s job timeout against a 30 s sleep."""
        jid, out = self._start(_py("import time; print('tick'); time.sleep(30)"), timeout=2)
        self.assertIn("(timeout 2s)", out)
        job = self._wait_terminal(jid, timeout=15)
        self.assertEqual(job.status, "timeout")
        self.assertTrue(job.timed_out)
        self.assertEqual(job.exit_code, -1)
        self.assertFalse(sj.process_alive(job.child_pid, job.child_start_time, job.child_start_source))
        status = self._call("status", job_id=jid)
        self.assertIn("(timed out at 2s)", status)
        self.assertIn("tick", self._call("tail", job_id=jid))

    def test_supervisor_survives_the_parent(self):
        """~5 s: the process that starts the job exits at once; the job still finishes."""
        cmd = _py("import time; time.sleep(2); print('survived')")
        payload = {"cmd": cmd, "exec_cmd": cmd, "env": {}, "secrets": {}, "cred_names": [], "tmpl_used": []}
        script = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"os.environ['C3_SHELL_OUT_DIR'] = {str(self.store_root)!r}\n"
            "from services.shell_jobs import JobStore\n"
            f"job = JobStore().start(project_path={str(self.project)!r}, session_id={self.session_id!r}, "
            f"cmd={cmd!r}, cwd={str(self.project)!r}, timeout_s=60, payload={payload!r}, wait_s=0)\n"
            "print(job.id)\n"
        )
        res = subprocess.run([PY, "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        jid = res.stdout.strip().splitlines()[-1]
        self._started.append(jid)
        self.assertRegex(jid, r"^j-[0-9a-f]{12}$")
        # the throwaway parent is gone; the supervisor is not
        job = self._wait_terminal(jid, timeout=20)
        self.assertEqual(job.status, "done", job.error)
        self.assertEqual(job.exit_code, 0)
        self.assertIn("survived", self._call("tail", job_id=jid))

    def test_injected_credential_never_lands_on_disk(self):
        """~2 s. The value travels to the supervisor on stdin only and is redacted in the spool."""
        value = "s3cr3t-value-Q9x7-never-on-disk"

        def fake_expand(cmd, project_path="."):
            used = ["TOK"] if "{{cred:TOK}}" in cmd else []
            return cmd.replace("{{cred:TOK}}", value), used, []

        def fake_resolve(names, project_path="."):
            return ({"TOK": value} if "TOK" in names else {}), [n for n in names if n != "TOK"]

        from services import credential_store as creds
        with patch.object(creds, "expand_templates", fake_expand), \
                patch.object(creds, "resolve", fake_resolve), \
                patch.object(creds, "list_entries", return_value={}), \
                patch.object(creds, "get_entry", return_value={"env_var": "TOK"}), \
                patch.object(creds, "touch_last_used", return_value=None):
            jid, out = self._start(
                _py("import os; print('env:' + os.environ['TOK']); print('tmpl:{{cred:TOK}}')"),
                env_creds="TOK")
        self.assertNotIn(value, out)
        self.assertIn("creds TOK", out)
        job = self._wait_terminal(jid, timeout=20)
        self.assertEqual(job.status, "done", job.error)
        self.assertNotIn(value, json.dumps(job.to_dict()))
        self.assertNotIn(value, " ".join(job.supervisor_argv))
        for path in self.store_root.rglob("*"):
            if path.is_file():
                self.assertNotIn(value.encode(), path.read_bytes(), f"{path.name} holds the secret")
        meta = ShellOutputStore().resolve(job.output_id, project_path=str(self.project),
                                          session_id=self.session_id, guard_check=_allow)
        text = ShellOutputStore().read(meta, "stdout")
        self.assertIn("env:[cred:TOK]", text)
        self.assertIn("tmpl:[cred:TOK]", text)
        activity = (self.project / ".c3" / "activity_log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(value, activity)
        self.assertIn('"creds": ["TOK"]', activity)


class TestProjectProxy(unittest.TestCase):
    def test_c3_project_refuses_to_proxy_jobs(self):
        from cli.tools.project import handle_project
        out = handle_project("shell_job", _svc("."), _finalize, project="whatever")
        self.assertIn("not proxied across projects", out)


if __name__ == "__main__":
    unittest.main()
