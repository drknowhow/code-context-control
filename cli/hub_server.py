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
import time
import urllib.request
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.ide import PROFILES, detect_ide, get_profile, load_ide_config, normalize_ide_name
from services import access_guard
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
    "main_view": "projects",
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

# JS load order for the concatenated hub UI build (mirrors cli/server.py).
# ui/* files are SHARED with the per-project UI — reference, don't copy.
_HUB_JS_FILES = [
    "ui/theme.js",
    "ui/icons.js",
    "ui/api.js",
    "ui/shared.js",
    "ui/pm_shared.js",
    "hub_ui/state.js",
    "hub_ui/components/toasts.js",
    "hub_ui/components/topbar.js",
    "hub_ui/components/sidebar.js",
    "hub_ui/components/add_project.js",
    "hub_ui/components/summary_bar.js",
    "hub_ui/components/project_card.js",
    "hub_ui/components/project_tree.js",
    "hub_ui/components/task_board.js",
    "hub_ui/components/session_drawer.js",
    "hub_ui/components/drill_panel.js",
    "hub_ui/components/drill_views.js",
    "hub_ui/components/hub_credentials.js",
    "hub_ui/components/hub_locks.js",
    "hub_ui/components/hub_enforcement.js",
    "hub_ui/components/hub_ci.js",
    "hub_ui/components/drill_subprojects.js",
    "hub_ui/components/drill_health.js",
    "hub_ui/components/drill_tasks.js",
    "hub_ui/components/drill_artifacts.js",
    "hub_ui/components/config_editor.js",
    "hub_ui/components/mcp_manager.js",
    "hub_ui/components/global_search.js",
    "hub_ui/components/modals.js",
    "hub_ui/components/settings_modal.js",
    "hub_ui/app.js",
]


def _build_hub_html() -> str:
    """Concatenate hub_ui.html shell + all JS component files into one response."""
    cli_dir = Path(__file__).parent
    shell_path = cli_dir / "hub_ui.html"
    if not shell_path.exists():
        return "<h1>C3 Hub UI not found.</h1>"

    shell = shell_path.read_text(encoding="utf-8")

    js_parts = []
    for rel in _HUB_JS_FILES:
        js_path = cli_dir / rel
        if js_path.exists():
            js_parts.append(f"    // ═══ {rel} ═══\n" + js_path.read_text(encoding="utf-8"))

    return shell.replace("/* __C3_HUB_SCRIPTS__ */", "\n\n".join(js_parts))


# Cache the built HTML (rebuilt on first request; cleared on server restart)
_hub_html_cache: str | None = None


@app.route("/")
def index():
    global _hub_html_cache
    if _hub_html_cache is None:
        _hub_html_cache = _build_hub_html()
    from flask import Response
    return Response(_hub_html_cache, mimetype="text/html")


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
    if "main_view" in data:
        main_view = str(data["main_view"]).strip().lower()
        if main_view not in {"projects", "board", "creds", "locks", "enforce"}:
            return jsonify({"error": "main_view must be 'projects', 'board', "
                                     "'creds', 'locks' or 'enforce'"}), 400
        cfg["main_view"] = main_view
    if "oracle_url" in data:
        cfg["oracle_url"] = str(data["oracle_url"]).strip()
    if "sidebar_group" in data:
        cfg["sidebar_group"] = str(data["sidebar_group"]).strip()
    if "sidebar_collapsed" in data:
        cfg["sidebar_collapsed"] = bool(data["sidebar_collapsed"])
    if "runtime_cache_size" in data:
        try:
            cfg["runtime_cache_size"] = max(1, int(data["runtime_cache_size"]))
        except (ValueError, TypeError):
            return jsonify({"error": "runtime_cache_size must be an integer"}), 400
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


_sub_links_cache = {"fp": None, "ts": 0.0, "data": None, "computing": False}
_sub_links_lock = threading.Lock()
_SUB_LINKS_TTL = 15.0


def _invalidate_subproject_links():
    """Drop the cached link-health snapshot (call after any hierarchy mutation)."""
    with _sub_links_lock:
        _sub_links_cache.update({"fp": None, "ts": 0.0, "data": None})


def _sub_federation_errors(sub):
    """Schema check for the hybrid.subprojects federation dict (config PUT)."""
    allowed = {"memory_rollup", "search_fanout", "max_children_per_query"}
    unknown = sorted(set(sub) - allowed)
    if unknown:
        return f"unknown keys {unknown}"
    for k in ("memory_rollup", "search_fanout"):
        if k in sub and not isinstance(sub[k], bool):
            return f"'{k}' must be a boolean"
    v = sub.get("max_children_per_query")
    if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 256):
        return "'max_children_per_query' must be an integer between 1 and 256"
    return ""


def _norm_realpath(s):
    """resolve()+normcase, matching services.subprojects._norm — plain
    normcase alone would flag symlinked/8.3/trailing-slash registry rows
    as false orphans that Reconcile then denies."""
    try:
        return os.path.normcase(str(Path(s).resolve()))
    except Exception:
        return os.path.normcase(str(s or ""))


def _compute_subproject_links(projects):
    """Statuses only — deliberately NOT SubprojectManager.list(), which
    also parses facts.json and notifications.jsonl per child; those counts
    were computed and thrown away on every poll recompute."""
    from services.subprojects import SubprojectManager, get_subprojects
    parents_out, children_out = {}, {}
    registry = None
    for parent in projects:
        if not (parent.get("is_parent") or parent.get("subproject_count")):
            continue
        try:
            mgr = SubprojectManager(parent.get("path", ""))
            entries = get_subprojects(mgr.parent_path)
            if registry is None:
                registry = mgr.pm._read_projects()
        except Exception:
            continue
        issues = 0
        config_paths = set()
        for entry in entries:
            try:
                key = _norm_realpath(mgr._abs_child(entry.get("rel_path", "")))
                status = mgr._entry_status(entry, registry)
            except Exception:
                continue
            config_paths.add(key)
            if status != "ok":
                issues += 1
            children_out[key] = status
        parent_key = _norm_realpath(parent.get("path", ""))
        for row in projects:
            rp = row.get("parent_path")
            if (rp and _norm_realpath(rp) == parent_key
                    and _norm_realpath(row.get("path", "")) not in config_paths):
                issues += 1
                children_out[_norm_realpath(row.get("path", ""))] = "orphan"
        if issues:
            parents_out[parent_key] = issues
    return {"parents": parents_out, "children": children_out}


def _annotate_subproject_links(projects):
    """Per-parent link-health rollup + per-child link_status for the cards.

    SubprojectManager.list() is config + filesystem + raw-registry reads
    only (no child runtimes, no port probing). Registry orphans (rows
    claiming a parent that has no matching config entry) count as issues
    too — same rule as reconcile. The snapshot is cached _SUB_LINKS_TTL
    seconds and computed single-flight, so overlapping UI polls never
    stack behind a slow disk (e.g. a disconnected network drive); the
    fingerprint ties the cache to the registry rows, and mutating
    sub-project endpoints call _invalidate_subproject_links().
    """
    fp = tuple(sorted((os.path.normcase(p.get("path", "")),
                       os.path.normcase(p.get("parent_path") or ""))
                      for p in projects))
    now = time.time()
    do_compute = False
    with _sub_links_lock:
        snap = _sub_links_cache["data"]
        fresh = (snap is not None and _sub_links_cache["fp"] == fp
                 and now - _sub_links_cache["ts"] < _SUB_LINKS_TTL)
        if not fresh:
            if _sub_links_cache["computing"]:
                if _sub_links_cache["fp"] != fp:
                    snap = None          # stale snapshot of a different registry — skip
            else:
                _sub_links_cache["computing"] = True
                do_compute = True
    if do_compute:
        try:
            snap = _compute_subproject_links(projects)
            with _sub_links_lock:
                _sub_links_cache.update({"fp": fp, "ts": time.time(), "data": snap})
        finally:
            with _sub_links_lock:
                _sub_links_cache["computing"] = False
    if not snap:
        return
    for p in projects:
        key = _norm_realpath(p.get("path", ""))
        if key in snap["parents"]:
            p["subproject_issues"] = snap["parents"][key]
        if key in snap["children"]:
            p["link_status"] = snap["children"][key]


@app.route("/api/projects", methods=["GET"])
def api_projects_list():
    try:
        from services.task_store import open_task_count
        projects = _pm().list_projects()
        parent_paths = {os.path.normcase(p["parent_path"]) for p in projects if p.get("parent_path")}
        for p in projects:
            p["notification_count"] = _notification_count(p.get("path", ""))
            p["is_parent"] = os.path.normcase(p.get("path", "")) in parent_paths
            p["open_task_count"] = open_task_count(p.get("path", ""))
        _annotate_subproject_links(projects)
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
        resolved = os.path.normcase(str(Path(path).resolve()))
        orphans = [p.get("path") for p in _pm().list_projects()
                   if p.get("parent_path") and os.path.normcase(p["parent_path"]) == resolved]
        result = {"removed": _pm().remove_project(path)}
        if result["removed"]:
            _invalidate_subproject_links()
        if result["removed"] and orphans:
            result["orphaned_children"] = orphans
        return jsonify(result)
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
            # probe() initializes lazy backends; a fresh instance's .ready
            # is always False and would skip the build unconditionally.
            if not ei.probe()["ready"]:
                return jsonify({"success": True, "output": f"Embedding index skipped ({ei.unavailable_reason()})."})
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


