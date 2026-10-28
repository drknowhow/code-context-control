"""The gateway's registered-project row filter resolves each registered
project and each distinct row path once per call, with the same result as
checking every row on its own."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("C3_ORACLE_API_KEY", "registered-rows-key")

from oracle.services import mobile_api  # noqa: E402


class _CountingScanner:
    def __init__(self, projects):
        self.projects = projects
        self.calls = 0

    def discover(self, force=False):
        self.calls += 1
        return [dict(p) for p in self.projects]


class TestRegisteredRows(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.alpha = root / "alpha"
        self.beta = root / "beta"
        self.plain = root / "plain"          # registered, but no .c3
        self.outsider = root / "outsider"    # never registered
        self.nested = self.alpha / ".claude" / "worktrees" / "wt1"
        for p in (self.alpha, self.beta, self.plain, self.outsider, self.nested):
            p.mkdir(parents=True)
        self.scanner = _CountingScanner([
            {"path": str(self.alpha), "has_c3": True},
            {"path": str(self.beta), "has_c3": True},
            {"path": str(self.plain), "has_c3": False},
        ])
        self._prev = mobile_api._scanner
        mobile_api.init_services(scanner=self.scanner)
        self.rows = ([{"id": f"a{i}", "project_path": str(self.alpha)} for i in range(40)]
                     + [{"id": f"o{i}", "project_path": str(self.outsider)} for i in range(40)]
                     + [{"id": f"n{i}", "project_path": str(self.nested)} for i in range(10)]
                     + [{"id": "p", "project_path": str(self.plain)},
                        {"id": "none", "project_path": ""}])

    def tearDown(self):
        mobile_api._scanner = self._prev
        self._tmp.cleanup()

    def _ids(self, rows):
        return [r["id"] for r in rows]

    def test_keeps_only_rows_of_registered_projects(self):
        kept = mobile_api._registered_rows(self.rows)
        expected = [r for r in self.rows
                    if mobile_api._resolve_registered(r["project_path"]) is not None]
        self.assertEqual(self._ids(kept), self._ids(expected))
        self.assertEqual(self._ids(kept), [f"a{i}" for i in range(40)])

    def test_serving_also_keeps_rows_nested_inside_a_project(self):
        kept = mobile_api._registered_rows(self.rows, serving=True)
        self.assertEqual(self._ids(kept),
                         [f"a{i}" for i in range(40)] + [f"n{i}" for i in range(10)])

    def test_projects_and_row_paths_are_resolved_once_per_call(self):
        real_resolve = Path.resolve
        with mock.patch.object(Path, "resolve", autospec=True,
                               side_effect=real_resolve) as resolve:
            mobile_api._registered_rows(self.rows, serving=True)
        self.assertEqual(self.scanner.calls, 1)
        # Two registered roots with .c3, four distinct non-empty row paths.
        self.assertEqual(resolve.call_count, 6)


if __name__ == "__main__":
    unittest.main()
