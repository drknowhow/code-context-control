"""Tests for services/bitbucket_credentials.py.

Replaces the lazy ``_keyring_module()`` with an in-memory stub so the OS
keyring is never touched. Verifies round-trip storage, account index, the
active-account pointer, and ``.c3/config.json`` shape.
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

from services import bitbucket_credentials as bb_creds


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


class TestBitbucketCredentials(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._patcher = mock.patch.object(bb_creds, "_keyring_module", return_value=self._stub)
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
            return json.load(f).get("bitbucket", {})

    def test_save_credentials_writes_keyring_and_config(self):
        bb_creds.save_credentials(
            "https://bb.example.com/", "alice", "secret",
            project_path=self.project_path,
        )
        self.assertEqual(
            self._stub.get_password("c3-bitbucket", "https://bb.example.com|alice"),
            "secret",
        )
        section = self._load_section()
        self.assertEqual(section["accounts"], [{"base_url": "https://bb.example.com", "username": "alice"}])
        self.assertEqual(section["active"], {"base_url": "https://bb.example.com", "username": "alice"})

    def test_load_token_returns_stored_token(self):
        bb_creds.save_credentials(
            "https://bb.example.com", "bob", "tok",
            project_path=self.project_path,
        )
        self.assertEqual(bb_creds.load_token("https://bb.example.com", "bob"), "tok")

    def test_load_token_missing_returns_none(self):
        self.assertIsNone(bb_creds.load_token("https://bb.example.com", "ghost"))

    def test_delete_credentials_clears_both_stores(self):
        bb_creds.save_credentials(
            "https://bb.example.com", "carol", "tok",
            project_path=self.project_path,
        )
        ok = bb_creds.delete_credentials(
            "https://bb.example.com", "carol", project_path=self.project_path
        )
        self.assertTrue(ok)
        self.assertIsNone(bb_creds.load_token("https://bb.example.com", "carol"))
        section = self._load_section()
        self.assertEqual(section["accounts"], [])
        self.assertEqual(section["active"], {"base_url": "", "username": ""})

    def test_set_active_account_promotes_existing(self):
        bb_creds.save_credentials(
            "https://a", "u1", "t1",
            project_path=self.project_path,
        )
        bb_creds.save_credentials(
            "https://b", "u2", "t2",
            project_path=self.project_path, set_active=False,
        )
        bb_creds.set_active_account("https://b", "u2", project_path=self.project_path)
        active = bb_creds.get_active_account(self.project_path)
        self.assertEqual(active, {"base_url": "https://b", "username": "u2"})

    def test_set_default_repo_persists(self):
        bb_creds.set_default_repo("KEY", "slug", project_path=self.project_path)
        section = self._load_section()
        self.assertEqual(section["default_project"], "KEY")
        self.assertEqual(section["default_repo"], "slug")

    def test_get_active_token_round_trip(self):
        bb_creds.save_credentials(
            "https://bb", "dan", "tk",
            project_path=self.project_path,
        )
        self.assertEqual(bb_creds.get_active_token(self.project_path), "tk")


if __name__ == "__main__":
    unittest.main()
