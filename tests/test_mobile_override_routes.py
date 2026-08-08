"""Tests for the mobile gateway's Override Requests surface (spec §8).

These routes are the point where an agent's *message* becomes a *capability*:
a phone tap here mints a real grant that unblocks a real denied tool call. So
the tests below are weighted toward the refusals, not the happy path —
capability-off 404s, unauthenticated 401s, the typed-confirm challenges, the
session-grant switch, and the TTL clamp.

``test_wire_contract_field_names_the_mobile_client_reads`` is the one test
that must never be "fixed" by changing the assertion.
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

os.environ.setdefault("C3_ORACLE_API_KEY", "mobile-override-key")

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import mobile_api  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import override_grants as og  # noqa: E402
from services import override_policy as opol  # noqa: E402
from services import override_requests as orq  # noqa: E402


class _StubScanner:
    def __init__(self, projects):
        self.projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self.projects]


class _Denial:
    """Stand-in for an access_guard.Denial — only the fields classify() reads."""

    def __init__(self, rule, scope="builtin", kind="deny"):
        self.rule = rule
        self.scope = scope
        self.kind = kind
        self.reason = "test"


class _OverrideRouteBase(unittest.TestCase):
    """Per-test tmp project + tmp home, with the request store redirected.

    ``Path.home()`` is patched because BOTH stores hang off it: the Oracle's
    request store (``~/.c3/oracle/override_requests.json``) and the mute store
    beside it. Without this a test run writes into the developer's real vault.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._homedir = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        self.other = Path(self._tmp.name) / "other"
        for p in (self.proj, self.other):
            (p / ".c3").mkdir(parents=True)
        self.home = Path(self._homedir.name)
        (self.home / ".c3").mkdir()

        self._prior_c3_home = os.environ.get("C3_HOME")
        os.environ["C3_HOME"] = str(self.home)

        self._patchers = [
            mock.patch("pathlib.Path.home", return_value=self.home),
            mock.patch.object(ag, "_global_base", return_value=self.home),
        ]
        for p in self._patchers:
            p.start()

        self._prior_cfg = srv._cfg
        srv._cfg = {
            "mobile_api_enabled": True,
            "api_rate_limit_per_min": 0,
            "mobile_security_rate_limit_per_min": 0,
            "api_audit_enabled": True,
            "mobile_override_enabled": True,
            "mobile_override_write": True,
        }
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None

        mobile_api.init_services(scanner=_StubScanner([
            {"path": str(self.proj), "name": "proj", "tags": [],
             "active": False, "has_c3": True, "fact_count": 0},
            {"path": str(self.other), "name": "other", "tags": [],
             "active": False, "has_c3": True, "fact_count": 0},
        ]))
        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()
        self.auth = {"Authorization": "Bearer " + os.environ["C3_ORACLE_API_KEY"]}

        self.write_policy(self.proj)
        self.write_policy(self.other)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        srv._cfg = self._prior_cfg
        if self._prior_c3_home is None:
            os.environ.pop("C3_HOME", None)
        else:
            os.environ["C3_HOME"] = self._prior_c3_home
        self._tmp.cleanup()
        self._homedir.cleanup()

    # helpers ------------------------------------------------------------

    def write_policy(self, project, **over):
        """An `override` section with every escalatable layer ON.

        Tests that care about a layer being off say so explicitly; the default
        here is permissive so each test exercises the ROUTE's refusal rather
        than the policy's.
        """
        section = {
            "enabled": True,
            "layers": {k: True for k in opol.LAYER_KEYS},
            "max_ttl_s": 900,
            "default_uses": 1,
            "request_ttl_s": 600,
            "max_pending_per_session": 20,
            "max_requests_per_hour": 200,
            "allow_session_grants": False,
        }
        section.update(over)
        cfg_file = Path(project) / ".c3" / "config.json"
        cfg = {}
        if cfg_file.is_file():
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        cfg["override"] = section
        cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def get(self, path, **kw):
        return self.client.get(path, headers=self.auth, **kw)

    def post(self, path, payload=None):
        return self.client.post(path, headers=self.auth, json=payload or {})

    def make_request(self, *, project=None, session_id="sess-1", tool="Read",
                     op="read", name=".env.local", rule="**/.env*",
                     rule_class=opol.LAYER_ACCESS_BUILTIN,
                     justification="need it"):
        """Create a real request row through the real service."""
        project = project or self.proj
        target = Path(project) / name
        target.write_text("x", encoding="utf-8")
        scope = {opol.LAYER_ACCESS_BUILTIN: "builtin",
                 opol.LAYER_ACCESS_DENY: "project",
                 opol.LAYER_ACCESS_READONLY: "project"}.get(rule_class, "project")
        kind = "read_only" if rule_class == opol.LAYER_ACCESS_READONLY else "deny"
        return orq.create(
            str(project), session_id=session_id, tool=tool, op=op,
            path=str(target), denial=_Denial(rule, scope=scope, kind=kind),
            justification=justification, refusal="[c3-access:denied] test")


