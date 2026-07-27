#!/usr/bin/env python3
"""
C3 Web Server — Flask API + embedded UI

Serves both the API endpoints and the single-page React dashboard.
Launch with: c3 ui [--port 3333]
"""
import atexit
import csv
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core import count_tokens
from core.config import load_delegate_config, load_mcp_config, load_proxy_config
from core.ide import PROFILES, detect_ide, get_profile, load_ide_config, normalize_ide_name
from services.protocol import CompressionProtocol
from services.runtime import build_runtime, stop_runtime
from services.session_manager import SessionManager

app = Flask(__name__)

# ─── Globals (set on startup) ────────────────────────────
PROJECT_PATH = None
compressor = None
indexer = None
session_mgr = None
protocol = None
memory_store = None
claude_md_mgr = None
activity_log = None
notification_store = None
vector_store = None
output_filter = None
router = None
metrics_collector = None
hybrid_config = None
watcher = None
file_memory = None
artifact_store = None
ollama_client = None
agents = []
runtime = None

# ─── Global session registry ─────────────────────────────
_GLOBAL_C3_DIR = Path.home() / ".c3"
_REGISTRY_FILE = _GLOBAL_C3_DIR / "registry.json"
_REGISTRY_LOCK = threading.Lock()
_HUB_CONFIG_FILE = _GLOBAL_C3_DIR / "hub_config.json"


