"""Hub + MCP surfaces for path-based linkage and multi-level hierarchy (2.96).

Covers what step 1's service-level tests cannot: that the new capability is
actually reachable.

- POST /api/projects/subprojects/inspect  -- read-only path report
- GET  /api/projects/subprojects?depth=   -- recursive tree, depth-clamped
- GET  /api/projects/hierarchy            -- the whole forest
- POST /api/projects/subprojects/link     -- register-and-link one-shot
- c3_project sub_tree / sub_inspect / sub_link

Fixture conventions follow tests/test_hub_subproject_links.py (temp dirs,
pm_mod._PROJECTS_FILE patching, hub_server.app.test_client()).
"""
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
from services import project_runtime as pr_mod  # noqa: E402
from services import subprojects as sp  # noqa: E402


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class LinkageBase(unittest.TestCase):
    """Three sibling projects -- the shape containment could never express.

    root/, alpha/, beta/ all live side by side. None contains another, so the
    only way to relate them is an absolute-path link.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self._make_project("root")
        self.alpha = self._make_project("alpha")
        self.beta = self._make_project("beta")
        self.reg_file = self.base / "projects.json"
        _write_json(self.reg_file, {"projects": []})
        # project_runtime reads ~/.c3/projects.json independently of
        # ProjectManager, and c3_project's registration gate goes through it.
        self._patches = [
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
            mock.patch.object(pr_mod, "_PROJECTS_FILE", self.reg_file),
        ]
        for p in self._patches:
            p.start()
        self.client = hub_server.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _make_project(self, name: str) -> Path:
        d = self.base / name
        (d / ".c3").mkdir(parents=True)
        _write_json(d / ".c3" / "config.json",
                    {"meta": {"name": name}, "version": "2.95.0", "ide": "vscode"})
        return d

    def _register_all(self):
        _write_json(self.reg_file, {"projects": [
            {"name": p.name, "path": str(p), "ide": "vscode"}
            for p in (self.root, self.alpha, self.beta)
        ]})

    def _chain(self):
        """root -> alpha -> beta, all by absolute path."""
        self._register_all()
        self.assertTrue(sp.SubprojectManager(str(self.root))
                        .add(str(self.alpha), reindex_parent=False)["added"])
        self.assertTrue(sp.SubprojectManager(str(self.alpha))
                        .add(str(self.beta), reindex_parent=False)["added"])


class TestInspectEndpoint(LinkageBase):
    def test_requires_a_path(self):
        resp = self.client.post("/api/projects/subprojects/inspect", json={})
        self.assertEqual(resp.status_code, 400)

    def test_reports_an_unregistered_project_without_registering_it(self):
        resp = self.client.post("/api/projects/subprojects/inspect",
                                json={"path": str(self.alpha)})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["has_c3"])
        self.assertFalse(body["registered"])
        self.assertEqual(body["project"]["name"], "alpha")
        self.assertTrue(body["linkable"])
        # The whole point of inspect: it must not mutate the registry.
        self.assertEqual(json.loads(self.reg_file.read_text())["projects"], [])

    def test_reports_the_parent_chain_of_a_linked_child(self):
        self._chain()
        resp = self.client.post("/api/projects/subprojects/inspect",
                                json={"path": str(self.beta)})
        body = resp.get_json()
        self.assertEqual(body["depth"], 2)
        self.assertEqual([a["name"] for a in body["ancestors"]], ["alpha", "root"])
        self.assertFalse(body["linkable"])  # already claimed

    def test_plain_folder_is_reported_as_not_a_project(self):
        plain = self.base / "plain"
        plain.mkdir()
        body = self.client.post("/api/projects/subprojects/inspect",
                                json={"path": str(plain)}).get_json()
        self.assertFalse(body["has_c3"])
        self.assertIsNone(body["project"])
        self.assertTrue(body["linkable"])


class TestTreeEndpointDepth(LinkageBase):
    def test_default_depth_returns_the_whole_hierarchy(self):
        self._chain()
        resp = self.client.get("/api/projects/subprojects",
                               query_string={"parent": str(self.root)})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(len(body["children"]), 1)
        self.assertEqual(len(body["children"][0]["children"]), 1)
        self.assertEqual(body["rollup"]["children"], 2)
        self.assertEqual(body["rollup"]["direct_children"], 1)

    def test_depth_1_is_the_legacy_shape(self):
        self._chain()
        body = self.client.get("/api/projects/subprojects",
                               query_string={"parent": str(self.root), "depth": 1}).get_json()
        self.assertEqual(body["children"][0]["children"], [])

    def test_non_integer_depth_is_rejected(self):
        resp = self.client.get("/api/projects/subprojects",
                               query_string={"parent": str(self.root), "depth": "deep"})
        self.assertEqual(resp.status_code, 400)

    def test_parent_is_required(self):
        self.assertEqual(self.client.get("/api/projects/subprojects").status_code, 400)


class TestHierarchyEndpoint(LinkageBase):
    def test_flat_registry_is_all_roots(self):
        self._register_all()
        body = self.client.get("/api/projects/hierarchy").get_json()
        self.assertEqual(body["root_count"], 3)
        self.assertEqual(body["linked_count"], 0)

    def test_linked_projects_collapse_into_one_root(self):
        self._chain()
        body = self.client.get("/api/projects/hierarchy").get_json()
        self.assertEqual(body["root_count"], 1)
        self.assertEqual(body["linked_count"], 2)
        root = body["roots"][0]
        self.assertEqual(Path(root["parent"]["path"]).resolve(), self.root.resolve())
        self.assertEqual(root["rollup"]["children"], 2)

    def test_a_child_of_an_unregistered_parent_still_surfaces_as_a_root(self):
        # A broken or half-registered link must never make a project vanish
        # from the listing.
        self._chain()
        _write_json(self.reg_file, {"projects": [
            {"name": "alpha", "path": str(self.alpha), "parent_path": str(self.root)},
        ]})
        body = self.client.get("/api/projects/hierarchy").get_json()
        self.assertEqual(body["root_count"], 1)
        self.assertEqual(Path(body["roots"][0]["parent"]["path"]).resolve(),
                         self.alpha.resolve())


class TestLinkEndpoint(LinkageBase):
    """/link shells out to `c3 sub link --json`, like /add and /remove."""

    def test_requires_parent_and_folder(self):
        resp = self.client.post("/api/projects/subprojects/link",
                                json={"parent": str(self.root)})
        self.assertEqual(resp.status_code, 400)

    def test_passes_the_link_verb_and_reports_success(self):
        payload = {"added": True, "name": "alpha", "path": str(self.alpha),
                   "link_kind": "external", "rel_path": None, "depth": 1}
        with mock.patch.object(hub_server, "_run_c3",
                               return_value={"success": True,
                                             "output": json.dumps(payload)}) as run:
            resp = self.client.post("/api/projects/subprojects/link",
                                    json={"parent": str(self.root),
                                          "folder": str(self.alpha), "name": "alpha"})
        self.assertEqual(resp.status_code, 201)
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ["sub", "link"])
        self.assertIn("--json", args)
        self.assertIn("--name", args)
        self.assertNotIn("--init", args)
        self.assertEqual(resp.get_json()["result"]["link_kind"], "external")

    def test_init_flag_is_forwarded(self):
        with mock.patch.object(hub_server, "_run_c3",
                               return_value={"success": True,
                                             "output": json.dumps({"added": True})}) as run:
            self.client.post("/api/projects/subprojects/link",
                             json={"parent": str(self.root), "folder": str(self.alpha),
                                   "init": True})
        self.assertIn("--init", run.call_args[0][0])

    def test_failure_surfaces_at_result_error(self):
        # The frontend's apiErr() digs the message out of result.error, so a
        # refusal must land there rather than only in the HTTP status.
        err = {"added": False, "error": "not a C3 project"}
        with mock.patch.object(hub_server, "_run_c3",
                               return_value={"success": True, "output": json.dumps(err)}):
            resp = self.client.post("/api/projects/subprojects/link",
                                    json={"parent": str(self.root),
                                          "folder": str(self.alpha)})
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json()["result"]["error"], "not a C3 project")


class TestProjectsListDepth(LinkageBase):
    def test_depth_is_reported_per_project(self):
        self._chain()
        rows = {r["name"]: r for r in self.client.get("/api/projects").get_json()}
        self.assertEqual(rows["root"]["depth"], 0)
        self.assertEqual(rows["alpha"]["depth"], 1)
        self.assertEqual(rows["beta"]["depth"], 2)
        # A mid-tree node is both a child and a parent.
        self.assertTrue(rows["alpha"]["is_parent"])
        self.assertEqual(Path(rows["alpha"]["parent_path"]).resolve(), self.root.resolve())

    def test_a_registry_cycle_cannot_hang_the_listing(self):
        _write_json(self.reg_file, {"projects": [
            {"name": "root", "path": str(self.root), "parent_path": str(self.alpha)},
            {"name": "alpha", "path": str(self.alpha), "parent_path": str(self.root)},
        ]})
        rows = {r["name"]: r for r in self.client.get("/api/projects").get_json()}
        self.assertIn("root", rows)
        self.assertLessEqual(rows["root"]["depth"], 2)


class TestMcpSubOps(LinkageBase):
    """c3_project sub_tree / sub_inspect / sub_link via handle_project."""

    def _call(self, action, **kw):
        from cli.tools.project import handle_project

        def finalize(_n, _a, resp, _s="", **_k):
            return resp

        return handle_project(action, None, finalize, project=str(self.root), **kw)

    def test_sub_tree_renders_every_level(self):
        self._chain()
        out = self._call("sub_tree")
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertIn("2 total", out)

    def test_subprojects_stays_direct_children_only(self):
        self._chain()
        out = self._call("subprojects")
        self.assertIn("alpha", out)
        self.assertNotIn("beta", out)

    def test_sub_inspect_is_a_read(self):
        self._register_all()
        out = self._call("sub_inspect", target=str(self.alpha))
        self.assertIn("alpha", out)
        self.assertNotIn("blocked", out)
        # No allow_write was passed and nothing was linked.
        self.assertEqual(sp.SubprojectManager(str(self.root)).list(), [])

    def test_sub_link_requires_allow_write(self):
        self._register_all()
        out = self._call("sub_link", target=str(self.alpha))
        self.assertIn("allow_write", out)
        self.assertEqual(sp.SubprojectManager(str(self.root)).list(), [])

    def test_sub_link_creates_an_external_link(self):
        self._register_all()
        out = self._call("sub_link", target=str(self.alpha), allow_write=True)
        self.assertIn("Linked by path", out)
        rows = sp.SubprojectManager(str(self.root)).list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["link_kind"], "external")

    def test_sub_link_refuses_a_folder_that_is_not_a_project(self):
        self._register_all()
        plain = self.base / "plain"
        plain.mkdir()
        out = self._call("sub_link", target=str(plain), allow_write=True)
        self.assertIn("not a C3 project", out)
        self.assertIn("sub_add", out)  # points at the verb that would work


if __name__ == "__main__":
    unittest.main()
