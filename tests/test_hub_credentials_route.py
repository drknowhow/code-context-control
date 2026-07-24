"""Tests for the hub's read-only GET /api/projects/credentials route."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.hub_server as hub_server
from services import credential_store as cs

CANARY = "hub-canary-value-31zz"


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


class TestHubCredentialsRoute(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=_StubKeyring()),
            mock.patch.object(cs, "_global_base", return_value=Path(self._home.name)),
        ]
        for p in self._patchers:
            p.start()
        self.client = hub_server.app.test_client()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()
        self._home.cleanup()

    def test_requires_path(self):
        resp = self.client.get("/api/projects/credentials")
        self.assertEqual(resp.status_code, 400)

    def test_lists_metadata_never_values(self):
        cs.set_credential("HUB_KEY", CANARY, project_path=str(self.proj),
                          description="hub test")
        cs.set_credential("GLOBAL_KEY", "g-" + CANARY, scope="global",
                          project_path=str(self.proj))
        resp = self.client.get(
            f"/api/projects/credentials?path={self.proj}"
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("HUB_KEY", body)
        self.assertIn("GLOBAL_KEY", body)
        self.assertNotIn(CANARY, body)
        scopes = {e["name"]: e["scope"] for e in resp.get_json()["entries"]}
        self.assertEqual(scopes["HUB_KEY"], "project")
        self.assertEqual(scopes["GLOBAL_KEY"], "global")

    def test_credentials_section_not_hub_writable(self):
        self.assertNotIn("credentials", hub_server._CONFIG_WRITE_SECTIONS)
        resp = self.client.put("/api/projects/config", json={
            "path": str(self.proj), "section": "credentials",
            "values": {"entries": {"X": {"inject": True}}},
        })
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
