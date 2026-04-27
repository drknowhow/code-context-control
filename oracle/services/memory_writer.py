"""Handles approved write-backs to project .c3/facts/."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from oracle.config import ORACLE_DIR

_SUGGESTIONS_FILE = ORACLE_DIR / "suggestions.json"


def _load_suggestions() -> list[dict]:
    try:
        if _SUGGESTIONS_FILE.is_file():
            with open(_SUGGESTIONS_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_suggestions(suggestions: list[dict]):
    ORACLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_SUGGESTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(suggestions, f, indent=2)


class MemoryWriter:
    """Manages suggestion queue and approved write-backs to project memory."""

    def suggest(self, project_path: str, suggestion_type: str, data: dict) -> dict:
        """Create a pending suggestion for a project.

        suggestion_type: 'merge_facts', 'archive_facts', 'add_fact'
        data: type-specific payload
        """
        suggestions = _load_suggestions()
        suggestion = {
            "id": f"sug_{uuid.uuid4().hex[:12]}",
            "project_path": project_path,
            "type": suggestion_type,
            "data": data,
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "resolved_at": None,
        }
        suggestions.append(suggestion)
        _save_suggestions(suggestions)
        return suggestion

    def list_pending(self, project_path: str | None = None) -> list[dict]:
        """Return pending suggestions, optionally filtered by project."""
        suggestions = _load_suggestions()
        pending = [s for s in suggestions if s.get("status") == "pending"]
        if project_path:
            pending = [s for s in pending if s.get("project_path") == project_path]
        return pending

    def approve_suggestion(self, suggestion_id: str) -> dict:
        """Execute an approved suggestion and write to project memory."""
        suggestions = _load_suggestions()
        target = None
        for s in suggestions:
            if s["id"] == suggestion_id and s["status"] == "pending":
                target = s
                break
        if not target:
            return {"error": "Suggestion not found or already resolved"}

        result = self._execute(target)
        target["status"] = "approved"
        target["resolved_at"] = datetime.now(timezone.utc).isoformat()
        target["result"] = result
        _save_suggestions(suggestions)
        return {"approved": True, "id": suggestion_id, "result": result}

    def dismiss_suggestion(self, suggestion_id: str) -> dict:
        """Mark a suggestion as dismissed."""
        suggestions = _load_suggestions()
        for s in suggestions:
            if s["id"] == suggestion_id and s["status"] == "pending":
                s["status"] = "dismissed"
                s["resolved_at"] = datetime.now(timezone.utc).isoformat()
                _save_suggestions(suggestions)
                return {"dismissed": True, "id": suggestion_id}
        return {"error": "Suggestion not found or already resolved"}

    def _execute(self, suggestion: dict) -> dict:
        """Execute a suggestion write-back."""
        stype = suggestion["type"]
        project_path = suggestion["project_path"]
        data = suggestion["data"]

        if stype == "archive_facts":
            return self._archive_facts(project_path, data.get("fact_ids", []))
        elif stype == "merge_facts":
            return self._merge_facts(project_path, data)
        elif stype == "add_fact":
            return self._add_fact(project_path, data)
        return {"error": f"Unknown suggestion type: {stype}"}

    def _load_project_facts(self, project_path: str) -> tuple[list[dict], Path]:
        facts_file = Path(project_path) / ".c3" / "facts" / "facts.json"
        if not facts_file.is_file():
            return [], facts_file
        try:
            with open(facts_file, encoding="utf-8") as f:
                return json.load(f), facts_file
        except Exception:
            return [], facts_file

    def _save_project_facts(self, facts: list[dict], facts_file: Path):
        facts_file.parent.mkdir(parents=True, exist_ok=True)
        with open(facts_file, "w", encoding="utf-8") as f:
            json.dump(facts, f, indent=2)

    def _archive_facts(self, project_path: str, fact_ids: list[str]) -> dict:
        """Set lifecycle=archived on specified facts."""
        facts, fpath = self._load_project_facts(project_path)
        archived = 0
        for fact in facts:
            if fact.get("id") in fact_ids:
                fact["lifecycle"] = "archived"
                archived += 1
        self._save_project_facts(facts, fpath)
        return {"archived": archived}

    def _merge_facts(self, project_path: str, data: dict) -> dict:
        """Merge duplicate facts: keep survivor, archive others."""
        facts, fpath = self._load_project_facts(project_path)
        survivor_id = data.get("survivor_id")
        merge_ids = set(data.get("merge_ids", []))

        survivor = None
        for fact in facts:
            if fact.get("id") == survivor_id:
                survivor = fact
                break
        if not survivor:
            return {"error": f"Survivor fact {survivor_id} not found"}

        merged_count = 0
        for fact in facts:
            if fact.get("id") in merge_ids and fact.get("id") != survivor_id:
                fact["lifecycle"] = "archived"
                survivor["relevance_count"] = (
                    int(survivor.get("relevance_count", 0))
                    + int(fact.get("relevance_count", 0))
                )
                merged_count += 1

        if data.get("merged_text"):
            survivor["fact"] = data["merged_text"]

        self._save_project_facts(facts, fpath)
        return {"merged": merged_count, "survivor_id": survivor_id}

    def _add_fact(self, project_path: str, data: dict) -> dict:
        """Add a new fact (e.g., cross-project insight) to a project."""
        facts, fpath = self._load_project_facts(project_path)
        fact_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "id": fact_id,
            "fact": data.get("fact", ""),
            "category": data.get("category", "oracle"),
            "source_session": "oracle",
            "timestamp": now,
            "last_accessed_at": None,
            "relevance_count": 0,
            "confidence": float(data.get("confidence", 0.8)),
            "source_quality": "oracle",
            "lifecycle": "active",
            "vector_id": fact_id,
            "recall_sessions": [],
            "confirmation_count": 0,
            "contradiction_count": 0,
        }
        facts.append(entry)
        self._save_project_facts(facts, fpath)
        return {"added": True, "id": fact_id}
