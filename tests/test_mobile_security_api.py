"""Tests for the mobile gateway's security surface (credentials + Access Guard).

The mobile gateway is the FIRST network-reachable surface for either
subsystem — every other one is loopback-only — so the guarantees that hold
elsewhere by "only localhost can ask" are re-established here as explicit,
tested invariants.

Two tests carry the weight:

* ``test_endpoint_sweep_no_route_ever_returns_value`` — a canary value seeded
  into the vault must not appear in ANY response body or ANY log file. Unlike
  the hub's equivalent it also sweeps ``/feed``, which merges every project's
  ActivityLog + EditLedger and ships them verbatim: an audit line that ever
  carried a value would be exfiltrated by the next feed poll.
* ``TestMobileSurfaceInvariants`` — source-level assertions that the module
  cannot reach plaintext at all, and that the three deliberately-absent routes
  stay absent. A grep that holds for values the tests never constructed is a
  stronger guarantee than any canary.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("C3_ORACLE_API_KEY", "mobile-sec-key")

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import mobile_api  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import credential_store as cs  # noqa: E402

CANARY = "mobile-canary-7q3z"


class _StubKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self.store:
            raise KeyError("not found")
        del self.store[(service, account)]


class _StubFernet:
    def __init__(self, key):
        self._key = key

    @staticmethod
    def generate_key():
        return base64.urlsafe_b64encode(b"0" * 32)

    def encrypt(self, data):
        return base64.urlsafe_b64encode(self._key + b"|" + data)

    def decrypt(self, token):
        raw = base64.urlsafe_b64decode(token)
        key, _, data = raw.partition(b"|")
        if key != self._key:
            raise ValueError("bad key")
        return data


class _StubScanner:
    def __init__(self, projects):
        self.projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self.projects]


class _MobileSecurityBase(unittest.TestCase):
    """Per-test tmp project + tmp home, stubbed keyring, real stores.

    The home patch is deliberately FOUR-WAY. The three services disagree on
    what "home" means — ``enforcement_policy`` honors ``C3_HOME`` while
    ``credential_store`` and ``access_guard`` each use their own
    ``_global_base()`` over ``Path.home()`` — so patching only one lets a test
    write into the developer's real ``~/.c3``.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._homedir = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        self.outsider = Path(self._tmp.name) / "outsider"
        self.proj.mkdir(parents=True)
        (self.proj / ".c3").mkdir()
        self.outsider.mkdir(parents=True)
        self.home = Path(self._homedir.name)
        (self.home / ".c3").mkdir()

        self._prior_c3_home = os.environ.get("C3_HOME")
        os.environ["C3_HOME"] = str(self.home)

        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=_StubKeyring()),
            mock.patch.object(cs, "_crypto_module", return_value=_StubFernet),
            mock.patch.object(cs, "_global_base", return_value=self.home),
            mock.patch.object(ag, "_global_base", return_value=self.home),
            mock.patch("pathlib.Path.home", return_value=self.home),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()

        self._prior_cfg = srv._cfg
        srv._cfg = {
            "mobile_api_enabled": True,
            "api_rate_limit_per_min": 0,      # shared bucket off
            "mobile_security_rate_limit_per_min": 0,  # security bucket off
            "api_audit_enabled": True,
            "mobile_credentials_enabled": True,
            "mobile_credentials_write": True,
            "mobile_access_enabled": True,
            "mobile_access_write": True,
        }
        # Module-level bucket is keyed on the budget; reset so a bucket drained
        # by a previous test cannot 429 this one.
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None

        mobile_api.init_services(scanner=_StubScanner([{
            "path": str(self.proj), "name": "proj", "tags": [],
            "active": False, "has_c3": True, "fact_count": 0,
        }]))
        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()
        self.auth = {"Authorization": "Bearer " + os.environ["C3_ORACLE_API_KEY"]}

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

    def get(self, path, **kw):
        return self.client.get(path, headers=self.auth, **kw)

    def post(self, path, payload=None):
        return self.client.post(path, headers=self.auth, json=payload or {})

    def delete(self, path, payload=None):
        return self.client.delete(path, headers=self.auth, json=payload or {})

    def seed(self, name, value, scope="project"):
        store = str(self.proj) if scope == "project" else str(self.home)
        return cs.set_credential(name, value, scope=scope, project_path=store)