def _registry_read() -> list:
    try:
        if _REGISTRY_FILE.exists():
            with open(_REGISTRY_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _registry_write(entries: list):
    _GLOBAL_C3_DIR.mkdir(parents=True, exist_ok=True)
    with open(_REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def _read_hub_config() -> dict:
    cfg = {
        "port": 3330,
        "auto_open_browser": True,
        "theme": "dark",
        "projects_view": "list",
    }
    try:
        if _HUB_CONFIG_FILE.exists():
            with open(_HUB_CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def _port_alive(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except Exception:
        return False


def _register_session(port: int, project_path: str, project_name: str):
    with _REGISTRY_LOCK:
        entries = _registry_read()
        # Remove our own port and any stale (dead) entries
        entries = [e for e in entries if e.get("port") != port and _port_alive(e.get("port", 0))]
        entries.append({
            "port": port,
            "project_path": project_path,
            "project_name": project_name,
            "started_at": time.time(),
        })
        _registry_write(entries)


def _unregister_session(port: int):
    with _REGISTRY_LOCK:
        entries = _registry_read()
        entries = [e for e in entries if e.get("port") != port]
        _registry_write(entries)


def init_services(project_path: str):
    """Initialize all C3 services for a project."""
    global PROJECT_PATH, compressor, indexer, session_mgr, protocol, memory_store, claude_md_mgr, activity_log, notification_store, vector_store, output_filter, router, metrics_collector, hybrid_config, watcher, file_memory, artifact_store, ollama_client, agents, runtime
    runtime = build_runtime(project_path)
    PROJECT_PATH = Path(runtime.project_path)
    protocol = CompressionProtocol(str(PROJECT_PATH), str(PROJECT_PATH / ".c3" / "dictionary.json"))
    compressor = runtime.compressor
    indexer = runtime.indexer
    session_mgr = runtime.session_mgr
    memory_store = runtime.memory
    claude_md_mgr = runtime.claude_md
    activity_log = runtime.activity_log
    notification_store = runtime.notifications
    vector_store = runtime.vector_store
    output_filter = runtime.output_filter
    router = runtime.router
    metrics_collector = runtime.metrics
    hybrid_config = runtime.hybrid_config
    watcher = runtime.watcher
    file_memory = runtime.file_memory
    artifact_store = runtime.artifact_store
    ollama_client = runtime.ollama_client
    agents = runtime.agents

    if watcher:
        watcher.start()
    for agent in agents:
        agent.start()


def _cleanup_runtime():
    """Best-effort shutdown for long-lived background services."""
    global agents, watcher, runtime
    stop_runtime(runtime)
    runtime = None
    agents = []
    watcher = None


atexit.register(_cleanup_runtime)


# ─── CORS middleware ──────────────────────────────────────
# Localhost-only security: Host-header allowlist + Origin/Referer CSRF guard +
# scoped CORS (no wildcard). This UI server always binds 127.0.0.1, so only
# loopback origins are accepted. A loopback bind alone does NOT stop a web page
# in the user's browser from driving these endpoints — see core/web_security.py.
from core.web_security import (
    allowed_hostnames as _allowed_hostnames,
)
from core.web_security import (
    guard_summary as _guard_summary,
)
from core.web_security import (
    install_guard as _install_web_guard,
)

_install_web_guard(app, lambda: _allowed_hostnames(None))


# ─── Serve the UI ─────────────────────────────────────────

# JS load order for concatenated UI build
_UI_JS_FILES = [
    "ui/theme.js",
    "ui/icons.js",
    "ui/api.js",
    "ui/shared.js",
    "ui/pm_shared.js",
    "ui/components/sidebar.js",
    "ui/components/dashboard.js",
    "ui/components/sessions.js",
    "ui/components/memory.js",
    "ui/components/tasks.js",
    "ui/components/edits.js",
    "ui/components/bitbucket.js",
    "ui/components/jira.js",
    "ui/components/credentials.js",
    "ui/components/access.js",
    "ui/components/instructions.js",
    "ui/components/settings.js",
    "ui/components/chat.js",
    "ui/app.js",
]

def _build_ui_html() -> str:
    """Concatenate ui.html shell + all JS component files into a single HTML response."""
    cli_dir = Path(__file__).parent
    shell_path = cli_dir / "ui.html"
    if not shell_path.exists():
        return "<h1>C3 UI not found. Run from the claude-companion directory.</h1>"

    shell = shell_path.read_text(encoding="utf-8")

    # Collect all JS files
    js_parts = []
    for rel in _UI_JS_FILES:
        js_path = cli_dir / rel
        if js_path.exists():
            js_parts.append(f"    // ═══ {rel} ═══\n" + js_path.read_text(encoding="utf-8"))

    combined_js = "\n\n".join(js_parts)

    # Inject into shell placeholder
    return shell.replace("/* __C3_UI_SCRIPTS__ */", combined_js)


# Cache the built HTML (rebuilt on first request; cleared on server restart)
_ui_html_cache: str | None = None

@app.route('/')
def serve_ui():
    global _ui_html_cache
    if _ui_html_cache is None:
        _ui_html_cache = _build_ui_html()
    return Response(_ui_html_cache, mimetype='text/html')


@app.route('/legacy')
def serve_ui_legacy():
    legacy_path = Path(__file__).parent / "ui_legacy.html"
    if legacy_path.exists():
        return send_file(str(legacy_path), mimetype='text/html')
    return "<h1>Legacy UI not found.</h1>", 404


@app.route('/nano')
def serve_ui_nano():
    nano_path = Path(__file__).parent / "ui_nano.html"
    if nano_path.exists():
        return send_file(str(nano_path), mimetype='text/html')
    return "<h1>C3 Nano UI not found. Run from the claude-companion directory.</h1>", 404


@app.route('/docs')
def serve_docs():
    docs_path = Path(__file__).parent / "docs.html"
    if docs_path.exists():
        return send_file(str(docs_path), mimetype='text/html')
    return "<h1>C3 Docs not found.</h1>", 404


@app.route('/edits')
def serve_edits():
    edits_path = Path(__file__).parent / "edits.html"
    if edits_path.exists():
        return send_file(str(edits_path), mimetype='text/html')
    return "<h1>C3 Edit Ledger not found.</h1>", 404


@app.route('/guide/')
@app.route('/guide/<path:filename>')
def serve_guide(filename="index.html"):
    """Serve the bundled in-app guide from the installed package (cli/guide/*)."""
    guide_dir = Path(__file__).parent / "guide"
    if not (guide_dir / filename).is_file():
        return "<h1>C3 guide not found.</h1>", 404
    return send_from_directory(str(guide_dir), filename)


@app.route('/api/hub/info')
def api_hub_info():
    cfg = _read_hub_config()
    port = int(cfg.get("port", 3330) or 3330)
    return jsonify({
        "port": port,
        "url": f"http://localhost:{port}",
    })


# ─── API: Health ─────────────────────────────────────────
@app.route('/api/projects/open', methods=['POST'])
def api_projects_open():
    """Open a project directory in the OS file explorer. Body: {path}"""
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


@app.route('/api/health')
def api_health():
    """Lightweight health check with connection sources."""
    sources = {"c3": True}  # If this endpoint responds, C3 API is up

    # Check Ollama
    try:
        from services.ollama_client import OllamaClient
        base_url = (hybrid_config or {}).get("ollama_base_url", "http://localhost:11434")
        client = OllamaClient(base_url)
        models = client.list_models()
        sources["ollama"] = models is not None
    except Exception:
        sources["ollama"] = False

    # Check proxy connected: config references mcp_proxy.py OR state file is recent
    try:
        import time as _time
        proxy_connected = False

        # Primary: config check — MCP config must reference mcp_proxy.py
        try:
            ide_name = load_ide_config(str(PROJECT_PATH))
            profile = get_profile(ide_name)
            mcp_cfg_path = _mcp_config_path_for_profile(profile)
            if mcp_cfg_path.exists():
                with open(mcp_cfg_path, encoding="utf-8") as f:
                    content = f.read()
                if "mcp_proxy.py" in content:
                    proxy_connected = True
        except Exception:
            pass

        # Secondary: proxy_state.json written within last 4 hours means proxy ran recently
        if not proxy_connected:
            state_file = PROJECT_PATH / ".c3" / "proxy_state.json"
            if state_file.exists():
                try:
                    with open(state_file, encoding="utf-8") as f:
                        pstate = json.load(f)
                    ts = pstate.get("last_updated")
                    if ts and (_time.time() - float(ts)) < 4 * 3600:
                        proxy_connected = True
                except Exception:
                    # Fall back to file mtime
                    age = _time.time() - state_file.stat().st_mtime
                    if age < 4 * 3600:
                        proxy_connected = True

        sources["proxy"] = proxy_connected
        sources["mcp_mode"] = load_mcp_config(str(PROJECT_PATH)).get("mode", "direct")
    except Exception:
        sources["proxy"] = False
        sources["mcp_mode"] = "direct"

    # Check SLTM
    sources["sltm"] = vector_store is not None

    # Minimal session info
    session_info = None
    try:
        current = session_mgr.current_session
        if current:
            session_info = {
                "tool_calls": len(current.get("tool_calls", [])),
                "started": current.get("started"),
            }
    except Exception:
        pass

    return jsonify({"service": "c3-ui", "sources": sources, "session": session_info,
                    "web_guard": _guard_summary()})


# ─── API: Session Registry ───────────────────────────────
@app.route('/api/registry/active')
def api_registry_active():
    """Running UI servers PLUS registered projects with a live agent session
    but no UI (port None) — the project switcher's data. On-demand only:
    get_active_sessions() probes ports, so this must never be polled."""
    try:
        from services.project_manager import ProjectManager
        return jsonify(ProjectManager().get_active_sessions())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/registry/launch', methods=['POST'])
def api_registry_launch():
    """Launch another project's UI server so the switcher can jump to it."""
    data = request.get_json(force=True) or {}
    path = (data.get('path') or '').strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    try:
        from services.project_manager import ProjectManager
        result = ProjectManager().launch_session(path)
        return jsonify(result), (200 if result.get("launched") else 400)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/registry')
def api_registry():
    """Return all live C3 sessions from the global registry."""
    with _REGISTRY_LOCK:
        entries = _registry_read()
    live = [e for e in entries if _port_alive(e.get("port", 0))]
    return jsonify(live)


# ─── API: Stats & Overview ───────────────────────────────
@app.route('/api/stats')
def api_stats():
    """Get comprehensive system stats."""
    idx_stats = indexer.get_stats()
    proto_stats = protocol.get_stats()

    # Calculate compression stats across all indexed files
    skip_dirs = indexer.skip_dirs
    code_exts = indexer.code_exts

    files_data = []
    total_orig = 0
    total_comp = 0

    for fpath in sorted(PROJECT_PATH.rglob('*')):
        if len(files_data) >= 100:
            break
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in code_exts:
            continue
        if any(skip in fpath.parts for skip in skip_dirs):
            continue
        if compressor.is_protected_file(fpath):
            continue
        try:
            content = fpath.read_text(encoding='utf-8', errors='replace')
            orig_tokens = count_tokens(content)
            result = compressor.compress_file(str(fpath), "smart")
            comp_tokens = result.get("compressed_tokens", orig_tokens)

            rel_path = str(fpath.relative_to(PROJECT_PATH))
            files_data.append({
                "name": fpath.name,
                "path": rel_path,
                "lines": len(content.splitlines()),
                "origTokens": orig_tokens,
                "compTokens": comp_tokens,
                "type": fpath.suffix.lstrip('.').lower(),
            })
            total_orig += orig_tokens
            total_comp += comp_tokens
        except Exception:
            continue

    # Session stats
    sessions = session_mgr.list_sessions(20)
    total_decisions = sum(s.get("decisions", 0) for s in sessions)

    savings_pct = round(((total_orig - total_comp) / total_orig * 100), 1) if total_orig > 0 else 0

    # Total lines of code
    total_lines = sum(f["lines"] for f in files_data)

    # Tech stack detection from file extensions
    ext_counts = {}
    for f in files_data:
        ext_counts[f["type"]] = ext_counts.get(f["type"], 0) + 1
    tech_stack = ", ".join(
        ext for ext, _ in sorted(ext_counts.items(), key=lambda x: -x[1])
    ) if ext_counts else "Unknown"

    # Last session date
    last_session = sessions[0].get("started", "") if sessions else None

    # Total tool calls across sessions
    total_tool_calls = sum(s.get("tool_calls", 0) for s in sessions)

    # Claude Code token usage (input/output from Claude's own session logs)
    try:
        claude_tokens = session_mgr.parse_claude_session_tokens()
    except Exception:
        claude_tokens = {"sessions_found": 0, "total_input_tokens": 0, "total_output_tokens": 0}

    # Per-session and per-source token usage from conversation store
    conversation_token_usage = {"sessions": [], "sources": {}, "totals": {"user_tokens": 0, "assistant_tokens": 0, "total_tokens": 0}}
    try:
        conv_store = _get_conv_store()
        conv_sessions = conv_store.list_sessions(limit=1000)
        source_totals = {}
        session_rows = []
        for s in conv_sessions:
            user_tok = int(s.get("user_tokens", 0) or 0)
            asst_tok = int(s.get("assistant_tokens", 0) or 0)
            total_tok = user_tok + asst_tok
            source = (s.get("source") or "manual").strip().lower()
            if not source:
                source = "manual"
            if source not in source_totals:
                source_totals[source] = {"sessions": 0, "user_tokens": 0, "assistant_tokens": 0, "total_tokens": 0}
            source_totals[source]["sessions"] += 1
            source_totals[source]["user_tokens"] += user_tok
            source_totals[source]["assistant_tokens"] += asst_tok
            source_totals[source]["total_tokens"] += total_tok
            conversation_token_usage["totals"]["user_tokens"] += user_tok
            conversation_token_usage["totals"]["assistant_tokens"] += asst_tok
            conversation_token_usage["totals"]["total_tokens"] += total_tok
            session_rows.append({
                "session_id": s.get("session_id", ""),
                "source": source,
                "started": s.get("started"),
                "ended": s.get("ended"),
                "turns": int(s.get("turns", 0) or 0),
                "user_tokens": user_tok,
                "assistant_tokens": asst_tok,
                "total_tokens": total_tok,
            })

        session_rows.sort(key=lambda x: x.get("started", 0), reverse=True)
        conversation_token_usage["sessions"] = session_rows
        conversation_token_usage["sources"] = source_totals
    except Exception:
        pass

    # Context budget from MCP session
    context_budget = None
    budget_file = PROJECT_PATH / ".c3" / "context_budget.json"
    if budget_file.exists():
        try:
            with open(budget_file, encoding='utf-8') as f:
                context_budget = json.load(f)
        except Exception:
            pass

    return jsonify({
        "project_path": str(PROJECT_PATH),
        "index": idx_stats,
        "protocol": proto_stats,
        "files": files_data,
        "total_original_tokens": total_orig,
        "total_compressed_tokens": total_comp,
        "savings_pct": savings_pct,
        "sessions_count": len(sessions),
        "total_decisions": total_decisions,
        "total_tool_calls": total_tool_calls,
        "total_lines": total_lines,
        "tech_stack": tech_stack,
        "last_session": last_session,
        "claude_tokens": claude_tokens,
        "conversation_token_usage": conversation_token_usage,
        "context_budget": context_budget,
    })


# ─── API: Claude Usage ───────────────────────────────────
@app.route('/api/claude_usage', methods=['GET'])
def api_claude_usage():
    """Detailed Claude Code token usage with per-session breakdown and global time-window stats."""
    try:
        data = session_mgr.parse_claude_session_tokens(detailed=True)
    except Exception:
        data = {"sessions_found": 0, "total_input_tokens": 0, "total_output_tokens": 0,
                "cache_creation_tokens": 0, "cache_read_tokens": 0, "sessions": []}
    try:
        # Accept optional query params so the UI can pass the user's configured reset schedule
        wd = request.args.get("weekly_reset_weekday", 4, type=int)   # 4=Friday
        wh = request.args.get("weekly_reset_hour_utc", 22, type=int) # 22=6PM ET
        data["global_windows"] = _compute_global_usage_windows(wd, wh)
    except Exception:
        data["global_windows"] = {}
    return jsonify(data)


@app.route('/api/session-stats', methods=['GET'])
def api_session_stats():
    """Session token/cost stats captured by the Stop hook (.c3/session_stats.jsonl)."""
    limit = request.args.get("limit", 50, type=int)
    try:
        entries = session_mgr.get_session_stats(limit=limit)
    except Exception:
        entries = []
    total_cost = sum(e.get("cost_usd") or 0 for e in entries)
    total_in = sum(e.get("input_tokens") or 0 for e in entries)
    total_out = sum(e.get("output_tokens") or 0 for e in entries)
    total_cache = sum(e.get("cache_read_tokens") or 0 for e in entries)
    return jsonify({
        "sessions": entries,
        "total_sessions": len(entries),
        "total_cost_usd": round(total_cost, 6),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cache_read_tokens": total_cache,
    })


@app.route('/api/session-stats/live', methods=['GET'])
def api_live_session_tokens():
    """Live token counts from the most recently modified Claude Code transcript file.

    Claude Code appends to transcript JSONL after each response, so polling this
    endpoint gives per-turn live visibility into the active session's token usage.
    """
    try:
        data = session_mgr.get_live_session_tokens()
    except Exception:
        data = {}
    return jsonify(data)


def _last_weekly_reset(now, reset_weekday: int, reset_hour: int):
    """Return the most recent occurrence of weekday/hour in UTC.

    reset_weekday: 0=Mon … 6=Sun (default 4=Fri)
    reset_hour:    hour in UTC (default 22 = Fri 6PM US-Eastern / UTC-4)
    """
    from datetime import timedelta
    # Walk back from now to find the previous reset
    candidate = now.replace(minute=0, second=0, microsecond=0, hour=reset_hour)
    for days_back in range(8):
        t = candidate - timedelta(days=days_back)
        if t.weekday() == reset_weekday and t <= now:
            return t
    return now - timedelta(days=7)


def _compute_global_usage_windows(weekly_reset_weekday: int = 4, weekly_reset_hour_utc: int = 22):
    """Scan ALL ~/.claude/projects JSONL files and compute usage windows.

    5-hour rolling window (matching Claude.ai 'Current Session').
    Weekly window since the last fixed reset day/time (matching Claude.ai 'This Week').

    weekly_reset_weekday: 0=Mon … 6=Sun, default 4 (Friday)
    weekly_reset_hour_utc: UTC hour of the weekly reset, default 22 (= 6 PM US Eastern / UTC-4)
    """
    from datetime import datetime, timedelta, timezone

    home = Path.home()
    projects_dir = home / ".claude" / "projects"
    if not projects_dir.exists():
        return {}

    now = datetime.now(timezone.utc)
    window_5h = now - timedelta(hours=5)

    # Weekly window: from the last scheduled reset (fixed day/time), not rolling
    last_reset = _last_weekly_reset(now, weekly_reset_weekday, weekly_reset_hour_utc)
    next_reset = last_reset + timedelta(days=7)
    window_weekly = last_reset

    # Read subscription/plan info from credentials
    cred_file = home / ".claude" / ".credentials.json"
    subscription_type = "unknown"
    try:
        with open(cred_file, encoding='utf-8') as f:
            cred = json.load(f)
        subscription_type = cred.get("claudeAiOauth", {}).get("subscriptionType", "unknown")
    except Exception:
        pass

    # Token-based limits per plan.
    # Derived empirically: Pro 5h ≈ 42M tokens, 7d ≈ 1.43B tokens.
    # These match the % values shown on claude.ai/settings (usage is input+cache+output tokens).
    PLAN_LIMITS = {
        "pro":     {"session_5h_tokens": 42_000_000,  "weekly_tokens": 1_430_000_000},
        "max":     {"session_5h_tokens": 140_000_000, "weekly_tokens": 5_000_000_000},
        "unknown": {"session_5h_tokens": 42_000_000,  "weekly_tokens": 1_430_000_000},
    }
    limits = PLAN_LIMITS.get(subscription_type, PLAN_LIMITS["unknown"])

    # Per-window accumulators
    sess_messages = 0
    sess_tokens = 0          # total tokens (in + out) for % calculation
    sess_input_tokens = 0
    sess_output_tokens = 0
    sess_window_start = None  # earliest assistant entry in current 5h window

    week_messages = 0
    week_tokens = 0
    week_input_tokens = 0
    week_output_tokens = 0
    week_window_start = None

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for session_file in project_dir.glob("*.jsonl"):
            try:
                with open(session_file, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        entry = json.loads(line)
                        ts_str = entry.get("timestamp")
                        if not ts_str:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except Exception:
                            continue

                        # Count assistant turns — each = one API call with usage data
                        if entry.get("type") != "assistant":
                            continue

                        msg = entry.get("message", {})
                        usage = msg.get("usage", {})
                        inp = (usage.get("input_tokens", 0)
                               + usage.get("cache_creation_input_tokens", 0)
                               + usage.get("cache_read_input_tokens", 0))
                        out = usage.get("output_tokens", 0)
                        total_tok = inp + out

                        if ts >= window_weekly:
                            week_messages += 1
                            week_input_tokens += inp
                            week_output_tokens += out
                            week_tokens += total_tok
                            if week_window_start is None or ts < week_window_start:
                                week_window_start = ts

                        if ts >= window_5h:
                            sess_messages += 1
                            sess_input_tokens += inp
                            sess_output_tokens += out
                            sess_tokens += total_tok
                            if sess_window_start is None or ts < sess_window_start:
                                sess_window_start = ts
            except Exception:
                continue

    # Compute reset times (5h rolling from first message, 7d rolling from first message)
    sess_reset_at = None
    sess_resets_in_s = None
    if sess_window_start:
        sess_reset_at = (sess_window_start + timedelta(hours=5)).isoformat()
        sess_resets_in_s = max(0, int((sess_window_start + timedelta(hours=5) - now).total_seconds()))

    # Weekly resets at the fixed schedule (next_reset), regardless of when first message was
    week_reset_at = next_reset.isoformat()
    week_resets_in_s = max(0, int((next_reset - now).total_seconds()))

    # Percentage based on token usage vs plan token limits
    sess_pct = min(100, round(sess_tokens / limits["session_5h_tokens"] * 100)) if limits["session_5h_tokens"] else 0
    week_pct = min(100, round(week_tokens / limits["weekly_tokens"] * 100)) if limits["weekly_tokens"] else 0

    def _fmt_tokens(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(n)

    return {
        "subscription_type": subscription_type,
        "limits": {
            "session_5h_tokens": limits["session_5h_tokens"],
            "weekly_tokens": limits["weekly_tokens"],
        },
        "session_5h": {
            "messages": sess_messages,
            "input_tokens": sess_input_tokens,
            "output_tokens": sess_output_tokens,
            "total_tokens": sess_tokens,
            "total_tokens_fmt": _fmt_tokens(sess_tokens),
            "limit_tokens_fmt": _fmt_tokens(limits["session_5h_tokens"]),
            "pct_used": sess_pct,
            "reset_at": sess_reset_at,
            "resets_in_seconds": sess_resets_in_s,
            "window_start": sess_window_start.isoformat() if sess_window_start else None,
        },
        "weekly": {
            "messages": week_messages,
            "input_tokens": week_input_tokens,
            "output_tokens": week_output_tokens,
            "total_tokens": week_tokens,
            "total_tokens_fmt": _fmt_tokens(week_tokens),
            "limit_tokens_fmt": _fmt_tokens(limits["weekly_tokens"]),
            "pct_used": week_pct,
            "reset_at": week_reset_at,
            "resets_in_seconds": week_resets_in_s,
            "window_start": week_window_start.isoformat() if week_window_start else None,
        },
    }


# ─── API: Compress ───────────────────────────────────────
@app.route('/api/compress', methods=['POST'])
def api_compress():
    """Compress a file and return results."""
    data = request.get_json() or {}
    filepath = data.get("file", "")
    mode = data.get("mode", "smart")

    if not filepath:
        return jsonify({"error": "No file specified"}), 400

    # Resolve relative paths against project
    full_path = (PROJECT_PATH / filepath).resolve()
    if not full_path.exists():
        return jsonify({"error": f"File not found: {filepath}"}), 404
    if compressor.is_protected_file(full_path):
        return jsonify({
            "error": f"Compression is blocked for protected file: {filepath}",
            "protected_files": compressor.get_protected_files(),
        }), 403

    result = compressor.compress_file(str(full_path), mode)
    if "error" in result:
        return jsonify(result), 400

    return jsonify(result)


# ─── API: Batch compress ─────────────────────────────────
@app.route('/api/compress/batch', methods=['POST'])
def api_compress_batch():
    data = request.get_json() or {}
    mode = data.get("mode", "smart")
    result = compressor.compress_directory(str(PROJECT_PATH), mode)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


# ─── API: List files ─────────────────────────────────────
@app.route('/api/compress/protected-files')
def api_compress_protected_files():
    """List files that cannot be compressed."""
    return jsonify({"protected_files": compressor.get_protected_files()})


@app.route('/api/files')
def api_files():
    """List project files available for compression."""
    skip_dirs = indexer.skip_dirs
    code_exts = indexer.code_exts

    files = []
    for fpath in sorted(PROJECT_PATH.rglob('*')):
        if len(files) >= 200:
            break
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in code_exts:
            continue
        if any(skip in fpath.parts for skip in skip_dirs):
            continue
        if compressor.is_protected_file(fpath):
            continue
        try:
            content = fpath.read_text(encoding='utf-8', errors='replace')
            files.append({
                "name": fpath.name,
                "path": str(fpath.relative_to(PROJECT_PATH)),
                "lines": len(content.splitlines()),
                "tokens": count_tokens(content),
                "type": fpath.suffix.lstrip('.').lower(),
            })
        except Exception:
            continue

    return jsonify(files)


# ─── API: Search Index ───────────────────────────────────
@app.route('/api/search', methods=['POST'])
def api_search():
    """Search the code index."""
    data = request.get_json() or {}
    query = data.get("query", "")
    top_k = max(1, min(int(data.get("top_k", 3)), 10))
    max_tokens = max(200, int(data.get("max_tokens", 1200)))

    if not query:
        return jsonify({"error": "No query specified"}), 400

    results = indexer.search(query, top_k=top_k, max_tokens=max_tokens)
    context = indexer.get_context(query, top_k=top_k, max_tokens=max_tokens)
    context_tokens = count_tokens(context)

    return jsonify({
        "results": results,
        "context": context,
        "context_tokens": context_tokens,
        "query": query,
    })


# ─── API: Rebuild Index ──────────────────────────────────
@app.route('/api/index/rebuild', methods=['POST'])
def api_rebuild_index():
    """Rebuild the code index."""
    result = indexer.build_index()
    return jsonify(result)


@app.route('/api/index/stats')
def api_index_stats():
    """Get index statistics."""
    return jsonify(indexer.get_stats())


# ─── API: Encode / Decode ────────────────────────────────
@app.route('/api/encode', methods=['POST'])
def api_encode():
    """Encode text using compression protocol."""
    data = request.get_json() or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "No text specified"}), 400
    result = protocol.encode(text)
    return jsonify(result)


@app.route('/api/decode', methods=['POST'])
def api_decode():
    """Decode compressed text."""
    data = request.get_json() or {}
    text = data.get("text", "")
    if not text:
        return jsonify({"error": "No text specified"}), 400
    decoded = protocol.decode(text)
    return jsonify({"decoded": decoded, "original": text})


@app.route('/api/protocol/header')
def api_protocol_header():
    """Get the protocol header for system prompts."""
    return jsonify({"header": protocol.get_protocol_header()})


@app.route('/api/protocol/dictionary')
def api_protocol_dictionary():
    """Get the full compression dictionary."""
    from services.protocol import ACTION_CODES, TERM_CODES
    return jsonify({
        "actions": {k: v for k, v in ACTION_CODES.items() if v},
        "terms": TERM_CODES,
        "custom": protocol.custom_dict,
    })


@app.route('/api/protocol/build-dictionary', methods=['POST'])
def api_build_dictionary():
    """Build project-specific dictionary."""
    new_terms = protocol.build_project_dictionary()
    return jsonify({"new_terms": new_terms, "total": len(protocol.custom_dict)})


# ─── API: Sessions ───────────────────────────────────────
@app.route('/api/sessions')
def api_sessions():
    """List all sessions, backfilling tool call counts from activity log."""
    sessions = session_mgr.list_sessions(50)
    for s in sessions:
        if s.get("tool_calls", 0) == 0 and s.get("started"):
            count = len(activity_log.get_recent(
                limit=500, event_type="tool_call",
                since=s["started"], until=s.get("ended") or None,
            ))
            s["tool_calls"] = count
    return jsonify(sessions)


@app.route('/api/sessions/current')
def api_session_current():
    """Get the currently running MCP session by reading the activity log.

    Finds the most recent session_start event, then gathers all activity
    since that timestamp to construct a live current-session view.
    """
    # Find the most recent session_start event
    starts = activity_log.get_recent(limit=1, event_type="session_start")
    if not starts:
        return jsonify(None)

    start_event = starts[0]
    session_id = start_event.get("session_id", "")
    started = start_event.get("timestamp", "")
    description = start_event.get("description", "")
    current = session_mgr.current_session or {}
    source_system = start_event.get("source_system", "") or current.get("source_system", "")
    source_ide = start_event.get("source_ide", "") or current.get("source_ide", "")

    # Check if this session was already saved (ended)
    saves = activity_log.get_recent(limit=1, event_type="session_save")
    if saves and saves[0].get("session_id") == session_id:
        # Session is saved/ended — load from disk instead
        saved = session_mgr.load_session(session_id)
        if saved:
            return jsonify(saved)

    # Session is still running — build live view from activity log
    events = activity_log.get_recent(limit=500, since=started)
    events.reverse()  # chronological order

    tool_calls = []
    decisions = []
    files_touched = []
    for ev in events:
        t = ev.get("type")
        if t == "tool_call":
            tool_calls.append({
                "tool": ev.get("tool", "unknown"),
                "args": ev.get("args", {}),
                "result_summary": ev.get("result_summary", ""),
                "timestamp": ev.get("timestamp", ""),
            })
        elif t == "decision":
            decisions.append({
                "decision": ev.get("data", ev.get("decision", "")),
                "reasoning": ev.get("reasoning", ""),
                "timestamp": ev.get("timestamp", ""),
            })
        elif t == "file_change":
            files_touched.append({
                "file": ev.get("file", ev.get("data", "")),
                "action": ev.get("action", "modified"),
                "timestamp": ev.get("timestamp", ""),
            })

    now = datetime.now(timezone.utc)
    started_dt = datetime.fromisoformat(started)
    duration_seconds = int((now - started_dt).total_seconds())
    hours, remainder = divmod(duration_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    duration = f"{hours}h {minutes}m" if hours else f"{minutes}m {secs}s"

    return jsonify({
        "id": session_id,
        "started": started,
        "ended": None,
        "description": description,
        "source_system": source_system,
        "source_ide": source_ide,
        "duration_seconds": duration_seconds,
        "duration": duration,
        "tool_calls": tool_calls,
        "decisions": decisions,
        "files_touched": files_touched,
        "context_notes": [],
        "live": True,
    })


@app.route('/api/sessions/<session_id>')
def api_session_detail(session_id):
    """Get session details."""
    session = session_mgr.load_session(session_id)
    return jsonify(session)


@app.route('/api/sessions/start', methods=['POST'])
def api_session_start():
    """Start a new session."""
    data = request.get_json(silent=True) or {}
    desc = data.get("description", "")
    source_system = data.get("source_system")
    result = session_mgr.start_session(desc, source_system=source_system)
    activity_log.log("session_start", {
        "session_id": result["session_id"],
        "description": desc,
        "source_system": result.get("source_system", ""),
        "source_ide": result.get("source_ide", ""),
    })
    return jsonify(result)


@app.route('/api/sessions/save', methods=['POST'])
def api_session_save():
    """Save current session."""
    data = request.get_json() or {}
    summary = data.get("summary", "")
    result = session_mgr.save_session(summary)
    return jsonify(result)


@app.route('/api/auto-snapshot', methods=['POST'])
def api_auto_snapshot():
    """Auto-snapshot triggered by the Stop hook on session end / Ctrl+C."""
    try:
        data = request.get_json() or {}
        # Flush auto-memory before capturing
        if hasattr(runtime, "auto_memory") and runtime.auto_memory:
            try:
                runtime.auto_memory.on_session_end()
            except Exception:
                pass
        # Capture snapshot with whatever session state is live
        snap_compressor = getattr(runtime, "compressor", None)
        result = runtime.snapshots.capture(
            session_mgr,
            memory_store,
            task_description=data.get("task_description", "auto-snapshot on stop"),
            compressor=snap_compressor,
        )
        session_mgr.mark_auto_snapshot_fired()
        return jsonify({"ok": True, "snapshot_id": result["snapshot_id"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route('/api/sessions/context')
def api_session_context():
    """Get compressed context from recent sessions."""
    n = request.args.get('n', 3, type=int)
    context = session_mgr.get_session_context(n_sessions=n)
    return jsonify({"context": context, "tokens": count_tokens(context)})


# ─── API: CLAUDE.md ──────────────────────────────────────
@app.route('/api/claudemd')
def api_claudemd():
    """Get generated CLAUDE.md content."""
    content = session_mgr.generate_claude_md()
    return jsonify({"content": content, "tokens": count_tokens(content)})


@app.route('/api/claudemd/save', methods=['POST'])
def api_claudemd_save():
    """Generate and save instructions file to project root using ClaudeMdManager."""
    gen = claude_md_mgr.generate(include_sessions=True)
    content = gen.get("content", "")
    if not content:
        return jsonify({"error": "Generation produced empty content"}), 500

    output_path = PROJECT_PATH / claude_md_mgr.instructions_file

    # Wrap in the C3 managed block; never clobber user content outside it.
    from services.claude_md import write_c3_instruction_doc
    write_c3_instruction_doc(output_path, content, project_path=PROJECT_PATH)

    return jsonify({
        "path": str(output_path),
        "tokens": gen.get("tokens", 0),
        "lines": gen.get("lines", 0),
        "status": "saved",
    })


@app.route('/api/claudemd/check')
def api_claudemd_check():
    """Check CLAUDE.md for staleness and drift."""
    result = claude_md_mgr.check_staleness()
    return jsonify(result)


@app.route('/api/claudemd/compact', methods=['POST'])
def api_claudemd_compact():
    """Compact CLAUDE.md to target line count."""
    data = request.get_json(silent=True) or {}
    target_lines = data.get('target_lines', 150)
    result = claude_md_mgr.compact(target_lines=target_lines)
    return jsonify(result)


@app.route('/api/claudemd/promote')
def api_claudemd_promote():
    """Get promotion candidates for CLAUDE.md."""
    min_relevance = request.args.get('min_relevance', 2, type=int)
    result = claude_md_mgr.get_promotion_candidates(min_relevance=min_relevance)
    return jsonify(result)


# ─── API: Optimize ───────────────────────────────────────
@app.route('/api/optimize')
def api_optimize():
    """Get optimization suggestions."""
    suggestions = session_mgr.get_optimization_suggestions()
    return jsonify({"suggestions": suggestions})


# ─── API: Project management (tasks/milestones/notes) ────
def _task_store():
    store = getattr(runtime, "task_store", None)
    if store is None:
        from services.task_store import TaskStore
        store = TaskStore(str(PROJECT_PATH))
    return store


def _pm_audit(entity, op, item_id=""):
    try:
        activity_log.log("pm_write", {"entity": entity, "op": op,
                                      "id": item_id, "source": "ui"})
    except Exception:
        pass


@app.route('/api/pm', methods=["GET"])
def api_pm_get():
    """PM board for this project. Query: milestone?, tag?, include_archived?"""
    store = _task_store()
    board = store.board(
        milestone_id=(request.args.get("milestone") or None),
        tag=(request.args.get("tag") or None),
        include_archived=request.args.get("include_archived") == "1",
    )
    return jsonify({"board": board, "notes": store.list_notes(limit=100)})


@app.route('/api/pm/task', methods=["POST", "PUT", "DELETE"])
def api_pm_task():
    data = request.get_json(force=True) or {}
    store = _task_store()
    if request.method == "POST":
        res = store.create_task(
            data.get("title", ""), description=data.get("description", ""),
            status=data.get("status") or "backlog",
            priority=data.get("priority") or "p2",
            due_date=data.get("due_date") or None,
            tags=data.get("tags") or [], milestone_id=data.get("milestone_id"),
            links=data.get("links") or [], created_by="ui")
        if "error" in res:
            return jsonify(res), 400
        _pm_audit("task", "create", res["id"])
        return jsonify({"created": True, "task": res}), 201
    if request.method == "PUT":
        task_id = (data.get("id") or "").strip()
        if not task_id:
            return jsonify({"error": "id is required"}), 400
        if data.get("restore"):
            res = store.restore_task(task_id, actor="ui")
            if "error" in res:
                return jsonify(res), 400
            _pm_audit("task", "restore", res["id"])
            return jsonify({"updated": True, "task": res})
        if not (data.get("fields") or data.get("move")):
            return jsonify({"error": "fields or move required"}), 400
        res = store.mutate_task(task_id, fields=data.get("fields"),
                                move=data.get("move"),
                                expected_rev=data.get("expected_rev"),
                                actor="ui")
        if "error" in res:
            return jsonify(res), 409 if res.get("code") == "rev_conflict" else 400
        _pm_audit("task", "update", res["id"])
        return jsonify({"updated": True, "task": res})
    if data.get("purge"):
        res = store.purge_archived("task")
        _pm_audit("task", "purge")
        return jsonify(res)
    task_id = (data.get("id") or "").strip()
    if not task_id:
        return jsonify({"error": "id is required"}), 400
    res = store.archive_task(task_id, actor="ui")
    if "error" in res:
        return jsonify(res), 400
    _pm_audit("task", "archive", res["id"])
    return jsonify({"archived": True, "task": res})


@app.route('/api/pm/milestone', methods=["POST", "PUT", "DELETE"])
def api_pm_milestone():
    data = request.get_json(force=True) or {}
    store = _task_store()
    if request.method == "POST":
        res = store.create_milestone(data.get("name", ""),
                                     description=data.get("description", ""),
                                     target_date=data.get("target_date") or None)
        if "error" in res:
            return jsonify(res), 400
        _pm_audit("milestone", "create", res["id"])
        return jsonify({"created": True, "milestone": res}), 201
    ms_id = (data.get("id") or "").strip()
    if not ms_id:
        return jsonify({"error": "id is required"}), 400
    if request.method == "PUT":
        res = store.update_milestone(ms_id, expected_rev=data.get("expected_rev"),
                                     **(data.get("fields") or {}))
        if "error" in res:
            return jsonify(res), 409 if res.get("code") == "rev_conflict" else 400
        _pm_audit("milestone", "update", res["id"])
        return jsonify({"updated": True, "milestone": res})
    res = store.archive_milestone(ms_id)
    if "error" in res:
        return jsonify(res), 400
    _pm_audit("milestone", "archive", res["id"])
    return jsonify({"archived": True, "milestone": res})


@app.route('/api/pm/note', methods=["POST", "PUT", "DELETE"])
def api_pm_note():
    data = request.get_json(force=True) or {}
    store = _task_store()
    if request.method == "POST":
        res = store.add_note(data.get("text", ""), kind=data.get("kind") or "note",
                             tags=data.get("tags") or [],
                             task_id=data.get("task_id"), author="ui")
        if "error" in res:
            return jsonify(res), 400
        _pm_audit("note", "create", res["id"])
        return jsonify({"created": True, "note": res}), 201
    note_id = (data.get("id") or "").strip()
    if not note_id:
        return jsonify({"error": "id is required"}), 400
    if request.method == "PUT":
        res = store.update_note(note_id, expected_rev=data.get("expected_rev"),
                                **(data.get("fields") or {}))
        if "error" in res:
            return jsonify(res), 409 if res.get("code") == "rev_conflict" else 400
        _pm_audit("note", "update", res["id"])
        return jsonify({"updated": True, "note": res})
    res = store.archive_note(note_id)
    if "error" in res:
        return jsonify(res), 400
    _pm_audit("note", "archive", res["id"])
    return jsonify({"archived": True, "note": res})


@app.route('/api/pm/link', methods=["POST"])
def api_pm_link():
    data = request.get_json(force=True) or {}
    task_id = (data.get("id") or "").strip()
    link = data.get("link") or {}
    op = (data.get("op") or "add").strip()
    if not task_id or not link.get("type") or not link.get("ref"):
        return jsonify({"error": "id and link {type, ref} are required"}), 400
    if op not in ("add", "remove"):
        return jsonify({"error": "op must be add|remove"}), 400
    store = _task_store()
    if op == "add":
        res = store.add_link(task_id, link["type"], link["ref"],
                             label=link.get("label", ""))
    else:
        res = store.remove_link(task_id, link["type"], link["ref"])
    if "error" in res:
        return jsonify(res), 400
    _pm_audit("link", op, res["id"])
    return jsonify({"task": res})


@app.route('/api/pm/events', methods=["GET"])
def api_pm_events():
    """PM event history, newest first. Query: entity?, id?, op?, limit?"""
    try:
        limit = max(1, min(int(request.args.get("limit") or 50), 500))
    except ValueError:
        limit = 50
    return jsonify({"events": _task_store().history(
        entity=(request.args.get("entity") or None),
        item_id=(request.args.get("id") or None),
        op=(request.args.get("op") or None),
        limit=limit)})


@app.route('/api/pm/deps', methods=["POST"])
def api_pm_deps():
    """Add/remove a blocked-by dependency: {id, blocker, op: add|remove}."""
    data = request.get_json(force=True) or {}
    task_id = (data.get("id") or "").strip()
    blocker = (data.get("blocker") or "").strip()
    op = (data.get("op") or "add").strip()
    if not task_id or not blocker:
        return jsonify({"error": "id and blocker are required"}), 400
    if op not in ("add", "remove"):
        return jsonify({"error": "op must be add|remove"}), 400
    store = _task_store()
    res = (store.add_dependency(task_id, blocker, actor="ui") if op == "add"
           else store.remove_dependency(task_id, blocker, actor="ui"))
    if "error" in res:
        return jsonify(res), 400
    _pm_audit("deps", op, res["id"])
    return jsonify({"task": res})


@app.route('/api/pm/report', methods=["GET"])
def api_pm_report():
    """PM health report: overdue, blocked chains, ready, milestones, throughput."""
    return jsonify(_task_store().report())


# ─── API: Time tracking ──────────────────────────────────
def _time_tracker():
    tracker = getattr(runtime, "time_tracker", None)
    if tracker is None:
        from services.time_tracker import TimeTracker
        tracker = TimeTracker(str(PROJECT_PATH))
    return tracker


@app.route('/api/time', methods=["GET"])
def api_time_get():
    """Time summary + recent auto sessions + manual entries."""
    tracker = _time_tracker()
    return jsonify({"summary": tracker.summary(),
                    "sessions": tracker.sessions()[:20],
                    "entries": tracker.list_entries(limit=100)})


@app.route('/api/time/entry', methods=["POST", "PUT", "DELETE"])
def api_time_entry():
    data = request.get_json(force=True) or {}
    tracker = _time_tracker()
    if request.method == "POST":
        res = tracker.add_entry(data.get("minutes"), note=data.get("note", ""),
                                date=data.get("date") or None,
                                task_id=data.get("task_id"), created_by="ui")
        if "error" in res:
            return jsonify(res), 400
        _pm_audit("time", "create", res["id"])
        return jsonify({"created": True, "entry": res}), 201
    entry_id = (data.get("id") or "").strip()
    if not entry_id:
        return jsonify({"error": "id is required"}), 400
    if request.method == "PUT":
        res = tracker.update_entry(entry_id, **(data.get("fields") or {}))
        if "error" in res:
            return jsonify(res), 400
        _pm_audit("time", "update", res["id"])
        return jsonify({"updated": True, "entry": res})
    res = tracker.delete_entry(entry_id)
    if "error" in res:
        return jsonify(res), 400
    _pm_audit("time", "delete", entry_id)
    return jsonify(res)


# ─── API: Agent artifacts (config tracking) ──────────────
def _artifact_store():
    store = artifact_store or getattr(runtime, "artifact_store", None)
    if store is None:
        from services.artifact_store import ArtifactStore
        store = ArtifactStore(str(PROJECT_PATH))
    return store


def _artifact_audit(op, ref=""):
    try:
        activity_log.log("artifact_write", {"op": op, "ref": ref, "source": "ui"})
    except Exception:
        pass


@app.route('/api/artifacts', methods=["GET"])
def api_artifacts():
    """Artifact inventory + tracker status. Query: cls?, provider?"""
    store = _artifact_store()
    return jsonify({
        "artifacts": store.list_artifacts(
            cls=request.args.get("cls", ""),
            provider=request.args.get("provider", "")),
        "status": store.status(),
    })


@app.route('/api/artifacts/history', methods=["GET"])
def api_artifacts_history():
    """History events, newest first. Query: artifact?, limit?"""
    store = _artifact_store()
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"events": store.get_history(
        artifact=request.args.get("artifact", ""), limit=limit)})


@app.route('/api/artifacts/scan', methods=["POST"])
def api_artifacts_scan():
    store = _artifact_store()
    store.consume_pending()
    res = store.scan()
    res.pop("events", None)  # event dicts are for the history endpoint
    _artifact_audit("scan")
    return jsonify(res)


@app.route('/api/artifacts/diff', methods=["POST"])
def api_artifacts_diff():
    data = request.get_json(force=True) or {}
    if not data.get("artifact") or not data.get("version"):
        return jsonify({"error": "artifact and version are required"}), 400
    store = _artifact_store()
    res = store.diff(data["artifact"], int(data["version"]),
                     int(data["against"]) if data.get("against") else None)
    if "error" in res:
        return jsonify(res), 400
    return jsonify(res)


@app.route('/api/artifacts/restore', methods=["POST"])
def api_artifacts_restore():
    data = request.get_json(force=True) or {}
    if not data.get("artifact") or not data.get("version"):
        return jsonify({"error": "artifact and version are required"}), 400
    store = _artifact_store()
    res = store.restore(data["artifact"], int(data["version"]), session_id="ui")
    if "error" in res:
        return jsonify(res), 400
    ledger = getattr(runtime, "edit_ledger", None)
    if ledger is not None:
        try:
            for path in res["files_written"]:
                ledger.log_edit(path, "restored",
                                f"restored {res['id']} to v{data['version']} via UI",
                                tags=["artifact_restore"], session_id="ui")
        except Exception:
            pass
    _artifact_audit("restore", f"{res['id']}@v{data['version']}")
    return jsonify(res)


# ─── API: Memory ─────────────────────────────────────────
@app.route('/api/memory/facts')
def api_memory_facts():
    """List all stored facts."""
    return jsonify(memory_store.facts)


def _sync_memory_md():
    """Rewrite .c3/MEMORY.md as a markdown index of active facts, grouped by category."""
    try:
        facts = [f for f in memory_store.facts if f.get("lifecycle") != "archived"]
        facts.sort(key=lambda f: (f.get("relevance_count", 0), f.get("last_accessed_at") or ""), reverse=True)
        by_cat: dict = {}
        for f in facts:
            by_cat.setdefault(f.get("category", "general"), []).append(f)
        now = datetime.now(timezone.utc).isoformat()
        lines = [
            "# C3 Memory Index",
            "",
            f"_Auto-generated {now} — {len(facts)} active facts. Do not edit by hand._",
            "",
        ]
        for cat in sorted(by_cat):
            lines.append(f"## {cat}")
            lines.append("")
            for e in by_cat[cat]:
                fid = (e.get("id") or "")[:8]
                recalls = e.get("relevance_count", 0)
                lines.append(f"- `{fid}` (recalls={recalls}) — {e['fact']}")
            lines.append("")
        out = PROJECT_PATH / ".c3" / "MEMORY.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except Exception:
        pass


@app.route('/api/memory/remember', methods=['POST'])
def api_memory_remember():
    """Store a new fact."""
    data = request.get_json() or {}
    fact = data.get("fact", "").strip()
    category = data.get("category", "general")
    if not fact:
        return jsonify({"error": "No fact specified"}), 400
    session_id = (session_mgr.current_session or {}).get("id", "")
    result = memory_store.remember(fact, category, source_session=session_id)
    _sync_memory_md()
    return jsonify(result)


@app.route('/api/memory/recall', methods=['POST'])
def api_memory_recall():
    """Search facts by query."""
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    top_k = max(1, min(int(data.get("top_k", 3)), 10))
    if not query:
        return jsonify({"error": "No query specified"}), 400
    results = memory_store.recall(query, top_k)
    return jsonify(results)


@app.route('/api/memory/query', methods=['POST'])
def api_memory_query():
    """Search facts + sessions."""
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    top_k = data.get("top_k", 5)
    if not query:
        return jsonify({"error": "No query specified"}), 400
    results = memory_store.query_all(query, top_k)
    return jsonify(results)


@app.route('/api/memory/facts/<fact_id>', methods=['DELETE'])
def api_memory_delete(fact_id):
    """Delete a fact by ID."""
    result = memory_store.delete_fact(fact_id)
    if "error" in result:
        return jsonify(result), 404
    _sync_memory_md()
    return jsonify(result)


@app.route('/api/memory/export')
def api_memory_export():
    """Export all active facts as markdown, grouped by category."""
    category = request.args.get("category", "")
    facts = [f for f in memory_store.facts if f.get("lifecycle") != "archived"]
    if category:
        facts = [f for f in facts if f.get("category") == category]
    facts.sort(key=lambda f: (f.get("relevance_count", 0), f.get("last_accessed_at") or ""), reverse=True)
    by_cat = {}
    for f in facts:
        by_cat.setdefault(f.get("category", "general"), []).append(f)
    lines = ["# C3 Memory Export", ""]
    for cat, entries in sorted(by_cat.items()):
        lines.append(f"## {cat}")
        lines.append("")
        for e in entries:
            lines.append(f"- {e['fact']}")
        lines.append("")
    return jsonify({"markdown": "\n".join(lines).rstrip() + "\n", "count": len(facts)})


@app.route('/api/memory/graph')
def api_memory_graph():
    """Return memory graph: nodes (facts + optional file/symbol nodes) + edges + clusters."""
    graph = getattr(runtime, "memory_graph", None)
    if graph is None:
        return jsonify({"nodes": [], "edges": [], "clusters": [], "stats": {}})

    include_non_fact = request.args.get("include_non_fact", "0") == "1"
    min_weight = float(request.args.get("min_weight", 0) or 0)

    facts_by_id = {f["id"]: f for f in memory_store.facts if f.get("lifecycle") != "archived"}
    edges = [
        {"src": e["src"], "dst": e["dst"], "type": e["type"], "weight": e.get("weight", 1.0)}
        for e in graph._edges
        if e.get("weight", 1.0) >= min_weight
    ]

    node_ids: set[str] = set()
    for e in edges:
        node_ids.add(e["src"])
        node_ids.add(e["dst"])

    nodes = []
    for nid in node_ids:
        if nid in facts_by_id:
            f = facts_by_id[nid]
            nodes.append({
                "id": nid,
                "kind": "fact",
                "label": (f.get("fact", "")[:80]),
                "category": f.get("category", "general"),
                "relevance": f.get("relevance_count", 0),
                "confidence": f.get("confidence", 1.0),
                "last_accessed": f.get("last_accessed_at", ""),
            })
        elif include_non_fact:
            kind = "file" if nid.startswith("file:") else ("symbol" if nid.startswith("symbol:") else "other")
            nodes.append({"id": nid, "kind": kind, "label": nid.split(":", 1)[-1]})

    if not include_non_fact:
        keep = {n["id"] for n in nodes}
        edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]

    clusters = graph.detect_clusters()
    return jsonify({
        "nodes": nodes,
        "edges": edges,
        "clusters": clusters,
        "stats": graph.stats(),
    })


@app.route('/api/memory/fact/<fact_id>')
def api_memory_fact_detail(fact_id):
    """Return a single fact plus its graph neighbors."""
    fact = next((f for f in memory_store.facts if f.get("id") == fact_id), None)
    if not fact:
        return jsonify({"error": "not found"}), 404
    graph = getattr(runtime, "memory_graph", None)
    neighbors = []
    if graph is not None:
        for e in graph.get_edges(fact_id):
            other = e["dst"] if e["src"] == fact_id else e["src"]
            other_fact = next((f for f in memory_store.facts if f.get("id") == other), None)
            neighbors.append({
                "id": other,
                "type": e["type"],
                "weight": e.get("weight", 1.0),
                "label": (other_fact.get("fact", "")[:80] if other_fact else other),
                "kind": "fact" if other_fact else ("file" if other.startswith("file:") else "other"),
            })
    return jsonify({"fact": fact, "neighbors": neighbors})


@app.route('/api/memory/facts/<fact_id>', methods=['PATCH'])
def api_memory_update(fact_id):
    """Update a fact's text/category."""
    data = request.get_json() or {}
    result = memory_store.update_fact(
        fact_id,
        fact=data.get("fact", ""),
        category=data.get("category", ""),
    )
    if "error" in result:
        return jsonify(result), 404
    _sync_memory_md()
    return jsonify(result)


def _get_consolidator():
    from services.memory_consolidator import MemoryConsolidator
    return MemoryConsolidator(
        memory_store=memory_store,
        graph=getattr(runtime, "memory_graph", None),
        project_path=str(PROJECT_PATH),
    )


@app.route('/api/memory/trends')
def api_memory_trends():
    """Recall frequency / category trends."""
    try:
        return jsonify(_get_consolidator().detect_trends())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/memory/lifespan')
def api_memory_lifespan():
    """Fact age / staleness distribution."""
    try:
        return jsonify(_get_consolidator().fact_lifespan_analysis())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/memory/consolidate', methods=['POST'])
def api_memory_consolidate():
    """Trigger consolidation pipeline."""
    try:
        session = (session_mgr.current_session if session_mgr else None)
        result = _get_consolidator().run(session)
        _sync_memory_md()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/memory/ground/<fact_id>', methods=['POST'])
def api_memory_ground(fact_id):
    """Verify a fact against current code via MemoryGrounder."""
    from services.memory_grounder import MemoryGrounder
    fact = next((f for f in memory_store.facts if f.get("id") == fact_id), None)
    if not fact:
        return jsonify({"error": "not found"}), 404
    try:
        grounder = MemoryGrounder(str(PROJECT_PATH), memory_store)
        result = grounder.ground_fact(fact)
        return jsonify(result.to_dict() if hasattr(result, "to_dict") else result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── API: Watcher (read-only from sessions) ──────────────
@app.route('/api/watcher/changes')
def api_watcher_changes():
    """Return recent file changes from session tool_calls."""
    sessions = session_mgr.list_sessions(5)
    changes = []
    session_dir = PROJECT_PATH / ".c3" / "sessions"
    for s in sessions[:5]:
        sf = session_dir / f"session_{s['id']}.json"
        if sf.exists():
            try:
                with open(sf, encoding='utf-8') as f:
                    full = json.load(f)
                for tc in full.get("tool_calls", []):
                    tool = tc.get("tool", "")
                    if any(kw in tool.lower() for kw in ["write", "edit", "read", "file", "create"]):
                        changes.append({
                            "tool": tool,
                            "args_summary": str(tc.get("args", {}))[:120],
                            "timestamp": tc.get("timestamp", ""),
                            "session_id": s["id"],
                        })
            except Exception:
                continue
    return jsonify(changes[:50])


# ─── API: Project Meta ────────────────────────────────────
def _load_project_meta():
    """Load editable project metadata from .c3/config.json."""
    config_path = PROJECT_PATH / ".c3" / "config.json"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                return json.load(f).get("meta", {})
        except Exception:
            pass
    return {}


def _save_project_meta(meta: dict):
    """Save editable project metadata to .c3/config.json."""
    config_path = PROJECT_PATH / ".c3" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    config["meta"] = meta
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


@app.route('/api/project/meta')
def api_project_meta_get():
    """Get editable project metadata."""
    return jsonify(_load_project_meta())


@app.route('/api/project/meta', methods=['PUT'])
def api_project_meta_put():
    """Update editable project metadata."""
    data = request.get_json() or {}
    meta = _load_project_meta()
    meta.update(data)
    _save_project_meta(meta)
    return jsonify(meta)


# ─── API: Project Data Management ────────────────────────
@app.route('/api/data/summary')
def api_data_summary():
    """Return size and item counts for each .c3/ data category."""
    c3 = PROJECT_PATH / ".c3"

    def _dir_stats(path, glob="*", skip_prefix="_"):
        if not path.exists():
            return {"count": 0, "size_kb": 0.0}
        files = [f for f in path.glob(glob) if f.is_file() and not f.name.startswith(skip_prefix)]
        size = sum(f.stat().st_size for f in files)
        return {"count": len(files), "size_kb": round(size / 1024, 1)}

    # Index
    idx = _dir_stats(c3 / "index")
    idx_files = 0
    try:
        if indexer:
            idx_files = indexer._index.get("metadata", {}).get("files_indexed", 0) if indexer._index else 0
    except Exception:
        pass
    idx["files_indexed"] = idx_files

    # Sessions
    sess_files = list((c3 / "sessions").glob("session_*.json")) if (c3 / "sessions").exists() else []
    sess_size = sum(f.stat().st_size for f in sess_files)

    # Cache
    cache = _dir_stats(c3 / "cache")

    # Snapshots
    snap_files = list((c3 / "snapshots").glob("snap_*.json")) if (c3 / "snapshots").exists() else []
    snap_size = sum(f.stat().st_size for f in snap_files)

    # File memory maps
    fm = _dir_stats(c3 / "file_memory", "*.json")

    # Notifications
    notif_path = c3 / "notifications.jsonl"
    notif_count = 0
    notif_size = 0
    if notif_path.exists():
        try:
            lines = notif_path.read_text(encoding="utf-8").strip().splitlines()
            notif_count = len([l for l in lines if l.strip()])
            notif_size = notif_path.stat().st_size
        except Exception:
            pass

    # SLTM
    sltm_records = 0
    sltm_size = 0
    if vector_store:
        try:
            vstats = vector_store.get_stats()
            sltm_records = sum(c.get("count", 0) for c in vstats.get("collections", {}).values())
        except Exception:
            pass
    sltm_dir = c3 / "sltm"
    if sltm_dir.exists():
        for f in sltm_dir.rglob("*"):
            if f.is_file():
                try:
                    sltm_size += f.stat().st_size
                except Exception:
                    pass

    # Total .c3/ size
    total_bytes = 0
    if c3.exists():
        for f in c3.rglob("*"):
            if f.is_file():
                try:
                    total_bytes += f.stat().st_size
                except Exception:
                    pass

    return jsonify({
        "index":         {"count": idx_files, "size_kb": idx["size_kb"]},
        "sessions":      {"count": len(sess_files), "size_kb": round(sess_size / 1024, 1)},
        "cache":         {"count": cache["count"], "size_kb": cache["size_kb"]},
        "snapshots":     {"count": len(snap_files), "size_kb": round(snap_size / 1024, 1)},
        "file_memory":   {"count": fm["count"], "size_kb": fm["size_kb"]},
        "notifications": {"count": notif_count, "size_kb": round(notif_size / 1024, 1)},
        "sltm":          {"count": sltm_records, "size_kb": round(sltm_size / 1024, 1)},
        "total_kb":      round(total_bytes / 1024, 1),
    })


@app.route('/api/data/cache', methods=['DELETE'])
def api_data_clear_cache():
    """Clear .c3/cache/ (compression cache)."""
    cache_dir = PROJECT_PATH / ".c3" / "cache"
    count = 0
    if cache_dir.exists():
        for f in cache_dir.iterdir():
            if f.is_file():
                f.unlink()
                count += 1
    return jsonify({"cleared": count})


@app.route('/api/data/snapshots', methods=['DELETE'])
def api_data_clear_snapshots():
    """Clear .c3/snapshots/ (context snapshots)."""
    snaps_dir = PROJECT_PATH / ".c3" / "snapshots"
    count = 0
    if snaps_dir.exists():
        for f in snaps_dir.glob("snap_*.json"):
            f.unlink()
            count += 1
    return jsonify({"cleared": count})


@app.route('/api/data/file-memory', methods=['DELETE'])
def api_data_clear_file_memory():
    """Clear .c3/file_memory/ structural maps (rebuilt on next use)."""
    fm_dir = PROJECT_PATH / ".c3" / "file_memory"
    count = 0
    if fm_dir.exists():
        for f in fm_dir.iterdir():
            if f.is_file() and not f.name.startswith("_"):
                f.unlink()
                count += 1
    return jsonify({"cleared": count})


@app.route('/api/data/notifications', methods=['DELETE'])
def api_data_clear_notifications():
    """Truncate the notifications queue."""
    notif_path = PROJECT_PATH / ".c3" / "notifications.jsonl"
    if notif_path.exists():
        notif_path.write_text("", encoding="utf-8")
    return jsonify({"cleared": True})


@app.route('/api/data/sessions', methods=['DELETE'])
def api_data_clear_sessions():
    """Delete old sessions, keeping the most recent N (default 5)."""
    keep = request.args.get('keep', 5, type=int)
    sessions_dir = PROJECT_PATH / ".c3" / "sessions"
    cleared = 0
    kept = 0
    if sessions_dir.exists():
        all_sessions = sorted(
            sessions_dir.glob("session_*.json"),
            key=lambda f: f.stat().st_mtime, reverse=True
        )
        kept = min(keep, len(all_sessions))
        for f in all_sessions[keep:]:
            f.unlink()
            cleared += 1
    return jsonify({"cleared": cleared, "kept": kept})


# ─── API: Activity Log ────────────────────────────────────
@app.route('/api/activity')
def api_activity():
    """Get recent activity log events."""
    limit = request.args.get('limit', 100, type=int)
    event_type = request.args.get('type', None)
    if event_type == '':
        event_type = None
    since = request.args.get('since', None)
    until = request.args.get('until', None)
    events = activity_log.get_recent(limit, event_type, since=since, until=until)
    return jsonify(events)


@app.route('/api/activity/stats')
def api_activity_stats():
    """Get activity log statistics."""
    return jsonify(activity_log.get_stats())


# ─── API: Edit Ledger ────────────────────────────────────
@app.route('/api/edits')
def api_edits_get():
    """Get edit history, optionally filtered by file."""
    file = request.args.get('file', None)
    limit = request.args.get('limit', 50, type=int)
    since = request.args.get('since', None)
    if runtime and runtime.edit_ledger:
        return jsonify(runtime.edit_ledger.get_history(file=file, limit=limit, since=since))
    return jsonify([])

@app.route('/api/edits', methods=['POST'])
def api_edits_post():
    """Log a new edit."""
    if not runtime or not runtime.edit_ledger:
        return jsonify({"error": "edit ledger not available"}), 503
    data = request.get_json(force=True)
    entry = runtime.edit_ledger.log_edit(
        file=data.get("file", ""),
        change_type=data.get("change_type", "modified"),
        summary=data.get("summary", ""),
        lines_changed=data.get("lines_changed"),
        tags=data.get("tags"),
        session_id=data.get("session_id"),
    )
    return jsonify(entry)

@app.route('/api/edits/stats')
def api_edits_stats():
    """Edit statistics."""
    if runtime and runtime.edit_ledger:
        return jsonify(runtime.edit_ledger.get_stats())
    return jsonify({"total": 0, "by_type": {}, "files": 0, "most_edited": []})

@app.route('/api/edits/versions/<path:filepath>')
def api_edits_versions(filepath):
    """File version history."""
    if runtime and runtime.edit_ledger:
        return jsonify(runtime.edit_ledger.get_file_versions(filepath))
    return jsonify([])


# ─── API: Notifications ──────────────────────────────────
@app.route('/api/notifications')
def api_notifications():
    """Get unacknowledged notifications.

    Query params:
        limit: max entries (default 20)
        severities: comma-separated severity filter (default 'warning,critical').
            Pass 'all' or 'info,warning,critical' to include info events
            (e.g. for an activity log view).
    """
    limit = request.args.get('limit', 20, type=int)
    severities_raw = request.args.get('severities', 'warning,critical')
    if severities_raw in ('all', '*'):
        severities = ()
    else:
        severities = tuple(s.strip() for s in severities_raw.split(',') if s.strip())
    return jsonify(notification_store.get_unacknowledged(limit, severities=severities))


@app.route('/api/notifications/history')
def api_notifications_history():
    """Get historical notifications (including acknowledged) for the activity console."""
    limit = request.args.get('limit', 50, type=int)
    return jsonify(notification_store.get_history(limit))


@app.route('/api/notifications/ack', methods=['POST'])
def api_notifications_ack():
    """Acknowledge a notification by ID."""
    data = request.get_json(silent=True) or {}
    nid = data.get('id', '')
    if not nid:
        return jsonify({"error": "id required"}), 400
    found = notification_store.acknowledge(nid)
    return jsonify({"acknowledged": found, "id": nid})


@app.route('/api/notifications/ack-all', methods=['POST'])
def api_notifications_ack_all():
    """Acknowledge all notifications."""
    count = notification_store.acknowledge_all()
    return jsonify({"acknowledged": count})


def _shutdown_process_after_delay(delay_seconds: float = 0.2):
    """Terminate this server process shortly after responding to the caller."""
    time.sleep(max(0.0, delay_seconds))
    try:
        os.kill(os.getpid(), signal.SIGINT)
    except Exception:
        os._exit(0)


def _windows_process_name(pid: int) -> str:
    """Return lowercase executable name for a Windows PID, or empty string."""
    if not sys.platform.startswith("win") or pid <= 0:
        return ""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
        ).strip()
    except Exception:
        return ""
    if not out or out.lower().startswith("info:"):
        return ""
    try:
        row = next(csv.reader([out]))
    except Exception:
        return ""
    if not row:
        return ""
    return str(row[0]).strip().lower()


def _force_close_parent_terminal_if_safe() -> bool:
    """Best-effort Windows terminal close by killing the immediate parent shell tree."""
    if not sys.platform.startswith("win"):
        return False
    parent_pid = os.getppid()
    if parent_pid <= 0:
        return False
    parent_name = _windows_process_name(parent_pid)
    # Guardrail: only terminate common shell/terminal hosts, never unknown parents.
    allowed = {
        "powershell.exe",
        "pwsh.exe",
        "cmd.exe",
        "windowsterminal.exe",
        "conhost.exe",
        "mintty.exe",
        "wezterm-gui.exe",
        "alacritty.exe",
    }
    if parent_name not in allowed:
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(parent_pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
        return True
    except Exception:
        return False


def _shutdown_after_delay_with_optional_terminal_close(force_terminal_close: bool, delay_seconds: float = 0.2):
    """Shutdown server; optionally attempt safe parent terminal close on Windows first."""
    time.sleep(max(0.0, delay_seconds))
    if force_terminal_close and _force_close_parent_terminal_if_safe():
        return
    try:
        os.kill(os.getpid(), signal.SIGINT)
    except Exception:
        os._exit(0)


@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    """Shut down C3 UI server (equivalent to pressing Ctrl+C in terminal)."""
    data = request.get_json(silent=True) or {}
    force_terminal_close = bool(data.get("force_close_terminal", False))
    try:
        if session_mgr and getattr(session_mgr, "current_session", None):
            session_mgr.save_session("Shutdown requested from UI")
    except Exception:
        pass

    threading.Thread(
        target=_shutdown_after_delay_with_optional_terminal_close,
        args=(force_terminal_close,),
        daemon=True,
        name="c3-ui-shutdown",
    ).start()
    return jsonify({
        "success": True,
        "message": "C3 is shutting down",
        "force_terminal_close_requested": force_terminal_close,
        "force_terminal_close_supported": sys.platform.startswith("win"),
    })


# ─── API: Hybrid Metrics & Config ─────────────────────────
@app.route('/api/hybrid/metrics')
def api_hybrid_metrics():
    """Get all tier metrics."""
    if not metrics_collector:
        return jsonify({"error": "No hybrid services initialized"})
    return jsonify(metrics_collector.collect())


@app.route('/api/hybrid/config', methods=['GET'])
def api_hybrid_config_get():
    """Get current hybrid feature flags and config."""
    return jsonify(hybrid_config or {})


@app.route('/api/hybrid/config', methods=['PUT'])
def api_hybrid_config_put():
    """Update hybrid feature flags. Persists to .c3/config.json."""
    global hybrid_config
    data = request.get_json() or {}

    # Only allow updating known hybrid keys
    from core.config import DEFAULTS
    allowed_keys = set(DEFAULTS.keys())
    updates = {k: v for k, v in data.items() if k in allowed_keys}
    if "show_savings_footer" in updates and "SHOW_SAVINGS_SUMMARY" not in updates:
        updates["SHOW_SAVINGS_SUMMARY"] = bool(updates["show_savings_footer"])
    elif "SHOW_SAVINGS_SUMMARY" in updates and "show_savings_footer" not in updates:
        updates["show_savings_footer"] = bool(updates["SHOW_SAVINGS_SUMMARY"])

    if not updates:
        return jsonify({"error": "No valid keys to update"}), 400

    # Update in-memory config
    if hybrid_config:
        hybrid_config.update(updates)

    # Persist to disk
    config_path = PROJECT_PATH / ".c3" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    config.setdefault("hybrid", {}).update(updates)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return jsonify(hybrid_config or {})


# ─── API: Budget Config ───────────────────────────────────
@app.route('/api/budget/config', methods=['GET'])
def api_budget_config_get():
    """Get current context budget threshold."""
    config_path = PROJECT_PATH / ".c3" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    budget = config.get("context_budget", {})
    defaults = SessionManager.DEFAULT_BUDGET_THRESHOLDS
    return jsonify({
        "threshold": budget.get("threshold", defaults["threshold"]),
        "show_context_nudges": (hybrid_config or {}).get("show_context_nudges", True),
    })


@app.route('/api/budget/config', methods=['PUT'])
def api_budget_config_put():
    """Update context budget threshold. Persists to .c3/config.json."""
    data = request.get_json() or {}
    config_path = PROJECT_PATH / ".c3" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass

    if "threshold" in data:
        try:
            config.setdefault("context_budget", {})["threshold"] = max(1000, int(data["threshold"]))
        except (ValueError, TypeError):
            return jsonify({"error": "threshold must be an integer >= 1000"}), 400

    if "show_context_nudges" in data:
        config.setdefault("hybrid", {})["show_context_nudges"] = bool(data["show_context_nudges"])

    # Persist
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # Reload budget thresholds in session manager
    try:
        svc = app.config.get("svc")
        if svc and hasattr(svc, "session_mgr"):
            svc.session_mgr._budget_thresholds = svc.session_mgr._load_budget_thresholds()
    except Exception:
        pass

    defaults = SessionManager.DEFAULT_BUDGET_THRESHOLDS
    merged_budget = config.get("context_budget", {})
    return jsonify({
        "threshold": merged_budget.get("threshold", defaults["threshold"]),
        "show_context_nudges": (hybrid_config or {}).get("show_context_nudges", True),
    })


# ─── API: Permissions ─────────────────────────────────────
@app.route('/api/permissions', methods=['GET'])
def api_permissions_get():
    """Get current permission tier and available tiers (Claude Code only)."""
    from cli.c3 import PERMISSION_TIERS, _build_permission_tier, _detect_current_tier
    from core.ide import load_ide_config
    ide = load_ide_config(str(PROJECT_PATH))
    settings_path = PROJECT_PATH / ".claude" / "settings.local.json"
    current = _detect_current_tier(settings_path)
    # Also check .c3/config.json for stored tier choice
    config_path = PROJECT_PATH / ".c3" / "config.json"
    stored_tier = None
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                stored_tier = json.load(f).get("permission_tier")
        except Exception:
            pass
    # Count current rules
    allow_count = 0
    deny_count = 0
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


@app.route('/api/permissions', methods=['PUT'])
def api_permissions_put():
    """Apply a permission tier. Body: {tier: "read-only"|"standard"|"permissive"}"""
    from cli.c3 import (
        PERMISSION_TIERS,
        _build_permission_tier,
        _merge_permission_tier,
        _safe_read_json,
    )
    data = request.get_json() or {}
    tier = data.get("tier", "").strip()
    if tier not in PERMISSION_TIERS:
        return jsonify({"error": f"Unknown tier: {tier}. Available: {', '.join(PERMISSION_TIERS)}"}), 400

    tier_perms = _build_permission_tier(tier)
    settings_path = PROJECT_PATH / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _safe_read_json(settings_path, str(settings_path))
    settings["permissions"] = _merge_permission_tier(
        settings.get("permissions") or {}, tier_perms["permissions"]
    )
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

    # Persist tier choice
    config_path = PROJECT_PATH / ".c3" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    config["permission_tier"] = tier
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return jsonify({
        "current_tier": tier,
        "allow_count": len(tier_perms["permissions"]["allow"]),
        "deny_count": len(tier_perms["permissions"]["deny"]),
        "message": f"Applied '{tier}' permissions. Restart Claude Code to activate.",
    })


# ─── API: Agent Config ────────────────────────────────────
@app.route('/api/agents/config', methods=['GET'])
def api_agents_config_get():
    """Get current agent config (merged with defaults)."""
    from core.config import load_agent_config
    return jsonify(load_agent_config(str(PROJECT_PATH)))


@app.route('/api/agents/config', methods=['PUT'])
def api_agents_config_put():
    """Update agent config. Persists to .c3/config.json."""
    from core.config import AGENT_DEFAULTS
    global agents
    data = request.get_json() or {}

    # Validate: only accept known agent names and known keys
    updates = {}
    for agent_name, overrides in data.items():
        if agent_name not in AGENT_DEFAULTS:
            continue
        if not isinstance(overrides, dict):
            continue
        allowed_keys = set(AGENT_DEFAULTS[agent_name].keys())
        valid = {k: v for k, v in overrides.items() if k in allowed_keys}
        if valid:
            updates[agent_name] = valid

    if not updates:
        return jsonify({"error": "No valid agent config keys to update"}), 400

    # Persist to disk
    config_path = PROJECT_PATH / ".c3" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    agents_cfg = config.setdefault("agents", {})
    for agent_name, overrides in updates.items():
        agents_cfg.setdefault(agent_name, {}).update(overrides)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    # Apply updates to any live in-process agents so the UI server reflects changes immediately.
    by_name = {agent.name: agent for agent in (agents or [])}
    for agent_name, overrides in updates.items():
        agent = by_name.get(agent_name)
        if not agent:
            continue
        was_enabled = bool(getattr(agent, "enabled", False))
        for key, value in overrides.items():
            if hasattr(agent, key):
                setattr(agent, key, value)
        now_enabled = bool(getattr(agent, "enabled", False))
        if was_enabled and not now_enabled:
            agent.stop()
        elif now_enabled and not getattr(agent, "_thread", None):
            agent.start()
        elif now_enabled and getattr(agent, "_thread", None) and not agent._thread.is_alive():
            agent.start()

    # Return merged config
    from core.config import load_agent_config
    return jsonify(load_agent_config(str(PROJECT_PATH)))


@app.route('/api/agents/status', methods=['GET'])
def api_agents_status():
    """Return runtime status for all initialized background agents."""
    if not agents:
        return jsonify({"agents": [], "count": 0, "running": 0})
    statuses = [agent.get_status() for agent in agents]
    return jsonify({
        "agents": statuses,
        "count": len(statuses),
        "running": sum(1 for item in statuses if item.get("running")),
    })


@app.route('/api/agents/run/<agent_name>', methods=['POST'])
def api_agents_run(agent_name):
    """Run a single background agent check immediately."""
    target = None
    for agent in agents or []:
        if agent.name.lower() == agent_name.lower():
            target = agent
            break

    if not target:
        return jsonify({"error": f"Unknown agent: {agent_name}"}), 404

    result = target.run_once()
    status = result.get("status", target.get_status())
    if result.get("ok"):
        return jsonify({
            "success": True,
            "agent": target.name,
            "status": status,
        })
    return jsonify({
        "error": result.get("error", "Agent run failed"),
        "agent": target.name,
        "status": status,
    }), 500


@app.route('/api/delegate/config', methods=['GET'])
def api_delegate_config_get():
    """Get current delegate config (merged with defaults)."""
    return jsonify(load_delegate_config(str(PROJECT_PATH)))


@app.route('/api/delegate/config', methods=['PUT'])
def api_delegate_config_put():
    """Update delegate config. Persists to .c3/config.json."""
    from core.config import DELEGATE_DEFAULTS
    data = request.get_json() or {}
    allowed_keys = set(DELEGATE_DEFAULTS.keys())
    updates = {k: v for k, v in data.items() if k in allowed_keys}

    if not updates:
        return jsonify({"error": "No valid delegate config keys to update"}), 400

    config_path = PROJECT_PATH / ".c3" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    config.setdefault("delegate", {}).update(updates)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return jsonify(load_delegate_config(str(PROJECT_PATH)))


@app.route('/api/ollama/models')
def api_ollama_models():
    """List locally available Ollama models."""
    from services.ollama_client import OllamaClient
    base_url = (hybrid_config or {}).get("ollama_base_url", "http://localhost:11434")
    client = OllamaClient(base_url)
    models = client.list_models()
    if models is None:
        return jsonify({"error": "Ollama not reachable", "models": []}), 503
    return jsonify({"models": models})


@app.route('/api/memory-llm/config', methods=['GET'])
def api_memory_llm_config_get():
    """Get memory_llm config (merged with defaults). Never returns the API key."""
    from core.config import load_memory_llm_config
    cfg = load_memory_llm_config(str(PROJECT_PATH))
    key_set = False
    try:
        from services.ollama_credentials import api_key_available
        key_set = api_key_available(cfg.get("cloud_base_url", ""),
                                    cfg.get("api_key_env", ""),
                                    cfg.get("api_key", ""))
    except Exception:
        pass
    cfg["api_key"] = "*****" if cfg.get("api_key") else ""
    cfg["api_key_set"] = bool(key_set)
    return jsonify(cfg)


@app.route('/api/memory-llm/config', methods=['PUT'])
def api_memory_llm_config_put():
    """Update memory_llm config. Persists to .c3/config.json (never the key)."""
    from core.config import MEMORY_LLM_DEFAULTS, load_memory_llm_config
    data = request.get_json() or {}
    # api_key is keyring-only (set via /api/memory-llm/key) — config.json is
    # not gitignored by default, so a plaintext key there could get committed.
    allowed_keys = set(MEMORY_LLM_DEFAULTS.keys()) - {"api_key"}
    updates = {k: v for k, v in data.items() if k in allowed_keys}

    if not updates:
        return jsonify({"error": "No valid memory_llm config keys to update"}), 400

    config_path = PROJECT_PATH / ".c3" / "config.json"
    config = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            pass
    config.setdefault("memory_llm", {}).update(updates)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return jsonify(load_memory_llm_config(str(PROJECT_PATH)))


@app.route('/api/memory-llm/key', methods=['POST'])
def api_memory_llm_key_set():
    """Store (or clear, with an empty key) the Ollama Cloud API key in the OS keyring."""
    from core.config import load_memory_llm_config
    from services.ollama_credentials import delete_api_key, save_api_key
    data = request.get_json() or {}
    key = str(data.get("key") or "").strip()
    base = load_memory_llm_config(str(PROJECT_PATH)).get("cloud_base_url", "")
    if not key:
        removed = delete_api_key(base)
        return jsonify({"cleared": bool(removed), "api_key_set": False})
    try:
        save_api_key(key, base)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"api_key_set": True})


# ── Credential vault (v2.58.0) ──────────────────────────
# Write-only wire contract: no endpoint ever returns a stored value.
# Values go IN over POST and come out only at the subprocess boundary
# (cli/tools/shell.py). GETs return masked metadata + live fingerprint.


def _cred_route_audit(action: str, name: str, scope: str) -> None:
    """Names only — never values. Failure-safe."""
    try:
        from services.activity_log import ActivityLog
        ActivityLog(str(PROJECT_PATH)).log("cred_action", {
            "kind": "creds", "action": action, "name": name,
            "scope": scope, "via": "ui",
        })
    except Exception:
        pass
    try:
        from services.edit_ledger import EditLedger
        EditLedger(str(PROJECT_PATH)).log_edit(
            file=f"cred://{name}", change_type=f"cred_{action}",
            summary=f"{action} {name} ({scope}) via Credentials UI",
            tags=["creds", action],
            detail={"kind": "creds", "action": action, "name": name,
                    "scope": scope},
        )
    except Exception:
        pass


@app.route('/api/credentials', methods=['GET'])
def api_credentials_list():
    """Masked entry list (values never included)."""
    from services import credential_store as cred_store
    pp = str(PROJECT_PATH)
    usage = cred_store.read_usage_state(pp)
    out = []
    for name, entry in cred_store.list_entries(pp).items():
        rec = dict(entry)
        rec["name"] = name
        rec["last_used"] = (usage.get(name) or {}).get("last_used", "")
        rec["use_count"] = (usage.get(name) or {}).get("use_count", 0)
        out.append(rec)
    return jsonify({"entries": out})


@app.route('/api/credentials', methods=['POST'])
def api_credentials_set():
    """Create/update an entry. `value` optional — metadata-only update when
    absent; a submitted value is stored and never echoed back."""
    from services import credential_store as cred_store
    data = request.get_json() or {}
    name = str(data.get("name") or "").strip()
    scope = str(data.get("scope") or "project").strip()
    value = str(data.get("value") or "")
    ctype = str(data.get("type") or data.get("ctype") or "token")
    meta = {
        "description": str(data.get("description") or ""),
        "env_var": str(data.get("env_var") or ""),
        "agent_readable": bool(data.get("agent_readable")),
        "inject": bool(data.get("inject")),
    }
    try:
        if value:
            entry = cred_store.set_credential(
                name, value, scope=scope, project_path=str(PROJECT_PATH),
                ctype=ctype, **meta)
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
                name, scope=scope, project_path=str(PROJECT_PATH), **fields)
    except cred_store.CredentialError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    entry = dict(entry)
    entry["name"] = name
    entry["scope"] = scope
    _cred_route_audit("set" if value else "update", name, scope)
    return jsonify({"entry": entry})


@app.route('/api/credentials/import', methods=['POST'])
def api_credentials_import():
    """Import KEY=VALUE lines (.env paste). Values are stored, never echoed."""
    from services import credential_store as cred_store
    data = request.get_json() or {}
    text = str(data.get("text") or "")
    scope = str(data.get("scope") or "project").strip()
    try:
        result = cred_store.import_env(
            text, scope=scope, project_path=str(PROJECT_PATH),
            overwrite=bool(data.get("overwrite")),
        )
    except cred_store.CredentialError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    for created in result["created"]:
        _cred_route_audit("set", created, scope)
    return jsonify(result)


@app.route('/api/credentials/<name>', methods=['DELETE'])
def api_credentials_delete(name):
    from services import credential_store as cred_store
    pp = str(PROJECT_PATH)
    scope = str(request.args.get("scope") or "").strip()
    if not scope:
        entry = cred_store.get_entry(name, project_path=pp)
        scope = entry.get("scope") or "project" if entry else "project"
    try:
        removed = cred_store.delete_credential(name, scope=scope, project_path=pp)
    except cred_store.CredentialError as exc:
        return jsonify({"error": str(exc)}), 400
    if removed:
        _cred_route_audit("delete", name, scope)
    return jsonify({"removed": bool(removed), "scope": scope})


@app.route('/api/credentials/<name>/check', methods=['POST'])
def api_credentials_check(name):
    """Resolvability probe — returns a fingerprint, never the value."""
    from services import credential_store as cred_store
    pp = str(PROJECT_PATH)
    entry = cred_store.get_entry(name, project_path=pp)
    if not entry:
        return jsonify({"error": f"no credential named '{name}'"}), 404
    return jsonify({
        "name": name,
        "scope": entry["scope"],
        "storage": entry.get("storage", "keyring"),
        "resolvable": cred_store.get_value(name, project_path=pp) is not None,
        "fingerprint": cred_store.fingerprint(name, project_path=pp),
    })


# ── Access Guard (v2.62.0) ──────────────────────────────
# Human-only rule management: ALL mutations arrive from this UI (or the
# `c3 access` CLI) and are ledger/activity-logged. No agent-facing mutation
# surface exists — the agent's read view is c3_status; its runtime interface
# is the refusal string (docs/access-guard.md §1).


def _access_route_audit(action: str, glob: str, kind: str, scope: str) -> None:
    """Rule identifiers only. Failure-safe."""
    try:
        from services.activity_log import ActivityLog
        ActivityLog(str(PROJECT_PATH)).log("access_action", {
            "kind": "access", "action": action, "glob": glob,
            "rule_kind": kind, "scope": scope, "via": "ui",
        })
    except Exception:
        pass
    try:
        from services.edit_ledger import EditLedger
        EditLedger(str(PROJECT_PATH)).log_edit(
            file=f"access://{glob}", change_type=f"access_{action}",
            summary=f"{action} {kind} rule '{glob}' ({scope}) via Access Guard UI",
            tags=["access", action],
            detail={"kind": "access", "action": action, "glob": glob,
                    "rule_kind": kind, "scope": scope},
        )
    except Exception:
        pass


@app.route('/api/access', methods=['GET'])
def api_access_list():
    """Rule registry: builtin pseudo-scope + global + project, plus the §5
    coverage matrix string and the list of corrupt (deny-all) scopes."""
    from services import access_guard
    scopes = access_guard.list_rules(str(PROJECT_PATH))
    corrupt = [s for s in ("global", "project")
               if (scopes.get(s) or {}).get("corrupt")]
    return jsonify({"scopes": scopes, "corrupt": corrupt,
                    "coverage": access_guard.COVERAGE_MATRIX})


@app.route('/api/access', methods=['POST'])
def api_access_add():
    """Add a rule {glob, kind, scope}. Human surface — mutations audited."""
    from services import access_guard
    data = request.get_json() or {}
    glob = str(data.get("glob") or "")
    kind = str(data.get("kind") or "").strip()
    scope = str(data.get("scope") or "project").strip()
    try:
        result = access_guard.set_rule(glob, kind, scope, str(PROJECT_PATH))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if result["added"]:
        _access_route_audit("add", result["glob"], kind, scope)
    return jsonify({"rule": result})


@app.route('/api/access', methods=['DELETE'])
def api_access_remove():
    """Remove a rule (query args or JSON body: glob, kind, scope)."""
    from services import access_guard
    data = request.get_json(silent=True) or {}
    glob = str(request.args.get("glob") or data.get("glob") or "")
    kind = str(request.args.get("kind") or data.get("kind") or "").strip()
    scope = str(request.args.get("scope") or data.get("scope") or "project").strip()
    try:
        result = access_guard.remove_rule(glob, kind, scope, str(PROJECT_PATH))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if result["removed"]:
        _access_route_audit("remove", result["glob"], kind, scope)
    return jsonify(result)


@app.route('/api/access/check', methods=['GET', 'POST'])
def api_access_check():
    """Test-path probe: verdict + matched rule + scope + exact refusal string.
    Read-only — never mutates rules; safe to call freely."""
    from services import access_guard
    data = request.get_json(silent=True) or {}
    path = str(request.args.get("path") or data.get("path") or "").strip()
    op = str(request.args.get("op") or data.get("op") or "read").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    if op not in ("read", "write", "create", "delete"):
        return jsonify({"error": f"unknown op '{op}' — expected "
                        "read|write|create|delete"}), 400
    denial = access_guard.check(path, op, str(PROJECT_PATH))
    if denial is None:
        return jsonify({"path": path, "op": op, "verdict": "allowed",
                        "rule": "", "scope": "", "refusal": ""})
    return jsonify({
        "path": path, "op": op,
        "verdict": "read_only" if denial.kind == "read_only" else "denied",
        "rule": denial.rule, "scope": denial.scope, "reason": denial.reason,
        "refusal": access_guard.refusal(denial, path, op),
    })


@app.route('/api/delegate', methods=['POST'])
def api_delegate():
    """Delegate a task to local LLM with optional streaming."""
    data = request.get_json() or {}
    task = data.get("task", "").strip()
    task_type = data.get("task_type", "ask")
    file_path = data.get("file_path", "")
    stream = bool(data.get("stream", False))

    if not task:
        return jsonify({"error": "No task specified"}), 400

    if not router:
        return jsonify({"error": "Router not initialized"}), 503

    if stream:
        def generate_sse():
            result = router.route(task, force_class=task_type)
            # If router didn't return a generator, it means it's a non-streaming result or error
            response_gen = result.get("response")

            # Metadata chunk
            meta = {
                "model": result.get("model"),
                "route_class": result.get("route_class"),
                "latency_ms": result.get("latency_ms"),
                "is_meta": True
            }
            yield f"data: {json.dumps(meta)}\n\n"

            if isinstance(response_gen, str):
                yield f"data: {json.dumps({'text': response_gen})}\n\n"
            elif response_gen:
                for chunk in response_gen:
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

            yield "data: [DONE]\n\n"

        return Response(generate_sse(), mimetype='text/event-stream')

    # Non-streaming path
    result = router.route(task, force_class=task_type)
    return jsonify(result)


@app.route('/api/summarize', methods=['POST'])
def api_summarize():
    """Summarize text with optional streaming."""
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    style = data.get("style", "concise")
    stream = bool(data.get("stream", False))

    if not text:
        return jsonify({"error": "No text specified"}), 400

    if not router:
        return jsonify({"error": "Router not initialized"}), 503

    if stream:
        def generate_sse():
            result = router.summarize(text, style=style)
            # Update: router.summarize needs to handle streaming too
            # For now, let's just wrap the result if it's not a generator
            summary = result.get("summary")

            meta = {
                "model": result.get("model"),
                "style": result.get("style"),
                "is_meta": True
            }
            yield f"data: {json.dumps(meta)}\n\n"

            if isinstance(summary, str):
                yield f"data: {json.dumps({'text': summary})}\n\n"
            elif summary:
                for chunk in summary:
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

            yield "data: [DONE]\n\n"

        return Response(generate_sse(), mimetype='text/event-stream')

    result = router.summarize(text, style=style)
    return jsonify(result)


# ─── API: SLTM ───────────────────────────────────────────
@app.route('/api/sltm/stats')
def api_sltm_stats():
    """Get SLTM backend status and collection sizes."""
    if not vector_store:
        return jsonify({"error": "SLTM not initialized", "vector_enabled": False})
    return jsonify(vector_store.get_stats())


@app.route('/api/sltm/search', methods=['POST'])
def api_sltm_search():
    """Search SLTM with hybrid TF-IDF + vector search."""
    if not vector_store:
        return jsonify({"error": "SLTM not initialized"}), 503
    data = request.get_json() or {}
    query = data.get("query", "").strip()
    category = data.get("category", "")
    top_k = data.get("top_k", 5)
    if not query:
        return jsonify({"error": "No query specified"}), 400
    results = vector_store.search(query, category, top_k)
    return jsonify(results)


@app.route('/api/sltm/add', methods=['POST'])
def api_sltm_add():
    """Add a record to SLTM."""
    if not vector_store:
        return jsonify({"error": "SLTM not initialized"}), 503
    data = request.get_json() or {}
    text = data.get("text", "").strip()
    category = data.get("category", "general")
    metadata = data.get("metadata", {})
    if not text:
        return jsonify({"error": "No text specified"}), 400
    result = vector_store.add(text, category, metadata)
    return jsonify(result)


# ─── API: Proxy ──────────────────────────────────────────
@app.route('/api/proxy/metrics')
def api_proxy_metrics():
    """Return proxy metrics from .c3/proxy_metrics.json."""
    metrics_file = PROJECT_PATH / ".c3" / "proxy_metrics.json"
    if not metrics_file.exists():
        return jsonify({"error": "No proxy metrics found", "hint": "Proxy writes metrics on shutdown"})
    try:
        with open(metrics_file, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/proxy/state')
def api_proxy_state():
    """Return live proxy state from .c3/proxy_state.json."""
    state_file = PROJECT_PATH / ".c3" / "proxy_state.json"
    if not state_file.exists():
        return jsonify({"error": "No proxy state found", "hint": "Proxy writes state after each tool call"})
    try:
        with open(state_file, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/proxy/config', methods=['GET'])
def api_proxy_config_get():
    """Return current proxy configuration."""
    cfg = load_proxy_config(str(PROJECT_PATH))
    return jsonify(cfg)


@app.route('/api/proxy/config', methods=['PUT'])
def api_proxy_config_put():
    """Update proxy configuration in .c3/config.json."""
    updates = request.get_json() or {}
    config_file = PROJECT_PATH / ".c3" / "config.json"
    data = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    proxy = data.get("proxy", {})
    proxy.update(updates)
    data["proxy"] = proxy
    config_file.parent.mkdir(parents=True, exist_ok=True)
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return jsonify(load_proxy_config(str(PROJECT_PATH)))


@app.route('/api/proxy/tools')
def api_proxy_tools():
    """Return full tool inventory with categories and visibility status."""
    from services.tool_classifier import CATEGORIES
    cfg = load_proxy_config(str(PROJECT_PATH))
    always_visible = cfg.get("always_visible", ["core"])
    filter_enabled = cfg.get("filter_tools", False)
    effective_filtering = bool(filter_enabled) and "all" not in always_visible

    # Build tool list with category info
    tools = []
    for cat_name, cat_info in CATEGORIES.items():
        for tool_name in cat_info["tools"]:
            visible = (not filter_enabled
                       or "all" in always_visible
                       or cat_name in always_visible)
            tools.append({
                "name": tool_name,
                "category": cat_name,
                "visible": visible,
                "priority": cat_info.get("priority", 99),
            })

    return jsonify({
        "tools": tools,
        "categories": {
            name: {
                "tools": info["tools"],
                "priority": info.get("priority", 99),
                "pinned": (not filter_enabled
                           or "all" in always_visible
                           or name in always_visible),
            }
            for name, info in CATEGORIES.items()
        },
        "filter_enabled": filter_enabled,
        "effective_filtering": effective_filtering,
        "always_visible": always_visible,
    })


# ─── API: MCP Status ─────────────────────────────────────
from core.mcp_toml import (
    parse_toml_mcp_servers as _parse_toml_mcp_servers,
)
from core.mcp_toml import (
    remove_toml_section as _remove_toml_section,
)
from core.mcp_toml import (
    upsert_toml_section as _upsert_toml_section,
)


def _find_server_script(servers: dict) -> bool:
    """Check whether any configured MCP server args reference an existing .py script."""
    for srv in servers.values():
        args = srv.get("args", []) if isinstance(srv, dict) else []
        for arg in args:
            if isinstance(arg, str) and arg.endswith(".py") and Path(arg).exists():
                return True
    return False


def _resolve_mcp_profile(ide_name: str | None):
    requested = (ide_name or "").strip().lower()
    if requested and requested != "auto":
        requested = normalize_ide_name(requested)
        if requested not in PROFILES:
            raise ValueError(f"Unknown IDE profile: {requested}")
        return requested, get_profile(requested)

    configured_ide = load_ide_config(str(PROJECT_PATH))
    detected_ide = detect_ide(str(PROJECT_PATH))
    active_ide = configured_ide or detected_ide or "claude-code"
    return active_ide, get_profile(active_ide)


def _mcp_config_path_for_profile(profile) -> Path:
    return (Path.home() / profile.config_path) if profile.config_path_global else (PROJECT_PATH / profile.config_path)


def _read_mcp_servers_for_profile(profile, mcp_file: Path) -> tuple[dict, dict]:
    """Read MCP servers for a profile. Returns (servers, full_json_config)."""
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


def _cleanup_c3_artifacts(profile) -> list[str]:
    """Remove project files/hooks related to C3 registration for the given profile."""
    removed_files = []

    # Remove instructions file generated for this IDE profile.
    if getattr(profile, "instructions_file", None):
        p = PROJECT_PATH / profile.instructions_file
        if p.exists():
            p.unlink()
            removed_files.append(str(p))

    # Claude-only hook and enabled-server cleanup.
    if profile.name == "claude-code" and profile.settings_path:
        settings_path = PROJECT_PATH / profile.settings_path
        if settings_path.exists():
            try:
                with open(settings_path, encoding="utf-8") as f:
                    settings = json.load(f)
            except Exception:
                settings = {}

            enabled = settings.get("enabledMcpjsonServers", [])
            if isinstance(enabled, list):
                settings["enabledMcpjsonServers"] = [x for x in enabled if x != "c3"]

            hooks = settings.get("hooks", {}).get("PostToolUse", [])
            filtered_hooks = []
            for item in hooks if isinstance(hooks, list) else []:
                matcher = item.get("matcher")
                hlist = item.get("hooks", [])
                if matcher in ("Bash", "Read"):
                    keep_sub = []
                    for h in hlist if isinstance(hlist, list) else []:
                        cmd = (h or {}).get("command", "")
                        if "hook_filter.py" in cmd or "hook_read.py" in cmd:
                            continue
                        keep_sub.append(h)
                    if keep_sub:
                        item["hooks"] = keep_sub
                        filtered_hooks.append(item)
                else:
                    filtered_hooks.append(item)
            settings.setdefault("hooks", {})["PostToolUse"] = filtered_hooks

            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            removed_files.append(str(settings_path))

    return removed_files


@app.route('/api/mcp/status')
def api_mcp_status():
    """Return MCP configuration status for the active IDE profile."""
    try:
        ide_name, profile = _resolve_mcp_profile(request.args.get("ide"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    mcp_file = _mcp_config_path_for_profile(profile)

    result = {
        "configured": False,   # c3 entry present in IDE config
        "active": False,       # c3 is not explicitly disabled
        "config": {"mcpServers": {}},
        "server_found": False,
        "mode": load_mcp_config(str(PROJECT_PATH)).get("mode", "direct"),
        "entrypoint": "",
        "ide": ide_name,
        "config_path": str(mcp_file),
    }

    if not mcp_file.exists():
        return jsonify(result)

    try:
        servers, _ = _read_mcp_servers_for_profile(profile, mcp_file)

        result["config"] = {"mcpServers": servers}
        result["server_found"] = _find_server_script(servers)

        c3_entry = servers.get("c3", {})
        if isinstance(c3_entry, dict):
            result["configured"] = True
            result["active"] = c3_entry.get("enabled", True) is not False
            args = c3_entry.get("args", [])
            if isinstance(args, list):
                for arg in args:
                    if isinstance(arg, str) and arg.endswith(".py"):
                        result["entrypoint"] = arg
                        if arg.endswith("mcp_proxy.py"):
                            result["mode"] = "proxy"
                        elif arg.endswith("mcp_server.py"):
                            result["mode"] = "direct"
                        break
    except Exception:
        pass

    return jsonify(result)


@app.route('/api/mcp/install', methods=['POST'])
def api_mcp_install():
    """Install MCP configuration."""
    try:
        data = request.get_json(silent=True) or {}
        ide_name = data.get('ide', 'auto')
        mcp_mode = data.get('mcp_mode', 'direct')
        from types import SimpleNamespace

        from cli.c3 import cmd_install_mcp
        args = SimpleNamespace(project_path=str(PROJECT_PATH), ide=ide_name, mcp_mode=mcp_mode)
        result = cmd_install_mcp(args)
        return jsonify({"success": True, "result": str(result)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/mcp/servers', methods=['POST'])
def api_mcp_add_server():
    """Add a custom MCP server to the project configuration."""
    try:
        data = request.get_json(silent=True) or {}
        name = data.get('name')
        command = data.get('command')
        args = data.get('args', [])
        env = data.get('env', {})
        ide_name = data.get('ide', 'auto')
        enabled = data.get('enabled', True)

        if not name or not command:
            return jsonify({"error": "Name and command are required"}), 400

        ide_name, profile = _resolve_mcp_profile(ide_name)
        mcp_file = _mcp_config_path_for_profile(profile)
        mcp_file.parent.mkdir(parents=True, exist_ok=True)

        if profile.config_format == "toml":
            # Codex TOML path
            section = f"{profile.config_key}.{name}"
            entries = {"command": command, "args": args}
            if profile.name == "codex":
                entries["enabled"] = bool(enabled)
            _upsert_toml_section(mcp_file, section, entries)
        else:
            servers, raw_config = _read_mcp_servers_for_profile(profile, mcp_file)
            server_config = {"command": command, "args": args}
            if env:
                server_config["env"] = env
            if profile.name == "codex":
                server_config["enabled"] = bool(enabled)
            servers[name] = server_config

            if not raw_config:
                raw_config = {}
            raw_config.setdefault(profile.config_key, {})
            raw_config[profile.config_key] = servers
            with open(mcp_file, "w", encoding="utf-8") as f:
                json.dump(raw_config, f, indent=2)

        return jsonify({"success": True, "ide": ide_name, "config_path": str(mcp_file)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/mcp/servers/<name>', methods=['DELETE'])
def api_mcp_remove_server(name):
    """Remove a custom MCP server from the project configuration."""
    try:
        query_ide = request.args.get("ide")
        body = request.get_json(silent=True) or {}
        ide_name = body.get("ide", query_ide or "auto")
        raw_remove = body.get("remove_files", request.args.get("remove_files", "0"))
        if isinstance(raw_remove, bool):
            remove_files = raw_remove
        else:
            remove_files = str(raw_remove).strip().lower() in ("1", "true", "yes")

        ide_name, profile = _resolve_mcp_profile(ide_name)
        mcp_file = _mcp_config_path_for_profile(profile)
        if not mcp_file.exists():
            return jsonify({"error": "MCP config not found"}), 404

        removed = False
        removed_files = []
        if profile.config_format == "toml":
            section = f"{profile.config_key}.{name}"
            removed = _remove_toml_section(mcp_file, section)
            # Remove empty TOML file if all mcp server sections are gone
            if mcp_file.exists():
                remaining, _ = _read_mcp_servers_for_profile(profile, mcp_file)
                if not remaining:
                    mcp_file.unlink()
                    removed_files.append(str(mcp_file))
        else:
            servers, raw_config = _read_mcp_servers_for_profile(profile, mcp_file)
            if name in servers:
                del servers[name]
                removed = True
                raw_config.setdefault(profile.config_key, {})
                raw_config[profile.config_key] = servers
                # If no servers remain, remove config file to fully clean this IDE MCP config.
                if not servers and len(raw_config.keys()) <= 1:
                    mcp_file.unlink()
                    removed_files.append(str(mcp_file))
                else:
                    with open(mcp_file, "w", encoding="utf-8") as f:
                        json.dump(raw_config, f, indent=2)

        if not removed:
            return jsonify({"error": "Server not found"}), 404

        if name == "c3" and remove_files:
            removed_files.extend(_cleanup_c3_artifacts(profile))

        return jsonify({"success": True, "ide": ide_name, "removed_files": removed_files})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── API: Conversations ───────────────────────────────────
_conv_store = None


def _get_conv_store():
    global _conv_store
    if _conv_store is None:
        _conv_store = runtime.convo_store if runtime and runtime.convo_store else None
        if _conv_store is None:
            from services.conversation_store import ConversationStore
            _conv_store = ConversationStore(str(PROJECT_PATH))
    return _conv_store


@app.route('/api/conversations')
def api_conversations_list():
    limit = int(request.args.get('limit', 100))
    store = _get_conv_store()
    return jsonify(store.list_sessions(limit=limit))


@app.route('/api/conversations/sync')
def api_conversations_sync():
    store = _get_conv_store()
    source = (request.args.get('source', 'all') or 'all').strip().lower()
    force_raw = (request.args.get('force', '') or '').strip().lower()
    force = force_raw in ("1", "true", "yes", "on")
    result = store.sync(source=source, force=force)
    configured_ide = load_ide_config(str(PROJECT_PATH))
    detected_ide = detect_ide(str(PROJECT_PATH))
    active_ide = configured_ide or detected_ide or "claude-code"
    if configured_ide == "claude-code" and detected_ide and detected_ide != "claude-code":
        active_ide = detected_ide
    result["ide"] = {
        "configured": configured_ide,
        "detected": detected_ide,
        "active": active_ide,
        "display_name": get_profile(active_ide).display_name,
    }
    return jsonify(result)


@app.route('/api/conversations/search')
def api_conversations_search():
    q = request.args.get('q', '').strip()
    limit = int(request.args.get('limit', 30))
    session_id = request.args.get('session_id') or None
    if not q:
        return jsonify([])
    store = _get_conv_store()
    return jsonify(store.search(q, limit=limit, session_id=session_id))


@app.route('/api/conversations/stats')
def api_conversations_stats():
    store = _get_conv_store()
    return jsonify(store.get_stats())


@app.route('/api/conversations/<session_id>')
def api_conversations_get(session_id):
    store = _get_conv_store()
    offset = request.args.get('offset')
    limit = request.args.get('limit')

    try:
        offset_val = max(0, int(offset)) if offset is not None else 0
    except Exception:
        offset_val = 0
    try:
        limit_val = int(limit) if limit is not None else None
    except Exception:
        limit_val = None

    return jsonify(store.get_session(session_id, offset=offset_val, limit=limit_val))


@app.route('/api/conversations/<session_id>/turn', methods=['POST'])
def api_conversations_add_turn(session_id):
    data = request.json or {}
    role = data.get('role', 'user')
    text = data.get('text', '').strip()
    tool_calls = data.get('tool_calls')
    source = (data.get('source', 'api') or 'api').strip().lower()
    if not text:
        return jsonify({'error': 'text required'}), 400
    store = _get_conv_store()
    turn = store.add_turn(session_id, role, text, tool_calls=tool_calls, source=source)
    return jsonify(turn)


@app.route('/api/conversations/<session_id>/export')
def api_conversations_export(session_id):
    """Export a conversation session as markdown or JSON."""
    fmt = request.args.get('format', 'markdown')
    store = _get_conv_store()
    turns = store.get_session(session_id)
    sessions = store.list_sessions(limit=500)
    meta = next((s for s in sessions if s['session_id'] == session_id), {})
    if fmt == 'json':
        return jsonify({'session': meta, 'turns': turns})
    lines = [f"# {meta.get('title', session_id)}", "",
             f"**Source:** {meta.get('source', '?')} | **Turns:** {len(turns)} | **Date:** {meta.get('started', '')}", ""]
    for t in turns:
        label = "User" if t.get('role') == 'user' else "Assistant"
        lines += [f"## {label}", "", t.get('text', '')]
        if t.get('tool_calls'):
            lines += ["", "**Tool calls:**"]
            lines += [f"- `{tc.get('tool', '?')}` {tc.get('args', '')}" for tc in t['tool_calls']]
        lines += ["", "---", ""]
    return jsonify({'markdown': "\n".join(lines), 'title': meta.get('title', session_id)})


# ─── API: Bitbucket Data Center / Server (v2.30.0) ───────


def _bb_client_and_repo():
    """Return (client, project_key, repo_slug, error_response_or_None).

    On any setup failure (missing account, missing token, repo not configured),
    returns (None, "", "", flask_response). Callers should early-return that
    response unchanged.
    """
    from core.config import load_bitbucket_config
    from services import bitbucket_credentials as bb_creds
    from services.bitbucket_client import BitbucketDataCenterClient

    cfg = load_bitbucket_config(PROJECT_PATH)
    active = cfg.get("active") or {}
    base_url = active.get("base_url")
    username = active.get("username")
    if not base_url or not username:
        return None, "", "", (jsonify({"error": "no active Bitbucket account — run `c3 bitbucket login`"}), 400)
    try:
        token = bb_creds.load_token(base_url, username)
    except RuntimeError as exc:
        return None, "", "", (jsonify({"error": str(exc)}), 500)
    if not token:
        return None, "", "", (jsonify({"error": f"no keyring token for {username}@{base_url}"}), 400)

    project = (request.args.get("project") or "").strip() or cfg.get("default_project", "")
    repo = (request.args.get("repo") or "").strip() or cfg.get("default_repo", "")
    client = BitbucketDataCenterClient(
        base_url=base_url, token=token,
        verify_tls=bool(cfg.get("verify_tls", True)),
    )
    return client, project, repo, None


def _bb_require_repo(project: str, repo: str):
    if not project or not repo:
        return jsonify({"error": "project and repo required (set defaults via `c3 bitbucket set-default`)"}), 400
    return None


def _bb_handle(call):
    """Run a BB API closure; translate BitbucketError to HTTP response."""
    from services.bitbucket_client import BitbucketError
    try:
        return jsonify(call())
    except BitbucketError as exc:
        return jsonify({
            "error": str(exc),
            "status": exc.status,
            "path": exc.path,
        }), 502 if exc.status == 0 else exc.status
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


@app.route('/api/bitbucket/status')
def api_bitbucket_status():
    """Active account, accounts list, defaults, and a connection probe."""
    from core.config import load_bitbucket_config
    from services import bitbucket_credentials as bb_creds
    from services.bitbucket_client import BitbucketDataCenterClient, BitbucketError

    cfg = load_bitbucket_config(PROJECT_PATH)
    active = cfg.get("active") or {}
    out = {"config": cfg, "connection": {"ok": False}}

    if not active.get("base_url") or not active.get("username"):
        out["connection"]["error"] = "no active account"
        return jsonify(out)
    try:
        token = bb_creds.load_token(active["base_url"], active["username"])
    except RuntimeError as exc:
        out["connection"]["error"] = str(exc)
        return jsonify(out)
    if not token:
        out["connection"]["error"] = "no keyring token"
        return jsonify(out)
    try:
        client = BitbucketDataCenterClient(
            base_url=active["base_url"], token=token,
            verify_tls=bool(cfg.get("verify_tls", True)),
        )
        props = client.application_properties()
        out["connection"] = {"ok": True, "version": props.get("version", "?")}
    except BitbucketError as exc:
        out["connection"] = {"ok": False, "error": str(exc), "status": exc.status}
    return jsonify(out)


@app.route('/api/bitbucket/prs')
def api_bitbucket_list_prs():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    state = (request.args.get("state") or "OPEN").upper()
    return _bb_handle(lambda: {
        "values": client.list_pull_requests(project, repo, state=state,
                                            limit=int(request.args.get("limit", 50))),
    })


@app.route('/api/bitbucket/prs', methods=['POST'])
def api_bitbucket_create_pr():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    data = request.get_json(force=True) or {}
    return _bb_handle(lambda: client.create_pull_request(
        project, repo,
        title=data.get("title", ""),
        from_branch=data.get("from_branch", ""),
        to_branch=data.get("to_branch", ""),
        description=data.get("description", ""),
        reviewers=data.get("reviewers") or [],
    ))


@app.route('/api/bitbucket/prs/<int:pr_id>')
def api_bitbucket_get_pr(pr_id: int):
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: client.get_pull_request(project, repo, pr_id))


@app.route('/api/bitbucket/prs/<int:pr_id>/diff')
def api_bitbucket_pr_diff(pr_id: int):
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: {
        "diff": client.get_pr_diff(project, repo, pr_id,
                                   context_lines=int(request.args.get("context_lines", 3))),
    })


@app.route('/api/bitbucket/prs/<int:pr_id>/approve', methods=['POST'])
def api_bitbucket_approve_pr(pr_id: int):
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: client.approve_pr(project, repo, pr_id))


@app.route('/api/bitbucket/prs/<int:pr_id>/decline', methods=['POST'])
def api_bitbucket_decline_pr(pr_id: int):
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err

    def _do():
        pr = client.get_pull_request(project, repo, pr_id)
        return client.decline_pr(project, repo, pr_id, version=pr.get("version", 0))
    return _bb_handle(_do)


@app.route('/api/bitbucket/prs/<int:pr_id>/merge', methods=['POST'])
def api_bitbucket_merge_pr(pr_id: int):
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    data = request.get_json(silent=True) or {}

    def _do():
        pr = client.get_pull_request(project, repo, pr_id)
        return client.merge_pr(project, repo, pr_id, version=pr.get("version", 0),
                               message=data.get("message", ""))
    return _bb_handle(_do)


@app.route('/api/bitbucket/prs/<int:pr_id>/comments', methods=['POST'])
def api_bitbucket_comment_pr(pr_id: int):
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    data = request.get_json(force=True) or {}
    text = data.get("text", "") or data.get("body", "")
    return _bb_handle(lambda: client.comment_on_pr(project, repo, pr_id, text=text))


@app.route('/api/bitbucket/branches')
def api_bitbucket_list_branches():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: {
        "values": client.list_branches(project, repo,
                                       filter_text=request.args.get("filter", "")),
    })


@app.route('/api/bitbucket/branches', methods=['POST'])
def api_bitbucket_create_branch():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    data = request.get_json(force=True) or {}
    return _bb_handle(lambda: client.create_branch(
        project, repo,
        name=data.get("name", ""),
        start_point=data.get("start_point", ""),
        message=data.get("message", ""),
    ))


@app.route('/api/bitbucket/branches/<path:name>', methods=['DELETE'])
def api_bitbucket_delete_branch(name: str):
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: client.delete_branch(project, repo, name=name))


@app.route('/api/bitbucket/builds')
def api_bitbucket_builds():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    commit = request.args.get("commit", "")
    if not commit:
        repo_err = _bb_require_repo(project, repo)
        if repo_err is not None:
            return repo_err
        # Resolve to default branch's latest commit.
        try:
            default = client.get_default_branch(project, repo)
            commit = default.get("latestCommit", "")
        except Exception:
            commit = ""
    if not commit:
        return jsonify({"error": "no commit hash and could not resolve default branch"}), 400
    return _bb_handle(lambda: {
        "commit": commit,
        "values": client.get_build_status(commit),
    })


@app.route('/api/bitbucket/activity')
def api_bitbucket_activity():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: {
        "values": client.list_repo_activities(project, repo,
                                              limit=int(request.args.get("limit", 30))),
    })


@app.route('/api/bitbucket/repo-settings')
def api_bitbucket_repo_settings_get():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: client.get_repo_settings(project, repo))


@app.route('/api/bitbucket/repo-settings', methods=['PUT'])
def api_bitbucket_repo_settings_put():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    data = request.get_json(force=True) or {}
    return _bb_handle(lambda: client.update_repo_settings(project, repo, settings=data))


@app.route('/api/bitbucket/webhooks')
def api_bitbucket_webhooks_get():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: {"values": client.list_webhooks(project, repo)})


@app.route('/api/bitbucket/webhooks', methods=['POST'])
def api_bitbucket_webhooks_create():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    data = request.get_json(force=True) or {}
    return _bb_handle(lambda: client.create_webhook(
        project, repo,
        name=data.get("name", ""),
        url=data.get("url", ""),
        events=data.get("events") or [],
        active=bool(data.get("active", True)),
        secret=data.get("secret", ""),
    ))


@app.route('/api/bitbucket/webhooks/<int:webhook_id>', methods=['DELETE'])
def api_bitbucket_webhooks_delete(webhook_id: int):
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: client.delete_webhook(project, repo, webhook_id=webhook_id))


@app.route('/api/bitbucket/permissions')
def api_bitbucket_permissions():
    client, project, repo, err = _bb_client_and_repo()
    if err is not None:
        return err
    repo_err = _bb_require_repo(project, repo)
    if repo_err is not None:
        return repo_err
    return _bb_handle(lambda: {
        "users": client.list_repo_user_permissions(project, repo),
        "groups": client.list_repo_group_permissions(project, repo),
    })


# ─── Jira (Cloud / Data Center) ──────────────────────────
def _jira_client_and_entry():
    """Return (client, account_entry, error_response_or_None)."""
    from core.config import load_jira_config
    from services import jira_credentials as jr_creds
    from services.jira_client import JiraClient

    cfg = load_jira_config(str(PROJECT_PATH))
    name = cfg.get("default_account", "")
    entry = (cfg.get("accounts") or {}).get(name)
    if not isinstance(entry, dict) or not entry.get("base_url"):
        return None, {}, (jsonify({"error": "no Jira account — run `c3 jira login`"}), 400)
    try:
        token = jr_creds.load_token(entry["base_url"], entry.get("username", ""))
    except RuntimeError as exc:
        return None, {}, (jsonify({"error": str(exc)}), 500)
    if not token:
        return None, {}, (jsonify({
            "error": f"no keyring token for {entry.get('username')}@{entry['base_url']}",
        }), 400)
    try:
        client = JiraClient(
            entry["base_url"], entry.get("username", ""), token,
            deployment=entry.get("deployment", "cloud"),
            verify_tls=bool(entry.get("verify_tls", True)),
            ca_bundle=entry.get("ca_bundle", ""),
        )
    except ValueError as exc:
        return None, {}, (jsonify({"error": str(exc)}), 400)
    return client, {**entry, "name": name}, None


def _jira_handle(call):
    """Run a Jira API closure; translate JiraError to an HTTP response."""
    from services.jira_client import JiraError
    try:
        return jsonify(call())
    except JiraError as exc:
        return jsonify({
            "error": str(exc),
            "status": exc.status,
            "path": exc.path,
        }), 502 if exc.status == 0 else exc.status
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500


def _jira_my_jql(entry: dict) -> str:
    clauses = ["assignee = currentUser()"]
    project = request.args.get("project") or entry.get("default_project", "")
    if project:
        safe = project.replace("\\", "\\\\").replace('"', '\\"')
        clauses.append(f'project = "{safe}"')
    return " AND ".join(clauses) + " ORDER BY updated DESC"


@app.route('/api/jira/status')
def api_jira_status():
    """Accounts registry, default account, and a connection probe."""
    from core.config import load_jira_config
    from services import jira_credentials as jr_creds
    from services.jira_client import JiraClient, JiraError

    cfg = load_jira_config(str(PROJECT_PATH))
    out = {"config": cfg, "connection": {"ok": False}}
    name = cfg.get("default_account", "")
    entry = (cfg.get("accounts") or {}).get(name)
    if not isinstance(entry, dict) or not entry.get("base_url"):
        out["connection"]["error"] = "no default account"
        return jsonify(out)
    try:
        token = jr_creds.load_token(entry["base_url"], entry.get("username", ""))
    except RuntimeError as exc:
        out["connection"]["error"] = str(exc)
        return jsonify(out)
    if not token:
        out["connection"]["error"] = "no keyring token"
        return jsonify(out)
    try:
        client = JiraClient(
            entry["base_url"], entry.get("username", ""), token,
            deployment=entry.get("deployment", "cloud"),
            verify_tls=bool(entry.get("verify_tls", True)),
            ca_bundle=entry.get("ca_bundle", ""),
        )
        info = client.server_info()
        me = client.myself()
        out["connection"] = {
            "ok": True,
            "version": info.get("version", "?"),
            "user": me.get("displayName", ""),
            "deployment": entry.get("deployment", ""),
        }
    except (JiraError, ValueError) as exc:
        out["connection"] = {"ok": False, "error": str(exc)}
    return jsonify(out)


@app.route('/api/jira/issues/my')
def api_jira_my_issues():
    client, entry, err = _jira_client_and_entry()
    if err is not None:
        return err
    return _jira_handle(lambda: client.search(
        _jira_my_jql(entry),
        limit=int(request.args.get("limit", 50)),
        cursor=request.args.get("cursor", ""),
    ))


@app.route('/api/jira/board')
def api_jira_board():
    """My issues grouped by status category — the v1 'board' derives from
    plain JQL statusCategory grouping, no agile API involved."""
    client, entry, err = _jira_client_and_entry()
    if err is not None:
        return err

    def call():
        result = client.search(_jira_my_jql(entry),
                               limit=int(request.args.get("limit", 100)))
        columns = {"new": [], "indeterminate": [], "done": []}
        for issue in result.get("issues", []):
            columns.setdefault(
                issue.get("status_category") or "new", columns["new"]
            ).append(issue)
        return {"columns": columns, "next_cursor": result.get("next_cursor", "")}
    return _jira_handle(call)


@app.route('/api/jira/search')
def api_jira_search():
    client, entry, err = _jira_client_and_entry()
    if err is not None:
        return err
    jql = (request.args.get("jql") or "").strip()
    if not jql:
        return jsonify({"error": "jql query parameter is required"}), 400
    return _jira_handle(lambda: client.search(
        jql,
        limit=int(request.args.get("limit", 50)),
        cursor=request.args.get("cursor", ""),
    ))


@app.route('/api/jira/issue/<key>')
def api_jira_get_issue(key: str):
    client, entry, err = _jira_client_and_entry()
    if err is not None:
        return err

    def call():
        issue = client.get_issue(key)
        issue["transitions"] = client.list_transitions(key)
        return issue
    return _jira_handle(call)


@app.route('/api/jira/issue', methods=['POST'])
def api_jira_create_issue():
    client, entry, err = _jira_client_and_entry()
    if err is not None:
        return err
    data = request.get_json(force=True) or {}
    project = data.get("project") or entry.get("default_project", "")
    issue_type = data.get("issue_type", "")
    summary = data.get("summary", "")
    if not project or not issue_type or not summary:
        return jsonify({"error": "project, issue_type, and summary are required"}), 400
    return _jira_handle(lambda: client.create_issue(
        project, issue_type, summary,
        description=data.get("description", ""),
        fields=data.get("fields") or None,
    ))


@app.route('/api/jira/issue/<key>/transition', methods=['POST'])
def api_jira_transition(key: str):
    client, entry, err = _jira_client_and_entry()
    if err is not None:
        return err
    data = request.get_json(force=True) or {}
    transition_id = str(data.get("transition", "")).strip()
    if not transition_id:
        return jsonify({"error": "transition id is required"}), 400
    return _jira_handle(lambda: client.transition_issue(
        key, transition_id, comment=data.get("comment", ""),
    ) or {"ok": True})


@app.route('/api/jira/issue/<key>/comment', methods=['POST'])
def api_jira_comment(key: str):
    client, entry, err = _jira_client_and_entry()
    if err is not None:
        return err
    data = request.get_json(force=True) or {}
    body = data.get("body", "")
    if not str(body).strip():
        return jsonify({"error": "body is required"}), 400
    return _jira_handle(lambda: client.add_comment(key, body))


@app.route('/api/jira/activity')
def api_jira_activity():
    """Local issue-key activity — current branch + edit-ledger scan.
    Pure local (no Jira call), so it works without a configured account."""
    from services import jira_links
    out = jira_links.branch_issue_keys(str(PROJECT_PATH))
    out["entries"] = jira_links.ledger_activity(
        str(PROJECT_PATH),
        key=request.args.get("key", ""),
        limit=int(request.args.get("limit", 25)),
    )
    return jsonify(out)


@app.route('/api/jira/issue/<key>/assign', methods=['POST'])
def api_jira_assign(key: str):
    client, entry, err = _jira_client_and_entry()
    if err is not None:
        return err
    data = request.get_json(force=True) or {}
    user = (data.get("user") or "").strip()
    if not user:
        return jsonify({"error": "user (deployment-native id) is required"}), 400
    return _jira_handle(lambda: client.assign_issue(key, user) or {"ok": True})


# ─── Launch ──────────────────────────────────────────────
def find_free_port(start: int = 3333, max_tries: int = 20) -> int:
    """Return the first free TCP port starting from *start*."""
    import socket
    for offset in range(max_tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port found in range {start}–{start + max_tries - 1}")


def run_server(
    project_path: str,
    port: int = 3333,
    open_browser: bool = True,
    silent: bool = False,
    nano: bool = False,
):
    """Launch the C3 web UI, auto-selecting a free port if the requested one is busy."""
    init_services(project_path)

    # Try to load existing index
    if not indexer._load_index():
        print("Building index for the first time...")
        result = indexer.build_index()
        print(f"  Indexed {result['files_indexed']} files, {result['chunks_created']} chunks")

    port = find_free_port(port)
    start_path = "/nano" if nano else ""
    url = f"http://localhost:{port}{start_path}"

    # Register in global session registry (for project switcher in UI)
    try:
        _meta = {}
        _meta_path = Path(project_path) / ".c3" / "config.json"
        if _meta_path.exists():
            import json as _json
            _meta = _json.loads(_meta_path.read_text(encoding="utf-8")).get("meta", {})
        _resolved = Path(project_path).resolve()
        # Fallback chain: custom meta name → folder name → parent/folder for disambiguation
        _folder = _resolved.name or _resolved.parent.name or "project"
        _proj_name = (_meta.get("name") or "").strip() or _folder
        _register_session(port, str(_resolved), _proj_name)
        atexit.register(_unregister_session, port)
    except Exception:
        pass

    # Use ASCII-safe banner (Windows cmd chokes on Unicode box-drawing)
    print("")
    print("  +==========================================+")
    print("  |   C3 — Code Context Control UI           |")
    print(f"  |   {url:<38} |")
    print(f"  |   Project: {str(PROJECT_PATH)[:28]:<28} |")
    print("  +==========================================+")
    print("")
    print("  Press Ctrl+C to stop the server.")
    print("")

    if open_browser and not silent:
        def _open():
            time.sleep(1.5)
            import webbrowser
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    if silent:
        # Suppress Flask/Werkzeug request logging noise (e.g., /api/* per-request lines).
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app.run(host='127.0.0.1', port=port, debug=False)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="C3 Web UI Server")
    parser.add_argument("project_path", nargs="?", default=".")
    parser.add_argument("--port", type=int, default=3333)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--silent", action="store_true")
    parser.add_argument("--nano", action="store_true")
    args = parser.parse_args()
    run_server(args.project_path, args.port, not args.no_browser, args.silent, args.nano)
