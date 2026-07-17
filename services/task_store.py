"""Per-project project-management store: tasks, milestones, decision notes.

One document — ``.c3/pm/pm.json`` — holding all three collections, so
cross-entity operations (milestone archive detaching tasks, etc.) are a
single atomic write under one lock.

Concurrency model: the hub Flask server, the MCP stdio server, and the
per-project web server are SEPARATE PROCESSES that may all mutate the same
file. TaskStore therefore keeps **no in-memory cache** — every operation is
``load -> mutate -> atomic save`` under a ``threading.Lock`` (in-process
serialization) plus a ``pm.lock`` OS file lock (cross-process
serialization), unique temp + fsync + ``os.replace`` (crash safety), and a
monotonic document ``rev`` for optimistic concurrency (``expected_rev``).
``pm.json.bak`` keeps the previous good document; a corrupt ``pm.json`` is
quarantined, restored from the backup when possible, and the recovery is
surfaced via ``TaskStore.last_recovery``.
"""

import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
STATUSES = ("backlog", "in_progress", "blocked", "done")
PRIORITIES = ("p0", "p1", "p2", "p3")
LINK_TYPES = ("file", "commit", "edit")
NOTE_KINDS = ("note", "decision")
_RANK_STEP = 1024.0
_MIN_GAP = 1e-6
_EVENTS_ROTATE_BYTES = 5_000_000

# Schema migrations: _MIGRATIONS[n] upgrades a doc from schema_version n to
# n+1. Register with @_migration(n) as SCHEMA_VERSION grows past 1.
_MIGRATIONS = {}


def _migration(from_version):
    def register(fn):
        _MIGRATIONS[from_version] = fn
        return fn
    return register


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _valid_date(value) -> bool:
    if value is None or value == "":
        return True
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
        return True
    except ValueError:
        return False


_LOCK_TIMEOUT_S = 30.0