class TestWakeIsNotRemotelyWritable(_OverrideRouteBase):
    """`override.wake` names an argv this machine runs. A token is not hands.

    Every other key on the policy route widens what a tap can APPROVE, and the
    typed-confirm challenge is the right guard for that. This one would decide
    what executes when the tap happens — a different question, with a
    different answer: not from here, at all, confirmed or not.
    """

    def test_policy_set_refuses_wake(self):
        r = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj),
            "override": {"wake": {"command": ["calc.exe"]}},
        })
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["key"], "wake")

    def test_refusal_beats_the_typed_confirmation(self):
        # 'widen' unlocks widenings. It must not unlock this.
        r = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj), "confirm": "widen",
            "override": {"enabled": True,
                         "wake": {"command": ["calc.exe"]}},
        })
        self.assertEqual(r.status_code, 403)
        cfg = json.loads((self.proj / ".c3" / "config.json")
                         .read_text(encoding="utf-8"))
        self.assertNotIn("wake", cfg["override"])

    def test_policy_get_reports_only_whether_one_is_configured(self):
        self.write_policy(self.proj, wake={"command": ["c3-wake", "{message}"],
                                           "cwd": str(self.proj)})
        body = self.get(f"/api/mobile/overrides/policy?project={self.proj}") \
            .get_json()
        self.assertTrue(body["policy"]["wake_configured"])
        self.assertNotIn("wake", body["policy"])
        self.assertNotIn("c3-wake", json.dumps(body))


class TestWireContract(_OverrideRouteBase):
    """The cross-repo contract. See the comment inside the test."""

    def test_wire_contract_field_names_the_mobile_client_reads(self):
        # WHY THIS TEST EXISTS, and why it must not be relaxed:
        #
        # A previous cross-repo feature shipped with BOTH sides green. Each
        # side had tests, and each side's tests pinned that side's OWN
        # spelling of the contract — so the server serialised one set of key
        # names, the client read another, and the wire silently dropped every
        # field in between. Nothing failed; the data just never arrived.
        #
        # These string literals are therefore duplicated ON PURPOSE from
        # docs/override-requests.md §3.3 and §8. Do not replace them with a
        # reference to _OVERRIDE_FIELDS: importing the constant under test
        # would make this assertion tautological and it would go green again
        # the moment someone renames a field on the server side only.
        #
        # If this test fails, the correct fix is almost never to edit this
        # list. It is to change the code back, or to change the spec AND the
        # c3-mobile client in the same breath.
        expected = {
            "id", "project_path", "session_id", "created_at", "expires_at",
            "status", "layer", "rule", "rule_class", "scope", "tool", "op",
            "path", "refusal", "justification", "resolved_at", "decided_by",
            "decision_note",
        }

        row = self.make_request()
        listed = self.get(f"/api/mobile/overrides?project={self.proj}")
        self.assertEqual(listed.status_code, 200, listed.get_data(as_text=True))
        card = listed.get_json()["requests"][0]
        missing = expected - set(card)
        self.assertFalse(
            missing,
            f"GET /overrides dropped fields the phone reads: {sorted(missing)}")

        one = self.get(f"/api/mobile/overrides/{row['id']}")
        self.assertEqual(one.status_code, 200)
        detail = one.get_json()["request"]
        missing = expected - set(detail)
        self.assertFalse(
            missing,
            f"GET /overrides/<id> dropped fields the phone reads: "
            f"{sorted(missing)}")

        # The list envelope key itself is part of the contract.
        self.assertIn("requests", listed.get_json())

        # And the challenge protocol's two keys, spelled exactly (§8).
        blocked = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                            {"decision": "approve"})
        self.assertEqual(blocked.status_code, 400)
        body = blocked.get_json()
        self.assertIs(body.get("needs_confirmation"), True,
                      "the challenge flag must be literally 'needs_confirmation'")
        self.assertIn("confirm_with", body,
                      "the challenge value must be literally 'confirm_with'")

        # decided_by is the field the phone renders as "you approved this".
        approved = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                             {"decision": "approve", "confirm": "**/.env*"})
        self.assertEqual(approved.status_code, 200,
                         approved.get_data(as_text=True))
        self.assertEqual(approved.get_json()["request"]["decided_by"], "mobile")

    def test_path_key_never_crosses_the_wire(self):
        # Internal grant-matching identity. The phone gets `path`; leaking the
        # canonical key would put a normalised local filesystem layout on the
        # network for no client benefit.
        row = self.make_request()
        listed = self.get(f"/api/mobile/overrides?project={self.proj}")
        self.assertNotIn("path_key", listed.get_json()["requests"][0])
        one = self.get(f"/api/mobile/overrides/{row['id']}")
        self.assertNotIn("path_key", one.get_json()["request"])


