"""Codex hook compatibility (issue #84).

Codex CLI is the third host driving cli/hook_dispatch.py, and unlike Claude
Code and Gemini CLI it VALIDATES hook output: every event's schema is
additionalProperties:false and hookSpecificOutput.hookEventName is required.
An unknown key is a hard deserialization error, so the Claude shape
({"tool_result": ...} / top-level {"additionalContext": ...}) makes Codex
discard the entire hook response.

These tests pin two things:
  1. no C3 hook output for Codex is ever schema-invalid, and
  2. large Bash output never reaches hook_filter (and so never reaches
     tiktoken) on Codex, while payload-free bookkeeping keeps running.

The contract below was extracted from the embedded JSON Schemas in
codex.exe 0.147.0 (`<event>.command.output`). It is duplicated here on
purpose: the test must fail if hook_dispatch drifts from the real Codex
schema, which it cannot do if both read the same constant.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

from cli import hook_dispatch  # noqa: E402
from cli._hook_utils import (  # noqa: E402
    HOST_CLAUDE,
    HOST_CODEX,
    HOST_GEMINI,
    detect_host,
)
from cli.hook_dispatch import dispatch, merge_outputs  # noqa: E402

# ── Codex wire contract, verbatim from codex.exe 0.147.0 ────────────────────
_TOP_LEVEL = {
    "continue", "decision", "hookSpecificOutput", "reason",
    "stopReason", "suppressOutput", "systemMessage",
}
# hso=None means the event's schema has no hookSpecificOutput member at all.
CODEX_CONTRACT = {
    "pretool": ("PreToolUse", {
        "hookEventName", "additionalContext",
        "permissionDecision", "permissionDecisionReason", "updatedInput"}),
    "posttool": ("PostToolUse", {
        "hookEventName", "additionalContext", "updatedMCPToolOutput"}),
    "prompt": ("UserPromptSubmit", {"hookEventName", "additionalContext"}),
    "stop": ("Stop", None),
}


def assert_codex_valid(testcase, event: str, output):
    """Fail unless *output* would deserialize under Codex's schema."""
    if output is None:
        return
    expected_event, hso_keys = CODEX_CONTRACT[event]

    unknown = set(output) - _TOP_LEVEL
    testcase.assertEqual(
        unknown, set(),
        f"{event}: unknown top-level field(s) {sorted(unknown)} — Codex "
        f"rejects the whole response (additionalProperties: false)")

    hso = output.get("hookSpecificOutput")
    if hso is None:
        return
    testcase.assertIsNotNone(
        hso_keys, f"{event}: schema has no hookSpecificOutput member")
    testcase.assertIn(
        "hookEventName", hso, f"{event}: hookEventName is required")
    testcase.assertEqual(hso["hookEventName"], expected_event)
    unknown_hso = set(hso) - hso_keys
    testcase.assertEqual(
        unknown_hso, set(),
        f"{event}: unknown hookSpecificOutput field(s) {sorted(unknown_hso)}")


def codex_payload(event_name: str, **extra) -> dict:
    """A payload shaped like Codex's `<event>.command.input`."""
    payload = {
        "cwd": "/repo",
        "hook_event_name": event_name,
        "model": "gpt-5.4-codex",
        "permission_mode": "default",
        "session_id": "sess-codex",
        "transcript_path": None,
        "turn_id": "turn-1",
    }
    payload.update(extra)
    return payload


class TestDetectHost(unittest.TestCase):
    def test_turn_id_identifies_codex(self):
        self.assertEqual(detect_host(codex_payload("PostToolUse")), HOST_CODEX)

    def test_claude_payload_without_turn_id(self):
        self.assertEqual(
            detect_host({"tool_name": "Bash", "tool_response": "out"}),
            HOST_CLAUDE)

    def test_gemini_dict_response(self):
        self.assertEqual(
            detect_host({"tool_response": {"llmContent": "out"}}), HOST_GEMINI)

    def test_codex_wins_over_gemini_dict_response(self):
        # Codex declares tool_response as free-form, so an MCP tool returning
        # an object must NOT be mistaken for Gemini.
        payload = codex_payload("PostToolUse",
                                tool_response={"llmContent": "out"})
        self.assertEqual(detect_host(payload), HOST_CODEX)

    def test_non_dict_payload_is_claude(self):
        self.assertEqual(detect_host(None), HOST_CLAUDE)


