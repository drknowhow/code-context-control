"""Smoke tests for the run(payload, project_path) entry points added in v2.42.

These hooks previously had zero coverage; each test drives the importable
run() function directly with a JSON payload — no subprocess spawns.
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

import cli.hook_auto_snapshot as hook_auto_snapshot  # noqa: E402
import cli.hook_c3_signal as hook_c3_signal  # noqa: E402
import cli.hook_artifact as hook_artifact  # noqa: E402
import cli.hook_c3read as hook_c3read  # noqa: E402
import cli.hook_edit_ledger as hook_edit_ledger  # noqa: E402
import cli.hook_edit_unlock as hook_edit_unlock  # noqa: E402
import cli.hook_ghost_files as hook_ghost_files  # noqa: E402
import cli.hook_session_stats as hook_session_stats  # noqa: E402
import cli.hook_terse_advisor as hook_terse_advisor  # noqa: E402


class SmokeBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestSessionStats(SmokeBase):
    def test_writes_stats_entry(self):
        hook_session_stats.run({
            "session_id": "sess-9", "stop_reason": "end_turn", "cost_usd": 0.5,
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cache_read_input_tokens": 90},
        }, project_path=self.tmp)
        entry = json.loads(
            (self.tmp / ".c3" / "session_stats.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(entry["session_id"], "sess-9")
        self.assertEqual(entry["input_tokens"], 100)
        self.assertEqual(entry["cache_read_tokens"], 90)

    def test_no_c3_dir_is_a_noop(self):
        bare = Path(tempfile.mkdtemp())
        try:
            hook_session_stats.run({"session_id": "x"}, project_path=bare)
            self.assertFalse((bare / ".c3").exists())
        finally:
            shutil.rmtree(bare, ignore_errors=True)


class TestAutoSnapshot(SmokeBase):
    def test_fallback_snapshot_written(self):
        saved = hook_auto_snapshot._REGISTRY_FILE
        hook_auto_snapshot._REGISTRY_FILE = self.tmp / "no_registry.json"
        try:
            hook_auto_snapshot.run(
                {"session_id": "sess-1", "stop_reason": "end_turn"},
                project_path=self.tmp)
        finally:
            hook_auto_snapshot._REGISTRY_FILE = saved
        snaps = list((self.tmp / ".c3" / "snapshots").glob("snap_*.json"))
        self.assertEqual(len(snaps), 1)
        snap = json.loads(snaps[0].read_text(encoding="utf-8"))
        self.assertEqual(snap["session_id"], "sess-1")
        self.assertEqual(snap["trigger"], "stop_hook")


class TestGhostFiles(SmokeBase):
    def test_ghost_deleted_and_reported(self):
        ghost = self.tmp / "word`"
        ghost.touch()
        out = hook_ghost_files.run({"tool_name": "Bash"}, project_path=self.tmp)
        self.assertFalse(ghost.exists())
        self.assertIn("[c3:ghost-cleanup]", out["additionalContext"])
        self.assertIn("word`", out["additionalContext"])

    def test_non_trigger_tool_is_noop(self):
        ghost = self.tmp / "dict"
        ghost.touch()
        out = hook_ghost_files.run({"tool_name": "Edit"}, project_path=self.tmp)
        self.assertIsNone(out)
        self.assertTrue(ghost.exists())

    def test_clean_root_returns_none(self):
        out = hook_ghost_files.run({"tool_name": "Bash"}, project_path=self.tmp)
        self.assertIsNone(out)


class TestC3Signal(SmokeBase):
    def test_signal_recorded_in_state(self):
        out = hook_c3_signal.run(
            {"tool_name": "mcp__c3__c3_search", "session_id": "s7"},
            project_path=self.tmp)
        self.assertIsNone(out)
        state = _hook_utils.load_enforcement_state(self.tmp)
        self.assertEqual(state["last_c3_call"]["tool"], "c3_search")
        self.assertTrue(state["last_c3_call"]["read_unlocked"])
        self.assertEqual(state["session_id"], "s7")

    def test_non_c3_tool_ignored(self):
        hook_c3_signal.run({"tool_name": "Bash"}, project_path=self.tmp)
        self.assertFalse((self.tmp / ".c3" / "enforcement_state.json").exists())


class TestC3Read(SmokeBase):
    def test_unlocks_editable_file(self):
        fp = self.tmp / "mod.py"
        fp.write_text("x = 1\n", encoding="utf-8")
        out = hook_c3read.run(
            {"tool_name": "mcp__c3__c3_read",
             "tool_input": {"file_path": str(fp)}},
            project_path=self.tmp)
        self.assertIn("[c3:edit-ready]", out["additionalContext"])
        state = _hook_utils.load_enforcement_state(self.tmp)
        cats = state["unlocked_files"].get(str(fp.resolve()), [])
        self.assertEqual(cats, ["edit", "read"])

    def test_other_tools_ignored(self):
        out = hook_c3read.run(
            {"tool_name": "mcp__c3__c3_compress",
             "tool_input": {"file_path": "a.py"}},
            project_path=self.tmp)
        self.assertIsNone(out)

    def test_non_editable_extension_ignored(self):
        out = hook_c3read.run(
            {"tool_name": "mcp__c3__c3_read",
             "tool_input": {"file_path": "photo.jpg"}},
            project_path=self.tmp)
        self.assertIsNone(out)


class TestEditUnlock(SmokeBase):
    def _payload(self, fp):
        return {"tool_name": "mcp__c3__c3_compress",
                "tool_input": {"file_path": str(fp)},
                "tool_response": "ok"}

    def test_compress_unlocks_and_nudges(self):
        fp = self.tmp / "mod.py"
        fp.write_text("x = 1\n", encoding="utf-8")
        out = hook_edit_unlock.run(self._payload(fp), project_path=self.tmp)
        self.assertIn("[c3:edit-ready]", out["additionalContext"])
        state = _hook_utils.load_enforcement_state(self.tmp)
        self.assertIn(str(fp.resolve()), state["unlocked_files"])

    def test_duplicate_nudge_suppressed(self):
        fp = self.tmp / "mod.py"
        fp.write_text("x = 1\n", encoding="utf-8")
        first = hook_edit_unlock.run(self._payload(fp), project_path=self.tmp)
        second = hook_edit_unlock.run(self._payload(fp), project_path=self.tmp)
        self.assertIsNotNone(first)
        self.assertIsNone(second, "same file must not nudge twice")


class TestEditLedger(SmokeBase):
    def test_edit_logged_with_text_output(self):
        fp = self.tmp / "mod.py"
        fp.write_text("x = 1\n", encoding="utf-8")
        out = hook_edit_ledger.run(
            {"tool_name": "Edit",
             "tool_input": {"file_path": str(fp),
                            "old_string": "x = 1", "new_string": "x = 2"}},
            project_path=self.tmp)
        self.assertIn("[c3:ledger]", out["_text"])
        ledger = (self.tmp / ".c3" / "edit_ledger.jsonl").read_text(encoding="utf-8")
        entry = json.loads(ledger.strip().splitlines()[-1])
        self.assertEqual(entry["file"], "mod.py")
        self.assertEqual(entry["change_type"], "modified")

    def test_unrelated_tool_ignored(self):
        out = hook_edit_ledger.run({"tool_name": "Bash", "tool_input": {}},
                                   project_path=self.tmp)
        self.assertIsNone(out)


class TestArtifactHook(SmokeBase):
    def _pending(self):
        return self.tmp / ".c3" / "agent_artifacts" / "pending.jsonl"

    def test_artifact_write_appends_one_pending_signal(self):
        fp = self.tmp / ".claude" / "settings.local.json"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("{}", encoding="utf-8")
        out = hook_artifact.run(
            {"tool_name": "Edit", "tool_input": {"file_path": str(fp)}},
            project_path=self.tmp)
        self.assertIsNone(out)  # silent hook — no user-visible output
        lines = self._pending().read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        sig = json.loads(lines[0])
        self.assertEqual(sig["path"], ".claude/settings.local.json")
        self.assertEqual(sig["source"], "hook")
        self.assertEqual(sig["tool"], "Edit")

    def test_non_artifact_path_appends_nothing(self):
        fp = self.tmp / "src" / "mod.py"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("x = 1\n", encoding="utf-8")
        hook_artifact.run(
            {"tool_name": "Write", "tool_input": {"file_path": str(fp)}},
            project_path=self.tmp)
        self.assertFalse(self._pending().exists())

    def test_unrelated_tool_and_outside_path_ignored(self):
        self.assertIsNone(hook_artifact.run(
            {"tool_name": "Bash", "tool_input": {}}, project_path=self.tmp))
        outside = Path(tempfile.gettempdir()) / "CLAUDE.md"
        hook_artifact.run(
            {"tool_name": "Edit", "tool_input": {"file_path": str(outside)}},
            project_path=self.tmp)
        self.assertFalse(self._pending().exists())


class TestTerseAdvisor(SmokeBase):
    def _with_state(self, state_dict, payload):
        state_file = self.tmp / "terse_state.json"
        if state_dict is not None:
            state_file.write_text(json.dumps(state_dict), encoding="utf-8")
        saved = hook_terse_advisor._STATE_FILE
        hook_terse_advisor._STATE_FILE = state_file
        try:
            return hook_terse_advisor.run(payload, project_path=self.tmp)
        finally:
            hook_terse_advisor._STATE_FILE = saved

    def test_dismissed_is_silent(self):
        out = self._with_state({"dismissed": True},
                               {"session_id": "s", "transcript_path": "x"})
        self.assertIsNone(out)

    def test_verbose_response_nudges(self):
        transcript = self.tmp / "transcript.jsonl"
        transcript.write_text(json.dumps({
            "message": {"role": "assistant", "content": "z" * 700},
        }) + "\n", encoding="utf-8")
        out = self._with_state(None, {"session_id": "s",
                                      "transcript_path": str(transcript)})
        self.assertIsNotNone(out)
        self.assertIn("/terse", out["_text"])


if __name__ == "__main__":
    unittest.main()