class TestCapabilityAndAuth(_OverrideRouteBase):

    def test_capability_off_is_404_not_403(self):
        # 404 so a switched-off feature is indistinguishable from a server too
        # old to have it — one client code path handles both (§3.2).
        row = self.make_request()
        srv._cfg["mobile_override_enabled"] = False
        for resp in (self.get("/api/mobile/overrides"),
                     self.get(f"/api/mobile/overrides/{row['id']}"),
                     self.get(f"/api/mobile/overrides/policy?project={self.proj}")):
            self.assertEqual(resp.status_code, 404)

    def test_write_capability_off_is_404_on_mutations_only(self):
        row = self.make_request()
        srv._cfg["mobile_override_write"] = False
        self.assertEqual(self.get("/api/mobile/overrides").status_code, 200)
        self.assertEqual(
            self.post(f"/api/mobile/overrides/{row['id']}/decide",
                      {"decision": "deny"}).status_code, 404)
        self.assertEqual(
            self.post(f"/api/mobile/overrides/{row['id']}/mute").status_code,
            404)
        self.assertEqual(
            self.post("/api/mobile/overrides/policy",
                      {"project": str(self.proj),
                       "override": {"enabled": True}}).status_code, 404)
        # And nothing was decided.
        self.assertEqual(orq.get(row["id"])["status"], orq.STATUS_PENDING)

    def test_capabilities_list_advertises_override(self):
        info = self.get("/api/mobile/info").get_json()
        self.assertIn("override", info["capabilities"])
        self.assertIn("override_write", info["capabilities"])
        srv._cfg["mobile_override_write"] = False
        info = self.get("/api/mobile/info").get_json()
        self.assertIn("override", info["capabilities"])
        self.assertNotIn("override_write", info["capabilities"])

    def test_no_bearer_is_401_on_every_method_including_get(self):
        row = self.make_request()
        for method, path in (
                ("get", "/api/mobile/overrides"),
                ("get", f"/api/mobile/overrides/{row['id']}"),
                ("get", f"/api/mobile/overrides/policy?project={self.proj}"),
                ("post", f"/api/mobile/overrides/{row['id']}/decide"),
                ("post", f"/api/mobile/overrides/{row['id']}/mute"),
                ("post", "/api/mobile/overrides/policy")):
            resp = getattr(self.client, method)(path, json={})
            self.assertEqual(resp.status_code, 401, f"{method} {path}")

    def test_wrong_bearer_is_401_and_decides_nothing(self):
        row = self.make_request()
        resp = self.client.post(
            f"/api/mobile/overrides/{row['id']}/decide",
            headers={"Authorization": "Bearer not-the-key"},
            json={"decision": "approve", "confirm": "**/.env*"})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(orq.get(row["id"])["status"], orq.STATUS_PENDING)
        self.assertEqual(og.active(str(self.proj), session_id="sess-1"), [])

    def test_unknown_project_is_404(self):
        outsider = Path(self._tmp.name) / "nope"
        outsider.mkdir()
        self.assertEqual(
            self.get(f"/api/mobile/overrides?project={outsider}").status_code,
            404)


class TestListing(_OverrideRouteBase):

    def test_newest_first(self):
        first = self.make_request(name="a.env", rule="**/*.env")
        second = self.make_request(name="b.env", rule="**/*.env")
        third = self.make_request(name="c.env", rule="**/*.env")
        ids = [r["id"] for r in
               self.get("/api/mobile/overrides").get_json()["requests"]]
        self.assertEqual(ids[:3], [third["id"], second["id"], first["id"]],
                         "the inbox must be newest-first")

    def test_filter_by_project(self):
        mine = self.make_request(project=self.proj)
        theirs = self.make_request(project=self.other)
        ids = [r["id"] for r in
               self.get(f"/api/mobile/overrides?project={self.proj}")
               .get_json()["requests"]]
        self.assertIn(mine["id"], ids)
        self.assertNotIn(theirs["id"], ids)

    def test_omitting_project_returns_every_project(self):
        # §15.5: the inbox exists to be answered away from the desk, so the
        # default is everything rather than the currently-picked project.
        mine = self.make_request(project=self.proj)
        theirs = self.make_request(project=self.other)
        ids = [r["id"] for r in
               self.get("/api/mobile/overrides").get_json()["requests"]]
        self.assertIn(mine["id"], ids)
        self.assertIn(theirs["id"], ids)

    def test_unregistered_project_rows_are_not_served(self):
        # The store is one file for the whole machine. A row for a project the
        # scanner does not serve must not appear in the cross-project listing.
        row = self.make_request(project=self.other)
        mobile_api.init_services(scanner=_StubScanner([
            {"path": str(self.proj), "name": "proj", "tags": [],
             "active": False, "has_c3": True, "fact_count": 0}]))
        ids = [r["id"] for r in
               self.get("/api/mobile/overrides").get_json()["requests"]]
        self.assertNotIn(row["id"], ids)
        self.assertEqual(
            self.get(f"/api/mobile/overrides/{row['id']}").status_code, 404)

    def test_filter_by_status(self):
        pending = self.make_request(name="p.env", rule="**/*.env")
        denied = self.make_request(name="d.env", rule="**/*.env")
        self.post(f"/api/mobile/overrides/{denied['id']}/decide",
                  {"decision": "deny"})
        ids = [r["id"] for r in
               self.get("/api/mobile/overrides?status=pending")
               .get_json()["requests"]]
        self.assertIn(pending["id"], ids)
        self.assertNotIn(denied["id"], ids)
        ids = [r["id"] for r in
               self.get("/api/mobile/overrides?status=denied")
               .get_json()["requests"]]
        self.assertIn(denied["id"], ids)

    def test_limit_caps_the_page(self):
        for i in range(5):
            self.make_request(name=f"{i}.env", rule="**/*.env")
        body = self.get("/api/mobile/overrides?limit=2").get_json()
        self.assertEqual(len(body["requests"]), 2)
        self.assertEqual(body["limit"], 2)

    def test_get_by_id(self):
        row = self.make_request()
        body = self.get(f"/api/mobile/overrides/{row['id']}").get_json()
        self.assertEqual(body["request"]["id"], row["id"])
        self.assertEqual(body["request"]["justification"], "need it")
        self.assertTrue(body["needs_typed_confirm"])
        self.assertEqual(body["confirm_with"], "**/.env*")

    def test_get_unknown_id_is_404(self):
        self.assertEqual(
            self.get("/api/mobile/overrides/ovr_deadbeef").status_code, 404)


