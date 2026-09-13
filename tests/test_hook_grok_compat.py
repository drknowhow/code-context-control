"""Grok Build hook compatibility, driven by payloads captured from grok 1.0.30.

tests/fixtures/grok/hook_payloads_1.0.30.json holds the raw stdin of a real
headless grok run (scratch project, project hook, every tool class exercised).
The contract pinned here, from Grok's hook docs and that run:

  * input: snake_case aliases carry Grok-native values — read_file/target_file,
    search_replace, write, run_terminal_command, grep, list_dir, and MCP calls
    as `c3__<tool>` with the real arguments wrapped in tool_input.tool_input;
  * output: only hookSpecificOutput {hookEventName, permissionDecision,
    permissionDecisionReason, additionalContext} for PreToolUse/PostToolUse;
  * Stop context keeps the agent working and prompt context is discarded, so
    C3 prints NOTHING for stop / prompt / start / compact / end.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

from cli import hook_dispatch  # noqa: E402
from cli._hook_utils import HOST_CLAUDE, HOST_CODEX, HOST_GROK, detect_host, response_text_failed  # noqa: E402
from cli.hook_dispatch import dispatch, merge_outputs  # noqa: E402
from core.grok_payload import response_text, translate  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "grok"
PAYLOADS = json.loads((FIXTURES / "hook_payloads_1.0.30.json").read_text(encoding="utf-8"))["payloads"]

_HSO_KEYS = {"hookEventName", "permissionDecision", "permissionDecisionReason", "additionalContext"}
_EVENT_NAMES = {"pretool": "PreToolUse", "posttool": "PostToolUse"}


def fixture(key: str, **overrides) -> dict:
    payload = json.loads(json.dumps(PAYLOADS[key]))
    payload.update(overrides)
    return payload


def assert_grok_valid(test: unittest.TestCase, event: str, out) -> None:
    if event not in _EVENT_NAMES:
        test.assertIsNone(out, f"{event} must print nothing on Grok: {out!r}")
        return
    if out is None:
        return
    test.assertEqual(set(out), {"hookSpecificOutput"}, out)
    hso = out["hookSpecificOutput"]
    test.assertTrue(set(hso) <= _HSO_KEYS, hso)
    test.assertEqual(hso["hookEventName"], _EVENT_NAMES[event])
    if "permissionDecision" in hso:
        test.assertEqual(hso["permissionDecision"], "deny")
        test.assertTrue(hso.get("permissionDecisionReason"))


def with_paths(payload: dict, project: Path, file_path: Path) -> dict:
    """Re-root a captured payload onto a test project."""
    payload["cwd"] = str(project)
    payload["workspaceRoot"] = str(project)
    for key in ("tool_input", "toolInput"):
        if isinstance(payload.get(key), dict):
            for field in ("file_path", "target_file"):
                if field in payload[key]:
                    payload[key][field] = str(file_path)
    return payload


class TestDetectAndTranslate(unittest.TestCase):
    def test_every_captured_payload_detects_as_grok(self):
        for key, payload in PAYLOADS.items():
            self.assertEqual(detect_host(payload), HOST_GROK, key)

    def test_other_hosts_are_unchanged(self):
        self.assertEqual(detect_host({"tool_name": "Read", "session_id": "s"}), HOST_CLAUDE)
        self.assertEqual(detect_host({"turn_id": "t", "hookEventName": "x"}), HOST_CODEX)
        self.assertEqual(detect_host({}, explicit_host="grok"), HOST_GROK)

    def test_builtin_tools_map_to_claude_names_and_keys(self):
        cases = {
            "PreToolUse:read_file": ("Read", "file_path"),
            "PreToolUse:write": ("Write", "file_path"),
            "PreToolUse:search_replace": ("Edit", "file_path"),
            "PreToolUse:run_terminal_command": ("Bash", "command"),
            "PreToolUse:grep": ("Grep", "pattern"),
            "PreToolUse:list_dir": ("Glob", "path"),
        }
        for key, (name, input_key) in cases.items():
            out = translate(PAYLOADS[key])
            self.assertEqual(out["tool_name"], name, key)
            self.assertIn(input_key, out["tool_input"], key)
            self.assertEqual(out["_c3_host"], "grok")
            self.assertEqual(out["_c3_original_tool"], PAYLOADS[key]["tool_name"])
        read = translate(PAYLOADS["PreToolUse:read_file"])["tool_input"]["file_path"]
        self.assertTrue(Path(read).is_absolute(), read)  # relative target_file joined to cwd
        self.assertEqual(PAYLOADS["PreToolUse:read_file"]["tool_input"], {"target_file": "README.md"})

    def test_empty_old_string_is_a_create(self):
        payload = fixture("PreToolUse:search_replace")
        payload["tool_input"] = {**payload["tool_input"], "old_string": ""}
        self.assertEqual(translate(payload)["tool_name"], "Write")

    def test_mcp_call_unwraps_use_tool_arguments(self):
        out = translate(PAYLOADS["PreToolUse:c3__c3_status"])
        self.assertEqual(out["tool_name"], "mcp__c3__c3_status")
        self.assertEqual(out["tool_input"], {})
        payload = fixture("PreToolUse:c3__c3_status", tool_name="c3__c3_read")
        payload["tool_input"] = {"tool_name": "c3__c3_read", "tool_input": {"file_path": "a.py"}}
        self.assertEqual(translate(payload)["tool_input"], {"file_path": "a.py"})

    def test_search_tool_and_session_events_pass_through(self):
        self.assertEqual(translate(PAYLOADS["PreToolUse:search_tool"])["tool_name"], "search_tool")
        stop = translate(PAYLOADS["Stop"])
        self.assertNotIn("tool_name", PAYLOADS["Stop"])
        self.assertEqual(stop["_c3_host"], "grok")

    def test_tagged_results_flatten_to_model_text(self):
        self.assertIn("hello world", translate(PAYLOADS["PostToolUse:read_file"])["tool_response"])
        self.assertIn("terminal-ok", translate(PAYLOADS["PostToolUse:run_terminal_command"])["tool_response"])
        self.assertIn("alpha", translate(PAYLOADS["PostToolUse:grep"])["tool_response"])
        self.assertIn("a.txt", translate(PAYLOADS["PostToolUse:list_dir"])["tool_response"])
        self.assertIn("updated successfully", translate(PAYLOADS["PostToolUse:search_replace"])["tool_response"])
        mcp = translate(PAYLOADS["PostToolUse:c3__c3_status"])
        self.assertTrue(mcp["tool_response"].startswith("[ctx_status]"))
        self.assertFalse(_hook_utils.tool_response_failed(mcp))

    def test_mcp_error_arm_reads_as_failure(self):
        failed = {"type": "MCP", "tool_name": "c3_read", "server_name": "c3",
                  "output": {"ErrorOutput": "File not found"}}
        self.assertTrue(response_text_failed(response_text(failed)))
        self.assertEqual(response_text("already a string"), "already a string")


class TestGrokOutputShape(unittest.TestCase):
    def test_deny_carries_only_the_verdict(self):
        deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                       "permissionDecisionReason": "use c3_edit"}}
        out = merge_outputs([{"additionalContext": "hint"}, deny], [], event="pretool", host=HOST_GROK)
        assert_grok_valid(self, "pretool", out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], "use c3_edit")
        self.assertNotIn("additionalContext", out["hookSpecificOutput"])

    def test_tool_result_and_text_fold_into_context(self):
        out = merge_outputs([{"tool_result": "filtered"}, {"_text": "note"}], ["warn"],
                            event="posttool", host=HOST_GROK)
        assert_grok_valid(self, "posttool", out)
        self.assertEqual(out["hookSpecificOutput"]["additionalContext"], "filtered\nwarn\nnote")

    def test_passive_and_looping_events_print_nothing(self):
        for event in ("stop", "prompt", "start", "compact", "end"):
            out = merge_outputs([{"additionalContext": "x", "_text": "y"}], ["z"], event=event, host=HOST_GROK)
            self.assertIsNone(out, event)


class TestGrokDispatchEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        (self.tmp / ".c3" / "config.json").write_text(
            json.dumps({"enforcement": {"mode": "strict"}}), encoding="utf-8")
        self.target = self.tmp / "a.txt"
        self.target.write_text("alpha beta\n", encoding="utf-8")
        self._saved_cache = dict(hook_dispatch._RUN_CACHE)
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        hook_dispatch._RUN_CACHE.clear()
        hook_dispatch._RUN_CACHE.update(self._saved_cache)
        _hook_utils.drain_state_warnings()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _payload(self, key, **overrides):
        return with_paths(fixture(key, **overrides), self.tmp, self.target)

    def test_native_edit_before_c3_is_denied(self):
        out = dispatch("pretool", self._payload("PreToolUse:search_replace"), project_path=self.tmp)
        assert_grok_valid(self, "pretool", out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_c3_read_via_use_tool_unlocks_the_edit(self):
        post = self._payload("PostToolUse:c3__c3_status", tool_name="c3__c3_read")
        post["tool_input"] = {"tool_name": "c3__c3_read", "tool_input": {"file_path": str(self.target)}}
        post["tool_response"] = {"type": "MCP", "tool_name": "c3_read", "server_name": "c3",
                                 "output": {"OkayOutput": "a.txt (1L text)\nalpha beta"}}
        assert_grok_valid(self, "posttool", dispatch("posttool", post, project_path=self.tmp))
        state = _hook_utils.load_enforcement_state(self.tmp)
        self.assertEqual(state["last_c3_call"]["tool"], "c3_read")
        out = dispatch("pretool", self._payload("PreToolUse:search_replace"), project_path=self.tmp)
        assert_grok_valid(self, "pretool", out)
        if out is not None:
            self.assertNotIn("permissionDecision", out["hookSpecificOutput"])

    def test_failed_c3_call_does_not_unlock(self):
        # Grok's non-Okay MCP arm must read as a failure, so neither the signal
        # nor hook_edit_unlock (both failure-aware) credits the call.
        post = self._payload("PostToolUse:c3__c3_status", tool_name="c3__c3_compress")
        post["tool_input"] = {"tool_name": "c3__c3_compress", "tool_input": {"file_path": str(self.target)}}
        post["tool_response"] = {"type": "MCP", "output": {"ErrorOutput": "File not found"}}
        dispatch("posttool", post, project_path=self.tmp)
        state = _hook_utils.load_enforcement_state(self.tmp)
        self.assertIsNone(state.get("last_c3_call"))
        self.assertNotIn(_hook_utils.canonical_key(self.target), state.get("unlocked_files", {}))
        out = dispatch("pretool", self._payload("PreToolUse:search_replace"), project_path=self.tmp)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_read_advisory_is_valid(self):
        out = dispatch("pretool", self._payload("PreToolUse:read_file"), project_path=self.tmp)
        assert_grok_valid(self, "pretool", out)
        if out is not None:
            self.assertIn("[c3:hint]", out["hookSpecificOutput"]["additionalContext"])

    def test_bash_never_reaches_hook_filter(self):
        def _boom(payload, project_path=None):
            raise AssertionError("hook_filter must not run on Grok")
        hook_dispatch._RUN_CACHE["hook_filter"] = (_boom, "")
        payload = self._payload("PostToolUse:run_terminal_command")
        out = dispatch("posttool", payload, project_path=self.tmp)
        assert_grok_valid(self, "posttool", out)

    def test_stop_prints_nothing_even_when_sub_hooks_speak(self):
        hook_dispatch._RUN_CACHE["hook_auto_snapshot"] = (lambda p, pp=None: {"additionalContext": "snap"}, "")
        hook_dispatch._RUN_CACHE["hook_session_stats"] = (lambda p, pp=None: {"_text": "stats"}, "")
        self.assertNotIn("hook_terse_advisor", list(hook_dispatch._routes("stop", "", "", HOST_GROK)))
        out = dispatch("stop", self._payload("Stop"), project_path=self.tmp)
        self.assertIsNone(out)


class TestGrokSessionStats(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "proj"
        (self.project / ".c3").mkdir(parents=True)
        self.grok_home = self.tmp / "grokhome"
        payload = PAYLOADS["Stop"]
        self.session_id = payload["session_id"]
        self.session_dir = self.grok_home / "sessions" / "U%3A%5Cproj" / self.session_id
        self.session_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "session_usage_1.0.30.json", self.session_dir / "usage.json")
        self._old = os.environ.get("GROK_HOME")
        os.environ["GROK_HOME"] = str(self.grok_home)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("GROK_HOME", None)
        else:
            os.environ["GROK_HOME"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, transcript):
        from cli.hook_session_stats import run
        payload = fixture("Stop", transcript_path=str(transcript), cwd=str(self.project))
        run(translate(payload), self.project)
        return [json.loads(line) for line in (self.project / ".c3" / "session_stats.jsonl").read_text().splitlines()]

    def test_usage_row_matches_grok_totals(self):
        rows = self._run(self.session_dir / "updates.jsonl")
        row = rows[-1]
        self.assertEqual(row["provider"], "grok")
        self.assertEqual(row["source"], "transcript")
        self.assertEqual(row["input_tokens"], 219878 - 196352)
        self.assertEqual(row["cache_read_tokens"], 196352)
        self.assertEqual(row["output_tokens"], 1673)
        self.assertAlmostEqual(row["cost_usd"], 0.05279044)
        self.assertEqual(row["model"], "grok-4.6-build")

    def test_transcript_outside_grok_home_is_not_read(self):
        stray = self.tmp / "elsewhere" / self.session_id
        stray.mkdir(parents=True)
        shutil.copy(FIXTURES / "session_usage_1.0.30.json", stray / "usage.json")
        row = self._run(stray / "updates.jsonl")[-1]
        self.assertEqual(row["source"], "none")
        self.assertIsNone(row["input_tokens"])


class TestGrokEntrypointSubprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        (self.tmp / ".c3" / "config.json").write_text(
            json.dumps({"enforcement": {"mode": "strict"}}), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _main(self, event, payload):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["GROK_HOME"] = str(self.tmp / "grokhome")
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "cli" / "hook_dispatch.py"), event,
             "--host", "grok", "--project", str(self.tmp)],
            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.tmp), env=env, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_stop_and_session_end_print_nothing(self):
        for event, key in (("stop", "Stop"), ("end", "SessionEnd"), ("start", "SessionStart")):
            self.assertEqual(self._main(event, fixture(key, cwd=str(self.tmp))).strip(), "", event)

    def test_pretool_deny_is_valid_json(self):
        target = self.tmp / "a.txt"
        target.write_text("alpha\n", encoding="utf-8")
        stdout = self._main("pretool", with_paths(fixture("PreToolUse:search_replace"), self.tmp, target))
        out = json.loads(stdout)
        assert_grok_valid(self, "pretool", out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
