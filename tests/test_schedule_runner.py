import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from services import schedule_runner as sr
from services import schedule_store as ss

REPO = Path(__file__).resolve().parents[1]


def sched(**over):
    row = {
        "id": "sch-abc", "name": "report", "kind": "skill", "target": {"skill": "/daily_report", "args": "weekly"},
        "provider": "claude", "model": "", "effort": "", "permission_mode": "acceptEdits",
        "allowed_tools": "", "max_budget_usd": 0, "project_path": str(REPO), "cwd": str(REPO),
        "timeout_s": 900, "check_command": "",
    }
    row.update(over)
    return row


class FakeJob(SimpleNamespace):
    @property
    def terminal(self):
        return self.status in ("done", "failed", "timeout", "cancelled", "lost")


class FakeJobStore:
    def __init__(self, status="running", stdout=""):
        self.status = status
        self.stdout = stdout
        self.calls = []
        self.output_store = self

    def start(self, **kw):
        self.calls.append(kw)
        return FakeJob(id="j-1", status=self.status, exit_code=None, error="", created_at="2026-10-13T09:00:00+00:00",
                       output_id="o-1", duration_ms=5, project_path=kw["project_path"], session_id=kw["session_id"])

    def resolve(self, output_id, **kw):
        path = Path(tempfile.mkdtemp()) / "stdout"
        path.write_text(self.stdout, encoding="utf-8")
        return SimpleNamespace(stream_path=lambda stream: path)