class TestDecide(_OverrideRouteBase):

    def test_approve_mints_a_grant_flips_status_and_notifies(self):
        row = self.make_request(tool="Read", op="read")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "confirm": "**/.env*"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

        # 1. status flipped, attributed to the phone
        stored = orq.get(row["id"])
        self.assertEqual(stored["status"], orq.STATUS_APPROVED)
        self.assertEqual(stored["decided_by"], "mobile")

        # 2. a real grant exists, and it is the one that unblocks THIS call
        grants = og.active(str(self.proj), session_id="sess-1")
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["request_id"], row["id"])
        found = og.find(str(self.proj), session_id="sess-1",
                        layer=row["layer"], rule=row["rule"], tool="Read",
                        op="read", path=row["path"])
        self.assertIsNotNone(found, "the minted grant does not match its request")

        # 3. the audit trail recorded the approval
        events = [json.loads(line)["event"]
                  for line in og.audit_path(str(self.proj))
                  .read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn(og.EV_APPROVED, events)

        # 4. the decision rode the notification feed (§8), and the entry is
        #    acknowledgeable — it carries an id the existing ack route accepts.
        from services.notifications import NotificationStore
        store = NotificationStore(str(self.proj))
        entries = store.get_history(limit=50)
        decided = [n for n in entries
                   if "approved" in str(n.get("title", "")).lower()]
        self.assertTrue(decided, f"no approval entry in the feed: {entries}")
        self.assertTrue(decided[0].get("id"),
                        "the feed entry must be acknowledgeable (needs an id)")
        self.assertTrue(store.acknowledge(decided[0]["id"]))

    def test_deny_flips_status_and_mints_nothing(self):
        row = self.make_request()
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "deny", "note": "not now"})
        self.assertEqual(resp.status_code, 200)
        stored = orq.get(row["id"])
        self.assertEqual(stored["status"], orq.STATUS_DENIED)
        self.assertEqual(stored["decided_by"], "mobile")
        self.assertEqual(stored["decision_note"], "not now")
        self.assertEqual(og.active(str(self.proj), session_id="sess-1"), [])

    def test_deny_needs_no_confirmation(self):
        # §9: deny must always be the cheaper gesture than approve.
        row = self.make_request()
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "deny"})
        self.assertEqual(resp.status_code, 200)

    def test_decide_twice_is_409_not_a_second_grant(self):
        row = self.make_request()
        self.post(f"/api/mobile/overrides/{row['id']}/decide",
                  {"decision": "deny"})
        again = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                          {"decision": "approve", "confirm": "**/.env*"})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(og.active(str(self.proj), session_id="sess-1"), [])

    def test_bad_decision_value_is_400(self):
        row = self.make_request()
        self.assertEqual(
            self.post(f"/api/mobile/overrides/{row['id']}/decide",
                      {"decision": "maybe"}).status_code, 400)


class TestTypedConfirm(_OverrideRouteBase):

    def test_access_builtin_challenge_is_the_rule_glob(self):
        row = self.make_request(rule="**/.env*",
                                rule_class=opol.LAYER_ACCESS_BUILTIN)
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve"})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertTrue(body["needs_confirmation"])
        # The challenge IS the rule glob — not the id, not a nonce, not "yes".
        self.assertEqual(body["confirm_with"], "**/.env*")
        self.assertEqual(orq.get(row["id"])["status"], orq.STATUS_PENDING)

    def test_access_deny_challenge_is_the_rule_glob(self):
        row = self.make_request(name="secrets.txt", rule="**/secrets.txt",
                                rule_class=opol.LAYER_ACCESS_DENY)
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["confirm_with"], "**/secrets.txt")

    def test_wrong_confirm_string_is_refused(self):
        row = self.make_request(rule="**/.env*")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "confirm": "**/.env"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(og.active(str(self.proj), session_id="sess-1"), [])

    def test_correct_confirm_string_approves(self):
        row = self.make_request(rule="**/.env*")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "confirm": "**/.env*"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))

    def test_readonly_layer_needs_no_typed_confirm(self):
        # Only access_deny / access_builtin carry the typed challenge (§8).
        row = self.make_request(name="ro.txt", rule="**/ro.txt",
                                rule_class=opol.LAYER_ACCESS_READONLY,
                                tool="Edit", op="write")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))


class TestSessionGrants(_OverrideRouteBase):

    def test_session_mode_refused_when_allow_session_grants_false(self):
        self.write_policy(self.proj, allow_session_grants=False)
        row = self.make_request(rule="**/.env*")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "mode": "session",
                          "confirm": "**/.env*"})
        self.assertEqual(resp.status_code, 403, resp.get_data(as_text=True))
        self.assertEqual(orq.get(row["id"])["status"], orq.STATUS_PENDING)
        self.assertEqual(og.active(str(self.proj), session_id="sess-1"), [])

    def test_session_mode_allowed_when_switch_is_on(self):
        self.write_policy(self.proj, allow_session_grants=True)
        row = self.make_request(rule="**/.env*")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "mode": "session",
                          "confirm": "**/.env*"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        grants = og.active(str(self.proj), session_id="sess-1")
        self.assertEqual(len(grants), 1)
        self.assertGreater(grants[0]["uses_remaining"], 1,
                           "a session grant should outlast one use")

    def test_session_mode_on_a_plain_layer_needs_its_own_challenge(self):
        self.write_policy(self.proj, allow_session_grants=True)
        row = self.make_request(name="ro.txt", rule="**/ro.txt",
                                rule_class=opol.LAYER_ACCESS_READONLY,
                                tool="Edit", op="write")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "mode": "session"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["confirm_with"], orq.CONFIRM_SESSION)

    def test_unknown_mode_is_400(self):
        row = self.make_request()
        self.assertEqual(
            self.post(f"/api/mobile/overrides/{row['id']}/decide",
                      {"decision": "approve", "mode": "forever",
                       "confirm": "**/.env*"}).status_code, 400)


