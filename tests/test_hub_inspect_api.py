"""Drill-in inspect, cross-project search, and config-editor endpoints (v2.44.0)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli import hub_server  # noqa: E402
from services import project_manager as pm_mod  # noqa: E402
from services import project_runtime as pr  # noqa: E402


class _StubMemory:
    def __init__(self):
        self.facts = [{"id": "f1", "fact": "alpha fact", "category": "general"}]

    def recall(self, query, top_k=5, session_id=""):
        return [f for f in self.facts if query.lower() in f["fact"].lower()][:top_k]


class _StubLedger:
    def get_stats(self):
        return {"total": 7}

    def get_history(self, file=None, limit=50):
        return [{"file": file or "x.py", "summary": "stub edit"}][:limit]


class _StubSessions:
    def list_sessions(self, limit=50):
        return [{"id": "s1"}]


class _StubIndexer:
    def search(self, query, top_k=5, max_tokens=4000, include_content=True):
        return [{"chunk_id": "c1", "file": "a.py", "name": "fn", "type": "function",
                 "lines": "1-5", "score": 0.9, "content": "def fn(): pass"}]


class _StubRuntime:
    def __init__(self):
        self.memory = _StubMemory()
        self.edit_ledger = _StubLedger()
        self.session_mgr = _StubSessions()
        self.indexer = _StubIndexer()


class HubApiBase(unittest.TestCase):
    def setUp(self):
        self.client = hub_server.app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.proj = self.base / "alpha"
        (self.proj / ".c3").mkdir(parents=True)
        (self.proj / ".c3" / "config.json").write_text(json.dumps({
            "hybrid": {"embed_model": "nomic-embed-text"},
            "version": "2.44.0",
        }), encoding="utf-8")
        self.reg_file = self.base / "projects.json"
        self.reg_file.write_text(json.dumps({"projects": [
            {"name": "alpha", "path": str(self.proj.resolve()), "ide": "claude-code"},
        ]}), encoding="utf-8")
        self._patches = [
            mock.patch.object(pr, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
            mock.patch.object(hub_server, "_get_runtime",
                              lambda path: _StubRuntime()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TestInspect(HubApiBase):
    def test_requires_path(self):
        resp = self.client.post("/api/projects/inspect", json={})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_project_404(self):
        resp = self.client.post("/api/projects/inspect",
                                json={"path": "zzz-does-not-exist"})
        self.assertEqual(resp.status_code, 404)

    def test_uninitialized_409_needs_init(self):
        bare = self.base / "bare"
        bare.mkdir()
        # Registered but not initialized: resolve succeeds, runtime build fails.
        self.reg_file.write_text(json.dumps({"projects": [
            {"name": "alpha", "path": str(self.proj.resolve()), "ide": "claude-code"},
            {"name": "bare", "path": str(bare.resolve()), "ide": "unknown"},
        ]}), encoding="utf-8")
        with mock.patch.object(hub_server, "_get_runtime",
                               side_effect=ValueError("No .c3 directory")):
            resp = self.client.post("/api/projects/inspect",
                                    json={"path": str(bare)})
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.get_json()["needs_init"])

    def test_overview_counts(self):
        resp = self.client.post("/api/projects/inspect",
                                json={"path": str(self.proj), "view": "overview"})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        self.assertEqual(data["counts"]["facts"], 1)
        self.assertEqual(data["counts"]["edits"], 7)
        self.assertEqual(data["counts"]["sessions"], 1)

    def test_memory_view_and_query(self):
        resp = self.client.post("/api/projects/inspect",
                                json={"path": str(self.proj), "view": "memory"})
        self.assertEqual(resp.get_json()["total"], 1)
        resp = self.client.post("/api/projects/inspect",
                                json={"path": str(self.proj), "view": "memory",
                                      "query": "alpha"})
        self.assertEqual(len(resp.get_json()["results"]), 1)

    def test_ledger_and_sessions_views(self):
        resp = self.client.post("/api/projects/inspect",
                                json={"path": str(self.proj), "view": "ledger"})
        self.assertEqual(resp.get_json()["stats"]["total"], 7)
        resp = self.client.post("/api/projects/inspect",
                                json={"path": str(self.proj), "view": "sessions"})
        self.assertEqual(len(resp.get_json()["sessions"]), 1)

    def test_unknown_view_400(self):
        resp = self.client.post("/api/projects/inspect",
                                json={"path": str(self.proj), "view": "explode"})
        self.assertEqual(resp.status_code, 400)


class TestGlobalSearch(HubApiBase):
    def test_requires_query(self):
        resp = self.client.post("/api/search/global", json={})
        self.assertEqual(resp.status_code, 400)

    def test_buckets_per_project(self):
        resp = self.client.post("/api/search/global", json={"query": "alpha"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["projects_searched"], 1)
        row = data["results"][0]
        self.assertEqual(row["project"]["name"], "alpha")
        self.assertEqual(len(row["code"]), 1)
        self.assertIn("snippet", row["code"][0])
        self.assertEqual(len(row["memory"]), 1)
        self.assertIsNone(row["error"])

    def test_broken_project_isolated(self):
        with mock.patch.object(hub_server, "_get_runtime",
                               side_effect=RuntimeError("boom")):
            resp = self.client.post("/api/search/global", json={"query": "alpha"})
        data = resp.get_json()
        self.assertEqual(data["results"][0]["error"], "boom")


class TestConfigEditor(HubApiBase):
    def test_get_returns_config_and_defaults(self):
        resp = self.client.get(
            f"/api/projects/config?path={self.proj}&section=hybrid")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["config"]["embed_model"], "nomic-embed-text")
        self.assertIn("embed_model", data["defaults"])

    def test_get_unknown_section_400(self):
        resp = self.client.get(f"/api/projects/config?path={self.proj}&section=nope")
        self.assertEqual(resp.status_code, 400)

    def test_put_round_trip_and_audit(self):
        resp = self.client.put("/api/projects/config", json={
            "path": str(self.proj), "section": "hybrid",
            "values": {"show_context_nudges": "false"},
        })
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIs(resp.get_json()["config"]["show_context_nudges"], False)  # coerced
        cfg = json.loads((self.proj / ".c3" / "config.json").read_text())
        self.assertIs(cfg["hybrid"]["show_context_nudges"], False)
        self.assertEqual(cfg["version"], "2.44.0")  # untouched siblings
        log = self.proj / ".c3" / "activity_log.jsonl"
        self.assertTrue(log.exists())
        self.assertIn("hub_config_write", log.read_text(encoding="utf-8"))

    def test_put_refuses_protected_keys_and_sections(self):
        for values in ({"version": "9.9.9"}, {"subprojects": []}, {"parent": {}}):
            resp = self.client.put("/api/projects/config", json={
                "path": str(self.proj), "section": "hybrid", "values": values})
            self.assertEqual(resp.status_code, 400, values)
        resp = self.client.put("/api/projects/config", json={
            "path": str(self.proj), "section": "bitbucket", "values": {"x": 1}})
        self.assertEqual(resp.status_code, 400)


class TestHubConfigPrefs(unittest.TestCase):
    def test_new_keys_persist(self):
        client = hub_server.app.test_client()
        with tempfile.TemporaryDirectory() as td:
            cfg_file = Path(td) / "hub_config.json"
            with mock.patch.object(hub_server, "_HUB_CONFIG_FILE", cfg_file):
                resp = client.post("/api/hub/config", json={
                    "sidebar_group": "tag:web",
                    "sidebar_collapsed": True,
                    "runtime_cache_size": 12,
                })
                self.assertEqual(resp.status_code, 200)
                saved = resp.get_json()["config"]
                self.assertEqual(saved["sidebar_group"], "tag:web")
                self.assertTrue(saved["sidebar_collapsed"])
                self.assertEqual(saved["runtime_cache_size"], 12)


if __name__ == "__main__":
    unittest.main()
