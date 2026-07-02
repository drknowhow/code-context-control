"""Federated search/recall across sub-projects (cli/tools/federate.py)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cli.tools import federate
from cli.tools.memory import handle_memory
from cli.tools.search import handle_search
from services.memory import MemoryStore


def _passthrough_finalize(_name, _args, resp, _summ="", **_kw):
    return resp


class _StubSessionMgr:
    current_session = {}


class _StubSvc:
    """Minimal svc for recall paths: real MemoryStore, no vector/graph/preloader."""

    def __init__(self, project_path: str):
        self.project_path = str(project_path)
        self.memory = MemoryStore(self.project_path)
        self.session_mgr = _StubSessionMgr()
        self.vector_store = None


class FederationBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.parent = self.base / "parent"
        (self.parent / ".c3").mkdir(parents=True)
        self.children = {}
        for name in ("api", "web"):
            child = self.parent / name
            (child / ".c3").mkdir(parents=True)
            (child / ".c3" / "config.json").write_text(
                json.dumps({"parent": {"name": "parent", "path": str(self.parent.resolve())}}),
                encoding="utf-8")
            self.children[name] = child
        (self.parent / ".c3" / "config.json").write_text(json.dumps({
            "meta": {"name": "parent"},
            "subprojects": [
                {"name": "api", "rel_path": "api", "added_at": "x"},
                {"name": "web", "rel_path": "web", "added_at": "x"},
            ],
        }), encoding="utf-8")
        # Registry so _entry_status == ok
        self.reg_file = self.base / "projects.json"
        self.reg_file.write_text(json.dumps({"projects": [
            {"name": n, "path": str(c.resolve()), "parent_path": str(self.parent.resolve())}
            for n, c in self.children.items()
        ]}), encoding="utf-8")
        from services import project_manager as pm_mod
        self._reg_patch = mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file)
        self._reg_patch.start()

    def tearDown(self):
        self._reg_patch.stop()
        self._tmp.cleanup()


class TestSubprojectScopes(FederationBase):
    def test_all_children(self):
        scopes = federate.subproject_scopes(_StubSvc(self.parent), "all")
        self.assertEqual({s["name"] for s in scopes}, {"api", "web"})

    def test_named_scope(self):
        scopes = federate.subproject_scopes(_StubSvc(self.parent), "API")
        self.assertEqual([s["name"] for s in scopes], ["api"])

    def test_no_children(self):
        (self.parent / ".c3" / "config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(federate.subproject_scopes(_StubSvc(self.parent), "all"), [])

    def test_max_children_cap(self):
        cfg = json.loads((self.parent / ".c3" / "config.json").read_text())
        cfg["hybrid"] = {"subprojects": {"max_children_per_query": 1}}
        (self.parent / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        scopes = federate.subproject_scopes(_StubSvc(self.parent), "all")
        self.assertEqual(len(scopes), 1)


class TestFederatedSearch(FederationBase):
    def _run(self, scope, top_k=5, max_tokens=1000, fail_for=None):
        calls = []

        def fake_search(query, action, top_k, max_tokens, svc, finalize, facts, **kw):
            calls.append({"path": svc.project_path, "top_k": top_k, "max_tokens": max_tokens})
            return f"HIT:{Path(svc.project_path).name}"

        def fake_runtime(path):
            if fail_for and Path(path).name == fail_for:
                raise RuntimeError("boom")
            return _StubSvc(path)

        with mock.patch("cli.tools.search.handle_search", side_effect=fake_search), \
             mock.patch.object(federate, "_child_runtime", side_effect=fake_runtime):
            resp = federate.federated_search(
                "query", "code", top_k, max_tokens, _StubSvc(self.parent),
                _passthrough_finalize, lambda *a, **k: "", scope)
        return resp, calls

    def test_scope_all_sections_and_budget(self):
        resp, calls = self._run("all", top_k=5, max_tokens=1000)
        self.assertIn("HIT:parent", resp)
        self.assertIn("=== [sub:api] ===", resp)
        self.assertIn("=== [sub:web] ===", resp)
        parent_call = calls[0]
        self.assertEqual(parent_call["max_tokens"], 600)   # 60% share
        for child_call in calls[1:]:
            self.assertEqual(child_call["max_tokens"], 200)  # 40% / 2
            self.assertLessEqual(child_call["top_k"], 3)

    def test_named_scope_child_only(self):
        resp, calls = self._run("api")
        self.assertNotIn("HIT:parent", resp)
        self.assertIn("=== [sub:api] ===", resp)
        self.assertNotIn("[sub:web]", resp)

    def test_unknown_scope_message(self):
        resp, _ = self._run("nope")
        self.assertIn("no linked sub-project matches", resp)

    def test_child_failure_tolerated(self):
        resp, _ = self._run("all", fail_for="web")
        self.assertIn("HIT:parent", resp)
        self.assertIn("=== [sub:api] ===\nHIT:api", resp)
        self.assertIn("[error] RuntimeError: boom", resp)


class TestRecallRollup(FederationBase):
    def setUp(self):
        super().setUp()
        MemoryStore(str(self.children["api"])).remember(
            "alpha endpoint lives in api routes", "architecture")
        MemoryStore(str(self.children["web"])).remember(
            "alpha widget rendered in web layer", "ui")

    def test_federated_recall_tagged(self):
        lines = federate.federated_recall("alpha", 3, _StubSvc(self.parent))
        self.assertEqual(len(lines), 2)
        self.assertTrue(any(l.startswith("[sub:api][architecture]") for l in lines), lines)
        self.assertTrue(any(l.startswith("[sub:web][ui]") for l in lines), lines)

    def test_handle_memory_recall_unions_by_default(self):
        svc = _StubSvc(self.parent)
        svc.memory.remember("alpha fact in the parent store", "general")
        out = handle_memory("recall", "alpha", "", "", 5, svc, _passthrough_finalize)
        self.assertIn("alpha fact in the parent store", out)
        self.assertIn("[sub:api]", out)
        self.assertIn("+2 sub-project", out)

    def test_handle_memory_scope_project_disables(self):
        svc = _StubSvc(self.parent)
        svc.memory.remember("alpha fact in the parent store", "general")
        out = handle_memory("recall", "alpha", "", "", 5, svc,
                            _passthrough_finalize, scope="project")
        self.assertNotIn("[sub:", out)

    def test_handle_memory_rollup_config_off(self):
        cfg = json.loads((self.parent / ".c3" / "config.json").read_text())
        cfg["hybrid"] = {"subprojects": {"memory_rollup": False}}
        (self.parent / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        svc = _StubSvc(self.parent)
        svc.memory.remember("alpha fact in the parent store", "general")
        out = handle_memory("recall", "alpha", "", "", 5, svc, _passthrough_finalize)
        self.assertNotIn("[sub:", out)

    def test_handle_memory_explicit_scope_overrides_config_off(self):
        cfg = json.loads((self.parent / ".c3" / "config.json").read_text())
        cfg["hybrid"] = {"subprojects": {"memory_rollup": False}}
        (self.parent / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        out = handle_memory("recall", "alpha", "", "", 5, _StubSvc(self.parent),
                            _passthrough_finalize, scope="all")
        self.assertIn("[sub:api]", out)


class TestSearchScopeWiring(FederationBase):
    def test_handle_search_routes_scope_to_federation(self):
        with mock.patch.object(federate, "federated_search",
                               return_value="FEDERATED") as fed:
            out = handle_search("q", "code", 3, 800, _StubSvc(self.parent),
                                _passthrough_finalize, lambda *a, **k: "", scope="all")
        self.assertEqual(out, "FEDERATED")
        fed.assert_called_once()

    def test_handle_search_fanout_config_off(self):
        cfg = json.loads((self.parent / ".c3" / "config.json").read_text())
        cfg["hybrid"] = {"subprojects": {"search_fanout": False}}
        (self.parent / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        with mock.patch.object(federate, "federated_search") as fed:
            try:
                handle_search("q", "files", 3, 800, _StubSvc(self.parent),
                              _passthrough_finalize, lambda *a, **k: "", scope="all")
            except AttributeError:
                pass  # stub svc lacks indexer; reaching local search is the point
        fed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
