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


if __name__ == "__main__":
    unittest.main()
