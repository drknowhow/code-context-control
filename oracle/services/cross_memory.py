"""Cross-project insight store for Oracle."""

import json
import uuid
from datetime import datetime, timezone

from oracle.config import ORACLE_DIR

_CROSS_MEMORY_FILE = ORACLE_DIR / "cross_memory.json"

INSIGHT_TYPES = {
    "pattern", "dependency", "convention", "risk", "opportunity", "drift",
    "shared_convention", "duplicated_fact", "divergent_decision", "shared_bug_pattern",
}


def _load() -> dict:
    try:
        if _CROSS_MEMORY_FILE.is_file():
            with open(_CROSS_MEMORY_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"version": 1, "insights": [], "project_links": []}


def _save(data: dict):
    ORACLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CROSS_MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


class CrossMemory:
    """Manages cross-project insights and project links."""

    def __init__(self):
        self.data = _load()

    def reload(self):
        self.data = _load()

    def get_all_insights(self) -> list[dict]:
        return [i for i in self.data.get("insights", []) if not i.get("dismissed")]

    def get_for_project(self, project_path: str) -> list[dict]:
        return [
            i for i in self.get_all_insights()
            if project_path in i.get("source_projects", [])
        ]

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        """Simple keyword search over insight text."""
        query_lower = query.lower()
        terms = query_lower.split()
        scored = []
        for insight in self.get_all_insights():
            text_lower = insight.get("text", "").lower()
            score = sum(1 for t in terms if t in text_lower)
            if score > 0:
                scored.append((score, insight))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored[:top_k]]

    def add_insight(
        self,
        text: str,
        insight_type: str,
        source_projects: list[str],
        source_fact_ids: dict[str, list[str]] | None = None,
        confidence: float = 0.7,
        tags: list[str] | None = None,
    ) -> dict:
        """Add a new cross-project insight, deduplicating by text similarity."""
        # Simple dedup: skip if very similar insight already exists
        for existing in self.get_all_insights():
            if self._jaccard(text, existing.get("text", "")) > 0.6:
                existing["last_reviewed"] = datetime.now(timezone.utc).isoformat()
                existing["confidence"] = max(existing.get("confidence", 0), confidence)
                _save(self.data)
                return existing

        now = datetime.now(timezone.utc).isoformat()
        insight = {
            "id": f"ins_{uuid.uuid4().hex[:12]}",
            "type": insight_type if insight_type in INSIGHT_TYPES else "pattern",
            "text": text,
            "source_projects": source_projects,
            "source_fact_ids": source_fact_ids or {},
            "confidence": confidence,
            "created_at": now,
            "last_reviewed": now,
            "dismissed": False,
            "tags": tags or [],
        }
        self.data["insights"].append(insight)

        # Update project links
        self._update_links(insight)
        _save(self.data)
        return insight

    def dismiss(self, insight_id: str) -> dict:
        for insight in self.data.get("insights", []):
            if insight["id"] == insight_id:
                insight["dismissed"] = True
                _save(self.data)
                return {"dismissed": True, "id": insight_id}
        return {"error": "Insight not found"}

    def get_project_links(self) -> list[dict]:
        return self.data.get("project_links", [])

    def stats(self) -> dict:
        insights = self.get_all_insights()
        by_type: dict[str, int] = {}
        for i in insights:
            t = i.get("type", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total_insights": len(insights),
            "by_type": by_type,
            "total_links": len(self.data.get("project_links", [])),
        }

    def _update_links(self, insight: dict):
        """Create or strengthen project links from an insight."""
        projects = insight.get("source_projects", [])
        links = self.data.setdefault("project_links", [])
        for i, src in enumerate(projects):
            for dst in projects[i + 1:]:
                existing = next(
                    (l for l in links if {l["src"], l["dst"]} == {src, dst}),
                    None,
                )
                if existing:
                    existing["strength"] = existing.get("strength", 0) + 1
                    if insight["id"] not in existing.get("insight_ids", []):
                        existing.setdefault("insight_ids", []).append(insight["id"])
                else:
                    links.append({
                        "src": src,
                        "dst": dst,
                        "link_type": insight.get("type", "pattern"),
                        "strength": 1,
                        "insight_ids": [insight["id"]],
                    })

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        return len(words_a & words_b) / len(words_a | words_b)
