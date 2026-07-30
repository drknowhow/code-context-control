"""Tests for the hub's Agent Locks routes (docs/agent-locks.md §7).

Two things these pin that a screenshot cannot:
  - a project we could not READ must not be reported as a project with zero
    leases ("all clear" is a different claim from "we don't know");
  - force-release must bump the fencing counter, or a holder that comes back
    is still considered current and the override achieved nothing.
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
from services import agent_locks as al  # noqa: E402


class _StubPM:
    def __init__(self, projects):
        self._projects = projects

    def list_projects(self):
        return self._projects


class TestHubLocksRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        self.bare = Path(self._tmp.name) / "bare"   # registered, no .c3
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

    def _lease(self, relpath="services/router.py", session="s1", intent="refactor"):
        return al.store_for(self.proj).acquire(
            [relpath], agent_id=al.agent_id_for(session), session_id=session,
            intent=intent)

    # ── overview ─────────────────────────────────────────────────────────

    def test_overview_lists_live_leases(self):
        self._lease()
        body = self.client.get("/api/hub/locks/overview").get_json()
        self.assertEqual(body["total"], 1)
        row = next(r for r in body["projects"] if r["name"] == "proj")
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["locks"][0]["agent_id"], "claude-code:s1")
        self.assertEqual(row["locks"][0]["intent"], "refactor")
        self.assertEqual(row["mode"], "advisory")

    def test_uninitialized_project_is_flagged_not_silently_empty(self):
        body = self.client.get("/api/hub/locks/overview").get_json()
        row = next(r for r in body["projects"] if r["name"] == "bare")
        self.assertFalse(row["initialized"])
        self.assertEqual(row["count"], 0)

    def test_disabled_locks_are_reported(self):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"locks": {"enabled": False}}), encoding="utf-8")
        body = self.client.get("/api/hub/locks/overview").get_json()
        row = next(r for r in body["projects"] if r["name"] == "proj")
        self.assertFalse(row["enabled"])

    def test_overview_carries_the_coverage_caveat(self):
        """The UI must be able to state its own limits rather than implying a
        lease covers everything (spec §9)."""
        body = self.client.get("/api/hub/locks/overview").get_json()
        self.assertIn("not covered", body["coverage_note"])

    def test_one_bad_project_does_not_sink_the_page(self):
        self._lease()
        (self.proj / ".c3" / "locks.json").write_text("{ not json", encoding="utf-8")
        body = self.client.get("/api/hub/locks/overview").get_json()
        self.assertEqual(len(body["projects"]), 2)

    # ── force-release ────────────────────────────────────────────────────

    def test_force_release_removes_the_lease_and_bumps_fencing(self):
        self._lease()
        before = al.store_for(self.proj).snapshot()["fencing"]
        res = self.client.post("/api/projects/locks/force-release", json={
            "path": str(self.proj), "relpath": "services/router.py",
            "note": "test"}).get_json()
        self.assertTrue(res["forced"])
        self.assertEqual(res["previous_owner"], "claude-code:s1")
        snap = al.store_for(self.proj).snapshot()
        self.assertEqual(snap["count"], 0)
        self.assertGreater(snap["fencing"], before,
                           "without a fencing bump a returning holder still "
                           "looks current and the override achieved nothing")

    def test_force_release_requires_both_params(self):
        for payload in ({"path": str(self.proj)}, {"relpath": "a.py"}, {}):
            r = self.client.post("/api/projects/locks/force-release", json=payload)
            self.assertEqual(r.status_code, 400)

    def test_force_release_rejects_an_unsupported_path(self):
        r = self.client.post("/api/projects/locks/force-release", json={
            "path": str(self.proj), "relpath": "//server/share/a.py"})
        self.assertEqual(r.status_code, 400)


class TestMainViewAcceptsLocks(unittest.TestCase):
    def test_locks_is_a_valid_main_view(self):
        hub_server.app.config["TESTING"] = True
        client = hub_server.app.test_client()
        r = client.post("/api/hub/config", json={"main_view": "locks"})
        self.assertEqual(r.status_code, 200)

    def test_unknown_main_view_still_rejected(self):
        hub_server.app.config["TESTING"] = True
        client = hub_server.app.test_client()
        r = client.post("/api/hub/config", json={"main_view": "nonsense"})
        self.assertEqual(r.status_code, 400)


class TestBundleIncludesComponent(unittest.TestCase):
    def test_hub_locks_js_is_served(self):
        """A component missing from the bundle list renders as a blank tab with
        no error — the failure mode is silence, so assert it explicitly."""
        path = REPO_ROOT / "cli" / "hub_ui" / "components" / "hub_locks.js"
        self.assertTrue(path.is_file())
        src = (REPO_ROOT / "cli" / "hub_server.py").read_text(encoding="utf-8")
        self.assertIn("hub_ui/components/hub_locks.js", src)


if __name__ == "__main__":
    unittest.main()
