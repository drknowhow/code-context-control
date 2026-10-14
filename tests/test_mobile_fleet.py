"""Gateway API v7: the fleet views C3 Desk reads from the Hub, for the phone.

What these pin, beyond "the route answers":

- every view is cross-project but never names a project the scanner does not
  register, and goes dark (404 + gone from /info) with its subsystem switch;
- what the phone may change is narrower than Desk: built-in guards only
  tighten (a looser mode is a 403 for EVERY token kind, Desk's included),
  "inherit" asks for the typed word when it loosens, locks stay read-only;
- the session catalog carries no transcript text unless
  mobile_sessions_transcripts is on, and never offers to resume on the PC;
- /now reports heartbeats and tokens without leaking the unlocked-path list
  that sits in the same enforcement_state.json.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("C3_ORACLE_API_KEY", "mobile-fleet-key")

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import client_tokens, mobile_api  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import enforcement_policy as ep  # noqa: E402
from services import fleet_overview, session_live  # noqa: E402
from services import override_policy as opol  # noqa: E402
from services import session_catalog as sc  # noqa: E402

ENV = "**/.env*"
GIT = "**/.git/**"


class _StubScanner:
    def __init__(self, projects):
        self.projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self.projects]


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, account, value):
        self.store[(service, account)] = value

    def get_password(self, service, account):
        return self.store.get((service, account))


def _entry(path, has_c3=True):
    return {"path": str(path), "name": Path(path).name, "tags": [],
            "active": False, "has_c3": has_c3, "fact_count": 0}


class FleetBase(unittest.TestCase):
    """Two registered projects, one ghost the scanner never lists, an isolated
    home (global realm, keyring) and every switch at its shipped default."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        (self.home / ".c3").mkdir(parents=True)
        self.proj = root / "proj"
        self.other = root / "other"
        self.ghost = root / "ghost"
        for p in (self.proj, self.other, self.ghost):
            (p / ".c3").mkdir(parents=True)

        fake = _FakeKeyring()
        keyring = types.ModuleType("keyring")
        keyring.set_password, keyring.get_password = fake.set_password, fake.get_password
        self._patches = [
            mock.patch.dict(sys.modules, {"keyring": keyring}),
            mock.patch.object(ag, "_global_base", return_value=self.home),
            mock.patch.object(opol, "_global_base", return_value=self.home),
            mock.patch.dict(os.environ, {"C3_HOME": str(self.home)}),
        ]
        for p in self._patches:
            p.start()

        self._prior_cfg = srv._cfg
        srv._cfg = {
            "mobile_api_enabled": True,
            "api_rate_limit_per_min": 0,
            "mobile_security_rate_limit_per_min": 0,
            "api_audit_enabled": False,
            "mobile_access_enabled": True,
            "mobile_access_write": True,
            "mobile_access_global_scope": False,
            "mobile_override_enabled": True,
            "mobile_override_write": True,
            "mobile_builtin_enabled": True,
            "mobile_builtin_tighten": True,
            "mobile_sessions_enabled": True,
            "mobile_sessions_write": True,
            "mobile_sessions_transcripts": False,
            "hub_url": "http://127.0.0.1:9",
        }
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None
        mobile_api.init_services(scanner=_StubScanner(
            [_entry(self.proj), _entry(self.other)]))
        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()
        self.auth = {"Authorization": "Bearer " + os.environ["C3_ORACLE_API_KEY"]}

    def tearDown(self):
        srv._cfg = self._prior_cfg
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def get(self, path, **params):
        r = self.client.get(path, headers=self.auth, query_string=params)
        return r.status_code, r.get_json()

    def post(self, path, body, headers=None):
        r = self.client.post(path, json=body, headers=headers or self.auth)
        return r.status_code, r.get_json()

    def paths(self, rows):
        return {str(Path(r["path"]).resolve()) for r in rows}


