"""Mask Guard human surfaces — REST routes and CLI param parsing.

These are the HUMAN surfaces (docs/mask-guard.md §8). The invariant worth
testing hardest: ``/api/access/preview`` returns the real file as ``before``,
which is fine for a localhost UI and would be a total bypass if it were ever
reachable by an agent — so it lives only here, never in a tool handler.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.server as srv  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import mask_mirror  # noqa: E402

SECRET = "AKIAIOSFODNN7EXAMPLE"


class MaskRouteBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        self._old_project_path = srv.PROJECT_PATH
        srv.PROJECT_PATH = self.proj
        self._gb = mock.patch.object(
            ag, "_global_base", return_value=Path(self._home.name))
        self._gb.start()
        self._hm = mock.patch.object(
            Path, "home", staticmethod(lambda: Path(self._home.name)))
        self._hm.start()
        self.client = srv.app.test_client()

    def tearDown(self):
        self._hm.stop()
        self._gb.stop()
        srv.PROJECT_PATH = self._old_project_path
        self._tmp.cleanup()
        self._home.cleanup()

    def _add_mask(self, glob, preset, params=None, scope="project"):
        return self.client.post("/api/access/mask", json={
            "glob": glob, "preset": preset, "params": params or {},
            "scope": scope})

    def _write(self, rel, text):
        path = self.proj / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class TestMaskRoutes(MaskRouteBase):

    def test_add_list_remove_round_trip(self):
        resp = self._add_mask("data/**", "sample_rows",
                              {"count": 5, "strategy": "first"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["rule"]["added"])

        listing = self.client.get("/api/access").get_json()
        entries = listing["scopes"]["project"]["mask"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["preset"], "sample_rows")
        self.assertIn("sample_rows", listing["presets"])
        self.assertTrue(listing["mask"]["stale"])

        gone = self.client.delete("/api/access/mask?glob=data/**&scope=project")
        self.assertTrue(gone.get_json()["removed"])
        self.assertEqual(
            self.client.get("/api/access").get_json()
            ["scopes"]["project"]["mask"], [])

    def test_add_rejects_unknown_preset(self):
        resp = self._add_mask("data/**", "summarize")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("unknown mask preset", resp.get_json()["error"])

    def test_add_rejects_bad_params(self):
        resp = self._add_mask("data/**", "sample_rows",
                              {"count": 0, "strategy": "first"})
        self.assertEqual(resp.status_code, 400)

    def test_add_replaces_same_glob_rather_than_conflicting(self):
        self._add_mask("data/**", "redact_secrets")
        resp = self._add_mask("data/**", "sample_rows",
                              {"count": 2, "strategy": "first"})
        self.assertTrue(resp.get_json()["rule"]["replaced"])
        entries = (self.client.get("/api/access").get_json()
                   ["scopes"]["project"]["mask"])
        self.assertEqual(len(entries), 1)

    def test_preview_shows_before_and_after(self):
        self._write("conf/app.py", f'KEY = "{SECRET}"\n')
        self._add_mask("conf/**", "redact_secrets")
        data = self.client.post("/api/access/preview",
                                json={"path": "conf/app.py"}).get_json()
        self.assertEqual(data["verdict"], "masked")
        self.assertIn(SECRET, data["before"])
        self.assertNotIn(SECRET, data["after"])
        self.assertIn(ag.TAG_MASKED, data["header"])
        self.assertEqual(data["preset"], "redact_secrets")

    def test_preview_of_unmasked_path_is_identity(self):
        self._write("src/app.py", "x = 1\n")
        self._add_mask("conf/**", "redact_secrets")
        data = self.client.post("/api/access/preview",
                                json={"path": "src/app.py"}).get_json()
        self.assertEqual(data["verdict"], "allowed")
        self.assertEqual(data["before"], data["after"])

    def test_preview_of_denied_path_returns_no_content(self):
        self._write("secret.txt", "top secret")
        self.client.post("/api/access",
                         json={"glob": "secret.txt", "kind": "deny",
                               "scope": "project"})
        data = self.client.post("/api/access/preview",
                                json={"path": "secret.txt"}).get_json()
        self.assertEqual(data["verdict"], "denied")
        self.assertNotIn("before", data)
        self.assertNotIn("after", data)

    def test_preview_surfaces_render_failure_without_falling_back(self):
        self._write("data/a.csv", "id,v\n1,x\n")
        self._add_mask("data/**", "redact_columns", {"columns": ["nope"]})
        data = self.client.post("/api/access/preview",
                                json={"path": "data/a.csv"}).get_json()
        self.assertEqual(data["after"], "")
        self.assertIn("not found in header", data["error"])

    def test_preview_requires_a_path(self):
        self.assertEqual(
            self.client.post("/api/access/preview", json={}).status_code, 400)

    def test_activate_reports_and_clears_stale(self):
        self._write("data/a.csv", "id,v\n1,x\n2,y\n")
        self._add_mask("data/**", "sample_rows",
                       {"count": 1, "strategy": "first"})
        self.assertTrue(self.client.get("/api/access").get_json()
                        ["mask"]["stale"])

        resp = self.client.post("/api/access/mask/activate", json={})
        report = resp.get_json()["report"]
        self.assertTrue(report["ok"])
        self.assertEqual(report["views_built"], 1)
        self.assertFalse(self.client.get("/api/access").get_json()
                         ["mask"]["stale"])

    def test_activate_reports_incomplete_without_hiding_it(self):
        self._write("data/a.csv", "id,v\n1,x\n")
        self._add_mask("data/**", "redact_columns", {"columns": ["nope"]})
        report = self.client.post("/api/access/mask/activate",
                                  json={}).get_json()["report"]
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "incomplete")

    def test_coverage_and_summary_are_exposed_to_the_ui(self):
        self._add_mask("data/**", "redact_secrets")
        data = self.client.get("/api/access").get_json()
        self.assertIn("not containment", data["coverage"].lower())
        self.assertIn("NOT activated", data["mask_summary"])


class TestMaskCliParams(unittest.TestCase):
    """`--params 'count=20,strategy=first'` must type-coerce correctly."""

    def setUp(self):
        from cli.c3 import _parse_mask_params
        self.parse = _parse_mask_params

    def test_sample_rows_params(self):
        self.assertEqual(self.parse("count=20,strategy=first", "sample_rows"),
                         {"count": 20, "strategy": "first"})

    def test_columns_list_accepts_bare_continuations(self):
        self.assertEqual(self.parse("columns=email,name,ssn", "redact_columns"),
                         {"columns": ["email", "name", "ssn"]})

    def test_empty_params_for_presets_that_take_none(self):
        self.assertEqual(self.parse("", "redact_secrets"), {})

    def test_unparseable_chunk_raises(self):
        with self.assertRaises(ValueError):
            self.parse("garbage", "redact_secrets")

    def test_parsed_params_satisfy_the_validator(self):
        for preset, raw in (("sample_rows", "count=3,strategy=last"),
                            ("redact_columns", "columns=a,b"),
                            ("redact_secrets", ""),
                            ("signatures_only", "")):
            entry = {"glob": "x/**", "preset": preset,
                     "params": self.parse(raw, preset)}
            self.assertEqual(ag.validate_mask_entry(entry), "",
                             f"CLI params for {preset} must validate")


if __name__ == "__main__":
    unittest.main()
