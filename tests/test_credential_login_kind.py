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

    def test_create_projects_site_and_target_but_never_username(self):
        entry = cs.set_credential("GH", _login(site_id="github",
                                               canonical_origin="https://github.com"),
                                  scope="project", project_path=self.project,
                                  ctype="login")
        self.assertEqual(entry["type"], "login")
        self.assertEqual(entry["display"], {"site_id": "github",
                                            "scheme": "https",
                                            "target": "https://github.com",
                                            "has_totp": False,
                                            "has_key": False})
        self.assertNotIn("username", json.dumps(entry["display"]))
        self.assertEqual(entry["fields"],
                         ["canonical_target", "password", "site_id", "username"])

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
            ("http://example.com", "cleartext"),
            ("https://example.com/login", "no path"),
            ("https://example.com?next=1", "no path"),
            ("https://user@example.com", "userinfo"),
            ("example.com", "full target"),
            ("https://example.com:99999", "port"),
            ("https://:8443", "host"),
            ("https://exa mple.com", "host"),
            # trailing-slash stripping must not turn a bare scheme into a
            # silently-accepted target
            ("https://", "full target"),
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

    def test_totp_digits_survive_normalization(self):
        """Regression: `[ -]` is a character RANGE covering the digits 2-7,
        so the first version of this normalizer deleted them. The seed still
        passed the base32 check afterwards — it was all letters by then — and
        would have generated wrong codes forever."""
        cs.set_credential("D", _login(totp_secret="234567 234567 AB"),
                          scope="project", project_path=self.project,
                          ctype="login")
        self.assertEqual(
            cs.get_value("D", project_path=self.project, field="totp_secret"),
            "234567234567AB")

    def test_cannot_flip_a_plain_entry_into_a_login(self):
        cs.set_credential("P", "plain-token", scope="project",
                          project_path=self.project, ctype="token")
        with self.assertRaises(cs.CredentialError) as ctx:
            cs.set_credential("P", _login(), scope="project",
                              project_path=self.project, ctype="login")
        self.assertIn("delete the entry", str(ctx.exception))


# ── v2.118.0: logins for servers, databases and other non-web targets ──────

_PEM = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + "b3BlbnNzaC1rZXktdjEAAAAA\n" * 40
        + "-----END OPENSSH PRIVATE KEY-----")


def _server_login(**over) -> str:
    base = {
        "site_id": "build01",
        "canonical_target": "ssh://build01.lan:22",
        "username": "deploy",
        "private_key": _PEM,
    }
    base.update(over)
    return json.dumps({k: v for k, v in base.items() if v is not None})


