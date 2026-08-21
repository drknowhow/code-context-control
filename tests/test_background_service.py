"""services/background_service.py — the shared login-service machinery, and
the two services built on it (hub, oracle).

What is pinned here and why:

* The Windows launcher must redirect stdout/stderr to the log BEFORE the
  server is imported. Under pythonw.exe both streams are None, and uvicorn's
  log formatter calls sys.stdout.isatty() — that is how the Oracle's MCP
  listener once died at startup while the REST port stayed up, passing
  every "is it alive" probe. The order of those two lines is the guard.
* The hub and the oracle must never share a task name, launcher or log.
* The oracle's port/host come from ~/.c3/oracle/config.json and "running"
  is a health check on the configured bind host, not a loopback connect.
* `start` must not launch a second copy of something already answering.
* The netstat parser matches the port exactly (:3331 is not :33310).
* cli/hub_server.py imports `_launch_background` / `HubService` from
  services.hub_service; the refactor must keep them.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import background_service as bs  # noqa: E402
from services.hub_service import (  # noqa: E402
    HubService,
    _launch_background,
    _make_hub_start_script,
)
from services.oracle_service import OracleService  # noqa: E402


class _HomeCase(unittest.TestCase):
    """Every test gets a throwaway HOME so nothing touches ~/.c3."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._home_patch = mock.patch("pathlib.Path.home", return_value=self.home)
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def write_oracle_cfg(self, **cfg):
        p = self.home / ".c3" / "oracle" / "config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg), encoding="utf-8")

    def write_hub_cfg(self, **cfg):
        p = self.home / ".c3" / "hub_config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg), encoding="utf-8")


class TestIdentities(_HomeCase):
    def test_hub_and_oracle_never_collide(self):
        h, o = HubService(), OracleService()
        for attr in ("KEY", "TASK_NAME", "PLIST_LABEL", "SYSTEMD_NAME", "LAUNCHER_NAME", "LOG_TAG"):
            self.assertNotEqual(getattr(h, attr), getattr(o, attr), attr)
        self.assertNotEqual(h.LOG_FILE, o.LOG_FILE)
        self.assertNotEqual(h.launcher_path, o.launcher_path)

    def test_hub_identity_is_unchanged_by_the_refactor(self):
        h = HubService()
        self.assertEqual(h.TASK_NAME, "C3ProjectHub")
        self.assertEqual(h.PLIST_LABEL, "com.c3.projecthub")
        self.assertEqual(h.SYSTEMD_NAME, "c3hub.service")
        self.assertEqual(h.launcher_path, self.home / ".c3" / "hub_start.py")
        self.assertEqual(h.LOG_FILE, self.home / ".c3" / "hub.log")

    def test_oracle_identity(self):
        o = OracleService()
        self.assertEqual(o.TASK_NAME, "C3Oracle")
        self.assertEqual(o.launcher_path, self.home / ".c3" / "oracle_start.py")
        # The launcher's log is NOT the Oracle's own oracle.log — the server
        # also attaches a StreamHandler to stderr, and sharing the file would
        # print every line twice.
        self.assertEqual(o.LOG_FILE, self.home / ".c3" / "oracle" / "service.log")


