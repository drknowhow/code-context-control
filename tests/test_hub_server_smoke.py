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

    # ── Sub-project endpoints (v2.44.0) ────────────────────────────

    def test_subprojects_tree_requires_parent(self):
        resp = self.client.get("/api/projects/subprojects")
        self.assertEqual(resp.status_code, 400)

    def test_subprojects_tree_unknown_parent_404(self):
        resp = self.client.get("/api/projects/subprojects?parent=Z:/no/such/dir")
        self.assertEqual(resp.status_code, 404)

    def test_subprojects_add_requires_fields(self):
        resp = self.client.post("/api/projects/subprojects/add", json={})
        self.assertEqual(resp.status_code, 400)

    def test_subprojects_add_invokes_cli(self):
        import unittest.mock as mock
        with mock.patch.object(self.mod, "_run_c3",
                               return_value={"success": True,
                                             "output": '{"added": true, "name": "x"}',
                                             "returncode": 0}) as run:
            resp = self.client.post("/api/projects/subprojects/add",
                                    json={"parent": "P", "folder": "sub1", "name": "x"})
        self.assertEqual(resp.status_code, 201)
        args = run.call_args[0][0]
        self.assertEqual(args[:3], ["sub", "add", "sub1"])
        self.assertIn("--json", args)
        self.assertEqual(resp.get_json()["result"]["added"], True)

    def test_subprojects_remove_validates_mode(self):
        resp = self.client.post("/api/projects/subprojects/remove",
                                json={"parent": "P", "ref": "x", "mode": "explode"})
        self.assertEqual(resp.status_code, 400)

    def test_subprojects_cascade_validates_op(self):
        resp = self.client.post("/api/projects/subprojects/cascade",
                                json={"parent": "P", "op": "explode"})
        self.assertEqual(resp.status_code, 400)

    def test_subprojects_cascade_status_shape(self):
        resp = self.client.get("/api/projects/subprojects/cascade/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        for key in ("running", "results", "total", "done"):
            self.assertIn(key, data)

    def test_subprojects_cascade_cancel_idle(self):
        resp = self.client.post("/api/projects/subprojects/cascade/cancel")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["cancelled"])

    # ── Modular hub UI bundle (v2.44.0) ────────────────────────────

    def test_root_serves_concatenated_bundle(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.content_type)
        html = resp.get_data(as_text=True)
        self.assertIn("hub_ui/app.js", html, "bundle marker missing")
        self.assertNotIn("/* __C3_HUB_SCRIPTS__ */", html, "placeholder not replaced")
        self.assertEqual(html.count("ReactDOM.render"), 1)

    def test_all_hub_js_files_exist(self):
        cli_dir = Path(self.mod.__file__).parent
        missing = [rel for rel in self.mod._HUB_JS_FILES
                   if not (cli_dir / rel).exists()]
        self.assertEqual(missing, [], f"_HUB_JS_FILES entries missing on disk: {missing}")

    def test_legacy_route_serves_old_hub(self):
        resp = self.client.get("/legacy")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("C3 Project Hub", resp.get_data(as_text=True))

    def test_pm_bundle_entries_present(self):
        for rel in ("ui/pm_shared.js", "hub_ui/components/drill_tasks.js",
                    "hub_ui/components/task_board.js"):
            self.assertIn(rel, self.mod._HUB_JS_FILES)

    def test_project_ui_bundle_files_exist(self):
        from cli import server as project_server
        cli_dir = Path(self.mod.__file__).parent
        for rel in ("ui/pm_shared.js", "ui/components/tasks.js"):
            self.assertIn(rel, project_server._UI_JS_FILES)
        missing = [rel for rel in project_server._UI_JS_FILES
                   if not (cli_dir / rel).exists()]
        self.assertEqual(missing, [], f"_UI_JS_FILES entries missing on disk: {missing}")

    def test_parse_json_tail_skips_init_noise(self):
        payload = self.mod._parse_json_tail(
            "Building code index...\n  Indexed 3 files\n{\n  \"added\": true\n}")
        self.assertEqual(payload, {"added": True})
        self.assertIsNone(self.mod._parse_json_tail("no json here"))

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
