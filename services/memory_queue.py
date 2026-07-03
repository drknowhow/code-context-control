"""Durable job queue for memory distillation.

Jobs live as single JSON files under .c3/memory_queue/, keyed by
(kind, session_id) so re-enqueueing the same session overwrites rather
than duplicates — on_session_end can fire more than once per process
(c3_session save/snapshot AND the MCP lifespan finally block).

Writes are atomic (tmp + os.replace): a job must survive the process
being killed mid-shutdown so the next session's MemoryDistillerAgent
can pick it up.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

TERMINAL_STATUSES = {"done", "done_degraded", "failed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryQueue:
    """File-backed queue of pending distillation jobs."""

    def __init__(self, project_path: str, data_dir: str = ".c3/memory_queue"):
        self.dir = Path(project_path) / data_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, kind: str, session_id: str) -> Path:
        safe_sid = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(session_id))
        return self.dir / f"{kind}_{safe_sid}.json"

    def _read(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write(self, path: Path, job: dict):
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def enqueue(self, kind: str, session_id: str, payload: dict) -> dict:
        """Create or overwrite the job for (kind, session_id) as pending."""
        path = self._path(kind, session_id)
        existing = self._read(path) or {}
        job = {
            "job_id": existing.get("job_id") or uuid.uuid4().hex[:12],
            "kind": kind,
            "session_id": str(session_id),
            "created_at": existing.get("created_at") or _now(),
            "updated_at": _now(),
            "attempts": 0,
            "status": "pending",
            "last_error": "",
            "tier_used": "",
            "payload": payload or {},
        }
        self._write(path, job)
        return job

    def claim_pending(self) -> list[dict]:
        """Return all pending jobs, oldest first."""
        jobs = []
        for path in self.dir.glob("*.json"):
            job = self._read(path)
            if job and job.get("status") == "pending":
                jobs.append(job)
        jobs.sort(key=lambda j: j.get("created_at", ""))
        return jobs

    def mark(self, job: dict, status: str, error: str = "", tier_used: str = "") -> dict:
        """Persist a status transition. status='pending' counts a failed attempt."""
        job = dict(job)
        if status == "pending":
            job["attempts"] = int(job.get("attempts", 0)) + 1
        job["status"] = status
        job["updated_at"] = _now()
        if error:
            job["last_error"] = str(error)[:500]
        if tier_used:
            job["tier_used"] = tier_used
        self._write(self._path(job.get("kind", "job"), job.get("session_id", "")), job)
        return job

    def prune(self, keep_days: int = 14) -> int:
        """Delete terminal jobs older than keep_days. Returns count removed."""
        removed = 0
        cutoff = datetime.now(timezone.utc).timestamp() - keep_days * 86400
        for path in self.dir.glob("*.json"):
            job = self._read(path)
            if not job or job.get("status") not in TERMINAL_STATUSES:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed
