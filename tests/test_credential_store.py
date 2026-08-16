"""Tests for services/credential_store.py.

Replaces the lazy ``_keyring_module()`` / ``_crypto_module()`` with in-memory
stubs so neither the OS keyring nor the ``cryptography`` package is touched,
and patches ``_global_base()`` to a temp dir standing in for the home
directory. Verifies realm-keyed storage, scope precedence, the
no-fall-through security invariant, the large-value encrypted sidecar,
template expansion, redaction, and .env import.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import credential_store as cs


class _StubKeyring:
    """Minimal in-memory replacement for the keyring module."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, account: str, password: str):
        self.store[(service, account)] = password

    def get_password(self, service: str, account: str):
        return self.store.get((service, account))

    def delete_password(self, service: str, account: str):
        if (service, account) not in self.store:
            raise KeyError("not found")
        del self.store[(service, account)]


class _StubFernet:
    """Reversible stand-in for cryptography.fernet.Fernet."""

    def __init__(self, key: bytes):
        self._key = key

    @staticmethod
    def generate_key() -> bytes:
        return base64.urlsafe_b64encode(b"0" * 32)

    def encrypt(self, data: bytes) -> bytes:
        return base64.urlsafe_b64encode(self._key + b"|" + data)

    def decrypt(self, token: bytes) -> bytes:
        raw = base64.urlsafe_b64decode(token)
        key, _, data = raw.partition(b"|")
        if key != self._key:
            raise ValueError("bad key")
        return data