class TestCodexRouting(unittest.TestCase):
    def _routes(self, event, tool, host):
        from cli._hook_utils import normalize_tool_name
        return list(hook_dispatch._routes(
            event, tool, normalize_tool_name(tool), host))

    def test_codex_bash_skips_hook_filter(self):
        routes = self._routes("posttool", "Bash", HOST_CODEX)
        self.assertNotIn("hook_filter", routes)

    def test_codex_bash_keeps_ghost_sweep(self):
        # Bookkeeping that never touches the payload stays on (requirement 3).
        self.assertEqual(self._routes("posttool", "Bash", HOST_CODEX),
                         ["hook_ghost_files"])

    def test_other_hosts_still_filter(self):
        for host in (HOST_CLAUDE, HOST_GEMINI):
            with self.subTest(host=host):
                self.assertEqual(self._routes("posttool", "Bash", host),
                                 ["hook_filter", "hook_ghost_files"])

    def test_codex_keeps_ledger_and_signal_routes(self):
        self.assertEqual(self._routes("posttool", "Edit", HOST_CODEX),
                         ["hook_edit_ledger", "hook_artifact"])
        self.assertEqual(
            self._routes("posttool", "mcp__c3__c3_search", HOST_CODEX),
            ["hook_c3_signal"])

    def test_codex_pretool_enforcement_unchanged(self):
        self.assertEqual(self._routes("pretool", "Edit", HOST_CODEX),
                         ["hook_access_guard", "hook_pretool_enforce"])