# ── AgentCI: local CI execution (docs/agent-ci.md) ────────────────────────
# A CI run is minutes long, so the POST starts a worker and returns; the UI
# polls. One run per project at a time — two concurrent runs would interleave
# their writes into the same .c3/ci run index and neither result would be
# trustworthy.

_ci_runs: dict = {}          # project path -> in-flight state
_ci_lock = threading.Lock()


def _ci_worker(project: str, job: str, allow_foreign: bool, workflow: str,
               engine: str = "auto") -> None:
    from services import ci_runner as cr
    try:
        result = cr.run_ci(project, selector=job, allow_foreign=allow_foreign,
                           workflow=workflow, engine=engine)
        payload = {"running": False, "done": True, "error": None,
                   "run_id": result.run_id, "verdict": result.verdict,
                   "note": result.note}
    except Exception as exc:            # a crashed worker must not look idle
        payload = {"running": False, "done": True, "run_id": "",
                   "error": f"{type(exc).__name__}: {exc}"}
    with _ci_lock:
        _ci_runs[project] = payload


@app.route("/api/ci/inspect", methods=["GET"])
def api_ci_inspect():
    """Workflows, the normalized job DAG, and what is runnable on this host."""
    from services.ci_workflow import inspect_project
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    try:
        # inspect_project owns the engine partition, so the Hub, the CLI
        # and the tool text can never disagree about what is runnable.
        return jsonify(inspect_project(resolved))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/ci/runs", methods=["GET"])
def api_ci_runs():
    from services import ci_runner as cr
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    limit = max(1, min(100, int(request.args.get("limit") or 20)))
    return jsonify({"runs": cr.list_runs(resolved, limit=limit)})


@app.route("/api/ci/run", methods=["GET"])
def api_ci_run_detail():
    from services import ci_runner as cr
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(cr.load_run(resolved, (request.args.get("run_id") or "").strip())
                   or {})