class TestLauncher(_HomeCase):
    def _src(self, svc, port):
        src = svc.launcher_source(r"U:\repo", port)
        compile(src, "<launcher>", "exec")  # must be valid Python
        return src

    def test_oracle_launcher_redirects_streams_before_importing_the_server(self):
        src = self._src(OracleService(), 3331)
        redirect = src.index("sys.stderr = _fh")
        imp = src.index("from oracle.oracle_server import run_oracle")
        self.assertLess(redirect, imp)
        self.assertIn("run_oracle(port=_PORT, open_browser=False)", src)
        self.assertIn("_PORT = 3331", src)

    def test_hub_launcher_redirects_streams_before_importing_the_server(self):
        src = self._src(HubService(), 3330)
        self.assertLess(src.index("sys.stderr = _fh"), src.index("from cli.hub_server import run_hub"))
        self.assertIn("run_hub(port=_PORT, open_browser=False, silent=True, quiet=True)", src)

    def test_launcher_exits_nonzero_on_startup_error(self):
        # Task Scheduler's restart-on-failure only fires on a non-zero exit.
        src = self._src(OracleService(), 3331)
        tail = src[src.index("except Exception as _e:"):]
        self.assertIn("sys.exit(1)", tail)

    def test_oracle_launcher_waits_for_a_non_loopback_bind_host(self):
        src = self._src(OracleService(), 3331)
        self.assertIn("bind_host", src)
        self.assertIn("_s.bind((_host, 0))", src)
        self.assertLess(src.index("_s.bind((_host, 0))"), src.index("from oracle.oracle_server"))

    def test_oracle_launcher_refuses_to_start_a_second_instance(self):
        # Windows lets two SO_REUSEADDR listeners share a port, so a duplicate
        # Oracle at logon does not fail to bind - it splits the traffic and
        # loses the MCP port. The launcher must ask first and exit 0 (no
        # Task Scheduler retry) when an Oracle already answers.
        self.write_oracle_cfg(bind_host="127.0.0.1", port=3331)
        repo = self.home / "repo"
        repo.mkdir()
        src = OracleService().launcher_source(str(repo), 3331)

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        seen = {}
        def fake_urlopen(url, timeout=None):
            seen["url"] = url
            return _Resp(b'{"service": "c3-oracle"}')

        ns = {"__name__": "__main__"}
        cwd, out, err = os.getcwd(), sys.stdout, sys.stderr
        try:
            with mock.patch("urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(SystemExit) as cm:
                    exec(compile(src, "oracle_start.py", "exec"), ns)
        finally:
            os.chdir(cwd)
            sys.stdout, sys.stderr = out, err
            fh = ns.get("_fh")
            if fh:
                fh.close()
            if str(repo) in sys.path:
                sys.path.remove(str(repo))
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(seen["url"], "http://127.0.0.1:3331/api/health?probe=1")
        log = (self.home / ".c3" / "oracle" / "service.log").read_text(encoding="utf-8")
        self.assertIn("not starting a second one", log)

    def test_write_launcher_lands_on_the_local_drive(self):
        o = OracleService()
        path = o.write_launcher(3331)
        self.assertEqual(path, self.home / ".c3" / "oracle_start.py")
        self.assertIn("_REPO = ", path.read_text(encoding="utf-8"))

    def test_launcher_log_path_is_resolved_at_runtime_not_install_time(self):
        src = self._src(OracleService(), 3331)
        self.assertIn("Path.home().joinpath('.c3', 'oracle', 'service.log')", src)
        self.assertNotIn(str(self.home), src)

    def test_cli_args_for_launchd_and_systemd(self):
        self.assertEqual(OracleService().cli_args(4444),
                         ["oracle", "serve", "--port", "4444", "--no-browser"])
        hub = HubService().cli_args(3330)
        self.assertEqual(hub[:1], ["hub"])
        self.assertIn("--no-browser", hub)


class TestOracleConfig(_HomeCase):
    def test_defaults_without_a_config_file(self):
        o = OracleService()
        self.assertEqual(o.default_port(), 3331)
        self.assertEqual(o.probe_host(), "127.0.0.1")
        self.assertEqual(o.url(), "http://localhost:3331")

    def test_port_and_bind_host_come_from_oracle_config(self):
        self.write_oracle_cfg(port=4444, bind_host="100.77.40.101", mcp_port=4445)
        o = OracleService()
        self.assertEqual(o.default_port(), 4444)
        self.assertEqual(o.probe_host(), "100.77.40.101")
        self.assertEqual(o.url(), "http://100.77.40.101:4444")

    def test_wildcard_bind_probes_loopback(self):
        self.write_oracle_cfg(bind_host="0.0.0.0")
        o = OracleService()
        self.assertEqual(o.probe_host(), "127.0.0.1")
        self.assertEqual(o.url(), "http://localhost:3331")

    def test_hub_port_comes_from_hub_config_not_oracle_config(self):
        self.write_oracle_cfg(port=4444)
        self.write_hub_cfg(port=5555, host="0.0.0.0")
        h = HubService()
        self.assertEqual(h.default_port(), 5555)
        self.assertEqual(h.probe_host(), "127.0.0.1")
        self.assertEqual(OracleService().default_port(), 4444)


class TestOracleLiveness(_HomeCase):
    def _urlopen(self, payload, raise_exc=None):
        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def fake(url, timeout=None):
            self.probed_url = url
            self.probed_timeout = timeout
            if raise_exc:
                raise raise_exc
            return _Resp(json.dumps(payload).encode())
        return fake

    def test_running_when_health_identifies_as_oracle(self):
        self.write_oracle_cfg(bind_host="100.77.40.101", port=3331)
        o = OracleService()
        with mock.patch("urllib.request.urlopen", self._urlopen({"service": "c3-oracle"})):
            self.assertTrue(o.is_running(3331))
        self.assertEqual(self.probed_url, "http://100.77.40.101:3331/api/health?probe=1")

    def test_probe_timeout_outlasts_an_older_oracles_full_health_check(self):
        # Pre-probe Oracles answer /api/health only after Ollama (3 s) + hub
        # (2 s) probes; a shorter timeout showed a healthy Oracle as stopped.
        o = OracleService()
        with mock.patch("urllib.request.urlopen", self._urlopen({"service": "c3-oracle"})):
            o.is_running(3331)
        self.assertGreaterEqual(self.probed_timeout, 5.5)

    def test_not_running_when_something_else_owns_the_port(self):
        o = OracleService()
        with mock.patch("urllib.request.urlopen", self._urlopen({"service": "c3-hub"})):
            self.assertFalse(o.is_running(3331))

    def test_not_running_when_unreachable(self):
        o = OracleService()
        with mock.patch("urllib.request.urlopen", self._urlopen(None, raise_exc=OSError("refused"))):
            self.assertFalse(o.is_running(3331))


class TestStartStop(_HomeCase):
    def test_start_does_not_launch_a_second_copy(self):
        o = OracleService()
        with mock.patch.object(OracleService, "is_running", return_value=True), \
                mock.patch.object(OracleService, "launch_background") as launch:
            r = o.start(3331)
        self.assertTrue(r["success"])
        self.assertTrue(r["already_running"])
        launch.assert_not_called()

    def test_start_launches_when_down(self):
        o = OracleService()
        with mock.patch.object(OracleService, "is_running", return_value=False), \
                mock.patch.object(OracleService, "launch_background") as launch:
            r = o.start(3331)
        self.assertTrue(r["success"])
        self.assertFalse(r["already_running"])
        launch.assert_called_once_with(3331)

    def test_start_reports_a_launch_failure(self):
        o = OracleService()
        with mock.patch.object(OracleService, "is_running", return_value=False), \
                mock.patch.object(OracleService, "launch_background", side_effect=OSError("no pythonw")):
            r = o.start(3331)
        self.assertFalse(r["success"])
        self.assertIn("no pythonw", r["output"])

    def test_install_starts_after_registering(self):
        o = OracleService()
        plat = {"win32": "_win_install", "darwin": "_mac_install"}.get(sys.platform, "_linux_install")
        with mock.patch.object(OracleService, plat, return_value={"success": True, "output": "registered"}), \
                mock.patch.object(OracleService, "start",
                                  return_value={"success": True, "output": "starting"}) as start:
            r = o.install(3331)
        self.assertTrue(r["success"])
        start.assert_called_once_with(3331)
        self.assertIn("registered", r["output"])
        self.assertIn("starting", r["output"])

    def test_install_does_not_start_when_registration_failed(self):
        o = OracleService()
        plat = {"win32": "_win_install", "darwin": "_mac_install"}.get(sys.platform, "_linux_install")
        with mock.patch.object(OracleService, plat, return_value={"success": False, "output": "denied"}), \
                mock.patch.object(OracleService, "start") as start:
            r = o.install(3331)
        self.assertFalse(r["success"])
        start.assert_not_called()

    def test_launch_background_uses_pythonw_on_windows_and_detaches(self):
        o = OracleService()
        with mock.patch.object(bs.subprocess, "Popen") as popen:
            o.launch_background(3331)
        argv, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(Path(argv[1]), self.home / ".c3" / "oracle_start.py")
        self.assertEqual(kwargs["stdin"], bs.subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertTrue(argv[0].lower().endswith("pythonw.exe") or argv[0] == sys.executable)
            self.assertTrue(kwargs["creationflags"] & bs.subprocess.DETACHED_PROCESS)
            self.assertTrue(kwargs["creationflags"] & bs.subprocess.CREATE_NO_WINDOW)
            # Redirecting only stdin makes Windows hand the child the parent's
            # stdout/stderr; `c3 oracle serve --install | tail` then never
            # returns because the detached server holds the pipe. All three
            # must be redirected.
            self.assertEqual(kwargs["stdout"], bs.subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], bs.subprocess.DEVNULL)
        else:
            self.assertTrue(kwargs["start_new_session"])


class TestNetstatParser(unittest.TestCase):
    NETSTAT = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:3330         0.0.0.0:0              LISTENING       46020\n"
        "  TCP    100.77.40.101:3331     0.0.0.0:0              LISTENING       1111\n"
        "  TCP    0.0.0.0:33310          0.0.0.0:0              LISTENING       2222\n"
        "  TCP    127.0.0.1:3331         127.0.0.1:5000         ESTABLISHED     3333\n"
        "  TCP    [::]:3331              [::]:0                 LISTENING       4444\n"
        "  UDP    0.0.0.0:3331           *:*                                    5555\n"
    )

    def test_exact_port_match(self):
        self.assertEqual(bs._listening_pids_from_netstat(self.NETSTAT, 3331), {"1111", "4444"})

    def test_prefix_port_is_not_a_match(self):
        self.assertEqual(bs._listening_pids_from_netstat(self.NETSTAT, 3330), {"46020"})
        self.assertNotIn("2222", bs._listening_pids_from_netstat(self.NETSTAT, 3331))

    def test_no_listener(self):
        self.assertEqual(bs._listening_pids_from_netstat(self.NETSTAT, 9999), set())