class TestCapabilities(FleetBase):
    NEW = ("locks_overview", "access_overview", "enforcement_overview",
           "enforcement_clear", "override_policy_overview",
           "override_policy_clear", "builtin", "builtin_tighten",
           "sessions", "sessions_write", "now")

    def test_v7_advertises_every_fleet_capability(self):
        _, info = self.get("/api/mobile/info")
        self.assertEqual(info["api_version"], 7)
        for cap in self.NEW:
            self.assertIn(cap, info["capabilities"])

    def test_each_view_goes_dark_with_its_subsystem(self):
        srv._cfg["mobile_access_enabled"] = False
        srv._cfg["mobile_override_enabled"] = False
        srv._cfg["mobile_builtin_enabled"] = False
        srv._cfg["mobile_sessions_enabled"] = False
        _, info = self.get("/api/mobile/info")
        for cap in ("access_overview", "enforcement_overview",
                    "override_policy_overview", "builtin", "sessions"):
            self.assertNotIn(cap, info["capabilities"])
        for path in ("/api/mobile/access/overview",
                     "/api/mobile/enforcement/overview",
                     "/api/mobile/overrides/policy/overview",
                     "/api/mobile/access/builtin",
                     "/api/mobile/sessions/overview"):
            self.assertEqual(self.get(path)[0], 404, path)
        # Ops reads like /locks and /status have no switch.
        self.assertIn("locks_overview", info["capabilities"])
        self.assertIn("now", info["capabilities"])

    def test_every_new_route_needs_the_bearer(self):
        for path in ("/api/mobile/locks/overview", "/api/mobile/now",
                     "/api/mobile/sessions", "/api/mobile/access/builtin"):
            self.assertEqual(self.client.get(path).status_code, 401, path)


class TestLocksOverview(FleetBase):
    def test_lists_registered_projects_and_their_leases_only(self):
        from services import agent_locks as al
        al.store_for(str(self.proj)).acquire(["src/a.py"], agent_id="agent-1",
                                             session_id="s1", intent="refactor")
        al.store_for(str(self.ghost)).acquire(["x.py"], agent_id="agent-2",
                                              session_id="s2")
        status, body = self.get("/api/mobile/locks/overview")
        self.assertEqual(status, 200)
        self.assertEqual(self.paths(body["projects"]),
                         {str(self.proj.resolve()), str(self.other.resolve())})
        self.assertEqual(body["total"], 1)
        row = next(r for r in body["projects"] if r["count"])
        self.assertEqual(row["locks"][0]["agent_id"], "agent-1")
        self.assertIn("coverage_note", body)

    def test_there_is_no_force_release_on_the_phone(self):
        rules = [str(r) for r in srv.app.url_map.iter_rules()
                 if str(r).startswith("/api/mobile/")]
        self.assertFalse([r for r in rules
                          if "force-release" in r or r.endswith("/release")])


class TestAccessOverview(FleetBase):
    def test_project_rules_and_writable_scopes(self):
        ag.set_rule("**/*.pem", "deny", "project", str(self.proj))
        status, body = self.get("/api/mobile/access/overview")
        self.assertEqual(status, 200)
        row = next(r for r in body["projects"]
                   if Path(r["path"]).resolve() == self.proj.resolve())
        self.assertIn("**/*.pem", row["rules"]["deny"])
        self.assertEqual(body["writable"], {"project": True, "global": False})
        srv._cfg["mobile_access_global_scope"] = True
        self.assertTrue(self.get("/api/mobile/access/overview")[1]
                        ["writable"]["global"])
        srv._cfg["mobile_access_write"] = False
        self.assertEqual(self.get("/api/mobile/access/overview")[1]["writable"],
                         {"project": False, "global": False})


