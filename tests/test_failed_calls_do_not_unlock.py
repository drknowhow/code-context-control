"""A c3_* call that FAILED is not evidence that c3 was used (ISSUE-3).

Before: c3_compress on a missing path returned "Error: File not found",
hook_c3_signal recorded a fresh signal, hook_edit_unlock stuck an unlock on
that path, and the activity log counted the call — so native Write on the
very path was allowed. Now all three routes read the result first.
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
from cli._hook_utils import (  # noqa: E402
    load_enforcement_state,
    response_text_failed,
    tool_response_failed,
)

sys.modules.setdefault("_hook_utils", _hook_utils)

from cli import hook_c3_signal, hook_edit_unlock  # noqa: E402
from cli.hook_pretool_enforce import run as enforce  # noqa: E402


class TestFailureHeuristic(unittest.TestCase):
    def test_text(self):
        self.assertTrue(response_text_failed("Error: File not found: x.py"))
        self.assertTrue(response_text_failed("  Error reading file"))
        self.assertTrue(response_text_failed("[read:error] File not found: x"))
        self.assertTrue(response_text_failed("[file_map:error] not found"))
        self.assertTrue(response_text_failed("[c3-mask:unsupported] policy"))
        self.assertFalse(response_text_failed("## results\nError handling lives in errors.py"))
        self.assertFalse(response_text_failed("# a.py (.py) — 12 lines"))
        self.assertFalse(response_text_failed(""))
        self.assertFalse(response_text_failed(None))

    def test_payload_shapes(self):
        self.assertTrue(tool_response_failed({"tool_response": "Error: nope"}))
        self.assertTrue(tool_response_failed({"tool_response": {"isError": True, "content": [{"type": "text", "text": "ok?"}]}}))
        self.assertTrue(tool_response_failed({"tool_response": {"content": [{"type": "text", "text": "Error: x"}]}}))
        self.assertTrue(tool_response_failed({"tool_response": [{"type": "text", "text": "[read:error] x"}]}))
        self.assertTrue(tool_response_failed({"tool_response": {"llmContent": "Error: gemini shape"}}))
        self.assertFalse(tool_response_failed({"tool_response": "fine"}))
        self.assertFalse(tool_response_failed({"tool_response": {"content": [{"type": "text", "text": "fine"}]}}))
        self.assertFalse(tool_response_failed({}))


class _Project(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        _hook_utils.drain_state_warnings()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _payload(self, tool, file_path, response, session="s1"):
        return {"tool_name": tool, "session_id": session,
                "tool_input": {"file_path": file_path, "mode": "map"},
                "tool_response": response}


class TestSignalHook(_Project):
    def test_failed_call_writes_no_signal(self):
        hook_c3_signal.run(self._payload("mcp__c3__c3_compress", "ghost.py", "Error: File not found: ghost.py"), self.tmp)
        self.assertIsNone(load_enforcement_state(self.tmp, session_id="s1").get("last_c3_call"))

    def test_successful_call_writes_signal(self):
        hook_c3_signal.run(self._payload("mcp__c3__c3_compress", "a.py", "# a.py map"), self.tmp)
        self.assertEqual(load_enforcement_state(self.tmp, session_id="s1")["last_c3_call"]["tool"], "c3_compress")


class TestUnlockHook(_Project):
    def test_failed_compress_unlocks_nothing(self):
        target = str(self.tmp / "ghost.py")
        out = hook_edit_unlock.run(self._payload("mcp__c3__c3_compress", target, "Error: File not found"), self.tmp)
        self.assertIsNone(out)
        self.assertEqual(load_enforcement_state(self.tmp, session_id="s1").get("unlocked_files"), {})
        deny = enforce({"tool_name": "Write", "session_id": "s1",
                        "tool_input": {"file_path": target, "content": "x"}}, project_path=self.tmp)
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_successful_compress_still_unlocks(self):
        target = str(self.tmp / "real.py")
        out = hook_edit_unlock.run(self._payload("mcp__c3__c3_compress", target, "# real.py (.py) — 3 lines"), self.tmp)
        self.assertIsNotNone(out)
        self.assertTrue(load_enforcement_state(self.tmp, session_id="s1").get("unlocked_files"))


class TestActivityLogScan(_Project):
    def _log(self, tool, summary, ok=None):
        entry = {"timestamp": "2026-08-22T12:00:00+00:00", "type": "tool_call", "tool": tool,
                 "args": {}, "result_summary": summary}
        if ok is not None:
            entry["ok"] = ok
        with open(self.tmp / ".c3" / "activity_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _write(self):
        return enforce({"tool_name": "Write", "session_id": "s9",
                        "tool_input": {"file_path": str(self.tmp / "n.py"), "content": "x"}}, project_path=self.tmp)

    def test_failed_entry_by_flag_is_skipped(self):
        self._log("c3_edit", "120->40tok", ok=False)
        self.assertEqual(self._write()["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_failed_entry_by_text_is_skipped(self):
        self._log("c3_edit", "Error: File not found: n.py")
        self.assertEqual(self._write()["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_successful_entry_still_counts(self):
        self._log("c3_edit", "edited n.py", ok=True)
        self.assertNotIn("hookSpecificOutput", self._write() or {})


if __name__ == "__main__":
    unittest.main()
