"""/api/oracle/service* — the hub's buttons for `c3 oracle serve`.

Flask test-client only; OracleService is mocked so nothing registers a task
or spawns a process. The one piece of real logic on the route layer is
`_adopt_oracle_url`: a successful install/start fills an EMPTY hub
`oracle_url` with the Oracle's address — the exact gap that once hid the
top bar's Open Oracle button while the Oracle was healthy — and never
overwrites a value the user set.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli import hub_server  # noqa: E402
from services.oracle_service import OracleService  # noqa: E402


class _RouteCase(unittest.TestCase):
    def setUp(self):
        self.client = hub_server.app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        c3dir = Path(self._tmp.name) / ".c3"
        c3dir.mkdir()
        self.cfg_file = c3dir / "hub_config.json"
        for name, val in (("_GLOBAL_C3_DIR", c3dir), ("_HUB_CONFIG_FILE", self.cfg_file)):
            p = mock.patch.object(hub_server, name, val)
            p.start()
            self.addCleanup(p.stop)

    def hub_cfg(self) -> dict:
        return json.loads(self.cfg_file.read_text(encoding="utf-8")) if self.cfg_file.exists() else {}

    def set_hub_cfg(self, **cfg):
        self.cfg_file.write_text(json.dumps(cfg), encoding="utf-8")


class TestStatus(_RouteCase):
    def test_status_passes_service_payload_through(self):
        payload = {"installed": True, "running": False, "port": 3331, "platform": "windows",
                   "method": "Windows Task Scheduler (runs at login, no terminal)",
                   "log_path": "x", "service": "oracle", "url": "http://localhost:3331",
                   "bind_host": "127.0.0.1", "mcp_port": 3332}
        with mock.patch.object(OracleService, "status", return_value=payload):
            resp = self.client.get("/api/oracle/service")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), payload)

    def test_unknown_action_is_404(self):
        resp = self.client.post("/api/oracle/service/explode", json={})
        self.assertEqual(resp.status_code, 404)


class TestInstallAndStart(_RouteCase):
    def test_install_fills_an_empty_oracle_url(self):
        self.set_hub_cfg(oracle_url="")
        with mock.patch.object(OracleService, "install", return_value={"success": True, "output": "ok"}), \
                mock.patch.object(OracleService, "url", return_value="http://100.77.40.101:3331"):
            resp = self.client.post("/api/oracle/service/install", json={})
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["oracle_url_set"])
        self.assertEqual(data["oracle_url"], "http://100.77.40.101:3331")
        self.assertEqual(self.hub_cfg()["oracle_url"], "http://100.77.40.101:3331")

    def test_start_does_not_overwrite_a_user_set_oracle_url(self):
        self.set_hub_cfg(oracle_url="http://my.host:9999/")
        with mock.patch.object(OracleService, "start", return_value={"success": True, "output": "ok"}), \
                mock.patch.object(OracleService, "url", return_value="http://localhost:3331"):
            resp = self.client.post("/api/oracle/service/start", json={})
        data = resp.get_json()
        self.assertTrue(data["success"])
        self.assertNotIn("oracle_url_set", data)
        self.assertEqual(data["oracle_url"], "http://my.host:9999/")
        self.assertEqual(self.hub_cfg()["oracle_url"], "http://my.host:9999/")

    def test_failed_start_leaves_config_alone(self):
        self.set_hub_cfg(oracle_url="")
        with mock.patch.object(OracleService, "start", return_value={"success": False, "output": "boom"}):
            resp = self.client.post("/api/oracle/service/start", json={})
        data = resp.get_json()
        self.assertFalse(data["success"])
        self.assertNotIn("oracle_url", data)
        self.assertEqual(self.hub_cfg().get("oracle_url", ""), "")

    def test_install_uses_the_oracles_own_port(self):
        # No port in the body: the service resolves it from ~/.c3/oracle/config.json.
        with mock.patch.object(OracleService, "install", return_value={"success": True, "output": ""}) as inst:
            self.client.post("/api/oracle/service/install", json={})
        inst.assert_called_once_with()


class TestUninstallAndStop(_RouteCase):
    def test_uninstall(self):
        with mock.patch.object(OracleService, "uninstall", return_value={"success": True, "output": "gone"}):
            resp = self.client.post("/api/oracle/service/uninstall", json={})
        self.assertEqual(resp.get_json(), {"success": True, "output": "gone"})

    def test_stop_does_not_take_the_hub_down_with_it(self):
        # Unlike /api/hub/service/stop, stopping the Oracle must leave the hub alive.
        with mock.patch.object(OracleService, "stop", return_value={"success": True, "output": "Killed process on :3331"}), \
                mock.patch.object(hub_server.os, "_exit") as os_exit:
            resp = self.client.post("/api/oracle/service/stop", json={})
        self.assertTrue(resp.get_json()["success"])
        os_exit.assert_not_called()


class TestCliParser(unittest.TestCase):
    def test_oracle_serve_has_service_flags(self):
        from cli.commands.parser import build_parser
        parser = build_parser("0.0.0", lambda v: v)
        a = parser.parse_args(["oracle", "serve", "--install", "--port", "3331"])
        self.assertTrue(a.install)
        self.assertEqual(a.port, 3331)
        self.assertTrue(parser.parse_args(["oracle", "serve", "--status"]).status)
        self.assertTrue(parser.parse_args(["oracle", "serve", "--uninstall"]).uninstall)
        # The alias keeps working.
        self.assertTrue(parser.parse_args(["oracle", "start", "--status"]).status)

    def test_cmd_oracle_routes_service_flags_before_importing_the_server(self):
        import argparse

        from cli import c3
        args = argparse.Namespace(oracle_cmd="serve", install=False, uninstall=False,
                                  status=True, port=None, no_browser=False)
        with mock.patch.object(OracleService, "status", return_value={
                "platform": "test", "installed": False, "running": False, "port": 3331,
                "bind_host": "127.0.0.1", "mcp_port": 3332, "url": "http://localhost:3331",
                "method": "not installed", "log_path": "x"}) as status, \
                mock.patch.dict(sys.modules, {"oracle.oracle_server": None}):
            # sys.modules[...] = None makes `from oracle.oracle_server import run_oracle`
            # raise ImportError — proving the server is never imported for --status.
            c3.cmd_oracle(args)
        status.assert_called_once()


if __name__ == "__main__":
    unittest.main()