class TestNonWebLogins(TestCredentialStore):
    def set_login(self, name, payload):
        return cs.set_credential(name, payload, scope="project",
                                 project_path=self.project, ctype="login")

    def test_every_allowed_scheme_normalizes(self):
        for i, scheme in enumerate(cs.TARGET_SCHEMES):
            with self.subTest(scheme=scheme):
                entry = self.set_login(f"S{i}", _server_login(
                    canonical_target=f"{scheme}://Host.Example.COM:0443"))
                self.assertEqual(entry["display"]["target"],
                                 f"{scheme}://host.example.com:443")
                self.assertEqual(entry["display"]["scheme"], scheme)

    def test_cleartext_schemes_name_their_replacement(self):
        for bad, good in (("http", "https"), ("ftp", "ftps"),
                          ("telnet", "ssh"), ("imap", "imaps"),
                          ("ldap", "ldaps"), ("smtp", "smtps")):
            with self.subTest(scheme=bad):
                with self.assertRaises(cs.CredentialError) as ctx:
                    self.set_login("C", _server_login(
                        canonical_target=f"{bad}://host.example.com"))
                self.assertIn("cleartext", str(ctx.exception))
                self.assertIn(good, str(ctx.exception))

    def test_unknown_scheme_is_refused_not_guessed(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            self.set_login("U", _server_login(
                canonical_target="gopher://host.example.com"))
        self.assertIn("unsupported scheme", str(ctx.exception))

    def test_a_server_target_keeps_every_bare_target_rule(self):
        for raw, needle in (("ssh://h.example.com/path", "no path"),
                            ("ssh://user@h.example.com", "userinfo"),
                            ("ssh://h.example.com:99999", "port"),
                            ("ssh://", "full target")):
            with self.subTest(raw=raw):
                with self.assertRaises(cs.CredentialError) as ctx:
                    self.set_login("B", _server_login(canonical_target=raw))
                self.assertIn(needle, str(ctx.exception))

    # ── the origin-pinning property, which must NOT be weakened ──
    def test_canonical_origin_resolves_only_for_https(self):
        """The load-bearing test. A browser broker pins a credential by
        asking for `canonical_origin`; handing it an `ssh://` string would
        give it something it cannot compare to a top-level frame."""
        self.set_login("WEB", _login(canonical_origin="https://bank.example.com"))
        self.set_login("SSH", _server_login())
        self.assertEqual(
            cs.get_value("WEB", project_path=self.project,
                         field="canonical_origin"),
            "https://bank.example.com")
        self.assertIsNone(
            cs.get_value("SSH", project_path=self.project,
                         field="canonical_origin"))

    def test_canonical_target_resolves_for_both(self):
        self.set_login("WEB", _login(canonical_origin="https://bank.example.com"))
        self.set_login("SSH", _server_login())
        self.assertEqual(
            cs.get_value("WEB", project_path=self.project,
                         field="canonical_target"),
            "https://bank.example.com")
        self.assertEqual(
            cs.get_value("SSH", project_path=self.project,
                         field="canonical_target"),
            "ssh://build01.lan:22")

    def test_a_pre_2118_record_still_reads_back(self):
        """Stored blobs carry `canonical_origin` literally. No migration
        runs, so both spellings must resolve off the old field."""
        self.set_login("OLD", _login())
        raw = json.loads(cs._get_raw("OLD", project_path=self.project))
        raw["canonical_origin"] = raw.pop("canonical_target")
        cs._store_value("OLD", json.dumps(raw), scope="project",
                        project_path=self.project,
                        realm_s=cs.realm("project", self.project))
        self.assertEqual(
            cs.get_value("OLD", project_path=self.project,
                         field="canonical_target"),
            "https://login.example.com")
        self.assertEqual(
            cs.get_value("OLD", project_path=self.project,
                         field="canonical_origin"),
            "https://login.example.com")

    def test_the_legacy_spelling_is_accepted_on_input(self):
        entry = self.set_login("A", _login())
        self.assertEqual(entry["fields"],
                         ["canonical_target", "password", "site_id",
                          "username"])

    def test_two_disagreeing_targets_are_refused(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            self.set_login("X", json.dumps({
                "site_id": "x", "username": "u", "password": "p",
                "canonical_target": "https://a.example.com",
                "canonical_origin": "https://b.example.com"}))
        self.assertIn("disagree", str(ctx.exception))

    # ── password-or-key ──
    def test_a_key_only_login_is_valid(self):
        entry = self.set_login("K", _server_login())
        self.assertTrue(entry["display"]["has_key"])
        self.assertNotIn("private_key", json.dumps(entry["display"]))

    def test_a_login_with_neither_secret_is_refused(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            self.set_login("N", _server_login(private_key=None))
        self.assertIn("needs a secret", str(ctx.exception))

    def test_a_non_pem_private_key_is_refused(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            self.set_login("P", _server_login(
                private_key="ssh-ed25519 AAAAC3Nz... deploy@build01"))
        self.assertIn("PRIVATE KEY", str(ctx.exception))

    def test_a_passphrase_without_a_key_is_refused(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            self.set_login("Q", _login(passphrase="hunter2"))
        self.assertIn("no 'private_key'", str(ctx.exception))

    def test_an_oversize_key_is_accepted_and_goes_to_the_sidecar(self):
        entry = self.set_login("K", _server_login())
        self.assertGreater(len(_PEM), cs.FILE_STORAGE_THRESHOLD)
        self.assertEqual(entry["storage"], "file")
        self.assertEqual(
            cs.get_value("K", project_path=self.project, field="private_key"),
            _PEM)

    def test_a_key_beyond_its_own_cap_is_refused(self):
        cap = cs._field_max("private_key")
        with self.assertRaises(cs.CredentialError) as ctx:
            self.set_login("H", _server_login(
                private_key="-----BEGIN PRIVATE KEY-----\n" + "x" * (cap + 1)))
        self.assertIn("private_key", str(ctx.exception))

    def test_other_fields_keep_the_small_cap(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            self.set_login("W", _login(password="p" * 300))
        self.assertIn("256", str(ctx.exception))

    # ── inject-only survives the new shape ──
    def test_reveal_is_still_refused_for_a_server_login(self):
        self.set_login("K", _server_login())
        self.assertIsNone(cs.get_value("K", project_path=self.project))

    def test_errors_never_echo_the_key(self):
        with self.assertRaises(cs.CredentialError) as ctx:
            self.set_login("E", _server_login(
                canonical_target="gopher://nope.example.com"))
        self.assertNotIn(_PEM[:60], str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