class TestEnforcement(FleetBase):
    def test_overview_reports_each_project_and_never_global_writable(self):
        ep.set_mode("advisory", str(self.proj), set_by=ep.SET_BY_USER,
                    scope="project")
        status, body = self.get("/api/mobile/enforcement/overview")
        self.assertEqual(status, 200)
        modes = {Path(r["path"]).name: r["mode"] for r in body["projects"]}
        self.assertEqual(modes["proj"], "advisory")
        self.assertFalse(body["writable"]["global"])

    def test_clear_inherits_again(self):
        ep.set_mode("advisory", str(self.proj), set_by=ep.SET_BY_USER,
                    scope="project")
        status, body = self.post("/api/mobile/enforcement/clear",
                                 {"project": str(self.proj)})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["cleared"])
        self.assertEqual(ep.resolve(str(self.proj)).mode or ep.DEFAULT_MODE,
                         body["mode"])

    def test_clear_that_would_inherit_off_needs_the_word(self):
        ep.set_mode("off", ".", set_by=ep.SET_BY_USER, scope="global")
        ep.set_mode("strict", str(self.proj), set_by=ep.SET_BY_USER,
                    scope="project")
        status, body = self.post("/api/mobile/enforcement/clear",
                                 {"project": str(self.proj)})
        self.assertEqual(status, 400)
        self.assertEqual(body["confirm_with"], "off")
        self.assertEqual(ep.resolve(str(self.proj)).mode, "strict")
        status, body = self.post("/api/mobile/enforcement/clear",
                                 {"project": str(self.proj), "confirm": "off"})
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "off")

    def test_clear_refuses_an_unregistered_project(self):
        status, _ = self.post("/api/mobile/enforcement/clear",
                              {"project": str(self.ghost)})
        self.assertEqual(status, 404)


class TestOverridePolicy(FleetBase):
    def test_overview_and_clear(self):
        opol.write_section(self.proj, {"enabled": False})
        status, body = self.get("/api/mobile/overrides/policy/overview")
        self.assertEqual(status, 200)
        row = next(r for r in body["projects"]
                   if Path(r["path"]).resolve() == self.proj.resolve())
        self.assertIn("enabled", row["project_keys"])
        self.assertFalse(body["writable"]["global"])
        self.assertNotIn("wake", body["defaults"])

        status, body = self.post("/api/mobile/overrides/policy/clear",
                                 {"project": str(self.proj)})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["cleared"])

    def test_clear_that_widens_needs_widen(self):
        # Global turns the feature on; the project had pinned it off.
        gcfg = self.home / ".c3" / "config.json"
        gcfg.write_text(json.dumps({"override": {"enabled": True}}),
                        encoding="utf-8")
        opol.write_section(self.proj, {"enabled": False})
        self.assertTrue(opol.resolve_global().enabled)
        status, body = self.post("/api/mobile/overrides/policy/clear",
                                 {"project": str(self.proj)})
        self.assertEqual(status, 400, body)
        self.assertEqual(body.get("confirm_with"), "widen")
        status, body = self.post("/api/mobile/overrides/policy/clear",
                                 {"project": str(self.proj), "confirm": "widen"})
        self.assertEqual(status, 200, body)


