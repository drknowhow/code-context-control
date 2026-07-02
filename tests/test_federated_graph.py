"""Tests for FederatedGraph service (cross-project memory graph)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oracle.services.federated_graph import FederatedGraph, _slugify


def _write_project(root: Path, name: str, facts: list[dict], edges: list[dict] | None = None) -> str:
    proj = root / name
    (proj / ".c3" / "facts").mkdir(parents=True, exist_ok=True)
    (proj / ".c3" / "facts" / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
    if edges:
        (proj / ".c3" / "facts" / "memory_graph.json").write_text(
            json.dumps({"edges": edges, "adjacency": {}}), encoding="utf-8"
        )
    return str(proj)


class TestFederatedGraph(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self.path_a = _write_project(self.root, "proj-a", [
            {"id": "a1", "fact": "use rate limiter on api gateway", "category": "architecture", "relevance_count": 5, "lifecycle": "active"},
            {"id": "a2", "fact": "logger uses structured json", "category": "convention", "relevance_count": 2, "lifecycle": "active"},
        ], edges=[{"src": "a1", "dst": "a2", "type": "co_recalled", "weight": 1.5}])

        self.path_b = _write_project(self.root, "proj-b", [
            {"id": "b1", "fact": "api gateway rate limiter enabled", "category": "architecture", "relevance_count": 4, "lifecycle": "active"},
            {"id": "b2", "fact": "completely unrelated biology note", "category": "general", "relevance_count": 1, "lifecycle": "active"},
        ], edges=[])

        # Patch cache location to tmp so tests don't pollute ~/.c3
        cache_dir = self.root / "_oracle"
        cache_dir.mkdir()
        self._patches = [
            patch("oracle.services.federated_graph.ORACLE_DIR", cache_dir),
            patch("oracle.services.federated_graph._CACHE_FILE", cache_dir / "federated_graph.json"),
            patch("oracle.services.federated_graph._EMBED_CACHE_FILE", cache_dir / "federated_embeddings.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_builds_per_project_nodes_and_within_edges(self):
        fed = FederatedGraph(ollama_bridge=None)  # forces TF-IDF fallback
        data = fed.build([self.path_a, self.path_b])

        self.assertEqual(len(data["nodes"]), 4)
        slugs = {n["project"] for n in data["nodes"]}
        self.assertEqual(slugs, {_slugify(self.path_a), _slugify(self.path_b)})

        within = [e for e in data["edges"] if e["scope"] == "within_project"]
        self.assertEqual(len(within), 1)
        # Node IDs are project-scoped
        self.assertTrue(all(":" in n["id"] for n in data["nodes"]))

    def test_cross_similar_edge_found_between_projects(self):
        fed = FederatedGraph(ollama_bridge=None)
        data = fed.build([self.path_a, self.path_b], min_sim=0.1)  # low threshold for TF-IDF
        cross = [e for e in data["edges"] if e["scope"] == "cross_similar"]
        self.assertTrue(any(
            (e["src"].endswith("a1") and e["dst"].endswith("b1")) or
            (e["src"].endswith("b1") and e["dst"].endswith("a1"))
            for e in cross
        ), f"expected cross_similar edge between a1 and b1, got {cross}")
        self.assertEqual(data["stats"]["similarity_method"], "tfidf")

    def test_unrelated_facts_not_linked_at_high_threshold(self):
        fed = FederatedGraph(ollama_bridge=None)
        data = fed.build([self.path_a, self.path_b], min_sim=0.99)
        cross = [e for e in data["edges"] if e["scope"] == "cross_similar"]
        self.assertEqual(cross, [])

    def test_cache_hit_on_unchanged_mtimes(self):
        fed = FederatedGraph(ollama_bridge=None)
        first = fed.build([self.path_a, self.path_b])
        # Second call should return cached structure (same generated_at)
        second = fed.build([self.path_a, self.path_b])
        self.assertEqual(first["generated_at"], second["generated_at"])

    def test_force_rebuild_bypasses_cache(self):
        fed = FederatedGraph(ollama_bridge=None)
        first = fed.build([self.path_a, self.path_b])
        second = fed.build([self.path_a, self.path_b], force=True)
        self.assertGreaterEqual(second["generated_at"], first["generated_at"])

    def test_99_project_cap(self):
        fed = FederatedGraph(ollama_bridge=None)
        many = [self.path_a] * 150
        data = fed.build(many)
        # Should not crash; capped to 99 duplicates → same slugs, but node list bounded
        self.assertEqual(data["stats"]["projects"], 99)

    def test_ollama_embedding_path_used_when_available(self):
        class FakeBridge:
            def embed(self, text, model="x"):
                # simple hash-based vector so similar strings map closely
                import hashlib
                h = hashlib.md5(text.encode()).digest()
                return [b / 255.0 for b in h]

            def embed_batch(self, texts, model="x"):
                return [self.embed(t, model) for t in texts]

        fed = FederatedGraph(ollama_bridge=FakeBridge())
        data = fed.build([self.path_a, self.path_b], min_sim=0.0, force=True)
        self.assertTrue(data["stats"]["similarity_method"].startswith("embedding:"))


class TestFederatedGraphAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path_a = _write_project(self.root, "p-a", [
            {"id": "a1", "fact": "shared rate limiter pattern", "category": "architecture", "relevance_count": 3, "lifecycle": "active"},
        ])
        self.path_b = _write_project(self.root, "p-b", [
            {"id": "b1", "fact": "shared rate limiter pattern", "category": "architecture", "relevance_count": 3, "lifecycle": "active"},
        ])

        cache_dir = self.root / "_oracle"
        cache_dir.mkdir()
        self._patches = [
            patch("oracle.services.federated_graph.ORACLE_DIR", cache_dir),
            patch("oracle.services.federated_graph._CACHE_FILE", cache_dir / "federated_graph.json"),
            patch("oracle.services.federated_graph._EMBED_CACHE_FILE", cache_dir / "federated_embeddings.json"),
        ]
        for p in self._patches:
            p.start()

        from oracle import oracle_server as srv
        from oracle.services.federated_graph import FederatedGraph as FG

        class FakeScanner:
            def discover(self_inner):
                return [
                    {"path": self.path_a, "has_facts": True},
                    {"path": self.path_b, "has_facts": True},
                ]

        self._srv_patches = [
            patch.object(srv, "_federated", FG(ollama_bridge=None)),
            patch.object(srv, "_scanner", FakeScanner()),
        ]
        for p in self._srv_patches:
            p.start()

        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()
        # Mutating endpoints (rebuild) sit behind the local write gate;
        # GET / issues the dashboard session cookie into the client's jar.
        self.client.get("/")

    def tearDown(self):
        for p in self._srv_patches:
            p.stop()
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_federated_graph_endpoint(self):
        resp = self.client.get("/api/graph/federated?min_sim=0.1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["nodes"]), 2)
        self.assertEqual(data["stats"]["projects"], 2)

    def test_rebuild_endpoint(self):
        resp = self.client.post("/api/graph/federated/rebuild",
                                data=json.dumps({}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200)

    def test_stats_endpoint(self):
        resp = self.client.get("/api/graph/federated/stats")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("stats", resp.get_json())

    def test_node_detail_404(self):
        resp = self.client.get("/api/graph/federated/node/nope:missing")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