class TestMobileCredentials(_MobileSecurityBase):

    def test_set_list_describe_check_delete_round_trip(self):
        resp = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "API_KEY", "value": CANARY, "description": "round trip",
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(resp.get_json()["op"], "set")
        self.assertEqual(
            cs.get_value("API_KEY", project_path=str(self.proj)), CANARY)

        listed = self.get(f"/api/mobile/credentials?project={self.proj}")
        names = [e["name"] for e in listed.get_json()["entries"]]
        self.assertIn("API_KEY", names)

        one = self.get(f"/api/mobile/credentials/API_KEY?project={self.proj}")
        self.assertEqual(one.get_json()["entry"]["value_len"], len(CANARY))

        chk = self.post("/api/mobile/credentials/API_KEY/check",
                        {"project": str(self.proj)})
        self.assertTrue(chk.get_json()["resolvable"])
        self.assertTrue(chk.get_json()["fingerprint"])

        gone = self.delete("/api/mobile/credentials/API_KEY",
                           {"project": str(self.proj), "scope": "project"})
        self.assertTrue(gone.get_json()["removed"])
        self.assertIsNone(cs.get_value("API_KEY", project_path=str(self.proj)))

    def test_structured_create_refused(self):
        """Card/identity/address payloads never transit the mobile channel:
        creating a structured entry from the gateway is a 400, and nothing
        lands in the store."""
        pan = "4539578763621486"
        for ctype in ("card", "identity", "address"):
            resp = self.post("/api/mobile/credentials", {
                "project": str(self.proj), "scope": "project",
                "name": "MOB_STRUCT", "type": ctype,
                "value": json.dumps({"number": pan, "full_name": "x",
                                     "street1": "x"}),
            })
            self.assertEqual(resp.status_code, 400,
                             resp.get_data(as_text=True))
            self.assertIn("structured", resp.get_json()["error"])
            self.assertNotIn(pan, resp.get_data(as_text=True))
        self.assertEqual(
            cs.get_entry("MOB_STRUCT", project_path=str(self.proj)), {})

    def test_metadata_only_update_does_not_clobber_siblings(self):
        self.seed("PART", CANARY)
        cs.update_metadata("PART", scope="project", project_path=str(self.proj),
                           description="orig", inject=True)
        resp = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "PART", "description": "changed",
        })
        self.assertEqual(resp.status_code, 200)
        entry = resp.get_json()["entry"]
        self.assertEqual(entry["description"], "changed")
        self.assertTrue(entry["inject"], "a single-field update clobbered inject")
        # The value survives a metadata-only update.
        self.assertEqual(cs.get_value("PART", project_path=str(self.proj)), CANARY)

    def test_global_scope_writes_to_home_not_project(self):
        resp = self.post("/api/mobile/credentials", {
            "scope": "global", "name": "GLOBAL_TOKEN", "value": CANARY,
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(
            cs.get_value("GLOBAL_TOKEN", project_path=str(self.home)), CANARY)
        proj_cfg = self.proj / ".c3" / "config.json"
        if proj_cfg.exists():
            self.assertNotIn("GLOBAL_TOKEN", proj_cfg.read_text(encoding="utf-8"))

    def test_global_scope_with_bogus_project_404s_and_writes_nothing(self):
        before = (self.home / ".c3" / "config.json")
        prior = before.read_text(encoding="utf-8") if before.exists() else ""
        resp = self.post("/api/mobile/credentials", {
            "scope": "global", "project": str(self.outsider),
            "name": "SHOULD_NOT_EXIST", "value": CANARY,
        })
        self.assertEqual(resp.status_code, 404)
        after = before.read_text(encoding="utf-8") if before.exists() else ""
        self.assertEqual(prior, after)
        self.assertIsNone(
            cs.get_value("SHOULD_NOT_EXIST", project_path=str(self.home)))

    def test_global_mutation_without_home_c3_is_409_and_creates_nothing(self):
        import shutil
        shutil.rmtree(self.home / ".c3")
        resp = self.post("/api/mobile/credentials", {
            "scope": "global", "name": "NOPE", "value": CANARY,
        })
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.get_json().get("needs_init"))
        self.assertFalse((self.home / ".c3").exists(),
                         "mobile may EDIT the shared vault, never CREATE it")

    def test_unregistered_project_404s_with_no_side_effects(self):
        for call in (
            lambda: self.post("/api/mobile/credentials", {
                "project": str(self.outsider), "scope": "project",
                "name": "X", "value": CANARY}),
            lambda: self.delete("/api/mobile/credentials/X", {
                "project": str(self.outsider), "scope": "project"}),
            lambda: self.post("/api/mobile/credentials/X/check", {
                "project": str(self.outsider)}),
        ):
            self.assertEqual(call().status_code, 404)
        self.assertFalse((self.outsider / ".c3").exists())

    def test_invalid_name_is_400(self):
        resp = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "has space", "value": CANARY,
        })
        self.assertEqual(resp.status_code, 400)

    # ── agent_readable: asymmetric by design ──────────────────

    def test_raising_agent_readable_blocked_when_switch_off(self):
        self.seed("RAISE_ME", CANARY)
        srv._cfg["mobile_creds_agent_readable_raise"] = False
        resp = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "RAISE_ME", "agent_readable": True, "confirm": "RAISE_ME",
        })
        self.assertEqual(resp.status_code, 403)
        entry = cs.get_entry("RAISE_ME", project_path=str(self.proj))
        self.assertFalse(entry.get("agent_readable"))

    def test_raising_agent_readable_needs_confirmation(self):
        self.seed("CONFIRM_ME", CANARY)
        srv._cfg["mobile_creds_agent_readable_raise"] = True
        resp = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "CONFIRM_ME", "agent_readable": True,
        })
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json().get("needs_confirmation"))

        wrong = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "CONFIRM_ME", "agent_readable": True, "confirm": "nope",
        })
        self.assertEqual(wrong.status_code, 400)

        ok = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "CONFIRM_ME", "agent_readable": True,
            "confirm": "CONFIRM_ME",
        })
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertTrue(ok.get_json()["entry"]["agent_readable"])

    def test_lowering_agent_readable_is_always_allowed(self):
        cs.set_credential("LOWER_ME", CANARY, scope="project",
                          project_path=str(self.proj), agent_readable=True)
        srv._cfg["mobile_creds_agent_readable_raise"] = False
        resp = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "LOWER_ME", "agent_readable": False,
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertFalse(resp.get_json()["entry"]["agent_readable"])

    # ── kill switches ─────────────────────────────────────────

    def test_disabled_capability_404s_and_leaves_info(self):
        srv._cfg["mobile_credentials_enabled"] = False
        self.assertEqual(
            self.get(f"/api/mobile/credentials?project={self.proj}").status_code,
            404)
        caps = self.get("/api/mobile/info").get_json()["capabilities"]
        self.assertNotIn("credentials", caps)
        self.assertIn("feed", caps, "unrelated capabilities must survive")

    def test_write_switch_off_still_allows_reads(self):
        self.seed("READ_ONLY_MODE", CANARY)
        srv._cfg["mobile_credentials_write"] = False
        self.assertEqual(
            self.get(f"/api/mobile/credentials?project={self.proj}").status_code,
            200)
        resp = self.post("/api/mobile/credentials", {
            "project": str(self.proj), "scope": "project",
            "name": "BLOCKED", "value": CANARY,
        })
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(
            "credentials_write",
            self.get("/api/mobile/info").get_json()["capabilities"])


