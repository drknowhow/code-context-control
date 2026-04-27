"""Discovers C3 projects via hub API or direct file read."""

import json
import urllib.error
import urllib.request
from pathlib import Path

_GLOBAL_C3_DIR = Path.home() / ".c3"
_PROJECTS_FILE = _GLOBAL_C3_DIR / "projects.json"


class ProjectScanner:
    """Discovers registered C3 projects."""

    def __init__(self, hub_url: str = "http://localhost:3330"):
        self.hub_url = hub_url.rstrip("/")

    def discover(self) -> list[dict]:
        """Return list of project dicts with memory metadata.

        Tries hub API first, falls back to reading ~/.c3/projects.json directly.
        """
        projects = self._from_hub() or self._from_file()
        return [self._enrich(p) for p in projects]

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
        }
