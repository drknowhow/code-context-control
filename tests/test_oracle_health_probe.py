"""
/api/health?probe=1 — the liveness form the Oracle service manager polls.

The full health check reaches out to Ollama (3 s timeout) and the hub (2 s)
before answering, which is what a status poll asking only "is this the
Oracle?" must not pay for. With ``probe=1`` the route answers from local
state alone and reports the upstream fields as ``null``.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracle.oracle_server as srv  # noqa: E402


class _Bridge:
    def __init__(self):
        self.calls = 0

    def is_available(self, timeout=None):
        self.calls += 1
        return True


class TestHealthProbe(unittest.TestCase):
    def setUp(self):
        self._saved = (srv._cfg, srv._bridge)
        srv._cfg = {"bind_host": "127.0.0.1", "hub_url": "http://127.0.0.1:1",
                    "model": "test-model"}
        self.bridge = _Bridge()
        srv._bridge = self.bridge
        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()

    def tearDown(self):
        srv._cfg, srv._bridge = self._saved

    def test_probe_skips_upstream_checks_and_still_identifies_the_oracle(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            r = self.client.get("/api/health?probe=1")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["service"], "c3-oracle")
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["probe"])
        self.assertIsNone(body["ollama_available"])
        self.assertIsNone(body["hub_available"])
        self.assertEqual(self.bridge.calls, 0)
        urlopen.assert_not_called()

    def test_full_check_is_unchanged_without_the_flag(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("hub down")):
            r = self.client.get("/api/health")
        body = r.get_json()
        self.assertEqual(body["service"], "c3-oracle")
        self.assertFalse(body["probe"])
        self.assertTrue(body["ollama_available"])
        self.assertFalse(body["hub_available"])
        self.assertEqual(self.bridge.calls, 1)

    def test_probe_zero_means_the_full_check(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("hub down")):
            body = self.client.get("/api/health?probe=0").get_json()
        self.assertFalse(body["probe"])
        self.assertEqual(self.bridge.calls, 1)


if __name__ == "__main__":
    unittest.main()
