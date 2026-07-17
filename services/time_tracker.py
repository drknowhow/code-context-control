"""Per-project time tracking: automatic activity sessions + manual entries.

Automatic: the MCP server pings on startup (the IDE loading the c3 server
signals work has begun) and on every tool call (throttled to one write per
minute). Pings land in monthly JSONL files
(``.c3/time/pings-YYYY-MM.jsonl``) and are coalesced into work sessions on
read: pings closer together than ``idle_gap_min`` belong to one session,
whose duration is first-ping -> last-ping (an isolated ping counts as
``min_session_min``). This measures *active* time — an IDE window left open
overnight adds nothing once the idle gap closes the session.

Manual: full-CRUD entries (``.c3/time/entries.json``) for time logged by
hand, with the same cross-process lock + atomic-write discipline as
TaskStore (the lock class is shared from there).
"""

import json
import os
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.task_store import _FileLock

ENTRY_SCHEMA_VERSION = 1
PING_THROTTLE_S = 60.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _valid_date(value) -> bool:
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


class TimeTracker:
    """Activity pings + manual time entries for one project."""

    def __init__(self, project_path, idle_gap_min=15, min_session_min=1):
        self.project_path = Path(project_path)
        self.data_dir = self.project_path / ".c3" / "time"
        self.entries_file = self.data_dir / "entries.json"
        self.lock_file = self.data_dir / "time.lock"
        self.idle_gap_min = max(1, int(idle_gap_min))
        self.min_session_min = max(1, int(min_session_min))
        self._lock = threading.Lock()
        self._last_ping = 0.0

    # ── Automatic pings ────────────────────────────────────────────

    def _ping_file(self, month=None):
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        return self.data_dir / f"pings-{month}.jsonl"

    def ping(self, source="tool", session_id="") -> bool:
        """Throttled activity heartbeat; safe to call on every tool call.

        ``startup`` pings bypass the throttle so a fresh IDE launch is
        always recorded as the start of a work session.
        """
        now = _time.time()
        if source != "startup" and (now - self._last_ping) < PING_THROTTLE_S:
            return False
        self._last_ping = now
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps({"ts": _now(), "source": source,
                               "session": session_id or "", "pid": os.getpid()})
            with self._lock, open(self._ping_file(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True
        except OSError:
            return False

    def _read_pings(self, months=2) -> list:
        """Sorted ping timestamps from the last N monthly files."""
        out = []
        cursor = datetime.now(timezone.utc).replace(day=1)
        for _ in range(max(1, int(months))):
            path = self._ping_file(cursor.strftime("%Y-%m"))
            if path.exists():
                try:
                    for ln in path.read_text(encoding="utf-8").splitlines():
                        try:
                            out.append(datetime.fromisoformat(
                                json.loads(ln).get("ts")))
                        except (ValueError, TypeError):
                            continue
                except OSError:
                    pass
            cursor = (cursor - timedelta(days=1)).replace(day=1)
        out.sort()
        return out

    def sessions(self, months=2) -> list:
        """Coalesced work sessions, newest first."""
        gap = timedelta(minutes=self.idle_gap_min)
        out = []
        start = end = None
        for ts in self._read_pings(months):
            if start is None:
                start = end = ts
            elif ts - end <= gap:
                end = ts
            else:
                out.append(self._session(start, end))
                start = end = ts
        if start is not None:
            out.append(self._session(start, end))
        out.reverse()
        return out

    def _session(self, start, end) -> dict:
        minutes = max(self.min_session_min,
                      int(round((end - start).total_seconds() / 60)))
        return {"start": start.isoformat(), "end": end.isoformat(),
                "date": start.strftime("%Y-%m-%d"), "minutes": minutes}

    # ── Manual entries (full CRUD) ─────────────────────────────────

    def _empty_doc(self) -> dict:
        return {"schema_version": ENTRY_SCHEMA_VERSION, "entries": []}

    def _load(self) -> dict:
        if not self.entries_file.exists():
            return self._empty_doc()
        try:
            doc = json.loads(self.entries_file.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
                raise ValueError("malformed entries.json")
            return doc
        except Exception:
            for n in range(1, 100):
                target = self.entries_file.with_name(f"entries.json.corrupt-{n}")
                if not target.exists():
                    try:
                        os.replace(self.entries_file, target)
                    except OSError:
                        pass
                    break
            return self._empty_doc()

    def _save(self, doc) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.entries_file.with_name(
            f"entries.json.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.entries_file)

    @staticmethod
    def _resolve(entries, ref):
        ref = (ref or "").strip()
        if not ref:
            return None
        for e in entries:
            if e.get("id") == ref:
                return e
        if len(ref) >= 4:
            matches = [e for e in entries
                       if str(e.get("id", "")).startswith(ref)]
            if len(matches) == 1:
                return matches[0]
        return None

    @staticmethod
    def _validate_minutes(minutes):
        try:
            m = int(minutes)
        except (TypeError, ValueError):
            return None, "minutes must be an integer"
        if not 1 <= m <= 1440:
            return None, "minutes must be between 1 and 1440"
        return m, None

    def add_entry(self, minutes, note="", date=None, task_id=None,
                  created_by="") -> dict:
        m, err = self._validate_minutes(minutes)
        if err:
            return {"error": err}
        if date and not _valid_date(date):
            return {"error": "date must be YYYY-MM-DD"}
        now = _now()
        entry = {
            "id": uuid.uuid4().hex[:12], "date": date or _today(),
            "minutes": m, "note": (note or "").strip(),
            "task_id": task_id or None, "source": "manual",
            "created_by": created_by or "",
            "created_at": now, "updated_at": now,
        }
        with self._lock, _FileLock(self.lock_file):
            doc = self._load()
            doc["entries"].append(entry)
            self._save(doc)
        return dict(entry)

    _ENTRY_FIELDS = {"minutes", "note", "date", "task_id"}

    def update_entry(self, entry_id, **fields) -> dict:
        unknown = set(fields) - self._ENTRY_FIELDS
        if unknown:
            return {"error": f"unknown fields: {sorted(unknown)}"}
        if "minutes" in fields:
            m, err = self._validate_minutes(fields["minutes"])
            if err:
                return {"error": err}
            fields["minutes"] = m
        if "date" in fields and not _valid_date(fields["date"]):
            return {"error": "date must be YYYY-MM-DD"}
        with self._lock, _FileLock(self.lock_file):
            doc = self._load()
            entry = self._resolve(doc["entries"], entry_id)
            if entry is None:
                return {"error": f"no time entry matches: {entry_id}"}
            entry.update(fields)
            entry["updated_at"] = _now()
            self._save(doc)
            return dict(entry)

    def delete_entry(self, entry_id) -> dict:
        with self._lock, _FileLock(self.lock_file):
            doc = self._load()
            entry = self._resolve(doc["entries"], entry_id)
            if entry is None:
                return {"error": f"no time entry matches: {entry_id}"}
            doc["entries"] = [e for e in doc["entries"] if e is not entry]
            self._save(doc)
            return {"deleted": True, "entry": dict(entry)}

    def list_entries(self, limit=100) -> list:
        doc = self._load()
        out = [dict(e) for e in doc["entries"]]
        out.sort(key=lambda e: (e.get("date", ""), e.get("created_at", "")),
                 reverse=True)
        return out[: max(1, int(limit))]

    # ── Aggregates ─────────────────────────────────────────────────

    def summary(self, months=2) -> dict:
        sessions = self.sessions(months)
        entries = self.list_entries(limit=1000)
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        cutoff7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        cutoff30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")

        auto_by_day, manual_by_day = {}, {}
        for s in sessions:
            auto_by_day[s["date"]] = auto_by_day.get(s["date"], 0) + s["minutes"]
        for e in entries:
            d = e.get("date") or today
            manual_by_day[d] = manual_by_day.get(d, 0) + int(e.get("minutes") or 0)

        def pack(since):
            auto = sum(v for d, v in auto_by_day.items() if d >= since)
            manual = sum(v for d, v in manual_by_day.items() if d >= since)
            return {"auto_min": auto, "manual_min": manual,
                    "total_min": auto + manual}

        by_day = []
        for i in range(13, -1, -1):
            d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            by_day.append({"date": d,
                           "auto_min": auto_by_day.get(d, 0),
                           "manual_min": manual_by_day.get(d, 0)})
        return {
            "today": pack(today),
            "last_7d": pack(cutoff7),
            "last_30d": pack(cutoff30),
            "by_day": by_day,
            "sessions_count": len(sessions),
        }