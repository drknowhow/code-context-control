"""Memory Graph — directed graph connecting facts, files, and symbols.

Facts are nodes; relationships are weighted directed edges with types.
The graph enables spreading activation (recall neighbours of recalled facts),
cluster detection, and gap analysis.

Edge types:
  co_recalled   — two facts recalled in the same query/session
  caused_by     — causal chain (user-stated or inferred)
  leads_to      — consequence/dependency
  touches       — fact references a file or symbol
  contradicts   — newer fact overrides or conflicts with older one
  refines       — fact was updated; old version linked
  clusters_with — computed via community detection on co-recall edges

Storage: .c3/memory_graph.json (adjacency list)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

EDGE_TYPES = {
    "co_recalled", "caused_by", "leads_to", "touches",
    "contradicts", "refines", "clusters_with",
}

# Limits
MAX_EDGES_PER_NODE = 50
MAX_TOTAL_EDGES = 5000


class MemoryGraph:
    """Persistent directed graph over memory facts."""

    def __init__(self, project_path: str, data_dir: str = ".c3/facts"):
        self.project_path = Path(project_path)
        self.data_dir = self.project_path / data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.graph_file = self.data_dir / "memory_graph.json"
        self._edges: list[dict] = []
        self._adjacency: dict[str, list[dict]] = defaultdict(list)
        self._load()

    # ── Edge management ─────────────────────────────────────────────

    def add_edge(
        self,
        src: str,
        dst: str,
        edge_type: str,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> dict:
        """Add or strengthen an edge between two nodes."""
        if edge_type not in EDGE_TYPES:
            return {"error": f"unknown edge type: {edge_type}"}

        existing = self._find_edge(src, dst, edge_type)
        now = datetime.now(timezone.utc).isoformat()

        if existing:
            existing["weight"] = round(existing.get("weight", 1.0) + weight, 4)
            existing["last_seen"] = now
            existing["hit_count"] = existing.get("hit_count", 1) + 1
            if metadata:
                existing.setdefault("metadata", {}).update(metadata)
            self._save()
            return {"strengthened": True, "edge": existing}

        edge = {
            "src": src,
            "dst": dst,
            "type": edge_type,
            "weight": round(weight, 4),
            "created_at": now,
            "last_seen": now,
            "hit_count": 1,
            "metadata": metadata or {},
        }

        # Enforce limits
        if len(self._edges) >= MAX_TOTAL_EDGES:
            self._prune_weakest(count=MAX_TOTAL_EDGES // 10)

        src_edges = self._adjacency[src]
        if len(src_edges) >= MAX_EDGES_PER_NODE:
            self._prune_node_edges(src, keep=MAX_EDGES_PER_NODE - 5)

        self._edges.append(edge)
        self._adjacency[src].append(edge)
        self._adjacency[dst].append(edge)
        self._save()
        return {"added": True, "edge": edge}

    def remove_edge(self, src: str, dst: str, edge_type: str) -> dict:
        """Remove a specific edge."""
        edge = self._find_edge(src, dst, edge_type)
        if not edge:
            return {"error": "not found"}
        self._edges.remove(edge)
        self._rebuild_adjacency()
        self._save()
        return {"removed": True}

    def remove_node(self, node_id: str) -> dict:
        """Remove all edges involving a node (when a fact is deleted)."""
        before = len(self._edges)
        self._edges = [
            e for e in self._edges
            if e["src"] != node_id and e["dst"] != node_id
        ]
        self._rebuild_adjacency()
        self._save()
        return {"removed_edges": before - len(self._edges)}

    # ── Query ───────────────────────────────────────────────────────

    def get_edges(self, node_id: str, edge_type: str | None = None) -> list[dict]:
        """Get all edges for a node, optionally filtered by type."""
        edges = self._adjacency.get(node_id, [])
        if edge_type:
            edges = [e for e in edges if e["type"] == edge_type]
        return edges

    def get_neighbors(self, node_id: str, edge_type: str | None = None) -> list[str]:
        """Get neighbor node IDs."""
        neighbors = set()
        for e in self.get_edges(node_id, edge_type):
            if e["src"] == node_id:
                neighbors.add(e["dst"])
            else:
                neighbors.add(e["src"])
        return list(neighbors)

    def spreading_activation(
        self,
        seed_ids: list[str],
        max_depth: int = 2,
        min_weight: float = 0.5,
        max_results: int = 20,
    ) -> list[dict]:
        """Activate from seed nodes and spread through the graph.

        Returns nodes ranked by accumulated activation energy.
        Activation decays by 0.5 at each hop and is weighted by edge weight.
        """
        activation: dict[str, float] = {}
        visited: set[str] = set()
        frontier = [(nid, 1.0) for nid in seed_ids]

        for depth in range(max_depth + 1):
            next_frontier: list[tuple[str, float]] = []
            for node_id, energy in frontier:
                if node_id in visited:
                    activation[node_id] = max(
                        activation.get(node_id, 0.0), energy
                    )
                    continue
                visited.add(node_id)
                activation[node_id] = max(
                    activation.get(node_id, 0.0), energy
                )

                if depth < max_depth:
                    for edge in self._adjacency.get(node_id, []):
                        neighbor = (
                            edge["dst"] if edge["src"] == node_id
                            else edge["src"]
                        )
                        if neighbor in visited:
                            continue
                        edge_weight = edge.get("weight", 1.0)
                        if edge_weight < min_weight:
                            continue
                        # Decay: energy * 0.5 * normalized_edge_weight
                        prop_energy = energy * 0.5 * min(edge_weight / 5.0, 1.0)
                        if prop_energy > 0.01:
                            next_frontier.append((neighbor, prop_energy))

            frontier = next_frontier

        # Remove seeds from results (caller already has them)
        seed_set = set(seed_ids)
        results = [
            {"id": nid, "activation": round(act, 4)}
            for nid, act in activation.items()
            if nid not in seed_set
        ]
        results.sort(key=lambda x: x["activation"], reverse=True)
        return results[:max_results]

    # ── Co-recall tracking ──────────────────────────────────────────

    def record_co_recall(self, fact_ids: list[str]) -> int:
        """Record that a set of facts were recalled together.

        Creates/strengthens co_recalled edges between all pairs.
        Returns the number of edges created or strengthened.
        """
        count = 0
        for i, a in enumerate(fact_ids):
            for b in fact_ids[i + 1:]:
                self.add_edge(a, b, "co_recalled", weight=1.0)
                count += 1
        return count

    # ── Cluster detection ───────────────────────────────────────────

    def detect_clusters(self, min_cluster_size: int = 3) -> list[list[str]]:
        """Simple connected-component clustering on co_recalled edges.

        Returns list of clusters (each is a list of fact IDs).
        """
        co_edges = [e for e in self._edges if e["type"] == "co_recalled"]
        adj: dict[str, set[str]] = defaultdict(set)
        for e in co_edges:
            adj[e["src"]].add(e["dst"])
            adj[e["dst"]].add(e["src"])

        visited: set[str] = set()
        clusters: list[list[str]] = []

        for node in adj:
            if node in visited:
                continue
            # BFS
            cluster: list[str] = []
            queue = [node]
            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                cluster.append(current)
                for neighbor in adj.get(current, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)
            if len(cluster) >= min_cluster_size:
                clusters.append(cluster)

        clusters.sort(key=len, reverse=True)
        return clusters

    # ── File/symbol touch tracking ──────────────────────────────────

    def record_touch(self, fact_id: str, file_path: str, symbol: str = "") -> dict:
        """Link a fact to a file (and optionally a symbol)."""
        target = f"file:{file_path}"
        result = self.add_edge(fact_id, target, "touches")
        if symbol:
            sym_target = f"symbol:{file_path}:{symbol}"
            self.add_edge(fact_id, sym_target, "touches")
        return result

    def get_facts_touching(self, file_path: str) -> list[str]:
        """Get fact IDs that touch a given file."""
        target = f"file:{file_path}"
        return [
            e["src"] for e in self._adjacency.get(target, [])
            if e["type"] == "touches"
        ]

    # ── Contradiction/refinement tracking ───────────────────────────

    def record_contradiction(self, old_fact_id: str, new_fact_id: str) -> dict:
        """Mark that new_fact contradicts old_fact."""
        return self.add_edge(new_fact_id, old_fact_id, "contradicts")

    def record_refinement(self, old_fact_id: str, new_fact_id: str) -> dict:
        """Mark that new_fact refines/updates old_fact."""
        return self.add_edge(new_fact_id, old_fact_id, "refines")

    # ── Stats ───────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Summary statistics about the graph."""
        type_counts: dict[str, int] = defaultdict(int)
        for e in self._edges:
            type_counts[e["type"]] += 1
        nodes = set()
        for e in self._edges:
            nodes.add(e["src"])
            nodes.add(e["dst"])
        return {
            "total_edges": len(self._edges),
            "total_nodes": len(nodes),
            "edge_types": dict(type_counts),
            "clusters": len(self.detect_clusters()),
        }

    # ── Maintenance ─────────────────────────────────────────────────

    def decay_edges(self, half_life_days: float = 30.0) -> int:
        """Reduce weight of stale edges. Returns count of decayed edges."""
        import math
        now = datetime.now(timezone.utc)
        decayed = 0
        to_remove: list[dict] = []

        for edge in self._edges:
            try:
                last = datetime.fromisoformat(edge.get("last_seen", ""))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age_days = (now - last).total_seconds() / 86400
            except (ValueError, TypeError):
                age_days = 0

            if age_days > 0:
                decay = math.exp(-0.693 * age_days / half_life_days)
                edge["weight"] = round(edge["weight"] * decay, 4)
                decayed += 1

            if edge["weight"] < 0.01:
                to_remove.append(edge)

        for edge in to_remove:
            self._edges.remove(edge)

        if to_remove or decayed:
            self._rebuild_adjacency()
            self._save()

        return decayed

    # ── Internal ────────────────────────────────────────────────────

    def _find_edge(self, src: str, dst: str, edge_type: str) -> dict | None:
        for e in self._adjacency.get(src, []):
            if e["dst"] == dst and e["type"] == edge_type:
                return e
            if e["src"] == dst and e["type"] == edge_type:
                return e
        return None

    def _prune_weakest(self, count: int = 100):
        """Remove the weakest edges globally."""
        self._edges.sort(key=lambda e: e.get("weight", 0))
        del self._edges[:count]
        self._rebuild_adjacency()

    def _prune_node_edges(self, node_id: str, keep: int = 40):
        """Prune weakest edges for a specific node."""
        node_edges = [
            e for e in self._edges
            if e["src"] == node_id or e["dst"] == node_id
        ]
        if len(node_edges) <= keep:
            return
        node_edges.sort(key=lambda e: e.get("weight", 0))
        to_remove = set(id(e) for e in node_edges[:len(node_edges) - keep])
        self._edges = [e for e in self._edges if id(e) not in to_remove]
        self._rebuild_adjacency()

    def _rebuild_adjacency(self):
        self._adjacency = defaultdict(list)
        for e in self._edges:
            self._adjacency[e["src"]].append(e)
            self._adjacency[e["dst"]].append(e)

    def _load(self):
        if not self.graph_file.exists():
            self._edges = []
            self._adjacency = defaultdict(list)
            return
        try:
            with open(self.graph_file, encoding="utf-8") as f:
                data = json.load(f)
            self._edges = data.get("edges", [])
            self._rebuild_adjacency()
        except Exception:
            self._edges = []
            self._adjacency = defaultdict(list)

    def _save(self):
        data = {"edges": self._edges}
        with open(self.graph_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
