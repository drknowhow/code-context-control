"""Tests for Oracle sub-project awareness (Wave 2).

ProjectScanner._enrich must surface the v2.44 parent/child hierarchy
(registry parent_path authoritative, child config back-link as fallback,
broken links degrading to top-level), and C3Bridge._scoped_projects must
resolve the cross-tool scope param ('' | 'all' | 'top' | name/path).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from oracle.services.c3_bridge import C3Bridge  # noqa: E402
from oracle.services.project_scanner import ProjectScanner  # noqa: E402


def _mk(root: Path, rel: str, config: dict | None = None) -> str:
    d = root / rel
    (d / ".c3").mkdir(parents=True, exist_ok=True)
    if config is not None:
        (d / ".c3" / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return str(d.resolve())


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.parent = _mk(root, "parent", {
            "subprojects": [{"name": "child", "rel_path": "child"}],
        })
        self.child = _mk(root, "parent/child", {
            "parent": {"name": "parent", "path": self.parent, "rel_path": ".."},
        })
        self.solo = _mk(root, "solo", {})


class TestScannerEnrichment(_Fixture):
    def setUp(self):
        super().setUp()
        self.scanner = ProjectScanner()

    def test_parent_lists_subprojects(self):
        p = self.scanner._enrich({"path": self.parent})
        self.assertFalse(p["is_subproject"])
        self.assertEqual(p["parent_path"], "")
        self.assertEqual(p["subproject_rel_paths"], ["child"])
        self.assertEqual(p["subproject_count"], 1)

    def test_child_via_registry_parent_path(self):
        p = self.scanner._enrich({"path": self.child, "parent_path": self.parent})
        self.assertTrue(p["is_subproject"])
        self.assertEqual(p["parent_path"], self.parent)

    def test_child_via_backlink_fallback(self):
        # Registry has no parent_path (stale) — the config back-link covers it.
        p = self.scanner._enrich({"path": self.child})
        self.assertTrue(p["is_subproject"])
        self.assertEqual(p["parent_path"], self.parent)

    def test_broken_backlink_degrades_to_top_level(self):
        broken = _mk(Path(self.tmp.name), "broken", {"parent": "not-a-dict"})
        p = self.scanner._enrich({"path": broken})
        self.assertFalse(p["is_subproject"])
        self.assertEqual(p["parent_path"], "")

    def test_solo_project_has_empty_hierarchy(self):
        p = self.scanner._enrich({"path": self.solo})
        self.assertFalse(p["is_subproject"])
        self.assertEqual(p["subproject_count"], 0)


class _StubScanner:
    def __init__(self, projects):
        self._projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self._projects]


class TestScopedProjects(_Fixture):
    def setUp(self):
        super().setUp()
        self.projects = [
            {"path": self.parent, "has_c3": True, "is_subproject": False,
             "parent_path": ""},
            {"path": self.child, "has_c3": True, "is_subproject": True,
             "parent_path": self.parent},
            {"path": self.solo, "has_c3": True, "is_subproject": False,
             "parent_path": ""},
        ]
        self.bridge = C3Bridge(scanner=_StubScanner(self.projects))

    def _paths(self, scope):
        return sorted(p["path"] for p in self.bridge._scoped_projects(scope))

    def test_empty_and_all_return_everything(self):
        everything = sorted([self.parent, self.child, self.solo])
        self.assertEqual(self._paths(""), everything)
        self.assertEqual(self._paths("all"), everything)

    def test_top_excludes_subprojects(self):
        self.assertEqual(self._paths("top"), sorted([self.parent, self.solo]))

    def test_parent_scope_includes_children(self):
        self.assertEqual(self._paths(self.parent), sorted([self.parent, self.child]))

    def test_child_scope_is_just_the_child(self):
        self.assertEqual(self._paths(self.child), [self.child])

    def test_unknown_scope_raises(self):
        with self.assertRaises(ValueError):
            self.bridge._scoped_projects("no-such-project-zzz")

    def test_cross_search_result_carries_scope(self):
        # c3_search per project would build runtimes; stub it out.
        self.bridge.c3_search = lambda path, q, a, k: {"result": f"hit:{path}"}
        out = self.bridge.c3_search_cross("q", scope="top")
        self.assertEqual(out["scope"], "top")
        self.assertEqual(out["projects_queried"], 2)


if __name__ == "__main__":
    unittest.main()
