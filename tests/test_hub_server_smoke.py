"""Smoke tests for cli/hub_server.py — Flask test-client only, no real bind."""
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class TestHubServerSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cli import hub_server  # noqa: F401
        cls.mod = hub_server
        cls.client = hub_server.app.test_client()

    def test_main_is_callable_entry_point(self):
        self.assertTrue(callable(getattr(self.mod, "main", None)),
                        "cli.hub_server.main must exist for the c3-hub entry-point")

    def test_default_bind_host_is_loopback(self):
        defaults = self.mod._HUB_CONFIG_DEFAULTS
        self.assertEqual(defaults.get("host"), "127.0.0.1",
                         "Hub must default to loopback bind to avoid LAN exposure")

    def test_version_endpoint(self):
        resp = self.client.get("/api/version")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("c3_version", data)
        self.assertRegex(str(data["c3_version"]), r"\d+\.\d+\.\d+")

    def test_health_endpoint(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("service"), "c3-hub")

    def test_guide_assets_colocated_with_package(self):
        # The in-app guide must live inside the cli package so it ships in the
        # wheel and is locatable via __file__ after a pure pip install.
        guide_dir = Path(self.mod.__file__).parent / "guide"
        self.assertTrue((guide_dir / "index.html").is_file(),
                        "guide/index.html must sit inside the cli package")

    def test_guide_route_serves_index(self):
        resp = self.client.get("/guide/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("Content-Type", ""))

    def test_guide_route_serves_named_page(self):
        resp = self.client.get("/guide/tools.html")
        self.assertEqual(resp.status_code, 200)

    def test_guide_route_404_for_missing(self):
        resp = self.client.get("/guide/does-not-exist.html")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