class TestTaskXml(_HomeCase):
    def test_task_xml_is_well_formed_and_carries_the_launcher(self):
        o = OracleService()
        xml = o.task_xml(r"C:\Python\pythonw.exe", o.launcher_path)
        root = ET.fromstring(xml.encode("utf-16"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:Exec/t:Command", ns).text, r"C:\Python\pythonw.exe")
        self.assertEqual(root.find(".//t:Exec/t:Arguments", ns).text, str(o.launcher_path))
        self.assertEqual(root.find(".//t:LogonTrigger/t:Delay", ns).text, "PT45S")
        self.assertIsNotNone(root.find(".//t:RestartOnFailure", ns))
        self.assertIn("Oracle", root.find(".//t:Description", ns).text)

    def test_hub_task_keeps_its_delay(self):
        h = HubService()
        root = ET.fromstring(h.task_xml("pythonw.exe", h.launcher_path).encode("utf-16"))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:LogonTrigger/t:Delay", ns).text, "PT30S")


@unittest.skipUnless(sys.platform == "win32", "Task Scheduler / Run key are Windows-only")
class TestWindowsInstall(_HomeCase):
    def test_install_registers_the_oracle_task_and_drops_a_stale_run_key(self):
        o = OracleService()
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            xml_path = argv[argv.index("/xml") + 1]
            seen["xml"] = Path(xml_path).read_text(encoding="utf-16")
            return mock.Mock(returncode=0, stdout="SUCCESS", stderr="")

        with mock.patch.object(bs.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(OracleService, "_win_reg_uninstall", return_value=True) as reg_rm:
            r = o._win_install(3331)
        self.assertTrue(r["success"], r)
        self.assertEqual(seen["argv"][:4], ["schtasks", "/create", "/tn", "C3Oracle"])
        self.assertIn("oracle_start.py", seen["xml"])
        self.assertIn("pythonw.exe", seen["xml"].lower())
        self.assertTrue(o.launcher_path.exists())
        reg_rm.assert_called_once()

    def test_install_falls_back_to_the_run_key_when_schtasks_is_refused(self):
        o = OracleService()
        with mock.patch.object(bs.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="Access is denied.")), \
                mock.patch.object(OracleService, "_win_reg_install", return_value=True) as reg_add:
            r = o._win_install(3331)
        self.assertTrue(r["success"], r)
        self.assertIn("Registry Run key", r["output"])
        reg_add.assert_called_once()

    def test_install_fails_cleanly_when_both_are_refused(self):
        o = OracleService()
        with mock.patch.object(bs.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="Access is denied.")), \
                mock.patch.object(OracleService, "_win_reg_install", return_value=False):
            r = o._win_install(3331)
        self.assertFalse(r["success"])
        self.assertIn("Access is denied", r["output"])

    def test_uninstall_removes_task_key_and_launcher(self):
        o = OracleService()
        o.write_launcher(3331)
        with mock.patch.object(OracleService, "_win_task_registered", return_value=True), \
                mock.patch.object(bs, "_win_reg_registered", return_value=True), \
                mock.patch.object(OracleService, "_win_reg_uninstall", return_value=True), \
                mock.patch.object(bs.subprocess, "run",
                                  return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            r = o._win_uninstall()
        self.assertTrue(r["success"], r)
        self.assertEqual(run.call_args[0][0][:4], ["schtasks", "/delete", "/tn", "C3Oracle"])
        self.assertFalse(o.launcher_path.exists())
        self.assertIn("Launcher script removed.", r["output"])

    def test_status_reports_method(self):
        o = OracleService()
        with mock.patch.object(OracleService, "_win_task_registered", return_value=True), \
                mock.patch.object(OracleService, "is_running", return_value=False):
            st = o.status()
        self.assertTrue(st["installed"])
        self.assertFalse(st["running"])
        self.assertIn("Task Scheduler", st["method"])
        self.assertEqual(st["service"], "oracle")
        self.assertEqual(st["port"], 3331)
        self.assertIn("url", st)
        self.assertIn("bind_host", st)
        self.assertIn("mcp_port", st)


class TestBackCompat(_HomeCase):
    def test_hub_server_imports_still_resolve(self):
        self.assertTrue(callable(_launch_background))
        self.assertTrue(callable(_make_hub_start_script))

    def test_launch_background_delegates_to_hub_service(self):
        with mock.patch.object(HubService, "launch_background") as launch:
            _launch_background(3330)
        launch.assert_called_once_with(3330)

    def test_make_hub_start_script_writes_hub_start_py(self):
        p = _make_hub_start_script(r"U:\repo", 3330)
        self.assertEqual(p, self.home / ".c3" / "hub_start.py")
        src = p.read_text(encoding="utf-8")
        self.assertIn("_REPO = 'U:\\\\repo'", src)
        self.assertIn("from cli.hub_server import run_hub", src)


if __name__ == "__main__":
    unittest.main()
