"""Per-project project-management store: tasks, milestones, decision notes.

One document — ``.c3/pm/pm.json`` — holding all three collections, so
cross-entity operations (milestone archive detaching tasks, etc.) are a
single atomic write under one lock.

Concurrency model: the hub Flask server, the MCP stdio server, and the
per-project web server are SEPARATE PROCESSES that may all mutate the same
file. TaskStore therefore keeps **no in-memory cache** — every operation is
``load -> mutate -> atomic save`` under a ``threading.Lock`` (in-process
serialization) with temp + fsync + ``os.replace`` (crash safety). Cross-
process conflicts collapse to last-writer-wins *per operation*, which is
acceptable for a single-user tool.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
STATUSES = ("backlog", "in_progress", "blocked", "done")
PRIORITIES = ("p0", "p1", "p2", "p3")
LINK_TYPES = ("file", "commit", "edit")
NOTE_KINDS = ("note", "decision")
_RANK_STEP = 1024.0
_MIN_GAP = 1e-6


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
        self._lock = threading.Lock()

    # ── Persistence ────────────────────────────────────────────────

    def _empty_doc(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "tasks": [], "milestones": [], "notes": []}

    def _load(self) -> dict:
        if not self.pm_file.exists():
            return self._empty_doc()
        try:
            doc = json.loads(self.pm_file.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("tasks"), list):
                raise ValueError("malformed pm.json")
            doc.setdefault("schema_version", SCHEMA_VERSION)
            doc.setdefault("milestones", [])
            doc.setdefault("notes", [])
            return doc
        except Exception:
            # Corrupt file: preserve it for inspection, start empty.
            for n in range(1, 100):
                target = self.pm_file.with_name(f"pm.json.corrupt-{n}")
                if not target.exists():
                    try:
                        os.replace(self.pm_file, target)
                    except OSError:
                        pass
                    break
            return self._empty_doc()

    def _save(self, doc: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.pm_file.with_name("pm.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.pm_file)

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
        with self._lock:
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
            self._save(doc)
            return dict(task)

    _TASK_FIELDS = {"title", "description", "status", "priority", "due_date",
                    "tags", "milestone_id"}

    def update_task(self, task_id, **fields) -> dict:
        unknown = set(fields) - self._TASK_FIELDS
        if unknown:
            return {"error": f"unknown fields: {sorted(unknown)}"}
        if "status" in fields and fields["status"] not in STATUSES:
            return {"error": f"status must be one of {list(STATUSES)}"}
        if "priority" in fields and fields["priority"] not in PRIORITIES:
            return {"error": f"priority must be one of {list(PRIORITIES)}"}
        if "due_date" in fields and not _valid_date(fields["due_date"]):
            return {"error": "due_date must be YYYY-MM-DD"}
        if "title" in fields and not (fields["title"] or "").strip():
            return {"error": "title cannot be empty"}
        with self._lock:
            doc = self._load()
            task = self._resolve(doc["tasks"], task_id)
            if task is None:
                return {"error": f"no task matches: {task_id}"}
            if "milestone_id" in fields and fields["milestone_id"]:
                ms = self._resolve(doc["milestones"], fields["milestone_id"])
                if ms is None:
                    return {"error": f"no milestone matches: {fields['milestone_id']}"}
                fields["milestone_id"] = ms["id"]
            old_status = task["status"]
            for key, val in fields.items():
                task[key] = val
            if "status" in fields and fields["status"] != old_status:
                task["sort_key"] = self._next_rank(
                    [t for t in doc["tasks"] if t is not task], fields["status"])
                if fields["status"] == "done":
                    task["completed_at"] = _now()
                elif old_status == "done":
                    task["completed_at"] = None
            task["updated_at"] = _now()
            self._save(doc)
            return dict(task)

    def move_task(self, task_id, status=None, before_id=None, after_id=None) -> dict:
        """Column move and/or rank move (midpoint between neighbors)."""
        if status is not None and status not in STATUSES:
            return {"error": f"status must be one of {list(STATUSES)}"}
        with self._lock:
            doc = self._load()
            task = self._resolve(doc["tasks"], task_id)
            if task is None:
                return {"error": f"no task matches: {task_id}"}
            old_status = task["status"]
            if status and status != old_status:
                task["status"] = status
                task["sort_key"] = self._next_rank(
                    [t for t in doc["tasks"] if t is not task], status)
                if status == "done":
                    task["completed_at"] = _now()
                elif old_status == "done":
                    task["completed_at"] = None
            if before_id or after_id:
                anchor = self._resolve(doc["tasks"], before_id or after_id)
                if anchor is None or anchor.get("status") != task["status"]:
                    return {"error": "anchor task not found in the target column"}
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
            task["updated_at"] = _now()
            self._save(doc)
            return dict(task)

    def archive_task(self, task_id) -> dict:
        return self._set_lifecycle("tasks", task_id, "archived")

    def restore_task(self, task_id) -> dict:
        return self._set_lifecycle("tasks", task_id, "active")

    def _set_lifecycle(self, collection, ref, lifecycle) -> dict:
        with self._lock:
            doc = self._load()
            item = self._resolve(doc[collection], ref)
            if item is None:
                return {"error": f"no {collection[:-1]} matches: {ref}"}
            item["lifecycle"] = lifecycle
            item["updated_at"] = _now()
            self._save(doc)
            return dict(item)

    def purge_archived(self, entity="task") -> dict:
        key = {"task": "tasks", "milestone": "milestones", "note": "notes"}.get(entity)
        if not key:
            return {"error": "entity must be task|milestone|note"}
        with self._lock:
            doc = self._load()
            before = len(doc[key])
            doc[key] = [i for i in doc[key] if i.get("lifecycle") != "archived"]
            purged = before - len(doc[key])
            if purged:
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
        with self._lock:
            doc = self._load()
            task = self._resolve(doc["tasks"], task_id)
            if task is None:
                return {"error": f"no task matches: {task_id}"}
            if not any(l["type"] == link_type and l["ref"] == clean[0]["ref"]
                       for l in task.get("links", [])):
                task.setdefault("links", []).append(clean[0])
                task["updated_at"] = _now()
                self._save(doc)
            return dict(task)

    def remove_link(self, task_id, link_type, ref) -> dict:
        with self._lock:
            doc = self._load()
            task = self._resolve(doc["tasks"], task_id)
            if task is None:
                return {"error": f"no task matches: {task_id}"}
            before = len(task.get("links", []))
            task["links"] = [l for l in task.get("links", [])
                             if not (l["type"] == link_type and l["ref"] == ref)]
            if len(task["links"]) != before:
                task["updated_at"] = _now()
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
        with self._lock:
            doc = self._load()
            ranks = [m.get("sort_key", 0.0) for m in doc["milestones"]]
            ms = {
                "id": _new_id(), "name": name, "description": description or "",
                "target_date": target_date or None,
                "sort_key": (max(ranks) + _RANK_STEP) if ranks else _RANK_STEP,
                "lifecycle": "active", "created_at": now, "updated_at": now,
            }
            doc["milestones"].append(ms)
            self._save(doc)
            return dict(ms)

    _MS_FIELDS = {"name", "description", "target_date"}

    def update_milestone(self, milestone_id, **fields) -> dict:
        unknown = set(fields) - self._MS_FIELDS
        if unknown:
            return {"error": f"unknown fields: {sorted(unknown)}"}
        if "target_date" in fields and not _valid_date(fields["target_date"]):
            return {"error": "target_date must be YYYY-MM-DD"}
        if "name" in fields and not (fields["name"] or "").strip():
            return {"error": "name cannot be empty"}
        with self._lock:
            doc = self._load()
            ms = self._resolve(doc["milestones"], milestone_id)
            if ms is None:
                return {"error": f"no milestone matches: {milestone_id}"}
            ms.update(fields)
            ms["updated_at"] = _now()
            self._save(doc)
            return dict(ms)

    def archive_milestone(self, milestone_id) -> dict:
        """Archive a milestone and detach its tasks."""
        with self._lock:
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
        with self._lock:
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
            self._save(doc)
            return dict(note)

    _NOTE_FIELDS = {"text", "kind", "tags", "task_id"}

    def update_note(self, note_id, **fields) -> dict:
        unknown = set(fields) - self._NOTE_FIELDS
        if unknown:
            return {"error": f"unknown fields: {sorted(unknown)}"}
        if "kind" in fields and fields["kind"] not in NOTE_KINDS:
            return {"error": f"kind must be one of {list(NOTE_KINDS)}"}
        with self._lock:
            doc = self._load()
            note = self._resolve(doc["notes"], note_id)
            if note is None:
                return {"error": f"no note matches: {note_id}"}
            note.update(fields)
            note["updated_at"] = _now()
            self._save(doc)
            return dict(note)

    def archive_note(self, note_id) -> dict:
        return self._set_lifecycle("notes", note_id, "archived")

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
        doc = self._load()
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
                col.extend(dict(t) for t in doc["tasks"]
                           if t.get("status") == status
                           and t.get("lifecycle") == "archived")
            columns[status] = col
        return {
            "columns": columns,
            "milestones": [{**ms, "progress": self._progress(doc, ms["id"])}
                           for ms in sorted(doc["milestones"],
                                            key=lambda m: m.get("sort_key", 0.0))
                           if ms.get("lifecycle") == "active"],
            "stats": self.stats(),
        }