class TestCodexOutputShape(unittest.TestCase):
    def test_tool_result_never_reaches_codex(self):
        merged = merge_outputs([{"tool_result": "filtered!"}], [],
                               event="posttool", host=HOST_CODEX)
        self.assertNotIn("tool_result", merged)
        assert_codex_valid(self, "posttool", merged)
        # Not silently dropped either — it degrades to context.
        self.assertIn("filtered!",
                      merged["hookSpecificOutput"]["additionalContext"])

    def test_context_is_nested_with_event_name(self):
        merged = merge_outputs([{"additionalContext": "hello"}], [],
                               event="posttool", host=HOST_CODEX)
        self.assertNotIn("additionalContext", merged)
        self.assertEqual(merged["hookSpecificOutput"],
                         {"additionalContext": "hello",
                          "hookEventName": "PostToolUse"})
        assert_codex_valid(self, "posttool", merged)

    def test_deny_survives_and_stays_legal(self):
        deny = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "blocked",
        }}
        merged = merge_outputs([{"additionalContext": "fyi"}, deny], [],
                               event="pretool", host=HOST_CODEX)
        hso = merged["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertEqual(hso["permissionDecisionReason"], "blocked")
        self.assertEqual(hso["additionalContext"], "fyi")
        assert_codex_valid(self, "pretool", merged)

    def test_unknown_sub_hook_keys_are_dropped_not_forwarded(self):
        # Whitelist, not blacklist: a future sub-hook key cannot break Codex.
        rogue = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "somethingNew": {"nested": True},
        }}
        merged = merge_outputs([rogue], [], event="pretool", host=HOST_CODEX)
        self.assertNotIn("somethingNew", merged["hookSpecificOutput"])
        assert_codex_valid(self, "pretool", merged)

    def test_prompt_event_nests_context(self):
        merged = merge_outputs([{"additionalContext": "recall"}], [],
                               event="prompt", host=HOST_CODEX)
        self.assertEqual(merged["hookSpecificOutput"]["hookEventName"],
                         "UserPromptSubmit")
        assert_codex_valid(self, "prompt", merged)

    def test_stop_has_no_hook_specific_output(self):
        # stop.command.output carries no hookSpecificOutput and no
        # additionalContext — systemMessage is the only user-visible slot.
        merged = merge_outputs(
            [{"_text": "ledger line"}, {"additionalContext": "ctx"}], [],
            event="stop", host=HOST_CODEX)
        self.assertNotIn("hookSpecificOutput", merged)
        self.assertNotIn("_text", merged)
        self.assertIn("ctx", merged["systemMessage"])
        self.assertIn("ledger line", merged["systemMessage"])
        assert_codex_valid(self, "stop", merged)

    def test_plain_text_becomes_system_message(self):
        # Raw stdout is not valid Codex hook output, so _text must never
        # survive into main()'s print-raw branch.
        merged = merge_outputs([{"_text": "edit logged"}], [],
                               event="posttool", host=HOST_CODEX)
        self.assertNotIn("_text", merged)
        self.assertEqual(merged["systemMessage"], "edit logged")
        assert_codex_valid(self, "posttool", merged)

    def test_warnings_reach_codex(self):
        merged = merge_outputs([], ["[c3:hook-error] boom"],
                               event="posttool", host=HOST_CODEX)
        self.assertIn("[c3:hook-error] boom",
                      merged["hookSpecificOutput"]["additionalContext"])
        assert_codex_valid(self, "posttool", merged)

    def test_empty_stays_none(self):
        self.assertIsNone(
            merge_outputs([], [], event="posttool", host=HOST_CODEX))

    def test_pre_fix_shapes_would_be_rejected(self):
        """Counterfactual: proves assert_codex_valid is not vacuous.

        These are the exact shapes the dispatcher emitted for Codex before
        issue #84. Each must trip the contract checker — otherwise the tests
        above pass for the wrong reason.
        """
        claude_shape = merge_outputs(
            [{"tool_result": "filtered!"}, {"additionalContext": "ghost"}], [],
            event="posttool", host=HOST_CLAUDE)
        with self.assertRaises(AssertionError):
            assert_codex_valid(self, "posttool", claude_shape)

        gemini_shape = merge_outputs([{"additionalContext": "hello"}], [],
                                     event="posttool", host=HOST_GEMINI)
        # Well-formed nesting, but hookEventName is missing → still invalid.
        with self.assertRaises(AssertionError):
            assert_codex_valid(self, "posttool", gemini_shape)


class TestOtherHostsUnchanged(unittest.TestCase):
    """The Codex branch must not disturb the two existing hosts."""

    def test_claude_keeps_top_level_shape(self):
        merged = merge_outputs(
            [{"tool_result": "filtered!"}, {"additionalContext": "ghost"}], [],
            event="posttool")
        self.assertEqual(merged["tool_result"], "filtered!")
        self.assertEqual(merged["additionalContext"], "ghost")

    def test_gemini_still_degrades_tool_result(self):
        merged = merge_outputs([{"tool_result": "filtered!"}], [],
                               is_gemini=True, event="posttool")
        self.assertNotIn("tool_result", merged)
        self.assertIn("filtered!",
                      merged["hookSpecificOutput"]["additionalContext"])

    def test_host_overrides_is_gemini(self):
        merged = merge_outputs([{"additionalContext": "x"}], [],
                               is_gemini=True, event="posttool",
                               host=HOST_CLAUDE)
        self.assertEqual(merged["additionalContext"], "x")


class TestCodexDispatchEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        self._saved_cache = dict(hook_dispatch._RUN_CACHE)
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        hook_dispatch._RUN_CACHE.clear()
        hook_dispatch._RUN_CACHE.update(self._saved_cache)
        _hook_utils.drain_state_warnings()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _trip_filter(self):
        """Stub hook_filter so any invocation is an unmissable failure."""
        calls = []

        def _boom(payload, project_path=None):
            calls.append(payload)
            raise AssertionError("hook_filter must not run on Codex")

        hook_dispatch._RUN_CACHE["hook_filter"] = (_boom, "")
        return calls

    def test_large_bash_output_bypasses_filter_and_tiktoken(self):
        calls = self._trip_filter()
        # ~2 MB of tool output: the size class that makes tiktoken's Rust
        # encode allocate heavily. On Codex it must never be tokenized.
        big = ("some noisy build log line with detail\n" * 55_000)
        self.assertGreater(len(big), 2_000_000)
        payload = codex_payload(
            "PostToolUse", tool_name="Bash", tool_use_id="tu-1",
            tool_input={"command": "npm run build"}, tool_response=big)

        out = dispatch("posttool", payload, project_path=self.tmp)

        self.assertEqual(calls, [], "hook_filter was invoked on Codex")
        assert_codex_valid(self, "posttool", out)
        if out is not None:
            self.assertNotIn("tool_result", out)

    def test_claude_large_bash_still_routes_to_filter(self):
        # Same payload without turn_id is Claude: the filter must still run,
        # so the bypass is Codex-scoped rather than a blanket disable.
        calls = []
        hook_dispatch._RUN_CACHE["hook_filter"] = (
            lambda p, pp=None: calls.append(p) or {"tool_result": "small"}, "")
        out = dispatch("posttool",
                       {"tool_name": "Bash", "tool_response": "x" * 5000},
                       project_path=self.tmp)
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["tool_result"], "small")

    def test_codex_bookkeeping_still_runs(self):
        # c3_compress on Codex must still record the signal + sticky unlock.
        fp = self.tmp / "mod.py"
        fp.write_text("x = 1\n", encoding="utf-8")
        payload = codex_payload(
            "PostToolUse", tool_name="mcp__c3__c3_compress",
            session_id="sess-codex", tool_use_id="tu-2",
            tool_input={"file_path": str(fp)}, tool_response="ok")

        out = dispatch("posttool", payload, project_path=self.tmp)

        assert_codex_valid(self, "posttool", out)
        state = _hook_utils.load_enforcement_state(self.tmp)
        self.assertEqual(state["last_c3_call"]["tool"], "c3_compress")
        self.assertIn(_hook_utils.canonical_key(fp), state["unlocked_files"])

    def test_codex_pretool_deny_is_schema_valid(self):
        payload = codex_payload(
            "PreToolUse", tool_name="Edit", tool_use_id="tu-3",
            tool_input={"file_path": "x.py", "old_string": "a",
                        "new_string": "b"})
        out = dispatch("pretool", payload, project_path=self.tmp)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"],
                         "deny")
        assert_codex_valid(self, "pretool", out)

    def test_codex_pretool_advisory_is_schema_valid(self):
        payload = codex_payload(
            "PreToolUse", tool_name="Read", tool_use_id="tu-4",
            tool_input={"file_path": "x.py"})
        out = dispatch("pretool", payload, project_path=self.tmp)
        self.assertIn("[c3:hint]",
                      out["hookSpecificOutput"]["additionalContext"])
        assert_codex_valid(self, "pretool", out)

    def test_codex_ghost_sweep_output_is_schema_valid(self):
        # hook_ghost_files is the bookkeeping hook that still speaks on the
        # Bash route once hook_filter is gone.
        ghost = self.tmp / "dict"
        ghost.touch()
        payload = codex_payload(
            "PostToolUse", tool_name="Bash", tool_use_id="tu-5",
            tool_input={"command": "python -c 'x -> dict'"},
            tool_response="done")
        out = dispatch("posttool", payload, project_path=self.tmp)
        self.assertFalse(ghost.exists())
        self.assertIn("[c3:ghost-cleanup]",
                      out["hookSpecificOutput"]["additionalContext"])
        assert_codex_valid(self, "posttool", out)

    def test_crashing_sub_hook_still_yields_legal_codex_output(self):
        def _boom(p, pp=None):
            raise RuntimeError("kaboom")

        hook_dispatch._RUN_CACHE["hook_c3read"] = (_boom, "")
        payload = codex_payload(
            "PostToolUse", tool_name="mcp__c3__c3_read", tool_use_id="tu-6",
            tool_input={"file_path": "x.py"}, tool_response="ok")
        out = dispatch("posttool", payload, project_path=self.tmp)
        assert_codex_valid(self, "posttool", out)

    def test_import_failure_warning_is_legal_codex_output(self):
        hook_dispatch._RUN_CACHE["hook_c3read"] = (None, "ImportError: nope")
        payload = codex_payload(
            "PostToolUse", tool_name="mcp__c3__c3_read", tool_use_id="tu-7",
            tool_input={"file_path": "x.py"}, tool_response="ok")
        out = dispatch("posttool", payload, project_path=self.tmp)
        self.assertIn("[c3:hook-error] hook_c3read",
                      out["hookSpecificOutput"]["additionalContext"])
        assert_codex_valid(self, "posttool", out)


