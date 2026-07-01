"""ActivityLog — Append-only JSONL activity log for C3 events."""
import json
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path


class ActivityLog:
    """Persistent activity log stored as .c3/activity_log.jsonl.

    Size-capped: when the live file exceeds the configured threshold
    (retention.activity_log_max_mb, default 5MB) it is rotated into
    .c3/archive/activity_log.<date>.jsonl.gz. Readers here only scan the
    live file, which the rotation keeps bounded.
    """

    def __init__(self, project_path: str):
        self.project_path = str(project_path)
        self.log_file = Path(project_path) / ".c3" / "activity_log.jsonl"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, data: dict) -> dict:
        """Append an event. Returns the written entry.

        event_type: tool_call, decision, file_change, fact_stored,
                    session_start, session_save
        """
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **data,
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self._maybe_rotate()
        return entry

    def _maybe_rotate(self) -> None:
        """Cheap per-append size check; rotate into the archive when over cap.

        Failure-safe: retention problems must never break event logging.
        """
        try:
            from services.retention import (
                archive_dir_for,
                load_retention_config,
                mb_to_bytes,
                rotate_jsonl,
            )
            cfg = load_retention_config(self.project_path)
            if not cfg.get("enabled", True):
                return
            rotate_jsonl(
                self.log_file,
                mb_to_bytes(cfg.get("activity_log_max_mb", 5)),
                archive_dir_for(self.project_path),
            )
        except Exception:
            pass

    def get_recent(self, limit: int = 100, event_type: str = None,
                    since: str = None, until: str = None) -> list:
        """Read last N events, optionally filtered by type and time range.

        since/until: ISO timestamp strings for inclusive time-range filtering.
        """
        if not self.log_file.exists():
            return []
        events = []
        # When filtering by event_type, rare events (e.g. session_start) may be
        # far back in the log behind many tool_call entries.  Use a larger scan
        # window so they aren't missed.
        scan_factor = 100 if event_type else 5
        tail = deque(maxlen=max(1, limit * scan_factor))
        with open(self.log_file, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    tail.append(line)
        for line in reversed(tail):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_type and entry.get("type") != event_type:
                continue
            ts = entry.get("timestamp", "")
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            events.append(entry)
            if len(events) >= limit:
                break
        return events

    def get_stats(self) -> dict:
        """Counts by event type, total events, time range."""
        if not self.log_file.exists():
            return {"total": 0, "by_type": {}, "first": None, "last": None}
        counts = Counter()
        first_ts = None
        last_ts = None
        total = 0
        with open(self.log_file, encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                total += 1
                counts[entry.get("type", "unknown")] += 1
                ts = entry.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
        return {
            "total": total,
            "by_type": dict(counts),
            "first": first_ts,
            "last": last_ts,
        }
