"""The Hub's session-history routes (2.143.0).

WHAT THESE PIN.
1. Every route resolves its project against the REGISTRY: an unregistered
   path is 404 and an uninitialized one is 409 — the catalog, and above all
   the terminal, are never pointed at an arbitrary folder.
2. Resume spawns exactly ``["claude", "--resume", <uuid>]`` in the session's
   directory; nothing from the request body reaches the command line; a live
   session is 409 and an unknown id 404, with no terminal opened.
3. The overview isolates projects row by row and advertises ``features`` so
   an older Desk can probe capability.
4. A mark from the Hub is recorded as ``by: user``.
5. ``main_view`` accepts every top-bar tab (ci/tokens/access used to 400).
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
from services import session_catalog as sc  # noqa: E402
from services import terminal_launch  # noqa: E402
from tests.session_fixtures import U1, U2, SessionFixture  # noqa: E402


class _StubPM:
    def __init__(self, projects):
        self._projects = projects

    def list_registered(self):
        return self._projects


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = SessionFixture(Path(self._tmp.name))
        self._env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.fx.claude)})
        self._env.start()
        self._git = mock.patch.object(sc, "_local_branches", return_value=None)
        self._git.start()
        self.proj = self.fx.project("proj")
        self.other = self.fx.project("other")
        self.bare = self.fx.project("bare", init=False)
        self.fx.transcript(self.proj, U1, title="Login bug", bridge="cse_ABC")
        self.fx.transcript(self.other, U2, title="Docs pass")
        hub_server.app.config["TESTING"] = True
        self.client = hub_server.app.test_client()
        self._pm = mock.patch.object(hub_server, "_pm", return_value=_StubPM([
            {"name": "proj", "path": str(self.proj)},
            {"name": "other", "path": str(self.other)},
            {"name": "bare", "path": str(self.bare)},
        ]))
        self._pm.start()
        self._spawn = mock.patch.object(terminal_launch, "spawn_terminal")
        self.spawn = self._spawn.start()

    def tearDown(self):
        self._spawn.stop()
        self._pm.stop()
        self._git.stop()
        self._env.stop()
        self._tmp.cleanup()


class TestReads(_Base):
    def test_overview_rows_and_features(self):
        res = self.client.get("/api/hub/sessions/overview")
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertIn("resume", body["features"])
        by_name = {r["name"]: r for r in body["projects"]}
        self.assertEqual(by_name["proj"]["counts"]["total"], 1)
        self.assertEqual(by_name["proj"]["newest"]["title"], "Login bug")
        self.assertFalse(by_name["bare"]["initialized"])

    def test_list_one_project_and_all(self):
        one = self.client.get("/api/hub/sessions", query_string={"path": str(self.proj)})
        self.assertEqual([r["id"] for r in one.get_json()["sessions"]], [U1])
        allp = self.client.get("/api/hub/sessions").get_json()
        self.assertEqual(sorted(r["id"] for r in allp["sessions"]), sorted([U1, U2]))

    def test_list_validates_and_resolves_against_the_registry(self):
        self.assertEqual(self.client.get("/api/hub/sessions",
                                         query_string={"stale": "nope"}).status_code, 400)
        self.assertEqual(self.client.get("/api/hub/sessions",
                                         query_string={"limit": "x"}).status_code, 400)
        stranger = self.fx.project("stranger")
        self.assertEqual(self.client.get("/api/hub/sessions",
                                         query_string={"path": str(stranger)}).status_code, 404)
        self.assertEqual(self.client.get("/api/hub/sessions",
                                         query_string={"path": str(self.bare)}).status_code, 409)

    def test_detail(self):
        res = self.client.get("/api/hub/sessions/detail",
                              query_string={"path": str(self.proj), "id": U1[:8]})
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertEqual(body["id"], U1)
        self.assertEqual(body["resume"]["remote_url"], "https://claude.ai/code/session_ABC")
        self.assertTrue(body["preview"])
        miss = self.client.get("/api/hub/sessions/detail",
                               query_string={"path": str(self.proj), "id": U2})
        self.assertEqual(miss.status_code, 404)


class TestWrites(_Base):
    def test_mark_is_recorded_as_user(self):
        res = self.client.post("/api/hub/sessions/mark", json={
            "path": str(self.proj), "id": U1, "op": "stale", "reason": "done elsewhere"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["mark"]["by"], "user")
        hidden = self.client.get("/api/hub/sessions", query_string={"path": str(self.proj)})
        self.assertEqual(hidden.get_json()["sessions"], [])
        bad = self.client.post("/api/hub/sessions/mark",
                               json={"path": str(self.proj), "id": U1, "op": "delete"})
        self.assertEqual(bad.status_code, 400)

    def test_resume_spawns_the_fixed_argv(self):
        res = self.client.post("/api/hub/sessions/resume", json={
            "path": str(self.proj), "id": U1[:8], "argv": ["calc.exe"], "cmd": "rm -rf /"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.spawn.assert_called_once()
        cwd, argv = self.spawn.call_args[0]
        self.assertEqual(argv, ["claude", "--resume", U1])
        self.assertTrue(Path(cwd).samefile(self.proj))
        log = (self.proj / ".c3" / "activity_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"session_resume"', log)

    def test_resume_refusals_open_nothing(self):
        self.fx.heartbeat(self.proj, "c3live", U1)
        live = self.client.post("/api/hub/sessions/resume",
                                json={"path": str(self.proj), "id": U1})
        self.assertEqual(live.status_code, 409)
        foreign = self.client.post("/api/hub/sessions/resume",
                                   json={"path": str(self.proj), "id": U2})
        self.assertEqual(foreign.status_code, 404)
        unregistered = self.client.post("/api/hub/sessions/resume",
                                        json={"path": str(self.fx.project("x")), "id": U1})
        self.assertEqual(unregistered.status_code, 404)
        self.spawn.assert_not_called()

    def test_spawn_failure_is_reported_with_the_command(self):
        self.spawn.side_effect = FileNotFoundError("no terminal")
        res = self.client.post("/api/hub/sessions/resume",
                               json={"path": str(self.proj), "id": U1})
        self.assertEqual(res.status_code, 500)
        self.assertEqual(res.get_json()["command"], f"claude --resume {U1}")


class TestMainView(unittest.TestCase):
    def test_every_top_bar_tab_persists(self):
        hub_server.app.config["TESTING"] = True
        client = hub_server.app.test_client()
        saved = {}
        with mock.patch.object(hub_server, "_read_hub_config", return_value={}), \
                mock.patch.object(hub_server, "_write_hub_config",
                                  side_effect=lambda cfg: saved.update(cfg)):
            for view in ("ci", "tokens", "access", "sessions", "projects"):
                res = client.post("/api/hub/config", json={"main_view": view})
                self.assertEqual(res.status_code, 200, view)
                self.assertEqual(saved["main_view"], view)
            self.assertEqual(client.post("/api/hub/config",
                                         json={"main_view": "nope"}).status_code, 400)
        topbar = (REPO_ROOT / "cli" / "hub_ui" / "components" / "topbar.js").read_text(
            encoding="utf-8")
        for view in hub_server._MAIN_VIEWS:
            self.assertIn(f"['{view}',", topbar, f"top bar has no '{view}' tab")


if __name__ == "__main__":
    unittest.main()
