"""NotificationStore — Thread-safe notification queue for background agents.

Persists to .c3/notifications.jsonl. Supports dedup, severity filtering,
and auto-acknowledgement when surfaced to Claude via tool responses.
"""
import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# How long to suppress a repeated agent+title after it has been acknowledged.
_COOLDOWN_MINUTES = {"critical": 5, "warning": 30, "info": 60}

# Duplicate collapse: keep the most severe label when merging records.
_SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _recency_key(entry: dict) -> str:
    """Sort key: last occurrence of a (possibly collapsed) notification."""
    return entry.get("last_seen") or entry.get("timestamp", "")


def _entry_count(entry: dict) -> int:
    """Occurrence count of a (possibly collapsed) record; legacy rows count as 1."""
    try:
        return max(1, int(entry.get("count", 1) or 1))
    except (TypeError, ValueError):
        return 1


class NotificationStore:
    """Thread-safe JSONL notification store for background agents."""

    def __init__(self, project_path: str):
        self._file = Path(project_path) / ".c3" / "notifications.jsonl"
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # Retro-cleanup runs at most once per store instance, lazily on first
        # access, so pre-existing duplicate backlogs self-heal without a
        # dedicated maintenance pass.
        self._collapsed = False

    def add(self, agent: str, severity: str, title: str, message: str,
            ai_enhanced: bool = False, replace_if_unacked: bool = False) -> dict | None:
        """Append a notification. Dedup: an identical (agent, title, message)
        that is still unacknowledged collapses into the existing record — its
        ``count`` is bumped and ``last_seen`` refreshed instead of appending a
        duplicate line.

        severity: 'info', 'warning', 'critical'
        replace_if_unacked: if True and an unacked notification with the same agent+title
            already exists, update its message/severity in-place (bumping
            count/last_seen) instead of appending a new entry. Use for
            high-frequency agents (budget, index, key-file drift) to prevent pile-up.
        Returns the entry if written/updated, None if deduped.
        """
        with self._lock:
            self._maybe_collapse_locked()
            message_hash = hashlib.md5((message or "").encode("utf-8")).hexdigest()[:12]
            cooldown = timedelta(minutes=_COOLDOWN_MINUTES.get(severity, 30))
            now = datetime.now(timezone.utc)
            entries = self._read_all()
            for existing in entries:
                if existing.get("agent") != agent or existing.get("title") != title:
                    continue
                if not existing.get("acknowledged"):
                    if replace_if_unacked:
                        # Update in-place — prevents repeated pile-up for chatty agents
                        existing["message"] = message
                        existing["message_hash"] = message_hash
                        existing["severity"] = severity
                        existing["timestamp"] = now.isoformat()
                        existing["last_seen"] = now.isoformat()
                        existing["count"] = _entry_count(existing) + 1
                        existing["ai_enhanced"] = ai_enhanced
                        self._write_all(entries)
                        return existing
                    # Same notification still pending — collapse into the
                    # existing record instead of appending a duplicate.
                    if existing.get("message_hash") == message_hash:
                        existing["count"] = _entry_count(existing) + 1
                        existing["last_seen"] = now.isoformat()
                        self._write_all(entries)
                        return None
                else:
                    # Already acknowledged — suppress if within cooldown window
                    try:
                        acked_at = datetime.fromisoformat(existing["timestamp"])
                        if acked_at.tzinfo is None:
                            acked_at = acked_at.replace(tzinfo=timezone.utc)
                        if now - acked_at < cooldown:
                            return None
                    except (KeyError, ValueError):
                        pass

            entry = {
                "id": uuid.uuid4().hex[:12],
                "agent": agent,
                "severity": severity,
                "title": title,
                "message": message,
                "message_hash": message_hash,
                "timestamp": now.isoformat(),
                "last_seen": now.isoformat(),
                "count": 1,
                "acknowledged": False,
                "ai_enhanced": ai_enhanced,
            }
            with open(self._file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            return entry

    def get_pending_count(self) -> int:
        """Return count of unacknowledged warning/critical notifications without consuming them."""
        with self._lock:
            self._maybe_collapse_locked()
            return sum(
                1 for e in self._read_all()
                if not e.get("acknowledged")
                and e.get("severity") in ("warning", "critical")
            )

    # Default filter: only notifications a human should act on.
    # Info-level entries are archival; they show up in get_history(), not here.
    _ACTIONABLE = ("warning", "critical")

    def get_unacknowledged(self, limit: int = 5, severities=None) -> list:
        """Return unacknowledged notifications, newest first.

        severities: tuple of severities to include. Defaults to actionable
            ('warning', 'critical') so auto-chatter 'info' events don't
            drown real signals. Pass severities=() or ('info','warning','critical')
            to include info events (e.g. for an activity log view).
        """
        if severities is None:
            severities = self._ACTIONABLE
        with self._lock:
            self._maybe_collapse_locked()
            entries = [
                e for e in self._read_all()
                if not e.get("acknowledged")
                and (not severities or e.get("severity") in severities)
            ]
            entries.sort(key=_recency_key, reverse=True)
            return entries[:limit]

    def get_suppressed_info_count(self) -> int:
        """Count of unacknowledged 'info' notifications not shown in actionable view."""
        with self._lock:
            self._maybe_collapse_locked()
            return sum(
                1 for e in self._read_all()
                if not e.get("acknowledged") and e.get("severity") == "info"
            )

    def get_history(self, limit: int = 50) -> list:
        """Return all notifications (including acknowledged) for the activity console, newest first."""
        with self._lock:
            entries = self._read_all()
            entries.sort(key=_recency_key, reverse=True)
            return entries[:limit]

    def acknowledge(self, notification_id: str) -> bool:
        """Mark a single notification as acknowledged."""
        with self._lock:
            return self._set_ack(lambda e: e.get("id") == notification_id)

    def acknowledge_all(self) -> int:
        """Mark all unacknowledged notifications as acknowledged. Returns count."""
        with self._lock:
            entries = self._read_all()
            count = 0
            for e in entries:
                if not e.get("acknowledged"):
                    e["acknowledged"] = True
                    count += 1
            if count:
                self._write_all(entries)
            return count

    def get_pending_summary(self) -> str:
        """Format up to 3 unacked warning/critical notifications for prepending.

        Auto-acknowledges those included. Returns empty string if none.
        """
        with self._lock:
            self._maybe_collapse_locked()
            entries = self._read_all()
            pending = [
                e for e in entries
                if not e.get("acknowledged")
                and e.get("severity") in ("warning", "critical")
            ]
            # Newest first
            pending.sort(key=_recency_key, reverse=True)
            pending = pending[:3]

            if not pending:
                return ""

            # Auto-acknowledge
            pending_ids = {e["id"] for e in pending}
            for e in entries:
                if e.get("id") in pending_ids:
                    e["acknowledged"] = True
            self._write_all(entries)

            # Format
            lines = ["[c3:agents]"]
            for e in pending:
                prefix = "!!" if e["severity"] == "critical" else "!"
                count = _entry_count(e)
                repeat = f" (x{count})" if count > 1 else ""
                lines.append(f"{prefix} {e['agent']}: {e['title']} — {e['message']}{repeat}")
            return "\n".join(lines)

    def collapse_duplicates(self) -> int:
        """Retro-cleanup: merge unacknowledged duplicates into one record each.

        Unacknowledged entries sharing (agent, title) collapse into the most
        recent record, which absorbs the group's total ``count``, the max
        ``last_seen``, and the highest severity. The newest message wins —
        older same-title messages are stale snapshots of the same condition.
        Acknowledged history is never touched. Returns the number of duplicate
        records removed.
        """
        with self._lock:
            self._collapsed = True
            return self._collapse_locked()

    def _maybe_collapse_locked(self):
        """Run the duplicate collapse once per store instance. Caller must hold _lock."""
        if self._collapsed:
            return
        self._collapsed = True
        try:
            self._collapse_locked()
        except Exception:
            pass  # maintenance is best-effort — never break reads/writes

    def _collapse_locked(self) -> int:
        """Merge unacked duplicates (same agent+title). Caller must hold _lock."""
        entries = self._read_all()
        groups: dict[tuple, list] = {}
        for e in entries:
            if e.get("acknowledged"):
                continue
            groups.setdefault((e.get("agent"), e.get("title")), []).append(e)

        drop_ids: set[int] = set()
        removed = 0
        for group in groups.values():
            if len(group) < 2:
                continue
            survivor = max(group, key=_recency_key)
            survivor["count"] = sum(_entry_count(e) for e in group)
            survivor["last_seen"] = max(_recency_key(e) for e in group)
            survivor["severity"] = max(
                (e.get("severity", "info") for e in group),
                key=lambda s: _SEVERITY_RANK.get(s, 0),
            )
            for e in group:
                if e is not survivor:
                    drop_ids.add(id(e))
                    removed += 1
        if removed:
            self._write_all([e for e in entries if id(e) not in drop_ids])
        return removed

    def _read_all(self) -> list:
        """Read all entries from JSONL file. Caller must hold _lock."""
        if not self._file.exists():
            return []
        entries = []
        for line in self._file.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def _write_all(self, entries: list):
        """Rewrite entire file. Caller must hold _lock."""
        with open(self._file, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def _set_ack(self, predicate) -> bool:
        """Acknowledge entries matching predicate. Caller must hold _lock."""
        entries = self._read_all()
        found = False
        for e in entries:
            if not e.get("acknowledged") and predicate(e):
                e["acknowledged"] = True
                found = True
        if found:
            self._write_all(entries)
        return found