class TestCodexEntrypointSubprocess(unittest.TestCase):
    """Process-level check: what Codex actually reads off the hook's stdout.

    dispatch() covers composition, but only main() decides what is printed —
    and its "print raw _text when no structured output exists" branch is the
    one place non-JSON could still reach Codex.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dispatch(self, event: str, payload: dict, prelude: str = ""):
        import json as _json
        import os
        import subprocess

        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        # Never let an ambient Codex session leak into the env fallback.
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEX_MANAGED_BY_NPM", None)
        code = (prelude + "\nimport sys\nfrom cli import hook_dispatch\n"
                f"sys.argv = ['hook_dispatch.py', {event!r}]\n"
                "hook_dispatch.main()\n")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            input=_json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", cwd=str(self.tmp), env=env, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_large_codex_bash_payload_emits_nothing_invalid(self):
        import json as _json

        big = "noisy build log line with plenty of detail\n" * 55_000
        self.assertGreater(len(big), 2_000_000)
        stdout = self._dispatch("posttool", codex_payload(
            "PostToolUse", tool_name="Bash", tool_use_id="tu-1",
            tool_input={"command": "npm run build"}, tool_response=big))
        self.assertNotIn("tool_result", stdout)
        if stdout.strip():
            assert_codex_valid(self, "posttool", _json.loads(stdout))

    def test_codex_stop_text_is_json_not_raw(self):
        import json as _json

        prelude = (
            "from cli import hook_dispatch\n"
            "hook_dispatch._RUN_CACHE.update({\n"
            "  'hook_session_stats': (lambda p, pp=None: None, ''),\n"
            "  'hook_auto_snapshot': (lambda p, pp=None: None, ''),\n"
            "  'hook_terse_advisor': "
            "(lambda p, pp=None: {'_text': 'be terse'}, ''),\n"
            "})\n"
        )
        stdout = self._dispatch(
            "stop", codex_payload("Stop", stop_hook_active=False,
                                  last_assistant_message="hi"),
            prelude=prelude).strip()
        # Raw "be terse" on stdout is invalid hook output for Codex.
        self.assertTrue(stdout.startswith("{"), f"not JSON: {stdout!r}")
        parsed = _json.loads(stdout)
        self.assertEqual(parsed["systemMessage"], "be terse")
        assert_codex_valid(self, "stop", parsed)

    def test_claude_stop_text_still_prints_raw(self):
        # Parity guard: the Claude branch keeps its plain-text behavior.
        prelude = (
            "from cli import hook_dispatch\n"
            "hook_dispatch._RUN_CACHE.update({\n"
            "  'hook_session_stats': (lambda p, pp=None: None, ''),\n"
            "  'hook_auto_snapshot': (lambda p, pp=None: None, ''),\n"
            "  'hook_terse_advisor': "
            "(lambda p, pp=None: {'_text': 'be terse'}, ''),\n"
            "})\n"
        )
        stdout = self._dispatch("stop", {"session_id": "s1"}, prelude=prelude)
        self.assertEqual(stdout.strip(), "be terse")


if __name__ == "__main__":
    unittest.main()
