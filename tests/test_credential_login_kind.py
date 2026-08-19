"""Tests for the `login` structured credential kind.

C3 stores website logins; C3 never uses them. There is deliberately no
browser runner in this package — the consuming side is an out-of-process
auth broker in a separate, private repo. These tests pin the two properties
that make that split safe:

  1. The record is INJECT-ONLY and unreadable as a whole, exactly like
     card/address/identity. No surface hands the password back.
  2. `canonical_origin` is stored NORMALIZED and https-only, so a downstream
     runner can compare it to a live top-level frame without ambiguity.
     Ambiguity in an origin check is the entire attack.

Reuses the keyring/Fernet stubs and temp-dir fixture from
test_credential_store.TestCredentialStore.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import credential_store as cs
from tests.test_credential_store import TestCredentialStore


def _login(**over) -> str:
    base = {
        "site_id": "example",
        "canonical_origin": "https://login.example.com",
        "username": "dimitri@example.com",
        "password": "correct-horse-battery-staple",
    }
    base.update(over)
    return json.dumps({k: v for k, v in base.items() if v is not None})


class TestLoginKind(TestCredentialStore):

    # ── registration ──
    def test_login_is_a_structured_type(self):
        self.assertIn("login", cs.VALID_TYPES)
        self.assertIn("login", cs.STRUCTURED_TYPES)

    def test_create_projects_site_and_origin_but_never_username(self):
        entry = cs.set_credential("GH", _login(site_id="github",
                                               canonical_origin="https://github.com"),
                                  scope="project", project_path=self.project,
                                  ctype="login")
        self.assertEqual(entry["type"], "login")
        self.assertEqual(entry["display"], {"site_id": "github",
                                            "origin": "https://github.com",
                                            "has_totp": False})
        self.assertNotIn("username", json.dumps(entry["display"]))
        self.assertEqual(entry["fields"],
                         ["canonical_origin", "password", "site_id", "username"])

    def test_has_totp_flag_never_carries_the_seed(self):
        entry = cs.set_credential("T", _login(totp_secret="JBSWY3DPEHPK3PXP"),
                                  scope="project", project_path=self.project,
                                  ctype="login")
        self.assertTrue(entry["display"]["has_totp"])
        self.assertNotIn("JBSWY3DPEHPK3PXP", json.dumps(entry))

    # ── inject-only invariant (the whole point) ──
    def test_whole_payload_is_unreadable(self):
        cs.set_credential("L", _login(), scope="project",
                          project_path=self.project, ctype="login")
        self.assertIsNone(cs.get_value("L", project_path=self.project))

    def test_field_addressing_is_the_only_way_in(self):
        cs.set_credential("L", _login(), scope="project",
                          project_path=self.project, ctype="login")
        self.assertEqual(
            cs.get_value("L", project_path=self.project, field="password"),
            "correct-horse-battery-staple")

    def test_agent_readable_and_inject_are_refused(self):
        for flag in ("agent_readable", "inject"):
            with self.assertRaises(cs.CredentialError) as ctx:
                cs.set_credential("X", _login(), scope="project",
                                  project_path=self.project, ctype="login",
                                  **{flag: True})
            self.assertIn("inject-only", str(ctx.exception))

    # ── origin normalization ──
    def test_origin_is_normalized(self):
        cases = {
            "https://Login.Example.COM/": "https://login.example.com",
            "https://example.com:443": "https://example.com:443",
            " https://example.com ": "https://example.com",
        }
        for raw, want in cases.items():
            cs.set_credential("N", _login(canonical_origin=raw),
                              scope="project", project_path=self.project,
                              ctype="login")
            self.assertEqual(
                cs.get_value("N", project_path=self.project,
                             field="canonical_origin"), want)
            cs.delete_credential("N", scope="project",
                                 project_path=self.project)

    def test_origin_rejections(self):
        cases = [
            ("http://example.com", "https"),
            ("https://example.com/login", "no path"),
            ("https://example.com?next=1", "no path"),
            ("https://user@example.com", "userinfo"),
            ("example.com", "full origin"),
            ("https://example.com:99999", "port"),
            ("https://:8443", "host"),
            ("https://exa mple.com", "host"),
            # trailing-slash stripping must not turn a bare scheme into a
            # silently-accepted origin
            ("https://", "full origin"),
        ]
        for raw, needle in cases:
            with self.assertRaises(cs.CredentialError) as ctx:
                cs.set_credential("B", _login(canonical_origin=raw),
                                  scope="project", project_path=self.project,
                                  ctype="login")
            self.assertIn(needle, str(ctx.exception))

    # ── field hygiene ──
    def test_errors_never_echo_the_password(self):
        secret = "s3cr3t-do-not-echo"
        with self.assertRaises(cs.CredentialError) as ctx:
            cs.set_credential("E", json.dumps(
                {"site_id": "x", "canonical_origin": "http://x.com",
                 "username": "u", "password": secret}),
                scope="project", project_path=self.project, ctype="login")
        self.assertNotIn(secret, str(ctx.exception))

    def test_missing_and_unknown_fields(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            cs.set_credential("M", json.dumps({"site_id": "x"}),
                              scope="project", project_path=self.project,
                              ctype="login")
        self.assertIn("missing required", str(ctx.exception))
        with self.assertRaises(cs.CredentialError) as ctx:
            cs.set_credential("M", _login(bogus="y"), scope="project",
                              project_path=self.project, ctype="login")
        self.assertIn("unknown field", str(ctx.exception))

    def test_site_id_charset(self):
        for bad in ("Example", "has space", "a" * 65, "-"):
            with self.assertRaises(cs.CredentialError) as ctx:
                cs.set_credential("S", _login(site_id=bad), scope="project",
                                  project_path=self.project, ctype="login")
            self.assertIn("site_id", str(ctx.exception))

    def test_totp_seed_must_be_base32(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            cs.set_credential("T", _login(totp_secret="not-base32!"),
                              scope="project", project_path=self.project,
                              ctype="login")
        self.assertIn("base32", str(ctx.exception))
        cs.set_credential("T", _login(totp_secret="jbsw y3dp ehpk 3pxp"),
                          scope="project", project_path=self.project,
                          ctype="login")
        self.assertEqual(
            cs.get_value("T", project_path=self.project, field="totp_secret"),
            "JBSWY3DPEHPK3PXP")

    def test_cannot_flip_a_plain_entry_into_a_login(self):
        cs.set_credential("P", "plain-token", scope="project",
                          project_path=self.project, ctype="token")
        with self.assertRaises(cs.CredentialError) as ctx:
            cs.set_credential("P", _login(), scope="project",
                              project_path=self.project, ctype="login")
        self.assertIn("delete the entry", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
