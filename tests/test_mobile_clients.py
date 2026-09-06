"""Per-client tokens for the mobile gateway (api_version 5).

Covers ``oracle/services/client_tokens.py`` (the store) and the routes that
use it: the bootstrap-key exchange (``POST /api/mobile/clients``), listing,
revocation, the principal on ``g.c3_client``, ``decided_by`` attribution for
a desk client versus a phone versus the legacy Discovery token, the shared
guard in ``chat_poll``, and the dashboard's cookie-gated pairing routes.

Hermetic: ``Path.home`` is patched to a tmp dir so ``clients.json`` and the
bootstrap key never touch the developer's ``~/.c3``; the Discovery token is
the ``C3_ORACLE_API_KEY`` env override so no keyring is read.
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

os.environ.setdefault("C3_ORACLE_API_KEY", "mobile-clients-key")

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import client_tokens, local_session, mobile_api  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import override_policy as opol  # noqa: E402
from services import override_requests as orq  # noqa: E402

DISCOVERY = "mobile-clients-key"


class _StubScanner:
    def __init__(self, projects):
        self.projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self.projects]


class _Denial:
    def __init__(self, rule, scope="builtin", kind="deny"):
        self.rule = rule
        self.scope = scope
        self.kind = kind
        self.reason = "test"


# ── Store ─────────────────────────────────────────────────


class _HomeBase(unittest.TestCase):
    """A fresh fake home per test; the store path follows ``Path.home``."""

    def setUp(self):
        self._homedir = tempfile.TemporaryDirectory()
        self.home = Path(self._homedir.name)
        (self.home / ".c3" / "oracle").mkdir(parents=True)
        self._patchers = [mock.patch("pathlib.Path.home", return_value=self.home)]
        for p in self._patchers:
            p.start()
        client_tokens.reset_touch_cache()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        client_tokens.reset_touch_cache()
        self._homedir.cleanup()

    def store(self) -> list:
        return json.loads((self.home / ".c3" / "oracle" / "clients.json")
                          .read_text(encoding="utf-8"))


class TestClientTokenStore(_HomeBase):

    def test_mint_returns_token_once_and_stores_only_its_hash(self):
        row, token = client_tokens.mint("desk", "office pc")
        self.assertEqual(row["kind"], "desk")
        self.assertEqual(row["label"], "office pc")
        self.assertTrue(row["client_id"].startswith("desk-"))
        self.assertNotIn("token_hash", row)
        self.assertNotIn("token", row)
        self.assertGreaterEqual(len(token), 40)
        on_disk = self.store()
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(on_disk[0]["token_hash"], client_tokens.hash_token(token))
        self.assertNotIn(token, json.dumps(on_disk))
        self.assertEqual(set(on_disk[0]), {"client_id", "kind", "label", "token_hash",
                                           "created", "last_seen", "revoked_at"})

    def test_mint_rejects_unknown_kind_and_defaults_the_label(self):
        with self.assertRaises(ValueError):
            client_tokens.mint("desktop", "x")
        with self.assertRaises(ValueError):
            client_tokens.mint("", "x")
        row, _ = client_tokens.mint("mobile", "   ")
        self.assertEqual(row["label"], "phone")
        row, _ = client_tokens.mint("desk", None)
        self.assertEqual(row["label"], "desk")
        row, _ = client_tokens.mint("desk", "x" * 500)
        self.assertEqual(len(row["label"]), 64)

    def test_verify_matches_only_the_right_token(self):
        row_a, tok_a = client_tokens.mint("desk", "a")
        row_b, tok_b = client_tokens.mint("mobile", "b")
        self.assertEqual(client_tokens.verify(tok_a)["client_id"], row_a["client_id"])
        self.assertEqual(client_tokens.verify(tok_b)["client_id"], row_b["client_id"])
        self.assertIsNone(client_tokens.verify(tok_a[:-1] + ("A" if tok_a[-1] != "A" else "B")))
        self.assertIsNone(client_tokens.verify(""))
        self.assertIsNone(client_tokens.verify(None))
        self.assertIsNone(client_tokens.verify(DISCOVERY))

    def test_verify_never_returns_the_hash(self):
        _, tok = client_tokens.mint("desk", "a")
        row = client_tokens.verify(tok)
        self.assertNotIn("token_hash", row)
        self.assertEqual(row["kind"], "desk")

    def test_revoke_fails_the_token_on_the_next_verify(self):
        row, tok = client_tokens.mint("desk", "a")
        self.assertIsNotNone(client_tokens.verify(tok))
        gone = client_tokens.revoke(row["client_id"])
        self.assertIsNotNone(gone["revoked_at"])
        self.assertIsNone(client_tokens.verify(tok))
        # Idempotent, and unknown ids are None rather than an exception.
        again = client_tokens.revoke(row["client_id"])
        self.assertEqual(again["revoked_at"], gone["revoked_at"])
        self.assertIsNone(client_tokens.revoke("desk-000000000000"))

    def test_list_strips_hashes_and_keeps_revoked_rows(self):
        row, _ = client_tokens.mint("desk", "a")
        client_tokens.mint("mobile", "b")
        client_tokens.revoke(row["client_id"])
        rows = client_tokens.list_clients()
        self.assertEqual([r["kind"] for r in rows], ["desk", "mobile"])
        for r in rows:
            self.assertNotIn("token_hash", r)
        live = client_tokens.list_clients(include_revoked=False)
        self.assertEqual([r["kind"] for r in live], ["mobile"])

    def test_last_seen_is_touched_at_most_once_a_minute(self):
        row, tok = client_tokens.mint("desk", "a")
        self.assertIsNone(self.store()[0]["last_seen"])
        client_tokens.verify(tok)
        first = self.store()[0]["last_seen"]
        self.assertIsNotNone(first)
        # A second verify inside the interval must not rewrite the file.
        path = self.home / ".c3" / "oracle" / "clients.json"
        before = path.stat().st_mtime_ns
        with mock.patch.object(client_tokens, "_now_iso", return_value="9999-01-01T00:00:00+00:00"):
            client_tokens.verify(tok)
        self.assertEqual(self.store()[0]["last_seen"], first)
        self.assertEqual(path.stat().st_mtime_ns, before)
        # Once the interval has passed it is touched again.
        client_tokens._last_touch[row["client_id"]] -= client_tokens.LAST_SEEN_INTERVAL_S + 1
        with mock.patch.object(client_tokens, "_now_iso", return_value="9999-01-01T00:00:00+00:00"):
            client_tokens.verify(tok)
        self.assertEqual(self.store()[0]["last_seen"], "9999-01-01T00:00:00+00:00")

    def test_corrupt_store_fails_closed_and_the_next_mint_recovers(self):
        path = self.home / ".c3" / "oracle" / "clients.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(client_tokens.verify("anything"))
        self.assertEqual(client_tokens.list_clients(), [])
        row, tok = client_tokens.mint("desk", "a")
        self.assertEqual(client_tokens.verify(tok)["client_id"], row["client_id"])

    def test_store_is_written_atomically_with_no_stray_temp(self):
        client_tokens.mint("desk", "a")
        names = sorted(p.name for p in (self.home / ".c3" / "oracle").iterdir())
        self.assertEqual(names, ["clients.json"])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_store_is_owner_only(self):
        client_tokens.mint("desk", "a")
        mode = (self.home / ".c3" / "oracle" / "clients.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


# ── Routes ────────────────────────────────────────────────


class _RouteBase(_HomeBase):
    """Flask test client over the real app, a stub scanner with one project."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        self._prior_c3_home = os.environ.get("C3_HOME")
        os.environ["C3_HOME"] = str(self.home)
        self._prior_key = os.environ.get("C3_ORACLE_API_KEY")
        os.environ["C3_ORACLE_API_KEY"] = DISCOVERY
        self._patchers.append(mock.patch.object(ag, "_global_base", return_value=self.home))
        self._patchers[-1].start()

        self._prior_cfg = srv._cfg
        srv._cfg = {
            "mobile_api_enabled": True,
            "api_rate_limit_per_min": 0,
            "mobile_security_rate_limit_per_min": 0,
            "api_audit_enabled": False,
            "mobile_override_enabled": True,
            "mobile_override_write": True,
            "bind_host": "127.0.0.1",
        }
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None
        mobile_api.init_services(scanner=_StubScanner([
            {"path": str(self.proj), "name": "proj", "tags": [],
             "active": False, "has_c3": True, "fact_count": 0},
        ]))
        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()
        local_session.write_bootstrap_key(self.home / ".c3" / "oracle")
        self.bootstrap_key = local_session.read_bootstrap_key(self.home / ".c3" / "oracle")
        self.assertTrue(self.bootstrap_key)

    def tearDown(self):
        srv._cfg = self._prior_cfg
        if self._prior_c3_home is None:
            os.environ.pop("C3_HOME", None)
        else:
            os.environ["C3_HOME"] = self._prior_c3_home
        if self._prior_key is None:
            os.environ.pop("C3_ORACLE_API_KEY", None)
        else:
            os.environ["C3_ORACLE_API_KEY"] = self._prior_key
        self._tmp.cleanup()
        super().tearDown()

    # helpers ------------------------------------------------------------

    @staticmethod
    def bearer(token: str) -> dict:
        return {"Authorization": "Bearer " + token}

    def mint(self, kind="desk", label="office", key=None, **kw):
        return self.client.post("/api/mobile/clients", json={
            "bootstrap_key": self.bootstrap_key if key is None else key,
            "kind": kind, "label": label,
        }, **kw)

    def mint_token(self, kind="desk", label="office") -> tuple[str, str]:
        r = self.mint(kind, label)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()
        return body["client_id"], body["token"]

    def write_policy(self, project):
        section = {
            "enabled": True,
            "layers": {k: True for k in opol.LAYER_KEYS},
            "max_ttl_s": 900, "default_uses": 1, "request_ttl_s": 600,
            "max_pending_per_session": 20, "max_requests_per_hour": 200,
            "allow_session_grants": False,
        }
        cfg_file = Path(project) / ".c3" / "config.json"
        cfg_file.write_text(json.dumps({"override": section}, indent=2),
                            encoding="utf-8")

    def make_request(self, name=".env.local", rule="**/.env*"):
        self.write_policy(self.proj)
        target = self.proj / name
        target.write_text("x", encoding="utf-8")
        return orq.create(
            str(self.proj), session_id="sess-1", tool="Read", op="read",
            path=str(target), denial=_Denial(rule, scope="builtin", kind="deny"),
            justification="need it", refusal="[c3-access:denied] test")


