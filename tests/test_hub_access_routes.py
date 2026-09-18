"""Access Guard rules by file type, across projects, from the Hub (2.142.0).

WHY THE HUB. The phone's /api/mobile/access/rule cannot carry a bulk edit:
12 security calls a minute and no global scope, both deliberate for a device
that leaves the building. The per-project server only runs while that
project's UI is open. C3 Desk needs one loopback surface that reads every
project's rules in one call and writes them one rule at a time.

WHAT THESE PIN.
1. The overview never reports "no rules" for a project it could not read,
   and flags a corrupt scope (which evaluates deny-all) as corrupt.
2. Globs come back in the canonical storage form, so a client can match a
   row to the rule remove_rule() will compare against.
3. Adding tightens and needs nothing. Removing a deny rule, or any global
   rule, needs the glob retyped; removing a project read_only/confirm rule
   does not. A refused removal leaves the file untouched.
4. Every real write is audited on the target; a no-op is not.
5. A folder C3 does not manage gets a 409, not a fresh .c3/config.json.
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
from services import access_guard as ag  # noqa: E402


class _StubPM:
    def __init__(self, projects):
        self._projects = projects

    def list_projects(self):
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
        _write_cfg(self.proj, {"access": {"deny": ["secrets\\**"],
                                          "read_only": ["**/*.lock"]}})
        self.other = root / "other"
        _write_cfg(self.other, {})
        self.bare = root / "bare"
        self.bare.mkdir()
        self.home = root / "home"
        (self.home / ".c3").mkdir(parents=True)
        self._gbase = mock.patch.object(ag, "_global_base", return_value=self.home)
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
            hub_server, "_resolve_project_path", side_effect=lambda p: Path(p))
        self._resolve.start()

    def tearDown(self):
        self._resolve.stop()
        self._pm.stop()
        self._gbase.stop()
        self._tmp.cleanup()

    def post(self, url, body):
        resp = self.client.post(url, json=body)
        return resp.status_code, resp.get_json()


class TestOverview(_Base):
    def _overview(self):
        resp = self.client.get("/api/hub/access/overview")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def _row(self, data, name):
        return next(r for r in data["projects"] if r["name"] == name)

    def test_rows_carry_each_projects_own_rules_in_storage_form(self):
        row = self._row(self._overview(), "proj")
        self.assertEqual(row["rules"]["deny"], ["secrets/**"])
        self.assertEqual(row["rules"]["read_only"], ["**/*.lock"])
        self.assertEqual(row["rules"]["confirm"], [])
        self.assertFalse(row["rules"]["corrupt"])

    def test_uninitialized_and_unreadable_rows_are_not_reported_as_no_rules(self):
        real = ag.scope_rules

        def flaky(scope, path="."):
            if scope == "project" and Path(path) == self.other:
                raise RuntimeError("boom")
            return real(scope, path)

        with mock.patch.object(ag, "scope_rules", side_effect=flaky):
            data = self._overview()
        self.assertFalse(self._row(data, "bare")["initialized"])
        self.assertEqual(self._row(data, "other")["error"], "boom")
        self.assertEqual(self._row(data, "proj")["rules"]["deny"], ["secrets/**"])

    def test_a_corrupt_scope_is_flagged(self):
        _write_cfg(self.other, {"access": {"allow": ["**"]}})
        self.assertTrue(self._row(self._overview(), "other")["rules"]["corrupt"])

    def test_global_and_builtin(self):
        _write_cfg(self.home, {"access": {"confirm": ["**/*.sql"]}})
        data = self._overview()
        self.assertEqual(data["global"]["confirm"], ["**/*.sql"])
        self.assertIn("**/.env*", data["builtin"]["deny"])
        self.assertEqual(data["kinds"], ["confirm", "read_only", "deny"])
        self.assertIn("NOT enforced", data["coverage_note"])


class TestAdd(_Base):
    def test_add_writes_and_audits_on_the_target(self):
        with mock.patch("services.activity_log.ActivityLog") as log:
            status, body = self.post("/api/hub/access/rule", {
                "path": str(self.other), "glob": "**\\*.pem", "kind": "deny"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["rule"], {"glob": "**/*.pem", "kind": "deny",
                                        "scope": "project", "added": True})
        self.assertEqual(_cfg(self.other)["access"]["deny"], ["**/*.pem"])
        event, payload = log.return_value.log.call_args[0]
        self.assertEqual((event, payload["action"], payload["via"]),
                         ("access_action", "add", "hub"))

    def test_duplicate_is_a_noop_and_not_audited(self):
        with mock.patch("services.activity_log.ActivityLog") as log:
            status, body = self.post("/api/hub/access/rule", {
                "path": str(self.proj), "glob": "SECRETS/**", "kind": "deny"})
        self.assertEqual(status, 200)
        self.assertFalse(body["rule"]["added"])
        self.assertFalse(log.called)

    def test_global_scope_needs_no_path(self):
        status, body = self.post("/api/hub/access/rule", {
            "scope": "global", "glob": "**/*.key", "kind": "read_only"})
        self.assertEqual(status, 200, body)
        self.assertEqual(_cfg(self.home)["access"]["read_only"], ["**/*.key"])

    def test_refusals(self):
        self.assertEqual(self.post("/api/hub/access/rule", {
            "path": str(self.proj), "glob": "x/**", "kind": "allow"})[0], 400)
        self.assertEqual(self.post("/api/hub/access/rule", {
            "glob": "x/**", "kind": "deny"})[0], 400)
        self.assertEqual(self.post("/api/hub/access/rule", {
            "scope": "machine", "glob": "x/**", "kind": "deny"})[0], 400)
        status, body = self.post("/api/hub/access/rule", {
            "path": str(self.bare), "glob": "x/**", "kind": "deny"})
        self.assertEqual(status, 409)
        self.assertFalse((self.bare / ".c3").exists(), "it created .c3 anyway")


class TestRemove(_Base):
    URL = "/api/hub/access/rule/remove"

    def test_project_read_only_removal_needs_no_confirmation(self):
        status, body = self.post(self.URL, {
            "path": str(self.proj), "glob": "**/*.lock", "kind": "read_only"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["removed"])
        self.assertEqual(_cfg(self.proj)["access"]["read_only"], [])

    def test_deny_removal_needs_the_glob_retyped(self):
        status, body = self.post(self.URL, {
            "path": str(self.proj), "glob": "secrets/**", "kind": "deny"})
        self.assertEqual(status, 400)
        self.assertEqual((body["needs_confirmation"], body["confirm_with"]),
                         (True, "secrets/**"))
        self.assertEqual(_cfg(self.proj)["access"]["deny"], ["secrets\\**"],
                         "it wrote despite refusing")
        with mock.patch("services.activity_log.ActivityLog") as log:
            status, body = self.post(self.URL, {
                "path": str(self.proj), "glob": "secrets/**", "kind": "deny",
                "confirm": "secrets\\**"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["removed"])
        self.assertEqual(_cfg(self.proj)["access"]["deny"], [])
        self.assertEqual(log.return_value.log.call_args[0][1]["action"], "remove")

    def test_any_global_removal_needs_the_glob_retyped(self):
        _write_cfg(self.home, {"access": {"confirm": ["**/*.sql"]}})
        body = {"scope": "global", "glob": "**/*.sql", "kind": "confirm"}
        self.assertEqual(self.post(self.URL, body)[0], 400)
        status, res = self.post(self.URL, {**body, "confirm": "**/*.sql"})
        self.assertEqual(status, 200, res)
        self.assertEqual(_cfg(self.home)["access"]["confirm"], [])

    def test_a_corrupt_scope_is_refused_not_rewritten(self):
        _write_cfg(self.other, {"access": {"allow": ["**"], "read_only": ["a/**"]}})
        status, _ = self.post(self.URL, {
            "path": str(self.other), "glob": "a/**", "kind": "read_only"})
        self.assertEqual(status, 400)
        self.assertEqual(_cfg(self.other)["access"]["allow"], ["**"])


class TestRoutes(unittest.TestCase):
    def test_each_route_resolves_to_its_own_endpoint(self):
        adapter = hub_server.app.url_map.bind("localhost")
        for url, method, endpoint in (
                ("/api/hub/access/overview", "GET", "api_hub_access_overview"),
                ("/api/hub/access/rule", "POST", "api_hub_access_rule_add"),
                ("/api/hub/access/rule/remove", "POST",
                 "api_hub_access_rule_remove")):
            self.assertEqual(adapter.match(url, method=method)[0], endpoint)


if __name__ == "__main__":
    unittest.main()
