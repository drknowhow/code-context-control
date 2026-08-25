"""Discovers C3 projects via hub API or direct file read."""

import copy
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_GLOBAL_C3_DIR = Path.home() / ".c3"
_PROJECTS_FILE = _GLOBAL_C3_DIR / "projects.json"

DEFAULT_TTL_SECONDS = 20.0


class ProjectScanner:
    """Discovers registered C3 projects.

    Results are cached for a short TTL: ``discover()`` sits on the hot path of
    every Oracle tool call (``validate_project_path``) and several endpoints
    call it repeatedly per request. Callers mutate the returned dicts
    (``api_projects`` attaches health fields), so cache hits return copies.
    """

    def __init__(self, hub_url: str = "http://localhost:3330",
                 ttl: float = DEFAULT_TTL_SECONDS):
        self.hub_url = hub_url.rstrip("/")
        self.ttl = float(ttl)
        self._lock = threading.Lock()
        self._cached: tuple[float, list[dict]] | None = None

    def discover(self, force: bool = False) -> list[dict]:
        """Return list of project dicts with memory metadata.

        Tries hub API first, falls back to reading ~/.c3/projects.json
        directly. ``force=True`` bypasses the TTL cache (the dashboard's
        explicit Scan action). A failed/empty discovery is never cached, so a
        transient hub outage doesn't pin an empty project list for a full TTL.
        """
        with self._lock:
            if not force and self._cached is not None:
                ts, cached = self._cached
                if time.time() - ts < self.ttl:
                    return copy.deepcopy(cached)
            projects = self._from_hub() or self._from_file()
            enriched = [self._enrich(p) for p in projects]
            self._cached = (time.time(), enriched) if enriched else None
            return copy.deepcopy(enriched)

    def _from_hub(self) -> list[dict] | None:
        """Try fetching projects from the hub REST API."""
        try:
            req = urllib.request.Request(f"{self.hub_url}/api/projects")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
            raw = data if isinstance(data, list) else data.get("projects", [])
            return [{
                "path": p.get("path", ""), "name": p.get("name", ""),
                "tags": p.get("tags", []), "notes": p.get("notes", ""),
                "active": p.get("active", False), "ide": p.get("ide", ""),
                "parent_path": p.get("parent_path", ""),
            } for p in raw if p.get("path")]
        except Exception:
            return None

    def _from_file(self) -> list[dict]:
        """Fallback: read ~/.c3/projects.json directly."""
        try:
            if _PROJECTS_FILE.exists():
                with open(_PROJECTS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                return [{
                    "path": p.get("path", ""), "name": p.get("name", ""),
                    "tags": p.get("tags", []), "notes": p.get("notes", ""),
                    "parent_path": p.get("parent_path", ""),
                } for p in data.get("projects", []) if p.get("path")]
        except Exception:
            pass
        return []

    def _enrich(self, project: dict) -> dict:
        """Add C3 metadata to a project entry."""
        path = Path(project["path"])
        c3_dir = path / ".c3"
        facts_file = c3_dir / "facts" / "facts.json"

        has_c3 = c3_dir.is_dir()
        has_facts = facts_file.is_file()
        fact_count = 0
        last_modified = None

        if has_facts:
            try:
                stat = facts_file.stat()
                last_modified = stat.st_mtime
                with open(facts_file, encoding="utf-8") as f:
                    facts = json.load(f)
                fact_count = len(facts) if isinstance(facts, list) else 0
            except Exception:
                pass

        # Sub-project hierarchy (v2.44 parent/child model, multi-level since
        # 2.96). Registry parent_path is authoritative; the child config
        # back-link covers a stale registry. Best-effort: broken links degrade
        # to top-level.
        parent_path = str(project.get("parent_path") or "")
        sub_rel_paths: list[str] = []
        sub_paths: list[str] = []
        if has_c3:
            try:
                from services.subprojects import (
                    _read_config,
                    entry_abs_path,
                    get_subprojects,
                )
                if not parent_path:
                    back = _read_config(project["path"]).get("parent") or {}
                    parent_path = str(back.get("path") or "")
                entries = get_subprojects(project["path"])
                # rel_paths are the NESTED children only — they are what the
                # parent's index excludes. The count must include external
                # children too, or a parent whose children all live elsewhere
                # reports zero.
                sub_rel_paths = [s.get("rel_path", "") for s in entries if s.get("rel_path")]
                sub_paths = [str(entry_abs_path(project["path"], s)) for s in entries]
            except Exception:
                pass
        if parent_path:
            try:
                parent_path = str(Path(parent_path).resolve())
            except Exception:
                pass

        return {
            "path": project["path"],
            "name": project.get("name") or path.name,
            "tags": project.get("tags", []),
            "notes": project.get("notes", ""),
            "active": project.get("active", False),
            "ide": project.get("ide", ""),
            "has_c3": has_c3,
            "has_facts": has_facts,
            "fact_count": fact_count,
            "facts_mtime": last_modified,
            "parent_path": parent_path,
            "is_subproject": bool(parent_path),
            "subproject_rel_paths": sub_rel_paths,
            "subproject_paths": sub_paths,
            "subproject_count": len(sub_paths),
        }
