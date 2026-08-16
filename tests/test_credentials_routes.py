"""Tests for the /api/credentials/* Flask routes in cli/server.py.

Flask test client + temp PROJECT_PATH + stubbed keyring — fully offline.
The load-bearing test is the endpoint sweep: no route may ever return a
stored value under any parameters (write-only wire contract).
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

import cli.server as srv
from services import credential_store as cs


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


class _StubFernet:
    def __init__(self, key):
        self._key = key

    @staticmethod
    def generate_key():
        return base64.urlsafe_b64encode(b"0" * 32)

    def encrypt(self, data):
        return base64.urlsafe_b64encode(self._key + b"|" + data)

    def decrypt(self, token):
        raw = base64.urlsafe_b64decode(token)
        key, _, data = raw.partition(b"|")
        if key != self._key:
            raise ValueError("bad key")
        return data


CANARY = "canary-wire-value-77xq"


class TestCredentialsRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        self._old_project_path = srv.PROJECT_PATH
        srv.PROJECT_PATH = self.proj
        self._stub = _StubKeyring()
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=self._stub),
            mock.patch.object(cs, "_crypto_module", return_value=_StubFernet),
            mock.patch.object(cs, "_global_base", return_value=Path(self._home.name)),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()
        self.client = srv.app.test_client()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        cs._ACTIVE_SECRETS.clear()
        srv.PROJECT_PATH = self._old_project_path
        self._tmp.cleanup()
        self._home.cleanup()

    def _post(self, payload):
        return self.client.post("/api/credentials", json=payload)

    def test_post_creates_entry_and_never_echoes_value(self):
        resp = self._post({"name": "API_KEY", "value": CANARY,
                           "description": "test"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn(CANARY, body)
        entry = resp.get_json()["entry"]
        self.assertEqual(entry["name"], "API_KEY")
        self.assertEqual(entry["storage"], "keyring")
        self.assertNotIn("value", entry)

    def test_get_list_masked(self):
        self._post({"name": "API_KEY", "value": CANARY})
        resp = self.client.get("/api/credentials")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("API_KEY", body)
        self.assertNotIn(CANARY, body)
        rec = resp.get_json()["entries"][0]
        self.assertEqual(rec["value_len"], len(CANARY))

    def test_metadata_only_update(self):
        self._post({"name": "META", "value": CANARY})
        resp = self._post({"name": "META", "description": "updated",
                           "agent_readable": True})
        self.assertEqual(resp.status_code, 200)
        entry = resp.get_json()["entry"]
        self.assertEqual(entry["description"], "updated")
        self.assertTrue(entry["agent_readable"])
        # value untouched by the metadata update
        self.assertEqual(cs.get_value("META", project_path=str(self.proj)), CANARY)
        # unknown entry -> 400
        resp = self._post({"name": "GHOST", "description": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_invalid_name_rejected(self):
        resp = self._post({"name": "has space", "value": "v"})
        self.assertEqual(resp.status_code, 400)

    def test_delete_infers_owning_scope(self):
        self._post({"name": "DEL_ME", "value": "v"})
        resp = self.client.delete("/api/credentials/DEL_ME")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["removed"])
        self.assertEqual(resp.get_json()["scope"], "project")
        resp = self.client.delete("/api/credentials/DEL_ME")
        self.assertFalse(resp.get_json()["removed"])

    def test_check_probe(self):
        self._post({"name": "CHK", "value": CANARY})
        resp = self.client.post("/api/credentials/CHK/check")
        data = resp.get_json()
        self.assertTrue(data["resolvable"])
        self.assertEqual(len(data["fingerprint"]), 8)
        self.assertNotIn(CANARY, resp.get_data(as_text=True))
        self.assertEqual(
            self.client.post("/api/credentials/GHOST/check").status_code, 404
        )

    def test_endpoint_sweep_no_route_ever_returns_value(self):
        """Write-only wire contract, enforced over every credentials route."""
        responses = [
            self._post({"name": "SWEEP", "value": CANARY,
                        "agent_readable": True, "inject": True}),
            self.client.get("/api/credentials"),
            self.client.post("/api/credentials/SWEEP/check"),
            self._post({"name": "SWEEP", "description": "meta"}),
            self.client.delete("/api/credentials/SWEEP"),
        ]
        for resp in responses:
            self.assertNotIn(CANARY, resp.get_data(as_text=True))

    def test_responses_are_public_entry_shaped(self):
        """Serializer-identity: every entry payload a credentials route emits
        carries exactly the allowlist keyset.

        The list and set routes used to build responses via ``dict(entry)``,
        which silently forwards any field ever added to the store. Tying the
        keyset to ``PUBLIC_FIELDS`` itself kills that bug class: a new store
        field stays out of the wire until someone allowlists it on purpose."""
        allowed = {"name", "last_used", "use_count",
                   "shadows_global", "shadowed_in", *cs.PUBLIC_FIELDS}
        entries = [
            self._post({"name": "SHAPE", "value": CANARY,
                        "description": "d"}).get_json()["entry"],
            self._post({"name": "SHAPE",
                        "description": "meta-only"}).get_json()["entry"],
            *self.client.get("/api/credentials").get_json()["entries"],
        ]
        self.assertGreaterEqual(len(entries), 3)
        for entry in entries:
            extra = set(entry) - allowed
            self.assertFalse(extra, f"non-allowlisted keys on the wire: {extra}")

    PAN = "4539578763621486"          # Luhn-valid, distinctive middle + last4
    STREET = "742 Evergreen Terrace zq"

    def test_structured_sweep_no_field_value_on_the_wire(self):
        """Structured field values never appear in any credentials-route
        response — full PAN and PAN-middle asserted absent; the 4-char last4
        is the one allowed projection, checked separately below."""
        card = {"cardholder": "D T", "number": self.PAN,
                "expiry": "12/27", "cvc": "9137"}
        addr = {"street1": self.STREET, "city": "Columbia",
                "state": "MD", "zip": "21077"}
        responses = [
            self._post({"name": "SW_CARD", "value": card, "type": "card"}),
            self._post({"name": "SW_ADDR", "value": addr, "type": "address"}),
            self._post({"name": "SW_CARD", "value": {"expiry": "01/30"},
                        "type": "card"}),  # merge update
            self.client.get("/api/credentials"),
            self.client.post("/api/credentials/SW_CARD/check"),
            self._post({"name": "SW_CARD", "description": "meta"}),
            self.client.delete("/api/credentials/SW_CARD"),
            self.client.delete("/api/credentials/SW_ADDR"),
        ]
        sentinels = (self.PAN, self.PAN[:-4], self.STREET, "9137",
                     "12/27", "01/30", "21077")
        for resp in responses:
            self.assertLess(resp.status_code, 500)
            body = resp.get_data(as_text=True)
            for s in sentinels:
                self.assertNotIn(s, body)

    def test_structured_display_on_the_wire_is_thin(self):
        card = {"cardholder": "D T", "number": self.PAN, "expiry": "12/27"}
        self._post({"name": "SW2", "value": card, "type": "card"})
        rec = self.client.get("/api/credentials").get_json()["entries"][0]
        self.assertEqual(rec["display"], {"brand": "visa", "last4": "1486"})
        self.assertEqual(sorted(rec["fields"]),
                         ["cardholder", "expiry", "number"])

    def test_mutations_audited_without_values(self):
        self._post({"name": "AUD", "value": CANARY})
        self.client.delete("/api/credentials/AUD")
        log = self.proj / ".c3" / "activity_log.jsonl"
        self.assertTrue(log.exists())
        text = log.read_text(encoding="utf-8")
        self.assertIn("cred_action", text)
        self.assertIn("AUD", text)
        self.assertNotIn(CANARY, text)


if __name__ == "__main__":
    unittest.main()
