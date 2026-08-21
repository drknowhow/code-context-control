"""
Background service for the C3 Oracle (``c3 oracle serve``).

Same mechanics as the hub's (``services.background_service``): a windowless
``pythonw.exe`` launcher registered with Task Scheduler on Windows, a
LaunchAgent on macOS, a systemd user unit on Linux. What differs:

* **port / host come from** ``~/.c3/oracle/config.json`` (``port``,
  ``bind_host``), not from the hub config;
* **"running" is a health check**, not a TCP connect: the Oracle may bind a
  specific interface (a LAN or VPN address) where a plain loopback probe
  never answers, and the probe must also not mistake some other listener on
  3331 for the Oracle;
* the launcher **waits for a non-loopback bind host to come up** before
  starting. At logon a VPN address can take a while to appear, and binding
  before it exists is a fatal ``OSError`` — a one-shot logon task would then
  simply stay dead until the next reboot.

The launcher's own log is ``~/.c3/oracle/service.log``; the Oracle's
application log stays ``~/.c3/oracle/oracle.log``.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from services.background_service import BackgroundService, _probe_host, _read_json

ORACLE_DIR_REL = (".c3", "oracle")
DEFAULT_PORT = 3331


class OracleService(BackgroundService):
    KEY = "oracle"
    DISPLAY_NAME = "C3 Oracle"
    TASK_NAME = "C3Oracle"
    PLIST_LABEL = "com.c3.oracle"
    SYSTEMD_NAME = "c3oracle.service"
    DESCRIPTION = "C3 Oracle background server (dashboard + discovery API)"
    LOG_REL = (*ORACLE_DIR_REL, "service.log")
    LAUNCHER_NAME = "oracle_start.py"
    LOG_TAG = "c3-oracle"
    LOGON_DELAY = "PT45S"   # after the hub's PT30S, so the hub is up first

    # ── config ──

    def _read_oracle_config(self) -> dict:
        return _read_json(Path.home().joinpath(*ORACLE_DIR_REL, "config.json"))

    def default_port(self) -> int:
        try:
            return int(self._read_oracle_config().get("port", DEFAULT_PORT))
        except (TypeError, ValueError):
            return DEFAULT_PORT

    def bind_host(self) -> str:
        return str(self._read_oracle_config().get("bind_host") or "127.0.0.1")

    def probe_host(self) -> str:
        return _probe_host(self.bind_host())

    # ── liveness ──

    # An Oracle that predates ``?probe=1`` ignores the flag and runs its full
    # health check (Ollama 3 s + hub 2 s) before answering; the timeout has to
    # outlast that or a healthy older Oracle reads as "stopped".
    HEALTH_TIMEOUT = 7.0

    def is_running(self, port: int) -> bool:
        """True only when the listener on *port* identifies as the Oracle."""
        url = f"http://{self.probe_host()}:{port}/api/health?probe=1"
        try:
            with urllib.request.urlopen(url, timeout=self.HEALTH_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return isinstance(data, dict) and data.get("service") == "c3-oracle"
        except Exception:
            return False

    def status(self) -> dict:
        st = super().status()
        cfg = self._read_oracle_config()
        st["bind_host"] = self.bind_host()
        st["mcp_port"] = cfg.get("mcp_port", 3332)
        return st

    # ── launcher ──

    def launch_code(self, port: int) -> str:
        return (
            "from oracle.oracle_server import run_oracle\n"
            "run_oracle(port=_PORT, open_browser=False)\n"
        )

    def launcher_extra(self, port: int) -> str:
        return """
            # The Oracle may bind a specific interface (LAN / VPN address). At
            # logon that address can take a while to come up, and binding before
            # it exists is a fatal OSError. Wait up to 120 s for a test bind.
            import json, socket
            _host = '127.0.0.1'
            try:
                _cfg_path = Path.home() / '.c3' / 'oracle' / 'config.json'
                if _cfg_path.exists():
                    _host = str(json.loads(_cfg_path.read_text(encoding='utf-8')).get('bind_host') or '127.0.0.1')
                if _host not in ('0.0.0.0', '127.0.0.1', 'localhost') and ':' not in _host:
                    for _i in range(24):
                        try:
                            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
                                _s.bind((_host, 0))
                            break
                        except OSError:
                            if _i == 0:
                                _log(f'waiting for bind host {_host} to come up')
                            time.sleep(5)
                    else:
                        _log(f'bind host {_host} did not come up in 120 s; starting anyway')
            except Exception as _e:
                _log(f'bind-host check skipped: {_e}')

            # Never start a second Oracle. Windows lets two listeners share a
            # port (SO_REUSEADDR), so a duplicate does not fail to bind - it
            # silently splits the traffic, loses the MCP port, and can take the
            # first instance down with it when either one exits.
            if ':' not in _host:
                try:
                    import urllib.request
                    _probe = '127.0.0.1' if _host in ('0.0.0.0', '127.0.0.1', 'localhost') else _host
                    with urllib.request.urlopen(f'http://{_probe}:{_PORT}/api/health?probe=1', timeout=7) as _r:
                        _who = json.loads(_r.read().decode('utf-8', 'replace')).get('service')
                    if _who == 'c3-oracle':
                        _log(f'an Oracle already answers on {_probe}:{_PORT}; not starting a second one')
                        sys.exit(0)
                except SystemExit:
                    raise
                except Exception:
                    pass
        """

    def cli_args(self, port: int) -> list:
        return ["oracle", "serve", "--port", str(port), "--no-browser"]
