#!/usr/bin/env python3
"""
C3 Project Hub — global project & session manager web server.

Features:
- Dedicated configurable port (stored in ~/.c3/hub_config.json)
- Single-instance detection: if already running on configured port, opens browser
- Project CRUD + session management
- Init and MCP install runners

Launch with:  c3 hub [--port 3330] [--no-browser]
"""
import json
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ide import PROFILES, detect_ide, get_profile, load_ide_config, normalize_ide_name
from services.activity_log import ActivityLog
from services.project_manager import ProjectManager
from services.tool_classifier import CATEGORIES

app = Flask(__name__, static_folder=str(Path(__file__).parent))

# Localhost-only security: Host-header allowlist + Origin/Referer CSRF guard +
# scoped CORS. The hub manages MANY projects and exposes command-executing
# endpoints (launch-ide, mcp-server-add, permissions), so cross-origin CSRF /
# DNS-rebinding protection matters even though it binds loopback by default.
# Reads bind host + optional allowed_hosts per-request from hub_config.json.
from core.web_security import (
    allowed_hostnames as _allowed_hostnames,
)
from core.web_security import (
    install_guard as _install_web_guard,
)


def _hub_allowed_hosts():
    _c = _read_hub_config()
    return _allowed_hostnames(_c.get("host"), _c.get("allowed_hosts"))


_install_web_guard(app, _hub_allowed_hosts)

# ─── Hub config ───────────────────────────────────────────────────────────────

_GLOBAL_C3_DIR = Path.home() / ".c3"
_HUB_CONFIG_FILE = _GLOBAL_C3_DIR / "hub_config.json"

_HUB_CONFIG_DEFAULTS = {
    "port": 3330,
    "host": "127.0.0.1",  # loopback only by default; opt-in to 0.0.0.0 for LAN
    "auto_open_browser": True,
    "theme": "dark",
    "projects_view": "list",
    "oracle_url": "",
}


