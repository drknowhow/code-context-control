"""Oracle ChatStore — append-only JSONL persistence + lazy legacy migration (#30).

The store used to rewrite the whole transcript on every append (O(n) per turn).
These pin the new contract: appends are O(new), legacy JSON arrays migrate on
first touch, and no partial state loses or duplicates a message.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from oracle.services.chat_store import ChatStore  # noqa: E402


class ChatStoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ChatStore(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _seed_legacy(self, conv_id: str, messages: list[dict]) -> Path:
        """Write a pre-JSONL conversation + index entry, as v2.49 would have."""
        path = Path(self._tmp.name) / f"{conv_id}.json"
        path.write_text(json.dumps(messages, indent=2), "utf-8")
        index = [{"id": conv_id, "title": "old", "created": "2026-01-01",
                  "updated": "2026-01-01", "message_count": len(messages)}]
        (Path(self._tmp.name) / "index.json").write_text(json.dumps(index), "utf-8")
        return path


class TestAppendOnly(ChatStoreBase):
    def test_roundtrip_preserves_order_and_content(self):
        cid = self.store.create_conversation()
        self.store.append_message(cid, {"role": "user", "content": "one"})
        self.store.append_messages(cid, [
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ])
        got = [(m["role"], m["content"]) for m in self.store.get_conversation(cid)]
        self.assertEqual(got, [("user", "one"), ("assistant", "two"),
                               ("user", "three")])

    def test_file_is_jsonl_one_message_per_line(self):
        cid = self.store.create_conversation()
        self.store.append_messages(cid, [{"role": "user", "content": "a"},
                                         {"role": "assistant", "content": "b"}])
        raw = (Path(self._tmp.name) / f"{cid}.jsonl").read_text("utf-8")
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["content"], "a")

    def test_append_does_not_rewrite_prior_bytes(self):
        """The O(n) regression guard: existing bytes must be a strict prefix."""
        cid = self.store.create_conversation()
        self.store.append_message(cid, {"role": "user", "content": "first"})
        path = Path(self._tmp.name) / f"{cid}.jsonl"
        before = path.read_bytes()
        self.store.append_message(cid, {"role": "assistant", "content": "second"})
        after = path.read_bytes()
        self.assertTrue(after.startswith(before))

    def test_timestamp_stamped_when_absent_and_kept_when_present(self):
        cid = self.store.create_conversation()
        self.store.append_message(cid, {"role": "user", "content": "x"})
        self.store.append_message(cid, {"role": "user", "content": "y",
                                        "timestamp": "2020-01-01T00:00:00+00:00"})
        msgs = self.store.get_conversation(cid)
        self.assertTrue(msgs[0]["timestamp"])
        self.assertEqual(msgs[1]["timestamp"], "2020-01-01T00:00:00+00:00")

    def test_message_count_and_autotitle_track_index(self):
        cid = self.store.create_conversation()
        self.store.append_message(cid, {"role": "user", "content": "Fix the parser"})
        self.store.append_message(cid, {"role": "assistant", "content": "ok"})
        entry = next(e for e in self.store.list_conversations() if e["id"] == cid)
        self.assertEqual(entry["message_count"], 2)
        self.assertEqual(entry["title"], "Fix the parser")

    def test_empty_append_is_a_noop(self):
        cid = self.store.create_conversation()
        self.store.append_messages(cid, [])
        self.assertEqual(self.store.get_conversation(cid), [])

    def test_torn_final_line_does_not_lose_the_file(self):
        cid = self.store.create_conversation()
        self.store.append_message(cid, {"role": "user", "content": "good"})
        path = Path(self._tmp.name) / f"{cid}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"role": "assistant", "cont')  # truncated write
        msgs = self.store.get_conversation(cid)
        self.assertEqual([m["content"] for m in msgs], ["good"])

    def test_unknown_conversation_reads_empty(self):
        self.assertEqual(self.store.get_conversation("nope"), [])


class TestLegacyMigration(ChatStoreBase):
    _LEGACY = [{"role": "user", "content": "old one", "timestamp": "2026-01-01"},
               {"role": "assistant", "content": "old two", "timestamp": "2026-01-02"}]

    def test_legacy_conversation_is_readable(self):
        self._seed_legacy("abc123", self._LEGACY)
        msgs = self.store.get_conversation("abc123")
        self.assertEqual([m["content"] for m in msgs], ["old one", "old two"])

    def test_read_migrates_and_removes_legacy_file(self):
        legacy = self._seed_legacy("abc123", self._LEGACY)
        self.store.get_conversation("abc123")
        self.assertFalse(legacy.exists())
        self.assertTrue((Path(self._tmp.name) / "abc123.jsonl").exists())

    def test_migration_is_idempotent(self):
        self._seed_legacy("abc123", self._LEGACY)
        for _ in range(3):
            msgs = self.store.get_conversation("abc123")
        self.assertEqual(len(msgs), 2)

    def test_append_to_legacy_conversation_preserves_history(self):
        self._seed_legacy("abc123", self._LEGACY)
        self.store.append_message("abc123", {"role": "user", "content": "new"})
        msgs = self.store.get_conversation("abc123")
        self.assertEqual([m["content"] for m in msgs],
                         ["old one", "old two", "new"])

    def test_orphan_jsonl_merges_with_legacy_without_duplicating(self):
        """Crash between append and migration: both files hold real messages."""
        self._seed_legacy("abc123", self._LEGACY)
        (Path(self._tmp.name) / "abc123.jsonl").write_text(
            json.dumps({"role": "user", "content": "new"}) + "\n", "utf-8")
        msgs = self.store.get_conversation("abc123")
        self.assertEqual([m["content"] for m in msgs],
                         ["old one", "old two", "new"])

    def test_corrupt_legacy_file_does_not_raise(self):
        (Path(self._tmp.name) / "bad.json").write_text("{not json", "utf-8")
        self.assertEqual(self.store.get_conversation("bad"), [])

    def test_delete_removes_both_formats(self):
        self._seed_legacy("abc123", self._LEGACY)
        (Path(self._tmp.name) / "abc123.jsonl").write_text("", "utf-8")
        self.store.delete_conversation("abc123")
        self.assertFalse((Path(self._tmp.name) / "abc123.json").exists())
        self.assertFalse((Path(self._tmp.name) / "abc123.jsonl").exists())
        self.assertEqual(self.store.list_conversations(), [])


class TestIndexCache(ChatStoreBase):
    def test_external_index_write_is_picked_up(self):
        cid = self.store.create_conversation()
        self.store.list_conversations()  # warm the cache
        path = Path(self._tmp.name) / "index.json"
        index = json.loads(path.read_text("utf-8"))
        index[0]["title"] = "renamed elsewhere"
        path.write_text(json.dumps(index), "utf-8")
        entry = next(e for e in self.store.list_conversations() if e["id"] == cid)
        self.assertEqual(entry["title"], "renamed elsewhere")

    def test_state_roundtrip_and_defaults(self):
        cid = self.store.create_conversation()
        self.assertEqual(self.store.get_state(cid)["depth"], "normal")
        self.store.update_state(cid, depth="deep")
        state = self.store.get_state(cid)
        self.assertEqual(state["depth"], "deep")
        self.assertEqual(state["focused_projects"], [])


if __name__ == "__main__":
    unittest.main()
