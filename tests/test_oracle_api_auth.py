"""Tests for oracle/services/api_auth.py.

Replaces the lazy ``_keyring_module()`` with an in-memory stub so the OS keyring
is never touched. Verifies generate/verify/rotate/clear, the env override, and
bearer extraction.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from oracle.services import api_auth


class _StubKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self.store:
            raise KeyError("not found")
        del self.store[(service, account)]


class TestApiAuth(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._patcher = mock.patch.object(api_auth, "_keyring_module", return_value=self._stub)
        self._patcher.start()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(api_auth.ENV_OVERRIDE, None)

    def tearDown(self):
        self._patcher.stop()
        self._env.stop()

    def test_get_or_create_generates_and_persists(self):
        self.assertIsNone(api_auth.peek())
        key = api_auth.get_or_create_key()
        self.assertTrue(key)
        self.assertGreaterEqual(len(key), 20)
        self.assertEqual(api_auth.get_or_create_key(), key)  # idempotent
        self.assertEqual(api_auth.peek(), key)

    def test_verify(self):
        key = api_auth.get_or_create_key()
        self.assertTrue(api_auth.verify(key))
        self.assertFalse(api_auth.verify("wrong"))
        self.assertFalse(api_auth.verify(""))
        self.assertFalse(api_auth.verify(None))

    def test_rotate_changes_key(self):
        k1 = api_auth.get_or_create_key()
        k2 = api_auth.rotate()
        self.assertNotEqual(k1, k2)
        self.assertTrue(api_auth.verify(k2))
        self.assertFalse(api_auth.verify(k1))

    def test_clear_removes_key(self):
        api_auth.get_or_create_key()
        self.assertTrue(api_auth.clear())
        self.assertIsNone(api_auth.peek())
        self.assertFalse(api_auth.clear())  # nothing left to remove

    def test_env_override_wins_and_not_persisted(self):
        os.environ[api_auth.ENV_OVERRIDE] = "env-key-123456789012345"
        self.assertEqual(api_auth.peek(), "env-key-123456789012345")
        self.assertTrue(api_auth.verify("env-key-123456789012345"))
        self.assertEqual(api_auth.get_or_create_key(), "env-key-123456789012345")
        self.assertEqual(self._stub.store, {})  # env keys never hit the keyring

    def test_extract_bearer(self):
        self.assertEqual(api_auth.extract_bearer("Bearer abc"), "abc")
        self.assertEqual(api_auth.extract_bearer("bearer xyz"), "xyz")
        self.assertEqual(api_auth.extract_bearer("rawtoken"), "rawtoken")
        self.assertIsNone(api_auth.extract_bearer(""))
        self.assertIsNone(api_auth.extract_bearer(None))


if __name__ == "__main__":
    unittest.main()
