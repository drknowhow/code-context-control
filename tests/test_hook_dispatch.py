"""Dispatcher tests (cli/hook_dispatch.py).

One interpreter spawn per hook event; sub-hooks run in-process and their
outputs compose per Claude Code hook semantics:
  - deny beats allow
  - additionalContext strings concatenate
  - a crashing sub-hook is logged and does not kill the others
  - unimportable sub-hooks / corrupted state surface a visible
    "[c3:hook-error]" warning
"""
import json
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
from cli.hook_dispatch import dispatch, merge_outputs  # noqa: E402


class TestMergeOutputs(unittest.TestCase):
    def test_deny_beats_allow(self):
        deny = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "blocked",
        }}
        allow_ctx = {"additionalContext": "fyi"}
        merged = merge_outputs([allow_ctx, deny], [])
        self.assertEqual(
            merged["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("fyi", merged["additionalContext"])

    def test_contexts_concatenated_in_order(self):
        merged = merge_outputs(
            [{"additionalContext": "one"}, {"additionalContext": "two"}], [])
        self.assertEqual(merged["additionalContext"], "one\ntwo")

    def test_warnings_appended_last(self):
        merged = merge_outputs(
            [{"additionalContext": "ctx"}], ["[c3:hook-error] boom"])
        self.assertEqual(merged["additionalContext"], "ctx\n[c3:hook-error] boom")

    def test_tool_result_preserved(self):
        merged = merge_outputs(
            [{"tool_result": "filtered!"}, {"additionalContext": "ghost"}], [])
        self.assertEqual(merged["tool_result"], "filtered!")
        self.assertEqual(merged["additionalContext"], "ghost")

    def test_gemini_tool_result_degrades_to_context(self):
        merged = merge_outputs([{"tool_result": "filtered!"}], [], is_gemini=True)
        self.assertNotIn("tool_result", merged)
        self.assertIn("filtered!",
                      merged["hookSpecificOutput"]["additionalContext"])

    def test_gemini_context_nested(self):
        merged = merge_outputs([{"additionalContext": "hello"}], [], is_gemini=True)
        self.assertEqual(
            merged["hookSpecificOutput"]["additionalContext"], "hello")

    def test_plain_text_rides_along(self):
        merged = merge_outputs([{"_text": "ledger line"}], [])
        self.assertEqual(merged, {"_text": "ledger line"})

    def test_empty_outputs_return_none(self):
        self.assertIsNone(merge_outputs([], []))
        self.assertIsNone(merge_outputs([None, {}], []))


class TestRouting(unittest.TestCase):
    def _routes(self, event, tool):
        from cli._hook_utils import normalize_tool_name
        return list(hook_dispatch._routes(event, tool, normalize_tool_name(tool)))

    def test_pretool_routes_access_guard_first(self):
        self.assertEqual(self._routes("pretool", "Read"),
                         ["hook_access_guard", "hook_pretool_enforce"])
        self.assertEqual(self._routes("pretool", "Edit"),
                         ["hook_access_guard", "hook_pretool_enforce"])

    def test_posttool_bash_routes(self):
        self.assertEqual(self._routes("posttool", "Bash"),
                         ["hook_filter", "hook_ghost_files"])

    def test_posttool_read_routes(self):
        self.assertEqual(self._routes("posttool", "Read"),
                         ["hook_read", "hook_ghost_files"])

    def test_posttool_c3_read_routes(self):
        self.assertEqual(
            self._routes("posttool", "mcp__c3__c3_read"),
            ["hook_c3read", "hook_c3_signal", "hook_ghost_files"])

    def test_posttool_c3_compress_routes(self):
        self.assertEqual(
            self._routes("posttool", "mcp__c3__c3_compress"),
            ["hook_edit_unlock", "hook_c3_signal"])

    def test_posttool_edit_routes_to_ledger_and_artifact(self):
        self.assertEqual(self._routes("posttool", "Edit"),
                         ["hook_edit_ledger", "hook_artifact"])
        self.assertEqual(self._routes("posttool", "Write"),
                         ["hook_edit_ledger", "hook_artifact"])

    def test_posttool_c3_search_routes_to_signal_only(self):
        self.assertEqual(self._routes("posttool", "mcp__c3__c3_search"),
                         ["hook_c3_signal"])

    def test_posttool_unmatched_c3_tool_routes_nowhere(self):
        # c3_bitbucket / c3_project never had signal matchers — parity kept.
        self.assertEqual(self._routes("posttool", "mcp__c3__c3_bitbucket"), [])

    def test_stop_routes(self):
        self.assertEqual(
            self._routes("stop", ""),
            ["hook_session_stats", "hook_auto_snapshot", "hook_terse_advisor"])

    def test_unknown_event_routes_nowhere(self):
        self.assertEqual(self._routes("bogus", "Read"), [])


class DispatchBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        # Pin the enforcement mode. Without this the tmp project inherits
        # whatever ~/.c3/config.json says, so a developer who ran
        # `c3 enforce advisory` turns every deny assertion below into a
        # failure that has nothing to do with the dispatcher.
        (self.tmp / ".c3" / "config.json").write_text(
            json.dumps({"enforcement": {"mode": "strict"}}), encoding="utf-8")
        self._saved_cache = dict(hook_dispatch._RUN_CACHE)
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        hook_dispatch._RUN_CACHE.clear()
        hook_dispatch._RUN_CACHE.update(self._saved_cache)
        _hook_utils.drain_state_warnings()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stub(self, module_name, fn):
        hook_dispatch._RUN_CACHE[module_name] = (fn, "")


class TestDispatchComposition(DispatchBase):
    def test_multiple_sub_hooks_compose(self):
        self._stub("hook_c3read", lambda p, pp=None: {"additionalContext": "A"})
        self._stub("hook_c3_signal", lambda p, pp=None: None)
        self._stub("hook_ghost_files", lambda p, pp=None: {"additionalContext": "B"})
        out = dispatch("posttool", {"tool_name": "mcp__c3__c3_read"},
                       project_path=self.tmp)
        self.assertEqual(out["additionalContext"], "A\nB")

    def test_crashing_sub_hook_does_not_kill_others(self):
        def _boom(p, pp=None):
            raise RuntimeError("kaboom")
        self._stub("hook_c3read", _boom)
        self._stub("hook_c3_signal", lambda p, pp=None: None)
        self._stub("hook_ghost_files", lambda p, pp=None: {"additionalContext": "B"})
        out = dispatch("posttool", {"tool_name": "mcp__c3__c3_read"},
                       project_path=self.tmp)
        self.assertIsNotNone(out)
        self.assertIn("B", out["additionalContext"])
        # Crash is non-critical: logged, not surfaced.
        self.assertNotIn("[c3:hook-error]", out["additionalContext"])

    def test_deny_propagates(self):
        deny = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "no",
        }}
        self._stub("hook_pretool_enforce", lambda p, pp=None: deny)
        out = dispatch("pretool", {"tool_name": "Edit"}, project_path=self.tmp)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_import_failure_is_visible(self):
        hook_dispatch._RUN_CACHE["hook_c3read"] = (None, "ImportError: nope")
        self._stub("hook_c3_signal", lambda p, pp=None: None)
        self._stub("hook_ghost_files", lambda p, pp=None: None)
        out = dispatch("posttool", {"tool_name": "mcp__c3__c3_read"},
                       project_path=self.tmp)
        self.assertIn("[c3:hook-error] hook_c3read", out["additionalContext"])
        self.assertIn("hook_errors.log", out["additionalContext"])