class TestBuiltinGuards(FleetBase):
    def mode(self, glob, scope="project"):
        modes = (ag._scope_modes("global") if scope == "global"
                 else ag.effective_builtin_modes(str(self.proj)))
        return modes.get(glob, "default")

    def test_read_lists_guards_and_says_loosen_is_never_offered(self):
        status, body = self.get("/api/mobile/access/builtin")
        self.assertEqual(status, 200)
        globs = {g["glob"] for g in body["guards"]}
        self.assertIn(ENV, globs)
        self.assertFalse(body["writable"]["loosen"])
        self.assertFalse(body["writable"]["tighten_global"])

    def test_tightening_a_project_guard_applies(self):
        status, body = self.post("/api/mobile/access/builtin/mode", {
            "scope": "project", "project": str(self.proj),
            "glob": GIT, "mode": "deny"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["loosened"])
        self.assertEqual(self.mode(GIT), "deny")

    def test_loosening_is_refused_for_every_token_kind(self):
        desk = {"Authorization": "Bearer desk-token"}
        with mock.patch.object(client_tokens, "principal_for",
                               side_effect=lambda t: {"kind": "desk",
                                                      "client_id": "desk-1"}
                               if t == "desk-token" else None):
            for headers in (self.auth, desk):
                status, body = self.post("/api/mobile/access/builtin/mode", {
                    "scope": "project", "project": str(self.proj),
                    "glob": ENV, "mode": "allow", "confirm": ENV}, headers)
                self.assertEqual(status, 403, body)
                self.assertTrue(body["needs_desk"])
                self.assertIn("c3 access builtin mode", body["command"])
        self.assertEqual(self.mode(ENV), "default")

    def test_global_scope_needs_the_global_switch_even_to_tighten(self):
        status, _ = self.post("/api/mobile/access/builtin/mode", {
            "scope": "global", "glob": GIT, "mode": "deny"})
        self.assertEqual(status, 403)
        srv._cfg["mobile_access_global_scope"] = True
        status, body = self.post("/api/mobile/access/builtin/mode", {
            "scope": "global", "glob": GIT, "mode": "deny"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.mode(GIT, "global"), "deny")

    def test_tighten_switch_off_leaves_the_read(self):
        srv._cfg["mobile_builtin_tighten"] = False
        self.assertEqual(self.get("/api/mobile/access/builtin")[0], 200)
        status, _ = self.post("/api/mobile/access/builtin/mode", {
            "scope": "project", "project": str(self.proj),
            "glob": GIT, "mode": "deny"})
        self.assertEqual(status, 404)

    def test_ending_a_lease_early(self):
        from services import builtin_leases
        lease = builtin_leases.mint(ENV, "confirm", scope="project",
                                    project_path=str(self.proj), ttl_s=600,
                                    created_by="desk-1")
        status, body = self.get("/api/mobile/access/builtin")
        self.assertIn(lease["id"], {row["id"] for row in body["leases"]})
        status, body = self.post("/api/mobile/access/builtin/lease/revoke",
                                 {"id": lease["id"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["revoked"], lease["id"])
        status, _ = self.post("/api/mobile/access/builtin/lease/revoke",
                              {"id": lease["id"]})
        self.assertEqual(status, 404)

    def test_a_lease_on_an_unregistered_project_is_not_revocable_here(self):
        from services import builtin_leases
        lease = builtin_leases.mint(ENV, "confirm", scope="project",
                                    project_path=str(self.ghost), ttl_s=600,
                                    created_by="desk-1")
        status, _ = self.post("/api/mobile/access/builtin/lease/revoke",
                              {"id": lease["id"]})
        self.assertEqual(status, 404)
        self.assertIn(lease["id"], {r["id"] for r in builtin_leases.active()})


def _row(project, **over):
    row = {
        "id": "11111111-2222-3333-4444-555555555555", "short": "11111111",
        "provider": "claude", "project": {"name": project.name,
                                          "path": str(project)},
        "title": "fix the parser", "title_source": "ai",
        "first_prompt": "my first prompt", "last_prompt": "my last prompt",
        "started": "2026-10-14T10:00:00+00:00",
        "last_active": "2026-10-14T11:00:00+00:00", "branch": "main",
        "live": False, "stale": None, "hints": [], "note": None,
        "links": {"c3_sessions": [], "snapshots": 0, "tasks": 0,
                  "decisions": 0, "predecessor": "", "successor": ""},
        "resume": {"command": "claude --resume 1111", "cwd": str(project),
                   "remote_url": "https://claude.ai/code/session_abc",
                   "can_launch": True, "why_not": ""},
    }
    row.update(over)
    return row


class TestSessions(FleetBase):
    def test_list_withholds_transcript_text_by_default(self):
        rows = [_row(self.proj),
                _row(self.proj, id="9" * 36, title="my first prompt",
                     title_source="first_prompt")]
        with mock.patch.object(sc, "_build", return_value=(rows, {})):
            status, body = self.get("/api/mobile/sessions",
                                    project=str(self.proj))
        self.assertEqual(status, 200, body)
        self.assertFalse(body["transcripts"])
        for row in body["sessions"]:
            self.assertEqual(row["first_prompt"], "")
            self.assertEqual(row["last_prompt"], "")
            self.assertEqual(set(row["resume"]),
                             {"remote_url", "can_launch", "why_not"})
            self.assertFalse(row["resume"]["can_launch"])
        titles = {r["title_source"]: r["title"] for r in body["sessions"]}
        self.assertEqual(titles["ai"], "fix the parser")
        self.assertEqual(titles["withheld"], "(untitled)")
        self.assertEqual(body["sessions"][0]["resume"]["remote_url"],
                         "https://claude.ai/code/session_abc")

    def test_the_switch_lets_prompts_through(self):
        srv._cfg["mobile_sessions_transcripts"] = True
        with mock.patch.object(sc, "_build",
                               return_value=([_row(self.proj)], {})):
            _, body = self.get("/api/mobile/sessions", project=str(self.proj))
        self.assertTrue(body["transcripts"])
        self.assertEqual(body["sessions"][0]["first_prompt"], "my first prompt")

    def test_detail_drops_the_preview_by_default(self):
        detail = dict(_row(self.proj), decisions=[], snapshot=None, tasks=[],
                      preview=[{"role": "user", "text": "secret turn"}])
        with mock.patch.object(sc, "get_session", return_value=detail):
            status, body = self.get("/api/mobile/sessions/detail",
                                    project=str(self.proj), id="11111111")
        self.assertEqual(status, 200)
        self.assertEqual(body["preview"], [])
        self.assertNotIn("secret turn", json.dumps(body))

    def test_search_never_matches_hidden_text(self):
        rows = [_row(self.proj),
                _row(self.proj, id="9" * 36, title="deploy the parser",
                     title_source="first_prompt", first_prompt="deploy the parser")]
        with mock.patch.object(sc, "_build", return_value=(rows, {})):
            # "first" is only in a prompt; "deploy" only in a first-prompt title.
            for q in ("first", "last", "deploy"):
                _, body = self.get("/api/mobile/sessions",
                                   project=str(self.proj), q=q)
                self.assertEqual(body["sessions"], [], q)
            # Visible text still matches.
            _, body = self.get("/api/mobile/sessions", project=str(self.proj),
                               q="parser main")
            self.assertEqual(len(body["sessions"]), 1)
            srv._cfg["mobile_sessions_transcripts"] = True
            _, body = self.get("/api/mobile/sessions", project=str(self.proj),
                               q="first")
            self.assertEqual(len(body["sessions"]), 1)

    def test_search_across_projects_respects_the_switch_too(self):
        rows = [_row(self.proj)]
        with mock.patch.object(sc, "_build", return_value=(rows, {})):
            _, body = self.get("/api/mobile/sessions", q="first")
        self.assertEqual(body["sessions"], [])

    def test_overview_withholds_a_real_first_prompt_title(self):
        # Unmocked session_catalog.overview: newest must carry title_source
        # for the withholding to fire at all.
        tdir = Path(self._tmp.name) / "transcripts"
        tdir.mkdir()
        transcript = tdir / "abc.jsonl"
        transcript.write_text("{}\n", encoding="utf-8")
        meta = {"id": "abc", "title": "my secret first prompt",
                "title_source": "first_prompt"}
        with mock.patch.object(sc, "claude_transcript_dir", return_value=tdir), \
                mock.patch.object(sc, "_transcript_files",
                                  return_value=[transcript]), \
                mock.patch.object(sc, "_cached", return_value=meta):
            status, body = self.get("/api/mobile/sessions/overview")
        self.assertEqual(status, 200, body)
        self.assertNotIn("my secret first prompt", json.dumps(body))
        newest = body["projects"][0]["newest"]
        self.assertEqual(newest["title"], "(untitled)")

    def test_unregistered_project_is_a_404(self):
        status, _ = self.get("/api/mobile/sessions", project=str(self.ghost))
        self.assertEqual(status, 404)

    def test_bad_stale_filter_is_a_400(self):
        self.assertEqual(self.get("/api/mobile/sessions", stale="nope")[0], 400)

    def test_mark_records_a_user_mark(self):
        seen = {}

        def fake_mark(project, ref, op, **kw):
            seen.update(project=project, ref=ref, op=op, **kw)
            return {"mark": {"op": op, "by": kw["by"]},
                    "row": _row(self.proj), "warning": ""}

        with mock.patch.object(sc, "mark", side_effect=fake_mark):
            status, body = self.post("/api/mobile/sessions/mark", {
                "project": str(self.proj), "id": "11111111", "op": "stale",
                "reason": "superseded"})
        self.assertEqual(status, 200, body)
        self.assertEqual(seen["by"], "user")
        self.assertEqual(seen["op"], "stale")
        self.assertEqual(body["row"]["first_prompt"], "")

    def test_mark_switch_off_is_a_404(self):
        srv._cfg["mobile_sessions_write"] = False
        status, _ = self.post("/api/mobile/sessions/mark", {
            "project": str(self.proj), "id": "11111111", "op": "stale"})
        self.assertEqual(status, 404)

    def test_overview_counts_registered_projects(self):
        with mock.patch.object(sc, "overview", return_value={
                "counts": {"total": 3, "live": 1, "stale": 0, "idle": 1},
                "last_active": "2026-10-14T11:00:00+00:00",
                "newest": {"id": "x", "title": "my first prompt",
                           "title_source": "first_prompt"},
                "transcripts": True}):
            status, body = self.get("/api/mobile/sessions/overview")
        self.assertEqual(status, 200)
        self.assertEqual(self.paths(body["projects"]),
                         {str(self.proj.resolve()), str(self.other.resolve())})
        self.assertEqual(body["projects"][0]["newest"]["title"], "(untitled)")
        self.assertNotIn("resume", body["features"])


class TestNow(FleetBase):
    def test_live_session_with_tokens_and_last_tool(self):
        host = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        session_live.beat(str(self.proj), "c3-session-1", host_session_id=host,
                          ide="claude-code")
        c3 = self.proj / ".c3"
        (c3 / "session_stats.jsonl").write_text(
            json.dumps({"session_id": host, "input_tokens": 10,
                        "output_tokens": 100, "model": "m1"}) + "\n"
            + json.dumps({"session_id": host, "input_tokens": 20,
                          "output_tokens": 300, "model": "m1"}) + "\n",
            encoding="utf-8")
        (c3 / "enforcement_state.json").write_text(json.dumps({
            "session_id": host,
            "last_c3_call": {"ts": "2026-10-14T12:00:00+00:00",
                             "tool": "c3_shell"},
            "unlocked_files": {"secret/path.py": ["edit"]}}), encoding="utf-8")
        (c3 / "map.meta.json").write_text(json.dumps(
            {"generated_at": "2026-10-14T11:00:00+00:00"}), encoding="utf-8")

        status, body = self.get("/api/mobile/now")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["live_sessions"], 1)
        self.assertNotIn("secret/path.py", json.dumps(body))
        project = body["projects"][0]
        self.assertEqual(Path(project["path"]).resolve(), self.proj.resolve())
        s = project["sessions"][0]
        self.assertEqual(s["host_session_id"], host)
        self.assertEqual(s["last_tool"], "c3_shell")
        # Newest row, never a sum: rows are cumulative per session.
        self.assertEqual(s["tokens_out"], 300)
        self.assertEqual(s["tokens_total"], 320)
        self.assertEqual(project["map"]["state"], "fresh")
        self.assertEqual(body["services"]["oracle"]["api_version"], 7)
        self.assertFalse(body["services"]["hub"]["available"])

    def test_quiet_projects_and_ghosts_are_not_listed(self):
        session_live.beat(str(self.ghost), "ghost-session")
        status, body = self.get("/api/mobile/now")
        self.assertEqual(status, 200)
        self.assertEqual(body["projects"], [])
        self.assertEqual(body["live_sessions"], 0)

    def test_running_jobs_list_their_project(self):
        job = {"id": "j1", "project_path": str(self.other), "status": "running",
               "cmd_display": "pytest", "created_at": "2026-10-14T12:00:00Z"}
        body = fleet_overview.now([{"name": "other", "path": str(self.other)}],
                                  job_rows=[job], ci_runs=lambda p, limit: [])
        self.assertEqual(body["running_jobs"], 1)
        self.assertEqual(body["projects"][0]["running_jobs"][0]["id"], "j1")

    def test_a_stale_heartbeat_is_not_live(self):
        session_live.beat(str(self.proj), "old-session")
        path = session_live.heartbeat_path(str(self.proj), "old-session")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["ts"] = time.time() - 3600
        path.write_text(json.dumps(data), encoding="utf-8")
        _, body = self.get("/api/mobile/now")
        self.assertEqual(body["live_sessions"], 0)


if __name__ == "__main__":
    unittest.main()