class TestPlan(unittest.TestCase):
    def setUp(self):
        self.which = patch("cli.tools.delegate._which", side_effect=lambda n: f"/bin/{n}")
        self.which.start()

    def tearDown(self):
        self.which.stop()

    def test_claude_skill_run(self):
        fire = sr.plan(sched(model="sonnet", effort="medium", max_budget_usd=0.5, allowed_tools="Read Edit"))
        self.assertEqual(fire.argv[:5], ["/bin/claude", "-p", "--output-format", "json", "--no-session-persistence"])
        self.assertNotIn("--safe-mode", fire.argv)
        self.assertIn("--permission-mode", fire.argv)
        self.assertEqual(fire.argv[fire.argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(fire.argv[fire.argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(fire.argv[fire.argv.index("--model") + 1], "sonnet")
        self.assertEqual(fire.argv[fire.argv.index("--effort") + 1], "medium")
        self.assertEqual(fire.argv[fire.argv.index("--max-budget-usd") + 1], "0.5")
        self.assertEqual(fire.argv[fire.argv.index("--allowedTools") + 1], "Read Edit")
        self.assertEqual(fire.stdin_text, "/daily_report weekly")
        self.assertIn("CLAUDECODE", fire.env_drop)
        self.assertEqual(fire.parser, "claude")
        self.assertEqual(fire.timeout_s, 900)
        payload = fire.payload()
        self.assertEqual(payload["argv"], fire.argv)
        self.assertEqual(payload["stdin_text"], fire.stdin_text)
        self.assertIn("CLAUDECODE", payload["env_drop"])

    def test_claude_omits_unset_flags_and_rejects_bad_model(self):
        fire = sr.plan(sched(effort="ultra"))
        for flag in ("--model", "--effort", "--max-budget-usd", "--allowedTools"):
            self.assertNotIn(flag, fire.argv)
        with self.assertRaises(sr.PlanError):
            sr.plan(sched(model="sonnet; rm -rf"))

    def test_prompt_kind_uses_prompt_text(self):
        fire = sr.plan(sched(kind="prompt", target={"prompt": "summarise the week"}))
        self.assertEqual(fire.stdin_text, "summarise the week")

    def test_command_and_check(self):
        fire = sr.plan(sched(kind="command", target={"cmd": "git fetch --all"}))
        self.assertEqual((fire.cmd, fire.argv, fire.parser), ("git fetch --all", None, "command"))
        check = sr.plan(sched(check_command="test -s inbox.txt", timeout_s=7200), check=True)
        self.assertEqual((check.cmd, check.timeout_s), ("test -s inbox.txt", 600))
        with self.assertRaises(sr.PlanError):
            sr.plan(sched(), check=True)

    def test_codex_gemini_grok_shapes(self):
        codex = sr.plan(sched(provider="codex", model="o4-mini", effort="max"))
        self.assertEqual(codex.argv[:4], ["/bin/codex", "exec", "-m", "o4-mini"])
        self.assertIn("model_reasoning_effort=xhigh", codex.argv)
        self.assertEqual(codex.argv[-1], "-")
        self.assertEqual(codex.stdin_text, "/daily_report weekly")
        gemini = sr.plan(sched(provider="gemini"))
        self.assertEqual(gemini.argv[0], "/bin/gemini")
        self.assertNotIn("-p", gemini.argv)
        self.assertEqual(gemini.stdin_text, "/daily_report weekly")
        with self.assertRaises(sr.PlanError):
            sr.plan(sched(provider="grok"))
        grok = sr.plan(sched(provider="grok", effort="low"), prompt_file="p.txt")
        self.assertEqual(grok.argv[:3], ["/bin/grok", "--prompt-file", "p.txt"])
        self.assertIn("--yolo", grok.argv)
        self.assertIsNone(grok.stdin_text)

    def test_missing_cli(self):
        self.which.stop()
        with patch("cli.tools.delegate._which", return_value=None):
            with self.assertRaises(sr.PlanError):
                sr.plan(sched())
        self.which.start()


CLAUDE_JSON = json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "result": "Report sent.",
    "total_cost_usd": 0.0421, "num_turns": 3,
    "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": 50},
    "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 1200}},
})


class TestParseResult(unittest.TestCase):
    def test_claude(self):
        out = sr.parse_result("claude", "noise\n" + CLAUDE_JSON)
        self.assertEqual(out["result_excerpt"], "Report sent.")
        self.assertEqual(out["cost_usd"], 0.0421)
        self.assertEqual(out["turns"], 3)
        self.assertEqual(out["tokens"], {"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 50})
        self.assertEqual(out["model"], "claude-sonnet-5-5")

    def test_claude_error_payload_keeps_reason(self):
        out = sr.parse_result("claude", json.dumps({"is_error": True, "result": "budget exceeded"}))
        self.assertIn("budget exceeded", out["result_excerpt"])

    def test_codex_last_agent_message(self):
        lines = [json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
                 json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final"}})]
        self.assertEqual(sr.parse_result("codex", "\n".join(lines))["result_excerpt"], "final")

    def test_gemini_tokens(self):
        blob = json.dumps({"response": "ok", "stats": {"models": {"gemini-x": {"tokens": {"input": 10, "candidates": 4}}}}})
        out = sr.parse_result("gemini", "MCP ready\n" + blob)
        self.assertEqual((out["result_excerpt"], out["tokens"], out["model"]),
                         ("ok", {"input_tokens": 10, "output_tokens": 4}, "gemini-x"))

    def test_command_is_verbatim(self):
        self.assertEqual(sr.parse_result("command", " 3 files\n")["result_excerpt"], "3 files")


class TestDispatchFinalize(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ss.ScheduleStore(Path(self._tmp.name))
        self.row = self.store.create({"name": "r", "kind": "command", "target": {"cmd": "echo hi"},
                                      "project_path": str(REPO), "when": {"type": "every", "seconds": 600},
                                      "autonomy": "autonomous"})
        self.run = self.store.request_run(self.row["id"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_dispatch_records_job(self):
        jobs = FakeJobStore()
        run = sr.dispatch(self.store, jobs, self.row, self.run)
        self.assertEqual((run["status"], run["job_id"]), ("dispatched", "j-1"))
        call = jobs.calls[0]
        self.assertEqual((call["session_id"], call["cwd"], call["timeout_s"]), (sr.SESSION_ID, str(REPO), 1800))
        self.assertEqual(call["payload"]["cmd"], "echo hi")

    def test_check_dispatch(self):
        row = dict(self.row, check_command="exit 1")
        run = sr.dispatch(self.store, FakeJobStore(), row, self.run, check=True)
        self.assertEqual(run["status"], "check_running")

    def test_spawn_failure_and_plan_error_finish_the_run(self):
        failed = sr.dispatch(self.store, FakeJobStore(status="failed"), self.row, self.run)
        self.assertEqual(failed["status"], "failed")
        second = self.store.request_run(self.row["id"])
        bad = sr.dispatch(self.store, FakeJobStore(), dict(self.row, target={"cmd": ""}), second)
        self.assertEqual(bad["status"], "failed")
        self.assertIn("target.cmd", bad["error"])

    def test_finalize_folds_cost(self):
        jobs = FakeJobStore(stdout=CLAUDE_JSON)
        job = FakeJob(id="j-9", status="done", exit_code=0, duration_ms=1234, output_id="o-1", error="",
                      project_path=str(REPO), session_id=sr.SESSION_ID)
        run = sr.finalize(self.store, jobs, self.run, job, parser="claude")
        self.assertEqual((run["status"], run["exit_code"], run["duration_ms"], run["cost_usd"]),
                         ("done", 0, 1234, 0.0421))
        self.assertEqual(self.store.get(self.row["id"])["last_status"], "done")

    def test_finalize_maps_timeout_and_refuses_open_job(self):
        job = FakeJob(id="j-2", status="timeout", exit_code=None, duration_ms=10, output_id="", error="killed",
                      project_path=str(REPO), session_id=sr.SESSION_ID)
        run = sr.finalize(self.store, FakeJobStore(), self.run, job)
        self.assertEqual((run["status"], run["error"]), ("timeout", "killed"))
        with self.assertRaises(ValueError):
            sr.finalize(self.store, FakeJobStore(), self.run, FakeJob(id="j", status="running"))


if __name__ == "__main__":
    unittest.main()
