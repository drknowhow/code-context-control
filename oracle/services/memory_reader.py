"""Read-only access to per-project C3 memory stores."""

import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from services.memory_scorer import MemoryScorer  # noqa: E402


class MemoryReader:
    """Reads project .c3/facts/ files without writing."""

    def __init__(self):
        self._scorer = MemoryScorer()

    def read_facts(self, project_path: str) -> list[dict]:
        """Load all facts from a project's facts.json."""
        facts_file = Path(project_path) / ".c3" / "facts" / "facts.json"
        if not facts_file.is_file():
            return []
        try:
            with open(facts_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def read_graph(self, project_path: str) -> dict:
        """Load memory graph edges."""
        graph_file = Path(project_path) / ".c3" / "facts" / "memory_graph.json"
        if not graph_file.is_file():
            return {"edges": [], "adjacency": {}}
        try:
            with open(graph_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"edges": [], "adjacency": {}}

    def read_fingerprints(self, project_path: str) -> list[dict]:
        """Load session fingerprints."""
        fp_file = Path(project_path) / ".c3" / "facts" / "session_fingerprints.json"
        if not fp_file.is_file():
            return []
        try:
            with open(fp_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def get_fact_stats(self, project_path: str) -> dict:
        """Compute summary statistics for a project's facts."""
        facts = self.read_facts(project_path)
        if not facts:
            return {"total": 0, "by_category": {}, "by_tier": {}, "by_lifecycle": {}}

        by_category: dict[str, int] = {}
        by_lifecycle: dict[str, int] = {}
        for f in facts:
            cat = f.get("category", "general")
            by_category[cat] = by_category.get(cat, 0) + 1
            lc = f.get("lifecycle", "active")
            by_lifecycle[lc] = by_lifecycle.get(lc, 0) + 1

        tiers = self._scorer.tier_partition(facts)
        by_tier = {tier: len(tier_facts) for tier, tier_facts in tiers.items()}

        return {
            "total": len(facts),
            "by_category": by_category,
            "by_tier": by_tier,
            "by_lifecycle": by_lifecycle,
        }

    def get_graph_stats(self, project_path: str) -> dict:
        """Compute graph statistics."""
        graph = self.read_graph(project_path)
        edges = graph.get("edges", [])
        adjacency = graph.get("adjacency", {})

        nodes = set()
        edge_types: dict[str, int] = {}
        for edge in edges:
            nodes.add(edge.get("src", ""))
            nodes.add(edge.get("dst", ""))
            et = edge.get("type", "unknown")
            edge_types[et] = edge_types.get(et, 0) + 1

        # Count orphaned edges (referencing non-existent facts)
        facts = self.read_facts(project_path)
        fact_ids = {f.get("id") for f in facts if f.get("id")}
        orphaned = sum(
            1 for e in edges
            if e.get("src") not in fact_ids or e.get("dst") not in fact_ids
        )

        return {
            "total_edges": len(edges),
            "total_nodes": len(nodes),
            "edge_types": edge_types,
            "orphaned_edges": orphaned,
        }
