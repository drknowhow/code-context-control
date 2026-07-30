"""Tests for the Oracle UI concat bundle (Wave 3).

Mirrors the hub bundle smoke tests: every listed module exists on disk, the
build replaces the script token and stamps per-file markers, app.js (the init
IIFE) stays last, and / serves the bundle with the session cookie.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracle.oracle_server as srv  # noqa: E402
from oracle.oracle_server import _ORACLE_JS_FILES, _build_oracle_html  # noqa: E402

ORACLE_DIR = REPO_ROOT / "oracle"


class TestBundleFiles(unittest.TestCase):
    def test_every_listed_module_exists(self):
        for rel in _ORACLE_JS_FILES:
            self.assertTrue((ORACLE_DIR / rel).is_file(), f"missing {rel}")

    def test_app_js_is_last(self):
        self.assertEqual(_ORACLE_JS_FILES[-1], "ui/app.js")

    def test_shell_exists_and_legacy_monolith_is_gone(self):
        self.assertTrue((ORACLE_DIR / "oracle_ui.html").is_file())
        # The one-release /legacy escape hatch expired with v2.49.0.
        self.assertFalse((ORACLE_DIR / "oracle.html").exists())


class TestBundleBuild(unittest.TestCase):
    def setUp(self):
        self.html = _build_oracle_html()

    def test_token_replaced(self):
        self.assertNotIn("__C3_ORACLE_SCRIPTS__", self.html)

    def test_all_module_markers_present(self):
        for rel in _ORACLE_JS_FILES:
            self.assertIn(f"═══ {rel} ═══", self.html, f"marker missing for {rel}")

    def test_init_iife_present(self):
        self.assertIn("async function init()", self.html)

    def test_no_duplicate_module_markers(self):
        for rel in _ORACLE_JS_FILES:
            self.assertEqual(self.html.count(f"═══ {rel} ═══"), 1, rel)


class TestBundleRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        srv._cfg = {"bind_host": "127.0.0.1", "mcp_port": 3332,
                    "api_require_auth": True, "api_enabled": True,
                    "mcp_enabled": True}
        srv.app.config["TESTING"] = True
        srv._oracle_html_cache = None  # rebuild fresh for this run
        cls.client = srv.app.test_client()

    def test_root_serves_bundle_without_cookie(self):
        # #31: the bundle is served to any local caller, but the session
        # cookie now requires a single-use bootstrap code.
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("═══ ui/app.js ═══", body)
        self.assertNotIn("__C3_ORACLE_SCRIPTS__", body)
        cookies = r.headers.getlist("Set-Cookie")
        self.assertFalse(any(c.startswith("c3_oracle_session=") for c in cookies))

    def test_legacy_route_removed(self):
        self.assertEqual(self.client.get("/legacy").status_code, 404)

    def test_health_carries_version(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        # Header version badge (hub parity) reads this field.
        version = r.get_json().get("version", "")
        self.assertRegex(version, r"^\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