class TestDispatchEndToEnd(DispatchBase):
    """Real sub-hooks, tmp project, no stubs (no subprocess either)."""

    def test_pretool_read_advisory(self):
        out = dispatch("pretool",
                       {"tool_name": "Read", "tool_input": {"file_path": "x.py"}},
                       project_path=self.tmp)
        self.assertIn("[c3:hint]", out["additionalContext"])

    def test_pretool_edit_denied(self):
        out = dispatch("pretool",
                       {"tool_name": "Edit",
                        "tool_input": {"file_path": "x.py", "old_string": "a",
                                       "new_string": "b"}},
                       project_path=self.tmp)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_posttool_c3_compress_unlocks_and_signals(self):
        fp = self.tmp / "mod.py"
        fp.write_text("x = 1\n", encoding="utf-8")
        out = dispatch(
            "posttool",
            {"tool_name": "mcp__c3__c3_compress",
             "session_id": "sess-1",
             "tool_input": {"file_path": str(fp)},
             "tool_response": "ok"},
            project_path=self.tmp,
        )
        self.assertIn("[c3:edit-ready]", out["additionalContext"])
        state = _hook_utils.load_enforcement_state(self.tmp)
        self.assertEqual(state["session_id"], "sess-1")
        self.assertEqual(state["last_c3_call"]["tool"], "c3_compress")
        self.assertIn(_hook_utils.canonical_key(fp), state["unlocked_files"])
        # Full round-trip: the signal written by posttool unlocks pretool Read.
        out2 = dispatch("pretool",
                        {"tool_name": "Read", "session_id": "sess-1",
                         "tool_input": {"file_path": str(fp)}},
                        project_path=self.tmp)
        self.assertIsNone(out2)

    def test_corrupted_state_warning_reaches_output(self):
        (self.tmp / ".c3" / "enforcement_state.json").write_text(
            "]]not json[[", encoding="utf-8")
        out = dispatch("pretool",
                       {"tool_name": "Read", "tool_input": {"file_path": "x.py"}},
                       project_path=self.tmp)
        self.assertIn("[c3:hint]", out["additionalContext"])
        self.assertIn("[c3:hook-error] enforcement_state",
                      out["additionalContext"])

    def test_stop_event_runs_all_stop_hooks(self):
        import cli.hook_auto_snapshot as auto_snapshot
        saved_registry = auto_snapshot._REGISTRY_FILE
        auto_snapshot._REGISTRY_FILE = self.tmp / "no_registry.json"
        try:
            out = dispatch(
                "stop",
                {"session_id": "s1", "stop_reason": "end_turn",
                 "cost_usd": 0.01,
                 "usage": {"input_tokens": 10, "output_tokens": 5}},
                project_path=self.tmp,
            )
        finally:
            auto_snapshot._REGISTRY_FILE = saved_registry
        # session stats written
        stats = (self.tmp / ".c3" / "session_stats.jsonl")
        self.assertTrue(stats.exists())
        entry = json.loads(stats.read_text(encoding="utf-8").strip())
        self.assertEqual(entry["session_id"], "s1")
        # fallback snapshot written
        snaps = list((self.tmp / ".c3" / "snapshots").glob("snap_*.json"))
        self.assertEqual(len(snaps), 1)
        # terse advisor silent without transcript → no structured output
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
