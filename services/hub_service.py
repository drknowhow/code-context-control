"""
Cross-platform background service manager for C3 Project Hub.

Windows  → Windows Task Scheduler (ONLOGON trigger, pythonw.exe)
macOS    → launchd LaunchAgent (~/.config/LaunchAgents/)
Linux    → systemd user service (~/.config/systemd/user/)

Usage:
    svc = HubService()
    svc.status()             # {"installed", "running", "platform", "log_path"}
    svc.install(port=3330)   # register + immediately start background process
    svc.uninstall()          # remove auto-start registration
    svc.start(port=3330)     # start background process now
    svc.stop(port=3330)      # kill process listening on port
"""
import os
import subprocess
import sys
import json
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

_C3_PY = Path(__file__).parent.parent / "cli" / "c3.py"
_LOG_FILE = Path.home() / ".c3" / "hub.log"

# ── Windows helpers ───────────────────────────────────────────────────────────

def _pythonw() -> str:
    """Return pythonw.exe path (silent, no console window) on Windows."""
    pw = Path(sys.executable).parent / "pythonw.exe"
    return str(pw) if pw.exists() else sys.executable


def _win_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    return (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def _vbs_escape(value: str) -> str:
    return value.replace('"', '""')


def _win_reg_registered(task_name: str) -> bool:
    """Check if the hub is registered in the HKCU Run key."""
    if not winreg:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            winreg.QueryValueEx(key, task_name)
            return True
    except (OSError, FileNotFoundError):
        return False


def _kill_port_win(port: int) -> bool:
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            f"netstat -ano | findstr :{port}",
            shell=True, capture_output=True, text=True,
            **kwargs
        )
        pids = set()
        for line in r.stdout.strip().splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.strip().split()
                if len(parts) >= 5:
                    pids.add(parts[-1])
        for pid in pids:
            subprocess.run(f"taskkill /PID {pid} /F", shell=True, capture_output=True, **kwargs)
        return bool(pids)
    except Exception:
        return False


def _kill_port_unix(port: int) -> bool:
    try:
        subprocess.run(
            f"lsof -ti:{port} | xargs kill -9",
            shell=True, capture_output=True,
        )
        return True
    except Exception:
        return False


def _make_hub_start_script(repo_root: str, port: int) -> Path:
    """Write ~/.c3/hub_start.py — a self-contained launcher for the background hub.

    Stored on the local drive so it is always accessible even before network
    drives mount.  Sets sys.path itself (no PYTHONPATH needed) and redirects
    all output to hub.log so startup errors are visible.
    """
    script_path = Path.home() / ".c3" / "hub_start.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)

    escaped = repr(repo_root)       # produces 'r"..."' or regular quoted string
    content = (
        "import sys, os, time\n"
        "from pathlib import Path\n"
        "\n"
        f"_REPO = {escaped}\n"
        f"_PORT = {port}\n"
        "_LOG  = Path.home() / '.c3' / 'hub.log'\n"
        "\n"
        "# Capture all output so errors are visible in hub.log\n"
        "_LOG.parent.mkdir(parents=True, exist_ok=True)\n"
        "_fh = open(str(_LOG), 'a', encoding='utf-8', buffering=1)\n"
        "sys.stdout = _fh\n"
        "sys.stderr = _fh\n"
        "\n"
        "# Wait up to 60 s for the repo to be accessible (network-drive mounts)\n"
        "for _i in range(12):\n"
        "    if Path(_REPO).exists():\n"
        "        break\n"
        "    time.sleep(5)\n"
        "else:\n"
        "    import datetime\n"
        "    print(f'[c3-hub] {datetime.datetime.now()} repo not accessible after 60 s: {_REPO}', flush=True)\n"
        "    sys.exit(1)\n"
        "\n"
        "sys.path.insert(0, _REPO)\n"
        "os.chdir(_REPO)\n"
        "\n"
        "try:\n"
        "    from cli.hub_server import run_hub\n"
        "    run_hub(port=_PORT, open_browser=False, silent=True, quiet=True)\n"
        "except Exception as _e:\n"
        "    import traceback, datetime\n"
        "    print(f'[c3-hub] {datetime.datetime.now()} STARTUP ERROR: {_e}', flush=True)\n"
        "    traceback.print_exc(file=_fh)\n"
    )
    script_path.write_text(content, encoding="utf-8")
    return script_path