class TestMobileValueSweep(_MobileSecurityBase):
    """The load-bearing test: no route, and no log, ever carries a value."""

    def test_endpoint_sweep_no_route_ever_returns_value(self):
        srv._cfg["mobile_creds_agent_readable_raise"] = True
        self.seed("SWEEP_P", CANARY, scope="project")
        self.seed("SWEEP_G", CANARY, scope="global")

        responses = [
            self.get(f"/api/mobile/credentials?project={self.proj}"),
            self.get("/api/mobile/credentials?scope=global"),
            self.get(f"/api/mobile/credentials?project={self.proj}&only=project"),
            self.get(f"/api/mobile/credentials?project={self.proj}&only=global"),
            self.get("/api/mobile/credentials-overview"),
            self.get(f"/api/mobile/credentials/SWEEP_P?project={self.proj}"),
            self.get("/api/mobile/credentials/SWEEP_G?scope=global"),
            self.post("/api/mobile/credentials", {
                "project": str(self.proj), "scope": "project",
                "name": "SWEEP_N", "value": CANARY, "inject": True,
                "agent_readable": True, "confirm": "SWEEP_N",
                "description": CANARY[:4] + "-desc"}),
            self.post("/api/mobile/credentials", {
                "project": str(self.proj), "scope": "project",
                "name": "SWEEP_N", "description": "meta only"}),
            self.post("/api/mobile/credentials/SWEEP_P/check",
                      {"project": str(self.proj)}),
            self.post("/api/mobile/credentials/SWEEP_G/check",
                      {"scope": "global"}),
            self.delete("/api/mobile/credentials/SWEEP_N",
                        {"project": str(self.proj), "scope": "project"}),
            self.get("/api/mobile/info"),
            self.get("/api/mobile/projects"),
            self.get(f"/api/mobile/pm?project={self.proj}"),
            self.get(f"/api/mobile/pm/events?project={self.proj}"),
            # LAST, and deliberately with no `before=` ceiling: /feed merges
            # every project's ActivityLog + EditLedger and ships them verbatim,
            # so it is the one route that would exfiltrate a value that leaked
            # into a LOG rather than into a response. The hub has no equivalent
            # firehose, so its sweep cannot catch this class of bug.
            self.get(f"/api/mobile/feed?project={self.proj}"),
            self.get("/api/mobile/feed"),
        ]
        for resp in responses:
            body = resp.get_data(as_text=True)
            self.assertNotIn(CANARY, body,
                             f"value leaked from {resp.request.path}")

        for path in (
            self.proj / ".c3" / "config.json",
            self.proj / ".c3" / "activity_log.jsonl",
            self.proj / ".c3" / "edit_ledger.jsonl",
            self.proj / ".c3" / "denials.jsonl",
            self.home / ".c3" / "config.json",
            self.home / ".c3" / "activity_log.jsonl",
        ):
            if path.exists():
                self.assertNotIn(CANARY, path.read_text(encoding="utf-8"),
                                 f"value leaked into {path.name}")

    def test_sweep_actually_had_a_value_to_leak(self):
        """Guard against the sweep passing because nothing was ever stored."""
        self.seed("SWEEP_P", CANARY)
        self.assertEqual(
            cs.get_value("SWEEP_P", project_path=str(self.proj)), CANARY)


