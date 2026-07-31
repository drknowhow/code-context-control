"""Tests for the hub's Tool Discipline routes (docs/enforcement.md).

Three things these pin that a screenshot cannot:
  - a project we could not READ must not be reported as running a mode
    ("strict" is a claim; "we don't know" is the truth);
  - a mode set from the Hub is an explicit human choice, so it must be
    recorded as set_by='user' and survive a later permission-tier change;
  - the overview must keep saying what the mode does NOT switch off, so the
    tab can never imply that lowering discipline lowers a security boundary.
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

import cli.hub_server as hub_server  # noqa: E402
from services import access_telemetry as at  # noqa: E402
from services import enforcement_policy as ep  # noqa: E402


class _StubPM:
    def __init__(self, projects):
        self._projects = projects

    def list_projects(self):
        return self._projects


class TestHubEnforcementRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.proj = root / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"permission_tier": "standard"}), encoding="utf-8")
        self.bare = root / "bare"       # registered, no .c3
        self.bare.mkdir()

        hub_server.app.config["TESTING"] = True
        self.client = hub_server.app.test_client()
        self._pm = mock.patch.object(hub_server, "_pm", return_value=_StubPM([
            {"name": "proj", "path": str(self.proj)},
            {"name": "bare", "path": str(self.bare)},
        ]))
        self._pm.start()
        self._resolve = mock.patch.object(
            hub_server, "_resolve_project_path", side_effect=lambda p: str(Path(p)))
        self._resolve.start()

    def tearDown(self):
        self._pm.stop()
        self._resolve.stop()
        self._tmp.cleanup()

    def _overview(self):
        resp = self.client.get("/api/hub/enforcement/overview")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _row(self, data, name):
        return next(r for r in data["projects"] if r["name"] == name)

    # ── Overview ────────────────────────────────────────────────────────────

    def test_overview_reports_default_mode_for_unset_project(self):
        row = self._row(self._overview(), "proj")
        self.assertEqual(row["mode"], ep.DEFAULT_MODE)
        self.assertEqual(row["scope"], "default")
        self.assertTrue(row["initialized"])

    def test_uninitialized_project_reports_no_mode(self):
        """'No .c3' must not be rendered as a project running strict."""
        row = self._row(self._overview(), "bare")
        self.assertFalse(row["initialized"])
        self.assertIsNone(row["mode"])

    def test_unreadable_project_reports_error_not_a_mode(self):
        with mock.patch.object(ep, "resolve", side_effect=OSError("boom")):
            row = self._row(self._overview(), "proj")
        self.assertIsNotNone(row["error"])
        self.assertIsNone(row["mode"])

    def test_one_bad_project_does_not_blank_the_page(self):
        real = ep.resolve

        def flaky(path, *a, **kw):
            if str(path).endswith("proj"):
                raise OSError("boom")
            return real(path, *a, **kw)

        with mock.patch.object(ep, "resolve", side_effect=flaky):
            data = self._overview()
        self.assertEqual(len(data["projects"]), 2)

    def test_overview_surfaces_tier_and_what_it_implies(self):
        row = self._row(self._overview(), "proj")
        self.assertEqual(row["tier"], "standard")
        self.assertEqual(row["tier_implies"], ep.MODE_ADVISORY)

    def test_overview_states_what_the_mode_does_not_disable(self):
        note = self._overview()["coverage_note"].lower()
        self.assertIn("access guard", note)
        self.assertIn("vault", note)
        self.assertIn("locks", note)

    def test_overview_lists_every_mode_with_help(self):
        modes = self._overview()["modes"]
        self.assertEqual([m["id"] for m in modes], list(ep.MODES))
        self.assertTrue(all(m["help"] for m in modes))

    def test_malformed_section_is_surfaced_as_a_warning(self):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"enforcement": {"mode": "yolo"}}), encoding="utf-8")
        row = self._row(self._overview(), "proj")
        self.assertEqual(row["mode"], ep.MODE_STRICT)
        self.assertTrue(row["warnings"])

    # ── Denial telemetry ────────────────────────────────────────────────────

    def test_overview_includes_denial_counts_and_fixes(self):
        at.record(layer=at.LAYER_DISCIPLINE, rule="native-write-blocked",
                  tool="Edit", project_path=str(self.proj))
        at.record(layer=at.LAYER_ACCESS, rule="secrets/**", scope="project",
                  tool="Read", project_path=str(self.proj))
        data = self._overview()
        row = self._row(data, "proj")
        self.assertEqual(row["denial_total"], 2)
        self.assertEqual(data["totals"]["discipline"], 1)
        self.assertEqual(data["totals"]["access"], 1)
        fixes = {d["rule"]: d["fix"] for d in row["top_denials"]}
        self.assertIn("c3 enforce", fixes["native-write-blocked"])
        self.assertIn("access remove", fixes["secrets/**"])

    def test_clear_denials(self):
        at.record(layer=at.LAYER_DISCIPLINE, rule="x", tool="Edit",
                  project_path=str(self.proj))
        resp = self.client.delete(
            f"/api/projects/enforcement/denials?path={self.proj}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._row(self._overview(), "proj")["denial_total"], 0)

    def test_clear_denials_requires_path(self):
        self.assertEqual(
            self.client.delete("/api/projects/enforcement/denials").status_code,
            400)

    # ── Mutation ────────────────────────────────────────────────────────────

    def test_set_mode_persists(self):
        resp = self.client.post("/api/projects/enforcement",
                                json={"path": str(self.proj), "mode": "advisory"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ep.resolve(str(self.proj)).mode, "advisory")

    def test_hub_change_is_recorded_as_a_user_choice(self):
        """A deliberate Hub change must survive a later tier change."""
        self.client.post("/api/projects/enforcement",
                         json={"path": str(self.proj), "mode": "off"})
        self.assertEqual(ep.resolve(str(self.proj)).set_by, ep.SET_BY_USER)
        result = ep.set_mode("strict", str(self.proj), set_by=ep.SET_BY_TIER)
        self.assertTrue(result["deferred"])
        self.assertEqual(ep.resolve(str(self.proj)).mode, "off")

    def test_set_mode_rejects_unknown_mode(self):
        resp = self.client.post("/api/projects/enforcement",
                                json={"path": str(self.proj), "mode": "sorta"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_set_mode_requires_path_and_mode(self):
        for body in ({"mode": "off"}, {"path": str(self.proj)}, {}):
            self.assertEqual(
                self.client.post("/api/projects/enforcement", json=body).status_code,
                400)

    def test_set_mode_preserves_unrelated_config(self):
        self.client.post("/api/projects/enforcement",
                         json={"path": str(self.proj), "mode": "advisory"})
        cfg = json.loads(
            (self.proj / ".c3" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["permission_tier"], "standard")
        self.assertEqual(cfg["enforcement"]["mode"], "advisory")

    def test_set_mode_is_audited(self):
        with mock.patch("services.activity_log.ActivityLog") as log:
            self.client.post("/api/projects/enforcement",
                             json={"path": str(self.proj), "mode": "advisory"})
        self.assertTrue(log.called)
        event, payload = log.return_value.log.call_args[0]
        self.assertEqual(event, "access_action")
        self.assertEqual(payload["via"], "hub")
        self.assertEqual(payload["mode"], "advisory")


class TestHubEnforcementAssets(unittest.TestCase):
    """The view is useless if the bundle never loads it."""

    def test_component_is_in_the_js_bundle(self):
        self.assertIn("hub_ui/components/hub_enforcement.js",
                      hub_server._HUB_JS_FILES)

    def test_component_file_exists(self):
        path = REPO_ROOT / "cli" / "hub_ui" / "components" / "hub_enforcement.js"
        self.assertTrue(path.is_file())
        self.assertIn("function HubEnforcement", path.read_text(encoding="utf-8"))

    def test_nav_entry_and_route_are_wired(self):
        topbar = (REPO_ROOT / "cli" / "hub_ui" / "components" / "topbar.js"
                  ).read_text(encoding="utf-8")
        app = (REPO_ROOT / "cli" / "hub_ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("'enforce'", topbar)
        self.assertIn("<HubEnforcement", app)
        self.assertIn("mainView === 'enforce'", app)
        # The persisted main_view whitelist must accept the new tab, or a
        # reload silently drops the user back to Projects.
        whitelist = next(ln for ln in app.splitlines()
                         if "includes(cfg.main_view)" in ln)
        self.assertIn("'enforce'", whitelist)

    def test_shield_icon_exists(self):
        icons = (REPO_ROOT / "cli" / "ui" / "icons.js").read_text(encoding="utf-8")
        self.assertIn("shield:", icons)


if __name__ == "__main__":
    unittest.main()