class TestBootstrapExchange(_RouteBase):
    """``POST /api/mobile/clients`` — the one route without a Bearer."""

    def test_wire_contract(self):
        # Duplicated on purpose from the D0a spec: the desk client reads
        # these names. Do not "fix" this by importing a constant.
        r = self.mint("desk", "office")
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual({"client_id", "token", "kind", "label", "created"}, set(body))
        self.assertEqual(body["kind"], "desk")
        self.assertEqual(body["label"], "office")
        self.assertTrue(body["client_id"].startswith("desk-"))
        # The token is returned exactly once: not on list, not on disk.
        listed = self.client.get("/api/mobile/clients",
                                 headers=self.bearer(body["token"])).get_json()
        self.assertNotIn(body["token"], json.dumps(listed))
        self.assertNotIn(body["token"], json.dumps(self.store()))

    def test_refuses_a_wrong_bootstrap_key(self):
        r = self.mint(key="not-the-key")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.store() if (self.home / ".c3" / "oracle" / "clients.json").exists() else [], [])

    def test_refuses_an_empty_bootstrap_key(self):
        r = self.client.post("/api/mobile/clients", json={"kind": "desk"})
        self.assertEqual(r.status_code, 401)

    def test_refuses_a_non_local_address(self):
        r = self.mint(environ_base={"REMOTE_ADDR": "10.0.0.9"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse((self.home / ".c3" / "oracle" / "clients.json").exists())

    def test_bound_address_counts_as_local(self):
        # Same rule as the dashboard bootstrap: a Tailscale bind with the
        # request arriving FROM that same address is same-machine.
        srv._cfg["bind_host"] = "100.77.40.101"
        r = self.mint(environ_base={"REMOTE_ADDR": "100.77.40.101"})
        self.assertEqual(r.status_code, 201)
        r = self.mint(environ_base={"REMOTE_ADDR": "100.77.40.102"})
        self.assertEqual(r.status_code, 403)

    def test_rejects_an_unknown_kind(self):
        r = self.mint(kind="desktop")
        self.assertEqual(r.status_code, 400)
        r = self.mint(kind="")
        self.assertEqual(r.status_code, 400)

    def test_does_not_need_a_bearer_but_a_stale_one_is_ignored(self):
        r = self.client.post("/api/mobile/clients", json={
            "bootstrap_key": self.bootstrap_key, "kind": "mobile", "label": "p",
        }, headers=self.bearer("garbage"))
        self.assertEqual(r.status_code, 201)

    def test_is_throttled_by_the_security_bucket(self):
        srv._cfg["mobile_security_rate_limit_per_min"] = 2
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None
        # The bucket's floor burst is 5, so the sixth wrong guess in a row
        # is refused for its rate, not its key.
        codes = [self.mint(key="wrong").status_code for _ in range(8)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 401)
        self.assertEqual(codes[-1], 429)

    def test_disabled_gateway_404s(self):
        srv._cfg["mobile_api_enabled"] = False
        self.assertEqual(self.mint().status_code, 404)


class TestClientAuth(_RouteBase):

    def test_client_token_authenticates_and_info_names_the_principal(self):
        cid, tok = self.mint_token("desk", "office")
        r = self.client.get("/api/mobile/info", headers=self.bearer(tok))
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["api_version"], 5)
        self.assertIn("clients", body["capabilities"])
        self.assertEqual(body["client"], {"kind": "desk", "client_id": cid})

    def test_discovery_token_still_works_as_the_legacy_mobile_principal(self):
        r = self.client.get("/api/mobile/info", headers=self.bearer(DISCOVERY))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["client"],
                         {"kind": "mobile", "client_id": "discovery"})

    def test_garbage_and_missing_bearers_still_401(self):
        self.assertEqual(self.client.get("/api/mobile/info").status_code, 401)
        self.assertEqual(self.client.get(
            "/api/mobile/info", headers=self.bearer("nope")).status_code, 401)

    def test_client_token_passes_the_app_level_write_gate(self):
        # _local_write_guard used to require the cookie or the Discovery
        # token on every mutating /api/* call; a client token must reach the
        # blueprint (which answers 404 here: the project has no such id, so
        # anything but 401 proves the gate let it through).
        _, tok = self.mint_token("desk")
        r = self.client.post("/api/mobile/notifications/ack",
                             json={"project": str(self.proj), "id": "nope"},
                             headers=self.bearer(tok))
        self.assertNotEqual(r.status_code, 401)

    def test_client_token_does_not_unlock_dashboard_mutations(self):
        # Narrower than the Discovery token on purpose: a paired phone must
        # not be able to rotate the Discovery key.
        _, tok = self.mint_token("mobile")
        r = self.client.post("/api/apikey/rotate", headers=self.bearer(tok))
        self.assertEqual(r.status_code, 401)

    def test_chat_poll_shares_the_credential_set(self):
        _, tok = self.mint_token("desk")
        # Any chat route: an authenticated call cannot answer 401 (here it
        # is a 500 — no engine in this harness — which is the point: the
        # guard let it through to the handler).
        r = self.client.get("/api/mobile/chat/commands", headers=self.bearer(tok))
        self.assertNotEqual(r.status_code, 401)
        r = self.client.get("/api/mobile/chat/commands", headers=self.bearer("nope"))
        self.assertEqual(r.status_code, 401)

    def test_list_marks_the_caller(self):
        cid_a, tok_a = self.mint_token("desk", "a")
        cid_b, tok_b = self.mint_token("mobile", "b")
        body = self.client.get("/api/mobile/clients", headers=self.bearer(tok_a)).get_json()
        rows = {r["client_id"]: r for r in body["clients"]}
        self.assertTrue(rows[cid_a]["current"])
        self.assertFalse(rows[cid_b]["current"])
        self.assertEqual(body["client"], {"kind": "desk", "client_id": cid_a})
        for r in body["clients"]:
            self.assertNotIn("token_hash", r)
            self.assertNotIn("token", r)
        # The Discovery token has no row, so nothing is current.
        body = self.client.get("/api/mobile/clients", headers=self.bearer(DISCOVERY)).get_json()
        self.assertFalse(any(r["current"] for r in body["clients"]))

    def test_revoke_fails_auth_on_the_next_request(self):
        cid, tok = self.mint_token("desk")
        _, other = self.mint_token("mobile")
        r = self.client.delete(f"/api/mobile/clients/{cid}", headers=self.bearer(other))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["revoked"])
        self.assertEqual(self.client.get("/api/mobile/info",
                                         headers=self.bearer(tok)).status_code, 401)
        # The other device is untouched.
        self.assertEqual(self.client.get("/api/mobile/info",
                                         headers=self.bearer(other)).status_code, 200)

    def test_revoke_yourself_is_allowed_and_unknown_is_404(self):
        cid, tok = self.mint_token("desk")
        r = self.client.delete(f"/api/mobile/clients/{cid}", headers=self.bearer(tok))
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/mobile/clients/{cid}", headers=self.bearer(tok))
        self.assertEqual(r.status_code, 401)
        r = self.client.delete("/api/mobile/clients/desk-000000000000",
                               headers=self.bearer(DISCOVERY))
        self.assertEqual(r.status_code, 404)

    def test_revoke_needs_a_bearer(self):
        cid, _ = self.mint_token("desk")
        r = self.client.delete(f"/api/mobile/clients/{cid}")
        self.assertEqual(r.status_code, 401)


