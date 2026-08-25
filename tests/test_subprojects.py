"""Tests for services/subprojects.py — designation, linking, reconcile, cascade."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import project_manager as pm_mod
from services import subprojects as sp


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _stub_do_init(path, ide_name=None):
    """Cheap stand-in for cli.c3._do_init: just materialize .c3/config.json."""
    c3 = Path(path) / ".c3"
    c3.mkdir(parents=True, exist_ok=True)
    cfg = c3 / "config.json"
    if not cfg.exists():
        cfg.write_text("{}", encoding="utf-8")


class SubprojectBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.parent = self.base / "parent"
        (self.parent / ".c3").mkdir(parents=True)
        _write_json(self.parent / ".c3" / "config.json", {"meta": {"name": "parent"}})
        self.reg_file = self.base / "projects.json"
        _write_json(self.reg_file, {"projects": []})
        self._patches = [
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
            mock.patch("cli.c3._do_init", side_effect=_stub_do_init),
            mock.patch("cli.c3._uninstall_mcp_all", lambda *_a, **_k: None),
        ]
        self.init_mock = None
        started = [p.start() for p in self._patches]
        self.init_mock = started[2]

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _mgr(self) -> sp.SubprojectManager:
        return sp.SubprojectManager(str(self.parent))

    def _registry(self) -> list:
        return _read_json(self.reg_file)["projects"]

    def _parent_cfg(self) -> dict:
        return _read_json(self.parent / ".c3" / "config.json")

    def _make_child_dir(self, rel: str) -> Path:
        d = self.parent / rel
        d.mkdir(parents=True, exist_ok=True)
        return d


class TestAdd(SubprojectBase):
    def test_add_writes_all_three_link_stores(self):
        self._make_child_dir("services/api")
        res = self._mgr().add("services/api", reindex_parent=False)
        self.assertTrue(res["added"], res)
        self.assertFalse(res["adopted"])
        self.assertEqual(res["rel_path"], "services/api")  # POSIX-style

        entries = self._parent_cfg()["subprojects"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["rel_path"], "services/api")

        child_cfg = _read_json(self.parent / "services" / "api" / ".c3" / "config.json")
        self.assertEqual(Path(child_cfg["parent"]["path"]).resolve(),
                         self.parent.resolve())

        reg = self._registry()
        self.assertEqual(len(reg), 1)
        self.assertEqual(Path(reg[0]["parent_path"]).resolve(), self.parent.resolve())

    def test_add_rejects_nonexistent(self):
        res = self._mgr().add("no/such/dir", reindex_parent=False)
        self.assertFalse(res["added"])

    def test_add_links_outside_parent_as_external(self):
        # A sibling folder is not inside the parent and never could be
        # addressed by rel_path — since 2.96 it links by absolute path.
        outside = self.base / "outside"
        outside.mkdir()
        res = self._mgr().add(str(outside), reindex_parent=False)
        self.assertTrue(res["added"], res)
        self.assertEqual(res["link_kind"], sp.LINK_EXTERNAL)
        self.assertIsNone(res["rel_path"])

        entry = self._parent_cfg()["subprojects"][0]
        self.assertEqual(Path(entry["path"]).resolve(), outside.resolve())
        self.assertNotIn("rel_path", entry)
        self.assertEqual(entry["link"], sp.LINK_EXTERNAL)

    def test_add_rejects_parent_itself(self):
        res = self._mgr().add(str(self.parent), reindex_parent=False)
        self.assertFalse(res["added"])

    def test_add_allows_multi_level(self):
        # A project that is itself a sub-project may designate its own
        # children. This was refused outright before 2.96.
        cfg = self._parent_cfg()
        cfg["parent"] = {"name": "gp", "path": str(self.base)}
        _write_json(self.parent / ".c3" / "config.json", cfg)
        self._make_child_dir("sub1")
        res = self._mgr().add("sub1", reindex_parent=False)
        self.assertTrue(res["added"], res)
        self.assertEqual(res["depth"], 2)

    def test_add_duplicate_rejected(self):
        self._make_child_dir("sub1")
        self.assertTrue(self._mgr().add("sub1", reindex_parent=False)["added"])
        res = self._mgr().add("sub1", reindex_parent=False)
        self.assertFalse(res["added"])
        self.assertIn("already designated", res["error"])

    def test_adopt_existing_c3(self):
        child = self._make_child_dir("adopted")
        (child / ".c3").mkdir()
        _write_json(child / ".c3" / "config.json", {"meta": {"name": "adopted"}})
        calls_before = self.init_mock.call_count
        res = self._mgr().add("adopted", reindex_parent=False)
        self.assertTrue(res["added"])
        self.assertTrue(res["adopted"])
        self.assertEqual(self.init_mock.call_count, calls_before)  # no re-init

    def test_add_rejects_child_of_another_parent(self):
        child = self._make_child_dir("stolen")
        (child / ".c3").mkdir()
        _write_json(child / ".c3" / "config.json",
                    {"parent": {"name": "other", "path": str(self.base / "other")}})
        res = self._mgr().add("stolen", reindex_parent=False)
        self.assertFalse(res["added"])
        self.assertIn("already a sub-project of", res["error"])


class TestRemove(SubprojectBase):
    def _add(self, rel="sub1"):
        self._make_child_dir(rel)
        res = self._mgr().add(rel, reindex_parent=False)
        assert res["added"], res
        return res

    def test_remove_unlink_keeps_c3_and_registration(self):
        self._add("sub1")
        res = self._mgr().remove("sub1", mode="unlink", reindex_parent=False)
        self.assertTrue(res["removed"])
        self.assertNotIn("subprojects", self._parent_cfg())
        child_c3 = self.parent / "sub1" / ".c3"
        self.assertTrue(child_c3.is_dir())
        self.assertNotIn("parent", _read_json(child_c3 / "config.json"))
        reg = self._registry()
        self.assertEqual(len(reg), 1)  # still registered, now top-level
        self.assertNotIn("parent_path", reg[0])

    def test_remove_clear_wipes_c3_and_unregisters(self):
        self._add("sub2")
        res = self._mgr().remove("sub2", mode="clear", reindex_parent=False)
        self.assertTrue(res["removed"])
        self.assertFalse((self.parent / "sub2" / ".c3").exists())
        self.assertEqual(self._registry(), [])

    def test_remove_unknown_ref(self):
        res = self._mgr().remove("ghost", reindex_parent=False)
        self.assertFalse(res["removed"])

    def test_remove_by_rel_path(self):
        self._add("nested/deep")
        res = self._mgr().remove("nested/deep", mode="unlink", reindex_parent=False)
        self.assertTrue(res["removed"])


class TestReconcile(SubprojectBase):
    def _add(self, rel="sub1"):
        self._make_child_dir(rel)
        return self._mgr().add(rel, reindex_parent=False)

    def _statuses(self, report):
        return {c["rel_path"]: c["status"] for c in report["children"]}

    def test_all_ok(self):
        self._add("sub1")
        report = self._mgr().reconcile()
        self.assertTrue(report["ok"])
        self.assertEqual(self._statuses(report), {"sub1": "ok"})

    def test_missing_c3_reported_not_fixed(self):
        self._add("sub1")
        import shutil
        shutil.rmtree(self.parent / "sub1" / ".c3")
        report = self._mgr().reconcile(fix=True)
        self.assertEqual(self._statuses(report), {"sub1": "missing_c3"})
        self.assertFalse(report["ok"])

    def test_backlink_broken_fixed(self):
        self._add("sub1")
        cfg_path = self.parent / "sub1" / ".c3" / "config.json"
        cfg = _read_json(cfg_path)
        del cfg["parent"]
        _write_json(cfg_path, cfg)
        self.assertEqual(self._statuses(self._mgr().reconcile()), {"sub1": "backlink_broken"})
        report = self._mgr().reconcile(fix=True)
        self.assertEqual(self._statuses(report), {"sub1": "ok"})
        self.assertIn("parent", _read_json(cfg_path))

    def test_unregistered_fixed(self):
        self._add("sub1")
        _write_json(self.reg_file, {"projects": []})
        report = self._mgr().reconcile(fix=True)
        self.assertEqual(self._statuses(report), {"sub1": "ok"})
        self.assertEqual(len(self._registry()), 1)

    def test_missing_folder_pruned_only_with_fix(self):
        self._add("sub1")
        import shutil
        shutil.rmtree(self.parent / "sub1")
        report = self._mgr().reconcile()  # report-only
        self.assertEqual(self._statuses(report), {"sub1": "missing_folder"})
        report = self._mgr().reconcile(fix=True, prune=True)
        self.assertEqual(len(report["pruned"]), 1)
        self.assertNotIn("subprojects", self._parent_cfg())

    def test_registry_orphan_cleared(self):
        stray = self.base / "parent" / "stray"
        (stray / ".c3").mkdir(parents=True)
        _write_json(self.reg_file, {"projects": [{
            "name": "stray", "path": str(stray.resolve()),
            "parent_path": str(self.parent.resolve()),
        }]})
        report = self._mgr().reconcile()
        self.assertEqual(len(report["orphans"]), 1)
        self._mgr().reconcile(fix=True)
        self.assertNotIn("parent_path", self._registry()[0])


class TestCascade(SubprojectBase):
    def test_health_aggregates(self):
        self._make_child_dir("sub1")
        self._mgr().add("sub1", reindex_parent=False)
        with mock.patch("cli.c3._check_c3_health",
                        return_value={"healthy": True, "issues": []}):
            res = self._mgr().cascade("health")
        self.assertEqual(res["summary"], {"total": 1, "ok": 1, "failed": 0})
        self.assertIn("elapsed_ms", res["results"][0])

    def test_invalid_op(self):
        res = self._mgr().cascade("explode")
        self.assertIn("invalid op", res["error"])

    def test_missing_c3_skipped_as_failed_row(self):
        self._make_child_dir("sub1")
        self._mgr().add("sub1", reindex_parent=False)
        import shutil
        shutil.rmtree(self.parent / "sub1" / ".c3")
        with mock.patch("cli.c3._check_c3_health") as health:
            res = self._mgr().cascade("health")
        health.assert_not_called()
        self.assertEqual(res["results"][0]["error"], "missing_c3")
        self.assertEqual(res["summary"]["failed"], 1)


class TestHelpers(unittest.TestCase):
    def test_is_excluded_prefix_not_name(self):
        prefixes = [("services", "api")]
        self.assertTrue(sp.is_excluded(("services", "api", "x.py"), prefixes))
        self.assertTrue(sp.is_excluded(("Services", "API", "x.py"), prefixes))
        self.assertFalse(sp.is_excluded(("api", "x.py"), prefixes))       # root sibling
        self.assertFalse(sp.is_excluded(("services2", "api", "y.py"), prefixes))
        self.assertFalse(sp.is_excluded(("services",), prefixes))

    def test_exclusion_prefixes_fast_path(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(sp.exclusion_prefixes(td), [])

    def test_is_within_strict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a" / "b").mkdir(parents=True)
            self.assertTrue(sp.is_within(root / "a" / "b", root))
            self.assertFalse(sp.is_within(root, root))
            self.assertFalse(sp.is_within(root, root / "a"))

    @unittest.skipUnless(sys.platform == "win32", "case-insensitive paths are Windows behavior")
    def test_same_path_case_insensitive(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(sp._same_path(td, td.upper()))

class TestExternalLinkage(SubprojectBase):
    """Linking a project that does not live inside the parent (2.96+)."""

    def _external(self, name="sibling") -> Path:
        d = self.base / name
        (d / ".c3").mkdir(parents=True)
        _write_json(d / ".c3" / "config.json", {"meta": {"name": name}})
        return d

    def test_validate_reports_external_kind(self):
        ext = self._external()
        v = self._mgr().validate(str(ext))
        self.assertTrue(v["ok"], v["warnings"])
        self.assertEqual(v["link_kind"], sp.LINK_EXTERNAL)
        self.assertIsNone(v["rel_path"])
        self.assertFalse(v["would_create_cycle"])

    def test_nested_still_uses_rel_path(self):
        self._make_child_dir("services/api")
        v = self._mgr().validate("services/api")
        self.assertEqual(v["link_kind"], sp.LINK_NESTED)
        self.assertEqual(v["rel_path"], "services/api")

    def test_external_child_resolves_and_lists(self):
        ext = self._external()
        self._mgr().add(str(ext), reindex_parent=False)
        rows = self._mgr().list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(Path(rows[0]["path"]).resolve(), ext.resolve())
        self.assertEqual(rows[0]["link_kind"], sp.LINK_EXTERNAL)
        self.assertEqual(rows[0]["status"], "ok")

    def test_external_child_is_not_an_index_exclusion(self):
        # It was never inside the parent's scan tree, so there is nothing to
        # carve out -- a nested child, by contrast, still produces a prefix.
        ext = self._external()
        self._mgr().add(str(ext), reindex_parent=False)
        self.assertEqual(sp.exclusion_prefixes(str(self.parent)), [])

        self._make_child_dir("services/api")
        self._mgr().add("services/api", reindex_parent=False)
        self.assertEqual(sp.exclusion_prefixes(str(self.parent)),
                         [("services", "api")])

    def test_external_child_is_not_a_repo_map_boundary(self):
        from services.repo_map import RepoMapService
        ext = self._external()
        self._mgr().add(str(ext), reindex_parent=False)
        self.assertEqual(RepoMapService(str(self.parent))._subproject_boundaries(), [])

        # A nested child, by contrast, IS a boundary in the rendered tree.
        self._make_child_dir("services/api")
        self._mgr().add("services/api", reindex_parent=False)
        self.assertEqual(RepoMapService(str(self.parent))._subproject_boundaries(),
                         ["services/api"])

    def test_backlink_omits_rel_path_across_drives(self):
        # os.path.relpath raises on Windows for paths on different drives.
        # The link must still be created; only rel_path is dropped.
        ext = self._external()
        with mock.patch("services.subprojects.os.path.relpath",
                        side_effect=ValueError("different drives")):
            res = self._mgr().add(str(ext), reindex_parent=False)
        self.assertTrue(res["added"], res)
        back = _read_json(ext / ".c3" / "config.json")["parent"]
        self.assertNotIn("rel_path", back)
        self.assertEqual(Path(back["path"]).resolve(), self.parent.resolve())

    def test_external_child_resolvable_by_ref_and_removable(self):
        ext = self._external()
        self._mgr().add(str(ext), reindex_parent=False)
        res = self._mgr().remove(str(ext), mode="unlink", reindex_parent=False)
        self.assertTrue(res["removed"], res)
        self.assertEqual(res["link_kind"], sp.LINK_EXTERNAL)
        self.assertEqual(self._mgr().list(), [])
        self.assertNotIn("parent", _read_json(ext / ".c3" / "config.json"))

    def test_external_link_skips_parent_reindex(self):
        # Nothing about the parent's scan tree changed, so rebuilding its
        # index would be pure waste.
        ext = self._external()
        with mock.patch.object(sp.SubprojectManager, "_reindex_parent") as rx:
            self._mgr().add(str(ext), reindex_parent=True)
        rx.assert_not_called()

    def test_reconcile_repairs_external_backlink(self):
        ext = self._external()
        self._mgr().add(str(ext), reindex_parent=False)
        cfg = _read_json(ext / ".c3" / "config.json")
        cfg.pop("parent")
        _write_json(ext / ".c3" / "config.json", cfg)

        self.assertEqual(self._mgr().list()[0]["status"], "backlink_broken")
        rep = self._mgr().reconcile(fix=True)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(self._mgr().list()[0]["status"], "ok")


class TestHierarchyDepth(SubprojectBase):
    """Multi-level trees: A -> B -> C and deeper (2.96+)."""

    def _chain(self, *names) -> list:
        """Build parent -> names[0] -> names[1] -> ... as external links."""
        made, prev = [], self.parent
        for name in names:
            d = self.base / name
            (d / ".c3").mkdir(parents=True)
            _write_json(d / ".c3" / "config.json", {"meta": {"name": name}})
            res = sp.SubprojectManager(str(prev)).add(str(d), reindex_parent=False)
            self.assertTrue(res["added"], res)
            made.append(d)
            prev = d
        return made

    def test_three_level_tree(self):
        a, b = self._chain("a", "b")
        tree = self._mgr().tree(depth=3)
        self.assertEqual(len(tree["children"]), 1)
        child_a = tree["children"][0]
        self.assertEqual(Path(child_a["path"]).resolve(), a.resolve())
        self.assertEqual(len(child_a["children"]), 1)
        self.assertEqual(Path(child_a["children"][0]["path"]).resolve(), b.resolve())
        # Rollup counts the whole subtree, not just the first hop.
        self.assertEqual(tree["rollup"]["children"], 2)
        self.assertEqual(tree["rollup"]["direct_children"], 1)

    def test_tree_depth_1_is_the_legacy_shape(self):
        self._chain("a", "b")
        tree = self._mgr().tree(depth=1)
        self.assertEqual(len(tree["children"]), 1)
        self.assertEqual(tree["children"][0]["children"], [])

    def test_descendants_are_flat_and_deduped(self):
        a, b = self._chain("a", "b")
        rows = self._mgr().descendants()
        self.assertEqual([Path(r["path"]).resolve() for r in rows],
                         [a.resolve(), b.resolve()])

    def test_ancestors_and_depth(self):
        a, b = self._chain("a", "b")
        chain = sp.ancestors(str(b))
        self.assertEqual([Path(c["path"]).resolve() for c in chain],
                         [a.resolve(), self.parent.resolve()])
        self.assertEqual(sp.depth_of(str(b)), 2)
        self.assertEqual(sp.depth_of(str(self.parent)), 0)

    def test_cascade_reaches_the_grandchild(self):
        a, b = self._chain("a", "b")
        with mock.patch("cli.c3._check_c3_health", return_value={"healthy": True}):
            res = self._mgr().cascade("health")
        touched = {Path(r["path"]).resolve() for r in res["results"]}
        self.assertEqual(touched, {a.resolve(), b.resolve()})
        self.assertEqual(res["summary"]["ok"], 2)

    def test_cascade_depth_1_stops_at_direct_children(self):
        a, _b = self._chain("a", "b")
        with mock.patch("cli.c3._check_c3_health", return_value={"healthy": True}):
            res = self._mgr().cascade("health", depth=1)
        self.assertEqual([Path(r["path"]).resolve() for r in res["results"]],
                         [a.resolve()])

    def test_cycle_is_rejected(self):
        _a, b = self._chain("a", "b")
        # b is a descendant of parent; linking parent under b closes a loop.
        v = sp.SubprojectManager(str(b)).validate(str(self.parent))
        self.assertFalse(v["ok"])
        self.assertTrue(v["would_create_cycle"])
        self.assertIn("cycle", " ".join(v["warnings"]))
        self.assertFalse(
            sp.SubprojectManager(str(b)).add(str(self.parent), reindex_parent=False)["added"])

    def test_self_link_is_rejected(self):
        v = self._mgr().validate(str(self.parent))
        self.assertFalse(v["ok"])
        self.assertTrue(v["would_create_cycle"])

    def test_ancestor_folder_is_rejected(self):
        # self.base physically contains the parent.
        v = self._mgr().validate(str(self.base))
        self.assertFalse(v["ok"])
        self.assertTrue(v["is_ancestor"])
        self.assertTrue(v["would_create_cycle"])

    def test_depth_cap_refuses_the_next_level(self):
        # A full-depth chain is legal: parent(0) -> n0(1) -> ... -> n7(MAX_DEPTH).
        made = self._chain(*[f"n{i}" for i in range(sp.MAX_DEPTH)])
        deepest = made[-1]
        self.assertEqual(sp.depth_of(str(deepest)), sp.MAX_DEPTH)

        # One more level is not.
        extra = self.base / "too_deep"
        (extra / ".c3").mkdir(parents=True)
        _write_json(extra / ".c3" / "config.json", {})
        v = sp.SubprojectManager(str(deepest)).validate(str(extra))
        self.assertFalse(v["ok"])
        self.assertIn(f"max {sp.MAX_DEPTH}", " ".join(v["warnings"]))

    def test_linking_a_deep_subtree_counts_its_own_depth(self):
        # A candidate that already carries descendants is measured whole.
        a, b = self._chain("a", "b")
        self.assertEqual(sp.subtree_depth(str(self.parent)), 2)
        self.assertEqual(sp.subtree_depth(str(a)), 1)
        self.assertEqual(sp.subtree_depth(str(b)), 0)


class TestBackwardCompatibility(SubprojectBase):
    """A pre-2.96 config carries only rel_path and must keep working."""

    def test_legacy_rel_path_entry_lists_and_reconciles(self):
        child = self._make_child_dir("legacy")
        (child / ".c3").mkdir()
        _write_json(child / ".c3" / "config.json",
                    {"parent": {"name": "parent", "path": str(self.parent),
                                "rel_path": ".."}})
        cfg = self._parent_cfg()
        cfg["subprojects"] = [{"name": "legacy", "rel_path": "legacy",
                               "added_at": "2026-01-01T00:00:00Z"}]
        _write_json(self.parent / ".c3" / "config.json", cfg)
        pm_mod.ProjectManager().add_project(str(child), name="legacy",
                                            parent_path=str(self.parent))

        rows = self._mgr().list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["link_kind"], sp.LINK_NESTED)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(Path(rows[0]["path"]).resolve(), child.resolve())
        self.assertTrue(self._mgr().reconcile()["ok"])

    def test_entry_abs_path_accepts_a_bare_rel_path_string(self):
        self.assertEqual(
            sp.entry_abs_path(str(self.parent), "services/api"),
            (self.parent / "services" / "api").resolve(),
        )


class TestInspectPath(SubprojectBase):
    def test_missing_folder(self):
        rep = sp.inspect_path(str(self.base / "nope"))
        self.assertFalse(rep["is_dir"])
        self.assertFalse(rep["linkable"])

    def test_plain_folder_is_linkable_and_reports_no_c3(self):
        plain = self.base / "plain"
        plain.mkdir()
        rep = sp.inspect_path(str(plain))
        self.assertTrue(rep["is_dir"])
        self.assertFalse(rep["has_c3"])
        self.assertTrue(rep["linkable"])
        self.assertIsNone(rep["project"])
        self.assertIn("no .c3", " ".join(rep["warnings"]))

    def test_unregistered_c3_project_reports_identity_without_registering(self):
        proj = self.base / "solo"
        (proj / ".c3").mkdir(parents=True)
        _write_json(proj / ".c3" / "config.json",
                    {"meta": {"name": "Solo"}, "version": "2.95.0", "ide": "vscode"})
        rep = sp.inspect_path(str(proj))
        self.assertTrue(rep["has_c3"])
        self.assertFalse(rep["registered"])
        self.assertEqual(rep["project"]["name"], "Solo")
        self.assertEqual(rep["project"]["c3_version"], "2.95.0")
        self.assertTrue(rep["linkable"])
        # Read-only: the registry must be untouched.
        self.assertEqual(self._registry(), [])

    def test_linked_child_reports_its_parent_chain(self):
        child = self.base / "kid"
        (child / ".c3").mkdir(parents=True)
        _write_json(child / ".c3" / "config.json", {"meta": {"name": "kid"}})
        self._mgr().add(str(child), reindex_parent=False)

        rep = sp.inspect_path(str(child))
        self.assertTrue(rep["registered"])
        self.assertEqual(Path(rep["parent"]["path"]).resolve(), self.parent.resolve())
        self.assertEqual(rep["depth"], 1)
        self.assertFalse(rep["linkable"])  # already claimed

    def test_reports_own_children(self):
        a, b = self.base / "a", self.base / "b"
        for d in (a, b):
            (d / ".c3").mkdir(parents=True)
            _write_json(d / ".c3" / "config.json", {})
        self._mgr().add(str(a), reindex_parent=False)
        sp.SubprojectManager(str(a)).add(str(b), reindex_parent=False)

        rep = sp.inspect_path(str(a))
        self.assertEqual(len(rep["children"]), 1)
        self.assertEqual(Path(rep["children"][0]["path"]).resolve(), b.resolve())

    def test_detects_unlinked_nested_projects_as_suggestions(self):
        nested = self.parent / "vendored"
        (nested / ".c3").mkdir(parents=True)
        _write_json(nested / ".c3" / "config.json", {})
        rep = sp.inspect_path(str(self.parent))
        found = {Path(d["path"]).resolve() for d in rep["detected"]}
        self.assertIn(nested.resolve(), found)
        self.assertFalse(rep["detected"][0]["linked"])
        # Detection is a suggestion, never an action.
        self.assertEqual(self._mgr().list(), [])

    def test_detected_excludes_already_linked_children(self):
        nested = self.parent / "vendored"
        (nested / ".c3").mkdir(parents=True)
        _write_json(nested / ".c3" / "config.json", {})
        self._mgr().add("vendored", reindex_parent=False)
        rep = sp.inspect_path(str(self.parent))
        self.assertEqual(rep["detected"], [])


class TestSetParentCycleGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.reg_file = self.base / "projects.json"
        _write_json(self.reg_file, {"projects": []})
        self._patches = [
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
        ]
        for p in self._patches:
            p.start()
        self.pm = pm_mod.ProjectManager()
        self.a, self.b, self.c = (self.base / n for n in "abc")
        for d in (self.a, self.b, self.c):
            (d / ".c3").mkdir(parents=True)
            self.pm.add_project(str(d))

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_rejects_self_parent(self):
        self.assertFalse(self.pm.set_parent(str(self.a), str(self.a)))

    def test_rejects_descendant_as_parent(self):
        self.assertTrue(self.pm.set_parent(str(self.b), str(self.a)))   # a -> b
        self.assertTrue(self.pm.set_parent(str(self.c), str(self.b)))   # b -> c
        # Making a a child of c would close the loop a -> b -> c -> a.
        self.assertFalse(self.pm.set_parent(str(self.a), str(self.c)))

    def test_allows_a_legitimate_deep_link(self):
        self.assertTrue(self.pm.set_parent(str(self.b), str(self.a)))
        self.assertTrue(self.pm.set_parent(str(self.c), str(self.b)))
        reg = {p["path"]: p.get("parent_path") for p in self.pm._read_projects()}
        self.assertEqual(Path(reg[str(self.c.resolve())]).resolve(), self.b.resolve())


if __name__ == "__main__":
    unittest.main()