@app.route("/api/ci/logs", methods=["GET"])
def api_ci_logs():
    from services import ci_runner as cr
    path = (request.args.get("path") or "").strip()
    run_id = (request.args.get("run_id") or "").strip()
    job = (request.args.get("job") or "").strip()
    if not (path and run_id and job):
        return jsonify({"error": "path, run_id and job are required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    tail = max(1, min(2000, int(request.args.get("tail") or 300)))
    return jsonify({"log": cr.read_log(resolved, run_id, job, tail=tail)})


@app.route("/api/ci/run", methods=["POST"])
def api_ci_run_start():
    """Start a local CI run in the background. Poll /api/ci/status."""
    data = request.get_json(force=True) or {}
    path = str(data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = str(_resolve_project_path(path))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    with _ci_lock:
        state = _ci_runs.get(resolved) or {}
        if state.get("running"):
            return jsonify({"error": "a CI run is already in progress here"}), 409
        _ci_runs[resolved] = {"running": True, "done": False, "error": None,
                              "run_id": "", "verdict": "", "note": ""}

    threading.Thread(
        target=_ci_worker,
        args=(resolved, str(data.get("job") or ""),
              bool(data.get("allow_foreign")), str(data.get("workflow") or ""),
              str(data.get("engine") or "auto")),
        daemon=True,
    ).start()
    return jsonify({"started": True})


@app.route("/api/ci/status", methods=["GET"])
def api_ci_status():
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = str(_resolve_project_path(path))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    with _ci_lock:
        return jsonify(dict(_ci_runs.get(resolved)
                            or {"running": False, "done": False}))


# ── Sub-projects: linked child .c3 branches (v2.44.0) ─────────────────────

def _sub_manager(parent: str):
    from services.subprojects import SubprojectManager
    return SubprojectManager(str(_resolve_project_path(parent)))


def _parse_json_tail(output: str):
    """Extract the trailing JSON object from CLI output (init noise precedes it)."""
    lines = (output or "").splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            try:
                return json.loads("\n".join(lines[i:]))
            except json.JSONDecodeError:
                continue
    return None


@app.route("/api/projects/subprojects", methods=["GET"])
def api_subprojects_tree():
    """Parent + children + rollup. Query: ?parent=<path>"""
    parent = (request.args.get("parent") or "").strip()
    if not parent:
        return jsonify({"error": "parent is required"}), 400
    try:
        return jsonify(_sub_manager(parent).tree())
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/subprojects/validate", methods=["POST"])
def api_subprojects_validate():
    """Pre-flight a designation. Body: {parent, folder}"""
    data = request.get_json(force=True) or {}
    parent = (data.get("parent") or "").strip()
    folder = (data.get("folder") or "").strip()
    if not parent or not folder:
        return jsonify({"error": "parent and folder are required"}), 400
    try:
        return jsonify(_sub_manager(parent).validate(folder))
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/subprojects/add", methods=["POST"])
def api_subprojects_add():
    """Designate a sub-folder (full init + link). Body: {parent, folder, name?, ide?}

    Runs through the CLI subprocess for isolation, consistent with run-init.
    """
    data = request.get_json(force=True) or {}
    parent = (data.get("parent") or "").strip()
    folder = (data.get("folder") or "").strip()
    if not parent or not folder:
        return jsonify({"error": "parent and folder are required"}), 400
    args = ["sub", "add", folder, "--parent", parent, "--json"]
    name = (data.get("name") or "").strip()
    if name:
        args += ["--name", name]
    ide = (data.get("ide") or "").strip()
    if ide and ide != "unknown":
        args += ["--ide", ide]
    res = _run_c3(args, timeout=300)
    payload = _parse_json_tail(res.get("output", ""))
    ok = res.get("success") and bool((payload or {}).get("added"))
    if ok:
        _invalidate_subproject_links()
    body = {"success": ok, "result": payload, "output": res.get("output", "")}
    return jsonify(body), (201 if ok else 500)


@app.route("/api/projects/subprojects/remove", methods=["POST"])
def api_subprojects_remove():
    """Unlink (Promote) or clear a sub-project. Body: {parent, ref, mode: unlink|clear}"""
    data = request.get_json(force=True) or {}
    parent = (data.get("parent") or "").strip()
    ref = (data.get("ref") or "").strip()
    mode = (data.get("mode") or "unlink").strip()
    if not parent or not ref:
        return jsonify({"error": "parent and ref are required"}), 400
    if mode not in ("unlink", "clear"):
        return jsonify({"error": "mode must be unlink|clear"}), 400
    args = ["sub", "remove", ref, "--parent", parent, "--json", "--yes"]
    if mode == "clear":
        args.append("--clear")
    res = _run_c3(args, timeout=300)
    payload = _parse_json_tail(res.get("output", ""))
    ok = res.get("success") and bool((payload or {}).get("removed"))
    if ok:
        _invalidate_subproject_links()
    body = {"success": ok, "result": payload, "output": res.get("output", "")}
    return jsonify(body), (200 if ok else 500)


@app.route("/api/projects/subprojects/reconcile", methods=["POST"])
def api_subprojects_reconcile():
    """Consistency check/repair. Body: {parent, fix?, prune?}"""
    data = request.get_json(force=True) or {}
    parent = (data.get("parent") or "").strip()
    if not parent:
        return jsonify({"error": "parent is required"}), 400
    try:
        result = _sub_manager(parent).reconcile(
            fix=bool(data.get("fix")), prune=bool(data.get("prune")))
        if data.get("fix"):
            _invalidate_subproject_links()
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_sub_cascade_state = {
    "running": False, "cancelled": False, "results": [],
    "current": None, "current_index": 0, "total": 0,
    "done": False, "error": None, "op": None, "parent": None,
}
_sub_cascade_lock = threading.Lock()


def _sub_cascade_worker(op: str, targets: list):
    """Per-child cascade in a background thread (progress + cancel like batch init)."""
    global _sub_cascade_state
    for i, child in enumerate(targets):
        with _sub_cascade_lock:
            if _sub_cascade_state["cancelled"]:
                break
            _sub_cascade_state["current"] = child.get("name") or child.get("path")
            _sub_cascade_state["current_index"] = i
        path = child.get("path")
        t0 = time.time()
        row = {"path": path, "name": child.get("name"), "success": False, "output": ""}
        try:
            if child.get("status") == "missing_folder":
                row["output"] = "missing_folder"
            elif child.get("status") == "missing_c3" and op != "update":
                row["output"] = "missing_c3"
            elif op == "update":
                res = _run_c3(["init", path, "--force"], timeout=600)
                row["success"] = bool(res.get("success"))
                row["output"] = res.get("output", "")
            elif op == "reindex":
                from services.doc_index import DocIndex
                from services.indexer import CodeIndex
                r = CodeIndex(path).build_index()
                try:
                    DocIndex(path).build()
                except Exception:
                    pass
                row["success"] = True
                row["output"] = (f"{r.get('files_indexed', 0)} files, "
                                 f"{r.get('chunks_created', 0)} chunks")
            elif op == "health":
                from cli.c3 import _check_c3_health
                info = _check_c3_health(path)
                row["success"] = bool(info.get("healthy"))
                row["output"] = "healthy" if row["success"] else "; ".join(info.get("issues", []))
        except Exception as e:
            row["output"] = str(e)
        row["elapsed_ms"] = int((time.time() - t0) * 1000)
        with _sub_cascade_lock:
            _sub_cascade_state["results"].append(row)
    with _sub_cascade_lock:
        _sub_cascade_state["running"] = False
        _sub_cascade_state["done"] = True
        _sub_cascade_state["current"] = None
    _invalidate_subproject_links()


@app.route("/api/projects/subprojects/cascade", methods=["POST"])
def api_subprojects_cascade():
    """Start an async cascade. Body: {parent, op: update|reindex|health, include_parent?}"""
    global _sub_cascade_state
    with _sub_cascade_lock:
        if _sub_cascade_state["running"]:
            return jsonify({"error": "Cascade already in progress"}), 409
    data = request.get_json(force=True) or {}
    parent = (data.get("parent") or "").strip()
    op = (data.get("op") or "").strip()
    if not parent:
        return jsonify({"error": "parent is required"}), 400
    if op not in ("update", "reindex", "health"):
        return jsonify({"error": "op must be update|reindex|health"}), 400
    try:
        sm = _sub_manager(parent)
        listed = sm.list()
        # Skipped children (missing folder; missing .c3 unless the op can
        # create it) are excluded up front so total and the success
        # denominator reflect real work — previously they inflated total
        # and reported as failures.
        skipped = [t for t in listed
                   if t.get("status") == "missing_folder"
                   or (t.get("status") == "missing_c3" and op != "update")]
        targets = [t for t in listed if t not in skipped]
        if data.get("include_parent"):
            targets.append({"name": "(parent)", "path": sm.parent_path, "status": "ok"})
        if not targets:
            return jsonify({"error": "No cascade-eligible sub-projects"
                            + (f" ({len(skipped)} skipped)" if skipped else "")}), 400
        with _sub_cascade_lock:
            _sub_cascade_state = {
                "running": True, "cancelled": False, "results": [],
                "current": None, "current_index": 0, "total": len(targets),
                "done": False, "error": None, "op": op, "parent": sm.parent_path,
            }
        t = threading.Thread(target=_sub_cascade_worker, args=(op, targets), daemon=True)
        t.start()
        return jsonify({"started": True, "total": len(targets), "op": op,
                        "skipped": len(skipped)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/projects/subprojects/cascade/status", methods=["GET"])
def api_subprojects_cascade_status():
    with _sub_cascade_lock:
        return jsonify(dict(_sub_cascade_state))


@app.route("/api/projects/subprojects/cascade/cancel", methods=["POST"])
def api_subprojects_cascade_cancel():
    with _sub_cascade_lock:
        if not _sub_cascade_state["running"]:
            return jsonify({"cancelled": False, "message": "No cascade in progress"})
        _sub_cascade_state["cancelled"] = True
        return jsonify({"cancelled": True})


# ── Drill-in inspection + cross-project search + config editor (v2.44.0) ──
# Read-only endpoints borrow in-process C3Runtimes from a hub-owned LRU cache
# (no per-project UI server needed); config writes audit to the target project.

_hub_rt_cache = None
_hub_rt_lock = threading.Lock()


def _get_runtime(path: str):
    """Borrow an in-process C3Runtime for inspection (LRU-cached, lazy import)."""
    global _hub_rt_cache
    with _hub_rt_lock:
        if _hub_rt_cache is None:
            from services.project_runtime import ProjectRuntimeCache
            try:
                size = max(1, int(_read_hub_config().get("runtime_cache_size", 8) or 8))
            except Exception:
                size = 8
            _hub_rt_cache = ProjectRuntimeCache(max_cached=size)
    return _hub_rt_cache.get(path)


def _shutdown_runtime_cache():
    """Best-effort release of cached runtimes (called before restart/stop)."""
    global _hub_rt_cache
    with _hub_rt_lock:
        cache, _hub_rt_cache = _hub_rt_cache, None
    if cache is not None:
        try:
            cache.shutdown()
        except Exception:
            pass


@app.route("/api/projects/browse", methods=["POST"])
def api_projects_browse():
    """Directory listing for the folder picker (dirs only, never file contents).

    Body: {path}. Same trust level as /api/projects/open — loopback + CSRF guard.
    """
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = Path(path).resolve()
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    if not resolved.is_dir():
        return jsonify({"error": f"Not a directory: {resolved}"}), 404
    registered = {os.path.normcase(p.get("path", "")) for p in _pm()._read_projects()}
    dirs = []
    try:
        for child in sorted(resolved.iterdir(), key=lambda c: c.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            dirs.append({
                "name": child.name,
                "path": str(child),
                "has_c3": (child / ".c3").is_dir(),
                "registered": os.path.normcase(str(child)) in registered,
            })
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    parent = str(resolved.parent) if resolved.parent != resolved else None
    return jsonify({"path": str(resolved), "parent": parent, "dirs": dirs})


@app.route("/api/projects/inspect", methods=["POST"])
def api_projects_inspect():
    """Drill-in data views. Body: {path, view: overview|memory|ledger|sessions, query?, file?, limit?}"""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    view = (data.get("view") or "overview").strip().lower()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        from services.project_runtime import resolve_project
        resolved = resolve_project(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    try:
        rt = _get_runtime(resolved["path"])
    except ValueError as e:
        return jsonify({"error": str(e), "needs_init": True}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    try:
        limit = max(1, min(int(data.get("limit") or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    query = (data.get("query") or "").strip()

    try:
        if view == "overview":
            details = _pm().get_project_details(resolved["path"]) or {}
            ledger_total = 0
            if rt.edit_ledger:
                try:
                    ledger_total = int(rt.edit_ledger.get_stats().get("total", 0))
                except Exception:
                    ledger_total = 0
            task_store = getattr(rt, "task_store", None)
            counts = {
                "facts": len(rt.memory.facts),
                "edits": ledger_total,
                "sessions": len(rt.session_mgr.list_sessions(500)),
                "notifications": _notification_count(resolved["path"]),
                "tasks_open": task_store.stats()["open"] if task_store else 0,
            }
            return jsonify({"project": details, "counts": counts})
        if view == "memory":
            if query:
                return jsonify({"results": rt.memory.recall(query, top_k=min(limit, 20))})
            return jsonify({"facts": rt.memory.facts, "total": len(rt.memory.facts)})
        if view == "ledger":
            if not rt.edit_ledger:
                return jsonify({"history": [], "stats": {}})
            file_filter = (data.get("file") or "").strip()
            return jsonify({
                "history": rt.edit_ledger.get_history(file=file_filter or None, limit=limit),
                "stats": rt.edit_ledger.get_stats(),
            })
        if view == "sessions":
            return jsonify({"sessions": rt.session_mgr.list_sessions(limit)})
        if view == "tasks":
            task_store = getattr(rt, "task_store", None)
            if task_store is None:
                return jsonify({"board": {"columns": {}, "milestones": [], "stats": {}},
                                "notes": []})
            return jsonify({"board": task_store.board(),
                            "notes": task_store.list_notes(limit=20)})
        return jsonify({"error": f"unknown view '{view}'"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/search/global", methods=["POST"])
def api_search_global():
    """Cross-project search. Body: {query, kind: code|memory|both, projects?, top_k?}"""
    data = request.get_json(force=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    kind = (data.get("kind") or "both").strip().lower()
    if kind not in ("code", "memory", "both"):
        return jsonify({"error": "kind must be code|memory|both"}), 400
    try:
        top_k = max(1, min(int(data.get("top_k") or 3), 10))
    except (TypeError, ValueError):
        top_k = 3
    t0 = time.time()

    wanted = data.get("projects") or []
    wanted_norm = ({os.path.normcase(str(Path(p).resolve())) for p in wanted if p}
                   if wanted else None)

    candidates, skipped = [], []
    for p in _pm().list_projects():
        ppath = p.get("path") or ""
        if wanted_norm is not None and os.path.normcase(ppath) not in wanted_norm:
            continue
        if not Path(ppath).is_dir():
            skipped.append({"path": ppath, "reason": "not accessible"})
            continue
        if not (Path(ppath) / ".c3").is_dir():
            skipped.append({"path": ppath, "reason": "not initialized"})
            continue
        candidates.append(p)
    if len(candidates) > 10:
        for p in candidates[10:]:
            skipped.append({"path": p.get("path"), "reason": "over per-request cap (10)"})
        candidates = candidates[:10]

    results = []
    for p in candidates:
        row = {"project": {"name": p.get("name"), "path": p.get("path")},
               "code": [], "memory": [], "error": None}
        try:
            rt = _get_runtime(p["path"])
            if kind in ("code", "both"):
                for r in rt.indexer.search(query, top_k=top_k, max_tokens=600):
                    row["code"].append({
                        "file": r.get("file"), "name": r.get("name"),
                        "type": r.get("type"), "lines": r.get("lines"),
                        "score": r.get("score"),
                        "snippet": (r.get("content") or "")[:300],
                    })
            if kind in ("memory", "both"):
                for f in rt.memory.recall(query, top_k=top_k):
                    row["memory"].append({"id": f.get("id"), "fact": f.get("fact"),
                                          "category": f.get("category")})
        except Exception as e:
            row["error"] = str(e)
        results.append(row)

    return jsonify({"query": query, "projects_searched": len(candidates),
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "results": results, "skipped": skipped})


_CONFIG_READ_SECTIONS = ("hybrid", "agents", "delegate", "proxy", "mcp", "bitbucket", "meta",
                         "memory_llm")
_CONFIG_WRITE_SECTIONS = ("hybrid", "agents", "delegate", "proxy", "mcp", "meta", "memory_llm")
# "enforcement" is deliberately NOT writable here: the generic deep-merge would
# bypass mode/ttl/blocked_tools validation and the set_by provenance rules.
# POST /api/projects/enforcement is the only write path.
# api_key: secrets never transit the hub or land in config.json — the Ollama
# cloud key lives in the OS keyring (project Settings UI / OLLAMA_API_KEY env).
_CONFIG_REFUSED_KEYS = ("version", "project_path", "permission_tier", "subprojects", "parent",
                        "api_key")


def _config_defaults(section: str) -> dict:
    from core import config as core_config
    return {
        "hybrid": core_config.DEFAULTS,
        "agents": core_config.AGENT_DEFAULTS,
        "delegate": core_config.DELEGATE_DEFAULTS,
        "proxy": core_config.PROXY_DEFAULTS,
        "bitbucket": core_config.BITBUCKET_DEFAULTS,
        "mcp": {"mode": "direct"},
        "meta": {},
        "memory_llm": core_config.MEMORY_LLM_DEFAULTS,
    }.get(section, {})


@app.route("/api/projects/config", methods=["GET"])
def api_projects_config_get():
    """Structured .c3/config.json read. Query: ?path=...&section=hybrid|agents|..."""
    path = (request.args.get("path") or "").strip()
    section = (request.args.get("section") or "").strip().lower()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    cfg_file = resolved / ".c3" / "config.json"
    if not cfg_file.exists():
        return jsonify({"error": "not initialized", "needs_init": True}), 409
    try:
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500
    if section:
        if section not in _CONFIG_READ_SECTIONS:
            return jsonify({"error": f"section must be one of {list(_CONFIG_READ_SECTIONS)}"}), 400
        return jsonify({"path": str(resolved), "section": section,
                        "config": cfg.get(section) or {},
                        "defaults": _config_defaults(section)})
    return jsonify({"path": str(resolved),
                    "config": {k: cfg.get(k) for k in _CONFIG_READ_SECTIONS if k in cfg},
                    "defaults": {k: _config_defaults(k) for k in _CONFIG_READ_SECTIONS}})


# ── Credentials (hub) ────────────────────────────────────────────────────────
# Write-only wire contract, hub edition: values go IN over POST and are never
# returned by any hub route (no reveal exists here). `credentials` stays out of
# _CONFIG_WRITE_SECTIONS — these dedicated routes are the only hub write path.

def _cred_entry_public(name, entry, *, usage=None, shadows_global=None):
    """Explicit allowlist serializer — structurally cannot emit a value.

    Delegates to credential_store.public_entry so the hub, the per-project UI
    and the mobile gateway all serialize through one allowlist; a second copy
    is exactly the drift the write-only wire contract cannot survive."""
    from services import credential_store as cred_store
    return cred_store.public_entry(
        name, entry, usage=usage, shadows_global=shadows_global)


def _resolve_cred_target(path: str, scope: str, *, mutation: bool):
    """Resolve a credentials request target to (project, store_path, error).

    scope='global' with no path targets the shared vault (~/.c3) directly;
    project scope requires a registered path. Mutations on a path without a
    .c3/ dir get 409 needs_init so the hub can't scatter .c3 dirs around."""
    from services import credential_store as cred_store
    if scope not in ("project", "global"):
        return None, "", (jsonify({"error": "scope must be 'project' or 'global'"}), 400)
    if not path:
        if scope == "project":
            return None, "", (jsonify({"error": "path is required for project scope"}), 400)
        home = cred_store.global_base()
        if home is None:
            return None, "", (jsonify({"error": "global scope unresolvable (no home dir)"}), 500)
        return None, str(home), None
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return None, "", (jsonify({"error": str(e)}), 404)
    if mutation and not (resolved / ".c3").is_dir():
        return None, "", (jsonify({"error": "not initialized", "needs_init": True}), 409)
    return resolved, str(resolved), None


def _hub_cred_audit(action: str, name: str, scope: str, project) -> None:
    """Names only — never values. Failure-safe. Project mutations audit to the
    target project's ActivityLog + EditLedger; global-scope mutations also land
    in ~/.c3/activity_log.jsonl so the shared vault keeps its own trail."""
    if project is not None:
        try:
            from services.activity_log import ActivityLog
            ActivityLog(str(project)).log("cred_action", {
                "kind": "creds", "action": action, "name": name,
                "scope": scope, "via": "hub",
            })
        except Exception:
            pass
        try:
            from services.edit_ledger import EditLedger
            EditLedger(str(project)).log_edit(
                file=f"cred://{name}", change_type=f"cred_{action}",
                summary=f"{action} {name} ({scope}) via Hub",
                tags=["creds", action],
                detail={"kind": "creds", "action": action, "name": name,
                        "scope": scope},
            )
        except Exception:
            pass
    if scope == "global" or project is None:
        try:
            from services import credential_store as cred_store
            from services.activity_log import ActivityLog
            home = cred_store.global_base()
            if home is not None:
                ActivityLog(str(home)).log("cred_action", {
                    "kind": "creds", "action": action, "name": name,
                    "scope": scope, "via": "hub",
                })
        except Exception:
            pass


@app.route("/api/projects/credentials", methods=["GET"])
def api_projects_credentials():
    """Masked credential registry for a project (global entries + project
    shadows). Values never transit the hub outbound — the allowlist serializer
    returns metadata, usage and shadow info only."""
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    from services import credential_store as cred_store
    usage = cred_store.read_usage_state(str(resolved))
    home = cred_store.global_base()
    global_names = set(cred_store.list_entries(str(home))) if home else set()
    entries = []
    for name, entry in cred_store.list_entries(str(resolved)).items():
        entries.append(_cred_entry_public(
            name, entry, usage=usage,
            shadows_global=(entry.get("scope") == "project"
                            and name in global_names)))
    return jsonify({"path": str(resolved), "entries": entries})


@app.route("/api/projects/credentials", methods=["POST"])
def api_projects_credentials_set():
    """Create/update an entry from the hub. `value` optional — metadata-only
    update when absent, touching ONLY the keys present in the payload; a
    submitted value is stored and never echoed back. scope='global' with no
    `path` targets the shared vault directly."""
    from services import credential_store as cred_store
    data = request.get_json(force=True) or {}
    name = str(data.get("name") or "").strip()
    scope = str(data.get("scope") or "project").strip().lower()
    project, store_path, err = _resolve_cred_target(
        str(data.get("path") or "").strip(), scope, mutation=True)
    if err:
        return err
    value = str(data.get("value") or "")
    ctype = str(data.get("type") or data.get("ctype") or "token")
    try:
        if value:
            entry = cred_store.set_credential(
                name, value, scope=scope, project_path=store_path, ctype=ctype,
                description=str(data.get("description") or ""),
                env_var=str(data.get("env_var") or ""),
                agent_readable=bool(data.get("agent_readable")),
                inject=bool(data.get("inject")))
        else:
            # Metadata-only update: touch ONLY the keys present in the payload
            # so a single-field toggle can't clobber the others.
            fields = {}
            for key in ("description", "env_var"):
                if key in data:
                    fields[key] = str(data[key] or "")
            for key in ("agent_readable", "inject"):
                if key in data:
                    fields[key] = bool(data[key])
            if "type" in data or "ctype" in data:
                fields["type"] = ctype
            entry = cred_store.update_metadata(
                name, scope=scope, project_path=store_path, **fields)
    except cred_store.CredentialError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    _hub_cred_audit("set" if value else "update", name, scope, project)
    return jsonify({"entry": _cred_entry_public(name, {**entry, "scope": scope})})


@app.route("/api/projects/credentials/import", methods=["POST"])
def api_projects_credentials_import():
    """Import KEY=VALUE lines (.env paste). Values are stored, never echoed."""
    from services import credential_store as cred_store
    data = request.get_json(force=True) or {}
    scope = str(data.get("scope") or "project").strip().lower()
    project, store_path, err = _resolve_cred_target(
        str(data.get("path") or "").strip(), scope, mutation=True)
    if err:
        return err
    try:
        result = cred_store.import_env(
            str(data.get("text") or ""), scope=scope, project_path=store_path,
            overwrite=bool(data.get("overwrite")))
    except cred_store.CredentialError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    for created in result["created"]:
        _hub_cred_audit("set", created, scope, project)
    return jsonify(result)


@app.route("/api/projects/credentials/<name>", methods=["DELETE"])
def api_projects_credentials_delete(name):
    """Delete an entry (value + registry). Scope inferred from the owning
    realm when omitted."""
    from services import credential_store as cred_store
    scope = str(request.args.get("scope") or "").strip().lower()
    path = str(request.args.get("path") or "").strip()
    project, store_path, err = _resolve_cred_target(
        path, scope or ("project" if path else "global"), mutation=True)
    if err:
        return err
    if not scope:
        entry = cred_store.get_entry(name, project_path=store_path)
        scope = (entry.get("scope") or "project") if entry else \
            ("project" if path else "global")
    try:
        removed = cred_store.delete_credential(
            name, scope=scope, project_path=store_path)
    except cred_store.CredentialError as exc:
        return jsonify({"error": str(exc)}), 400
    if removed:
        _hub_cred_audit("delete", name, scope, project)
    return jsonify({"removed": bool(removed), "scope": scope})


@app.route("/api/projects/credentials/<name>/check", methods=["POST"])
def api_projects_credentials_check(name):
    """Resolvability probe — returns a fingerprint, never the value."""
    from services import credential_store as cred_store
    data = request.get_json(silent=True) or {}
    path = str(data.get("path") or "").strip()
    project, store_path, err = _resolve_cred_target(
        path, "project" if path else "global", mutation=False)
    if err:
        return err
    entry = cred_store.get_entry(name, project_path=store_path)
    if not entry:
        return jsonify({"error": f"no credential named '{name}'"}), 404
    return jsonify({
        "name": name,
        "scope": entry["scope"],
        "storage": entry.get("storage", "keyring"),
        "resolvable": cred_store.get_value(name, project_path=store_path) is not None,
        "fingerprint": cred_store.fingerprint(name, project_path=store_path),
    })


@app.route("/api/hub/credentials/overview", methods=["GET"])
def api_hub_credentials_overview():
    """Cross-project credential inventory: the global vault plus each
    registered project's project-scoped entries, with shadow info both ways.
    Metadata only — the allowlist serializer structurally cannot emit a value."""
    from services import credential_store as cred_store
    home = cred_store.global_base()
    global_entries = cred_store.list_entries(str(home)) if home else {}
    shadowed_in = {name: [] for name in global_entries}
    projects_out = []
    for p in _pm().list_projects():
        ppath = str(p.get("path") or "")
        row = {"name": p.get("name") or "", "path": ppath,
               "initialized": True, "error": None, "entries": []}
        try:
            if not (Path(ppath) / ".c3").is_dir():
                row["initialized"] = False
            else:
                usage = cred_store.read_usage_state(ppath)
                for name, entry in cred_store.list_entries(ppath).items():
                    if entry.get("scope") != "project":
                        continue
                    row["entries"].append(_cred_entry_public(
                        name, entry, usage=usage,
                        shadows_global=name in global_entries))
                    if name in shadowed_in:
                        shadowed_in[name].append(
                            {"name": row["name"], "path": ppath})
        except Exception as e:  # per-row isolation, like /api/search/global
            row["error"] = str(e)
        projects_out.append(row)
    global_usage = cred_store.read_usage_state(str(home)) if home else {}
    global_out = [
        {**_cred_entry_public(name, entry, usage=global_usage),
         "shadowed_in": shadowed_in.get(name, [])}
        for name, entry in global_entries.items()
    ]
    return jsonify({"global": {"entries": global_out}, "projects": projects_out})


# ── Agent Locks (hub) ────────────────────────────────────────────────────────
# Read + one human override. force-release is here and in `c3 locks`, never in
# the c3_locks agent tool: it bumps the fencing counter so a holder that comes
# back is stale by construction, which is a decision for a person.


@app.route("/api/hub/locks/overview", methods=["GET"])
def api_hub_locks_overview():
    """Cross-project lease snapshot for the Locks tab.

    Per-row isolation like /api/hub/credentials/overview: one unreadable
    project must not blank the page. A project we cannot read reports
    ``error``/``initialized`` rather than an empty lease list — "0 leases"
    would claim the repo is clear when we simply do not know.
    """
    from services import agent_locks as al

    rows, total = [], 0
    for p in _pm().list_projects():
        ppath = str(p.get("path") or "")
        row = {"name": p.get("name") or "", "path": ppath, "initialized": True,
               "error": None, "enabled": True, "mode": "advisory",
               "locks": [], "count": 0, "fencing": 0}
        try:
            if not (Path(ppath) / ".c3").is_dir():
                row["initialized"] = False
            else:
                cfg = al.config(ppath)
                row["enabled"] = cfg["enabled"]
                row["mode"] = cfg["mode"]
                snap = al.store_for(ppath).snapshot()
                row["locks"] = snap["locks"]
                row["count"] = snap["count"]
                row["fencing"] = snap["fencing"]
                total += snap["count"]
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)
    return jsonify({"projects": rows, "total": total,
                    # Surfaced so the UI can state its own limits instead of
                    # implying a lease covers everything (spec §9).
                    "coverage_note": (
                        "Leases gate C3 tool surfaces only. A raw shell "
                        "redirect, a non-Claude agent, or a human in an editor "
                        "is not covered.")})


@app.route("/api/projects/locks/force-release", methods=["POST"])
def api_projects_locks_force_release():
    """Break one lease regardless of holder. Human-only, audited on the target."""
    from services import agent_locks as al

    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    relpath = (data.get("relpath") or "").strip()
    note = (data.get("note") or "").strip()
    if not path or not relpath:
        return jsonify({"error": "path and relpath are required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    res = al.store_for(resolved).force_release(relpath, by="hub", note=note)
    if not res.get("forced"):
        return jsonify({"error": res.get("error", "force-release failed"),
                        "reason": res.get("reason", "")}), 400

    try:
        from services.activity_log import ActivityLog
        ActivityLog(resolved).log("lock_force_release", {
            "kind": "locks", "relpath": res.get("relpath"),
            "previous_owner": res.get("previous_owner"), "via": "hub"})
    except Exception:
        pass
    try:
        from services.edit_ledger import EditLedger
        EditLedger(resolved).log_edit(
            file=f"lock://{res.get('relpath', relpath)}",
            change_type="lock_force_release",
            summary=f"force-released lease on {res.get('relpath', relpath)}"
                    + (f" — {note}" if note else ""),
            tags=["locks", "force_release"],
            detail={"kind": "locks", "previous_owner": res.get("previous_owner"),
                    "was_locked": res.get("was_locked"), "note": note})
    except Exception:
        pass
    return jsonify(res)


@app.route("/api/hub/enforcement/overview", methods=["GET"])
def api_hub_enforcement_overview():
    """Cross-project tool-discipline snapshot for the Enforcement tab.

    Per-row isolation like the Locks/Credentials overviews: one unreadable
    project must not blank the page, and a project we could not read reports
    ``error`` rather than a mode — claiming 'strict' for a repo we failed to
    read would be a guess presented as a fact.

    Denial counts come from services.access_telemetry, split by layer so the
    UI can point at the right lever: discipline blocks are cleared by changing
    the mode here, path denials by editing Access Guard rules.
    """
    from services import access_telemetry as at
    from services import enforcement_policy as ep

    rows = []
    totals = {"discipline": 0, "access": 0}
    for p in _pm().list_projects():
        ppath = str(p.get("path") or "")
        row = {"name": p.get("name") or "", "path": ppath, "initialized": True,
               "error": None, "mode": None, "scope": "", "set_by": "",
               "signal_ttl_s": ep.DEFAULT_SIGNAL_TTL_S, "warnings": [],
               "tier": "", "tier_implies": "", "denials": {}, "denial_total": 0}
        try:
            if not (Path(ppath) / ".c3").is_dir():
                row["initialized"] = False
            else:
                policy = ep.resolve(ppath)
                row["mode"] = policy.mode
                row["scope"] = policy.scope
                row["set_by"] = policy.set_by
                row["signal_ttl_s"] = policy.signal_ttl_s
                row["warnings"] = list(policy.warnings)
                row["blocked_tools"] = sorted(policy.blocked_tools)

                cfg_path = Path(ppath) / ".c3" / "config.json"
                try:
                    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    tier = str(cfg.get("permission_tier") or "")
                except Exception:
                    tier = ""
                row["tier"] = tier
                row["tier_implies"] = ep.derive_from_tier(tier) if tier else ""

                agg = at.aggregate(ppath)
                row["denials"] = agg["by_layer"]
                row["denial_total"] = agg["total"]
                row["top_denials"] = [
                    {**r, "fix": at.suggest(r)} for r in agg["rows"][:5]
                ]
                for layer, count in agg["by_layer"].items():
                    totals[layer] = totals.get(layer, 0) + count
        except Exception as e:
            row["error"] = str(e)
        rows.append(row)

    # The global (~/.c3) section, shown as its own card. A corrupt global
    # config must not blank the tab — report the error inside the object.
    try:
        g = ep.resolve_global()
        global_policy = {
            "configured": g.scope == "global",
            "mode": g.mode or None,
            "set_by": g.set_by,
            "signal_ttl_s": g.signal_ttl_s,
            "blocked_tools": sorted(g.blocked_tools),
            "warnings": list(g.warnings),
        }
    except Exception as e:
        global_policy = {"configured": False, "mode": None, "error": str(e)}

    return jsonify({
        "projects": rows,
        "modes": [{"id": m, "help": ep.MODE_HELP[m]} for m in ep.MODES],
        "default_mode": ep.DEFAULT_MODE,
        "tier_map": ep.TIER_TO_MODE,
        "totals": totals,
        "global_policy": global_policy,
        # Stated so the tab can never imply that turning discipline down also
        # turns the security boundaries down. Mirrors docs/enforcement.md.
        "coverage_note": (
            "Tool discipline only governs whether native Edit/Write are "
            "pushed through c3_edit. At every mode — including off — Access "
            "Guard path rules, the credential-vault write guard, and agent "
            "locks still enforce. The edit ledger records native writes "
            "either way; strict additionally gets c3_edit's pre-edit snapshot."
        ),
    })


@app.route("/api/projects/enforcement", methods=["GET"])
def api_projects_enforcement_get():
    """One project's effective policy plus its denial evidence.

    Hub-side mirror of the per-project GET /api/enforcement — feeds the drill
    panel's Discipline tab. Optional ``?session=`` narrows the denial
    aggregate to one session id (full id; the event search route does prefix
    matching for the UI's 8-char short ids).
    """
    from services import access_telemetry as at
    from services import enforcement_policy as ep

    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    policy = ep.resolve(resolved)
    try:
        cfg = json.loads((Path(resolved) / ".c3" / "config.json")
                         .read_text(encoding="utf-8"))
        tier = str(cfg.get("permission_tier") or "") if isinstance(cfg, dict) else ""
    except Exception:
        tier = ""
    session = (request.args.get("session") or "").strip()
    agg = at.aggregate(resolved, session_id=session)

    return jsonify({
        "mode": policy.mode,
        "scope": policy.scope,
        "set_by": policy.set_by,
        "signal_ttl_s": policy.signal_ttl_s,
        "blocked_tools": sorted(policy.blocked_tools),
        "warnings": list(policy.warnings),
        "tier": tier,
        "tier_implies": ep.derive_from_tier(tier) if tier else "",
        "default_mode": ep.DEFAULT_MODE,
        "modes": [{"id": m, "help": ep.MODE_HELP[m]} for m in ep.MODES],
        "denials": {
            "total": agg["total"],
            "by_layer": agg["by_layer"],
            "rows": [{**r, "fix": at.suggest(r)} for r in agg["rows"][:12]],
        },
    })


@app.route("/api/projects/enforcement", methods=["POST"])
def api_projects_enforcement_set():
    """Set tool-discipline policy fields. Human-only, audited on target.

    Body: ``{path?, scope?, mode?, signal_ttl_s?, blocked_tools?}``.
    ``scope`` defaults to project (``path`` required); ``global`` writes
    ``~/.c3`` and ignores ``path``. A body with ``mode`` goes through
    ``set_mode`` — always ``set_by='user'``: a change made deliberately in
    the Hub is an explicit choice and must survive a later permission-tier
    change, the same as `c3 enforce`. A mode-less body goes through
    ``set_fields``, which never touches ``mode``/``set_by``.
    """
    from services import enforcement_policy as ep

    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    scope = (data.get("scope") or "project").strip().lower()
    mode = (data.get("mode") or "").strip().lower()
    ttl = data.get("signal_ttl_s")
    blocked = data.get("blocked_tools")
    if scope not in ("project", "global"):
        return jsonify({"error": "scope must be 'project' or 'global'"}), 400
    if scope == "project":
        if not path:
            return jsonify({"error": "path is required for project scope"}), 400
        try:
            resolved = _resolve_project_path(path)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
    else:
        resolved = "."

    try:
        if mode:
            result = ep.set_mode(mode, resolved, set_by=ep.SET_BY_USER,
                                 scope=scope, signal_ttl_s=ttl,
                                 blocked_tools=blocked)
        elif ttl is not None or blocked is not None:
            result = ep.set_fields(resolved, scope=scope,
                                   signal_ttl_s=ttl, blocked_tools=blocked)
        else:
            return jsonify({"error": "nothing to set — pass mode, "
                                     "signal_ttl_s or blocked_tools"}), 400
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400

    # Audit on the target project. Global scope has no target project to
    # audit into — the ~/.c3/config.json write is itself the record.
    if scope == "project":
        action = "set_mode" if mode else "set_fields"
        detail = {"kind": "enforcement", "action": action,
                  "mode": result.get("mode", ""),
                  "previous": result.get("previous", ""),
                  "scope": result["scope"], "via": "hub"}
        if "signal_ttl_s" in result:
            detail["signal_ttl_s"] = result["signal_ttl_s"]
        if "blocked_tools" in result:
            detail["blocked_tools"] = result["blocked_tools"]
        if mode:
            summary = (f"tool discipline {result.get('previous') or 'default'} "
                       f"-> {result['mode']} via the Hub")
        else:
            parts = []
            if "signal_ttl_s" in result:
                parts.append(f"signal_ttl_s={result['signal_ttl_s']}")
            if "blocked_tools" in result:
                parts.append("blocked_tools="
                             + ",".join(result["blocked_tools"] or ["<none>"]))
            summary = "tool discipline " + ", ".join(parts) + " via the Hub"
        try:
            from services.activity_log import ActivityLog
            ActivityLog(resolved).log("access_action", dict(detail))
        except Exception:
            pass
        try:
            from services.edit_ledger import EditLedger
            EditLedger(resolved).log_edit(
                file=f"enforcement://{result['scope']}",
                change_type=f"enforcement_{action}",
                summary=summary,
                tags=["enforcement", "access"],
                detail=detail)
        except Exception:
            pass
    return jsonify(result)


@app.route("/api/projects/enforcement/denials/search", methods=["GET"])
def api_projects_enforcement_denials_search():
    """Search one project's raw denial events. Read-only; newest first.

    Params: ``path`` (required), ``q`` (AND'd case-insensitive substrings over
    path/rule/tool), ``layer``, ``tool`` (exact), ``session`` (prefix — the UI
    shows 8-char short ids), ``since`` (ISO-8601; events store
    second-resolution UTC isoformat, so plain string comparison is correct),
    ``limit`` (default 200, cap 500).
    """
    from services import access_telemetry as at

    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify(at.search_events(
        resolved,
        q=request.args.get("q") or "",
        layer=request.args.get("layer") or "",
        tool=request.args.get("tool") or "",
        session=request.args.get("session") or "",
        since=request.args.get("since") or "",
        limit=request.args.get("limit") or 200))


@app.route("/api/projects/enforcement/denials", methods=["DELETE"])
def api_projects_enforcement_denials_clear():
    """Reset one project's denial counters (they are diagnostics, not audit)."""
    from services import access_telemetry as at

    data = request.get_json(silent=True) or {}
    path = (request.args.get("path") or data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    return jsonify({"cleared": at.clear(resolved)})


@app.route("/api/projects/config", methods=["PUT"])
def api_projects_config_put():
    """Whitelisted section write: deep-merge, atomic replace, audited on the target."""
    data = request.get_json(force=True) or {}
    path = (data.get("path") or "").strip()
    section = (data.get("section") or "").strip().lower()
    values = data.get("values")
    if not path or not section:
        return jsonify({"error": "path and section are required"}), 400
    if section not in _CONFIG_WRITE_SECTIONS:
        return jsonify({"error": f"section must be one of {list(_CONFIG_WRITE_SECTIONS)}"}), 400
    if not isinstance(values, dict):
        return jsonify({"error": "values must be an object"}), 400
    for refused in _CONFIG_REFUSED_KEYS:
        if refused in values:
            # 'subprojects' under the hybrid section is the federation
            # settings DICT (memory_rollup / search_fanout / …), not the
            # top-level child-links ARRAY — the array stays refused (and is
            # unreachable through the section whitelist anyway). Schema-check
            # the dict: exactly these keys, exactly these types — the generic
            # deep-merge below would happily persist anything.
            if (section == "hybrid" and refused == "subprojects"
                    and isinstance(values["subprojects"], dict)):
                bad = _sub_federation_errors(values["subprojects"])
                if bad:
                    return jsonify({"error": f"hybrid.subprojects: {bad}"}), 400
                continue
            return jsonify({"error": f"'{refused}' cannot be edited here"}), 400
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    cfg_file = resolved / ".c3" / "config.json"
    if not cfg_file.exists():
        return jsonify({"error": "not initialized", "needs_init": True}), 409
    try:
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception as e:
        return jsonify({"error": f"config unreadable: {e}"}), 500

    current = cfg.get(section)
    merged = dict(current) if isinstance(current, dict) else {}
    defaults = _config_defaults(section)
    for key, val in values.items():
        base = merged.get(key, defaults.get(key))
        if isinstance(base, dict) and isinstance(val, dict):
            sub = dict(base)
            sub.update(val)
            merged[key] = sub
        elif isinstance(defaults.get(key), bool) and not isinstance(val, bool):
            merged[key] = str(val).strip().lower() in ("1", "true", "yes", "on")
        elif (isinstance(defaults.get(key), int) and not isinstance(defaults.get(key), bool)
              and not isinstance(val, (int, float)) ):
            try:
                merged[key] = int(val)
            except (TypeError, ValueError):
                return jsonify({"error": f"'{key}' must be an integer"}), 400
        else:
            merged[key] = val
    cfg[section] = merged
    tmp = cfg_file.with_name("config.json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, cfg_file)
    try:
        ActivityLog(str(resolved)).log("hub_config_write", {
            "section": section, "keys": sorted(values.keys()), "source": "hub"})
    except Exception:
        pass
    return jsonify({"saved": True, "section": section, "config": merged})


# ── Project management: tasks / milestones / notes (v2.45.0) ──────────────
# Direct TaskStore per request (reload-per-op store — no runtime build needed
# for mutations); every write audited to the target project's activity log.

def _pm_resolve(path: str):
    """Resolve a PM request path -> (Path, error_response|None)."""
    if not path:
        return None, (jsonify({"error": "path is required"}), 400)
    try:
        resolved = _resolve_project_path(path)
    except ValueError as e:
        return None, (jsonify({"error": str(e)}), 404)
    if not (resolved / ".c3").is_dir():
        return None, (jsonify({"error": "not initialized", "needs_init": True}), 409)
    return resolved, None


def _pm_store(path):
    from services.task_store import TaskStore
    return TaskStore(str(path))


def _pm_audit(path, entity, op, item_id=""):
    try:
        ActivityLog(str(path)).log("pm_write", {
            "entity": entity, "op": op, "id": item_id, "source": "hub"})
    except Exception:
        pass


@app.route("/api/projects/pm", methods=["GET"])
def api_projects_pm_get():
    """Full PM board for one project. Query: path, milestone?, tag?,
    include_children? (parent rollup), include_archived?"""
    resolved, err = _pm_resolve((request.args.get("path") or "").strip())
    if err:
        return err
    store = _pm_store(resolved)
    board = store.board(
        milestone_id=(request.args.get("milestone") or None),
        tag=(request.args.get("tag") or None),
        include_archived=request.args.get("include_archived") == "1",
    )
    out = {"path": str(resolved), "board": board, "notes": store.list_notes(limit=100)}
    if request.args.get("include_children") == "1":
        children = []
        try:
            from services.subprojects import SubprojectManager
            for c in SubprojectManager(str(resolved)).list():
                if c["status"] in ("missing_folder", "missing_c3"):
                    continue
                rows = [t for t in _pm_store(c["path"]).list_tasks(limit=100)
                        if t.get("status") != "done"]
                children.append({"name": c["name"], "path": c["path"], "tasks": rows})
        except Exception:
            pass
        out["children"] = children
    return jsonify(out)


@app.route("/api/projects/pm/task", methods=["POST", "PUT", "DELETE"])
def api_pm_task():
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    store = _pm_store(resolved)

    if request.method == "POST":
        res = store.create_task(
            data.get("title", ""), description=data.get("description", ""),
            status=data.get("status") or "backlog",
            priority=data.get("priority") or "p2",
            due_date=data.get("due_date") or None,
            tags=data.get("tags") or [], milestone_id=data.get("milestone_id"),
            links=data.get("links") or [], created_by="hub")
        if "error" in res:
            return jsonify(res), 400
        _pm_audit(resolved, "task", "create", res["id"])
        return jsonify({"created": True, "task": res}), 201

    if request.method == "PUT":
        task_id = (data.get("id") or "").strip()
        if not task_id:
            return jsonify({"error": "id is required"}), 400
        if data.get("restore"):
            res = store.restore_task(task_id, actor="hub")
            if "error" in res:
                return jsonify(res), 400
            _pm_audit(resolved, "task", "restore", res["id"])
            return jsonify({"updated": True, "task": res})
        if not (data.get("fields") or data.get("move")):
            return jsonify({"error": "fields or move required"}), 400
        res = store.mutate_task(task_id, fields=data.get("fields"),
                                move=data.get("move"),
                                expected_rev=data.get("expected_rev"),
                                actor="hub")
        if "error" in res:
            return jsonify(res), 409 if res.get("code") == "rev_conflict" else 400
        _pm_audit(resolved, "task", "update", res["id"])
        return jsonify({"updated": True, "task": res})

    # DELETE: archive by id, or purge all archived with {purge: true}
    if data.get("purge"):
        res = store.purge_archived("task")
        _pm_audit(resolved, "task", "purge")
        return jsonify(res)
    task_id = (data.get("id") or "").strip()
    if not task_id:
        return jsonify({"error": "id is required"}), 400
    res = store.archive_task(task_id, actor="hub")
    if "error" in res:
        return jsonify(res), 400
    _pm_audit(resolved, "task", "archive", res["id"])
    return jsonify({"archived": True, "task": res})


@app.route("/api/projects/pm/milestone", methods=["POST", "PUT", "DELETE"])
def api_pm_milestone():
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    store = _pm_store(resolved)

    if request.method == "POST":
        res = store.create_milestone(data.get("name", ""),
                                     description=data.get("description", ""),
                                     target_date=data.get("target_date") or None)
        if "error" in res:
            return jsonify(res), 400
        _pm_audit(resolved, "milestone", "create", res["id"])
        return jsonify({"created": True, "milestone": res}), 201

    ms_id = (data.get("id") or "").strip()
    if not ms_id:
        return jsonify({"error": "id is required"}), 400

    if request.method == "PUT":
        res = store.update_milestone(ms_id, expected_rev=data.get("expected_rev"),
                                     **(data.get("fields") or {}))
        if "error" in res:
            return jsonify(res), 409 if res.get("code") == "rev_conflict" else 400
        _pm_audit(resolved, "milestone", "update", res["id"])
        return jsonify({"updated": True, "milestone": res})

    res = store.archive_milestone(ms_id)  # DELETE = archive + detach tasks
    if "error" in res:
        return jsonify(res), 400
    _pm_audit(resolved, "milestone", "archive", res["id"])
    return jsonify({"archived": True, "milestone": res})


@app.route("/api/projects/pm/note", methods=["POST", "PUT", "DELETE"])
def api_pm_note():
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    store = _pm_store(resolved)

    if request.method == "POST":
        res = store.add_note(data.get("text", ""), kind=data.get("kind") or "note",
                             tags=data.get("tags") or [],
                             task_id=data.get("task_id"), author="hub")
        if "error" in res:
            return jsonify(res), 400
        _pm_audit(resolved, "note", "create", res["id"])
        return jsonify({"created": True, "note": res}), 201

    note_id = (data.get("id") or "").strip()
    if not note_id:
        return jsonify({"error": "id is required"}), 400

    if request.method == "PUT":
        res = store.update_note(note_id, expected_rev=data.get("expected_rev"),
                                **(data.get("fields") or {}))
        if "error" in res:
            return jsonify(res), 409 if res.get("code") == "rev_conflict" else 400
        _pm_audit(resolved, "note", "update", res["id"])
        return jsonify({"updated": True, "note": res})

    res = store.archive_note(note_id)
    if "error" in res:
        return jsonify(res), 400
    _pm_audit(resolved, "note", "archive", res["id"])
    return jsonify({"archived": True, "note": res})


@app.route("/api/projects/pm/link", methods=["POST"])
def api_pm_link():
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    task_id = (data.get("id") or "").strip()
    link = data.get("link") or {}
    op = (data.get("op") or "add").strip()
    if not task_id or not link.get("type") or not link.get("ref"):
        return jsonify({"error": "id and link {type, ref} are required"}), 400
    if op not in ("add", "remove"):
        return jsonify({"error": "op must be add|remove"}), 400
    store = _pm_store(resolved)
    if op == "add":
        res = store.add_link(task_id, link["type"], link["ref"],
                             label=link.get("label", ""))
    else:
        res = store.remove_link(task_id, link["type"], link["ref"])
    if "error" in res:
        return jsonify(res), 400
    _pm_audit(resolved, "link", op, res["id"])
    return jsonify({"task": res})


@app.route("/api/projects/pm/events", methods=["GET"])
def api_pm_events():
    """PM event history for one project, newest first.
    Query: path, entity?, id?, op?, limit?"""
    resolved, err = _pm_resolve((request.args.get("path") or "").strip())
    if err:
        return err
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 500))
    except ValueError:
        limit = 50
    return jsonify({"path": str(resolved), "events": _pm_store(resolved).history(
        entity=(request.args.get("entity") or None),
        item_id=(request.args.get("id") or None),
        op=(request.args.get("op") or None),
        limit=limit)})


@app.route("/api/projects/pm/deps", methods=["POST"])
def api_pm_deps():
    """Add/remove a blocked-by dependency: {path, id, blocker, op: add|remove}."""
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    task_id = (data.get("id") or "").strip()
    blocker = (data.get("blocker") or "").strip()
    op = (data.get("op") or "add").strip()
    if not task_id or not blocker:
        return jsonify({"error": "id and blocker are required"}), 400
    if op not in ("add", "remove"):
        return jsonify({"error": "op must be add|remove"}), 400
    store = _pm_store(resolved)
    res = (store.add_dependency(task_id, blocker, actor="hub") if op == "add"
           else store.remove_dependency(task_id, blocker, actor="hub"))
    if "error" in res:
        return jsonify(res), 400
    _pm_audit(resolved, "deps", op, res["id"])
    return jsonify({"task": res})


@app.route("/api/projects/pm/report", methods=["GET"])
def api_pm_report():
    """PM health report for one project. Query: path"""
    resolved, err = _pm_resolve((request.args.get("path") or "").strip())
    if err:
        return err
    return jsonify({"path": str(resolved),
                    "report": _pm_store(resolved).report()})


def _time_tracker(path):
    from services.time_tracker import TimeTracker
    return TimeTracker(str(path))


@app.route("/api/projects/time", methods=["GET"])
def api_projects_time():
    """Time summary for one project. Query: path"""
    resolved, err = _pm_resolve((request.args.get("path") or "").strip())
    if err:
        return err
    tracker = _time_tracker(resolved)
    return jsonify({"path": str(resolved), "summary": tracker.summary(),
                    "sessions": tracker.sessions()[:20],
                    "entries": tracker.list_entries(limit=100)})


@app.route("/api/projects/time/entry", methods=["POST", "PUT", "DELETE"])
def api_projects_time_entry():
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    tracker = _time_tracker(resolved)
    if request.method == "POST":
        res = tracker.add_entry(data.get("minutes"), note=data.get("note", ""),
                                date=data.get("date") or None,
                                task_id=data.get("task_id"), created_by="hub")
        if "error" in res:
            return jsonify(res), 400
        _pm_audit(resolved, "time", "create", res["id"])
        return jsonify({"created": True, "entry": res}), 201
    entry_id = (data.get("id") or "").strip()
    if not entry_id:
        return jsonify({"error": "id is required"}), 400
    if request.method == "PUT":
        res = tracker.update_entry(entry_id, **(data.get("fields") or {}))
        if "error" in res:
            return jsonify(res), 400
        _pm_audit(resolved, "time", "update", res["id"])
        return jsonify({"updated": True, "entry": res})
    res = tracker.delete_entry(entry_id)
    if "error" in res:
        return jsonify(res), 400
    _pm_audit(resolved, "time", "delete", entry_id)
    return jsonify(res)


# ── Agent artifacts: config tracking (v2.46.0) ────────────────────────────
# Direct ArtifactStore per request (load-per-op store — no runtime build
# needed); every mutation audited to the target project's activity log.

def _artifact_store_for(path):
    from services.artifact_store import ArtifactStore
    return ArtifactStore(str(path))


def _artifact_audit(path, op, ref=""):
    try:
        ActivityLog(str(path)).log("artifact_write", {
            "op": op, "ref": ref, "source": "hub"})
    except Exception:
        pass


@app.route("/api/projects/artifacts", methods=["GET"])
def api_projects_artifacts():
    """Artifact inventory + tracker status. Query: path, cls?, provider?"""
    resolved, err = _pm_resolve((request.args.get("path") or "").strip())
    if err:
        return err
    store = _artifact_store_for(resolved)
    return jsonify({
        "path": str(resolved),
        "artifacts": store.list_artifacts(
            cls=request.args.get("cls", ""),
            provider=request.args.get("provider", "")),
        "status": store.status(),
    })


@app.route("/api/projects/artifacts/history", methods=["GET"])
def api_projects_artifacts_history():
    """History events, newest first. Query: path, artifact?, limit?"""
    resolved, err = _pm_resolve((request.args.get("path") or "").strip())
    if err:
        return err
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 500))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"events": _artifact_store_for(resolved).get_history(
        artifact=request.args.get("artifact", ""), limit=limit)})


@app.route("/api/projects/artifacts/scan", methods=["POST"])
def api_projects_artifacts_scan():
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    store = _artifact_store_for(resolved)
    store.consume_pending()
    res = store.scan()
    res.pop("events", None)  # event dicts live on the history endpoint
    _artifact_audit(resolved, "scan")
    return jsonify(res)


@app.route("/api/projects/artifacts/diff", methods=["POST"])
def api_projects_artifacts_diff():
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    if not data.get("artifact") or not data.get("version"):
        return jsonify({"error": "artifact and version are required"}), 400
    res = _artifact_store_for(resolved).diff(
        data["artifact"], int(data["version"]),
        int(data["against"]) if data.get("against") else None)
    if "error" in res:
        return jsonify(res), 400
    return jsonify(res)


@app.route("/api/projects/artifacts/restore", methods=["POST"])
def api_projects_artifacts_restore():
    data = request.get_json(force=True) or {}
    resolved, err = _pm_resolve((data.get("path") or "").strip())
    if err:
        return err
    if not data.get("artifact") or not data.get("version"):
        return jsonify({"error": "artifact and version are required"}), 400
    try:
        res = _artifact_store_for(resolved).restore(
            data["artifact"], int(data["version"]), session_id="hub")
    except access_guard.AccessDenied as exc:
        return jsonify({"error": exc.message}), 403
    if "error" in res:
        return jsonify(res), 400
    _artifact_audit(resolved, "restore", f"{res['id']}@v{data['version']}")
    return jsonify(res)


@app.route("/api/pm/global", methods=["GET"])
def api_pm_global():
    """Open tasks across every registered project (raw registry — no port probes)."""
    try:
        limit = max(1, min(int(request.args.get("limit") or 500), 1000))
    except ValueError:
        limit = 500
    status_filter = (request.args.get("status") or "").strip()
    from services.task_store import TaskStore

    tasks, skipped, by_project = [], [], {}
    entries = _pm()._read_projects()
    for p in entries:
        ppath = p.get("path") or ""
        if not Path(ppath).is_dir():
            skipped.append({"path": ppath, "reason": "not accessible"})
            continue
        if not (Path(ppath) / ".c3").is_dir():
            skipped.append({"path": ppath, "reason": "not initialized"})
            continue
        try:
            everything = TaskStore(ppath).list_tasks(include_archived=True,
                                                     limit=1000)
        except Exception as e:
            skipped.append({"path": ppath, "reason": str(e)})
            continue
        active = [t for t in everything
                  if t.get("lifecycle", "active") == "active"]
        if status_filter and status_filter != "all":
            rows = [t for t in active if t.get("status") == status_filter]
        elif status_filter == "all":
            rows = active
        else:
            rows = [t for t in active if t.get("status") != "done"]
        if not rows:
            continue
        # Resolve blockers server-side: the aggregate ships only open tasks,
        # so clients cannot tell a done/archived blocker from an open one.
        by_id = {t["id"]: t for t in everything}
        for t in rows:
            deps = t.get("blocked_by") or []
            if not deps:
                continue
            found = [(d, by_id.get(d)) for d in deps]
            t["blockers_open"] = sum(
                1 for _, b in found
                if b is not None and b.get("lifecycle", "active") == "active"
                and b.get("status") != "done")
            t["blocker_titles"] = [
                (b.get("title") or d[:8]) if b is not None else d[:8]
                for d, b in found]
        proj_info = {"name": p.get("name"), "path": ppath}
        if p.get("parent_path"):
            proj_info["parent_path"] = p["parent_path"]
        for t in rows:
            t["project"] = proj_info
        tasks.extend(rows)
        by_project[ppath] = {
            "name": p.get("name"),
            "open": sum(1 for t in rows if t.get("status") != "done"),
            "shown": len(rows),
        }

    # priority asc, due asc, updated desc (stable two-stage sort)
    tasks.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    tasks.sort(key=lambda t: (t.get("priority", "p2"), t.get("due_date") or "9999"))
    capped = len(tasks) > limit
    return jsonify({"projects_scanned": len(entries) - len(skipped),
                    "skipped": skipped, "capped": capped,
                    "tasks": tasks[:limit], "by_project": by_project})


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
    from cli.c3 import PERMISSION_TIERS, _build_permission_tier, _merge_permission_tier
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
    settings["permissions"] = _merge_permission_tier(
        settings.get("permissions") or {}, tier_perms["permissions"]
    )
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
        _shutdown_runtime_cache()
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
        _shutdown_runtime_cache()
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
