"""Inherit again, and see every project's approval policy in one call (2.141.0).

WHAT WAS MISSING. A project that pinned a discipline mode or an `override`
opinion could never go back to "whatever global says": ``resolve()`` stops at
the first enforcement section it finds, and override policy AND-merges, so no
value a surface can WRITE means "inherit". Only removing the section does.
And a cross-project editor (C3 Desk's Rules view) had a bulk read for
enforcement but had to make one call per project for override policy.

WHAT THESE PIN.
1. Clearing an enforcement section drops EVERY field (a ttl left behind would
   be the mode-less partial section ``set_fields`` refuses to create).
2. Clearing override policy is widening-checked against the policy as it
   would resolve AFTERWARDS. Merge is tightening-only, so a project section
   can only ever sit at or below global — removing it can loosen, and that
   must need ``confirm: "widen"`` exactly like a set does.
3. ``wake`` survives a clear; a corrupt section is refused, not reset.
4. Every policy write from the Hub leaves an audit row on the target.
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

import cli.hub_server as hub_server  # noqa: E402
from services import enforcement_policy as ep  # noqa: E402
from services import override_policy as opol  # noqa: E402

WAKE = {"command": ["notify-send", "decided"]}


class _StubPM:
    def __init__(self, projects):
        self._projects = projects

    def list_registered(self):
        return self._projects


def _cfg(path: Path) -> dict:
    return json.loads((path / ".c3" / "config.json").read_text(encoding="utf-8"))


def _write_cfg(path: Path, data: dict) -> None:
    (path / ".c3").mkdir(parents=True, exist_ok=True)
    (path / ".c3" / "config.json").write_text(json.dumps(data), encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.proj = root / "proj"
        _write_cfg(self.proj, {"permission_tier": "standard"})
        self.other = root / "other"
        _write_cfg(self.other, {})
        self.bare = root / "bare"        # registered, no .c3
        self.bare.mkdir()
        # Both global scopes point at one clean home: enforcement honours
        # C3_HOME, override policy deliberately does not (it reads Path.home()),
        # so each is redirected the way its own module resolves it.
        self.home = root / "home"
        (self.home / ".c3").mkdir(parents=True)
        self._env = mock.patch.dict(os.environ, {"C3_HOME": str(self.home)})
        self._env.start()
        self._gbase = mock.patch.object(opol, "_global_base",
                                        return_value=self.home)
        self._gbase.start()

        hub_server.app.config["TESTING"] = True
        self.client = hub_server.app.test_client()
        self._pm = mock.patch.object(hub_server, "_pm", return_value=_StubPM([
            {"name": "proj", "path": str(self.proj)},
            {"name": "other", "path": str(self.other)},
            {"name": "bare", "path": str(self.bare)},
        ]))
        self._pm.start()
        self._resolve = mock.patch.object(
            hub_server, "_resolve_project_path", side_effect=lambda p: str(Path(p)))
        self._resolve.start()

    def tearDown(self):
        self._resolve.stop()
        self._pm.stop()
        self._gbase.stop()
        self._env.stop()
        self._tmp.cleanup()

    def set_global_override(self, section: dict) -> None:
        _write_cfg(self.home, {"override": section})


# ── enforcement: clear ─────────────────────────────────────────────────


class TestEnforcementClear(_Base):
    def test_clear_drops_every_field_and_inherits_global(self):
        ep.set_mode("advisory", scope="global")
        ep.set_mode("off", str(self.proj), signal_ttl_s=120,
                    blocked_tools=["Edit"])
        result = ep.clear(str(self.proj))
        self.assertTrue(result["cleared"])
        self.assertEqual(result["previous"], "off")
        self.assertEqual((result["mode"], result["scope"]), ("advisory", "global"))
        self.assertNotIn("enforcement", _cfg(self.proj))
        self.assertEqual(_cfg(self.proj)["permission_tier"], "standard")
        self.assertEqual(ep.resolve(str(self.proj)).scope, "global")

    def test_clear_with_no_global_falls_back_to_the_builtin_default(self):
        ep.set_mode("off", str(self.proj))
        result = ep.clear(str(self.proj))
        self.assertEqual((result["mode"], result["scope"]),
                         (ep.DEFAULT_MODE, "default"))

    def test_nothing_to_clear_is_a_noop_not_an_error(self):
        result = ep.clear(str(self.proj))
        self.assertFalse(result["cleared"])
        self.assertEqual(_cfg(self.proj), {"permission_tier": "standard"})

    def test_global_clear_returns_to_the_builtin_default(self):
        ep.set_mode("off", scope="global")
        result = ep.clear(scope="global")
        self.assertTrue(result["cleared"])
        self.assertEqual((result["mode"], result["scope"]),
                         (ep.DEFAULT_MODE, "default"))

    def test_unreadable_config_is_refused_not_overwritten(self):
        (self.proj / ".c3" / "config.json").write_text("{nope", encoding="utf-8")
        with self.assertRaises(ValueError):
            ep.clear(str(self.proj))
        self.assertEqual(
            (self.proj / ".c3" / "config.json").read_text(encoding="utf-8"),
            "{nope")

    def test_route_clears_and_audits_on_the_target(self):
        ep.set_mode("off", str(self.proj))
        with mock.patch("services.activity_log.ActivityLog") as log:
            resp = self.client.post("/api/projects/enforcement/clear",
                                    json={"path": str(self.proj)})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertTrue(resp.get_json()["cleared"])
        event, payload = log.return_value.log.call_args[0]
        self.assertEqual(event, "access_action")
        self.assertEqual((payload["action"], payload["via"]), ("clear", "hub"))
        self.assertEqual(payload["previous"], "off")

    def test_route_noop_is_200_and_not_audited(self):
        with mock.patch("services.activity_log.ActivityLog") as log:
            resp = self.client.post("/api/projects/enforcement/clear",
                                    json={"path": str(self.proj)})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["cleared"])
        self.assertFalse(log.called)

    def test_route_validates_scope_and_path(self):
        post = lambda b: self.client.post(  # noqa: E731
            "/api/projects/enforcement/clear", json=b).status_code
        self.assertEqual(post({}), 400)
        self.assertEqual(post({"scope": "machine"}), 400)
        self.assertEqual(post({"scope": "global"}), 200)


# ── override policy: clear ─────────────────────────────────────────────


class TestOverridePolicyClear(_Base):
    def test_nothing_to_clear(self):
        self.assertEqual(opol.clear_section(self.proj), (None, []))

    def test_clearing_a_tighter_project_widens_and_needs_confirmation(self):
        self.set_global_override({"enabled": True,
                                  "layers": {"discipline": True},
                                  "allow_session_grants": True})
        _write_cfg(self.proj, {"override": {"enabled": False,
                                            "allow_session_grants": False}})
        with self.assertRaises(opol.PolicyEditError) as ei:
            opol.clear_section(self.proj)
        self.assertEqual(ei.exception.payload["confirm_with"], "widen")
        self.assertIn("enabled", ei.exception.widens)
        self.assertIn("allow_session_grants", ei.exception.widens)
        self.assertIn("override", _cfg(self.proj), "it wrote despite refusing")

        kept, widens = opol.clear_section(self.proj, confirmed=True)
        self.assertEqual(kept, {})
        self.assertIn("enabled", widens)
        self.assertNotIn("override", _cfg(self.proj))
        self.assertTrue(opol.resolve(str(self.proj)).enabled)

    def test_clearing_a_project_that_opted_in_is_a_tightening(self):
        # No global opinion: the project's own `enabled: true` is what turned
        # the feature on, so dropping it can only close things.
        _write_cfg(self.proj, {"override": {"enabled": True,
                                            "layers": {"discipline": True}}})
        kept, widens = opol.clear_section(self.proj)
        self.assertEqual((kept, widens), ({}, []))
        self.assertFalse(opol.resolve(str(self.proj)).enabled)

    def test_wake_survives_the_clear(self):
        _write_cfg(self.proj, {"override": {"enabled": True, "wake": WAKE}})
        kept, _ = opol.clear_section(self.proj)
        self.assertEqual(kept, {"wake": WAKE})
        self.assertEqual(_cfg(self.proj)["override"], {"wake": WAKE})

    def test_corrupt_section_is_refused_not_reset(self):
        _write_cfg(self.proj, {"override": {"enabled": "yes"}})
        with self.assertRaises(opol.PolicyEditError) as ei:
            opol.clear_section(self.proj)
        self.assertEqual(ei.exception.status, 409)
        self.assertEqual(_cfg(self.proj)["override"], {"enabled": "yes"})

    def test_route_needs_widen_then_clears_and_audits(self):
        self.set_global_override({"enabled": True})
        _write_cfg(self.proj, {"override": {"enabled": False}})
        url = "/api/hub/overrides/policy/clear"
        resp = self.client.post(url, json={"path": str(self.proj)})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json()["needs_confirmation"])
        with mock.patch("services.activity_log.ActivityLog") as log:
            resp = self.client.post(url, json={"path": str(self.proj),
                                               "confirm": "widen"})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body["cleared"])
        self.assertTrue(body["policy"]["enabled"])
        _, payload = log.return_value.log.call_args[0]
        self.assertEqual((payload["kind"], payload["action"]),
                         ("override_policy", "clear"))
        self.assertEqual(payload["widened"], ["enabled"])

    def test_route_corrupt_is_409(self):
        _write_cfg(self.proj, {"override": {"bogus": 1}})
        resp = self.client.post("/api/hub/overrides/policy/clear",
                                json={"path": str(self.proj)})
        self.assertEqual(resp.status_code, 409)

    def test_route_requires_path(self):
        self.assertEqual(self.client.post(
            "/api/hub/overrides/policy/clear", json={}).status_code, 400)


# ── override policy: overview + audit on set ───────────────────────────


class TestOverridePolicyOverview(_Base):
    def _overview(self):
        resp = self.client.get("/api/hub/overrides/policy/overview")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _row(self, data, name):
        return next(r for r in data["projects"] if r["name"] == name)

    def test_rows_say_what_the_project_pins(self):
        _write_cfg(self.proj, {"override": {"enabled": True,
                                            "layers": {"discipline": True}}})
        data = self._overview()
        row = self._row(data, "proj")
        self.assertTrue(row["configured"])
        self.assertEqual(row["project_keys"], ["enabled", "layers.discipline"])
        self.assertTrue(row["policy"]["enabled"])
        other = self._row(data, "other")
        self.assertFalse(other["configured"])
        self.assertEqual(other["project_keys"], [])
        self.assertFalse(other["policy"]["enabled"])

    def test_uninitialized_and_corrupt_rows_are_not_reported_as_a_policy(self):
        _write_cfg(self.proj, {"override": {"enabled": "yes"}})
        data = self._overview()
        bare = self._row(data, "bare")
        self.assertFalse(bare["initialized"])
        self.assertIsNone(bare["policy"])
        proj = self._row(data, "proj")
        self.assertTrue(proj["corrupt"])
        self.assertEqual(proj["policy"]["corrupt_scopes"], ["project"])

    def test_one_bad_project_does_not_blank_the_page(self):
        with mock.patch.object(opol, "scope_keys",
                               side_effect=[RuntimeError("boom"),
                                            ([], False, False)]):
            data = self._overview()
        self.assertEqual(self._row(data, "proj")["error"], "boom")
        self.assertIsNone(self._row(data, "other")["error"])

    def test_global_is_served_read_only_with_its_keys(self):
        self.set_global_override({"enabled": True, "max_ttl_s": 300})
        g = self._overview()["global_policy"]
        self.assertTrue(g["configured"])
        self.assertEqual(g["keys"], ["enabled", "max_ttl_s"])
        self.assertTrue(g["policy"]["enabled"])
        self.assertEqual(g["policy"]["max_ttl_s"], 300)

    def test_schema_bits_and_features(self):
        data = self._overview()
        self.assertEqual(data["layers"], list(opol.LAYER_KEYS))
        self.assertIn("clear", data["features"])
        self.assertNotIn("wake", data["defaults"])
        self.assertIn("tightening only", data["coverage_note"])

    def test_policy_set_is_audited_with_keys_not_values(self):
        with mock.patch("services.activity_log.ActivityLog") as log:
            resp = self.client.post("/api/hub/overrides/policy", json={
                "path": str(self.proj), "confirm": "widen",
                "override": {"enabled": True, "layers": {"mask": False}}})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        _, payload = log.return_value.log.call_args[0]
        self.assertEqual((payload["kind"], payload["action"]),
                         ("override_policy", "set"))
        self.assertEqual(payload["keys"], ["enabled", "layers.mask"])
        self.assertEqual(payload["widened"], ["enabled"])

    def test_new_routes_resolve_to_their_own_endpoints(self):
        adapter = hub_server.app.url_map.bind("localhost")
        self.assertEqual(
            adapter.match("/api/hub/overrides/policy/overview", method="GET")[0],
            "api_hub_override_policy_overview")
        self.assertEqual(
            adapter.match("/api/hub/overrides/policy/clear", method="POST")[0],
            "api_hub_override_policy_clear")


if __name__ == "__main__":
    unittest.main()