class TestTtlClamping(_OverrideRouteBase):

    def test_a_week_becomes_the_policy_ceiling_and_says_so(self):
        self.write_policy(self.proj, max_ttl_s=900)
        row = self.make_request(rule="**/.env*")
        week = 7 * 24 * 3600
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "confirm": "**/.env*",
                          "ttl_s": week})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(body["grant"]["ttl_s"], 900)
        self.assertTrue(body["clamped"], "the client was not told about the clamp")
        self.assertIn("900", body["clamped_note"])

        # And the grant on disk really expires within the window.
        grant = og.active(str(self.proj), session_id="sess-1")[0]
        remaining = (og.parse_ts(grant["expires_at"]) - og.now()).total_seconds()
        self.assertLessEqual(remaining, 900 + 5)

    def test_ttl_under_the_ceiling_is_honoured_and_not_flagged(self):
        self.write_policy(self.proj, max_ttl_s=900)
        row = self.make_request(rule="**/.env*")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "confirm": "**/.env*",
                          "ttl_s": 120})
        body = resp.get_json()
        self.assertEqual(body["grant"]["ttl_s"], 120)
        self.assertNotIn("clamped", body)

    def test_project_ceiling_below_the_hard_max_wins(self):
        self.write_policy(self.proj, max_ttl_s=60)
        row = self.make_request(rule="**/.env*")
        body = self.post(f"/api/mobile/overrides/{row['id']}/decide",
                         {"decision": "approve", "confirm": "**/.env*",
                          "ttl_s": 900}).get_json()
        self.assertEqual(body["grant"]["ttl_s"], 60)
        self.assertTrue(body["clamped"])

    def test_non_integer_ttl_is_400(self):
        row = self.make_request()
        self.assertEqual(
            self.post(f"/api/mobile/overrides/{row['id']}/decide",
                      {"decision": "approve", "confirm": "**/.env*",
                       "ttl_s": "soon"}).status_code, 400)


class TestMute(_OverrideRouteBase):

    def test_mute_denies_and_suppresses_an_identical_follow_up(self):
        row = self.make_request(name=".env.local", rule="**/.env*")
        resp = self.post(f"/api/mobile/overrides/{row['id']}/mute")
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()["muted"])
        self.assertEqual(orq.get(row["id"])["status"], orq.STATUS_DENIED)

        # The identical follow-up is refused at creation — it never becomes a
        # card, which is the whole point: no second buzz for the same answer.
        with self.assertRaises(orq.OverrideError) as ctx:
            self.make_request(name=".env.local", rule="**/.env*")
        self.assertIn("muted", str(ctx.exception).lower())

    def test_mute_is_scoped_to_the_session_that_asked(self):
        row = self.make_request(session_id="sess-1", rule="**/.env*")
        self.post(f"/api/mobile/overrides/{row['id']}/mute")
        # A different session has a different problem and may ask once.
        fresh = self.make_request(session_id="sess-2", rule="**/.env*")
        self.assertEqual(fresh["status"], orq.STATUS_PENDING)

    def test_mute_does_not_suppress_a_different_path(self):
        row = self.make_request(name=".env.local", rule="**/.env*")
        self.post(f"/api/mobile/overrides/{row['id']}/mute")
        other = self.make_request(name=".env.prod", rule="**/.env*")
        self.assertEqual(other["status"], orq.STATUS_PENDING)

    def test_mute_mints_no_grant(self):
        row = self.make_request()
        self.post(f"/api/mobile/overrides/{row['id']}/mute")
        self.assertEqual(og.active(str(self.proj), session_id="sess-1"), [])

    def test_mute_on_a_decided_request_is_409(self):
        row = self.make_request()
        self.post(f"/api/mobile/overrides/{row['id']}/decide",
                  {"decision": "deny"})
        self.assertEqual(
            self.post(f"/api/mobile/overrides/{row['id']}/mute").status_code,
            409)


