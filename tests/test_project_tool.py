"""Tests for the cross-project tooling: resolver, discovery, runtime cache,
and the c3_project dispatcher (read ops + write guard)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cli.tools import project as tool
from services import project_runtime as pr


def _make_c3_project(parent: Path, name: str) -> Path:
    root = parent / name
    (root / ".c3").mkdir(parents=True)
    return root


def _write_registry(path: Path, projects: list[dict]) -> None:
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")


def _captured_finalize():
    seen: list[tuple] = []

    def finalize(name, args, resp, summary, **kw):
        seen.append((name, dict(args), resp, summary))
        return resp

    return finalize, seen


class _StubActivity:
    def __init__(self):
        self.events: list[tuple] = []

    def log(self, event_type, data):
        self.events.append((event_type, data))


class _StubSvc:
    def __init__(self, project_path: str):
        self.project_path = project_path
        self.activity_log = _StubActivity()


# ── Resolver ───────────────────────────────────────────────────────────────


class TestResolveProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.reg_file = self.base / "projects.json"
        self.proj = _make_c3_project(self.base, "Alpha Service")
        _write_registry(self.reg_file, [
            {"name": "Alpha Service", "path": str(self.proj), "ide": "claude-code"},
            {"name": "Beta", "path": str(self.base / "beta_missing")},
        ])
        self._patch = mock.patch.object(pr, "_PROJECTS_FILE", self.reg_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_resolve_by_path(self):
        out = pr.resolve_project(str(self.proj))
        self.assertEqual(out["path"], str(self.proj.resolve()))
        self.assertEqual(out["name"], "Alpha Service")

    def test_resolve_by_exact_name_normalized(self):
        # Different case + spacing still matches.
        out = pr.resolve_project("alphaservice")
        self.assertEqual(out["path"], str(self.proj.resolve()))

    def test_resolve_by_unique_substring(self):
        out = pr.resolve_project("alpha")
        self.assertEqual(out["name"], "Alpha Service")

    def test_unknown_raises_with_registry_listing(self):
        with self.assertRaises(ValueError) as cm:
            pr.resolve_project("does-not-exist")
        self.assertIn("Unknown project", str(cm.exception))
        self.assertIn("Alpha Service", str(cm.exception))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            pr.resolve_project("   ")


# ── Discovery ──────────────────────────────────────────────────────────────


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.reg_file = self.base / "projects.json"
        self.registered = _make_c3_project(self.base, "registered")
        self.stray = _make_c3_project(self.base, "stray")  # not in registry
        _make_c3_project(self.base, "deep_skip")  # also stray; both should appear
        _write_registry(self.reg_file, [
            {"name": "registered", "path": str(self.registered), "ide": "claude-code"},
        ])
        self._patch = mock.patch.object(pr, "_PROJECTS_FILE", self.reg_file)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_scan_finds_c3_dirs(self):
        found = pr.scan_for_c3([str(self.base)])
        self.assertIn(str(self.registered.resolve()), found)
        self.assertIn(str(self.stray.resolve()), found)

    def test_scan_skips_noise_dirs(self):
        noisy = self.base / "node_modules"
        (noisy / "pkg" / ".c3").mkdir(parents=True)
        found = pr.scan_for_c3([str(self.base)])
        self.assertNotIn(str((noisy / "pkg").resolve()), found)

    def test_discover_splits_registered_and_unregistered(self):
        data = pr.discover_projects(scan_roots=[str(self.base)], scan=True)
        reg_paths = {p["path"] for p in data["registered"]}
        unreg_paths = {p["path"] for p in data["unregistered"]}
        self.assertIn(str(self.registered.resolve()), reg_paths)
        self.assertIn(str(self.stray.resolve()), unreg_paths)
        self.assertNotIn(str(self.registered.resolve()), unreg_paths)

    def test_discover_no_scan_returns_only_registered(self):
        data = pr.discover_projects(scan=False)
        self.assertEqual(data["unregistered"], [])
        self.assertEqual(len(data["registered"]), 1)


# ── Runtime cache ──────────────────────────────────────────────────────────


class TestRuntimeCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_path_raises(self):
        cache = pr.ProjectRuntimeCache()
        with self.assertRaises(ValueError):
            cache.get(str(self.base / "nope"))

    def test_no_c3_dir_raises(self):
        plain = self.base / "plain"
        plain.mkdir()
        cache = pr.ProjectRuntimeCache()
        with self.assertRaises(ValueError) as cm:
            cache.get(str(plain))
        self.assertIn(".c3", str(cm.exception))

    def test_caches_and_evicts_lru(self):
        p1 = _make_c3_project(self.base, "p1")
        p2 = _make_c3_project(self.base, "p2")
        p3 = _make_c3_project(self.base, "p3")
        built: list[str] = []
        stopped: list[object] = []

        def fake_build(path, ide_name=None):
            built.append(path)
            return f"RT::{path}"

        with mock.patch.object(pr, "build_runtime", side_effect=fake_build), \
             mock.patch.object(pr, "stop_runtime", side_effect=stopped.append):
            cache = pr.ProjectRuntimeCache(max_cached=2)
            rt1 = cache.get(str(p1))
            cache.get(str(p2))
            # Re-access p1 so p2 becomes the LRU victim.
            self.assertIs(cache.get(str(p1)), rt1)
            cache.get(str(p3))  # evicts p2
            # p2 was stopped on eviction; p1 stayed cached (no rebuild).
            self.assertEqual(stopped, ["RT::" + str(p2.resolve())])
            self.assertEqual(built.count(str(p1.resolve())), 1)


# ── Dispatcher: discovery + read + write guard ─────────────────────────────


class TestHandleProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.reg_file = self.base / "projects.json"
        self.proj = _make_c3_project(self.base, "Target")
        _write_registry(self.reg_file, [
            {"name": "Target", "path": str(self.proj), "ide": "claude-code"},
        ])
        # Isolate BOTH the resolver's registry view (project_runtime) and the
        # writer's registry (project_manager) onto the same temp file, so
        # register/unregister never touch the real ~/.c3/projects.json.
        from services import project_manager as pm
        self._patches = [
            mock.patch.object(pr, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm, "_GLOBAL_C3_DIR", self.base),
        ]
        for p in self._patches:
            p.start()
        self.home = _StubSvc(str(self.base / "home"))

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_list_shows_registered(self):
        finalize, _ = _captured_finalize()
        resp = tool.handle_project("list", self.home, finalize)
        self.assertIn("Target", resp)
        self.assertIn("Registered C3 projects", resp)

    def test_unknown_action_errors(self):
        finalize, _ = _captured_finalize()
        resp = tool.handle_project("frobnicate", self.home, finalize)
        self.assertIn("Unknown action", resp)

    def test_register_then_unregister(self):
        finalize, _ = _captured_finalize()
        new = _make_c3_project(self.base, "Fresh")
        reg = tool.handle_project("register", self.home, finalize, project=str(new))
        self.assertIn("Registered:", reg)
        # It is now in the registry file.
        data = json.loads(self.reg_file.read_text())
        self.assertTrue(any(p["path"] == str(new.resolve()) for p in data["projects"]))

    def test_read_op_proxies_to_handler(self):
        fsvc = _StubSvc(str(self.proj))
        finalize, _ = _captured_finalize()
        with mock.patch.object(tool, "_runtime_for", return_value=fsvc), \
             mock.patch("cli.tools.status.handle_status",
                        return_value="STATUS-OK") as hs:
            resp = tool.handle_project("status", self.home, finalize,
                                       project="Target", view="health")
        hs.assert_called_once()
        self.assertIn("STATUS-OK", resp)
        self.assertIn("[c3_project:Target]", resp)

    def test_write_blocked_without_allow_write(self):
        sentinel = mock.Mock(side_effect=AssertionError("should not build runtime"))
        finalize, _ = _captured_finalize()
        with mock.patch.object(tool, "_runtime_for", sentinel):
            resp = tool.handle_project("edit", self.home, finalize,
                                       project="Target", file_path="x.py",
                                       old_string="a", new_string="b")
        self.assertIn("[c3_project:blocked]", resp)
        sentinel.assert_not_called()

    def test_write_allowed_with_flag_and_audited(self):
        fsvc = _StubSvc(str(self.proj))
        finalize, _ = _captured_finalize()
        with mock.patch.object(tool, "_runtime_for", return_value=fsvc), \
             mock.patch("cli.tools.edit.handle_edit",
                        return_value="EDITED") as he:
            resp = tool.handle_project("edit", self.home, finalize,
                                       project="Target", file_path="x.py",
                                       old_string="a", new_string="b",
                                       allow_write=True)
        he.assert_called_once()
        self.assertIn("EDITED", resp)
        # The foreign project recorded the cross-project mutation.
        self.assertTrue(
            any(ev == "cross_project_write" for ev, _ in fsvc.activity_log.events)
        )

    def test_memory_write_subaction_blocked_without_flag(self):
        sentinel = mock.Mock(side_effect=AssertionError("should not build runtime"))
        finalize, _ = _captured_finalize()
        with mock.patch.object(tool, "_runtime_for", sentinel):
            resp = tool.handle_project("memory", self.home, finalize,
                                       project="Target", mem_action="add",
                                       fact="x", category="general")
        self.assertIn("[c3_project:blocked]", resp)
        sentinel.assert_not_called()


# ── Sub-project ops (v2.44.0) ──────────────────────────────────────────────


class TestSubprojectOps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.reg_file = self.base / "projects.json"
        self.parent = _make_c3_project(self.base, "parent")
        (self.parent / ".c3" / "config.json").write_text(json.dumps({
            "meta": {"name": "parent"},
            "subprojects": [{"name": "api", "rel_path": "api", "added_at": "x"}],
        }), encoding="utf-8")
        child = self.parent / "api"
        (child / ".c3").mkdir(parents=True)
        (child / ".c3" / "config.json").write_text(json.dumps(
            {"parent": {"name": "parent", "path": str(self.parent.resolve())}}),
            encoding="utf-8")
        _write_registry(self.reg_file, [
            {"name": "parent", "path": str(self.parent)},
            {"name": "api", "path": str(child),
             "parent_path": str(self.parent.resolve())},
        ])
        from services import project_manager as pm_mod
        self._patches = [
            mock.patch.object(pr, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
        ]
        for p in self._patches:
            p.start()
        self.home = _StubSvc(str(self.base / "home"))

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_subprojects_tree_renders(self):
        finalize, _ = _captured_finalize()
        resp = tool.handle_project("subprojects", self.home, finalize, project="parent")
        self.assertIn("Sub-projects of parent", resp)
        self.assertIn("api", resp)
        self.assertIn("rollup:", resp)

    def test_sub_add_blocked_without_allow_write(self):
        finalize, _ = _captured_finalize()
        resp = tool.handle_project("sub_add", self.home, finalize,
                                   project="parent", target="newsub")
        self.assertIn("[c3_project:blocked]", resp)

    def test_sub_remove_blocked_without_allow_write(self):
        finalize, _ = _captured_finalize()
        resp = tool.handle_project("sub_remove", self.home, finalize,
                                   project="parent", target="api")
        self.assertIn("[c3_project:blocked]", resp)

    def test_sub_cascade_update_blocked_without_allow_write(self):
        finalize, _ = _captured_finalize()
        resp = tool.handle_project("sub_cascade", self.home, finalize,
                                   project="parent", mode="update")
        self.assertIn("[c3_project:blocked]", resp)

    def test_sub_cascade_health_is_read_only(self):
        finalize, _ = _captured_finalize()
        with mock.patch("cli.c3._check_c3_health",
                        return_value={"healthy": True, "issues": []}):
            resp = tool.handle_project("sub_cascade", self.home, finalize,
                                       project="parent", mode="health")
        self.assertIn("1/1 ok", resp)

    def test_sub_remove_unlink_with_flag(self):
        finalize, _ = _captured_finalize()
        with mock.patch("services.subprojects.SubprojectManager._reindex_parent",
                        return_value={}):
            resp = tool.handle_project("sub_remove", self.home, finalize,
                                       project="parent", target="api",
                                       allow_write=True)
        self.assertIn("Unlinked", resp)
        cfg = json.loads((self.parent / ".c3" / "config.json").read_text())
        self.assertNotIn("subprojects", cfg)


if __name__ == "__main__":
    unittest.main()
