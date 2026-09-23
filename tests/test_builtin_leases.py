"""Temporary and per-session builtin guard modes (2.148.0)."""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.hub_server as hub_server  # noqa: E402
from oracle.services import client_tokens  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import builtin_leases as bl  # noqa: E402

ENV = "**/.env*"
GIT = "**/.git/**"
DESK = {"Authorization": "Bearer desk-token"}


class _FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, account, value):
        self.store[(service, account)] = value

    def get_password(self, service, account):
        return self.store.get((service, account))


class _StubPM:
    def list_registered(self):
        return []


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        (self.home / ".c3").mkdir(parents=True)
        self.proj = root / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        self.other = root / "other"
        (self.other / ".c3").mkdir(parents=True)
        self.keyring = _FakeKeyring()
        mod = types.ModuleType("keyring")
        mod.set_password = self.keyring.set_password
        mod.get_password = self.keyring.get_password
        self._patches = [
            mock.patch.dict(sys.modules, {"keyring": mod}),
            mock.patch.object(ag, "_global_base", return_value=self.home),
        ]
        for p in self._patches:
            p.start()
        ag.bind_session("")

    def tearDown(self):
        ag.bind_session("")
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def env_kind(self, project=None):
        project = project or self.proj
        denial = ag.check(str(project / ".env"), "read", str(project))
        return denial.kind if denial else "allowed"

    def mint(self, mode="confirm", **kw):
        kw.setdefault("scope", "global")
        kw.setdefault("ttl_s", 900)
        return bl.mint(ENV, mode, created_by="desk-1", **kw)

    def rows(self):
        return json.loads((self.home / ".c3" / ag.LEASES_FILE).read_text())["leases"]

    def write_rows(self, rows):
        (self.home / ".c3" / ag.LEASES_FILE).write_text(json.dumps({"leases": rows}))


class LeaseEvaluation(_Base):
    def test_an_all_session_lease_loosens_every_project_until_revoked(self):
        self.assertEqual(self.env_kind(), "deny")
        row = self.mint("confirm")
        self.assertEqual(self.env_kind(), "confirm")
        self.assertEqual(self.env_kind(self.other), "confirm")
        bl.revoke(row["id"])
        self.assertEqual(self.env_kind(), "deny")

    def test_a_project_lease_stays_in_its_project(self):
        self.mint("allow", scope="project", project_path=str(self.proj))
        self.assertEqual(self.env_kind(), "allowed")
        self.assertEqual(self.env_kind(self.other), "deny")

    def test_a_session_lease_applies_only_to_the_bound_session(self):
        self.mint("allow", session_id="sess-a")
        self.assertEqual(self.env_kind(), "deny")
        ag.bind_session("sess-b")
        self.assertEqual(self.env_kind(), "deny")
        ag.bind_session("sess-a")
        self.assertEqual(self.env_kind(), "allowed")

    def test_an_expired_lease_does_nothing(self):
        row = self.mint("allow")
        ends = datetime.fromisoformat(row["expires_at"])
        with mock.patch.object(bl, "_now", return_value=ends):
            self.assertEqual(self.env_kind(), "deny")

    def test_a_row_edited_by_hand_loses_its_attestation(self):
        self.mint("confirm")
        rows = self.rows()
        rows[0]["mode"] = "allow"
        self.write_rows(rows)
        self.assertEqual(self.env_kind(), "deny")

    def test_a_row_written_without_the_keyring_half_is_ignored(self):
        at = datetime.now(timezone.utc)
        self.write_rows([{"id": "bls_forged", "glob": ENV, "mode": "allow",
                          "scope": "global", "realm": "global", "project_path": "",
                          "session_id": "", "created_at": at.isoformat(),
                          "expires_at": (at + timedelta(hours=1)).isoformat(),
                          "created_by": "agent"}])
        self.assertEqual(self.env_kind(), "deny")

    def test_a_revoked_row_copied_back_stays_void(self):
        row = self.mint("allow")
        saved = self.rows()
        bl.revoke(row["id"])
        self.write_rows(saved)
        self.assertEqual(self.env_kind(), "deny")

    def test_a_lease_never_tightens(self):
        ag.set_builtin_mode(GIT, "allow", "global")
        bl.mint(GIT, "confirm", scope="global", ttl_s=900, created_by="desk-1")
        self.assertIsNone(ag.check(str(self.proj / ".git" / "config"), "write", str(self.proj)))

    def test_mint_refuses_bad_input(self):
        for kwargs in ({"ttl_s": 30}, {"ttl_s": 8 * 3600 + 1}, {"ttl_s": "soon"}):
            with self.assertRaises(ValueError):
                self.mint(**kwargs)
        with self.assertRaises(ValueError):
            bl.mint(ENV, "deny", scope="global", ttl_s=900, created_by="x")
        with self.assertRaises(ValueError):
            bl.mint("**/.c3/secrets.enc", "allow", scope="global", ttl_s=900, created_by="x")

    def test_no_file_means_no_keyring_reads(self):
        with mock.patch.object(bl, "_attested") as attested:
            self.assertEqual(self.env_kind(), "deny")
        attested.assert_not_called()


class LeaseRoutes(_Base):
    def setUp(self):
        super().setUp()
        principal = {"desk-token": {"kind": "desk", "client_id": "desk-1"}}
        self._patches += [
            mock.patch.object(hub_server, "_pm", return_value=_StubPM()),
            mock.patch.object(hub_server, "_resolve_project_path", side_effect=Path),
            mock.patch.object(client_tokens, "principal_for", side_effect=principal.get),
        ]
        for p in self._patches[2:]:
            p.start()
        hub_server.app.config["TESTING"] = True
        self.client = hub_server.app.test_client()

    def post(self, url, body, headers=None):
        resp = self.client.post(url, json=body, headers=headers or {})
        return resp.status_code, resp.get_json()

    def lease_body(self, **kw):
        return {"scope": "global", "glob": ENV, "mode": "confirm", "ttl_s": 900,
                "confirm": ENV, **kw}

    def test_minting_needs_desk(self):
        status, body = self.post("/api/hub/access/builtin/lease", self.lease_body())
        self.assertEqual(status, 403)
        self.assertTrue(body["needs_human"])

    def test_minting_needs_the_glob_retyped(self):
        status, _ = self.post("/api/hub/access/builtin/lease",
                              self.lease_body(confirm="x"), DESK)
        self.assertEqual(status, 400)

    def test_a_lease_that_would_change_nothing_is_refused(self):
        ag.set_builtin_mode(ENV, "allow", "global")
        status, _ = self.post("/api/hub/access/builtin/lease", self.lease_body(), DESK)
        self.assertEqual(status, 400)

    def test_mint_list_revoke_round_trip(self):
        status, body = self.post("/api/hub/access/builtin/lease",
                                 self.lease_body(session_id="sess-a"), DESK)
        self.assertEqual(status, 200)
        lease_id = body["lease"]["id"]
        listed = self.client.get("/api/hub/access/builtin").get_json()["leases"]
        self.assertEqual([row["id"] for row in listed], [lease_id])
        self.assertEqual(listed[0]["created_by"], "desk-1")
        status, _ = self.post("/api/hub/access/builtin/lease/revoke", {"id": lease_id})
        self.assertEqual(status, 200)
        status, _ = self.post("/api/hub/access/builtin/lease/revoke", {"id": lease_id})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
