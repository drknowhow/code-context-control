"""Consumers that used to walk exactly one hop (2.96).

Search/memory federation and the three Oracle surfaces each resolved "the
children" as the parent's direct children. With multi-level hierarchies that
silently drops everything below the first level -- a grandchild is neither
searched nor counted, and nothing reports an error. These pin the transitive
behaviour.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import project_manager as pm_mod  # noqa: E402
from services import subprojects as sp  # noqa: E402


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class DepthBase(unittest.TestCase):
    """root -> alpha -> beta, linked by absolute path (all siblings on disk)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.reg_file = self.base / "projects.json"
        _write_json(self.reg_file, {"projects": []})
        self._patches = [
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
        ]
        for p in self._patches:
            p.start()

        self.root, self.alpha, self.beta = (self._project(n) for n in ("root", "alpha", "beta"))
        _write_json(self.reg_file, {"projects": [
            {"name": p.name, "path": str(p), "ide": "vscode"}
            for p in (self.root, self.alpha, self.beta)
        ]})
        sp.SubprojectManager(str(self.root)).add(str(self.alpha), reindex_parent=False)
        sp.SubprojectManager(str(self.alpha)).add(str(self.beta), reindex_parent=False)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _project(self, name: str) -> Path:
        d = self.base / name
        (d / ".c3").mkdir(parents=True)
        _write_json(d / ".c3" / "config.json", {"meta": {"name": name}})
        return d


class TestFederationDepth(DepthBase):
    def test_scopes_reach_the_grandchild(self):
        from cli.tools import federate
        svc = mock.Mock(project_path=str(self.root))
        names = [s["name"] for s in federate.subproject_scopes(svc, "all")]
        self.assertEqual(names, ["alpha", "beta"])  # breadth-first, nearest first

    def test_a_named_scope_can_be_a_grandchild(self):
        from cli.tools import federate
        svc = mock.Mock(project_path=str(self.root))
        scopes = federate.subproject_scopes(svc, "beta")
        self.assertEqual(len(scopes), 1)
        self.assertEqual(Path(scopes[0]["path"]).resolve(), self.beta.resolve())

    def test_cap_drops_the_most_distant_first(self):
        # Breadth-first ordering means a cap of 1 keeps the direct child.
        from cli.tools import federate
        cfg = self.root / ".c3" / "config.json"
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data["hybrid"] = {"subprojects": {"max_children_per_query": 1}}
        _write_json(cfg, data)
        svc = mock.Mock(project_path=str(self.root))
        self.assertEqual([s["name"] for s in federate.subproject_scopes(svc, "all")],
                         ["alpha"])

    def test_a_child_with_no_c3_is_skipped(self):
        import shutil

        from cli.tools import federate
        shutil.rmtree(self.beta / ".c3")
        svc = mock.Mock(project_path=str(self.root))
        self.assertEqual([s["name"] for s in federate.subproject_scopes(svc, "all")],
                         ["alpha"])


class TestOracleScopeDepth(DepthBase):
    def _bridge_projects(self):
        return [
            {"path": str(self.root), "name": "root", "has_c3": True},
            {"path": str(self.alpha), "name": "alpha", "has_c3": True,
             "parent_path": str(self.root), "is_subproject": True},
            {"path": str(self.beta), "name": "beta", "has_c3": True,
             "parent_path": str(self.alpha), "is_subproject": True},
        ]

    def _scoped(self, scope):
        from oracle.services.c3_bridge import C3Bridge
        bridge = C3Bridge.__new__(C3Bridge)
        bridge.scanner = mock.Mock()
        with mock.patch.object(C3Bridge, "_discover_c3_projects",
                               return_value=self._bridge_projects()), \
             mock.patch("oracle.services.c3_bridge.validate_project_path",
                        side_effect=lambda _s, p: p), \
             mock.patch("services.project_runtime.resolve_project",
                        return_value={"path": str(self.root), "name": "root"}):
            return [p["name"] for p in bridge._scoped_projects(scope)]

    def test_scope_covers_the_whole_subtree(self):
        self.assertEqual(sorted(self._scoped("root")), ["alpha", "beta", "root"])

    def test_top_scope_excludes_every_subproject(self):
        from oracle.services.c3_bridge import C3Bridge
        bridge = C3Bridge.__new__(C3Bridge)
        bridge.scanner = mock.Mock()
        with mock.patch.object(C3Bridge, "_discover_c3_projects",
                               return_value=self._bridge_projects()):
            self.assertEqual([p["name"] for p in bridge._scoped_projects("top")], ["root"])

    def test_a_registry_cycle_terminates(self):
        from oracle.services.c3_bridge import C3Bridge
        rows = self._bridge_projects()
        rows[0]["parent_path"] = str(self.beta)  # root <- beta, closing a loop
        bridge = C3Bridge.__new__(C3Bridge)
        bridge.scanner = mock.Mock()
        with mock.patch.object(C3Bridge, "_discover_c3_projects", return_value=rows), \
             mock.patch("oracle.services.c3_bridge.validate_project_path",
                        side_effect=lambda _s, p: p), \
             mock.patch("services.project_runtime.resolve_project",
                        return_value={"path": str(self.root), "name": "root"}):
            names = [p["name"] for p in bridge._scoped_projects("root")]
        self.assertEqual(sorted(names), ["alpha", "beta", "root"])