class _FileLock:
    """Advisory cross-process lock on a sidecar file (pm.lock).

    Held for the duration of one load->mutate->save transaction; the OS
    releases it if the holder dies. Windows locks a byte via msvcrt, POSIX
    uses flock. Bounded acquire so a wedged holder cannot hang callers.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._fh = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "ab")  # never read/written; fd exists to hold the lock
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise TimeoutError(
                        f"could not acquire {self.path.name} within "
                        f"{_LOCK_TIMEOUT_S:.0f}s")
                time.sleep(0.05)

    def release(self):
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
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def open_task_count(project_path) -> int:
    """Cheap open-task counter for hub card chips (no store instantiation)."""
    pm_file = Path(project_path) / ".c3" / "pm" / "pm.json"
    if not pm_file.exists():
        return 0
    try:
        doc = json.loads(pm_file.read_text(encoding="utf-8"))
        return sum(
            1 for t in doc.get("tasks", [])
            if isinstance(t, dict)
            and t.get("lifecycle", "active") == "active"
            and t.get("status") != "done"
        )
    except Exception:
        return 0


class TaskStore:
    """Tasks / milestones / notes for one project. See module docstring."""

    def __init__(self, project_path: str, data_dir: str = ".c3/pm"):
        self.project_path = Path(project_path)
        self.data_dir = self.project_path / data_dir
        self.pm_file = self.data_dir / "pm.json"
        self.bak_file = self.data_dir / "pm.json.bak"
        self.events_file = self.data_dir / "events.jsonl"
        self.lock_file = self.data_dir / "pm.lock"
        self._lock = threading.Lock()
        self._pending_events = []
        self.last_recovery = None  # set when _load quarantines/restores a corrupt doc

    # ── Persistence ────────────────────────────────────────────────

    def _empty_doc(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "rev": 0,
                "tasks": [], "milestones": [], "notes": []}

    def _load(self) -> dict:
        if not self.pm_file.exists():
            # Quarantined or manually deleted primary: serve the backup so the
            # store never silently restarts empty while good data exists. The
            # next mutation persists it back to pm.json.
            if self.bak_file.exists():
                try:
                    return self._normalize(self._parse(self.bak_file))
                except Exception:
                    pass
            return self._empty_doc()
        try:
            doc = self._parse(self.pm_file)
        except Exception:
            quarantined = self._quarantine()
            doc = None
            if self.bak_file.exists():
                try:
                    doc = self._parse(self.bak_file)
                except Exception:
                    doc = None
            self.last_recovery = {
                "quarantined": quarantined,
                "restored_from_backup": doc is not None,
                "at": _now(),
            }
            if doc is None:
                return self._empty_doc()
        return self._normalize(doc)

    def _normalize(self, doc: dict) -> dict:
        """Run registered schema migrations, then backfill structural defaults."""
        v = int(doc.get("schema_version", 1))
        while v < SCHEMA_VERSION and v in _MIGRATIONS:
            doc = _MIGRATIONS[v](doc)
            v += 1
            doc["schema_version"] = v
        doc.setdefault("schema_version", SCHEMA_VERSION)
        doc.setdefault("rev", 0)
        doc.setdefault("milestones", [])
        doc.setdefault("notes", [])
        return doc

    @staticmethod
    def _parse(path) -> dict:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("tasks"), list):
            raise ValueError("malformed pm.json")
        return doc

    def _quarantine(self) -> str:
        """Set the corrupt pm.json aside for inspection; never overwrite it."""
        for n in range(1, 100):
            target = self.pm_file.with_name(f"pm.json.corrupt-{n}")
            if not target.exists():
                try:
                    os.replace(self.pm_file, target)
                    return target.name
                except OSError:
                    break
        return ""

    def _save(self, doc: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        doc["rev"] = int(doc.get("rev", 0)) + 1
        tmp = self.pm_file.with_name(
            f"pm.json.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if self.pm_file.exists():
            try:
                shutil.copy2(self.pm_file, self.bak_file)
            except OSError:
                pass  # backup is best-effort; the atomic replace is not
        os.replace(tmp, self.pm_file)
        self._fsync_dir()
        self._sweep_stale_tmp()
        self._flush_events(doc["rev"])

    def _fsync_dir(self) -> None:
        if os.name == "nt":
            return  # directory handles cannot be fsynced on Windows
        try:
            fd = os.open(self.data_dir, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    def _sweep_stale_tmp(self) -> None:
        # Unique temp names mean a crashed writer leaves litter; sweep old ones.
        try:
            cutoff = time.time() - 3600
            for p in self.data_dir.glob("pm.json.tmp-*"):
                if p.stat().st_mtime < cutoff:
                    p.unlink()
        except OSError:
            pass

    def _event(self, entity, op, item_id="", patch=None, data=None, actor=""):
        """Queue a history event; _save stamps rev/ts and appends it."""
        self._pending_events.append({
            "entity": entity, "op": op, "id": item_id,
            "actor": actor or "", "patch": patch or None, "data": data or None,
        })

    def _flush_events(self, rev) -> None:
        if not self._pending_events:
            return
        events, self._pending_events = self._pending_events, []
        try:
            self._rotate_events()
            with open(self.events_file, "a", encoding="utf-8") as f:
                now = _now()
                for ev in events:
                    ev["ts"] = now
                    ev["rev"] = rev
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass  # history is best-effort; the snapshot save already succeeded

    def _rotate_events(self) -> None:
        try:
            if (self.events_file.exists()
                    and self.events_file.stat().st_size > _EVENTS_ROTATE_BYTES):
                os.replace(self.events_file,
                           self.events_file.with_name("events.jsonl.1"))
        except OSError:
            pass

    def history(self, entity=None, item_id=None, op=None, limit=50) -> list:
        """Read the event log, newest first (current file only, not rotated)."""
        try:
            lines = self.events_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if entity and ev.get("entity") != entity:
                continue
            if item_id and ev.get("id") != item_id:
                continue
            if op and ev.get("op") != op:
                continue
            out.append(ev)
            if len(out) >= max(1, int(limit)):
                break
        return out

    @contextmanager
    def _guard(self):
        """One mutation transaction: thread lock + cross-process file lock."""
        with self._lock, _FileLock(self.lock_file):
            self._pending_events = []
            yield

    @staticmethod
    def _rev_conflict(doc, expected_rev):
        """Optimistic-concurrency precondition; None when it passes."""
        if expected_rev is None or expected_rev == "":
            return None
        try:
            expected = int(expected_rev)
        except (TypeError, ValueError):
            return {"error": "expected_rev must be an integer"}
        current = int(doc.get("rev", 0))
        if current != expected:
            return {"error": f"revision conflict: doc is at rev {current}, "
                             f"expected {expected}",
                    "code": "rev_conflict", "current_rev": current}
        return None

    # ── Shared helpers ─────────────────────────────────────────────

    @staticmethod
    def _resolve(items: list, ref: str):
        """Exact id match, then unique prefix (>=4 chars)."""
        ref = (ref or "").strip()
        if not ref:
            return None
        for item in items:
            if item.get("id") == ref:
                return item
        if len(ref) >= 4:
            matches = [i for i in items if str(i.get("id", "")).startswith(ref)]
            if len(matches) == 1:
                return matches[0]
        return None

    @staticmethod
    def _column(tasks: list, status: str) -> list:
        col = [t for t in tasks
               if t.get("status") == status and t.get("lifecycle") == "active"]
        col.sort(key=lambda t: (t.get("sort_key", 0.0), t.get("created_at", "")))
        return col

    @staticmethod
    def _next_rank(tasks: list, status: str) -> float:
        col = [t.get("sort_key", 0.0) for t in tasks
               if t.get("status") == status and t.get("lifecycle") == "active"]
        return (max(col) + _RANK_STEP) if col else _RANK_STEP

    def _rebalance(self, tasks: list, status: str) -> None:
        for i, t in enumerate(self._column(tasks, status)):
            t["sort_key"] = _RANK_STEP * (i + 1)

    # ── Tasks ──────────────────────────────────────────────────────

    def create_task(self, title, description="", status="backlog", priority="p2",
                    due_date=None, tags=None, milestone_id=None, links=None,
                    created_by="", origin_session="") -> dict:
        title = (title or "").strip()
        if not title:
            return {"error": "title is required"}
        if status not in STATUSES:
            return {"error": f"status must be one of {list(STATUSES)}"}
        if priority not in PRIORITIES:
            return {"error": f"priority must be one of {list(PRIORITIES)}"}
        if not _valid_date(due_date):
            return {"error": "due_date must be YYYY-MM-DD"}
        clean_links, err = self._clean_links(links)
        if err:
            return {"error": err}
        now = _now()
        with self._guard():
            doc = self._load()
            if milestone_id:
                ms = self._resolve(doc["milestones"], milestone_id)
                if ms is None:
                    return {"error": f"no milestone matches: {milestone_id}"}
                milestone_id = ms["id"]
            task = {
                "id": _new_id(),
                "title": title,
                "description": description or "",
                "status": status,
                "priority": priority,
                "due_date": due_date or None,
                "tags": [t for t in (tags or []) if t],
                "milestone_id": milestone_id or None,
                "links": clean_links,
                "sort_key": self._next_rank(doc["tasks"], status),
                "lifecycle": "active",
                "created_at": now,
                "updated_at": now,
                "completed_at": now if status == "done" else None,
                "created_by": created_by or "",
                "origin_session": origin_session or "",
            }
            doc["tasks"].append(task)
            self._event("task", "create", task["id"], data=dict(task),
                        actor=created_by)
            self._save(doc)
            return dict(task)

    _TASK_FIELDS = {"title", "description", "status", "priority", "due_date",
                    "tags", "milestone_id"}

    @classmethod
    def _validate_task_fields(cls, fields):
        """Returns an error string, or None when the fields are valid."""
        unknown = set(fields) - cls._TASK_FIELDS
        if unknown:
            return f"unknown fields: {sorted(unknown)}"
        if "status" in fields and fields["status"] not in STATUSES:
            return f"status must be one of {list(STATUSES)}"
        if "priority" in fields and fields["priority"] not in PRIORITIES:
            return f"priority must be one of {list(PRIORITIES)}"
        if "due_date" in fields and not _valid_date(fields["due_date"]):
            return "due_date must be YYYY-MM-DD"
        if "title" in fields and not (fields["title"] or "").strip():
            return "title cannot be empty"
        return None

    @staticmethod
    def _stamp_status_change(task, old_status):
        if task["status"] == "done":
            task["completed_at"] = _now()
        elif old_status == "done":
            task["completed_at"] = None

    def _apply_fields(self, doc, task, fields):
        """Mutate task in place. Returns an error string, or None."""
        if "milestone_id" in fields and fields["milestone_id"]:
            ms = self._resolve(doc["milestones"], fields["milestone_id"])
            if ms is None:
                return f"no milestone matches: {fields['milestone_id']}"
            fields["milestone_id"] = ms["id"]
        old_status = task["status"]
        for key, val in fields.items():
            task[key] = val
        if "status" in fields and fields["status"] != old_status:
            task["sort_key"] = self._next_rank(
                [t for t in doc["tasks"] if t is not task], fields["status"])
            self._stamp_status_change(task, old_status)
        return None

    def _apply_move(self, doc, task, status=None, before_id=None, after_id=None):
        """Column and/or rank move. Returns an error string, or None."""
        old_status = task["status"]
        if status and status != old_status:
            task["status"] = status
            task["sort_key"] = self._next_rank(
                [t for t in doc["tasks"] if t is not task], status)
            self._stamp_status_change(task, old_status)
        if before_id or after_id:
            anchor = self._resolve(doc["tasks"], before_id or after_id)
            if anchor is None or anchor.get("status") != task["status"]:
                return "anchor task not found in the target column"
            col = [t for t in self._column(doc["tasks"], task["status"])
                   if t is not task]
            idx = col.index(anchor)
            if before_id:
                lo = col[idx - 1]["sort_key"] if idx > 0 else 0.0
                hi = anchor["sort_key"]
            else:
                lo = anchor["sort_key"]
                hi = col[idx + 1]["sort_key"] if idx + 1 < len(col) else lo + 2 * _RANK_STEP
            task["sort_key"] = (lo + hi) / 2.0
            if hi - lo < _MIN_GAP:
                self._rebalance(doc["tasks"], task["status"])
        return None

    def mutate_task(self, task_id, fields=None, move=None, expected_rev=None,
                    actor="") -> dict:
        """Apply field updates and/or a board move in ONE transaction.

        Replaces the update-then-move double write so a concurrent writer
        can no longer observe or clobber the half-updated state.
        """
        fields = dict(fields or {})
        move = dict(move or {})
        if not fields and not move:
            return {"error": "fields or move required"}
        if fields:
            err = self._validate_task_fields(fields)
            if err:
                return {"error": err}
        move_status = move.get("status")
        if move_status is not None and move_status not in STATUSES:
            return {"error": f"status must be one of {list(STATUSES)}"}
        with self._guard():
            doc = self._load()
            conflict = self._rev_conflict(doc, expected_rev)
            if conflict:
                return conflict
            task = self._resolve(doc["tasks"], task_id)
            if task is None:
                return {"error": f"no task matches: {task_id}"}
            before = {k: (list(v) if isinstance(v, list) else v)
                      for k, v in task.items()}
            if fields:
                err = self._apply_fields(doc, task, fields)
                if err:
                    return {"error": err}
            if move:
                err = self._apply_move(doc, task, status=move_status,
                                       before_id=move.get("before_id"),
                                       after_id=move.get("after_id"))
                if err:
                    return {"error": err}
            task["updated_at"] = _now()
            patch = {k: [before.get(k), task.get(k)] for k in task
                     if k != "updated_at" and before.get(k) != task.get(k)}
            self._event("task", "move" if (move and not fields) else "update",
                        task["id"], patch=patch, actor=actor)
            self._save(doc)
            return dict(task)

    def update_task(self, task_id, expected_rev=None, actor="", **fields) -> dict:
        return self.mutate_task(task_id, fields=fields, expected_rev=expected_rev,
                                actor=actor)

    def move_task(self, task_id, status=None, before_id=None, after_id=None,
                  expected_rev=None, actor="") -> dict:
        """Column move and/or rank move (midpoint between neighbors)."""
        return self.mutate_task(
            task_id,
            move={"status": status, "before_id": before_id, "after_id": after_id},
            expected_rev=expected_rev, actor=actor)

    def archive_task(self, task_id, actor="") -> dict:
        return self._set_lifecycle("tasks", task_id, "archived", actor=actor)

    def restore_task(self, task_id, actor="") -> dict:
        return self._set_lifecycle("tasks", task_id, "active", actor=actor)

    def _set_lifecycle(self, collection, ref, lifecycle, actor="") -> dict:
        with self._guard():
            doc = self._load()
            item = self._resolve(doc[collection], ref)
            if item is None:
                return {"error": f"no {collection[:-1]} matches: {ref}"}
            old = item.get("lifecycle", "active")
            item["lifecycle"] = lifecycle
            item["updated_at"] = _now()
            self._event(collection[:-1],
                        "archive" if lifecycle == "archived" else "restore",
                        item["id"], patch={"lifecycle": [old, lifecycle]},
                        actor=actor)
            self._save(doc)
            return dict(item)

    def purge_archived(self, entity="task") -> dict:
        key = {"task": "tasks", "milestone": "milestones", "note": "notes"}.get(entity)
        if not key:
            return {"error": "entity must be task|milestone|note"}
        with self._guard():
            doc = self._load()
            before = len(doc[key])
            doc[key] = [i for i in doc[key] if i.get("lifecycle") != "archived"]
            purged = before - len(doc[key])
            if purged:
                self._event(entity, "purge", data={"purged": purged})
                self._save(doc)
            return {"purged": purged}

    def get_task(self, task_id):
        doc = self._load()
        task = self._resolve(doc["tasks"], task_id)
        return dict(task) if task else None

    def list_tasks(self, status=None, milestone_id=None, tag=None, priority=None,
                   include_archived=False, query="", limit=500) -> list:
        doc = self._load()
        out = []
        q = (query or "").strip().lower()
        for t in doc["tasks"]:
            if not include_archived and t.get("lifecycle") != "active":
                continue
            if status and t.get("status") != status:
                continue
            if milestone_id and t.get("milestone_id") != milestone_id:
                continue
            if priority and t.get("priority") != priority:
                continue
            if tag and tag not in (t.get("tags") or []):
                continue
            if q and q not in (t.get("title", "") + " " + t.get("description", "")).lower():
                continue
            out.append(dict(t))
        out.sort(key=lambda t: (t.get("priority", "p2"), t.get("due_date") or "9999",
                                t.get("sort_key", 0.0)))
        return out[: max(1, int(limit))]

    # ── Links ──────────────────────────────────────────────────────

    @staticmethod
    def _clean_links(links):
        clean = []
        for link in (links or []):
            if not isinstance(link, dict):
                return None, "links must be objects {type, ref, label?}"
            ltype = link.get("type")
            ref = (link.get("ref") or "").strip()
            if ltype not in LINK_TYPES:
                return None, f"link type must be one of {list(LINK_TYPES)}"
            if not ref:
                return None, "link ref is required"
            clean.append({"type": ltype, "ref": ref, "label": link.get("label", "")})
        return clean, None

    def add_link(self, task_id, link_type, ref, label="") -> dict:
        clean, err = self._clean_links([{"type": link_type, "ref": ref, "label": label}])
        if err:
            return {"error": err}
        with self._guard():
            doc = self._load()
            task = self._resolve(doc["tasks"], task_id)
            if task is None:
                return {"error": f"no task matches: {task_id}"}
            if not any(l["type"] == link_type and l["ref"] == clean[0]["ref"]
                       for l in task.get("links", [])):
                task.setdefault("links", []).append(clean[0])
                task["updated_at"] = _now()
                self._event("task", "link", task["id"], data=dict(clean[0]))
                self._save(doc)
            return dict(task)

    def remove_link(self, task_id, link_type, ref) -> dict:
        with self._guard():
            doc = self._load()
            task = self._resolve(doc["tasks"], task_id)
            if task is None:
                return {"error": f"no task matches: {task_id}"}
            before = len(task.get("links", []))
            task["links"] = [l for l in task.get("links", [])
                             if not (l["type"] == link_type and l["ref"] == ref)]
            if len(task["links"]) != before:
                task["updated_at"] = _now()
                self._event("task", "unlink", task["id"],
                            data={"type": link_type, "ref": ref})
                self._save(doc)
            return dict(task)

    # ── Milestones ─────────────────────────────────────────────────

    def create_milestone(self, name, description="", target_date=None) -> dict:
        name = (name or "").strip()
        if not name:
            return {"error": "name is required"}
        if not _valid_date(target_date):
            return {"error": "target_date must be YYYY-MM-DD"}
        now = _now()
        with self._guard():
            doc = self._load()
            ranks = [m.get("sort_key", 0.0) for m in doc["milestones"]]
            ms = {
                "id": _new_id(), "name": name, "description": description or "",
                "target_date": target_date or None,
                "sort_key": (max(ranks) + _RANK_STEP) if ranks else _RANK_STEP,
                "lifecycle": "active", "created_at": now, "updated_at": now,
            }
            doc["milestones"].append(ms)
            self._event("milestone", "create", ms["id"], data=dict(ms))
            self._save(doc)
            return dict(ms)

    _MS_FIELDS = {"name", "description", "target_date"}

    def update_milestone(self, milestone_id, expected_rev=None, **fields) -> dict:
        unknown = set(fields) - self._MS_FIELDS
        if unknown:
            return {"error": f"unknown fields: {sorted(unknown)}"}
        if "target_date" in fields and not _valid_date(fields["target_date"]):
            return {"error": "target_date must be YYYY-MM-DD"}
        if "name" in fields and not (fields["name"] or "").strip():
            return {"error": "name cannot be empty"}
        with self._guard():
            doc = self._load()
            conflict = self._rev_conflict(doc, expected_rev)
            if conflict:
                return conflict
            ms = self._resolve(doc["milestones"], milestone_id)
            if ms is None:
                return {"error": f"no milestone matches: {milestone_id}"}
            before = {k: ms.get(k) for k in fields}
            ms.update(fields)
            ms["updated_at"] = _now()
            self._event("milestone", "update", ms["id"],
                        patch={k: [before[k], ms.get(k)] for k in fields
                               if before[k] != ms.get(k)})
            self._save(doc)
            return dict(ms)

    def archive_milestone(self, milestone_id) -> dict:
        """Archive a milestone and detach its tasks."""
        with self._guard():
            doc = self._load()
            ms = self._resolve(doc["milestones"], milestone_id)
            if ms is None:
                return {"error": f"no milestone matches: {milestone_id}"}
            ms["lifecycle"] = "archived"
            ms["updated_at"] = _now()
            detached = 0
            for t in doc["tasks"]:
                if t.get("milestone_id") == ms["id"]:
                    t["milestone_id"] = None
                    t["updated_at"] = _now()
                    detached += 1
            self._event("milestone", "archive", ms["id"],
                        patch={"lifecycle": ["active", "archived"]},
                        data={"detached_tasks": detached})
            self._save(doc)
            return {**ms, "detached_tasks": detached}

    def resolve_milestone(self, ref: str):
        """Resolve by id, unique id prefix, or unique case-insensitive name."""
        doc = self._load()
        ms = self._resolve(doc["milestones"], ref)
        if ms is not None:
            return dict(ms)
        want = (ref or "").strip().lower()
        matches = [m for m in doc["milestones"]
                   if m.get("lifecycle") == "active"
                   and (m.get("name") or "").lower() == want]
        return dict(matches[0]) if len(matches) == 1 else None

    def _progress(self, doc, milestone_id) -> dict:
        tasks = [t for t in doc["tasks"]
                 if t.get("milestone_id") == milestone_id
                 and t.get("lifecycle") == "active"]
        done = sum(1 for t in tasks if t.get("status") == "done")
        total = len(tasks)
        return {"total": total, "done": done,
                "pct": int(round(100 * done / total)) if total else 0}

    def milestone_progress(self, milestone_id) -> dict:
        doc = self._load()
        ms = self._resolve(doc["milestones"], milestone_id)
        if ms is None:
            return {"error": f"no milestone matches: {milestone_id}"}
        return self._progress(doc, ms["id"])

    def list_milestones(self, include_archived=False) -> list:
        doc = self._load()
        out = []
        for ms in sorted(doc["milestones"], key=lambda m: m.get("sort_key", 0.0)):
            if not include_archived and ms.get("lifecycle") != "active":
                continue
            out.append({**ms, "progress": self._progress(doc, ms["id"])})
        return out

    # ── Notes ──────────────────────────────────────────────────────

    def add_note(self, text, kind="note", tags=None, task_id=None, author="") -> dict:
        text = (text or "").strip()
        if not text:
            return {"error": "text is required"}
        if kind not in NOTE_KINDS:
            return {"error": f"kind must be one of {list(NOTE_KINDS)}"}
        now = _now()
        with self._guard():
            doc = self._load()
            if task_id:
                task = self._resolve(doc["tasks"], task_id)
                if task is None:
                    return {"error": f"no task matches: {task_id}"}
                task_id = task["id"]
            note = {
                "id": _new_id(), "text": text, "kind": kind,
                "tags": [t for t in (tags or []) if t], "task_id": task_id or None,
                "lifecycle": "active", "created_at": now, "updated_at": now,
                "author": author or "",
            }
            doc["notes"].append(note)
            self._event("note", "create", note["id"], data=dict(note),
                        actor=author)
            self._save(doc)
            return dict(note)

    _NOTE_FIELDS = {"text", "kind", "tags", "task_id"}

    def update_note(self, note_id, expected_rev=None, **fields) -> dict:
        unknown = set(fields) - self._NOTE_FIELDS
        if unknown:
            return {"error": f"unknown fields: {sorted(unknown)}"}
        if "kind" in fields and fields["kind"] not in NOTE_KINDS:
            return {"error": f"kind must be one of {list(NOTE_KINDS)}"}
        with self._guard():
            doc = self._load()
            conflict = self._rev_conflict(doc, expected_rev)
            if conflict:
                return conflict
            note = self._resolve(doc["notes"], note_id)
            if note is None:
                return {"error": f"no note matches: {note_id}"}
            before = {k: note.get(k) for k in fields}
            note.update(fields)
            note["updated_at"] = _now()
            self._event("note", "update", note["id"],
                        patch={k: [before[k], note.get(k)] for k in fields
                               if before[k] != note.get(k)})
            self._save(doc)
            return dict(note)

    def archive_note(self, note_id, actor="") -> dict:
        return self._set_lifecycle("notes", note_id, "archived", actor=actor)

    def list_notes(self, kind=None, include_archived=False, limit=100) -> list:
        doc = self._load()
        out = []
        for n in doc["notes"]:
            if not include_archived and n.get("lifecycle") != "active":
                continue
            if kind and n.get("kind") != kind:
                continue
            out.append(dict(n))
        out.sort(key=lambda n: n.get("created_at", ""), reverse=True)
        return out[: max(1, int(limit))]

    # ── Aggregates ─────────────────────────────────────────────────

    def stats(self) -> dict:
        return self._stats(self._load())

    def _stats(self, doc: dict) -> dict:
        active = [t for t in doc["tasks"] if t.get("lifecycle") == "active"]
        by_status = {s: 0 for s in STATUSES}
        for t in active:
            if t.get("status") in by_status:
                by_status[t["status"]] += 1
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        overdue = sum(1 for t in active
                      if t.get("status") != "done"
                      and t.get("due_date") and t["due_date"] < today)
        return {
            "open": sum(v for s, v in by_status.items() if s != "done"),
            "by_status": by_status,
            "overdue": overdue,
            "done_total": by_status["done"],
            "milestones_active": sum(1 for m in doc["milestones"]
                                     if m.get("lifecycle") == "active"),
            "notes": sum(1 for n in doc["notes"] if n.get("lifecycle") == "active"),
        }

    def board(self, milestone_id=None, tag=None, include_archived=False) -> dict:
        doc = self._load()
        columns = {}
        for status in STATUSES:
            col = []
            for t in self._column(doc["tasks"], status):
                if milestone_id and t.get("milestone_id") != milestone_id:
                    continue
                if tag and tag not in (t.get("tags") or []):
                    continue
                col.append(dict(t))
            if include_archived:
                for t in doc["tasks"]:
                    if (t.get("status") != status
                            or t.get("lifecycle") != "archived"):
                        continue
                    if milestone_id and t.get("milestone_id") != milestone_id:
                        continue
                    if tag and tag not in (t.get("tags") or []):
                        continue
                    col.append(dict(t))
            columns[status] = col
        out = {
            "columns": columns,
            "milestones": [{**ms, "progress": self._progress(doc, ms["id"])}
                           for ms in sorted(doc["milestones"],
                                            key=lambda m: m.get("sort_key", 0.0))
                           if ms.get("lifecycle") == "active"],
            "stats": self._stats(doc),
            "rev": int(doc.get("rev", 0)),
        }
        if self.last_recovery:
            out["recovery"] = dict(self.last_recovery)
        return out
