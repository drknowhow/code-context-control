"""Tests for services/jira_credentials.py.

Replaces the lazy ``_keyring_module()`` with an in-memory stub so the OS
keyring is never touched. Verifies round-trip storage, the named-account
registry, the default-account pointer, URL-scheme validation, and the
(base_url, username) token binding.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import jira_credentials as jr_creds


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


class TestJiraCredentials(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._patcher = mock.patch.object(jr_creds, "_keyring_module", return_value=self._stub)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.project_path = self._tmp.name

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def _load_section(self) -> dict:
        cfg_file = Path(self.project_path) / ".c3" / "config.json"
        if not cfg_file.exists():
            return {}
        with open(cfg_file, encoding="utf-8") as f:
            return json.load(f).get("jira", {})

    def _save(self, name="work", url="https://x.atlassian.net", user="a@x.com",
              token="sec", **kw):
        kw.setdefault("deployment", "cloud")
        jr_creds.save_credentials(
            name, url, user, token, project_path=self.project_path, **kw
        )

    def test_save_writes_keyring_and_registry(self):
        self._save()
        self.assertEqual(
            self._stub.get_password("c3-jira", "https://x.atlassian.net|a@x.com"),
            "sec",
        )
        section = self._load_section()
        self.assertEqual(section["default_account"], "work")
        entry = section["accounts"]["work"]
        self.assertEqual(entry["base_url"], "https://x.atlassian.net")
        self.assertEqual(entry["username"], "a@x.com")
        self.assertEqual(entry["deployment"], "cloud")
        self.assertTrue(entry["verify_tls"])
        self.assertEqual(entry["ca_bundle"], "")

    def test_save_normalizes_trailing_slash(self):
        self._save(url="https://x.atlassian.net/")
        section = self._load_section()
        self.assertEqual(
            section["accounts"]["work"]["base_url"], "https://x.atlassian.net"
        )
        self.assertEqual(
            jr_creds.load_token("https://x.atlassian.net", "a@x.com"), "sec"
        )

    def test_save_requires_fields(self):
        with self.assertRaises(jr_creds.JiraCredentialError):
            self._save(token="")

    def test_save_rejects_bad_deployment(self):
        with self.assertRaises(jr_creds.JiraCredentialError):
            self._save(deployment="server")

    def test_save_rejects_http_without_allow_insecure(self):
        with self.assertRaises(jr_creds.JiraCredentialError):
            self._save(url="http://jira.local")

    def test_save_allows_http_with_flag(self):
        self._save(url="http://jira.local", allow_insecure=True)
        self.assertEqual(jr_creds.load_token("http://jira.local", "a@x.com"), "sec")

    def test_second_account_set_default_false_keeps_first(self):
        self._save()
        self._save(name="internal", url="https://jira.corp.example",
                   deployment="data_center", set_default=False)
        section = self._load_section()
        self.assertEqual(section["default_account"], "work")
        self.assertEqual(
            section["accounts"]["internal"]["deployment"], "data_center"
        )

    def test_load_token_missing_returns_none(self):
        self.assertIsNone(jr_creds.load_token("https://x.atlassian.net", "ghost"))

    def test_token_bound_to_base_url(self):
        self._save()
        self.assertIsNone(jr_creds.load_token("https://evil.example", "a@x.com"))

    def test_delete_clears_keyring_registry_and_default(self):
        self._save()
        ok = jr_creds.delete_credentials("work", project_path=self.project_path)
        self.assertTrue(ok)
        self.assertIsNone(jr_creds.load_token("https://x.atlassian.net", "a@x.com"))
        section = self._load_section()
        self.assertEqual(section["accounts"], {})
        self.assertEqual(section["default_account"], "")

    def test_delete_falls_back_to_remaining_account(self):
        self._save()
        self._save(name="internal", url="https://jira.corp.example",
                   deployment="data_center", set_default=False)
        jr_creds.delete_credentials("work", project_path=self.project_path)
        section = self._load_section()
        self.assertEqual(section["default_account"], "internal")

    def test_delete_unknown_returns_false(self):
        self.assertFalse(
            jr_creds.delete_credentials("ghost", project_path=self.project_path)
        )

    def test_set_default_account_unknown_raises(self):
        with self.assertRaises(jr_creds.JiraCredentialError):
            jr_creds.set_default_account("ghost", project_path=self.project_path)

    def test_set_default_account_switches(self):
        self._save()
        self._save(name="internal", url="https://jira.corp.example",
                   deployment="data_center", set_default=False)
        jr_creds.set_default_account("internal", project_path=self.project_path)
        account = jr_creds.get_account(project_path=self.project_path)
        self.assertEqual(account["name"], "internal")

    def test_set_default_project_persists_on_account(self):
        self._save()
        jr_creds.set_default_project("PROJ", project_path=self.project_path)
        section = self._load_section()
        self.assertEqual(section["accounts"]["work"]["default_project"], "PROJ")

    def test_get_account_unresolved_returns_empty(self):
        self.assertEqual(jr_creds.get_account(project_path=self.project_path), {})

    def test_get_active_token_round_trip(self):
        self._save()
        self.assertEqual(jr_creds.get_active_token(self.project_path), "sec")

    def test_list_accounts_excludes_tokens(self):
        self._save()
        accounts = jr_creds.list_accounts(self.project_path)
        self.assertEqual(list(accounts), ["work"])
        self.assertNotIn("token", json.dumps(accounts))
        self.assertNotIn("sec", json.dumps(accounts))


if __name__ == "__main__":
    unittest.main()