class TestPolicyRoutes(_OverrideRouteBase):

    def test_get_returns_the_effective_section(self):
        self.write_policy(self.proj, enabled=True, max_ttl_s=300)
        body = self.get(
            f"/api/mobile/overrides/policy?project={self.proj}").get_json()
        self.assertTrue(body["policy"]["enabled"])
        self.assertEqual(body["policy"]["max_ttl_s"], 300)
        self.assertIn("layers", body["policy"])
        self.assertIn(opol.LAYER_ACCESS_BUILTIN, body["layers"])

    def test_policy_path_is_not_swallowed_by_the_id_route(self):
        # `/overrides/policy` must reach the policy handler, not be read as a
        # request whose id happens to be "policy".
        resp = self.get(f"/api/mobile/overrides/policy?project={self.proj}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("policy", resp.get_json())

    def test_tightening_needs_no_confirmation(self):
        self.write_policy(self.proj, enabled=True)
        resp = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj), "override": {"enabled": False}})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertFalse(opol.resolve(str(self.proj)).enabled)

    def test_widening_a_layer_needs_typed_confirm(self):
        self.write_policy(self.proj, enabled=True,
                          layers={k: False for k in opol.LAYER_KEYS})
        resp = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj),
            "override": {"layers": {opol.LAYER_ACCESS_BUILTIN: True}}})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertTrue(body["needs_confirmation"])
        self.assertEqual(body["confirm_with"], "widen")
        self.assertIn(f"layers.{opol.LAYER_ACCESS_BUILTIN}", body["widens"])
        # Nothing was written.
        self.assertFalse(
            opol.resolve(str(self.proj)).layers[opol.LAYER_ACCESS_BUILTIN])

    def test_widening_with_confirm_is_written(self):
        self.write_policy(self.proj, enabled=True,
                          layers={k: False for k in opol.LAYER_KEYS})
        resp = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj), "confirm": "widen",
            "override": {"layers": {opol.LAYER_ACCESS_BUILTIN: True}}})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(
            opol.resolve(str(self.proj)).layers[opol.LAYER_ACCESS_BUILTIN])

    def test_raising_a_ceiling_counts_as_widening(self):
        self.write_policy(self.proj, enabled=True, max_ttl_s=60)
        resp = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj), "override": {"max_ttl_s": 900}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("max_ttl_s", resp.get_json()["widens"])

    def test_allow_session_grants_counts_as_widening(self):
        self.write_policy(self.proj, enabled=True, allow_session_grants=False)
        resp = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj),
            "override": {"allow_session_grants": True}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("allow_session_grants", resp.get_json()["widens"])

    def test_unknown_key_is_a_hard_error(self):
        # §3.1: unknown keys never silently no-op.
        resp = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj), "override": {"enabled_pls": True}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("enabled_pls", resp.get_json()["error"])

    def test_unknown_layer_is_a_hard_error(self):
        resp = self.post("/api/mobile/overrides/policy", {
            "project": str(self.proj),
            "override": {"layers": {"root_access": True}}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("root_access", resp.get_json()["error"])


class TestNeverEscalatable(_OverrideRouteBase):

    def test_no_route_mints_a_grant_for_a_vault_file(self):
        # §11 threat 3 / §2 "never" rows: the phone must never be a one-tap
        # path to the credential vault. The request cannot even be created.
        with self.assertRaises(orq.OverrideError):
            orq.create(str(self.proj), session_id="sess-1", tool="Read",
                       op="read",
                       path=str(self.proj / ".c3" / "secrets.enc"),
                       denial=_Denial("**/.c3/secrets.enc"),
                       justification="please")

    def test_endpoint_sweep_no_route_mints_a_grant_without_decide(self):
        # Every non-decide route, hit in sequence, must leave zero grants.
        row = self.make_request()
        self.get("/api/mobile/overrides")
        self.get(f"/api/mobile/overrides/{row['id']}")
        self.get(f"/api/mobile/overrides/policy?project={self.proj}")
        self.post("/api/mobile/overrides/policy",
                  {"project": str(self.proj), "override": {"enabled": True}})
        self.post(f"/api/mobile/overrides/{row['id']}/mute")
        self.assertEqual(og.active(str(self.proj), session_id="sess-1"), [],
                         "a non-decide route minted a grant")


if __name__ == "__main__":
    unittest.main()
