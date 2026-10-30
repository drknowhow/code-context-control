import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import schedule_api  # noqa: E402
from services import schedule_runner as sr  # noqa: E402
from services import schedule_store as ss  # noqa: E402
from services.shell_jobs import JobStore, _SpoolCapture  # noqa: E402
from services.shell_output import new_output_id  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "claude" / "stream_json_bash_echo.jsonl"
RESPONSE_KEYS = {"run_id", "status", "open", "job_id", "format", "source", "lines", "next", "total",
                 "dropped", "stderr_tail"}


class OutputCase(unittest.TestCase):
    """A real ScheduleStore and JobStore under a temp root; jobs are driven by hand, never spawned."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.proj = self.tmp / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        self.store = ss.ScheduleStore(self.tmp / "oracle")
        self.jobs = JobStore(self.tmp / "shell_out", apply_acl=False)
        self._prior = dict(get_scheduler=schedule_api._get_scheduler, get_store=schedule_api._get_store,
                           get_approvals=schedule_api._get_approvals, get_scanner=schedule_api._get_scanner,
                           get_cfg=schedule_api._get_cfg)
        schedule_api.configure(get_scheduler=lambda: SimpleNamespace(jobstore=self.jobs),
                               get_store=lambda: self.store, get_approvals=lambda: None,
                               get_scanner=lambda: None, get_cfg=lambda: {})

    def tearDown(self):
        schedule_api.configure(**self._prior)
        self._tmp.cleanup()

    def schedule(self, **over):
        fields = {"name": "report", "kind": "skill", "target": {"skill": "/daily_report"},
                  "project_path": str(self.proj), "when": {"type": "every", "seconds": 3600},
                  "autonomy": "autonomous"}
        fields.update(over)
        return self.store.create(fields)

    def running(self, schedule: dict, stdout: bytes = b"", stderr: bytes = b""):
        """A run whose job is ``running`` with a spool holding ``stdout``/``stderr``."""
        run = self.store.request_run(schedule["id"])
        job = self.jobs.create(project_path=str(self.proj), session_id=sr.SESSION_ID, cmd="claude -p x",
                               cwd=str(self.proj), timeout_s=60)
        oid = new_output_id()
        spool = self.jobs.output_store.spool_dir()
        out, err = spool / f"{oid}.stdout.part", spool / f"{oid}.stderr.part"
        out.write_bytes(stdout)
        err.write_bytes(stderr)
        job.status, job.output_id = "running", oid
        job.spool = {"stdout": str(out), "stderr": str(err)}
        self.jobs.save(job)
        run = self.store.update_run(run["run_id"], status="running", job_id=job.id,
                                    output_format=sr.output_format(schedule))
        return run, job

    def append(self, job, data: bytes) -> None:
        with open(job.spool["stdout"], "ab") as fh:
            fh.write(data)

    def end(self, job, status: str = "done", exit_code: int = 0):
        """What the supervisor does at the end: promote the spool, clear it, record the status."""
        capture = _SpoolCapture(job.output_id, job.spool["stdout"], job.spool["stderr"])
        meta = self.jobs.output_store.promote(capture, project_path=job.project_path, session_id=job.session_id,
                                              cmd=job.cmd_display, cwd=job.cwd, guard_paths=[],
                                              exit_code=exit_code, timed_out=status == "timeout", duration_ms=7)
        job.output_id, job.spool = meta.id, {}
        job.status, job.exit_code, job.duration_ms = status, exit_code, 7
        self.jobs.save(job)
        return self.jobs.find(job.id)

    def output(self, run_id: str, after=0, limit=500) -> dict:
        return schedule_api.run_output(run_id, after=after, limit=limit)


class TestSources(OutputCase):
    def test_line_numbers_hold_from_live_to_output_store_to_archive(self):
        fixture = FIXTURE.read_bytes()
        lines = fixture.splitlines(keepends=True)
        row = self.schedule()
        run, job = self.running(row, b"".join(lines[:5]) + lines[5][:40])

        live = self.output(run["run_id"])
        self.assertEqual(set(live), RESPONSE_KEYS)
        self.assertEqual((live["source"], live["open"], live["format"], live["job_id"]),
                         ("live", True, "claude-stream", job.id))
        self.assertEqual([r["n"] for r in live["lines"]], [1, 2, 3, 4, 5])
        self.assertEqual((live["next"], live["total"], live["dropped"], live["stderr_tail"]), (5, 5, 0, ""))

        self.append(job, lines[5][40:] + b"".join(lines[6:]))
        more = self.output(run["run_id"], after=live["next"])
        self.assertEqual([r["n"] for r in more["lines"]], [6, 7, 8])
        streamed = live["lines"] + more["lines"]
        self.assertEqual([r["text"] for r in streamed], fixture.decode("utf-8").split("\n")[:-1])

        job = self.end(job)
        kept = self.output(run["run_id"], after=0, limit=3)
        self.assertEqual((kept["source"], kept["open"], kept["next"], kept["total"]), ("output_store", True, 3, 8))
        self.assertEqual(kept["lines"], streamed[:3])

        done = sr.finalize(self.store, self.jobs, self.store.get_run(run["run_id"]), job, parser="claude")
        self.assertEqual((done["status"], done["output_lines"], done["output_dropped"]), ("done", 8, 0))
        self.assertEqual(done["cost_usd"], 0.01060426)
        self.assertIsNone(done["stderr_tail"])
        self.assertEqual((self.store.runs_dir / f"{run['run_id']}.out").read_bytes(), fixture)
        archived = self.output(run["run_id"], after=0)
        self.assertEqual((archived["source"], archived["open"], archived["status"]), ("archive", False, "done"))
        self.assertEqual(archived["lines"], streamed)
        self.assertEqual(self.output(run["run_id"], after=8)["lines"], [])
        self.assertEqual(self.output(run["run_id"], after=8)["next"], 8)

    def test_live_waits_for_a_partial_last_line(self):
        run, job = self.running(self.schedule(kind="command", target={"cmd": "build"}), b"one\ntwo")
        page = self.output(run["run_id"])
        self.assertEqual(([r["text"] for r in page["lines"]], page["total"], page["format"]),
                         (["one"], 1, "text"))
        self.append(job, b"\r\nthree\n")
        page = self.output(run["run_id"], after=1)
        self.assertEqual([(r["n"], r["text"]) for r in page["lines"]], [(2, "two"), (3, "three")])

    def test_terminal_output_counts_a_last_line_without_newline(self):
        run, job = self.running(self.schedule(), b"a\nb")
        self.end(job)
        page = self.output(run["run_id"])
        self.assertEqual(([r["text"] for r in page["lines"]], page["total"]), (["a", "b"], 2))

    def test_spool_promoted_between_reads_falls_through_to_output_store(self):
        run, job = self.running(self.schedule(), b"x\ny\n")
        stale = self.jobs.find(job.id)
        self.end(job)
        jobs = SimpleNamespace(find=mock.Mock(side_effect=[stale, self.jobs.find(job.id)]),
                               output_store=self.jobs.output_store)
        page = schedule_api.run_output(run["run_id"], jobstore=jobs)
        self.assertEqual((page["source"], [r["text"] for r in page["lines"]]), ("output_store", ["x", "y"]))

    def test_no_job_or_expired_output_is_none(self):
        row = self.schedule()
        claimed = self.store.request_run(row["id"])
        page = self.output(claimed["run_id"], after=4)
        self.assertEqual((page["source"], page["lines"], page["next"], page["total"], page["job_id"], page["open"]),
                         ("none", [], 4, 0, None, True))
        run, job = self.running(row, b"x\n")
        job = self.end(job)
        meta = self.jobs.output_store.resolve(job.output_id, project_path=job.project_path,
                                              session_id=job.session_id, guard_check=lambda _p: None)
        self.jobs.output_store.delete(meta)
        self.assertEqual(self.output(run["run_id"])["source"], "none")

    def test_unknown_or_malformed_run_is_404_and_bad_paging_400(self):
        for run_id in ("sr-0000000000", "..\\schedules", "../schedules", ""):
            with self.assertRaises(schedule_api.ApiError) as ctx:
                self.output(run_id)
            self.assertEqual(ctx.exception.status, 404)
        run = self.store.request_run(self.schedule()["id"])
        for bad in ({"after": "x"}, {"limit": "many"}):
            with self.assertRaises(schedule_api.ApiError) as ctx:
                self.output(run["run_id"], **bad)
            self.assertEqual(ctx.exception.status, 400)

    def test_format_follows_provider_and_kind(self):
        self.assertEqual(sr.output_format({"kind": "prompt", "provider": "claude"}), "claude-stream")
        self.assertEqual(sr.output_format({"kind": "skill"}), "claude-stream")
        self.assertEqual(sr.output_format({"kind": "prompt", "provider": "codex"}), "codex-json")
        for row in ({"kind": "prompt", "provider": "gemini"}, {"kind": "prompt", "provider": "grok"},
                    {"kind": "command", "provider": "claude"}):
            self.assertEqual(sr.output_format(row), "text")
        legacy = self.store.request_run(self.schedule(provider="codex")["id"])
        self.assertEqual(self.output(legacy["run_id"])["format"], "codex-json")
        self.store.update_run(legacy["run_id"], status="check_running")
        self.assertEqual(self.output(legacy["run_id"])["format"], "text")
        self.store.update_run(legacy["run_id"], status="claimed", output_format="claude-stream")
        self.assertEqual(self.output(legacy["run_id"])["format"], "claude-stream")

    def test_oracle_route(self):
        run, job = self.running(self.schedule(), b"l1\nl2\nl3\n")
        client = srv.app.test_client()
        resp = client.get(f"/api/schedules/runs/{run['run_id']}/output?after=1&limit=1")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertEqual(set(body), RESPONSE_KEYS)
        self.assertEqual((body["lines"], body["next"], body["total"]), ([{"n": 2, "text": "l2"}], 2, 3))
        self.assertEqual(client.get("/api/schedules/runs/sr-0000000000/output").status_code, 404)
        self.assertEqual(client.get(f"/api/schedules/runs/{run['run_id']}/output?after=x").status_code, 400)


class TestArchive(OutputCase):
    def test_cap_keeps_the_tail_from_a_line_start_and_numbers_from_dropped(self):
        body = b"".join(b"line-%03d\n" % i for i in range(1, 51))
        run, job = self.running(self.schedule(kind="command", target={"cmd": "x"}), body)
        job = self.end(job)
        before = self.output(run["run_id"], after=0, limit=50)["lines"]
        with mock.patch.object(sr, "ARCHIVE_MAX_BYTES", 100):
            done = sr.finalize(self.store, self.jobs, self.store.get_run(run["run_id"]), job, parser="command")
        archive = (self.store.runs_dir / f"{run['run_id']}.out").read_bytes()
        dropped = done["output_dropped"]
        self.assertEqual(done["output_lines"], 50)
        self.assertLessEqual(len(archive), 100)
        self.assertGreater(len(archive), 100 - 9)
        self.assertEqual(archive, body[body.index(b"line-%03d" % (dropped + 1)):])
        page = self.output(run["run_id"], after=0)
        self.assertEqual((page["source"], page["dropped"], page["total"]), ("archive", dropped, 50))
        self.assertEqual(page["lines"][0], {"n": dropped + 1, "text": "line-%03d" % (dropped + 1)})
        self.assertEqual(page["lines"], before[dropped:])

    def test_a_last_line_longer_than_the_cap_keeps_its_tail(self):
        body = b"head\n" + b"x" * 300
        run, job = self.running(self.schedule(kind="command", target={"cmd": "x"}), body)
        job = self.end(job)
        with mock.patch.object(sr, "ARCHIVE_MAX_BYTES", 100):
            done = sr.finalize(self.store, self.jobs, self.store.get_run(run["run_id"]), job, parser="command")
        self.assertEqual((done["output_lines"], done["output_dropped"]), (2, 1))
        self.assertEqual((self.store.runs_dir / f"{run['run_id']}.out").read_bytes(), b"x" * 100)
        self.assertEqual(self.output(run["run_id"])["lines"], [{"n": 2, "text": "x" * 100}])

    def test_stderr_tail_only_when_the_run_ends_badly(self):
        err = b"".join(b"err %d\n" % i for i in range(1, 101))
        failed, job = self.running(self.schedule(kind="command", target={"cmd": "x"}), b"out\n", err)
        job = self.end(job, status="failed", exit_code=2)
        done = sr.finalize(self.store, self.jobs, self.store.get_run(failed["run_id"]), job, parser="command")
        self.assertEqual(done["stderr_tail"].splitlines(), ["err %d" % i for i in range(41, 101)])
        self.assertEqual(self.output(failed["run_id"])["stderr_tail"], done["stderr_tail"])
        ok, job = self.running(self.schedule(name="ok", kind="command", target={"cmd": "x"}), b"out\n", err)
        job = self.end(job)
        done = sr.finalize(self.store, self.jobs, self.store.get_run(ok["run_id"]), job, parser="command")
        self.assertIsNone(done["stderr_tail"])
        self.assertEqual(self.output(ok["run_id"])["stderr_tail"], "")

    def test_stderr_tail_is_bounded_in_bytes(self):
        err = b"".join(b"%04d " % i + b"e" * 400 + b"\n" for i in range(1, 101))
        run, job = self.running(self.schedule(kind="command", target={"cmd": "x"}), b"", err)
        job = self.end(job, status="timeout", exit_code=137)
        done = sr.finalize(self.store, self.jobs, self.store.get_run(run["run_id"]), job, parser="command")
        self.assertLessEqual(len(done["stderr_tail"].encode()), sr.STDERR_TAIL_BYTES)
        self.assertTrue(done["stderr_tail"].startswith("00"))
        self.assertTrue(done["stderr_tail"].endswith("e"))

    def test_sweep_removes_the_archive_and_prompt_with_the_run(self):
        run, job = self.running(self.schedule(kind="command", target={"cmd": "x"}), b"out\n")
        job = self.end(job)
        done = sr.finalize(self.store, self.jobs, self.store.get_run(run["run_id"]), job, parser="command")
        prompt = self.store.runs_dir / f"{run['run_id']}.prompt.txt"
        prompt.write_text("p", encoding="utf-8")
        archive = self.store.runs_dir / f"{run['run_id']}.out"
        self.assertTrue(archive.is_file())
        later = ss.parse_iso(done["finished_at"]) + timedelta(days=31)
        self.assertEqual(self.store.sweep_runs(30, later), 1)
        self.assertFalse(archive.exists())
        self.assertFalse(prompt.exists())


class TestPageLines(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "out"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_long_line_is_clipped_and_flagged(self):
        self.path.write_bytes(b"short\n" + b"y" * (300 * 1024) + b"\nafter\n")
        lines, total = sr.page_lines(self.path)
        self.assertEqual(total, 3)
        self.assertEqual(lines[0], {"n": 1, "text": "short"})
        self.assertEqual((lines[1]["n"], len(lines[1]["text"]), lines[1]["clipped"]), (2, sr.OUTPUT_LINE_CLIP, True))
        self.assertEqual(lines[2], {"n": 3, "text": "after"})
        exact = Path(self._tmp.name) / "exact"
        exact.write_bytes(b"z" * sr.OUTPUT_LINE_CLIP + b"\n")
        self.assertNotIn("clipped", sr.page_lines(exact)[0][0])

    def test_limit_and_byte_budget_stop_the_page(self):
        self.path.write_bytes(b"".join(b"%d\n" % i for i in range(1, 11)))
        lines, total = sr.page_lines(self.path, after=2, limit=3)
        self.assertEqual(([r["n"] for r in lines], total), ([3, 4, 5], 10))
        lines, _ = sr.page_lines(self.path, after=0, max_bytes=5)
        self.assertEqual([r["text"] for r in lines], ["1", "2"])
        lines, _ = sr.page_lines(self.path, after=0, max_bytes=1)
        self.assertEqual([r["text"] for r in lines], ["1"])

    def test_skip_crosses_chunks_and_long_lines(self):
        with mock.patch.object(sr, "_CHUNK", 8):
            self.path.write_bytes(b"a\n" + b"b" * 30 + b"\nc\nd\n")
            lines, total = sr.page_lines(self.path, after=2)
            self.assertEqual(([(r["n"], r["text"]) for r in lines], total), ([(3, "c"), (4, "d")], 4))
            lines, total = sr.page_lines(self.path, first_n=11, after=12)
            self.assertEqual(([r["n"] for r in lines], total), ([13, 14], 14))

    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            sr.page_lines(self.path)


class TestRunOutputLimit(OutputCase):
    def test_limit_clamps_to_one_and_two_thousand(self):
        run, job = self.running(self.schedule(kind="command", target={"cmd": "x"}),
                                b"".join(b"%d\n" % i for i in range(1, 2501)))
        self.assertEqual(len(self.output(run["run_id"], limit=5000)["lines"]), 2000)
        self.assertEqual(len(self.output(run["run_id"], limit=0)["lines"]), 1)
        self.assertEqual(len(self.output(run["run_id"], limit=None)["lines"]), 500)

    def test_a_page_holds_about_one_mebibyte(self):
        line = b"q" * (200 * 1024) + b"\n"
        run, job = self.running(self.schedule(kind="command", target={"cmd": "x"}), line * 12)
        page = self.output(run["run_id"])
        self.assertEqual(len(page["lines"]), 5)
        self.assertEqual((page["next"], page["total"]), (5, 12))


if __name__ == "__main__":
    unittest.main()