class TestDecidedByAttribution(_RouteBase):
    """§3.3 ``decided_by`` follows the principal's kind, never a body field."""

    def _deny(self, token, request_id, body=None):
        return self.client.post(f"/api/mobile/overrides/{request_id}/decide",
                                json={"decision": "deny", **(body or {})},
                                headers=self.bearer(token))

    def test_desk_client_is_audited_as_desk(self):
        _, tok = self.mint_token("desk")
        row = self.make_request()
        r = self._deny(tok, row["id"])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["request"]["decided_by"], "desk")
        self.assertEqual(orq.get(row["id"])["decided_by"], "desk")

    def test_phone_client_is_audited_as_mobile(self):
        _, tok = self.mint_token("mobile", "pixel")
        row = self.make_request()
        r = self._deny(tok, row["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["request"]["decided_by"], "mobile")

    def test_discovery_token_stays_mobile(self):
        row = self.make_request()
        r = self._deny(DISCOVERY, row["id"])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["request"]["decided_by"], "mobile")

    def test_a_body_field_cannot_spoof_the_kind(self):
        _, tok = self.mint_token("mobile")
        row = self.make_request()
        r = self._deny(tok, row["id"], {"decided_by": "desk", "kind": "desk"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["request"]["decided_by"], "mobile")

    def test_mute_route_is_attributed_too(self):
        _, tok = self.mint_token("desk")
        row = self.make_request()
        r = self.client.post(f"/api/mobile/overrides/{row['id']}/mute",
                             json={}, headers=self.bearer(tok))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()["request"]["decided_by"], "desk")


class TestDashboardPairing(_RouteBase):
    """Cookie-gated routes the Settings tab uses for the QR."""

    def _login(self):
        code = local_session.mint_code()
        r = self.client.get(f"/?{local_session.BOOTSTRAP_PARAM}={code}")
        self.assertEqual(r.status_code, 302)

    def test_mint_requires_the_dashboard_session(self):
        r = self.client.post("/api/pair/mobile", json={"label": "pixel"})
        self.assertEqual(r.status_code, 401)
        self.assertFalse((self.home / ".c3" / "oracle" / "clients.json").exists())

    def test_mint_with_cookie_returns_a_mobile_token_once(self):
        self._login()
        r = self.client.post("/api/pair/mobile", json={"label": "pixel"})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["kind"], "mobile")
        self.assertEqual(body["label"], "pixel")
        self.assertIn("url", body)
        # The token pairs a real client.
        info = self.client.get("/api/mobile/info", headers=self.bearer(body["token"]))
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.get_json()["client"]["client_id"], body["client_id"])

    def test_list_needs_a_session_and_hides_hashes(self):
        self.assertEqual(self.client.get("/api/pair/clients").status_code, 401)
        self._login()
        self.client.post("/api/pair/mobile", json={"label": "pixel"})
        r = self.client.get("/api/pair/clients")
        self.assertEqual(r.status_code, 200)
        rows = r.get_json()["clients"]
        self.assertEqual([x["label"] for x in rows], ["pixel"])
        self.assertNotIn("token_hash", rows[0])

    def test_revoke_from_the_dashboard(self):
        self._login()
        body = self.client.post("/api/pair/mobile", json={}).get_json()
        self.assertEqual(body["label"], "phone")
        r = self.client.delete(f"/api/pair/clients/{body['client_id']}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/mobile/info",
                                         headers=self.bearer(body["token"])).status_code, 401)
        r = self.client.delete("/api/pair/clients/mobile-000000000000")
        self.assertEqual(r.status_code, 404)

    def test_revoke_needs_a_session(self):
        _, tok = self.mint_token("mobile")
        cid = client_tokens.list_clients()[0]["client_id"]
        r = self.client.delete(f"/api/pair/clients/{cid}")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.client.get("/api/mobile/info",
                                         headers=self.bearer(tok)).status_code, 200)


if __name__ == "__main__":
    unittest.main()
