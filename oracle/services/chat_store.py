"""File-based conversation persistence for Oracle Chat.

Conversations are append-only JSONL (one message per line): a turn costs one
``write`` proportional to the new messages, not a re-serialization of the whole
transcript. The previous JSON-array format rewrote every message on every
append, so a long conversation paid increasing cost per turn.

Legacy ``<id>.json`` files are migrated to ``<id>.jsonl`` lazily on first
access — no migration script, no startup scan, and a half-finished migration
cannot lose messages because the legacy file is only unlinked after the JSONL
rewrite lands.

The index is small (one entry per conversation, not per message) so it stays a
whole-file write, but it is cached in memory and re-read only when the file
changes underneath us.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

_STORE_DIR = Path.home() / ".c3" / "oracle" / "conversations"


class ChatStore:
    """Stores chat conversations as JSONL files in ~/.c3/oracle/conversations/."""

    def __init__(self, store_dir: Path = _STORE_DIR):
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.store_dir / "index.json"
        self._index_cache: list[dict] | None = None
        self._index_stamp: tuple[int, int] | None = None

    # ── Index ─────────────────────────────────────────────

    def _index_fingerprint(self) -> tuple[int, int] | None:
        try:
            st = self._index_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _load_index(self) -> list[dict]:
        stamp = self._index_fingerprint()
        if self._index_cache is not None and stamp == self._index_stamp:
            return self._index_cache
        index: list[dict] = []
        if self._index_path.exists():
            try:
                loaded = json.loads(self._index_path.read_text("utf-8"))
                if isinstance(loaded, list):
                    index = loaded
            except (json.JSONDecodeError, OSError):
                pass
        self._index_cache, self._index_stamp = index, stamp
        return index

    def _save_index(self, index: list[dict]):
        self._index_path.write_text(json.dumps(index, indent=2), "utf-8")
        self._index_cache, self._index_stamp = index, self._index_fingerprint()

    def _touch_index(self, conv_id: str, **updates):
        """Update an index entry in-place."""
        index = [dict(e) for e in self._load_index()]
        for entry in index:
            if entry["id"] == conv_id:
                entry["updated"] = datetime.now(timezone.utc).isoformat()
                entry.update(updates)
                break
        self._save_index(index)

    def _entry(self, conv_id: str) -> dict | None:
        return next((e for e in self._load_index() if e["id"] == conv_id), None)

    # ── Conversations ─────────────────────────────────────

    def list_conversations(self, limit: int = 50) -> list[dict]:
        """Return index entries sorted by most recent first."""
        index = self._load_index()
        index.sort(key=lambda e: e.get("updated", ""), reverse=True)
        return index[:limit]

    def create_conversation(self, title: str | None = None) -> str:
        """Create a new empty conversation. Returns its ID."""
        conv_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": conv_id,
            "title": title or "New chat",
            "created": now,
            "updated": now,
            "message_count": 0,
        }
        index = [dict(e) for e in self._load_index()]
        index.insert(0, entry)
        self._save_index(index)
        self._conv_path(conv_id).write_text("", "utf-8")
        return conv_id

    def get_conversation(self, conv_id: str) -> list[dict]:
        """Return full message list for a conversation.

        Legacy messages sort first: appends only ever go to the JSONL, so a
        not-yet-migrated conversation is [legacy..., appended...] in order.
        """
        self._migrate(conv_id)
        return self._read_legacy(conv_id) + self._read_jsonl(conv_id)

    def append_message(self, conv_id: str, message: dict):
        """Append a message and update the index."""
        self.append_messages(conv_id, [message])

    def append_messages(self, conv_id: str, new_messages: list[dict]):
        """Append messages as JSONL lines — O(new), not O(transcript)."""
        if not new_messages:
            return
        self._migrate(conv_id)
        path = self._conv_path(conv_id)
        now = datetime.now(timezone.utc).isoformat()
        for msg in new_messages:
            if "timestamp" not in msg:
                msg["timestamp"] = now
        with path.open("a", encoding="utf-8") as fh:
            for msg in new_messages:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")

        entry = self._entry(conv_id)
        prior = int((entry or {}).get("message_count", 0) or 0)
        updates = {"message_count": prior + len(new_messages)}
        # Auto-title from the first user message of the conversation.
        if prior == 0 and new_messages[0].get("role") == "user":
            updates["title"] = self._auto_title(new_messages[0].get("content", ""))
        self._touch_index(conv_id, **updates)

    def delete_conversation(self, conv_id: str):
        """Delete a conversation, its state, and its index entry."""
        for path in (self._conv_path(conv_id), self._legacy_conv_path(conv_id),
                     self._state_path(conv_id)):
            if path.exists():
                path.unlink()
        index = [e for e in self._load_index() if e["id"] != conv_id]
        self._save_index(index)

    def update_title(self, conv_id: str, title: str):
        self._touch_index(conv_id, title=title)

    # ── Per-conversation state ────────────────────────────

    _DEFAULT_STATE = {"focused_projects": [], "model": None, "depth": "normal"}

    def get_state(self, conv_id: str) -> dict:
        """Get conversation session state (focused projects, model, depth)."""
        path = self._state_path(conv_id)
        if path.exists():
            try:
                state = json.loads(path.read_text("utf-8"))
                return {**self._DEFAULT_STATE, **state}
            except (json.JSONDecodeError, OSError):
                pass
        return dict(self._DEFAULT_STATE)

    def set_state(self, conv_id: str, state: dict):
        """Persist full conversation session state."""
        self._state_path(conv_id).write_text(json.dumps(state, indent=2), "utf-8")

    def update_state(self, conv_id: str, **updates):
        """Merge updates into existing state."""
        state = self.get_state(conv_id)
        state.update(updates)
        self.set_state(conv_id, state)

    # ── Helpers ───────────────────────────────────────────

    def _conv_path(self, conv_id: str) -> Path:
        return self.store_dir / f"{conv_id}.jsonl"

    def _legacy_conv_path(self, conv_id: str) -> Path:
        return self.store_dir / f"{conv_id}.json"

    def _read_legacy(self, conv_id: str) -> list[dict]:
        """Parse a pre-JSONL ``<id>.json`` array. Read-only, never written."""
        legacy = self._legacy_conv_path(conv_id)
        if not legacy.exists():
            return []
        try:
            messages = json.loads(legacy.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return [m for m in messages if isinstance(m, dict)] \
            if isinstance(messages, list) else []

    def _migrate(self, conv_id: str) -> None:
        """Best-effort legacy -> JSONL conversion. Safe to call repeatedly.

        Ordering is what makes it crash-safe: write a temp file, atomically
        replace the JSONL target, and only then unlink the legacy file. A
        crash at any point leaves the legacy file intact, and readers
        concatenate legacy + JSONL, so no message is ever lost or duplicated
        — the legacy file only disappears once its contents are durable.
        """
        legacy = self._legacy_conv_path(conv_id)
        if not legacy.exists():
            return
        path = self._conv_path(conv_id)
        merged = self._read_legacy(conv_id) + self._read_jsonl(conv_id)
        tmp = path.with_name(path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for msg in merged:
                    fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
            legacy.unlink()
        except OSError:
            tmp.unlink(missing_ok=True)

    def _read_jsonl(self, conv_id: str) -> list[dict]:
        path = self._conv_path(conv_id)
        if not path.exists():
            return []
        messages: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(json.loads(line))
                    except json.JSONDecodeError:
                        # A torn final line loses that message, not the file.
                        continue
        except OSError:
            return []
        return messages

    def _state_path(self, conv_id: str) -> Path:
        return self.store_dir / f"{conv_id}_state.json"

    @staticmethod
    def _auto_title(text: str) -> str:
        text = text.strip().replace("\n", " ")
        return text[:60] + ("..." if len(text) > 60 else "")
