"""Hub /api/projects sub-project link annotation — _annotate_subproject_links.

Covers the per-parent link-health rollup added to cli/hub_server.py:
- healthy three-way link -> no subproject_issues, child link_status == 'ok'
- broken/missing child back-link -> backlink_broken counted
- registry rows claiming a parent with no config entry -> orphan counted
- deleted child folder -> missing_folder counted
- endpoint-level: fields flow through GET /api/projects (Flask test client)

Fixture conventions follow tests/test_subprojects.py (temp dirs, .c3 config
writing, pm_mod._PROJECTS_FILE/_REGISTRY_FILE patching) and
tests/test_hub_inspect_api.py (hub_server.app.test_client()).
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


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class LinkBase(unittest.TestCase):
    """Temp parent project + patched global registry, per the three-way link:

    1. parent .c3/config.json  -> subprojects[] entries (POSIX rel_path)
    2. child  .c3/config.json  -> parent{} back-link
    3. ~/.c3/projects.json row -> parent_path
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.parent = self.base / "parent"
        (self.parent / ".c3").mkdir(parents=True)
        self._write_parent_cfg([])
        self.reg_file = self.base / "projects.json"
        _write_json(self.reg_file, {"projects": []})
        self._patches = [
            mock.patch.object(pm_mod, "_PROJECTS_FILE", self.reg_file),
            mock.patch.object(pm_mod, "_REGISTRY_FILE", self.base / "registry.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    # ── fixture helpers ────────────────────────────────────────────

    def _write_parent_cfg(self, entries: list) -> None:
        _write_json(self.parent / ".c3" / "config.json",
                    {"meta": {"name": "parent"}, "subprojects": entries})

    def _entry(self, rel: str = "child") -> dict:
        return {"name": rel, "rel_path": rel, "added_at": "2026-07-24T00:00:00Z"}

    def _make_child(self, rel: str = "child", backlink: bool = True,
                    backlink_path: str = None) -> Path:
        """Create the child folder + .c3 config, optionally with the back-link."""
        d = self.parent / rel
        (d / ".c3").mkdir(parents=True)
        cfg = {"meta": {"name": rel}}
        if backlink:
            cfg["parent"] = {"path": backlink_path or str(self.parent),
                             "name": "parent"}
        _write_json(d / ".c3" / "config.json", cfg)
        return d

    def _register(self, *rows: dict) -> None:
        _write_json(self.reg_file, {"projects": list(rows)})

    def _parent_reg_row(self) -> dict:
        return {"name": "parent", "path": str(self.parent), "ide": "claude-code"}

    def _child_reg_row(self, rel: str = "child") -> dict:
        return {"name": rel, "path": str(self.parent / rel), "ide": "claude-code",
                "parent_path": str(self.parent)}


class TestAnnotateHelper(LinkBase):
    """Direct calls to hub_server._annotate_subproject_links()."""

    def _rows_for_helper(self, *child_rels: str) -> tuple:
        """(projects_list, parent_row, {rel: child_row}) mimicking list_projects rows."""
        parent_row = {"name": "parent", "path": str(self.parent), "is_parent": True}
        child_rows = {}
        projects = [parent_row]
        for rel in child_rels:
            row = {"name": rel, "path": str(self.parent / rel),
                   "parent_path": str(self.parent)}
            child_rows[rel] = row
            projects.append(row)
        return projects, parent_row, child_rows

    def test_healthy_link_no_issues_child_ok(self):
        self._write_parent_cfg([self._entry("child")])
        self._make_child("child")
        self._register(self._parent_reg_row(), self._child_reg_row("child"))
        projects, parent_row, child_rows = self._rows_for_helper("child")

        hub_server._annotate_subproject_links(projects)

        self.assertNotIn("subproject_issues", parent_row)
        self.assertEqual(child_rows["child"]["link_status"], "ok")

    def test_backlink_missing_counts_issue(self):
        self._write_parent_cfg([self._entry("child")])
        self._make_child("child", backlink=False)
        self._register(self._parent_reg_row(), self._child_reg_row("child"))
        projects, parent_row, child_rows = self._rows_for_helper("child")

        hub_server._annotate_subproject_links(projects)

        self.assertEqual(parent_row["subproject_issues"], 1)
        self.assertEqual(child_rows["child"]["link_status"], "backlink_broken")

    def test_backlink_pointing_elsewhere_counts_issue(self):
        self._write_parent_cfg([self._entry("child")])
        elsewhere = self.base / "not-the-parent"
        elsewhere.mkdir()
        self._make_child("child", backlink_path=str(elsewhere))
        self._register(self._parent_reg_row(), self._child_reg_row("child"))
        projects, parent_row, child_rows = self._rows_for_helper("child")

        hub_server._annotate_subproject_links(projects)

        self.assertEqual(parent_row["subproject_issues"], 1)
        self.assertEqual(child_rows["child"]["link_status"], "backlink_broken")

    def test_registry_orphan_counts_issue(self):
        # Healthy config child + a registry row claiming this parent with NO
        # matching parent-config entry -> orphan, same rule as reconcile.
        self._write_parent_cfg([self._entry("child")])
        self._make_child("child")
        self._register(self._parent_reg_row(), self._child_reg_row("child"),
                       self._child_reg_row("stray"))
        projects, parent_row, child_rows = self._rows_for_helper("child", "stray")

        hub_server._annotate_subproject_links(projects)

        self.assertEqual(parent_row["subproject_issues"], 1)
        self.assertEqual(child_rows["stray"]["link_status"], "orphan")
        self.assertEqual(child_rows["child"]["link_status"], "ok")

    def test_missing_folder_counts_issue(self):
        # Config entry exists but the child folder was deleted.
        self._write_parent_cfg([self._entry("ghost")])
        self._register(self._parent_reg_row(), self._child_reg_row("ghost"))
        projects, parent_row, child_rows = self._rows_for_helper("ghost")

        hub_server._annotate_subproject_links(projects)

        self.assertEqual(parent_row["subproject_issues"], 1)
        self.assertEqual(child_rows["ghost"]["link_status"], "missing_folder")

    def test_non_parent_rows_untouched(self):
        # Rows without is_parent/subproject_count are skipped entirely.
        self._write_parent_cfg([self._entry("child")])
        self._make_child("child", backlink=False)
        self._register(self._parent_reg_row(), self._child_reg_row("child"))
        plain = {"name": "parent", "path": str(self.parent)}  # no is_parent
        projects = [plain]

        hub_server._annotate_subproject_links(projects)

        self.assertNotIn("subproject_issues", plain)


class TestProjectsEndpoint(LinkBase):
    """GET /api/projects surfaces link_status + subproject_issues."""

    def setUp(self):
        super().setUp()
        self.client = hub_server.app.test_client()

    def _get_rows(self) -> dict:
        resp = self.client.get("/api/projects")
        self.assertEqual(resp.status_code, 200)
        rows = resp.get_json()
        self.assertIsInstance(rows, list)
        return {r["name"]: r for r in rows}

    def test_healthy_link_through_endpoint(self):
        self._write_parent_cfg([self._entry("child")])
        self._make_child("child")
        self._register(self._parent_reg_row(), self._child_reg_row("child"))

        by_name = self._get_rows()

        parent = by_name["parent"]
        child = by_name["child"]
        self.assertTrue(parent["is_parent"])
        self.assertNotIn("subproject_issues", parent)
        self.assertEqual(child["link_status"], "ok")
        self.assertEqual(Path(child["parent_path"]).resolve(),
                         self.parent.resolve())

    def test_broken_backlink_through_endpoint(self):
        self._write_parent_cfg([self._entry("child")])
        self._make_child("child", backlink=False)
        self._register(self._parent_reg_row(), self._child_reg_row("child"))

        by_name = self._get_rows()

        self.assertEqual(by_name["parent"]["subproject_issues"], 1)
        self.assertEqual(by_name["child"]["link_status"], "backlink_broken")


if __name__ == "__main__":
    unittest.main()
