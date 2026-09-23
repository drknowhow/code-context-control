"""Builtin guard modes from the Hub (2.147.0): read every realm, tighten
freely, loosen only with a Desk client token and the glob retyped."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.hub_server as hub_server  # noqa: E402
from oracle.services import client_tokens  # noqa: E402
from services import access_guard as ag  # noqa: E402

ENV = "**/.env*"
GIT = "**/.git/**"
DESK = {"Authorization": "Bearer desk-token"}
PHONE = {"Authorization": "Bearer phone-token"}


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, account, value):
        self.store[(service, account)] = value

    def get_password(self, service, account):
        return self.store.get((service, account))


class _StubPM:
    def __init__(self, projects):
        self._projects = projects

    def list_registered(self):
        return self._projects


def _principal(token):
    return {"desk-token": {"kind": "desk", "client_id": "desk-1"},
            "phone-token": {"kind": "mobile", "client_id": "mobile-1"}}.get(token)


class HubBuiltinRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        (self.home / ".c3").mkdir(parents=True)
        self.proj = root / "proj"
        (self.proj / ".c3").mkdir(parents=True)

        fake = _FakeKeyring()
        mod = types.ModuleType("keyring")
        mod.set_password, mod.get_password = fake.set_password, fake.get_password
        self._patches = [
            mock.patch.dict(sys.modules, {"keyring": mod}),
            mock.patch.object(ag, "_global_base", return_value=self.home),
            mock.patch.object(hub_server, "_pm", return_value=_StubPM(
                [{"name": "proj", "path": str(self.proj)}])),
            mock.patch.object(hub_server, "_resolve_project_path",
                              side_effect=lambda p: Path(p)),
            mock.patch.object(client_tokens, "principal_for",
                              side_effect=_principal),
        ]
        for p in self._patches:
            p.start()
        hub_server.app.config["TESTING"] = True
        self.client = hub_server.app.test_client()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def set_mode(self, body, headers=None):
        resp = self.client.post("/api/hub/access/builtin/mode", json=body,
                                headers=headers or {})
        return resp.status_code, resp.get_json()

    def global_mode(self, glob):
        data = self.client.get("/api/hub/access/builtin").get_json()
        return next(g["global"] for g in data["guards"] if g["glob"] == glob)

    def test_strictness_ranks_the_same_word_differently_per_tier(self):
        self.assertEqual(ag.builtin_strictness(ENV, "default"), 3)
        self.assertEqual(ag.builtin_strictness(GIT, "default"), 2)
        self.assertEqual(ag.builtin_strictness(GIT, "deny"), 3)
        self.assertEqual(ag.builtin_strictness(ENV, "confirm"), 1)
        self.assertEqual(ag.builtin_strictness(ENV, "allow"), 0)
        with self.assertRaises(ValueError):
            ag.builtin_strictness("**/.c3/secrets.enc", "allow")
        with self.assertRaises(ValueError):
            ag.builtin_strictness(ENV, "off")

    def test_tightening_needs_no_token(self):
        status, body = self.set_mode({"scope": "global", "glob": GIT, "mode": "deny"})
        self.assertEqual(status, 200)
        self.assertFalse(body["loosened"])
        self.assertEqual(self.global_mode(GIT), "deny")

    def test_loosening_without_a_token_is_refused_with_the_cli_command(self):
        status, body = self.set_mode({"scope": "global", "glob": ENV,
                                      "mode": "confirm", "confirm": ENV})
        self.assertEqual(status, 403)
        self.assertTrue(body["needs_human"])
        self.assertEqual(body["command"], 'c3 access builtin mode "**/.env*" confirm')
        self.assertEqual(self.global_mode(ENV), "default")

    def test_a_phone_token_does_not_count_as_desk(self):
        status, _ = self.set_mode({"scope": "global", "glob": ENV, "mode": "confirm",
                                   "confirm": ENV}, PHONE)
        self.assertEqual(status, 403)

    def test_loosening_with_desk_needs_the_glob_retyped(self):
        status, body = self.set_mode({"scope": "global", "glob": ENV,
                                      "mode": "confirm", "confirm": "nope"}, DESK)
        self.assertEqual(status, 400)
        self.assertEqual(body["confirm_with"], ENV)
        self.assertEqual(self.global_mode(ENV), "default")

    def test_loosening_with_desk_and_confirm_applies_and_evaluation_honours_it(self):
        status, body = self.set_mode({"scope": "global", "glob": ENV,
                                      "mode": "confirm", "confirm": ENV}, DESK)
        self.assertEqual(status, 200)
        self.assertTrue(body["loosened"])
        self.assertEqual(self.global_mode(ENV), "confirm")
        self.assertEqual(ag.check(str(self.proj / ".env"), "read",
                                  str(self.proj)).kind, "confirm")

    def test_a_project_is_judged_against_what_its_agents_see(self):
        self.set_mode({"scope": "global", "glob": ENV, "mode": "allow",
                       "confirm": ENV}, DESK)
        status, _ = self.set_mode({"scope": "project", "path": str(self.proj),
                                   "glob": ENV, "mode": "confirm"})
        self.assertEqual(status, 200)
        data = self.client.get("/api/hub/access/builtin").get_json()
        self.assertEqual(data["projects"][0]["modes"], {ENV: "confirm"})

    def test_project_refusal_names_the_project_in_the_command(self):
        status, body = self.set_mode({"scope": "project", "path": str(self.proj),
                                      "glob": ENV, "mode": "allow"})
        self.assertEqual(status, 403)
        self.assertIn(f'--project --path "{self.proj}"', body["command"])

    def test_config_without_the_keyring_half_reads_as_default(self):
        (self.home / ".c3" / "config.json").write_text(
            json.dumps({"access": {"builtin_mode": {ENV: "allow"}}}), encoding="utf-8")
        self.assertEqual(self.global_mode(ENV), "default")

    def test_unknown_glob_is_a_400(self):
        status, _ = self.set_mode({"scope": "global", "glob": "*.pem", "mode": "deny"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
