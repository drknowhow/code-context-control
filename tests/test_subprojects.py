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

    def test_add_rejects_outside_parent(self):
        outside = self.base / "outside"
        outside.mkdir()
        res = self._mgr().add(str(outside), reindex_parent=False)
        self.assertFalse(res["added"])

    def test_add_rejects_parent_itself(self):
        res = self._mgr().add(str(self.parent), reindex_parent=False)
        self.assertFalse(res["added"])

    def test_add_rejects_nesting_depth(self):
        # Parent that is itself a sub-project cannot designate children.
        cfg = self._parent_cfg()
        cfg["parent"] = {"name": "gp", "path": str(self.base)}
        _write_json(self.parent / ".c3" / "config.json", cfg)
        self._make_child_dir("sub1")
        res = self._mgr().add("sub1", reindex_parent=False)
        self.assertFalse(res["added"])
        self.assertIn("depth", res["error"])

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


if __name__ == "__main__":
    unittest.main()
