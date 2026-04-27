"""Tests for memory graph API endpoints + MEMORY.md sync."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli import server as srv
from services.memory import MemoryStore
from services.memory_graph import MemoryGraph


class _StubSession:
    current_session = {"id": "sess-test"}

    def list_sessions(self, n):
        return []


class _StubRuntime:
    def __init__(self, memory, graph):
        self.memory_graph = graph
        self.memory = memory


class TestMemoryGraphAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir(exist_ok=True)

        self.memory = MemoryStore(str(self.project))
        a = self.memory.remember("arch fact", "architecture", "sess-test")["id"]
        b = self.memory.remember("bug fact", "bug", "sess-test")["id"]
        c = self.memory.remember("conv fact", "convention", "sess-test")["id"]
        self.graph = MemoryGraph(str(self.project))
        self.graph.record_co_recall([a, b, c])
        self.ids = (a, b, c)

        # Patch globals in server module
        self._patches = [
            patch.object(srv, "memory_store", self.memory),
            patch.object(srv, "runtime", _StubRuntime(self.memory, self.graph)),
            patch.object(srv, "session_mgr", _StubSession()),
            patch.object(srv, "PROJECT_PATH", self.project),
        ]
        for p in self._patches:
            p.start()

        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_graph_returns_nodes_and_edges(self):
        resp = self.client.get("/api/memory/graph")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data["nodes"]), 3)
        self.assertEqual(len(data["edges"]), 3)  # 3 co-recall pairs
        self.assertIn("stats", data)
        self.assertEqual(data["stats"]["total_nodes"], 3)
        kinds = {n["kind"] for n in data["nodes"]}
        self.assertEqual(kinds, {"fact"})

    def test_graph_min_weight_filter(self):
        resp = self.client.get("/api/memory/graph?min_weight=99")
        data = resp.get_json()
        self.assertEqual(data["edges"], [])

    def test_fact_detail_returns_neighbors(self):
        a = self.ids[0]
        resp = self.client.get(f"/api/memory/fact/{a}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["fact"]["id"], a)
        self.assertEqual(len(data["neighbors"]), 2)

    def test_fact_detail_404(self):
        resp = self.client.get("/api/memory/fact/nonexistent")
        self.assertEqual(resp.status_code, 404)

    def test_patch_updates_fact_and_syncs_md(self):
        a = self.ids[0]
        resp = self.client.patch(
            f"/api/memory/facts/{a}",
            data=json.dumps({"fact": "updated arch fact"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        md = (self.project / ".c3" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("updated arch fact", md)
        self.assertIn("## architecture", md)

    def test_remember_syncs_md(self):
        resp = self.client.post(
            "/api/memory/remember",
            data=json.dumps({"fact": "new pref fact", "category": "preference"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        md = (self.project / ".c3" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("new pref fact", md)
        self.assertIn("## preference", md)

    def test_delete_syncs_md(self):
        a = self.ids[0]
        self.client.delete(f"/api/memory/facts/{a}")
        md = (self.project / ".c3" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertNotIn("arch fact", md.split("## bug")[0] if "## bug" in md else md)

    def test_trends_endpoint(self):
        resp = self.client.get("/api/memory/trends")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), dict)

    def test_lifespan_endpoint(self):
        resp = self.client.get("/api/memory/lifespan")
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.get_json(), dict)


if __name__ == "__main__":
    unittest.main()