def _read_hub_config() -> dict:
    cfg = dict(_HUB_CONFIG_DEFAULTS)
    try:
        if _HUB_CONFIG_FILE.exists():
            with open(_HUB_CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def _write_hub_config(cfg: dict):
    _GLOBAL_C3_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(_HUB_CONFIG_DEFAULTS)
    merged.update(cfg)
    with open(_HUB_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)


# ─── C3 version ───────────────────────────────────────────────────────────────

_C3_PY = Path(__file__).parent / "c3.py"
_ver_pat = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')
try:
    C3_VERSION = _ver_pat.search(_C3_PY.read_text(encoding="utf-8-sig")).group(1)
except Exception:
    C3_VERSION = "unknown"

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _pm() -> ProjectManager:
    return ProjectManager()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _find_free_port(start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        if _port_free(port):
            return port
    raise RuntimeError(f"No free port found near {start}")


def _is_hub_running(port: int) -> bool:
    """Return True if a C3 hub is already listening on this port."""
    try:
        url = f"http://127.0.0.1:{port}/api/health"
        with urllib.request.urlopen(url, timeout=1) as r:
            data = json.loads(r.read())
            return data.get("service") == "c3-hub"
    except Exception:
        return False


def _run_c3(args: list, cwd: str = None, timeout: int = 90) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parent.parent)
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    cmd = [sys.executable, str(_C3_PY)] + args
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace",
            cwd=cwd or str(Path(__file__).parent.parent),
            timeout=timeout,
            **kwargs
        )
        output = (result.stdout or "") + (result.stderr or "")
        return {"success": result.returncode == 0, "output": output.strip(), "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"success": False, "output": "Command timed out.", "returncode": -1}
    except Exception as e:
        return {"success": False, "output": str(e), "returncode": -1}


def _resolve_project_path(path: str) -> Path:
    resolved = Path(path or ".").resolve()
    if not resolved.exists():
        raise ValueError(f"Project path not found: {resolved}")
    return resolved


def _resolve_project_ide_profile(project_path: str, ide_name: str | None):
    project_root = _resolve_project_path(project_path)
    requested = (ide_name or "").strip().lower()
    if requested and requested != "auto":
        requested = normalize_ide_name(requested)
        if requested not in PROFILES:
            raise ValueError(f"Unknown IDE profile: {requested}")
        return project_root, requested, get_profile(requested)

    configured_ide = load_ide_config(str(project_root))
    detected_ide = detect_ide(str(project_root))
    active_ide = configured_ide or detected_ide or "claude-code"
    return project_root, active_ide, get_profile(active_ide)


def _project_mcp_config_path(project_root: Path, profile) -> Path:
    return (Path.home() / profile.config_path) if profile.config_path_global else (project_root / profile.config_path)


from core.mcp_toml import (
    parse_toml_mcp_servers as _parse_toml_mcp_servers,
)
from core.mcp_toml import (
    upsert_toml_section as _upsert_toml_section,
)


def _read_project_mcp_servers_for_profile(profile, mcp_file: Path) -> tuple[dict, dict]:
    if not mcp_file.exists():
        return {}, {}

    if profile.config_format == "toml":
        content = mcp_file.read_text(encoding="utf-8")
        return _parse_toml_mcp_servers(content), {}

    with open(mcp_file, encoding="utf-8") as f:
        raw_config = json.load(f)
    servers = raw_config.get(profile.config_key, {})
    if not isinstance(servers, dict):
        servers = {}
    return servers, raw_config


def _build_mcp_cli_capabilities() -> dict:
    return {
        "commands": [
            {
                "name": "install-mcp",
                "usage": "c3 install-mcp [project_path] [ide] --ide <ide> --mcp-mode <direct|proxy>",
                "summary": "Install or update the C3 MCP entrypoint for the target IDE profile.",
                "options": ["project_path", "ide", "--ide", "--mcp-mode"],
            },
            {
                "name": "mcp-install",
                "usage": "c3 mcp-install [project_path] [ide] --ide <ide> --mcp-mode <direct|proxy>",
                "summary": "Alias for install-mcp.",
                "options": ["project_path", "ide", "--ide", "--mcp-mode"],
            },
            {
                "name": "mcp-remove",
                "usage": "c3 mcp-remove <name> [project_path] --ide <ide>",
                "summary": "Remove a named MCP server from the target IDE configuration.",
                "options": ["name", "project_path", "--ide"],
            },
        ],
        "modes": ["direct", "proxy"],
        "ides": [
            {"value": value, "label": profile.display_name}
            for value, profile in PROFILES.items()
            if value != "antigravity"
        ],
        "tool_categories": [
            {
                "name": name,
                "priority": info.get("priority", 99),
                "tools": info.get("tools", []),
            }
            for name, info in sorted(CATEGORIES.items(), key=lambda item: item[1].get("priority", 99))
        ],
    }


def _serialize_mcp_servers(profile, servers: dict) -> list[dict]:
    items = []
    for name, conf in (servers or {}).items():
        if not isinstance(conf, dict):
            continue
        items.append({
            "name": name,
            "command": conf.get("command", ""),
            "args": conf.get("args", []),
            "type": conf.get("type", ""),
            "env_keys": list((conf.get("env") or {}).keys()),
            "enabled": conf.get("enabled", True),
        })
    return items


def _detail_mode_from_servers(servers: dict, fallback: str) -> str:
    c3_entry = (servers or {}).get("c3", {})
    args = c3_entry.get("args", []) if isinstance(c3_entry, dict) else []
    for arg in args:
        if isinstance(arg, str) and arg.endswith("mcp_proxy.py"):
            return "proxy"
        if isinstance(arg, str) and arg.endswith("mcp_server.py"):
            return "direct"
    return fallback


def _win_find_ide(cmd: str) -> str:
    """Find the full path of VS Code or Cursor on Windows if not in PATH."""
    if sys.platform != "win32":
        return cmd

    # 1. Try PATH
    p = shutil.which(cmd)
    if p:
        return p

    # 2. Try common installation paths
    user_appdata = os.environ.get("LocalAppData", "")
    prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")

    search_paths = []
    if cmd == "code":
        search_paths = [
            Path(user_appdata) / "Programs" / "Microsoft VS Code" / "bin" / "code.cmd",
            Path(prog_files) / "Microsoft VS Code" / "bin" / "code.cmd",
        ]
    elif cmd == "cursor":
        search_paths = [
            Path(user_appdata) / "Programs" / "Cursor" / "bin" / "cursor.cmd",
            Path(user_appdata) / "Programs" / "cursor" / "resources" / "app" / "bin" / "cursor.cmd",
        ]
    elif cmd == "claude-app":
        search_paths = [
            Path(user_appdata) / "Programs" / "Claude" / "Claude.exe",
            Path(user_appdata) / "Programs" / "claude-code" / "Claude Code.exe",
            Path(prog_files) / "Claude" / "Claude.exe",
        ]

    for p in search_paths:
        if p.exists():
            return str(p)

    return cmd


# ─── Routes: static ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(str(Path(__file__).parent), "hub.html")


@app.route("/guide/")
@app.route("/guide/<path:filename>")
def serve_guide(filename="index.html"):
    """Serve the bundled in-app guide from the installed package (cli/guide/*)."""
    guide_dir = Path(__file__).parent / "guide"
    if not (guide_dir / filename).is_file():
        return "<h1>C3 guide not found.</h1>", 404
    return send_from_directory(str(guide_dir), filename)


# ─── Routes: health & version ────────────────────────────────────────────────

@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "service": "c3-hub", "c3_version": C3_VERSION})


@app.route("/api/version")
def api_version():
    return jsonify({"c3_version": C3_VERSION})


# ─── Routes: hub config ──────────────────────────────────────────────────────

@app.route("/api/hub/config", methods=["GET"])
def api_hub_config_get():
    cfg = _read_hub_config()
    cfg["has_terminal"] = sys.stdin is not None and sys.stdin.isatty()
    return jsonify(cfg)


@app.route("/api/hub/config", methods=["POST"])
def api_hub_config_set():
    data = request.get_json(force=True) or {}
    cfg = _read_hub_config()
    if "port" in data:
        try:
            cfg["port"] = int(data["port"])
        except (ValueError, TypeError):
            return jsonify({"error": "port must be an integer"}), 400
    if "auto_open_browser" in data:
        cfg["auto_open_browser"] = bool(data["auto_open_browser"])
    if "theme" in data:
        theme = str(data["theme"]).strip().lower()
        if theme not in {"dark", "light"}:
            return jsonify({"error": "theme must be 'dark' or 'light'"}), 400
        cfg["theme"] = theme
    if "projects_view" in data:
        projects_view = str(data["projects_view"]).strip().lower()
        if projects_view not in {"list", "grid"}:
            return jsonify({"error": "projects_view must be 'list' or 'grid'"}), 400
        cfg["projects_view"] = projects_view
    if "oracle_url" in data:
        cfg["oracle_url"] = str(data["oracle_url"]).strip()
    _write_hub_config(cfg)
    return jsonify({"saved": True, "config": cfg})


# ─── Routes: projects ────────────────────────────────────────────────────────

