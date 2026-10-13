"""Pending approvals for scheduled runs whose schedule says ``autonomy: approval``.

A row is created when such a run is claimed, lands in the project's
notification feed (``kind="schedule"``) so the Hub, the Desk and the phone can
show it, and is decided by a human only. The scheduler polls ``approved`` rows
and dispatches them; ``sweep_expired`` lapses the rest at their TTL. Spawning
never happens here.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.atomic_json import write_json_atomic
from services.notifications import notify
from services.schedule_store import _FileLock, _path_key, default_root, parse_iso

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED, STATUS_EXPIRED)
DECISIONS = ("approve", "deny")
NOTE_CAP = 400
RETAIN_DECIDED_DAYS = 30


class ApprovalError(ValueError):
    """The approval does not exist or is no longer pending."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class ApprovalStore:
    """``schedule_approvals.json`` under the Oracle root; see the module docstring."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else default_root()
        self.file = self.root / "schedule_approvals.json"
        self.lock_file = self.root / "schedule_approvals.lock"
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        if not self.file.exists():
            return []
        try:
            rows = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _save(self, rows: list[dict]) -> None:
        write_json_atomic(self.file, rows, ensure_ascii=False)

    def _txn(self):
        return _Txn(self)

    def list(self, *, status: str | None = None, project_path: str | None = None,
             limit: int = 100) -> list[dict]:
        rows = self._load()
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if project_path:
            key = _path_key(project_path)
            rows = [r for r in rows if _path_key(r.get("project_path", "")) == key]
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return rows[:limit] if limit else rows

    def get(self, approval_id: str) -> dict | None:
        for row in self._load():
            if row.get("id") == approval_id:
                return row
        return None

    def pending_for(self, schedule_id: str) -> dict | None:
        for row in self._load():
            if row.get("schedule_id") == schedule_id and row.get("status") == STATUS_PENDING:
                return row
        return None

    def create(self, schedule: dict, run: dict, *, ttl_s: int) -> dict:
        """A pending row for ``run``; an existing pending row for the same schedule is returned instead."""
        now = _now()
        with self._txn() as rows:
            for row in rows:
                if row.get("schedule_id") == schedule.get("id") and row.get("status") == STATUS_PENDING:
                    return dict(row, duplicate=True)
            row = {
                "id": "sa-" + uuid.uuid4().hex[:10],
                "schedule_id": schedule.get("id"), "schedule_name": schedule.get("name", ""),
                "run_id": run.get("run_id"), "project_path": schedule.get("project_path", ""),
                "summary": _summary(schedule), "status": STATUS_PENDING,
                "created_at": _iso(now), "expires_at": _iso(now + timedelta(seconds=max(60, int(ttl_s)))),
                "resolved_at": None, "decided_by": "", "note": "",
            }
            rows.append(row)
        notify(row["project_path"], agent="scheduler", severity="warning",
               title=f"Approve scheduled run: {row['schedule_name']}?",
               message=f"{row['summary']} — expires {row['expires_at']}",
               kind="schedule", ref_id=row["id"])
        return row

    def decide(self, approval_id: str, decision: str, *, decided_by: str, note: str = "") -> dict:
        """Human decision on a pending row. Raises ``ApprovalError`` when it is not pending."""
        if decision not in DECISIONS:
            raise ApprovalError(f"decision must be one of {', '.join(DECISIONS)}")
        now = _now()
        with self._txn() as rows:
            row = next((r for r in rows if r.get("id") == approval_id), None)
            if row is None:
                raise ApprovalError(f"no approval {approval_id}")
            if row.get("status") != STATUS_PENDING:
                raise ApprovalError(f"approval {approval_id} is already {row.get('status')}")
            if parse_iso(row["expires_at"]) <= now:
                row["status"] = STATUS_EXPIRED
                row["resolved_at"] = _iso(now)
                raise ApprovalError(f"approval {approval_id} expired at {row['expires_at']}")
            row["status"] = STATUS_APPROVED if decision == "approve" else STATUS_DENIED
            row["resolved_at"] = _iso(now)
            row["decided_by"] = str(decided_by or "")[:80]
            row["note"] = str(note or "")[:NOTE_CAP]
            result = dict(row)
        notify(result["project_path"], agent="scheduler", severity="info",
               title=f"Scheduled run {result['status']}: {result['schedule_name']}",
               message=f"by {result['decided_by'] or 'unknown'}" + (f" — {result['note']}" if result["note"] else ""),
               kind="schedule", ref_id=result["id"])
        return result

    def sweep_expired(self, now: datetime | None = None) -> list[dict]:
        """Lapse pending rows past ``expires_at`` and drop decided rows older than the retention."""
        now = now or _now()
        cutoff = now - timedelta(days=RETAIN_DECIDED_DAYS)
        expired: list[dict] = []
        with self._txn() as rows:
            keep = []
            for row in rows:
                if row.get("status") == STATUS_PENDING and parse_iso(row["expires_at"]) <= now:
                    row["status"] = STATUS_EXPIRED
                    row["resolved_at"] = _iso(now)
                    expired.append(dict(row))
                stamp = row.get("resolved_at") or row.get("created_at") or ""
                if row.get("status") != STATUS_PENDING and stamp and parse_iso(stamp) < cutoff:
                    continue
                keep.append(row)
            rows[:] = keep
        for row in expired:
            notify(row["project_path"], agent="scheduler", severity="info",
                   title=f"Scheduled run expired unapproved: {row['schedule_name']}",
                   message=row["summary"], kind="schedule", ref_id=row["id"])
        return expired


class _Txn:
    def __init__(self, store: ApprovalStore):
        self.store = store
        self.flock = _FileLock(store.lock_file)
        self.rows: list[dict] = []

    def __enter__(self) -> list[dict]:
        self.store._lock.acquire()
        try:
            self.flock.__enter__()
        except Exception:
            self.store._lock.release()
            raise
        self.rows = self.store._load()
        return self.rows

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None or isinstance(exc, ApprovalError):
                self.store._save(self.rows)
        finally:
            self.flock.release()
            self.store._lock.release()
        return False


def _summary(schedule: dict) -> str:
    target = schedule.get("target") or {}
    if schedule.get("kind") == "skill":
        what = f"{target.get('skill', '')} {target.get('args') or ''}".strip()
    elif schedule.get("kind") == "prompt":
        what = str(target.get("prompt") or "")[:120]
    else:
        what = str(target.get("cmd") or "")[:120]
    parts = [what]
    if schedule.get("kind") != "command":
        parts.append(schedule.get("provider", "claude") + (f"/{schedule['model']}" if schedule.get("model") else ""))
        if schedule.get("effort"):
            parts.append(f"effort {schedule['effort']}")
    parts.append(f"in {schedule.get('cwd') or schedule.get('project_path', '')}")
    return " · ".join(p for p in parts if p)
