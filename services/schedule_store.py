"""Machine-wide store for scheduled tasks, their runs and the audit trail.

Lives under ``~/.c3/oracle`` because the runner is per machine: one Oracle
owns the lock, the due-claim transaction and the approval inbox. Every row
still names its ``project_path`` so notifications and runs attach to the
right project.

Files: ``schedules.json`` (all schedule rows, ``rev``-versioned),
``schedule_runs/<run_id>.json`` (one mutable file per run),
``schedule_events.jsonl`` (append-only audit), ``schedules.lock``.

Concurrency: the Oracle tick, the Hub and an MCP server are separate
processes that may all mutate ``schedules.json``. There is no in-memory
cache; every mutation is load -> mutate -> atomic save under an in-process
``threading.Lock`` plus the OS byte lock in ``schedules.lock``. ``claim_due``
advances ``next_run_at`` inside that same transaction, so a row stops being
due the moment it is claimed, whichever process claimed it.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.atomic_json import write_json_atomic
from services.cron_expr import next_fire, parse_iso, validate_when

SCHEMA_VERSION = 1
KINDS = ("skill", "prompt", "command")
PROVIDERS = ("claude", "codex", "gemini", "grok")
EFFORT_LEVELS = ("", "low", "medium", "high", "xhigh", "max")
AUTONOMY = ("autonomous", "approval")
OVERLAP = ("skip", "allow")
MISFIRE_POLICIES = ("grace", "skip")
PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan")
TRIGGERS = ("cron", "manual", "approval", "check")

RUN_STATUSES = (
    "claimed", "skipped_overlap", "missed", "check_running", "check_skipped",
    "awaiting_approval", "approved", "denied", "expired",
    "dispatched", "running", "done", "failed", "timeout", "cancelled", "lost",
)
OPEN_RUN_STATUSES = frozenset(
    {"claimed", "check_running", "awaiting_approval", "approved", "dispatched", "running"})
TERMINAL_RUN_STATUSES = frozenset(RUN_STATUSES) - OPEN_RUN_STATUSES
EXECUTED_STATUSES = frozenset({"done", "failed", "timeout", "cancelled", "lost"})

MIN_TIMEOUT_S = 30
MAX_TIMEOUT_S = 6 * 3600
DEFAULT_TIMEOUT_S = 1800
DEFAULT_APPROVAL_TTL_S = 3600
DEFAULT_MISFIRE_GRACE_S = 900
RESULT_EXCERPT_CAP = 2000
_LOCK_TIMEOUT_S = 30.0
_EVENTS_ROTATE_BYTES = 5_000_000

_SCHEDULE_FIELDS = (
    "id", "name", "kind", "target", "provider", "model", "effort", "permission_mode",
    "allowed_tools", "max_budget_usd", "autonomy", "approval_ttl_s", "project_path", "cwd",
    "enabled", "timeout_s", "when", "misfire", "overlap", "check_command",
    "next_run_at", "last_run_at", "last_run_id", "last_status", "run_count",
    "created_at", "updated_at", "created_by",
)
_EDITABLE = frozenset(_SCHEDULE_FIELDS) - {
    "id", "next_run_at", "last_run_at", "last_run_id", "last_status", "run_count",
    "created_at", "updated_at", "created_by"}
_RUN_FIELDS = (
    "run_id", "schedule_id", "schedule_name", "project_path", "trigger", "due_at", "claimed_at",
    "dispatched_at", "finished_at", "status", "job_id", "approval_id", "exit_code", "duration_ms",
    "output_id", "cost_usd", "tokens", "turns", "model", "result_excerpt", "error",
)


class ScheduleError(ValueError):
    """A schedule or run argument is unusable; the message names the field."""


class RevConflict(ScheduleError):
    """``expected_rev`` no longer matches the document on disk."""


def default_root() -> Path:
    return Path.home() / ".c3" / "oracle"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _new_schedule_id() -> str:
    return "sch-" + uuid.uuid4().hex[:8]


def _new_run_id() -> str:
    return "sr-" + uuid.uuid4().hex[:10]


class _FileLock:
    """Advisory cross-process lock on a sidecar file, released by the OS on death."""

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    def acquire(self, timeout_s: float = _LOCK_TIMEOUT_S) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")  # the fd exists only to hold the lock
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    return False
                time.sleep(0.05)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"could not acquire {self.path.name} within {_LOCK_TIMEOUT_S:.0f}s")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def normalize_schedule(fields: dict, *, defaults: dict | None = None, existing: dict | None = None) -> dict:
    """Validated schedule row from ``fields`` (merged over ``existing`` on update).

    ``defaults`` supplies ``timeout_s``, ``max_timeout_s``, ``approval_ttl_s``,
    ``misfire_grace_s``, ``permission_mode``, ``allow_bypass_permissions`` and
    ``max_budget_usd`` when the caller omits them. Raises ``ScheduleError``.
    """
    d = defaults or {}
    row = dict(existing or {})
    unknown = set(fields) - _EDITABLE
    if unknown:
        raise ScheduleError(f"unknown field(s): {', '.join(sorted(unknown))}")
    row.update(fields)

    name = str(row.get("name") or "").strip()
    if not name:
        raise ScheduleError("name is required")
    kind = str(row.get("kind") or "skill").strip().lower()
    if kind not in KINDS:
        raise ScheduleError(f"kind must be one of {', '.join(KINDS)}")
    target = row.get("target")
    if not isinstance(target, dict):
        raise ScheduleError("target must be an object")
    target = _normalize_target(kind, target)
    provider = str(row.get("provider") or "claude").strip().lower()
    if provider not in PROVIDERS:
        raise ScheduleError(f"provider must be one of {', '.join(PROVIDERS)}")
    effort = str(row.get("effort") or "").strip().lower()
    if effort not in EFFORT_LEVELS:
        raise ScheduleError(f"effort must be one of {', '.join(e for e in EFFORT_LEVELS if e)}")
    mode = str(row.get("permission_mode") or d.get("permission_mode") or "acceptEdits").strip()
    if mode not in PERMISSION_MODES:
        raise ScheduleError(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    if mode == "bypassPermissions" and not d.get("allow_bypass_permissions", False):
        raise ScheduleError("permission_mode bypassPermissions is disabled by scheduler_allow_bypass_permissions")
    autonomy = str(row.get("autonomy") or "approval").strip().lower()
    if autonomy not in AUTONOMY:
        raise ScheduleError(f"autonomy must be one of {', '.join(AUTONOMY)}")
    overlap = str(row.get("overlap") or "skip").strip().lower()
    if overlap not in OVERLAP:
        raise ScheduleError(f"overlap must be one of {', '.join(OVERLAP)}")
    project_path = str(row.get("project_path") or "").strip()
    if not project_path:
        raise ScheduleError("project_path is required")
    cwd = str(row.get("cwd") or project_path).strip()

    timeout_s = _int_in(row.get("timeout_s", d.get("timeout_s", DEFAULT_TIMEOUT_S)),
                        MIN_TIMEOUT_S, int(d.get("max_timeout_s", MAX_TIMEOUT_S)), "timeout_s")
    approval_ttl_s = _int_in(row.get("approval_ttl_s", d.get("approval_ttl_s", DEFAULT_APPROVAL_TTL_S)),
                             60, 7 * 86400, "approval_ttl_s")
    misfire = row.get("misfire")
    if misfire is None:
        misfire = {}
    if not isinstance(misfire, dict):
        raise ScheduleError("misfire must be an object")
    policy = str(misfire.get("policy") or "grace").strip().lower()
    if policy not in MISFIRE_POLICIES:
        raise ScheduleError(f"misfire.policy must be one of {', '.join(MISFIRE_POLICIES)}")
    grace_s = _int_in(misfire.get("grace_s", d.get("misfire_grace_s", DEFAULT_MISFIRE_GRACE_S)),
                      0, 7 * 86400, "misfire.grace_s")
    try:
        budget = float(row.get("max_budget_usd", d.get("max_budget_usd", 0)) or 0)
    except (TypeError, ValueError) as exc:
        raise ScheduleError("max_budget_usd must be a number") from exc
    if budget < 0:
        raise ScheduleError("max_budget_usd must be >= 0")
    try:
        when = validate_when(row.get("when"))
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc

    row.update({
        "name": name, "kind": kind, "target": target, "provider": provider,
        "model": str(row.get("model") or "").strip(), "effort": effort,
        "permission_mode": mode, "allowed_tools": str(row.get("allowed_tools") or "").strip(),
        "max_budget_usd": budget, "autonomy": autonomy, "approval_ttl_s": approval_ttl_s,
        "project_path": project_path, "cwd": cwd, "enabled": bool(row.get("enabled", True)),
        "timeout_s": timeout_s, "when": when,
        "misfire": {"policy": policy, "grace_s": grace_s}, "overlap": overlap,
        "check_command": str(row.get("check_command") or "").strip(),
    })
    return row


def _normalize_target(kind: str, target: dict) -> dict:
    if kind == "skill":
        skill = str(target.get("skill") or "").strip()
        if not skill.startswith("/") or len(skill) < 2 or " " in skill:
            raise ScheduleError("target.skill must look like /skill-name")
        return {"skill": skill, "args": str(target.get("args") or "").strip()}
    if kind == "prompt":
        prompt = str(target.get("prompt") or "").strip()
        if not prompt:
            raise ScheduleError("target.prompt is required")
        return {"prompt": prompt}
    cmd = str(target.get("cmd") or "").strip()
    if not cmd:
        raise ScheduleError("target.cmd is required")
    return {"cmd": cmd}


def _int_in(value, lo: int, hi: int, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"{label} must be an integer") from exc
    if number < lo or number > hi:
        raise ScheduleError(f"{label} must be between {lo} and {hi}")
    return number


def prompt_text(schedule: dict) -> str:
    """The text handed to an LLM provider for a skill or prompt schedule."""
    target = schedule.get("target") or {}
    if schedule.get("kind") == "skill":
        args = target.get("args") or ""
        return f"{target.get('skill', '')} {args}".strip()
    return str(target.get("prompt") or "")


class ScheduleStore:
    """Schedules, runs and events under one root. See the module docstring."""

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else default_root()
        self.schedules_file = self.root / "schedules.json"
        self.runs_dir = self.root / "schedule_runs"
        self.events_file = self.root / "schedule_events.jsonl"
        self.lock_file = self.root / "schedules.lock"
        self._lock = threading.Lock()

    def _empty_doc(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "rev": 0, "schedules": []}

    def _load(self) -> dict:
        if not self.schedules_file.exists():
            return self._empty_doc()
        try:
            doc = json.loads(self.schedules_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._empty_doc()
        if not isinstance(doc, dict) or not isinstance(doc.get("schedules"), list):
            return self._empty_doc()
        doc.setdefault("schema_version", SCHEMA_VERSION)
        doc.setdefault("rev", 0)
        return doc

    def _save(self, doc: dict) -> None:
        doc["rev"] = int(doc.get("rev", 0)) + 1
        write_json_atomic(self.schedules_file, doc, ensure_ascii=False)

    def _txn(self):
        return _Txn(self)

    def _event(self, event: str, **data) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            if self.events_file.exists() and self.events_file.stat().st_size > _EVENTS_ROTATE_BYTES:
                os.replace(self.events_file, self.events_file.with_suffix(".jsonl.1"))
        except OSError:
            pass
        row = {"ts": _iso(_now()), "event": event, **data}
        with open(self.events_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def list(self, *, project_path: str | None = None, enabled: bool | None = None) -> list[dict]:
        rows = self._load()["schedules"]
        if project_path:
            key = _path_key(project_path)
            rows = [r for r in rows if _path_key(r.get("project_path", "")) == key]
        if enabled is not None:
            rows = [r for r in rows if bool(r.get("enabled")) == enabled]
        return sorted(rows, key=lambda r: (r.get("next_run_at") or "~", r.get("name", "")))

    def get(self, schedule_id: str) -> dict | None:
        for row in self._load()["schedules"]:
            if row.get("id") == schedule_id:
                return row
        return None

    def rev(self) -> int:
        return int(self._load().get("rev", 0))

    def create(self, fields: dict, *, defaults: dict | None = None, created_by: str = "") -> dict:
        now = _now()
        row = normalize_schedule(fields, defaults=defaults)
        row.update({
            "id": _new_schedule_id(), "created_at": _iso(now), "updated_at": _iso(now),
            "created_by": created_by, "last_run_at": None, "last_run_id": None,
            "last_status": None, "run_count": 0,
        })
        row["next_run_at"] = _iso(next_fire(row["when"], now)) if row["enabled"] else None
        with self._txn() as doc:
            doc["schedules"].append(row)
        self._event("schedule.created", schedule_id=row["id"], name=row["name"],
                    project_path=row["project_path"], by=created_by)
        return row

    def update(self, schedule_id: str, patch: dict, *, expected_rev: int | None = None,
               defaults: dict | None = None, by: str = "") -> dict:
        with self._txn() as doc:
            if expected_rev is not None and int(doc.get("rev", 0)) != int(expected_rev):
                raise RevConflict(f"rev is {doc.get('rev')}, expected {expected_rev}")
            row = _find(doc, schedule_id)
            if row is None:
                raise ScheduleError(f"no schedule {schedule_id}")
            updated = normalize_schedule(patch, defaults=defaults, existing=row)
            now = _now()
            updated["updated_at"] = _iso(now)
            if not updated["enabled"]:
                updated["next_run_at"] = None
            elif "when" in patch or not row.get("enabled") or not row.get("next_run_at"):
                updated["next_run_at"] = _iso(next_fire(updated["when"], now))
            row.clear()
            row.update(updated)
            result = dict(row)
        self._event("schedule.updated", schedule_id=schedule_id, fields=sorted(patch), by=by)
        return result

    def set_enabled(self, schedule_id: str, enabled: bool, *, by: str = "") -> dict:
        return self.update(schedule_id, {"enabled": bool(enabled)}, by=by)

    def delete(self, schedule_id: str, *, by: str = "") -> bool:
        with self._txn() as doc:
            before = len(doc["schedules"])
            doc["schedules"] = [r for r in doc["schedules"] if r.get("id") != schedule_id]
            removed = len(doc["schedules"]) != before
        if removed:
            self._event("schedule.deleted", schedule_id=schedule_id, by=by)
        return removed

    def claim_due(self, now: datetime | None = None) -> list[tuple[dict, dict]]:
        """Claim every enabled schedule whose ``next_run_at`` has passed.

        Inside one locked transaction each claimed row gets its ``next_run_at``
        advanced (a one-shot ``at`` is disabled instead) and a ``claimed`` run
        row is written, so a second caller sees nothing due. Returns
        ``(schedule, run)`` pairs; ``run.due_at`` keeps the slot that fired.
        """
        now = now or _now()
        claimed: list[tuple[dict, dict]] = []
        with self._txn() as doc:
            for row in doc["schedules"]:
                due = row.get("next_run_at")
                if not row.get("enabled") or not due:
                    continue
                if parse_iso(due) > now:
                    continue
                nxt = next_fire(row["when"], now)
                if row["when"].get("type") == "at" or nxt is None:
                    row["enabled"] = False
                    row["next_run_at"] = None
                else:
                    row["next_run_at"] = _iso(nxt)
                row["updated_at"] = _iso(now)
                run = self._new_run(row, trigger="cron", due_at=due, now=now)
                claimed.append((dict(row), run))
        for row, run in claimed:
            self._event("run.claimed", run_id=run["run_id"], schedule_id=row["id"],
                        due_at=run["due_at"], trigger="cron")
        return claimed

    def request_run(self, schedule_id: str, *, trigger: str = "manual", by: str = "",
                    approved: bool = False) -> dict:
        """A run outside the cadence (Run now); ``approved`` skips the approval gate once."""
        if trigger not in TRIGGERS:
            raise ScheduleError(f"trigger must be one of {', '.join(TRIGGERS)}")
        now = _now()
        with self._txn() as doc:
            row = _find(doc, schedule_id)
            if row is None:
                raise ScheduleError(f"no schedule {schedule_id}")
            run = self._new_run(row, trigger=trigger, due_at=_iso(now), now=now,
                                status="approved" if approved else "claimed")
        self._event("run.claimed", run_id=run["run_id"], schedule_id=schedule_id,
                    due_at=run["due_at"], trigger=trigger, by=by, status=run["status"])
        return run

    def _new_run(self, row: dict, *, trigger: str, due_at: str, now: datetime,
                 status: str = "claimed") -> dict:
        run = {k: None for k in _RUN_FIELDS}
        run.update({
            "run_id": _new_run_id(), "schedule_id": row["id"], "schedule_name": row.get("name", ""),
            "project_path": row.get("project_path", ""), "trigger": trigger, "due_at": due_at,
            "claimed_at": _iso(now), "status": status, "tokens": {}, "error": "",
        })
        self._write_run(run)
        return run

    def _run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def _write_run(self, run: dict) -> None:
        write_json_atomic(self._run_path(run["run_id"]), run, ensure_ascii=False, fsync=False)

    def get_run(self, run_id: str) -> dict | None:
        path = self._run_path(run_id)
        if not path.exists():
            return None
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return run if isinstance(run, dict) else None

    def update_run(self, run_id: str, **patch) -> dict:
        unknown = set(patch) - set(_RUN_FIELDS)
        if unknown:
            raise ScheduleError(f"unknown run field(s): {', '.join(sorted(unknown))}")
        status = patch.get("status")
        if status is not None and status not in RUN_STATUSES:
            raise ScheduleError(f"unknown run status {status!r}")
        with self._lock:
            run = self.get_run(run_id)
            if run is None:
                raise ScheduleError(f"no run {run_id}")
            run.update(patch)
            if "result_excerpt" in patch and run.get("result_excerpt"):
                run["result_excerpt"] = str(run["result_excerpt"])[:RESULT_EXCERPT_CAP]
            self._write_run(run)
        if status is not None:
            self._event("run.status", run_id=run_id, schedule_id=run.get("schedule_id"),
                        status=status, job_id=run.get("job_id"))
        return run

    def finish_run(self, run_id: str, status: str, **patch) -> dict:
        """Terminal update of a run plus the owning schedule's ``last_*`` summary."""
        if status not in TERMINAL_RUN_STATUSES:
            raise ScheduleError(f"{status!r} is not a terminal run status")
        now = _now()
        run = self.update_run(run_id, status=status, finished_at=_iso(now), **patch)
        with self._txn() as doc:
            row = _find(doc, run.get("schedule_id", ""))
            if row is not None:
                row["last_run_at"] = _iso(now)
                row["last_run_id"] = run_id
                row["last_status"] = status
                if status in EXECUTED_STATUSES:
                    row["run_count"] = int(row.get("run_count") or 0) + 1
        return run

    def runs(self, *, schedule_id: str | None = None, status=None, project_path: str | None = None,
             limit: int = 50) -> list[dict]:
        statuses = None
        if status:
            statuses = {status} if isinstance(status, str) else set(status)
        key = _path_key(project_path) if project_path else None
        out = []
        for run in self._iter_runs():
            if schedule_id and run.get("schedule_id") != schedule_id:
                continue
            if statuses and run.get("status") not in statuses:
                continue
            if key and _path_key(run.get("project_path", "")) != key:
                continue
            out.append(run)
        out.sort(key=lambda r: r.get("claimed_at") or "", reverse=True)
        return out[:max(0, int(limit))] if limit else out

    def open_runs(self) -> list[dict]:
        return self.runs(status=OPEN_RUN_STATUSES, limit=0)

    def open_run_for(self, schedule_id: str, *, exclude: str = "") -> dict | None:
        for run in self.open_runs():
            if run.get("schedule_id") == schedule_id and run.get("run_id") != exclude:
                return run
        return None

    def _iter_runs(self):
        if not self.runs_dir.exists():
            return
        for path in self.runs_dir.glob("sr-*.json"):
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(run, dict) and run.get("run_id"):
                yield run

    def sweep_runs(self, retention_days: int, now: datetime | None = None) -> int:
        """Delete terminal runs older than ``retention_days``; returns the count."""
        now = now or _now()
        cutoff = now - timedelta(days=max(1, int(retention_days)))
        removed = 0
        for run in list(self._iter_runs()):
            if run.get("status") in OPEN_RUN_STATUSES:
                continue
            stamp = run.get("finished_at") or run.get("claimed_at") or ""
            try:
                when = parse_iso(stamp)
            except ValueError:
                continue
            if when < cutoff:
                try:
                    self._run_path(run["run_id"]).unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


class _Txn:
    """``with store._txn() as doc:`` — locked load, mutate, save on success."""

    def __init__(self, store: ScheduleStore):
        self.store = store
        self.flock = _FileLock(store.lock_file)
        self.doc = None

    def __enter__(self) -> dict:
        self.store._lock.acquire()
        try:
            self.flock.__enter__()
        except Exception:
            self.store._lock.release()
            raise
        self.doc = self.store._load()
        return self.doc

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.store._save(self.doc)
        finally:
            self.flock.release()
            self.store._lock.release()
        return False


def _find(doc: dict, schedule_id: str) -> dict | None:
    for row in doc["schedules"]:
        if row.get("id") == schedule_id:
            return row
    return None


def _path_key(path: str) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(Path(path).resolve())))
    except OSError:
        return os.path.normcase(os.path.normpath(str(path)))
