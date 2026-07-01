"""Consolidated enforcement-state layer (cli/_hook_utils.py).

v2.42 replaced three state mechanisms spread over four hook files
(last_c3_call.json, unlocked_files.json, ad-hoc writers) with a single
.c3/enforcement_state.json written atomically through one shared module.
Legacy files are read as a fallback for one release but never written.
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
    record_c3_signal,
    record_json_unlocks,
    record_unlocked_files,
    save_enforcement_state,
)


class StateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        _hook_utils.drain_state_warnings()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state_path(self) -> Path:
        return self.tmp / ".c3" / "enforcement_state.json"


class TestWriters(StateBase):
    def test_record_c3_signal_writes_only_new_file(self):
        record_c3_signal("c3_search", True, session_id="s1", project_path=self.tmp)
        self.assertTrue(self._state_path().exists())
        # Legacy files must NOT be written anymore.
        self.assertFalse((self.tmp / ".c3" / "last_c3_call.json").exists())
        self.assertFalse((self.tmp / ".c3" / "unlocked_files.json").exists())

        state = json.loads(self._state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["session_id"], "s1")
        self.assertEqual(state["last_c3_call"]["tool"], "c3_search")
        self.assertTrue(state["last_c3_call"]["read_unlocked"])
        self.assertIn("ts", state["last_c3_call"])

    def test_record_unlocked_files_merges_categories(self):
        fp = str(self.tmp / "foo.py")
        record_unlocked_files([fp], {"read"}, project_path=self.tmp)
        record_unlocked_files([fp], {"edit"}, project_path=self.tmp)
        state = load_enforcement_state(self.tmp)
        normalized = str(Path(fp).resolve())
        self.assertEqual(state["unlocked_files"][normalized], ["edit", "read"])

    def test_record_json_unlocks_compat_wrapper(self):
        fp = str(self.tmp / "foo.py")
        record_json_unlocks([fp], project_path=self.tmp)
        state = load_enforcement_state(self.tmp)
        normalized = str(Path(fp).resolve())
        self.assertEqual(state["unlocked_files"][normalized], ["edit", "read"])

    def test_signal_preserves_existing_unlocks(self):
        fp = str(self.tmp / "foo.py")
        record_unlocked_files([fp], {"read"}, project_path=self.tmp)
        record_c3_signal("c3_compress", True, project_path=self.tmp)
        state = load_enforcement_state(self.tmp)
        self.assertEqual(state["last_c3_call"]["tool"], "c3_compress")
        self.assertTrue(state["unlocked_files"], "unlocks must survive a new signal")

    def test_atomic_write_leaves_no_temp_files(self):
        save_enforcement_state(
            {"session_id": "", "last_c3_call": None, "unlocked_files": {}},
            project_path=self.tmp,
        )
        leftovers = [p.name for p in (self.tmp / ".c3").iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])


class TestSessionScoping(StateBase):
    def test_new_session_write_resets_stale_state(self):
        record_unlocked_files([str(self.tmp / "a.py")], {"edit"},
                              session_id="old-session", project_path=self.tmp)
        record_c3_signal("c3_search", True, session_id="new-session",
                         project_path=self.tmp)
        state = load_enforcement_state(self.tmp)
        self.assertEqual(state["session_id"], "new-session")
        self.assertEqual(state["unlocked_files"], {},
                         "unlocks from a previous session must not carry over")

    def test_load_with_mismatched_session_returns_empty(self):
        record_c3_signal("c3_search", True, session_id="A", project_path=self.tmp)
        state = load_enforcement_state(self.tmp, session_id="B")
        self.assertIsNone(state["last_c3_call"])
        self.assertEqual(state["unlocked_files"], {})

    def test_load_without_session_accepts_any_state(self):
        record_c3_signal("c3_search", True, session_id="A", project_path=self.tmp)
        state = load_enforcement_state(self.tmp)  # no session context
        self.assertEqual(state["last_c3_call"]["tool"], "c3_search")


class TestLegacyFallback(StateBase):
    def test_legacy_signal_mapped_to_new_shape(self):
        (self.tmp / ".c3" / "last_c3_call.json").write_text(json.dumps({
            "timestamp": "2026-07-01T00:00:00+00:00",
            "tool": "c3_read",
            "read_unlocked": True,
        }), encoding="utf-8")
        state = load_enforcement_state(self.tmp)
        self.assertEqual(state["last_c3_call"]["ts"], "2026-07-01T00:00:00+00:00")
        self.assertEqual(state["last_c3_call"]["tool"], "c3_read")

    def test_legacy_unlock_map_loaded(self):
        (self.tmp / ".c3" / "unlocked_files.json").write_text(
            json.dumps({"X:/proj/foo.py": ["read", "edit"]}), encoding="utf-8")
        state = load_enforcement_state(self.tmp)
        self.assertEqual(state["unlocked_files"]["X:/proj/foo.py"], ["read", "edit"])

    def test_corrupt_legacy_files_are_not_critical(self):
        (self.tmp / ".c3" / "last_c3_call.json").write_text("junk", encoding="utf-8")
        (self.tmp / ".c3" / "unlocked_files.json").write_text("junk", encoding="utf-8")
        state = load_enforcement_state(self.tmp)
        self.assertIsNone(state["last_c3_call"])
        self.assertEqual(_hook_utils.drain_state_warnings(), [],
                         "legacy corruption must not raise critical warnings")


class TestCorruption(StateBase):
    def test_corrupt_state_quarantined_and_warned(self):
        self._state_path().write_text("{{{{", encoding="utf-8")
        state = load_enforcement_state(self.tmp, session_id="s")
        self.assertEqual(state["unlocked_files"], {})
        self.assertFalse(self._state_path().exists())
        self.assertTrue(
            (self.tmp / ".c3" / "enforcement_state.json.corrupt").exists())
        warnings = _hook_utils.drain_state_warnings()
        self.assertEqual(len(warnings), 1)
        self.assertIn("[c3:hook-error]", warnings[0])
        self.assertIn("hook_errors.log", warnings[0])

    def test_non_dict_root_is_corruption(self):
        self._state_path().write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        load_enforcement_state(self.tmp)
        self.assertTrue(
            (self.tmp / ".c3" / "enforcement_state.json.corrupt").exists())

    def test_recovery_after_quarantine(self):
        self._state_path().write_text("{{{{", encoding="utf-8")
        load_enforcement_state(self.tmp)
        _hook_utils.drain_state_warnings()
        # Next write recreates a healthy file.
        record_c3_signal("c3_search", True, project_path=self.tmp)
        state = load_enforcement_state(self.tmp)
        self.assertEqual(state["last_c3_call"]["tool"], "c3_search")
        self.assertEqual(_hook_utils.drain_state_warnings(), [])


if __name__ == "__main__":
    unittest.main()
