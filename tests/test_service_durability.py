"""Durability/concurrency regressions for the .c3 service layer.

Covers the server-side fixes (the hook side lives in test_edit_ledger_hook.py):
  EditLedger:        append-only tag_edit, locked log_edit append, collision-
                     resistant ids, tags_add patch merge, orphan-patch tolerance.
  ConversationStore: atomic sessions.json write, no-wipe on corrupt load,
                     empty-list cache sentinel (no re-read).
  FileMemoryStore:   brace-block end ignores braces in strings/comments.
  ContextSnapshot:   _load_snapshot('latest') skips a corrupt newest file.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.edit_ledger import EditLedger  # noqa: E402
from services.conversation_store import ConversationStore  # noqa: E402
from services.file_memory import FileMemoryStore  # noqa: E402
from services.context_snapshot import ContextSnapshot  # noqa: E402


class TestEditLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = EditLedger(str(self.tmp))

    def _lines(self):
        return [
            json.loads(ln)
            for ln in self.ledger.ledger_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    def test_log_edit_id_has_random_suffix(self):
        # Fix #3: ids carry a 4-hex suffix so server/hook can't collide.
        e1 = self.ledger.log_edit("a.py", "edit", "one", include_git=False)
        e2 = self.ledger.log_edit("a.py", "edit", "two", include_git=False)
        for eid in (e1["id"], e2["id"]):
            parts = eid.split("_")
            self.assertEqual(len(parts), 5, f"unexpected id shape: {eid}")
            self.assertEqual(len(parts[-1]), 4)
            int(parts[-1], 16)
        self.assertNotEqual(e1["id"], e2["id"])

    def test_tag_edit_appends_patch_not_rewrite(self):
        # Fix #1: tag_edit must APPEND a patch, never rewrite existing lines.
        e = self.ledger.log_edit("a.py", "edit", "x", include_git=False)
        before = self.ledger.ledger_file.read_text(encoding="utf-8")
        ok = self.ledger.tag_edit(e["id"], "reviewed")
        self.assertTrue(ok)
        after = self.ledger.ledger_file.read_text(encoding="utf-8")
        # Original content is a prefix of the new content (append-only).
        self.assertTrue(after.startswith(before), "tag_edit rewrote the file")
        lines = self._lines()
        patch = lines[-1]
        self.assertEqual(patch["target_id"], e["id"])
        self.assertEqual(patch["tags_add"], ["reviewed"])
        # And _load_merged applies the tag to the base entry.
        merged = self.ledger.get_history(file="a.py")
        self.assertIn("reviewed", merged[-1]["tags"])

    def test_tag_edit_unknown_id_returns_false(self):
        self.ledger.log_edit("a.py", "edit", "x", include_git=False)
        self.assertFalse(self.ledger.tag_edit("edit_nope_000_zzzz", "t"))

    def test_load_merged_tolerates_orphan_patch(self):
        # Fix #4: orphan patch (no base) must not crash _load_merged.
        e = self.ledger.log_edit("a.py", "edit", "x", include_git=False)
        with open(self.ledger.ledger_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"target_id": "edit_ghost_000_aaaa",
                                "tags_add": ["t"]}) + "\n")
        merged = self.ledger.get_history()
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["id"], e["id"])


class TestConversationStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _store(self):
        return ConversationStore(str(self.tmp))

    def test_add_turn_atomic_and_indexed(self):
        store = self._store()
        store.add_turn("s1", "user", "hello world", source="manual")
        store.add_turn("s1", "assistant", "hi there", source="manual")
        stats = store.get_stats()
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["turns"], 2)
        # sessions.json is valid JSON (atomic write left no partial file).
        sf = self.tmp / ".c3" / "conversations" / "sessions.json"
        json.loads(sf.read_text(encoding="utf-8"))
        # No leftover temp files.
        leftovers = list((self.tmp / ".c3" / "conversations").glob("sessions.json.tmp-*"))
        self.assertEqual(leftovers, [])

    def test_corrupt_sessions_not_wiped(self):
        # Fix #5: a corrupt sessions.json must be backed up, not silently
        # reset to [] (which would let the next save wipe history).
        store = self._store()
        store.add_turn("s1", "user", "hello", source="manual")
        sf = self.tmp / ".c3" / "conversations" / "sessions.json"
        sf.write_text("{ this is not json", encoding="utf-8")
        fresh = self._store()  # forces a fresh load from the corrupt file
        with self.assertRaises(Exception):
            fresh._load_sessions()
        backups = list((self.tmp / ".c3" / "conversations").glob("sessions.json.corrupt-*"))
        self.assertTrue(backups, "corrupt sessions.json was not backed up")

    def test_empty_sessions_cached_no_reread(self):
        # Fix #6: an empty (but loaded) catalog must not be re-read every call.
        store = self._store()
        self.assertEqual(store._load_sessions(), [])
        self.assertIsNotNone(store._sessions)  # sentinel flipped from None
        # Simulate a writer creating a file AFTER first load; cache should hold.
        sf = self.tmp / ".c3" / "conversations" / "sessions.json"
        sf.write_text("[{\"session_id\": \"x\"}]", encoding="utf-8")
        self.assertEqual(store._load_sessions(), [])  # served from cache


class TestFileMemoryBraceBlock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = FileMemoryStore(str(self.tmp))

    def test_brace_in_string_not_counted(self):
        # Fix #9: a '}' inside a string before the real close must be ignored.
        lines = [
            "function f() {",
            '  log("}");',
            "  return 1;",
            "}",
        ]
        end = self.store._find_brace_block_end(lines, 0)
        self.assertEqual(end, 4)  # the real closing brace is line 4 (1-indexed)

    def test_brace_in_line_comment_not_counted(self):
        lines = [
            "function f() {",
            "  // closing } here is a comment",
            "  return 1;",
            "}",
        ]
        self.assertEqual(self.store._find_brace_block_end(lines, 0), 4)

    def test_brace_in_block_comment_not_counted(self):
        lines = [
            "function f() {",
            "  /* } still",
            "     open } */",
            "  return 1;",
            "}",
        ]
        self.assertEqual(self.store._find_brace_block_end(lines, 0), 5)


class TestContextSnapshotCorrupt(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snap = ContextSnapshot(str(self.tmp))

    def test_latest_skips_corrupt_newest(self):
        # Fix #10: a corrupt newest snapshot must not break 'latest'.
        d = self.snap.data_dir
        (d / "snap_20260101_000000.json").write_text(
            json.dumps({"snapshot_id": "20260101_000000"}), encoding="utf-8")
        # A lexicographically-newer corrupt file.
        (d / "snap_20260102_000000.json").write_text("{ broken", encoding="utf-8")
        loaded = self.snap._load_snapshot("latest")
        self.assertNotIn("error", loaded)
        self.assertEqual(loaded["snapshot_id"], "20260101_000000")

    def test_corrupt_specific_returns_error(self):
        d = self.snap.data_dir
        (d / "snap_bad.json").write_text("{ broken", encoding="utf-8")
        loaded = self.snap._load_snapshot("bad")
        self.assertIn("error", loaded)


if __name__ == "__main__":
    unittest.main()
