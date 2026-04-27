"""File-based conversation persistence for Oracle Chat."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

_STORE_DIR = Path.home() / ".c3" / "oracle" / "conversations"


class ChatStore:
    """Stores chat conversations as JSON files in ~/.c3/oracle/conversations/."""

    def __init__(self, store_dir: Path = _STORE_DIR):
        self.store_dir = store_dir
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.store_dir / "index.json"

    # ── Index ─────────────────────────────────────────────

    def _load_index(self) -> list[dict]:
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def _save_index(self, index: list[dict]):
        self._index_path.write_text(json.dumps(index, indent=2), "utf-8")

    def _touch_index(self, conv_id: str, **updates):
        """Update an index entry in-place."""
        index = self._load_index()
        for entry in index:
            if entry["id"] == conv_id:
                entry["updated"] = datetime.now(timezone.utc).isoformat()
                entry.update(updates)
                break
        self._save_index(index)

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
        index = self._load_index()
        index.insert(0, entry)
        self._save_index(index)
        self._conv_path(conv_id).write_text("[]", "utf-8")
        return conv_id

    def get_conversation(self, conv_id: str) -> list[dict]:
        """Return full message list for a conversation."""
        path = self._conv_path(conv_id)
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def append_message(self, conv_id: str, message: dict):
        """Append a message and update the index."""
        path = self._conv_path(conv_id)
        messages = self.get_conversation(conv_id)
        if "timestamp" not in message:
            message["timestamp"] = datetime.now(timezone.utc).isoformat()
        messages.append(message)
        path.write_text(json.dumps(messages, indent=2), "utf-8")

        updates = {"message_count": len(messages)}
        # Auto-title from first user message
        if len(messages) == 1 and message.get("role") == "user":
            updates["title"] = self._auto_title(message.get("content", ""))
        self._touch_index(conv_id, **updates)

    def append_messages(self, conv_id: str, new_messages: list[dict]):
        """Append multiple messages at once."""
        path = self._conv_path(conv_id)
        messages = self.get_conversation(conv_id)
        now = datetime.now(timezone.utc).isoformat()
        for msg in new_messages:
            if "timestamp" not in msg:
                msg["timestamp"] = now
            messages.append(msg)
        path.write_text(json.dumps(messages, indent=2), "utf-8")

        updates = {"message_count": len(messages)}
        if len(messages) == len(new_messages) and new_messages and new_messages[0].get("role") == "user":
            updates["title"] = self._auto_title(new_messages[0].get("content", ""))
        self._touch_index(conv_id, **updates)

    def delete_conversation(self, conv_id: str):
        """Delete a conversation, its state, and its index entry."""
        for path in (self._conv_path(conv_id), self._state_path(conv_id)):
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
        return self.store_dir / f"{conv_id}.json"

    def _state_path(self, conv_id: str) -> Path:
        return self.store_dir / f"{conv_id}_state.json"

    @staticmethod
    def _auto_title(text: str) -> str:
        text = text.strip().replace("\n", " ")
        return text[:60] + ("..." if len(text) > 60 else "")
