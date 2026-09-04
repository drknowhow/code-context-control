"""S0 of the c3_shell remediation: the renderer is a pure function and
telemetry finally records what a call did.

Measured 2026-09-04 over every registered project's .c3/tool_telemetry.jsonl:
duration_ms was null on 100% of c3_shell records although handle_shell had
measured it on every call, and nothing recorded exit code, timeout, stream
sizes or the longest line — so the single-line monsters (a 1.4M-token grep
hit on a minified bundle) were invisible to any analysis. These tests pin:

- render_shell_response reproduces the body handle_shell always produced,
  from a result dict alone (no subprocess), so the shell-eval harness can
  feed captured streams through the live code path;
- its stats measure the RAW streams (before the auto-filter) and the longest
  line, and classify the command;
- handle_shell hands duration_ms and the stats to the telemetry record.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools import shell as shell_mod  # noqa: E402
from services.output_filter import OutputFilter  # noqa: E402
from services.session_manager import SessionManager  # noqa: E402
from services.telemetry import (  # noqa: E402
    SHELL_BUDGET_BYTES,
    aggregate_tool_telemetry,
    append_telemetry_record,
)


def _result(stdout="", stderr="", exit_code=0, timed_out=False, duration_ms=7):
    return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
            "duration_ms": duration_ms, "timed_out": timed_out, "shell": "git-bash"}


def _svc(tmp: str, session_mgr=None):
    return SimpleNamespace(project_path=tmp, activity_log=None, edit_ledger=None,
                           output_filter=OutputFilter(), session_mgr=session_mgr,
                           hybrid_config={})


class TestRenderer(unittest.TestCase):
    def test_small_output_renders_the_classic_body(self):
        body, stats = shell_mod.render_shell_response(
            "echo hi", _result(stdout="hi\n", stderr="warn\n"), _svc("."))
        self.assertEqual(
            body,
            "[c3_shell:OK] 7ms\n$ echo hi\n--- stdout ---\nhi\n--- stderr ---\nwarn\n")
        self.assertEqual(stats["stdout_bytes"], 3)
        self.assertEqual(stats["stderr_bytes"], 5)
        self.assertEqual(stats["longest_line"], 4)
        self.assertFalse(stats["filtered"])
        self.assertFalse(stats["spilled"])
        self.assertIsNone(stats["output_id"])
        self.assertEqual(stats["cmd_class"], "echo")
        self.assertEqual(stats["response_bytes"], len(body.encode()))
        self.assertGreater(stats["response_tokens"], 0)

    def test_status_variants_and_sections(self):
        body, _ = shell_mod.render_shell_response(
            "false", _result(exit_code=7), _svc("."))
        self.assertTrue(body.startswith("[c3_shell:FAIL(7)]"))
        body, _ = shell_mod.render_shell_response(
            "sleep 9", _result(exit_code=-1, timed_out=True), _svc("."))
        self.assertTrue(body.startswith("[c3_shell:TIMEOUT]"))
        body, _ = shell_mod.render_shell_response(
            "git commit -m x", _result(stdout="ok\n"), _svc("."),
            warn="[c3_shell:warn] w\n", capped_note="[c3_shell:capped] c\n",
            touched_files=["a.py"], cred_names=["TOK"], swept_ghosts=["x"])
        self.assertTrue(body.startswith("[c3_shell:warn] w\n[c3_shell:capped] c\n[c3_shell:OK]"))
        for section in ("--- ledger ---\nlogged 1 file(s)", "--- creds ---\ninjected: TOK",
                        "--- ghost-sweep ---\nremoved 1 stray"):
            self.assertIn(section, body)

    def test_stats_measure_the_raw_streams_not_the_filtered_body(self):
        long = "\n".join(f"line {i}" for i in range(200)) + "\n"
        body, stats = shell_mod.render_shell_response("ls -R", _result(stdout=long), _svc("."))
        self.assertTrue(stats["filtered"])
        self.assertIn("[stdout filtered]", body)
        self.assertEqual(stats["stdout_bytes"], len(long.encode()))
        self.assertLess(stats["response_bytes"], stats["stdout_bytes"])

    def test_filter_off_or_short_output_is_untouched(self):
        long = "\n".join(f"line {i}" for i in range(200)) + "\n"
        body, stats = shell_mod.render_shell_response(
            "ls -R", _result(stdout=long), _svc("."), filter_output=False)
        self.assertFalse(stats["filtered"])
        self.assertIn("line 150", body)
        short = "\n".join(f"line {i}" for i in range(20)) + "\n"
        body, stats = shell_mod.render_shell_response("ls", _result(stdout=short), _svc("."))
        self.assertFalse(stats["filtered"])
        self.assertIn("line 19", body)

    def test_longest_line_sees_the_single_line_monster(self):
        monster = "x" * 50_000 + "\n"  # one grep hit on a minified bundle
        _, stats = shell_mod.render_shell_response(
            "grep -rn foo dist/", _result(stdout=monster), _svc("."))
        self.assertEqual(stats["longest_line"], 50_000)
        self.assertFalse(stats["filtered"])  # the newline trigger never fires: the defect S1 fixes
        self.assertEqual(stats["cmd_class"], "file-read")


class TestCommandClass(unittest.TestCase):
    def test_classes(self):
        cases = {
            'cd "U:/x y" && grep -rn foo js/ | head': "file-read",
            "cat CHANGELOG.md": "file-read",
            "python -m pytest tests -q": "tests",
            "npm test": "tests",
            "git status --porcelain": "git",
            "python - <<'EOF'\nprint(1)\nEOF": "python",
            "pip install x": "python",
            "npx tsc --noEmit": "build",
            "curl -s http://127.0.0.1:3330/api/health": "ops",
            "echo hi": "echo",
            "gh pr list": "gh",
            "c3 --version": "c3",
            "PYTHONPATH= c3 upgrade --check": "c3",
            "./scripts/run.sh": "other",
            "": "other",
        }
        for cmd, want in cases.items():
            self.assertEqual(shell_mod._cmd_class(cmd), want, cmd)


class TestTelemetry(unittest.TestCase):
    def test_handle_shell_records_duration_and_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(tmp)
            sm.start_session("t")
            svc = _svc(tmp, session_mgr=sm)

            def finalize(name, args, resp, summ, **kw):
                sm.log_tool_call(name, args, summ)
                sm.track_response(name, resp, kw.get("response_tokens", 0))
                return resp

            fake = _result(stdout="hello\n", exit_code=0, duration_ms=123)
            with patch.object(shell_mod, "_run_sync", return_value=fake):
                out = asyncio.run(shell_mod.handle_shell(
                    "echo hello", "", 10, True, False, svc, finalize))
            self.assertIn("hello", out)
            rows = [json.loads(line) for line in
                    open(Path(tmp) / ".c3" / "tool_telemetry.jsonl", encoding="utf-8")]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["tool"], "c3_shell")
            self.assertEqual(row["duration_ms"], 123)
            detail = row["detail"]
            self.assertEqual(detail["exit_code"], 0)
            self.assertFalse(detail["timed_out"])
            self.assertEqual(detail["stdout_bytes"], 6)
            self.assertEqual(detail["stderr_bytes"], 0)
            self.assertEqual(detail["longest_line"], 5)
            self.assertEqual(detail["cmd_class"], "echo")
            self.assertFalse(detail["filtered"])
            self.assertFalse(detail["spilled"])
            self.assertEqual(row["response_tokens"], detail["response_tokens"])

    def test_detail_is_absent_for_tools_that_do_not_send_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(tmp)
            sm.start_session("t")
            sm.log_tool_call("c3_read", {"file_path": "a.py"})
            sm.record_tool_tokens("c3_read", raw_tokens=100, optimized_tokens=10, duration_ms=4)
            sm.track_response("c3_read", "x" * 40, 10)
            row = json.loads(open(Path(tmp) / ".c3" / "tool_telemetry.jsonl",
                                  encoding="utf-8").readline())
            self.assertNotIn("detail", row)
            self.assertEqual(row["duration_ms"], 4)

    def test_a_stale_pending_from_another_tool_never_leaks_its_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = SessionManager(tmp)
            sm.start_session("t")
            sm.record_tool_tokens("c3_shell", duration_ms=9, detail={"exit_code": 1})
            sm.log_tool_call("c3_read", {"file_path": "a.py"})
            sm.track_response("c3_read", "x" * 40, 10)
            row = json.loads(open(Path(tmp) / ".c3" / "tool_telemetry.jsonl",
                                  encoding="utf-8").readline())
            self.assertEqual(row["tool"], "c3_read")
            self.assertNotIn("detail", row)
            self.assertIsNone(row["duration_ms"])


class TestShellAggregate(unittest.TestCase):
    def _rec(self, **detail):
        base = {"exit_code": 0, "timed_out": False, "stdout_bytes": 10, "stderr_bytes": 0,
                "longest_line": 5, "filtered": False, "spilled": False, "output_id": None,
                "cmd_class": "file-read", "response_bytes": 100, "response_tokens": 25}
        base.update(detail)
        return {"tool": "c3_shell", "response_tokens": base["response_tokens"],
                "duration_ms": detail.get("duration_ms", 50), "detail": base}

    def test_shell_by_class_is_the_before_after_instrument(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rec in (
                self._rec(),
                self._rec(response_bytes=SHELL_BUDGET_BYTES + 1, longest_line=400_000,
                          stdout_bytes=1_400_000, duration_ms=900),
                self._rec(cmd_class="tests", exit_code=1, filtered=True, duration_ms=66_000),
                self._rec(cmd_class="ops", exit_code=-1, timed_out=True),
            ):
                append_telemetry_record(tmp, rec)
            # A pre-2.111.0 record without detail must not break or count.
            append_telemetry_record(tmp, {"tool": "c3_shell", "response_tokens": 5})
            append_telemetry_record(tmp, {"tool": "c3_read", "response_tokens": 5,
                                          "detail": {"cmd_class": "file-read"}})
            agg = aggregate_tool_telemetry(tmp, days=0)
            classes = agg["shell_by_class"]
            self.assertEqual(sorted(classes), ["file-read", "ops", "tests"])
            fr = classes["file-read"]
            self.assertEqual(fr["calls"], 2)
            self.assertEqual(fr["over_budget"], 1)
            self.assertEqual(fr["longest_line_max"], 400_000)
            self.assertEqual(fr["stdout_bytes"], 1_400_010)
            self.assertEqual(fr["p50_duration_ms"], 900.0)
            self.assertEqual(classes["tests"]["failures"], 1)
            self.assertEqual(classes["tests"]["filtered"], 1)
            self.assertEqual(classes["ops"]["timeouts"], 1)
            self.assertEqual(classes["ops"]["failures"], 1)
            self.assertEqual(agg["shell_budget_bytes"], SHELL_BUDGET_BYTES)
            self.assertEqual(agg["by_tool"]["c3_shell"]["calls"], 5)

    def test_no_shell_records_means_an_empty_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(aggregate_tool_telemetry(tmp, days=0)["shell_by_class"], {})


if __name__ == "__main__":
    unittest.main()
