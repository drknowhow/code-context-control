"""
Cross-platform background-service manager for C3's long-running servers.

Windows  → Task Scheduler (logon trigger, pythonw.exe), HKCU Run key fallback
macOS    → launchd LaunchAgent (~/Library/LaunchAgents/)
Linux    → systemd user service (~/.config/systemd/user/)

``BackgroundService`` holds the platform mechanics once. A concrete service
(``HubService``, ``OracleService``) supplies only what differs between them:
the task/label names, where its log goes, which port it defaults to, the
Python body of its launcher script and the ``c3`` arguments launchd/systemd
run.

Windows launcher contract
-------------------------
Every Windows registration runs ``pythonw.exe ~/.c3/<LAUNCHER_NAME>``. The
launcher is a self-contained script written at install time so it survives
``pip`` relocations and starts before network drives are mounted:

* it redirects ``sys.stdout``/``sys.stderr`` to the service log BEFORE the
  server is imported. Under pythonw both streams are ``None`` and anything
  that touches them crashes — uvicorn's log formatter calls
  ``sys.stdout.isatty()`` — which is how the Oracle's MCP listener once died
  at startup while its REST port stayed up;
* it waits up to 60 s for the repo path to exist (network-drive mounts);
* it exits non-zero on a startup exception so Task Scheduler's
  restart-on-failure policy can fire.

Usage::

    svc = OracleService()
    svc.status()         # {"installed", "running", "port", "url", ...}
    svc.install(3331)    # register + start now (no terminal window)
    svc.uninstall()      # remove the registration
    svc.start(3331)      # start now, without registering
    svc.stop(3331)       # kill whatever listens on the port
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

try:
    import winreg
except ImportError:  # not Windows
    winreg = None

_REPO_ROOT = Path(__file__).resolve().parent.parent
_C3_PY = _REPO_ROOT / "cli" / "c3.py"

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_LOOPBACK_HOSTS = {"", "0.0.0.0", "127.0.0.1", "localhost", "::", "::1"}


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _quiet_kwargs() -> dict:
    """subprocess kwargs that never pop a console and never block on stdin."""
    kw: dict = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


def _pythonw() -> str:
    """Return pythonw.exe (no console window) on Windows, else sys.executable."""
    pw = Path(sys.executable).parent / "pythonw.exe"
    return str(pw) if pw.exists() else sys.executable


def _win_startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _win_reg_registered(task_name: str) -> bool:
    """Is *task_name* present in the HKCU Run key?"""
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, task_name)
            return True
    except OSError:
        return False


def _listening_pids_from_netstat(text: str, port: int) -> set:
    """Parse ``netstat -ano -p tcp`` output for PIDs listening on exactly *port*.

    Matches the port as a whole token — ``:3331`` must not catch ``:33310``.
    """
    want = str(port)
    pids = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if state.upper() != "LISTENING":
            continue
        if local.rsplit(":", 1)[-1] == want and pid.isdigit():
            pids.add(pid)
    return pids


def _kill_port_win(port: int) -> bool:
    try:
        r = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=20, **_quiet_kwargs(),
        )
        pids = _listening_pids_from_netstat(r.stdout, port)
        for pid in pids:
            subprocess.run(
                ["taskkill", "/PID", pid, "/F"],
                capture_output=True, timeout=20, **_quiet_kwargs(),
            )
        return bool(pids)
    except Exception:
        return False


def _kill_port_unix(port: int) -> bool:
    try:
        r = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
        )
        pids = [p for p in r.stdout.split() if p.isdigit()]
        import signal
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass
        return bool(pids)
    except Exception:
        return False


def _probe_host(bind_host) -> str:
    """Address to probe for a server bound to *bind_host*.

    A wildcard or loopback bind answers on 127.0.0.1; a specific interface
    (LAN / VPN address) only answers on that address.
    """
    host = str(bind_host or "").strip()
    return "127.0.0.1" if host in _LOOPBACK_HOSTS else host


def _port_alive(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


# ── BackgroundService ──────────────────────────────────────────────────────────

class BackgroundService:
    """Platform mechanics for a login-started, windowless C3 server.

    Subclasses set the identity attributes and implement ``default_port``,
    ``probe_host``, ``launch_code`` and ``cli_args``.
    """

    # ── identity (override) ──
    KEY = "service"                       # short id in API payloads
    DISPLAY_NAME = "C3 service"
    TASK_NAME = "C3Service"               # Task Scheduler task / Run-key value
    PLIST_LABEL = "com.c3.service"        # launchd label
    SYSTEMD_NAME = "c3service.service"    # systemd user unit
    DESCRIPTION = "C3 background server"
    LOG_REL: tuple = (".c3", "service.log")   # relative to the home dir
    LAUNCHER_NAME = "service_start.py"    # written under ~/.c3/
    LOG_TAG = "c3"                        # prefix on launcher log lines
    LOGON_DELAY = "PT30S"                 # ISO-8601 delay after logon (Windows)

    # ── per-service hooks (override) ──

    def default_port(self) -> int:
        raise NotImplementedError

    def probe_host(self) -> str:
        """Address that answers when the server is up (see ``_probe_host``)."""
        return "127.0.0.1"

    def launch_code(self, port: int) -> str:
        """Python statements that import and run the server (blocking)."""
        raise NotImplementedError

    def launcher_extra(self, port: int) -> str:
        """Optional Python statements run after the repo wait, before launch."""
        return ""

    def cli_args(self, port: int) -> list:
        """Arguments after ``c3.py`` for the launchd/systemd registration."""
        raise NotImplementedError

    def is_running(self, port: int) -> bool:
        return _port_alive(self.probe_host(), port)

    def url(self, port: int | None = None) -> str:
        port = port or self.default_port()
        host = self.probe_host()
        return f"http://{'localhost' if host == '127.0.0.1' else host}:{port}"

    # ── derived paths ──

    @property
    def LOG_FILE(self) -> Path:
        return Path.home().joinpath(*self.LOG_REL)

    @property
    def launcher_path(self) -> Path:
        return Path.home() / ".c3" / self.LAUNCHER_NAME

    @property
    def _startup_script_path(self) -> Path:
        """Legacy Startup-folder VBS (pre-Task-Scheduler installs)."""
        return _win_startup_dir() / f"{self.TASK_NAME}.vbs"

    # ── public API ──

    def status(self) -> dict:
        if sys.platform == "win32":
            st = self._win_status()
        elif sys.platform == "darwin":
            st = self._mac_status()
        else:
            st = self._linux_status()
        port = self.default_port()
        st.setdefault("port", port)
        st["service"] = self.KEY
        st["url"] = self.url(port)
        st["log_path"] = str(self.LOG_FILE)
        return st

    def install(self, port: int | None = None) -> dict:
        port = port or self.default_port()
        if sys.platform == "win32":
            r = self._win_install(port)
        elif sys.platform == "darwin":
            r = self._mac_install(port)
        else:
            r = self._linux_install(port)
        if r.get("success"):
            started = self.start(port)
            if started.get("output"):
                r["output"] = (r.get("output", "") + "\n" + started["output"]).strip()
        return r

    def uninstall(self) -> dict:
        if sys.platform == "win32":
            return self._win_uninstall()
        elif sys.platform == "darwin":
            return self._mac_uninstall()
        else:
            return self._linux_uninstall()

    def start(self, port: int | None = None) -> dict:
        port = port or self.default_port()
        try:
            if self.is_running(port):
                return {"success": True, "already_running": True,
                        "output": f"{self.DISPLAY_NAME} already running at {self.url(port)}"}
            self.launch_background(port)
            return {"success": True, "already_running": False,
                    "output": f"{self.DISPLAY_NAME} starting on port {port}…"}
        except Exception as e:
            return {"success": False, "output": str(e)}

    def stop(self, port: int | None = None) -> dict:
        port = port or self.default_port()
        try:
            ok = _kill_port_win(port) if sys.platform == "win32" else _kill_port_unix(port)
            return {"success": ok,
                    "output": f"Killed process on :{port}" if ok else "No process found"}
        except Exception as e:
            return {"success": False, "output": str(e)}

    # ── launcher script ──

    def launcher_source(self, repo_root: str, port: int) -> str:
        """Self-contained Python source for ``~/.c3/<LAUNCHER_NAME>``."""
        log_rel = ", ".join(repr(p) for p in self.LOG_REL)
        launch = textwrap.indent(textwrap.dedent(self.launch_code(port)).strip(), "    ")
        extra = textwrap.dedent(self.launcher_extra(port)).strip()
        parts = [
            "import sys, os, time\n"
            "from pathlib import Path\n"
            "\n"
            f"_REPO = {repo_root!r}\n"
            f"_PORT = {int(port)}\n"
            f"_LOG  = Path.home().joinpath({log_rel})\n"
            f"_TAG  = {self.LOG_TAG!r}\n"
            "\n"
            "# Redirect all output to the log BEFORE importing the server. Under\n"
            "# pythonw.exe sys.stdout/sys.stderr are None, and anything that touches\n"
            "# them (uvicorn's log formatter calls sys.stdout.isatty()) would crash.\n"
            "_LOG.parent.mkdir(parents=True, exist_ok=True)\n"
            "_fh = open(str(_LOG), 'a', encoding='utf-8', buffering=1)\n"
            "sys.stdout = _fh\n"
            "sys.stderr = _fh\n"
            "\n"
            "def _log(msg):\n"
            "    import datetime\n"
            "    print(f'[{_TAG}] {datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}', flush=True)\n"
            "\n"
            "# Wait up to 60 s for the repo to be accessible (network-drive mounts)\n"
            "for _i in range(12):\n"
            "    if Path(_REPO).exists():\n"
            "        break\n"
            "    time.sleep(5)\n"
            "else:\n"
            "    _log(f'repo not accessible after 60 s: {_REPO}')\n"
            "    sys.exit(1)\n"
            "\n"
            "sys.path.insert(0, _REPO)\n"
            "os.chdir(_REPO)\n"
        ]
        if extra:
            parts.append("\n" + extra + "\n")
        parts.append(
            "\n"
            "try:\n"
            f"{launch}\n"
            "except Exception as _e:\n"
            "    import traceback\n"
            "    _log(f'STARTUP ERROR: {_e}')\n"
            "    traceback.print_exc(file=_fh)\n"
            "    sys.exit(1)\n"
        )
        return "".join(parts)

    def write_launcher(self, port: int, repo_root: str | None = None) -> Path:
        """Write the launcher to the local drive (always reachable at logon)."""
        path = self.launcher_path
        path.parent.mkdir(parents=True, exist_ok=True)
        src = self.launcher_source(repo_root or str(_REPO_ROOT), port)
        compile(src, str(path), "exec")  # refuse to install a launcher that cannot parse
        path.write_text(src, encoding="utf-8")
        return path

    def launch_background(self, port: int) -> None:
        """Start the server as a detached, windowless background process."""
        script = self.write_launcher(port)
        self.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        exe = _pythonw() if sys.platform == "win32" else sys.executable
        kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
        if sys.platform == "win32":
            # Redirect ALL three std handles. On Windows, redirecting only one
            # makes CreateProcess pass the parent's current stdout/stderr to
            # the child — and when the parent is `c3 ... | tail`, the detached
            # server then holds the pipe open and the caller never returns.
            # The launcher re-points sys.stdout/stderr at its log anyway.
            kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
            log = open(self.LOG_FILE, "a", encoding="utf-8")
            kwargs["stdout"] = log
            kwargs["stderr"] = log
        subprocess.Popen([exe, str(script)], **kwargs)

    # ── Windows ──

    def _win_task_registered(self) -> bool:
        try:
            r = subprocess.run(
                ["schtasks", "/query", "/tn", self.TASK_NAME, "/fo", "LIST"],
                capture_output=True, text=True, timeout=20, **_quiet_kwargs(),
            )
            return r.returncode == 0
        except Exception:
            return False

    def _win_status(self) -> dict:
        port = self.default_port()
        task_installed = self._win_task_registered()
        reg_installed = _win_reg_registered(self.TASK_NAME)
        startup_installed = self._startup_script_path.exists()
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
            "running": self.is_running(port),
            "port": port,
            "platform": "windows",
            "method": method,
        }

    def task_xml(self, pythonw: str, start_script: Path) -> str:
        def _xe(s: str) -> str:
            return (s.replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;").replace('"', "&quot;"))
        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            '  <RegistrationInfo>\n'
            f'    <Description>{_xe(self.DESCRIPTION)}</Description>\n'
            '  </RegistrationInfo>\n'
            '  <Triggers>\n'
            '    <LogonTrigger>\n'
            '      <Enabled>true</Enabled>\n'
            f'      <Delay>{self.LOGON_DELAY}</Delay>\n'
            '    </LogonTrigger>\n'
            '  </Triggers>\n'
            '  <Principals>\n'
            '    <Principal id="Author">\n'
            '      <LogonType>InteractiveToken</LogonType>\n'
            '      <RunLevel>LeastPrivilege</RunLevel>\n'
            '    </Principal>\n'
            '  </Principals>\n'
            '  <Settings>\n'
            '    <RestartOnFailure>\n'
            '      <Interval>PT1M</Interval>\n'
            '      <Count>3</Count>\n'
            '    </RestartOnFailure>\n'
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

    def _win_reg_install(self, pythonw: str, start_script: Path) -> bool:
        if not winreg:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.TASK_NAME, 0, winreg.REG_SZ,
                                  f'"{pythonw}" "{start_script}"')
                return True
        except OSError:
            return False

    def _win_reg_uninstall(self) -> bool:
        if not winreg:
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.TASK_NAME)
                return True
        except OSError:
            return False

    def _win_install(self, port: int) -> dict:
        r"""Register for auto-start on Windows.

        Task Scheduler first (logon delay, restart-on-failure); falls back to
        the HKCU\...\Run key when schtasks is refused (e.g. Access Denied).
        """
        pythonw = _pythonw()
        start_script = self.write_launcher(port)
        tmp_xml = None
        messages = []
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", delete=False, encoding="utf-16",
                prefix=f"{self.KEY}_task_",
            ) as f:
                f.write(self.task_xml(pythonw, start_script))
                tmp_xml = f.name

            r = subprocess.run(
                ["schtasks", "/create", "/tn", self.TASK_NAME, "/xml", tmp_xml, "/f"],
                capture_output=True, text=True, timeout=30, **_quiet_kwargs(),
            )
            if r.returncode == 0:
                messages.append(f"Task '{self.TASK_NAME}' registered in Task Scheduler.")
                self._win_reg_uninstall()  # drop a stale Run-key entry
            elif self._win_reg_install(pythonw, start_script):
                messages.append("Task Scheduler refused; registered in the Registry Run key instead.")
            else:
                out = (r.stdout + r.stderr).strip()
                return {"success": False,
                        "output": out or "Failed to register via Task Scheduler or Registry."}

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
                except OSError:
                    pass

    def _win_uninstall(self) -> dict:
        messages = []
        success = True
        if self._win_task_registered():
            try:
                r = subprocess.run(
                    ["schtasks", "/delete", "/tn", self.TASK_NAME, "/f"],
                    capture_output=True, text=True, timeout=30, **_quiet_kwargs(),
                )
                if r.returncode == 0:
                    messages.append("Task Scheduler task removed.")
                else:
                    success = False
                    messages.append((r.stdout + r.stderr).strip() or "Failed to remove Task Scheduler task.")
            except Exception as e:
                success = False
                messages.append(str(e))
        if _win_reg_registered(self.TASK_NAME):
            if self._win_reg_uninstall():
                messages.append("Registry Run key removed.")
            else:
                success = False
                messages.append("Failed to remove Registry Run key.")
        if self.launcher_path.exists():
            self.launcher_path.unlink()
            messages.append("Launcher script removed.")
        if self._startup_script_path.exists():
            self._startup_script_path.unlink()
            messages.append("Legacy startup-folder script removed.")
        return {"success": success,
                "output": "\n".join(messages) or "No startup registration found."}

    # ── macOS ──

    @property
    def _plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.PLIST_LABEL}.plist"

    def _mac_status(self) -> dict:
        installed = self._plist_path.exists()
        running = None
        if installed:
            r = subprocess.run(["launchctl", "list", self.PLIST_LABEL],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
            running = r.returncode == 0
        running = bool(running) or self.is_running(self.default_port())
        return {"installed": installed, "running": running, "platform": "macos",
                "method": "launchd LaunchAgent (RunAtLoad)"}

    def _mac_install(self, port: int) -> dict:
        args = "".join(f"    <string>{a}</string>\n" for a in
                       [sys.executable, str(_C3_PY), *self.cli_args(port)])
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"\n'
            '  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'  <key>Label</key><string>{self.PLIST_LABEL}</string>\n'
            '  <key>ProgramArguments</key>\n'
            '  <array>\n'
            f'{args}'
            '  </array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><true/>\n'
            f'  <key>StandardOutPath</key><string>{self.LOG_FILE}</string>\n'
            f'  <key>StandardErrorPath</key><string>{self.LOG_FILE}</string>\n'
            '</dict></plist>\n'
        )
        self._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self._plist_path.write_text(plist, encoding="utf-8")
        r = subprocess.run(["launchctl", "load", str(self._plist_path)],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return {"success": r.returncode == 0,
                "output": (r.stdout + r.stderr).strip() or "LaunchAgent loaded."}

    def _mac_uninstall(self) -> dict:
        if self._plist_path.exists():
            subprocess.run(["launchctl", "unload", str(self._plist_path)],
                           capture_output=True, stdin=subprocess.DEVNULL)
            self._plist_path.unlink()
        return {"success": True, "output": "LaunchAgent removed."}

    # ── Linux (systemd user) ──

    @property
    def _service_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / self.SYSTEMD_NAME

    def _linux_status(self) -> dict:
        installed = self._service_path.exists()
        running = None
        if installed:
            r = subprocess.run(["systemctl", "--user", "is-active", self.SYSTEMD_NAME],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
            running = r.stdout.strip() == "active"
        running = bool(running) or self.is_running(self.default_port())
        return {"installed": installed, "running": running, "platform": "linux",
                "method": "systemd user service (loginctl linger recommended)"}

    def _linux_install(self, port: int) -> dict:
        exec_start = " ".join([sys.executable, str(_C3_PY), *self.cli_args(port)])
        unit = (
            "[Unit]\n"
            f"Description={self.DESCRIPTION}\n"
            "After=network.target\n\n"
            "[Service]\n"
            f"ExecStart={exec_start}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            f"StandardOutput=append:{self.LOG_FILE}\n"
            f"StandardError=append:{self.LOG_FILE}\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
        self._service_path.parent.mkdir(parents=True, exist_ok=True)
        self._service_path.write_text(unit, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, stdin=subprocess.DEVNULL)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", self.SYSTEMD_NAME],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return {"success": r.returncode == 0,
                "output": (r.stdout + r.stderr).strip() or "Service enabled and started."}

    def _linux_uninstall(self) -> dict:
        subprocess.run(["systemctl", "--user", "disable", "--now", self.SYSTEMD_NAME],
                       capture_output=True, stdin=subprocess.DEVNULL)
        if self._service_path.exists():
            self._service_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, stdin=subprocess.DEVNULL)
        return {"success": True, "output": "systemd user service removed."}