def _launch_background(port: int):
    """Start hub as a detached background process in quiet background mode."""
    start_script = _make_hub_start_script(str(Path(__file__).parent.parent), port)
    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    exe = _pythonw() if sys.platform == "win32" else sys.executable
    cmd = [exe, str(start_script)]
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


# ── HubService ────────────────────────────────────────────────────────────────

class HubService:
    TASK_NAME    = "C3ProjectHub"
    PLIST_LABEL  = "com.c3.projecthub"
    SYSTEMD_NAME = "c3hub.service"

    # ── Public API ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        if sys.platform == "win32":
            return self._win_status()
        elif sys.platform == "darwin":
            return self._mac_status()
        else:
            return self._linux_status()

    def install(self, port: int) -> dict:
        if sys.platform == "win32":
            r = self._win_install(port)
        elif sys.platform == "darwin":
            r = self._mac_install(port)
        else:
            r = self._linux_install(port)
        if r.get("success"):
            _launch_background(port)
        return r

    def uninstall(self) -> dict:
        if sys.platform == "win32":
            return self._win_uninstall()
        elif sys.platform == "darwin":
            return self._mac_uninstall()
        else:
            return self._linux_uninstall()

    def start(self, port: int) -> dict:
        try:
            _launch_background(port)
            return {"success": True, "output": f"Hub starting on port {port}…"}
        except Exception as e:
            return {"success": False, "output": str(e)}

    def stop(self, port: int) -> dict:
        try:
            if sys.platform == "win32":
                ok = _kill_port_win(port)
            else:
                ok = _kill_port_unix(port)
            return {"success": ok, "output": f"Killed process on :{port}" if ok else "No process found"}
        except Exception as e:
            return {"success": False, "output": str(e)}

    # ── Windows ───────────────────────────────────────────────────────────────

    @property
    def _startup_script_path(self) -> Path:
        return _win_startup_dir() / f"{self.TASK_NAME}.vbs"

    def _win_task_registered(self) -> bool:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", self.TASK_NAME, "/fo", "LIST"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return r.returncode == 0

    def _win_status(self) -> dict:
        cfg = self._read_hub_config()
        port = cfg.get("port", 3330)
        task_installed = self._win_task_registered()
        reg_installed = _win_reg_registered(self.TASK_NAME)
        startup_installed = self._startup_script_path.exists()
        running = self._is_port_alive(port)
        
        if task_installed:
            method = "Windows Task Scheduler (runs at login, no terminal)"
        elif reg_installed:
            method = "Windows Registry Run key (runs at login, silent)"
        elif startup_installed:
            method = "Windows Startup folder — legacy, consider reinstalling"
        else:
            method = "not installed"
            
        return {
            "installed": task_installed or reg_installed or startup_installed,
            "running": running,
            "port": port,
            "platform": "windows",
            "log_path": str(_LOG_FILE),
            "method": method,
        }

    def _is_port_alive(self, port: int) -> bool:
        """Check if anything is listening on the given port."""
        import socket
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return True
        except Exception:
            return False

    def _read_hub_config(self) -> dict:
        """Read hub config from ~/.c3/hub_config.json."""
        import json
        config_path = Path.home() / ".c3" / "hub_config.json"
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _win_reg_install(self, pythonw: str, start_script: Path) -> bool:
        """Register the hub in the HKCU Run key."""
        if not winreg:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                # Command: "pythonw.exe" "hub_start.py"
                cmd = f'"{pythonw}" "{start_script}"'
                winreg.SetValueEx(key, self.TASK_NAME, 0, winreg.REG_SZ, cmd)
                return True
        except OSError:
            return False

    def _win_reg_uninstall(self) -> bool:
        """Remove the hub from the HKCU Run key."""
        if not winreg:
            return False
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, self.TASK_NAME)
                return True
        except (OSError, FileNotFoundError):
            return False

    def _win_install(self, port: int) -> dict:
        r"""Register the hub for auto-start on Windows.

        Tries Windows Task Scheduler first (allows 30s delay).
        Falls back to HKCU\...\Run registry key if Task Scheduler fails (e.g. Access Denied).
        """
        pythonw   = _pythonw()
        repo_root = str(Path(__file__).parent.parent)

        # Write the launcher script to the local drive (~/.c3/hub_start.py)
        start_script = _make_hub_start_script(repo_root, port)

        def _xe(s: str) -> str:
            """Minimal XML attribute/text escaping."""
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

        task_xml = (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            '  <RegistrationInfo>\n'
            '    <Description>C3 Project Hub background server</Description>\n'
            '  </RegistrationInfo>\n'
            '  <Triggers>\n'
            '    <LogonTrigger>\n'
            '      <Enabled>true</Enabled>\n'
            '      <Delay>PT30S</Delay>\n'
            '    </LogonTrigger>\n'
            '  </Triggers>\n'
            '  <Principals>\n'
            '    <Principal id="Author">\n'
            '      <LogonType>InteractiveToken</LogonType>\n'
            '      <RunLevel>LeastPrivilege</RunLevel>\n'
            '    </Principal>\n'
            '  </Principals>\n'
            '  <Settings>\n'
            '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
            '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
            '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
            '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n'
            '    <WakeToRun>false</WakeToRun>\n'
            '  </Settings>\n'
            '  <Actions Context="Author">\n'
            '    <Exec>\n'
            f'      <Command>{_xe(pythonw)}</Command>\n'
            f'      <Arguments>{_xe(str(start_script))}</Arguments>\n'
            '    </Exec>\n'
            '  </Actions>\n'
            '</Task>\n'
        )

        import tempfile
        tmp_xml = None
        messages = []
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", delete=False,
                encoding="utf-16", prefix="c3hub_",
            ) as f:
                f.write(task_xml)
                tmp_xml = f.name

            # Try Task Scheduler
            r = subprocess.run(
                ["schtasks", "/create", "/tn", self.TASK_NAME, "/xml", tmp_xml, "/f"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                messages.append(f"Task '{self.TASK_NAME}' registered in Task Scheduler.")
                # Clean up registry if it was there before
                self._win_reg_uninstall()
            else:
                # Fallback to Registry
                if self._win_reg_install(pythonw, start_script):
                    messages.append("Task Scheduler failed (Access Denied); registered in Registry Run key instead.")
                else:
                    out = (r.stdout + r.stderr).strip()
                    return {"success": False, "output": out or "Failed to register via Task Scheduler or Registry."}

            # Remove legacy Startup-folder VBS if present
            if self._startup_script_path.exists():
                self._startup_script_path.unlink()
                messages.append("Removed legacy startup-folder script.")

            return {"success": True, "output": "\n".join(messages)}

        except Exception as e:
            return {"success": False, "output": str(e)}
        finally:
            if tmp_xml:
                try:
                    os.unlink(tmp_xml)
                except Exception:
                    pass

    def _win_uninstall(self) -> dict:
        messages = []
        success = True

        # Remove Task Scheduler task
        if self._win_task_registered():
            r = subprocess.run(
                ["schtasks", "/delete", "/tn", self.TASK_NAME, "/f"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            out = (r.stdout + r.stderr).strip()
            if r.returncode == 0:
                messages.append("Task Scheduler task removed.")
            else:
                success = False
                messages.append(out or "Failed to remove Task Scheduler task.")

        # Remove Registry key
        if _win_reg_registered(self.TASK_NAME):
            if self._win_reg_uninstall():
                messages.append("Registry Run key removed.")
            else:
                success = False
                messages.append("Failed to remove Registry Run key.")

        # Remove the hub_start.py launcher script
        start_script = Path.home() / ".c3" / "hub_start.py"
        if start_script.exists():
            start_script.unlink()
            messages.append("Launcher script removed.")

        # Remove legacy Startup-folder VBS if still present
        if self._startup_script_path.exists():
            self._startup_script_path.unlink()
            messages.append("Legacy startup-folder script removed.")

        return {
            "success": success,
            "output": "\n".join(messages) or "No startup registration found.",
        }

    # ── macOS ─────────────────────────────────────────────────────────────────

    @property
    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.PLIST_LABEL}.plist"

    def _mac_status(self) -> dict:
        installed = self._plist_path.exists()
        running = None
        if installed:
            r = subprocess.run(
                ["launchctl", "list", self.PLIST_LABEL],
                capture_output=True, text=True,
            )
            running = r.returncode == 0
        return {
            "installed": installed,
            "running": running,
            "platform": "macos",
            "log_path": str(_LOG_FILE),
            "method": "launchd LaunchAgent (RunAtLoad)",
        }

    def _mac_install(self, port: int) -> dict:
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{self.PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{_C3_PY}</string>
    <string>hub</string>
    <string>--port</string><string>{port}</string>
    <string>--no-browser</string>
    <string>--silent</string>
    <string>--extra-silent</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{_LOG_FILE}</string>
  <key>StandardErrorPath</key><string>{_LOG_FILE}</string>
</dict></plist>"""
        self._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self._plist_path.write_text(plist, encoding="utf-8")
        r = subprocess.run(
            ["launchctl", "load", str(self._plist_path)],
            capture_output=True, text=True,
        )
        return {
            "success": r.returncode == 0,
            "output": (r.stdout + r.stderr).strip() or "LaunchAgent loaded.",
        }

    def _mac_uninstall(self) -> dict:
        if self._plist_path.exists():
            subprocess.run(
                ["launchctl", "unload", str(self._plist_path)],
                capture_output=True,
            )
            self._plist_path.unlink()
        return {"success": True, "output": "LaunchAgent removed."}

    # ── Linux (systemd user) ──────────────────────────────────────────────────

    @property
    def _service_path(self) -> Path:
        return (
            Path.home() / ".config" / "systemd" / "user" / self.SYSTEMD_NAME
        )

    def _linux_status(self) -> dict:
        installed = self._service_path.exists()
        running = None
        if installed:
            r = subprocess.run(
                ["systemctl", "--user", "is-active", self.SYSTEMD_NAME],
                capture_output=True, text=True,
            )
            running = r.stdout.strip() == "active"
        return {
            "installed": installed,
            "running": running,
            "platform": "linux",
            "log_path": str(_LOG_FILE),
            "method": "systemd user service (loginctl linger recommended)",
        }

    def _linux_install(self, port: int) -> dict:
        unit = (
            "[Unit]\n"
            "Description=C3 Project Hub\n"
            "After=network.target\n\n"
            "[Service]\n"
            f"ExecStart={sys.executable} {_C3_PY} hub --port {port} --no-browser --silent --extra-silent\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            f"StandardOutput=append:{_LOG_FILE}\n"
            f"StandardError=append:{_LOG_FILE}\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        self._service_path.parent.mkdir(parents=True, exist_ok=True)
        self._service_path.write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(
            ["systemctl", "--user", "enable", "--now", self.SYSTEMD_NAME],
            capture_output=True, text=True,
        )
        return {
            "success": r.returncode == 0,
            "output": (r.stdout + r.stderr).strip() or "Service enabled and started.",
        }

    def _linux_uninstall(self) -> dict:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", self.SYSTEMD_NAME],
            capture_output=True,
        )
        if self._service_path.exists():
            self._service_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return {"success": True, "output": "systemd user service removed."}
