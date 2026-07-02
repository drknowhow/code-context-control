"""Tests for the Oracle UI concat bundle (Wave 3).

Mirrors the hub bundle smoke tests: every listed module exists on disk, the
build replaces the script token and stamps per-file markers, app.js (the init
IIFE) stays last, / serves the bundle with the session cookie, and /legacy
serves the frozen monolith.
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

    def test_shell_and_legacy_exist(self):
        self.assertTrue((ORACLE_DIR / "oracle_ui.html").is_file())
        self.assertTrue((ORACLE_DIR / "oracle.html").is_file())


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

    def test_root_serves_bundle_with_cookie(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("═══ ui/app.js ═══", body)
        self.assertNotIn("__C3_ORACLE_SCRIPTS__", body)
        cookies = r.headers.getlist("Set-Cookie")
        self.assertTrue(any(c.startswith("c3_oracle_session=") for c in cookies))

    def test_legacy_serves_frozen_monolith(self):
        r = self.client.get("/legacy")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # The monolith carries its inline script, not concat markers.
        self.assertNotIn("═══ ui/app.js ═══", body)
        self.assertIn("async function init()", body)
        cookies = r.headers.getlist("Set-Cookie")
        self.assertTrue(any(c.startswith("c3_oracle_session=") for c in cookies))

    def test_health_unaffected(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