def _notification_count(project_path: str) -> int:
    """Count unacknowledged notifications for a project by reading its .c3/notifications.jsonl."""
    nf = Path(project_path) / ".c3" / "notifications.jsonl"
    if not nf.exists():
        return 0
    count = 0
    try:
        for line in nf.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if not entry.get("acknowledged"):
                    count += 1
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return count


@app.route("/api/projects", methods=["GET"])
def api_projects_list():
    try:
        projects = _pm().list_projects()
        for p in projects:
            p["notification_count"] = _notification_count(p.get("path", ""))
        return jsonify(projects)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects", methods=["POST"])
def api_projects_add():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    name = (data.get("name") or "").strip() or None
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        result = _pm().add_project(path, name)
        resolved = str(Path(path).resolve())
        result["c3_initialized"] = (Path(resolved) / ".c3").exists()
        return jsonify(result), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/remove", methods=["POST"])
def api_projects_remove():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        return jsonify({"removed": _pm().remove_project(path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/open", methods=["POST"])
def api_projects_open():
    """Open project directory in OS file explorer. Body: {path}"""
    try:
        data = request.get_json(force=True) or {}
        path_str = (data.get("path") or "").strip()
        if not path_str:
            return jsonify({"error": "path is required"}), 400

        path = Path(path_str).resolve()
        if not path.exists():
            return jsonify({"error": f"Path does not exist: {path_str}"}), 404
        # Only ever open directories. Opening a *file* via os.startfile would
        # invoke its default handler (e.g. run an .exe/.bat/.lnk), so refuse
        # anything that is not a folder.
        if not path.is_dir():
            return jsonify({"error": "Only directories can be opened"}), 400

        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
        return jsonify({"opened": True})
    except Exception as e:
        return jsonify({"error": f"Failed to open folder: {str(e)}"}), 500


@app.route("/api/projects/launch-ide", methods=["POST"])
def api_launch_ide():
    """Launch an IDE or CLI tool in the project directory. Body: {path, ide, custom_cmd?}"""
    _IDE_CMDS = {
        "claude-code":  ("claude",      False),
        "claude-app":   ("claude-app",  True),
        "codex":        ("codex",       False),
        "gemini":       ("gemini",      False),
        "antigravity":  ("antigravity", False),
        "vscode":       ("code",        True),
        "cursor":       ("cursor",      True),
    }
    try:
        data = request.get_json(force=True) or {}
        path_str   = (data.get("path")       or "").strip()
        ide        = (data.get("ide")        or "").strip()
        custom_cmd = (data.get("custom_cmd") or "").strip()

        if not path_str:
            return jsonify({"error": "path is required"}), 400
        if not ide:
            return jsonify({"error": "ide is required"}), 400

        path = Path(path_str).resolve()
        if not path.exists():
            return jsonify({"error": f"Path does not exist: {path_str}"}), 404

        if ide == "custom":
            if not custom_cmd:
                return jsonify({"error": "custom_cmd is required for custom IDE"}), 400
            cmd, is_gui = custom_cmd, False
        elif ide in _IDE_CMDS:
            cmd, is_gui = _IDE_CMDS[ide]
        else:
            return jsonify({"error": f"Unknown IDE: {ide}"}), 400

        if is_gui:
            # GUI IDEs (VS Code, Cursor) accept a path argument directly
            if sys.platform == "win32":
                if cmd == "claude-app":
                    # Windows Store app — find package family name dynamically and launch via explorer
                    try:
                        pfn = subprocess.check_output(
                            ["powershell", "-NoProfile", "-Command",
                             "(Get-AppxPackage | Where-Object { $_.Name -like '*claude*' } | Select-Object -First 1).PackageFamilyName"],
                            text=True, timeout=8
                        ).strip()
                    except Exception:
                        pfn = ""
                    if not pfn:
                        return jsonify({"error": "Claude app not found. Install it from the Microsoft Store."}), 404
                    subprocess.Popen(
                        ["explorer.exe", f"shell:AppsFolder\\{pfn}!App"],
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                else:
                    # Find full path if not in PATH
                    full_cmd = _win_find_ide(cmd)
                    # Launch exe directly — avoids cmd.exe splitting paths with spaces
                    subprocess.Popen(
                        [full_cmd, str(path)],
                        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
            else:
                kwargs = {"start_new_session": True}
                subprocess.Popen([cmd, str(path)], **kwargs)
        else:
            # Terminal CLIs: open a new terminal window running the command
            if sys.platform == "win32":
                # On Windows, use the command directly (globally installed CLIs)
                win_cmd = cmd

                # Try Windows Terminal first, fall back to cmd
                try:
                    # Windows Terminal 'wt' needs a full command to run
                    # We wrap the command in 'cmd /k' so the terminal stays open
                    subprocess.Popen(
                        ["wt", "-d", str(path), "cmd", "/k", win_cmd],
                        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                except FileNotFoundError:
                    # Fallback to classic cmd.exe
                    subprocess.Popen(
                        ["cmd", "/c", "start", "", "cmd", "/k", win_cmd],
                        cwd=str(path),
                        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
            elif sys.platform == "darwin":
                script = (
                    f'tell application "Terminal" to do script '
                    f'"cd {shlex.quote(str(path))} && {cmd}"'
                )
                subprocess.Popen(["osascript", "-e", script])
            else:
                q = shlex.quote(str(path))
                for term_args in [
                    ["gnome-terminal", "--", "bash", "-c", f"cd {q} && {cmd}; exec bash"],
                    ["xterm", "-e", f"bash -c 'cd {q} && {cmd}; exec bash'"],
                    ["konsole", "-e", "bash", "-c", f"cd {q} && {cmd}; exec bash"],
                    ["xfce4-terminal", "--command", f"bash -c 'cd {q} && {cmd}; exec bash'"],
                ]:
                    try:
                        subprocess.Popen(term_args, start_new_session=True)
                        break
                    except FileNotFoundError:
                        continue

        return jsonify({"launched": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/update", methods=["POST"])
def api_projects_update():
    """Update editable project fields (name, tags, notes). Body: {path, name?, tags?, notes?}"""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    fields = {}
    if "name" in data:
        fields["name"] = str(data["name"]).strip()
    if "tags" in data:
        # Accept list or comma-separated string
        raw = data["tags"]
        if isinstance(raw, list):
            fields["tags"] = [t.strip() for t in raw if str(t).strip()]
        else:
            fields["tags"] = [t.strip() for t in str(raw).split(",") if t.strip()]
    if "notes" in data:
        fields["notes"] = str(data["notes"])
    try:
        ok = _pm().update_project(path, **fields)
        return jsonify({"updated": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/rename", methods=["POST"])
def api_projects_rename():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    name = (data.get("name") or "").strip()
    if not path or not name:
        return jsonify({"error": "path and name are required"}), 400
    try:
        return jsonify({"renamed": _pm().rename_project(path, name)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/transfer", methods=["POST"])
def api_projects_transfer():
    """Transfer project registration to a new path. Body: {old_path, new_path}"""
    data = request.get_json(force=True) or {}
    old_path = (data.get("old_path") or "").strip()
    new_path = (data.get("new_path") or "").strip()
    if not old_path or not new_path:
        return jsonify({"error": "old_path and new_path are required"}), 400
    try:
        result = _pm().transfer_project(old_path, new_path)
        if result.get("error"):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/merge", methods=["POST"])
def api_projects_merge():
    """Merge source project's memory/sessions/ledger into target.

    Body: {source_path, target_path, cleanup: 'keep'|'clear'}
    """
    data = request.get_json(force=True) or {}
    src = (data.get("source_path") or "").strip()
    tgt = (data.get("target_path") or "").strip()
    cleanup = (data.get("cleanup") or "keep").strip().lower()
    if not src or not tgt:
        return jsonify({"error": "source_path and target_path are required"}), 400
    if cleanup not in ("keep", "clear"):
        return jsonify({"error": "cleanup must be 'keep' or 'clear'"}), 400
    try:
        result = _pm().merge_projects(src, tgt, cleanup=cleanup)
        if result.get("error"):
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/details", methods=["POST"])
def api_projects_details():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    ide_override = (data.get("ide") or "").strip() or None
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        details = _pm().get_project_details(path)
        if ide_override:
            project_root, ide_name, profile = _resolve_project_ide_profile(path, ide_override)
            mcp_file = _project_mcp_config_path(project_root, profile)
            servers, _ = _read_project_mcp_servers_for_profile(profile, mcp_file)
            details["ide"] = ide_name
            details["mcp_installed"] = mcp_file.exists()
            details["mcp_config_path"] = str(mcp_file) if mcp_file.exists() else None
            details["mcp_servers"] = _serialize_mcp_servers(profile, servers)
            details["mcp_mode"] = _detail_mode_from_servers(servers, details.get("mcp_mode", "unknown"))
        details["hub_c3_version"] = C3_VERSION
        return jsonify(details)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/mcp-capabilities", methods=["GET"])
def api_projects_mcp_capabilities():
    return jsonify(_build_mcp_cli_capabilities())


@app.route("/api/projects/mcp-server-add", methods=["POST"])
def api_projects_mcp_server_add():
    try:
        data = request.get_json(force=True) or {}
        path = (data.get("path") or "").strip()
        name = (data.get("name") or "").strip()
        command = (data.get("command") or "").strip()
        ide = (data.get("ide") or "").strip() or None
        args = data.get("args") or []
        env = data.get("env") or {}
        enabled = bool(data.get("enabled", True))

        if not path or not name or not command:
            return jsonify({"error": "path, name, and command are required"}), 400
        if not isinstance(args, list):
            return jsonify({"error": "args must be an array"}), 400
        if not isinstance(env, dict):
            return jsonify({"error": "env must be an object"}), 400

        project_root, ide_name, profile = _resolve_project_ide_profile(path, ide)
        mcp_file = _project_mcp_config_path(project_root, profile)
        mcp_file.parent.mkdir(parents=True, exist_ok=True)

        if profile.config_format == "toml":
            entries = {"command": command, "args": args}
            if profile.name == "codex":
                entries["enabled"] = enabled
            _upsert_toml_section(mcp_file, f"{profile.config_key}.{name}", entries)
        else:
            servers, raw_config = _read_project_mcp_servers_for_profile(profile, mcp_file)
            server_config = {"command": command, "args": args}
            if env:
                server_config["env"] = env
            if profile.needs_type_field:
                server_config["type"] = "stdio"
            if profile.name == "codex":
                server_config["enabled"] = enabled

            servers[name] = server_config
            if not raw_config:
                raw_config = {}
            raw_config.setdefault(profile.config_key, {})
            raw_config[profile.config_key] = servers
            with open(mcp_file, "w", encoding="utf-8") as f:
                json.dump(raw_config, f, indent=2)
                f.write("\n")

        return jsonify({"success": True, "ide": ide_name, "config_path": str(mcp_file)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/activity", methods=["POST"])
def api_projects_activity():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    try:
        limit = int(data.get("limit", 120))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400

    limit = max(1, min(limit, 500))
    since = (data.get("since") or "").strip() or None
    event_type = (data.get("event_type") or "").strip() or None

    try:
        projects = _pm().list_projects()
        project = next((p for p in projects if p.get("path") == path), None)
        events = ActivityLog(path).get_recent(limit=limit, event_type=event_type, since=since)
        latest_ts = events[0]["timestamp"] if events else since
        return jsonify({
            "path": path,
            "project": project,
            "events": events,
            "latest_timestamp": latest_ts,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/notifications", methods=["POST"])
def api_project_notifications():
    """Get notifications for a project by reading its .c3/notifications.jsonl."""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    limit = min(int(data.get("limit", 50)), 200)
    nf = Path(path) / ".c3" / "notifications.jsonl"
    if not nf.exists():
        return jsonify({"notifications": [], "total": 0})
    try:
        entries = []
        for line in nf.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        unacked = [e for e in entries if not e.get("acknowledged")]
        unacked.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return jsonify({"notifications": unacked[:limit], "total": len(unacked)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/notifications/clear", methods=["POST"])
def api_project_notifications_clear():
    """Acknowledge all notifications for a project."""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    nf = Path(path) / ".c3" / "notifications.jsonl"
    if not nf.exists():
        return jsonify({"cleared": 0})
    try:
        entries = []
        for line in nf.read_text(encoding="utf-8").strip().splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        count = 0
        for e in entries:
            if not e.get("acknowledged"):
                e["acknowledged"] = True
                count += 1
        if count:
            nf.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
                encoding="utf-8",
            )
        return jsonify({"cleared": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Routes: run commands ────────────────────────────────────────────────────

@app.route("/api/projects/run-init", methods=["POST"])
def api_run_init():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    ide = (data.get("ide") or "").strip() or None
    mcp_mode = (data.get("mcp_mode") or "").strip() or None
    init_mode = (data.get("init_mode") or "force").strip().lower()
    git = bool(data.get("git"))
    if not path:
        return jsonify({"error": "path is required"}), 400
    if init_mode not in {"force", "clear"}:
        return jsonify({"error": "init_mode must be 'force' or 'clear'"}), 400
    args = ["init", path, f"--{init_mode}"]
    if ide:
        args += ["--ide", ide]
    if mcp_mode and init_mode == "force":
        args += ["--mcp-mode", mcp_mode]
    if git and init_mode == "force":
        args += ["--git"]
    return jsonify(_run_c3(args))


@app.route("/api/projects/health", methods=["POST"])
def api_project_health():
    """Return health-check data for a single project."""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = str(Path(path).resolve())
        from cli.c3 import _check_c3_health
        health = _check_c3_health(resolved)
        health["path"] = resolved
        return jsonify(health)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/run-component", methods=["POST"])
def api_run_component():
    """Run a specific init component: index, dictionary, instructions, config, mcp, embeddings, doc_index."""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    component = (data.get("component") or "").strip().lower()
    ide = (data.get("ide") or "").strip() or None
    mcp_mode = (data.get("mcp_mode") or "").strip() or None
    if not path:
        return jsonify({"error": "path is required"}), 400
    valid = {"index", "dictionary", "instructions", "config", "mcp", "embeddings", "doc_index"}
    if component not in valid:
        return jsonify({"error": f"component must be one of: {', '.join(sorted(valid))}"}), 400
    try:
        resolved = str(Path(path).resolve())
        c3_dir = Path(resolved) / ".c3"
        if not c3_dir.exists():
            return jsonify({"error": "Project not initialized (.c3 directory missing)"}), 400

        import contextlib
        import io
        buf = io.StringIO()

        if component == "index":
            from services.indexer import CodeIndex
            with contextlib.redirect_stdout(buf):
                indexer = CodeIndex(resolved)
                result = indexer.build_index()
            output = buf.getvalue() + f"\nIndexed {result['files_indexed']} files, {result['chunks_created']} chunks."
            return jsonify({"success": True, "output": output.strip()})

        elif component == "dictionary":
            from services.protocol import CompressionProtocol
            with contextlib.redirect_stdout(buf):
                protocol = CompressionProtocol(resolved)
                new_terms = protocol.build_project_dictionary()
            output = buf.getvalue() + f"\nAdded {len(new_terms)} project-specific terms."
            return jsonify({"success": True, "output": output.strip()})

        elif component == "instructions":
            from cli.c3 import _sync_project_instruction_docs
            from services.session_manager import SessionManager
            sm = SessionManager(resolved)
            with contextlib.redirect_stdout(buf):
                _sync_project_instruction_docs(resolved, sm)
            return jsonify({"success": True, "output": buf.getvalue().strip()})

        elif component == "config":
            from cli.c3 import _C3_INIT_SUBDIRS, _build_init_config, save_config
            config = _build_init_config(resolved)
            save_config(config, resolved)
            for subdir in _C3_INIT_SUBDIRS:
                (Path(resolved) / ".c3" / subdir).mkdir(parents=True, exist_ok=True)
            return jsonify({"success": True, "output": "Config refreshed and subdirectories ensured."})

        elif component == "embeddings":
            from core.config import load_hybrid_config
            from services.embedding_index import EmbeddingIndex
            from services.indexer import CodeIndex
            from services.ollama_client import OllamaClient
            cfg = load_hybrid_config(resolved)
            ollama_url = cfg.get("ollama_base_url", "http://localhost:11434")
            ollama = OllamaClient(ollama_url)
            embed_model = cfg.get("embed_model", "nomic-embed-text")
            ei = EmbeddingIndex(resolved, ollama, embed_model=embed_model)
            if not ei.ready:
                return jsonify({"success": True, "output": "Embedding index skipped (Ollama not available or model not pulled)."})
            indexer = CodeIndex(resolved)
            if not indexer.chunks:
                indexer._load_index()
            if not indexer.chunks:
                indexer.build_index()
            result = ei.build(indexer, force=True)
            output = (f"Embedded {result.get('chunks_embedded', 0)} chunks "
                      f"from {result.get('files_processed', 0)} files. "
                      f"Total: {result.get('total_embedded', 0)} chunks indexed.")
            return jsonify({"success": True, "output": output})

        elif component == "doc_index":
            from services.doc_index import DocIndex
            di = DocIndex(resolved)
            result = di.build(force=True)
            output = (f"Indexed {result['docs_indexed']} docs, "
                      f"{result['chunks_created']} chunks. "
                      f"(skipped {result.get('skipped', 0)} unchanged)")
            return jsonify({"success": True, "output": output})

        elif component == "mcp":
            args = ["install-mcp", resolved]
            if ide:
                args += ["--ide", ide]
            if mcp_mode:
                args += ["--mcp-mode", mcp_mode]
            return jsonify(_run_c3(args))

    except Exception as e:
        return jsonify({"error": str(e), "success": False}), 500


_batch_state = {
    "running": False,
    "cancelled": False,
    "results": [],
    "current": None,
    "current_index": 0,
    "total": 0,
    "done": False,
    "error": None,
}
_batch_lock = threading.Lock()


def _batch_worker(projects):
    """Run batch init in background thread, updating _batch_state."""
    global _batch_state
    for i, p in enumerate(projects):
        with _batch_lock:
            if _batch_state["cancelled"]:
                break
            _batch_state["current"] = p.get("name") or p["path"]
            _batch_state["current_index"] = i

        path = p["path"]
        args = ["init", path, "--force"]
        ide = p.get("ide")
        if ide and ide != "unknown":
            args += ["--ide", ide]

        res = _run_c3(args)
        result = {
            "path": path,
            "name": p.get("name"),
            "success": res.get("success"),
            "output": res.get("output"),
            "returncode": res.get("returncode"),
        }
        with _batch_lock:
            _batch_state["results"].append(result)

    with _batch_lock:
        _batch_state["running"] = False
        _batch_state["done"] = True
        _batch_state["current"] = None


@app.route("/api/projects/run-init/batch", methods=["POST"])
def api_run_init_batch():
    """Start batch update of specified projects. Runs in background thread."""
    global _batch_state
    with _batch_lock:
        if _batch_state["running"]:
            return jsonify({"error": "Batch update already in progress"}), 409
    try:
        data = request.get_json(force=True) or {}
        projects = data.get("projects") or _pm().list_projects()
        if not projects:
            return jsonify({"error": "No projects to update"}), 400
        with _batch_lock:
            _batch_state = {
                "running": True,
                "cancelled": False,
                "results": [],
                "current": None,
                "current_index": 0,
                "total": len(projects),
                "done": False,
                "error": None,
            }
        t = threading.Thread(target=_batch_worker, args=(projects,), daemon=True)
        t.start()
        return jsonify({"started": True, "total": len(projects)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/run-init/batch/status", methods=["GET"])
def api_batch_status():
    """Return current batch update state for polling."""
    with _batch_lock:
        return jsonify(dict(_batch_state))


@app.route("/api/projects/run-init/batch/cancel", methods=["POST"])
def api_batch_cancel():
    """Signal cancellation of running batch update."""
    with _batch_lock:
        if not _batch_state["running"]:
            return jsonify({"cancelled": False, "message": "No batch in progress"})
        _batch_state["cancelled"] = True
        return jsonify({"cancelled": True})


@app.route("/api/projects/run-mcp", methods=["POST"])
def api_run_mcp():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    ide = (data.get("ide") or "").strip() or None
    mcp_mode = (data.get("mcp_mode") or "").strip() or None
    if not path:
        return jsonify({"error": "path is required"}), 400
    args = ["install-mcp", path]
    if ide:
        args += ["--ide", ide]
    if mcp_mode:
        args += ["--mcp-mode", mcp_mode]
    return jsonify(_run_c3(args, cwd=path))


@app.route("/api/projects/run-mcp-remove", methods=["POST"])
def api_run_mcp_remove():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    name = (data.get("name") or "").strip()
    ide = (data.get("ide") or "").strip() or None
    if not path or not name:
        return jsonify({"error": "path and name are required"}), 400
    args = ["mcp-remove", name, path]
    if ide:
        args += ["--ide", ide]
    return jsonify(_run_c3(args, cwd=path))


# ─── Routes: project budget config ────────────────────────────────

@app.route("/api/projects/budget", methods=["POST"])
def api_projects_budget_get():
    """Get budget config for a project. Body: {path}"""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    config_file = Path(path) / ".c3" / "config.json"
    config = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    budget = config.get("context_budget", {})
    hybrid = config.get("hybrid", {})
    return jsonify({
        "threshold": budget.get("threshold", 35000),
        "show_context_nudges": hybrid.get("show_context_nudges", True),
    })


@app.route("/api/projects/budget", methods=["PUT"])
def api_projects_budget_put():
    """Update budget config for a project. Body: {path, ...settings}"""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    config_file = Path(path) / ".c3" / "config.json"
    config = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass

    if "threshold" in data:
        try:
            config.setdefault("context_budget", {})["threshold"] = max(1000, int(data["threshold"]))
        except (ValueError, TypeError):
            return jsonify({"error": "threshold must be an integer"}), 400
    for k in ("show_context_nudges",):
        if k in data:
            config.setdefault("hybrid", {})[k] = bool(data[k])

    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    budget = config.get("context_budget", {})
    hybrid = config.get("hybrid", {})
    return jsonify({
        "threshold": budget.get("threshold", 35000),
        "show_context_nudges": hybrid.get("show_context_nudges", True),
    })


# ─── Routes: project permissions ──────────────────────────────────────────────

@app.route("/api/projects/permissions", methods=["POST"])
def api_projects_permissions_get():
    """Get permission tier for a project. Body: {path}"""
    from cli.c3 import PERMISSION_TIERS, _build_permission_tier, _detect_current_tier
    from core.ide import load_ide_config
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    project = Path(path)
    ide = load_ide_config(str(project))
    settings_path = project / ".claude" / "settings.local.json"
    current = _detect_current_tier(settings_path)
    # Check stored tier
    config_file = project / ".c3" / "config.json"
    stored_tier = None
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                stored_tier = json.load(f).get("permission_tier")
        except Exception:
            pass
    allow_count = deny_count = 0
    if settings_path.exists():
        try:
            with open(settings_path, encoding="utf-8") as f:
                s = json.load(f)
            allow_count = len(s.get("permissions", {}).get("allow", []))
            deny_count = len(s.get("permissions", {}).get("deny", []))
        except Exception:
            pass
    return jsonify({
        "current_tier": current or stored_tier,
        "detected_tier": current,
        "stored_tier": stored_tier,
        "allow_count": allow_count,
        "deny_count": deny_count,
        "ide": ide,
        "supported": ide == "claude-code",
        "tiers": {name: {"description": desc, "allow_count": len(_build_permission_tier(name)["permissions"]["allow"]),
                         "deny_count": len(_build_permission_tier(name)["permissions"]["deny"])}
                  for name, desc in PERMISSION_TIERS.items()},
    })


@app.route("/api/projects/permissions/apply", methods=["POST"])
def api_projects_permissions_put():
    """Apply permission tier to a project. Body: {path, tier}"""
    from cli.c3 import PERMISSION_TIERS, _build_permission_tier
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    tier = (data.get("tier") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    if tier not in PERMISSION_TIERS:
        return jsonify({"error": f"Unknown tier: {tier}. Available: {', '.join(PERMISSION_TIERS)}"}), 400

    project = Path(path)
    tier_perms = _build_permission_tier(tier)
    settings_path = project / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing settings to preserve hooks etc.
    settings = {}
    if settings_path.exists():
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except Exception:
            pass
    settings["permissions"] = tier_perms["permissions"]
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    # Store tier in .c3/config.json
    config_file = project / ".c3" / "config.json"
    config = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    config["permission_tier"] = tier
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return jsonify({
        "current_tier": tier,
        "allow_count": len(tier_perms["permissions"]["allow"]),
        "deny_count": len(tier_perms["permissions"]["deny"]),
        "message": f"Applied '{tier}' permissions. Restart Claude Code to activate.",
    })


# ─── Routes: hub service (background daemon) ────────────────────────────────

@app.route("/api/hub/service", methods=["GET"])
def api_hub_service_status():
    """Return whether the hub is registered as a startup service."""
    from services.hub_service import HubService
    status = HubService().status()
    status["port"] = _read_hub_config().get("port", 3330)
    return jsonify(status)


@app.route("/api/hub/service/install", methods=["POST"])
def api_hub_service_install():
    """Install hub as a login/startup service using the configured port."""
    from services.hub_service import HubService
    port = _read_hub_config().get("port", 3330)
    result = HubService().install(port)
    return jsonify(result)


@app.route("/api/hub/service/uninstall", methods=["POST"])
def api_hub_service_uninstall():
    """Remove the startup service registration."""
    from services.hub_service import HubService
    return jsonify(HubService().uninstall())


@app.route("/api/hub/service/start", methods=["POST"])
def api_hub_service_start():
    """Start a background hub process (no terminal needed)."""
    from services.hub_service import HubService
    port = _read_hub_config().get("port", 3330)
    return jsonify(HubService().start(port))


@app.route("/api/hub/service/stop", methods=["POST"])
def api_hub_service_stop():
    """Stop the hub process on its configured port (kills current server)."""
    import threading
    port = _read_hub_config().get("port", 3330)
    from services.hub_service import HubService
    result = HubService().stop(port)
    # Shut down Flask after sending response
    def _exit():
        import time
        time.sleep(0.4)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
    return jsonify(result)


@app.route("/api/hub/restart", methods=["POST"])
def api_hub_restart():
    """Restart the hub server in-place."""
    import threading

    def _restart():
        import time
        time.sleep(0.3)  # let response flush
        port = _read_hub_config().get("port", 3330)
        # Spawn a detached intermediate that waits for this process to fully exit,
        # then uses _launch_background so the new hub inherits proper PYTHONPATH.
        parent_dir = str(Path(__file__).parent.parent)
        launcher = (
            f"import time, sys; "
            f"sys.path.insert(0, r'{parent_dir}'); "
            f"time.sleep(1.5); "
            f"from services.hub_service import _launch_background; "
            f"_launch_background({port})"
        )
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-c", launcher], **kwargs)
        os._exit(0)

    threading.Thread(target=_restart, daemon=True).start()
    return jsonify({"restarting": True})


# ─── Routes: sessions ────────────────────────────────────────────────────────

@app.route("/api/sessions/start", methods=["POST"])
def api_session_start():
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        result = _pm().launch_session(path)
        if not result["launched"] and result.get("error"):
            return jsonify({"error": result["error"]}), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/stop", methods=["POST"])
def api_session_stop():
    data = request.get_json(force=True) or {}
    port = data.get("port")
    if not port:
        return jsonify({"error": "port is required"}), 400
    try:
        return jsonify({"stopped": _pm().stop_session(int(port))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/restart", methods=["POST"])
def api_session_restart():
    """Stop the UI server on the given port, then start a fresh one for the project."""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    port = data.get("port")
    if not path:
        return jsonify({"error": "path is required"}), 400
    pm = _pm()
    if port:
        pm.stop_session(int(port))
    result = pm.launch_session(path)
    return jsonify(result)


@app.route("/api/sessions/autostart", methods=["POST"])
def api_session_autostart():
    """Toggle per-project UI autostart. Body: {path, enabled: bool}."""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    enabled = bool(data.get("enabled", False))
    if not path:
        return jsonify({"error": "path is required"}), 400
    ok = _pm().update_project(path, autostart_ui=enabled)
    if not ok:
        return jsonify({"error": "project not found"}), 404
    return jsonify({"success": True, "autostart_ui": enabled})


@app.route("/api/sessions/end", methods=["POST"])
def api_session_end():
    """End an MCP-only session (no UI port) by marking it saved in the activity log."""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        return jsonify({"stopped": _pm().end_mcp_session(path)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions", methods=["GET"])
def api_sessions():
    try:
        return jsonify(_pm().get_active_sessions())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Error handlers ──────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": f"Not found: {request.path}"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": f"Method not allowed: {request.method} {request.path}"}), 405


# ─── Hook migration ──────────────────────────────────────────────────────────

def _migrate_project_hooks():
    """Idempotently add new C3 hooks to all registered projects' Claude and Gemini settings.

    Runs at hub startup so that existing projects pick up hook changes
    without requiring a manual 'c3 mcp install' on each project.
    """
    cli_dir = Path(__file__).parent
    hook_c3read_cmd = (
        f"{shlex.quote(sys.executable)} "
        f"{shlex.quote(str(cli_dir / 'hook_c3read.py'))}"
    )
    new_hook = {
        "matcher": "mcp__c3__c3_read",
        "hooks": [{"type": "command", "command": hook_c3read_cmd}],
    }

    # (settings_path, hook_event) pairs to check per project
    _HOOK_TARGETS = [
        (".claude/settings.local.json", "PostToolUse"),
        (".gemini/settings.json", "AfterTool"),
    ]

    try:
        projects = _pm().list_projects()
    except Exception:
        return

    updated = 0
    for p in projects:
        path = p.get("path", "")
        if not path:
            continue
        for rel_settings, hook_event in _HOOK_TARGETS:
            settings_path = Path(path) / rel_settings
            if not settings_path.exists():
                continue
            try:
                with open(settings_path, encoding="utf-8") as f:
                    settings = json.load(f)
            except Exception:
                continue

            existing = settings.get("hooks", {}).get(hook_event, [])
            if any(h.get("matcher") == "mcp__c3__c3_read" for h in existing):
                continue  # already present — skip

            existing.append(new_hook)
            settings.setdefault("hooks", {})[hook_event] = existing
            try:
                with open(settings_path, "w", encoding="utf-8") as f:
                    json.dump(settings, f, indent=2)
                updated += 1
            except Exception:
                pass

    if updated:
        logging.getLogger(__name__).info(
            "[c3] Migrated hook_c3read to %d project settings file(s)", updated
        )


# ─── Entry point ─────────────────────────────────────────────────────────────

def run_hub(
    port: int = None,
    open_browser: bool = None,
    silent: bool = False,
    quiet: bool = False,
):
    cfg = _read_hub_config()
    dedicated_port = port if port is not None else cfg.get("port", 3330)
    if open_browser is None:
        open_browser = cfg.get("auto_open_browser", True)

    # Single-instance check: if dedicated port is already our hub, just open it
    if not _port_free(dedicated_port):
        if _is_hub_running(dedicated_port):
            url = f"http://localhost:{dedicated_port}"
            if not quiet:
                print(f"C3 Hub already running at {url}")
            if open_browser:
                webbrowser.open(url)
            return
        # Port taken by something else → find next available
        actual_port = _find_free_port(dedicated_port + 1)
        if not quiet:
            print(f"Warning: dedicated port {dedicated_port} is in use. Using {actual_port} instead.")
    else:
        actual_port = dedicated_port

    logging.getLogger("werkzeug").setLevel(logging.ERROR if silent else logging.WARNING)
    url = f"http://localhost:{actual_port}"
    if not quiet:
        print(f"C3 Project Hub  →  {url}  (C3 v{C3_VERSION})")

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # Clean stale registry entries from before restart
    try:
        _pm().sweep_registry()
    except Exception:
        pass
    _migrate_project_hooks()

    # Auto-launch UI servers for projects with autostart_ui=True
    def _autostart_ui_servers():
        import time
        time.sleep(2)  # Give Flask a moment to bind
        try:
            pm = _pm()
            for proj_path in pm.get_autostart_projects():
                try:
                    pm.launch_session(proj_path)
                except Exception:
                    pass
        except Exception:
            pass

    threading.Thread(target=_autostart_ui_servers, daemon=True).start()

    bind_host = str(cfg.get("host", "127.0.0.1") or "127.0.0.1").strip()
    if bind_host not in ("127.0.0.1", "localhost", "::1") and not quiet:
        print(
            f"WARNING: C3 Hub is binding to {bind_host}. The hub has no built-in "
            "authentication; do not expose it to untrusted networks. Set "
            '"host": "127.0.0.1" in ~/.c3/hub_config.json to restrict to loopback.'
        )

    app.run(host=bind_host, port=actual_port, debug=False, use_reloader=False)


def main() -> None:
    """Entry-point for the ``c3-hub`` console script."""
    from services import error_reporting
    error_reporting.init(component="c3-hub", version=C3_VERSION)
    run_hub()


if __name__ == "__main__":
    main()
