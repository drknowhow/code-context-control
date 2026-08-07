"""Tests for the project UI's Tool Discipline routes (docs/enforcement.md).

The per-project counterpart to tests/test_hub_enforcement_routes.py. What these
pin that the Hub tests do not: the panel is the only place a user sees WHY they
are blocked, so the payload must carry the denial evidence, the provenance, and
the "this does not switch off your security boundaries" note — dropping any of
them turns an informed choice into a blind one.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.server as server  # noqa: E402
from services import access_telemetry as at  # noqa: E402
from services import enforcement_policy as ep  # noqa: E402


class TestEnforcementRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"permission_tier": "standard"}), encoding="utf-8")
        self._orig = server.PROJECT_PATH
        server.PROJECT_PATH = self.proj
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()
        # Enforcement resolves project -> global -> default. Without a clean
        # home, a developer whose own ~/.c3/config.json carries an
        # `enforcement` section sees scope 'global' here while CI passes.
        self._home = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"C3_HOME": self._home.name})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._home.cleanup()
        server.PROJECT_PATH = self._orig
        self._tmp.cleanup()

    def _get(self):
        resp = self.client.get("/api/enforcement")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    # ── Read ────────────────────────────────────────────────────────────────

    def test_defaults_to_strict_when_unset(self):
        d = self._get()
        self.assertEqual(d["mode"], ep.DEFAULT_MODE)
        self.assertEqual(d["scope"], "default")

    def test_reports_tier_and_what_it_implies(self):
        d = self._get()
        self.assertEqual(d["tier"], "standard")
        self.assertEqual(d["tier_implies"], ep.MODE_ADVISORY)

    def test_lists_every_mode_with_help(self):
        d = self._get()
        self.assertEqual([m["id"] for m in d["modes"]], list(ep.MODES))
        self.assertTrue(all(m["help"] for m in d["modes"]))

    def test_states_what_the_mode_does_not_disable(self):
        note = self._get()["coverage_note"].lower()
        self.assertIn("access guard", note)
        self.assertIn("vault", note)
        self.assertIn("locks", note)

    def test_malformed_section_reports_strict_plus_warning(self):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"enforcement": {"mode": "yolo"}}), encoding="utf-8")
        d = self._get()
        self.assertEqual(d["mode"], ep.MODE_STRICT)
        self.assertTrue(d["warnings"])

    def test_missing_config_does_not_500(self):
        (self.proj / ".c3" / "config.json").unlink()
        self.assertEqual(self._get()["mode"], ep.DEFAULT_MODE)

    # ── Denial event search (v2.67) ─────────────────────────────────────────

    def test_denial_event_search_route(self):
        at.record(layer=at.LAYER_DISCIPLINE, rule="native-write-blocked",
                  tool="Edit", path="src/app.py", session_id="s1",
                  project_path=str(self.proj))
        at.record(layer=at.LAYER_ACCESS, rule="secrets/**", scope="project",
                  tool="Read", path="secrets/k.txt", session_id="s2",
                  project_path=str(self.proj))
        d = self.client.get("/api/enforcement/denials/search").get_json()
        self.assertEqual(d["matched"], 2)
        self.assertEqual(d["events"][0]["path"], "secrets/k.txt",
                         "newest event must come first")
        self.assertIn("fix", d["events"][0])
        d = self.client.get(
            "/api/enforcement/denials/search?layer=discipline&q=src").get_json()
        self.assertEqual(d["matched"], 1)
        self.assertEqual(d["events"][0]["tool"], "Edit")

    def test_unparseable_config_does_not_500(self):
        (self.proj / ".c3" / "config.json").write_text("{ nope", encoding="utf-8")
        d = self._get()
        self.assertEqual(d["mode"], ep.MODE_STRICT)
        self.assertTrue(d["warnings"])

    # ── Denial evidence ─────────────────────────────────────────────────────

    def test_payload_carries_denials_with_fixes(self):
        at.record(layer=at.LAYER_DISCIPLINE, rule="native-write-blocked",
                  tool="Edit", project_path=str(self.proj))
        at.record(layer=at.LAYER_ACCESS, rule="secrets/**", scope="project",
                  tool="Read", project_path=str(self.proj))
        d = self._get()["denials"]
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["by_layer"]["discipline"], 1)
        self.assertEqual(d["by_layer"]["access"], 1)
        fixes = {r["rule"]: r["fix"] for r in d["rows"]}
        self.assertIn("c3 enforce", fixes["native-write-blocked"])
        self.assertIn("access remove", fixes["secrets/**"])

    def test_clear_denials(self):
        at.record(layer=at.LAYER_DISCIPLINE, rule="x", tool="Edit",
                  project_path=str(self.proj))
        self.assertEqual(
            self.client.delete("/api/enforcement/denials").status_code, 200)
        self.assertEqual(self._get()["denials"]["total"], 0)

    # ── Write ───────────────────────────────────────────────────────────────

    def test_set_mode_persists(self):
        resp = self.client.post("/api/enforcement", json={"mode": "advisory"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._get()["mode"], "advisory")

    def test_ui_change_is_an_explicit_user_choice(self):
        self.client.post("/api/enforcement", json={"mode": "off"})
        self.assertEqual(self._get()["set_by"], ep.SET_BY_USER)
        # …and therefore survives a later tier-derived write.
        self.assertTrue(
            ep.set_mode("strict", str(self.proj),
                        set_by=ep.SET_BY_TIER)["deferred"])
        self.assertEqual(self._get()["mode"], "off")

    def test_rejects_unknown_mode(self):
        resp = self.client.post("/api/enforcement", json={"mode": "sorta"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_rejects_missing_mode(self):
        self.assertEqual(
            self.client.post("/api/enforcement", json={}).status_code, 400)

    def test_preserves_unrelated_config(self):
        self.client.post("/api/enforcement", json={"mode": "advisory"})
        cfg = json.loads(
            (self.proj / ".c3" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["permission_tier"], "standard")
        self.assertEqual(cfg["enforcement"]["mode"], "advisory")


class TestEnforcementPanelAssets(unittest.TestCase):
    """The tab is useless if the bundle never loads it."""

    def test_component_is_in_the_js_bundle(self):
        self.assertIn("ui/components/enforcement.js", server._UI_JS_FILES)

    def test_component_file_exists(self):
        path = REPO_ROOT / "cli" / "ui" / "components" / "enforcement.js"
        self.assertTrue(path.is_file())
        self.assertIn("EnforcementPanel", path.read_text(encoding="utf-8"))

    def test_tab_is_registered_and_rendered(self):
        app_js = (REPO_ROOT / "cli" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id: "enforcement"', app_js)
        self.assertIn("<EnforcementPanel", app_js)

    def test_shield_icon_exists(self):
        icons = (REPO_ROOT / "cli" / "ui" / "icons.js").read_text(encoding="utf-8")
        self.assertIn("shield:", icons)


if __name__ == "__main__":
    unittest.main()