class TestCredentialStore(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._tmp_proj = tempfile.TemporaryDirectory()
        self._tmp_home = tempfile.TemporaryDirectory()
        self.project = self._tmp_proj.name
        self.home = Path(self._tmp_home.name)
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=self._stub),
            mock.patch.object(cs, "_crypto_module", return_value=_StubFernet),
            mock.patch.object(cs, "_global_base", return_value=self.home),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        cs._ACTIVE_SECRETS.clear()
        self._tmp_proj.cleanup()
        self._tmp_home.cleanup()

    def _project_config(self) -> dict:
        path = Path(self.project) / ".c3" / "config.json"
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # ── storage & realms ──────────────────────────────────

    def test_set_get_roundtrip_project_scope(self):
        entry = cs.set_credential("API_KEY", "sec-value", project_path=self.project)
        self.assertEqual(entry["storage"], "keyring")
        self.assertEqual(entry["value_len"], len("sec-value"))
        account = cs._account(cs.realm("project", self.project), "API_KEY")
        self.assertEqual(self._stub.get_password("c3-creds", account), "sec-value")
        self.assertEqual(cs.get_value("API_KEY", project_path=self.project), "sec-value")

    def test_global_scope_resolves_from_any_project(self):
        cs.set_credential("SHARED", "gsec", scope="global", project_path=self.project)
        self.assertEqual(self._stub.get_password("c3-creds", "global|SHARED"), "gsec")
        other = tempfile.TemporaryDirectory()
        try:
            self.assertEqual(cs.get_value("SHARED", project_path=other.name), "gsec")
        finally:
            other.cleanup()
        cfg = json.loads(
            (self.home / ".c3" / "config.json").read_text(encoding="utf-8")
        )
        self.assertIn("SHARED", cfg["credentials"]["entries"])

    def test_project_shadows_global(self):
        cs.set_credential("NAME", "global-v", scope="global", project_path=self.project)
        cs.set_credential("NAME", "project-v", scope="project", project_path=self.project)
        self.assertEqual(cs.get_value("NAME", project_path=self.project), "project-v")
        self.assertEqual(cs.list_entries(self.project)["NAME"]["scope"], "project")

    def test_project_registry_never_falls_through_to_global_value(self):
        # Security invariant: a repo's committed config.json registering a name
        # (e.g. with inject=true) must NOT siphon the global value.
        cs.set_credential("AWS_PROD", "topsecret", scope="global", project_path=self.project)
        cfg_path = Path(self.project) / ".c3" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({
            "credentials": {"entries": {"AWS_PROD": {
                "type": "env", "storage": "keyring", "inject": True,
                "agent_readable": True, "env_var": "AWS_PROD",
            }}}
        }), encoding="utf-8")
        self.assertIsNone(cs.get_value("AWS_PROD", project_path=self.project))
        values, missing = cs.resolve(["AWS_PROD"], project_path=self.project)
        self.assertEqual(values, {})
        self.assertEqual(missing, ["AWS_PROD"])
        self.assertEqual(cs.list_entries(self.project)["AWS_PROD"]["scope"], "project")

    def test_delete_clears_value_and_registry(self):
        cs.set_credential("GONE", "v", project_path=self.project)
        self.assertTrue(cs.delete_credential("GONE", scope="project", project_path=self.project))
        self.assertIsNone(cs.get_value("GONE", project_path=self.project))
        self.assertNotIn("GONE", self._project_config()["credentials"]["entries"])
        self.assertFalse(cs.delete_credential("GONE", scope="project", project_path=self.project))

    # ── large values / encrypted sidecar ──────────────────

    def test_large_value_routes_to_encrypted_sidecar(self):
        big = "x" * 2000
        entry = cs.set_credential("BLOB", big, project_path=self.project, ctype="multiline")
        self.assertEqual(entry["storage"], "file")
        sidecar = Path(self.project) / ".c3" / "secrets.enc"
        self.assertTrue(sidecar.exists())
        realm_s = cs.realm("project", self.project)
        self.assertIsNotNone(self._stub.get_password("c3-creds", "master|" + realm_s))
        self.assertIsNone(self._stub.get_password("c3-creds", cs._account(realm_s, "BLOB")))
        self.assertNotIn(big, sidecar.read_text(encoding="utf-8"))
        self.assertEqual(cs.get_value("BLOB", project_path=self.project), big)

    def test_shrunk_value_moves_back_to_keyring(self):
        cs.set_credential("SIZED", "y" * 2000, project_path=self.project)
        entry = cs.set_credential("SIZED", "small", project_path=self.project)
        self.assertEqual(entry["storage"], "keyring")
        sidecar = Path(self.project) / ".c3" / "secrets.enc"
        self.assertNotIn("SIZED", json.loads(sidecar.read_text(encoding="utf-8")))
        self.assertEqual(cs.get_value("SIZED", project_path=self.project), "small")

    # ── validation ────────────────────────────────────────

    def test_invalid_name_rejected(self):
        for bad in ("", "1BAD", "has-dash", "has space", "a" * 200):
            with self.assertRaises(cs.CredentialError):
                cs.set_credential(bad, "v", project_path=self.project)

    def test_empty_value_and_bad_type_rejected(self):
        with self.assertRaises(cs.CredentialError):
            cs.set_credential("K", "", project_path=self.project)
        with self.assertRaises(cs.CredentialError):
            cs.set_credential("K", "v", ctype="password", project_path=self.project)

    def test_update_metadata_preserves_created_and_value(self):
        first = cs.set_credential("META", "v", project_path=self.project, description="a")
        updated = cs.update_metadata(
            "META", scope="project", project_path=self.project,
            description="b", agent_readable=True,
        )
        self.assertEqual(updated["created"], first["created"])
        self.assertEqual(updated["description"], "b")
        self.assertTrue(updated["agent_readable"])
        self.assertEqual(cs.get_value("META", project_path=self.project), "v")
        with self.assertRaises(cs.CredentialError):
            cs.update_metadata("META", scope="project", project_path=self.project, value="x")
        with self.assertRaises(cs.CredentialError):
            cs.update_metadata("GHOST", scope="project", project_path=self.project, description="x")

    # ── templates, redaction, usage ───────────────────────

    def test_expand_templates(self):
        cs.set_credential("TOKEN", "tok123", project_path=self.project)
        expanded, used, missing = cs.expand_templates(
            "curl -H 'X: {{cred:TOKEN}}' {{cred:GHOST}}", project_path=self.project
        )
        self.assertIn("tok123", expanded)
        self.assertIn("{{cred:GHOST}}", expanded)
        self.assertEqual(used, ["TOKEN"])
        self.assertEqual(missing, ["GHOST"])

    def test_redact_after_resolve(self):
        cs.set_credential("R", "supersecret", project_path=self.project)
        values, missing = cs.resolve(["R"], project_path=self.project)
        self.assertEqual(values, {"R": "supersecret"})
        self.assertEqual(missing, [])
        self.assertEqual(
            cs.redact_text("out supersecret done"), "out [cred:R] done"
        )

    def test_list_entries_never_contains_values(self):
        cs.set_credential("LISTED", "hidden-value-xyz", project_path=self.project)
        dump = json.dumps(cs.list_entries(self.project))
        self.assertNotIn("hidden-value-xyz", dump)

    def test_fingerprint_live_not_persisted(self):
        cs.set_credential("FP", "v", project_path=self.project)
        fp = cs.fingerprint("FP", project_path=self.project)
        self.assertEqual(len(fp), 8)
        self.assertNotIn("fingerprint", json.dumps(self._project_config()))
        self.assertEqual(cs.fingerprint("GHOST", project_path=self.project), "")

    def test_touch_last_used_writes_state(self):
        cs.set_credential("USED", "v", project_path=self.project)
        cs.touch_last_used(["USED", "GHOST"], project_path=self.project)
        cs.touch_last_used(["USED"], project_path=self.project)
        state = json.loads(
            (Path(self.project) / ".c3" / "cred_state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["USED"]["use_count"], 2)
        self.assertNotIn("GHOST", state)
        self.assertEqual(cs.read_usage_state(self.project)["USED"]["use_count"], 2)

    # ── .env import ───────────────────────────────────────

    def test_import_env(self):
        text = "\n".join([
            "# comment",
            "export FOO=bar",
            'QUOTED="q v"',
            "1BAD=nope",
            "EMPTY=",
            "no_equals_line",
        ])
        result = cs.import_env(text, scope="project", project_path=self.project)
        self.assertEqual(sorted(result["created"]), ["FOO", "QUOTED"])
        self.assertIn("1BAD", result["skipped"])
        self.assertEqual(cs.get_value("FOO", project_path=self.project), "bar")
        self.assertEqual(cs.get_value("QUOTED", project_path=self.project), "q v")
        again = cs.import_env("FOO=new", scope="project", project_path=self.project)
        self.assertEqual(again["created"], [])
        self.assertEqual(cs.get_value("FOO", project_path=self.project), "bar")
        forced = cs.import_env(
            "FOO=new", scope="project", project_path=self.project, overwrite=True
        )
        self.assertEqual(forced["created"], ["FOO"])
        self.assertEqual(cs.get_value("FOO", project_path=self.project), "new")


PAN = "4242424242424242"          # Luhn-valid visa test number
PAN_BAD = "4242424242424241"      # fails checksum
CARD = json.dumps({"cardholder": "D Tselenchuk", "number": PAN,
                   "expiry": "12/27", "cvc": "123"})
ADDRESS = json.dumps({"street1": "1 Test Way", "city": "Columbia",
                      "state": "MD", "zip": "21044"})


class TestStructuredKinds(TestCredentialStore):
    """Structured (address/identity/card) semantics on the same fixture."""

    # ── creation + schema ──
    def test_card_create_display_and_fields(self):
        entry = cs.set_credential("VISA", CARD, scope="project",
                                  project_path=self.project, ctype="card")
        self.assertEqual(entry["display"], {"brand": "visa", "last4": "4242"})
        self.assertEqual(entry["fields"],
                         ["cardholder", "cvc", "expiry", "number"])
        self.assertEqual(entry["type"], "card")

    def test_schema_rejections_name_fields_only(self):
        cases = [
            ({"cardholder": "x", "number": PAN_BAD, "expiry": "12/27"},
             "checksum"),
            ({"cardholder": "x", "number": "123", "expiry": "12/27"},
             "12-19 digits"),
            ({"cardholder": "x", "number": PAN, "expiry": "13/27"}, "expiry"),
            ({"cardholder": "x", "number": PAN, "expiry": "12/27",
              "cvc": "12"}, "cvc"),
            ({"cardholder": "x", "number": PAN, "expiry": "12/27",
              "bogus": "y"}, "unknown field"),
            ({"cardholder": "x", "expiry": "12/27"}, "missing required"),
        ]
        for payload, needle in cases:
            with self.assertRaises(cs.CredentialError) as ctx:
                cs.set_credential("C", json.dumps(payload), scope="project",
                                  project_path=self.project, ctype="card")
            msg = str(ctx.exception)
            self.assertIn(needle, msg)
            for secret in (payload.get("number") or "", "12/27"):
                if secret and secret not in (PAN[-4:],):
                    self.assertNotIn(secret, msg,
                                     "error text echoed submitted content")

    def test_expiry_normalized(self):
        for raw in ("12/2027", "2027-12", "12-27"):
            cs.set_credential("N", json.dumps(
                {"cardholder": "x", "number": PAN, "expiry": raw}),
                scope="project", project_path=self.project, ctype="card")
            self.assertEqual(
                cs.get_value("N", project_path=self.project, field="expiry"),
                "12/27")
            cs.delete_credential("N", scope="project",
                                 project_path=self.project)

    def test_number_separators_stripped(self):
        cs.set_credential("S", json.dumps(
            {"cardholder": "x", "number": "4242 4242-4242 4242",
             "expiry": "12/27"}),
            scope="project", project_path=self.project, ctype="card")
        self.assertEqual(
            cs.get_value("S", project_path=self.project, field="number"), PAN)

    # ── the gate (H1) ──
    def test_whole_value_refused_field_allowed(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        self.assertIsNone(cs.get_value("VISA", project_path=self.project))
        self.assertEqual(
            cs.get_value("VISA", project_path=self.project, field="number"),
            PAN)
        self.assertIsNone(
            cs.get_value("VISA", project_path=self.project, field="nope"))
        # a field ref on a PLAIN cred is also refused
        cs.set_credential("TOK", "plainvalue", scope="project",
                          project_path=self.project)
        self.assertIsNone(
            cs.get_value("TOK", project_path=self.project, field="number"))

    def test_hostile_registry_type_flip_stays_structured(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        cfg_path = Path(self.project) / ".c3" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["credentials"]["entries"]["VISA"]["type"] = "token"
        cfg["credentials"]["entries"]["VISA"]["inject"] = True
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        # attestation keeps it structured: whole-value stays None
        self.assertIsNone(cs.get_value("VISA", project_path=self.project))
        values, missing = cs.resolve(["VISA"], project_path=self.project)
        self.assertEqual(values, {})
        self.assertEqual(missing, ["VISA"])

    def test_fingerprint_empty_for_structured(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        self.assertEqual(cs.fingerprint("VISA", project_path=self.project), "")

    # ── exposure flags + boundary ──
    def test_flags_refused(self):
        for kwargs in ({"agent_readable": True}, {"inject": True}):
            with self.assertRaises(cs.CredentialError):
                cs.set_credential("F", CARD, scope="project",
                                  project_path=self.project, ctype="card",
                                  **kwargs)
        cs.set_credential("F", CARD, scope="project",
                          project_path=self.project, ctype="card")
        for kwargs in ({"agent_readable": True}, {"inject": True},
                       {"type": "token"}):
            with self.assertRaises(cs.CredentialError):
                cs.update_metadata("F", scope="project",
                                   project_path=self.project, **kwargs)

    def test_boundary_immutable(self):
        cs.set_credential("P", "plain", scope="project",
                          project_path=self.project)
        with self.assertRaises(cs.CredentialError):
            cs.set_credential("P", CARD, scope="project",
                              project_path=self.project, ctype="card")
        with self.assertRaises(cs.CredentialError):
            cs.update_metadata("P", scope="project",
                               project_path=self.project, type="card")
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        with self.assertRaises(cs.CredentialError):
            cs.set_credential("VISA", "plain", scope="project",
                              project_path=self.project, ctype="token")
        with self.assertRaises(cs.CredentialError):
            cs.set_credential("VISA", ADDRESS, scope="project",
                              project_path=self.project, ctype="address")

    def test_import_env_never_flattens_structured(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        result = cs.import_env("VISA=oops", scope="project",
                               project_path=self.project, overwrite=True)
        self.assertIn("VISA", result["skipped"])
        self.assertEqual(
            cs.get_value("VISA", project_path=self.project, field="number"),
            PAN)

    # ── merge update ──
    def test_merge_updates_one_field(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        entry = cs.set_credential("VISA", json.dumps({"expiry": "01/30"}),
                                  scope="project",
                                  project_path=self.project, ctype="card")
        self.assertEqual(
            cs.get_value("VISA", project_path=self.project, field="expiry"),
            "01/30")
        self.assertEqual(
            cs.get_value("VISA", project_path=self.project, field="number"),
            PAN)
        self.assertEqual(entry["display"]["last4"], "4242")

    def test_merge_null_deletes_optional_field(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        cs.set_credential("VISA", json.dumps({"cvc": None}), scope="project",
                          project_path=self.project, ctype="card")
        self.assertIsNone(
            cs.get_value("VISA", project_path=self.project, field="cvc"))
        fields = cs.get_structured_fields("VISA", project_path=self.project)
        self.assertNotIn("cvc", fields)

    # ── templates + redaction ──
    def test_dotted_template_and_backcompat(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        cs.set_credential("TOK", "plaintok", scope="project",
                          project_path=self.project)
        cmd = "pay {{cred:VISA.number}} with {{cred:TOK}}"
        expanded, used, missing = cs.expand_templates(
            cmd, project_path=self.project)
        self.assertEqual(expanded, f"pay {PAN} with plaintok")
        self.assertEqual(sorted(used), ["TOK", "VISA.number"])
        self.assertEqual(missing, [])
        # bare structured name is missing, not expanded
        _, _, missing2 = cs.expand_templates(
            "x {{cred:VISA}}", project_path=self.project)
        self.assertEqual(missing2, ["VISA"])

    def test_field_redaction_uses_dotted_ref(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        cs.resolve(["VISA.number"], project_path=self.project)
        self.assertEqual(cs.redact_text(f"saw {PAN} in output"),
                         "saw [cred:VISA.number] in output")

    def test_describe_missing_reasons(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        cs.set_credential("TOK", "plaintok", scope="project",
                          project_path=self.project)
        reasons = cs.describe_missing(
            ["VISA", "VISA.bogus", "TOK.x", "GHOST"],
            project_path=self.project)
        self.assertIn("structured (card)", reasons["VISA"])
        self.assertIn("fields:", reasons["VISA"])
        self.assertIn("no field 'bogus'", reasons["VISA.bogus"])
        self.assertIn("not structured", reasons["TOK.x"])
        self.assertEqual(reasons["GHOST"], "unknown credential")
        self.assertNotIn(PAN, json.dumps(reasons))

    # ── storage + lifecycle ──
    def test_large_identity_routes_to_sidecar(self):
        payload = json.dumps({"full_name": "D T", "ssn": "x" * 250,
                              "dob": "y" * 250, "phone": "z" * 250,
                              "email": "e" * 250})
        entry = cs.set_credential("BIG", payload, scope="project",
                                  project_path=self.project, ctype="identity")
        self.assertEqual(entry["storage"], "file")
        self.assertEqual(
            cs.get_value("BIG", project_path=self.project, field="ssn"),
            "x" * 250)

    def test_is_resolvable_structured(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        self.assertTrue(cs.is_resolvable("VISA", project_path=self.project))

    def test_delete_clears_attestation_and_active_refs(self):
        cs.set_credential("VISA", CARD, scope="project",
                          project_path=self.project, ctype="card")
        cs.resolve(["VISA.number"], project_path=self.project)
        self.assertIn("VISA.number", cs._ACTIVE_SECRETS)
        cs.delete_credential("VISA", scope="project",
                             project_path=self.project)
        self.assertNotIn("VISA.number", cs._ACTIVE_SECRETS)
        realm_s = cs.realm("project", self.project)
        self.assertIsNone(self._stub.get_password(
            cs.KEYRING_SERVICE, cs._struct_account(realm_s, "VISA")))
        self.assertEqual(
            cs.structured_type("VISA", project_path=self.project), "")

    def test_get_structured_fields_none_for_plain(self):
        cs.set_credential("TOK", "plaintok", scope="project",
                          project_path=self.project)
        self.assertIsNone(
            cs.get_structured_fields("TOK", project_path=self.project))

    def test_missing_type_entry_stays_plain(self):
        cs.set_credential("OLD", "legacyvalue", scope="project",
                          project_path=self.project)
        cfg_path = Path(self.project) / ".c3" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        del cfg["credentials"]["entries"]["OLD"]["type"]
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        self.assertEqual(cs.get_value("OLD", project_path=self.project),
                         "legacyvalue")
        self.assertEqual(
            cs.structured_type("OLD", project_path=self.project), "")


if __name__ == "__main__":
    unittest.main()