class TestScannerCountsExternalChildren(DepthBase):
    def test_subproject_count_includes_children_with_no_rel_path(self):
        # root's only child is external, so rel_paths is empty -- the count
        # must not be driven by it.
        from oracle.services.project_scanner import ProjectScanner
        scanner = ProjectScanner.__new__(ProjectScanner)
        row = scanner._enrich({"path": str(self.root), "name": "root"})
        self.assertEqual(row["subproject_rel_paths"], [])
        self.assertEqual(row["subproject_count"], 1)
        self.assertEqual([Path(p).resolve() for p in row["subproject_paths"]],
                         [self.alpha.resolve()])

    def test_nested_child_still_reports_a_rel_path(self):
        from oracle.services.project_scanner import ProjectScanner
        nested = self.root / "inside"
        (nested / ".c3").mkdir(parents=True)
        _write_json(nested / ".c3" / "config.json", {})
        sp.SubprojectManager(str(self.root)).add("inside", reindex_parent=False)

        scanner = ProjectScanner.__new__(ProjectScanner)
        row = scanner._enrich({"path": str(self.root), "name": "root"})
        self.assertEqual(row["subproject_rel_paths"], ["inside"])
        self.assertEqual(row["subproject_count"], 2)

    def test_grandchild_is_marked_a_subproject(self):
        from oracle.services.project_scanner import ProjectScanner
        scanner = ProjectScanner.__new__(ProjectScanner)
        row = scanner._enrich({"path": str(self.beta), "name": "beta"})
        self.assertTrue(row["is_subproject"])
        self.assertEqual(Path(row["parent_path"]).resolve(), self.alpha.resolve())


class TestFederatedGraphDepth(DepthBase):
    def test_depth_is_derived_from_the_edge_set(self):
        from oracle.services.federated_graph import FederatedGraph, _slugify
        graph = FederatedGraph.__new__(FederatedGraph)
        paths = [str(self.root), str(self.alpha), str(self.beta)]
        result = {"projects": [{"slug": _slugify(p)} for p in paths]}
        out = graph._apply_hierarchy(result, paths)

        by_slug = {p["slug"]: p for p in out["projects"]}
        self.assertEqual(by_slug[_slugify(str(self.root))]["depth"], 0)
        self.assertEqual(by_slug[_slugify(str(self.alpha))]["depth"], 1)
        self.assertEqual(by_slug[_slugify(str(self.beta))]["depth"], 2)
        self.assertEqual(out["stats"]["parent_child"], 2)
        self.assertEqual(out["stats"]["max_depth"], 2)

    def test_parent_outside_the_set_is_not_an_edge(self):
        from oracle.services.federated_graph import FederatedGraph, _slugify
        graph = FederatedGraph.__new__(FederatedGraph)
        paths = [str(self.alpha), str(self.beta)]  # root omitted
        result = {"projects": [{"slug": _slugify(p)} for p in paths]}
        out = graph._apply_hierarchy(result, paths)
        by_slug = {p["slug"]: p for p in out["projects"]}
        self.assertEqual(by_slug[_slugify(str(self.alpha))]["depth"], 0)
        self.assertEqual(by_slug[_slugify(str(self.beta))]["depth"], 1)


if __name__ == "__main__":
    unittest.main()
