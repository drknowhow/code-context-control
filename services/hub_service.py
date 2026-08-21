"""
Background service for the C3 Project Hub.

The platform mechanics (Task Scheduler / Run key, launchd, systemd, the
pythonw launcher contract) live in ``services.background_service``; this
module only says what makes the hub the hub. ``OracleService`` is the
sibling for ``c3 oracle serve``.

Usage:
    svc = HubService()
    svc.status()             # {"installed", "running", "port", "platform", "log_path", ...}
    svc.install(port=3330)   # register + immediately start background process
    svc.uninstall()          # remove auto-start registration
    svc.start(port=3330)     # start background process now
    svc.stop(port=3330)      # kill process listening on port
"""
from pathlib import Path

from services.background_service import (  # noqa: F401  (re-exported for callers)
    _C3_PY,
    BackgroundService,
    _kill_port_unix,
    _kill_port_win,
    _probe_host,
    _pythonw,
    _read_json,
    _win_reg_registered,
    _win_startup_dir,
)

_LOG_FILE = Path.home() / ".c3" / "hub.log"


class HubService(BackgroundService):
    KEY = "hub"
    DISPLAY_NAME = "C3 Project Hub"
    TASK_NAME = "C3ProjectHub"
    PLIST_LABEL = "com.c3.projecthub"
    SYSTEMD_NAME = "c3hub.service"
    DESCRIPTION = "C3 Project Hub background server"
    LOG_REL = (".c3", "hub.log")
    LAUNCHER_NAME = "hub_start.py"
    LOG_TAG = "c3-hub"
    LOGON_DELAY = "PT30S"

    def _read_hub_config(self) -> dict:
        """Read ~/.c3/hub_config.json (port, host)."""
        return _read_json(Path.home() / ".c3" / "hub_config.json")

    def default_port(self) -> int:
        try:
            return int(self._read_hub_config().get("port", 3330))
        except (TypeError, ValueError):
            return 3330

    def probe_host(self) -> str:
        return _probe_host(self._read_hub_config().get("host", "127.0.0.1"))

    def launch_code(self, port: int) -> str:
        return (
            "from cli.hub_server import run_hub\n"
            "run_hub(port=_PORT, open_browser=False, silent=True, quiet=True)\n"
        )

    def cli_args(self, port: int) -> list:
        return ["hub", "--port", str(port), "--no-browser", "--silent", "--extra-silent"]


# ── Back-compat module-level helpers (cli/hub_server.py imports these) ─────────

def _make_hub_start_script(repo_root: str, port: int) -> Path:
    """Write ~/.c3/hub_start.py — the self-contained launcher for the hub."""
    return HubService().write_launcher(port, repo_root=repo_root)


def _launch_background(port: int):
    """Start the hub as a detached, windowless background process."""
    HubService().launch_background(port)