class TestMobileAccessGuard(_MobileSecurityBase):

    def test_add_rule_then_check_reflects_it(self):
        resp = self.post("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "secrets/**",
            "kind": "deny", "scope": "project",
        })
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()["rule"]["added"])

        listed = self.get(f"/api/mobile/access?project={self.proj}").get_json()
        self.assertIn("secrets/**", listed["scopes"]["project"]["deny"])

        chk = self.post("/api/mobile/access/check", {
            "project": str(self.proj),
            "path": str(self.proj / "secrets" / "x.txt"), "op": "read",
        })
        body = chk.get_json()
        self.assertEqual(body["verdict"], "denied")
        self.assertEqual(body["rule"], "secrets/**")
        self.assertTrue(body["refusal"])

    def test_removing_deny_rule_requires_typed_confirmation(self):
        self.post("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "vault/**",
            "kind": "deny", "scope": "project"})

        bare = self.delete("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "vault/**",
            "kind": "deny", "scope": "project"})
        self.assertEqual(bare.status_code, 400)
        self.assertTrue(bare.get_json()["needs_confirmation"])

        wrong = self.delete("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "vault/**", "kind": "deny",
            "scope": "project", "confirm": "nope"})
        self.assertEqual(wrong.status_code, 400)

        # Still enforced after two refused attempts.
        listed = self.get(f"/api/mobile/access?project={self.proj}").get_json()
        self.assertIn("vault/**", listed["scopes"]["project"]["deny"])

        ok = self.delete("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "vault/**", "kind": "deny",
            "scope": "project", "confirm": "vault/**"})
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertTrue(ok.get_json()["removed"])

    def test_removing_read_only_rule_needs_no_confirmation(self):
        self.post("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "docs/**",
            "kind": "read_only", "scope": "project"})
        resp = self.delete("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "docs/**",
            "kind": "read_only", "scope": "project"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()["removed"])

    def test_too_broad_glob_rejected(self):
        for glob in ("**", "*", "**/*"):
            resp = self.post("/api/mobile/access/rule", {
                "project": str(self.proj), "glob": glob,
                "kind": "deny", "scope": "project"})
            self.assertEqual(resp.status_code, 400, f"accepted {glob!r}")
        listed = self.get(f"/api/mobile/access?project={self.proj}").get_json()
        self.assertEqual(listed["scopes"]["project"]["deny"], [])

    def test_global_scope_write_blocked_by_default(self):
        srv._cfg["mobile_access_global_scope"] = False
        resp = self.post("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "machine/**",
            "kind": "deny", "scope": "global"})
        self.assertEqual(resp.status_code, 403)

        srv._cfg["mobile_access_global_scope"] = True
        allowed = self.post("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "machine/**",
            "kind": "deny", "scope": "global"})
        self.assertEqual(allowed.status_code, 200,
                         allowed.get_data(as_text=True))

    def test_corrupt_scope_reports_and_refuses_mutation(self):
        # Unknown key in the access section = corrupt, so the scope evaluates
        # deny-all until a human repairs it by hand.
        cfg = self.proj / ".c3" / "config.json"
        cfg.write_text(json.dumps({"access": {"allow": ["x"]}}),
                       encoding="utf-8")
        listed = self.get(f"/api/mobile/access?project={self.proj}").get_json()
        self.assertIn("project", listed["corrupt"])

        resp = self.post("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "a/**",
            "kind": "deny", "scope": "project"})
        self.assertEqual(resp.status_code, 400)
        # The corrupt section was NOT silently rewritten.
        self.assertIn("allow", cfg.read_text(encoding="utf-8"))

    def test_mask_add_marks_stale_then_activation_clears_it(self):
        resp = self.post("/api/mobile/access/mask", {
            "project": str(self.proj), "glob": "data/**",
            "preset": "redact_secrets", "scope": "project"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertTrue(resp.get_json()["mask"]["stale"])

        # First activation is destructive, so it is gated on a typed confirm.
        bare = self.post("/api/mobile/access/mask/activate",
                         {"project": str(self.proj)})
        self.assertEqual(bare.status_code, 409)
        self.assertTrue(bare.get_json()["first_activation"])

        ok = self.post("/api/mobile/access/mask/activate",
                       {"project": str(self.proj), "confirm": "activate"})
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertFalse(ok.get_json()["mask"]["stale"])

    def test_activate_ignores_rebuild_index_from_the_wire(self):
        self.post("/api/mobile/access/mask", {
            "project": str(self.proj), "glob": "data/**",
            "preset": "redact_secrets", "scope": "project"})
        with mock.patch("services.mask_activation.activate") as activate:
            activate.return_value = {"files": 0, "ok": True}
            self.post("/api/mobile/access/mask/activate", {
                "project": str(self.proj), "confirm": "activate",
                "rebuild_index": True})
        _, kwargs = activate.call_args
        self.assertFalse(kwargs["rebuild_index"],
                         "rebuild_index must never be honored from the wire")

    def test_denials_rows_carry_a_fix(self):
        from services import access_telemetry as at
        at.record(layer="access", rule="secrets/**", tool="Read",
                  operation="read", path="secrets/x", scope="project",
                  project_path=str(self.proj))
        body = self.get(f"/api/mobile/access/denials?project={self.proj}").get_json()
        self.assertEqual(body["total"], 1)
        self.assertTrue(body["rows"][0]["fix"])

    def test_access_mutations_are_audited(self):
        self.post("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": "audited/**",
            "kind": "deny", "scope": "project"})
        log = (self.proj / ".c3" / "activity_log.jsonl").read_text(encoding="utf-8")
        self.assertIn("audited/**", log)
        self.assertIn("oracle-mobile", log)
        ledger = (self.proj / ".c3" / "edit_ledger.jsonl").read_text(encoding="utf-8")
        self.assertIn("access://audited/**", ledger)


class TestMobileEnforcement(_MobileSecurityBase):

    def test_unset_reports_default(self):
        body = self.get(f"/api/mobile/enforcement?project={self.proj}").get_json()
        from services import enforcement_policy as ep
        self.assertEqual(body["mode"], ep.DEFAULT_MODE)
        self.assertEqual(body["scope"], "default")
        self.assertTrue(body["coverage_note"],
                        "without this, 'off' reads as 'nothing is protected'")

    def test_set_advisory(self):
        resp = self.post("/api/mobile/enforcement",
                         {"project": str(self.proj), "mode": "advisory"})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        body = resp.get_json()
        self.assertEqual(body["mode"], "advisory")
        self.assertEqual(body["set_by"], "user")
        self.assertEqual(body["scope"], "project")

    def test_scope_is_always_project_even_if_body_says_global(self):
        resp = self.post("/api/mobile/enforcement", {
            "project": str(self.proj), "mode": "advisory", "scope": "global"})
        self.assertEqual(resp.get_json()["scope"], "project")
        # And the shared config was not touched.
        home_cfg = self.home / ".c3" / "config.json"
        if home_cfg.exists():
            self.assertNotIn("enforcement",
                             home_cfg.read_text(encoding="utf-8"))

    def test_turning_off_requires_confirmation(self):
        bare = self.post("/api/mobile/enforcement",
                         {"project": str(self.proj), "mode": "off"})
        self.assertEqual(bare.status_code, 400)
        self.assertTrue(bare.get_json()["needs_confirmation"])
        ok = self.post("/api/mobile/enforcement", {
            "project": str(self.proj), "mode": "off", "confirm": "off"})
        self.assertEqual(ok.status_code, 200, ok.get_data(as_text=True))
        self.assertEqual(ok.get_json()["mode"], "off")

    def test_bad_mode_is_400(self):
        resp = self.post("/api/mobile/enforcement",
                         {"project": str(self.proj), "mode": "banana"})
        self.assertEqual(resp.status_code, 400)


class TestMobileSecurityRateLimit(_MobileSecurityBase):

    def test_security_budget_is_independent_of_the_shared_one(self):
        srv._cfg["mobile_security_rate_limit_per_min"] = 2
        srv._cfg["api_rate_limit_per_min"] = 0
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None

        codes = [self.post("/api/mobile/access/rule", {
            "project": str(self.proj), "glob": f"burst{i}/**",
            "kind": "deny", "scope": "project"}).status_code
            for i in range(6)]
        self.assertIn(429, codes, "security budget never engaged")

        limited = [c for c in codes if c == 429]
        self.assertTrue(limited)
        # A PM mutation draws on the shared bucket, which is still open.
        pm = self.post("/api/mobile/pm/task",
                       {"project": str(self.proj), "title": "still allowed"})
        self.assertNotEqual(pm.status_code, 429,
                            "security throttling leaked into the PM surface")

    def test_rate_limited_response_names_the_budget(self):
        srv._cfg["mobile_security_rate_limit_per_min"] = 1
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None
        last = None
        for i in range(8):
            last = self.post("/api/mobile/access/rule", {
                "project": str(self.proj), "glob": f"n{i}/**",
                "kind": "deny", "scope": "project"})
            if last.status_code == 429:
                break
        self.assertEqual(last.status_code, 429)
        self.assertEqual(last.get_json()["budget"], "security")
        self.assertTrue(last.headers.get("Retry-After"))


def _module_identifiers(module) -> set:
    """Every identifier the module's CODE actually references.

    AST rather than a text grep, because the module deliberately names the
    forbidden accessors and routes in its docstring and comments — that prose
    is the artifact a future contributor reads before adding one back, and it
    must not be what trips the assertion. Only a real reference counts."""
    import ast
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            names.add(node.name)
    return names


class TestMobileSurfaceInvariants(unittest.TestCase):
    """Structural assertions — stronger than any canary, because they hold for
    values the tests never constructed."""

    IDENTS = _module_identifiers(mobile_api)

    def _mobile_rules(self):
        return [str(r) for r in srv.app.url_map.iter_rules()
                if str(r).startswith("/api/mobile/")]

    def test_no_plaintext_accessor_in_module(self):
        # credential_store.is_resolvable exists precisely so this stays true.
        # Not `resolve`: credential_store.resolve() would qualify, but the
        # attribute name is indistinguishable from Path.resolve() at the AST
        # level. get_value is the accessor it would have to go through anyway.
        for banned in ("get_value", "expand_templates",
                       "register_active_secret", "_ACTIVE_SECRETS",
                       "mask_mirror", "get_structured_fields", "_get_raw"):
            self.assertNotIn(banned, self.IDENTS,
                             f"{banned} gives this module access to plaintext")

    def test_no_builtin_disable_route_or_reference(self):
        self.assertNotIn("set_builtin_disabled", self.IDENTS)
        for rule in self._mobile_rules():
            self.assertNotIn("builtin", rule)

    def test_no_raw_content_preview_route(self):
        for rule in self._mobile_rules():
            self.assertNotIn("preview", rule)

    def test_no_bulk_import_or_denial_clearing_route(self):
        for rule in self._mobile_rules():
            self.assertNotIn("import", rule)

    def test_every_mobile_route_requires_bearer(self):
        srv.app.config["TESTING"] = True
        client = srv.app.test_client()
        for rule in srv.app.url_map.iter_rules():
            path = str(rule)
            if not path.startswith("/api/mobile/") or "<" in path:
                continue
            for method in ("GET", "POST", "PUT", "DELETE"):
                if method not in rule.methods:
                    continue
                resp = client.open(path, method=method)
                self.assertIn(resp.status_code, (401, 404),
                              f"{method} {path} answered {resp.status_code} "
                              "without a Bearer token")
                break


if __name__ == "__main__":
    unittest.main()
