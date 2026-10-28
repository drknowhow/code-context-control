"""/api/health?probe=1 on the UI server answers identity without reaching
Ollama, and the hub's liveness check asks for that form."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.server as srv  # noqa: E402
from services.ollama_client import OllamaClient  # noqa: E402
from services.project_manager import ProjectManager  # noqa: E402


class TestUiHealthProbe(unittest.TestCase):
    def setUp(self):
        self.client = srv.app.test_client()

    def test_probe_answers_identity_without_listing_models(self):
        with mock.patch.object(OllamaClient, "list_models") as list_models:
            resp = self.client.get("/api/health?probe=1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"service": "c3-ui", "probe": True})
        list_models.assert_not_called()

    def test_full_health_still_reports_ollama(self):
        with mock.patch.object(OllamaClient, "list_models", return_value=["m"]) as list_models:
            resp = self.client.get("/api/health")
        body = resp.get_json()
        self.assertEqual(body["service"], "c3-ui")
        self.assertTrue(body["sources"]["ollama"])
        list_models.assert_called_once()


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestHubVerifiesWithProbe(unittest.TestCase):
    def test_verify_c3_session_requests_the_probe_form(self):
        pm = ProjectManager()
        with mock.patch("services.project_manager.urllib.request.urlopen",
                        return_value=_Resp({"service": "c3-ui", "probe": True})) as urlopen:
            self.assertTrue(pm._verify_c3_session(4321))
        url = urlopen.call_args[0][0]
        self.assertEqual(url, "http://127.0.0.1:4321/api/health?probe=1")


if __name__ == "__main__":
    unittest.main()
