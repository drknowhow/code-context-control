"""Oracle Memory Agent — Flask server + entry point."""

import atexit
import json
import logging
import socket
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path

# Ensure project root is on path for imports
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from flask import Flask, Response, jsonify, request  # noqa: E402

from oracle.config import ORACLE_DIR, load_config, save_config  # noqa: E402
from oracle.mcp_oracle import mcp_url, start_mcp_thread  # noqa: E402
from oracle.services import api_auth, local_session  # noqa: E402
from oracle.services.activity_reporter import ActivityReporter  # noqa: E402
from oracle.services.api_auth import extract_bearer  # noqa: E402
from oracle.services.api_auth import verify as verify_api_key  # noqa: E402
from oracle.services.c3_bridge import C3Bridge  # noqa: E402
from oracle.services.chat_engine import ChatEngine  # noqa: E402
from oracle.services.chat_store import ChatStore  # noqa: E402
from oracle.services.cross_memory import CrossMemory  # noqa: E402
from oracle.services.federated_graph import FederatedGraph  # noqa: E402
from oracle.services.health_checker import HealthChecker  # noqa: E402
from oracle.services.insight_engine import InsightEngine  # noqa: E402
from oracle.services.memory_reader import MemoryReader  # noqa: E402
from oracle.services.memory_writer import MemoryWriter  # noqa: E402
from services.ollama_bridge import OllamaBridge  # noqa: E402
from oracle.services.project_scanner import ProjectScanner  # noqa: E402
from oracle.services.review_agent import ReviewAgent  # noqa: E402
from oracle.services.tool_executor import ToolExecutor  # noqa: E402
from oracle.services.tool_registry import ToolRegistry, _c3_version  # noqa: E402

# ── App ───────────────────────────────────────────────────
app = Flask(__name__)

# ── Services (initialized at startup) ────────────────────
_cfg: dict = {}
_model_verified: bool | None = None  # cached result of startup model check
_bridge: OllamaBridge | None = None
_scanner: ProjectScanner | None = None
_reader: MemoryReader | None = None
_checker: HealthChecker | None = None
_writer: MemoryWriter | None = None
_cross_memory: CrossMemory | None = None
_engine: InsightEngine | None = None
_agent: ReviewAgent | None = None
_chat_store: ChatStore | None = None
_chat_engine: ChatEngine | None = None
_c3_bridge: C3Bridge | None = None
_federated: FederatedGraph | None = None
_tool_registry: ToolRegistry | None = None
_activity_reporter: ActivityReporter | None = None


def _init_services():
    global _cfg, _bridge, _scanner, _reader, _checker, _writer, _cross_memory, _engine, _agent, _model_verified, _chat_store, _chat_engine, _c3_bridge, _federated, _tool_registry, _activity_reporter
    _cfg = load_config()
    _bridge = OllamaBridge(
        base_url=_cfg.get("ollama_base_url", "https://ollama.com"),
        model=_cfg.get("model", "gemma4:31b-cloud"),
        api_key=_cfg.get("ollama_api_key", ""),
        cache_ttl_sec=int(_cfg.get("llm_cache_ttl_sec", 86400)),
    )
    # Verify model works on startup (background thread to avoid blocking)
    def _verify():
        global _model_verified
        if _bridge.is_available(timeout=5):
            _model_verified = _bridge.verify_model()
            status = "verified" if _model_verified else "FAILED"
            logging.getLogger("oracle").info("Model %s: %s", _bridge.model, status)
        else:
            _model_verified = False
            logging.getLogger("oracle").warning("Ollama unreachable — model not verified")
    threading.Thread(target=_verify, daemon=True, name="oracle-model-verify").start()
    _scanner = ProjectScanner(
        hub_url=_cfg.get("hub_url", "http://localhost:3330"),
        ttl=float(_cfg.get("scanner_ttl_seconds", 20)),
    )
    _reader = MemoryReader()
    _checker = HealthChecker(_reader)
    _writer = MemoryWriter()
    _cross_memory = CrossMemory()
    _engine = InsightEngine(_bridge, _reader, _cross_memory)
    _chat_store = ChatStore()
    _c3_bridge = C3Bridge(scanner=_scanner)
    _federated = FederatedGraph(reader=_reader, cross_memory=_cross_memory, ollama_bridge=_bridge)
    # Reporter before ReviewAgent: the review loop emits the scheduled digest.
    _activity_reporter = ActivityReporter(scanner=_scanner, ollama_bridge=_bridge)
    _agent = ReviewAgent(
        scanner=_scanner,
        reader=_reader,
        health_checker=_checker,
        insight_engine=_engine,
        cross_memory=_cross_memory,
        writer=_writer,
        interval=int(_cfg.get("review_interval_seconds", 1800)),
        federated_graph=_federated,
        activity_reporter=_activity_reporter,
    )
    _chat_engine = ChatEngine(
        bridge=_bridge,
        reader=_reader,
        writer=_writer,
        cross_memory=_cross_memory,
        health_checker=_checker,
        insight_engine=_engine,
        scanner=_scanner,
        store=_chat_store,
        c3_bridge=_c3_bridge,
        activity_reporter=_activity_reporter,
    )
    _tool_registry = ToolRegistry(
        ToolExecutor(_chat_engine),
        max_tier=_cfg.get("api_max_tier", "action"),
    )
    atexit.register(lambda: _c3_bridge.shutdown() if _c3_bridge else None)


# ── CORS ──────────────────────────────────────────────────
# Localhost security guard + scoped CORS (replaces the previous wildcard CORS).
# Host-header allowlist + Origin/Referer CSRF guard. Bearer auth on
# /api/discovery/* (see _discovery_auth_guard below) and the local write gate
# (_local_write_guard: session cookie or Bearer on every other mutating
# /api/* call) still apply on top; this guard blocks cross-origin browsers,
# the write gate blocks unauthenticated local processes.
from core.web_security import (
    allowed_hostnames as _allowed_hostnames,
)
from core.web_security import (
    install_guard as _install_web_guard,
)

_install_web_guard(
    app, lambda: _allowed_hostnames(_cfg.get("bind_host"), _cfg.get("allowed_hosts"))
)


# ── Discovery API auth guard ──────────────────────────────
@app.before_request
def _discovery_auth_guard():
    """Bearer-token gate for the external Discovery API (``/api/discovery/*``)."""
    path = request.path or ""
    if not path.startswith("/api/discovery"):
        return None
    if request.method == "OPTIONS":
        return None  # allow CORS preflight
    if not _cfg.get("api_enabled", True):
        return jsonify({"error": "discovery API disabled"}), 404
    if _cfg.get("api_require_auth", True):
        token = extract_bearer(request.headers.get("Authorization"))
        if not verify_api_key(token):
            return jsonify({"error": "unauthorized"}), 401
    return None


# ── Local write gate ──────────────────────────────────────
@app.before_request
def _local_write_guard():
    """Auth gate for mutating local API calls (everything except Discovery).

    Requires either the per-boot dashboard session cookie (issued on ``GET /``
    to loopback browsers) or the Discovery Bearer token. Default-deny: any
    future mutating ``/api/*`` endpoint is covered automatically. Closes the
    rotate-then-read kill chain — previously any local process could POST
    /api/apikey/rotate unauthenticated and read the fresh token, defeating
    the Bearer gates on /api/config and /api/discovery/*.
    """
    path = request.path or ""
    if not path.startswith("/api/") or path.startswith("/api/discovery"):
        return None
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if verify_api_key(extract_bearer(request.headers.get("Authorization"))):
        return None
    if local_session.verify(request.cookies.get(local_session.COOKIE_NAME)):
        return None
    return jsonify({"error": "unauthorized"}), 401


# ── Static ────────────────────────────────────────────────

# JS load order for the concatenated Oracle UI build (mirrors cli/hub_server.py).
# One shared script scope: function declarations hoist across files, and
# app.js (the init IIFE) must stay LAST.
_ORACLE_JS_FILES = [
    "ui/core.js",
    "ui/busy.js",
    "ui/theme_tabs.js",
    "ui/crossgraph.js",
    "ui/header.js",
    "ui/projects.js",
    "ui/insights.js",
    "ui/activity.js",
    "ui/suggestions.js",
    "ui/settings.js",
    "ui/agents.js",
    "ui/chat/markdown.js",
    "ui/chat/conversations.js",
    "ui/chat/stream_renderer.js",
    "ui/chat/toolbar.js",
    "ui/chat/input.js",
    "ui/chat/send.js",
    "ui/app.js",
]


def _build_oracle_html() -> str:
    """Concatenate oracle_ui.html shell + all JS module files into one response."""
    oracle_dir = Path(__file__).parent
    shell_path = oracle_dir / "oracle_ui.html"
    if not shell_path.exists():
        return "<h1>Oracle UI not found.</h1>"

    shell = shell_path.read_text(encoding="utf-8")

    js_parts = []
    for rel in _ORACLE_JS_FILES:
        js_path = oracle_dir / rel
        if js_path.exists():
            js_parts.append(f"// ═══ {rel} ═══\n" + js_path.read_text(encoding="utf-8"))

    return shell.replace("/* __C3_ORACLE_SCRIPTS__ */", "\n\n".join(js_parts))


# Cache the built HTML (built on first request; cleared on server restart).
_oracle_html_cache: str | None = None


@app.route("/")
def index():
    global _oracle_html_cache
    if _oracle_html_cache is None:
        _oracle_html_cache = _build_oracle_html()
    resp = Response(_oracle_html_cache, mimetype="text/html")
    # Issue the dashboard session cookie only to loopback browsers; remote
    # viewers (LAN bind) can read GET dashboards but cannot mutate.
    if local_session.is_loopback(request.remote_addr):
        local_session.attach_cookie(resp)
    return resp


# ── Health ────────────────────────────────────────────────
@app.route("/api/health")
def api_health():
    ollama_ok = _bridge.is_available(timeout=3) if _bridge else False
    hub_ok = False
    try:
        hub_url = _cfg.get("hub_url", "http://localhost:3330").rstrip("/")
        req = urllib.request.Request(f"{hub_url}/api/health")
        with urllib.request.urlopen(req, timeout=2) as r:
            hub_ok = json.loads(r.read()).get("service") == "c3-hub"
    except Exception:
        pass
    return jsonify({
        "status": "ok",
        "service": "c3-oracle",
        "version": _c3_version(),
        "model": _cfg.get("model", "gemma4:31b-cloud"),
        "ollama_available": ollama_ok,
        "model_verified": _model_verified,
        "hub_available": hub_ok,
    })


# ── Config ────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = load_config()
    # Mask API key in response — only show if set
    if cfg.get("ollama_api_key"):
        cfg["ollama_api_key"] = cfg["ollama_api_key"][:4] + "••••"
    return jsonify(cfg)


# Keys that POST /api/config may set. Derived from config DEFAULTS so the
# allowlist tracks the schema; anything outside this set is rejected. Notably
# excludes nothing sensitive by accident — but the gate below still requires the
# Bearer token, so even allowlisted keys can't be flipped by an unauthenticated
# local process (e.g. POST {"api_require_auth": false} to strip Discovery auth).
from oracle.config import DEFAULTS as _CONFIG_DEFAULTS  # noqa: E402

_CONFIG_SETTABLE_KEYS = frozenset(_CONFIG_DEFAULTS.keys())


@app.route("/api/config", methods=["POST"])
def api_config_set():
    global _cfg
    # Auth is enforced by _local_write_guard (session cookie or Bearer): this
    # endpoint can disable Discovery auth or repoint ollama_base_url, so it
    # must never be reachable unauthenticated.
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    # Reject unknown keys rather than blindly cfg.update(body) — an attacker
    # could otherwise smuggle arbitrary keys into the persisted config.
    unknown = sorted(k for k in body if k not in _CONFIG_SETTABLE_KEYS)
    if unknown:
        return jsonify({"error": "unknown config keys", "keys": unknown}), 400
    cfg = load_config()
    cfg.update(body)
    save_config(cfg)
    _cfg = cfg
    # Update bridge if config changed
    re_verify = False
    if _bridge:
        if "model" in body:
            _bridge.model = cfg["model"]
            re_verify = True
        if "ollama_api_key" in body:
            _bridge.api_key = cfg["ollama_api_key"]
            re_verify = True
        if "ollama_base_url" in body:
            _bridge.base_url = cfg["ollama_base_url"].rstrip("/")
            re_verify = True
    if re_verify and _bridge:
        def _rv():
            global _model_verified
            _model_verified = _bridge.verify_model()
        threading.Thread(target=_rv, daemon=True).start()
    return jsonify({"saved": True, "config": cfg})


# ── Projects ──────────────────────────────────────────────
@app.route("/api/projects")
def api_projects():
    projects = _scanner.discover() if _scanner else []
    # Attach cached health status + last_reviewed timestamp
    for p in projects:
        report = _agent.get_report(p["path"]) if _agent else None
        p["health_status"] = report.get("status", "unknown") if report else "unknown"
        p["health_issues"] = len(report.get("issues", [])) if report else 0
        p["last_reviewed"] = _agent.get_last_reviewed(p["path"]) if _agent else None
    return jsonify(projects)


@app.route("/api/projects/scan", methods=["POST"])
def api_projects_scan():
    # Explicit Scan action bypasses the scanner's TTL cache.
    projects = _scanner.discover(force=True) if _scanner else []
    return jsonify({"scanned": len(projects), "projects": projects})


@app.route("/api/projects/review", methods=["POST"])
def api_projects_review():
    body = request.get_json(silent=True) or {}
    path = body.get("path", "")
    if not path:
        return jsonify({"error": "path required"}), 400
    # Run health check synchronously + save report + update state
    if _agent:
        report = _agent.review_single(path)
    elif _checker:
        report = _checker.check(path)
    else:
        report = {"error": "not initialized"}
    # Run LLM analysis in background
    if _engine:
        def _analyze():
            try:
                _engine.analyze_project(path)
            except Exception:
                pass
        threading.Thread(target=_analyze, daemon=True).start()
    return jsonify(report)


@app.route("/api/projects/health")
def api_projects_health():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "path required"}), 400
    # Try cached report first
    report = _agent.get_report(path) if _agent else None
    if not report:
        report = _checker.check(path) if _checker else {"error": "not initialized"}
    return jsonify(report)


@app.route("/api/projects/facts")
def api_projects_facts():
    path = request.args.get("path", "")
    limit = int(request.args.get("limit", 50))
    if not path:
        return jsonify({"error": "path required"}), 400
    stats = _reader.get_fact_stats(path) if _reader else {}
    facts = _reader.read_facts(path) if _reader else []
    # Sort by relevance, limit
    facts.sort(key=lambda f: int(f.get("relevance_count", 0)), reverse=True)
    return jsonify({"stats": stats, "facts": facts[:limit]})


@app.route("/api/projects/graph")
def api_projects_graph():
    path = request.args.get("path", "")
    if not path:
        return jsonify({"error": "path required"}), 400
    stats = _reader.get_graph_stats(path) if _reader else {}
    return jsonify(stats)


# ── Insights ──────────────────────────────────────────────
@app.route("/api/insights")
def api_insights():
    if not _cross_memory:
        return jsonify([])
    return jsonify({
        "insights": _cross_memory.get_all_insights(),
        "stats": _cross_memory.stats(),
        "links": _cross_memory.get_project_links(),
    })


@app.route("/api/insights/project")
def api_insights_project():
    path = request.args.get("path", "")
    if not path or not _cross_memory:
        return jsonify([])
    return jsonify(_cross_memory.get_for_project(path))


@app.route("/api/insights/generate", methods=["POST"])
def api_insights_generate():
    if not _engine or not _scanner:
        return jsonify({"error": "not initialized"}), 500
    projects = _scanner.discover()
    paths = [p["path"] for p in projects if p.get("has_facts")]
    if len(paths) < 2:
        return jsonify({"error": "Need at least 2 projects with facts", "available": len(paths)}), 400
    insights = _engine.find_cross_project_links(paths)
    return jsonify({"generated": len(insights), "insights": insights})


@app.route("/api/graph/federated")
def api_graph_federated():
    """Return federated memory graph across projects."""
    if not _federated or not _scanner:
        return jsonify({"error": "not initialized"}), 500
    projects_param = request.args.get("projects", "")
    if projects_param:
        paths = [p for p in projects_param.split(",") if p]
    else:
        paths = [p["path"] for p in _scanner.discover() if p.get("has_facts")]
    min_sim = request.args.get("min_sim", type=float)
    top_k = request.args.get("top_k", type=int)
    force = request.args.get("force", "0") == "1"
    try:
        return jsonify(_federated.build(paths, force=force, min_sim=min_sim, top_k=top_k))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/graph/federated/node/<path:node_id>")
def api_graph_federated_node(node_id):
    """Return a single federated node's fact + cross-project neighbors."""
    if not _federated:
        return jsonify({"error": "not initialized"}), 500
    data = _federated.build([p["path"] for p in _scanner.discover() if p.get("has_facts")])
    node = next((n for n in data.get("nodes", []) if n["id"] == node_id), None)
    if not node:
        return jsonify({"error": "not found"}), 404
    neighbors = []
    nodes_by_id = {n["id"]: n for n in data["nodes"]}
    for e in data.get("edges", []):
        if e["src"] == node_id or e["dst"] == node_id:
            other_id = e["dst"] if e["src"] == node_id else e["src"]
            other = nodes_by_id.get(other_id)
            neighbors.append({
                "id": other_id,
                "type": e["type"],
                "scope": e.get("scope"),
                "weight": e.get("weight"),
                "label": (other["label"] if other else other_id),
                "project": other.get("project") if other else None,
            })
    return jsonify({"node": node, "neighbors": neighbors})


@app.route("/api/graph/federated/rebuild", methods=["POST"])
def api_graph_federated_rebuild():
    if not _federated or not _scanner:
        return jsonify({"error": "not initialized"}), 500
    body = request.get_json(silent=True) or {}
    paths = body.get("projects") or [p["path"] for p in _scanner.discover() if p.get("has_facts")]
    _federated.invalidate()
    return jsonify(_federated.build(paths, force=True))


@app.route("/api/graph/federated/stats")
def api_graph_federated_stats():
    if not _federated or not _scanner:
        return jsonify({"error": "not initialized"}), 500
    paths = [p["path"] for p in _scanner.discover() if p.get("has_facts")]
    data = _federated.build(paths)
    return jsonify({"stats": data.get("stats", {}), "projects": data.get("projects", [])})


@app.route("/api/insights/cross", methods=["POST"])
def api_insights_cross():
    """On-demand cross-project insight generation from federated graph."""
    if not _engine or not _scanner or not _federated:
        return jsonify({"error": "not initialized"}), 500
    body = request.get_json(silent=True) or {}
    paths = body.get("projects") or [p["path"] for p in _scanner.discover() if p.get("has_facts")]
    if len(paths) < 2:
        return jsonify({"error": "Need at least 2 projects with facts", "available": len(paths)}), 400
    fed = _federated.build(paths)
    try:
        if hasattr(_engine, "generate_cross_project_insights"):
            insights = _engine.generate_cross_project_insights(paths, federated_graph=fed)
        else:
            insights = _engine.find_cross_project_links(paths)
        return jsonify({"generated": len(insights), "insights": insights,
                        "graph_stats": fed.get("stats", {})})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/insights/dismiss", methods=["POST"])
def api_insights_dismiss():
    body = request.get_json(silent=True) or {}
    iid = body.get("id", "")
    if not iid or not _cross_memory:
        return jsonify({"error": "id required"}), 400
    return jsonify(_cross_memory.dismiss(iid))


# ── Suggestions ───────────────────────────────────────────
@app.route("/api/suggestions")
def api_suggestions():
    path = request.args.get("path")
    return jsonify(_writer.list_pending(path) if _writer else [])


@app.route("/api/suggestions/approve", methods=["POST"])
def api_suggestions_approve():
    body = request.get_json(silent=True) or {}
    sid = body.get("id", "")
    if not sid or not _writer:
        return jsonify({"error": "id required"}), 400
    return jsonify(_writer.approve_suggestion(sid))


@app.route("/api/suggestions/dismiss", methods=["POST"])
def api_suggestions_dismiss():
    body = request.get_json(silent=True) or {}
    sid = body.get("id", "")
    if not sid or not _writer:
        return jsonify({"error": "id required"}), 400
    return jsonify(_writer.dismiss_suggestion(sid))


# ── Review Agent ──────────────────────────────────────────
@app.route("/api/review/status")
def api_review_status():
    return jsonify(_agent.status if _agent else {"running": False})


@app.route("/api/review/start", methods=["POST"])
def api_review_start():
    if _agent:
        _agent.start()
    return jsonify({"started": True})


@app.route("/api/review/stop", methods=["POST"])
def api_review_stop():
    if _agent:
        _agent.stop()
    return jsonify({"stopped": True})


@app.route("/api/review/run-now", methods=["POST"])
def api_review_run_now():
    if _agent:
        _agent.run_now()
    return jsonify({"triggered": True})


# ── Ollama ────────────────────────────────────────────────
@app.route("/api/ollama/status")
def api_ollama_status():
    if not _bridge:
        return jsonify({"available": False, "models": []})
    available = _bridge.is_available(timeout=3)
    models = _bridge.list_models() if available else []
    in_tags = _bridge.has_model() if available else False
    return jsonify({
        "available": available,
        "models": models or [],
        "current_model": _cfg.get("model", "gemma4:31b-cloud"),
        "has_model": in_tags,
        "model_verified": _model_verified,  # None = still verifying, True/False = result
    })


@app.route("/api/ollama/test", methods=["POST"])
def api_ollama_test():
    if not _bridge:
        return jsonify({"error": "not initialized"}), 500

    # Try chat first (most robust for cloud/local)
    result = _bridge.chat([{"role": "user", "content": "Say 'Oracle is online' in exactly 4 words."}], max_tokens=32)

    # Fallback to generate for legacy local models
    if not result:
        result = _bridge.generate("Say 'Oracle is online' in exactly 4 words.", max_tokens=32)

    return jsonify({"response": result or "No response — check Ollama and model availability"})


# ── Chat ──────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Streaming chat with Oracle via SSE."""
    if not _chat_engine:
        return jsonify({"error": "not initialized"}), 500
    data = request.get_json(silent=True) or {}
    message = data.get("message", "").strip()
    conv_id = data.get("conversation_id") or None
    if not message:
        return jsonify({"error": "No message provided"}), 400

    def generate():
        for event in _chat_engine.chat(conv_id, message):
            yield f"data: {json.dumps(event, default=str)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/api/chat/conversations", methods=["GET"])
def api_chat_conversations_list():
    if not _chat_store:
        return jsonify({"error": "not initialized"}), 500
    limit = request.args.get("limit", 50, type=int)
    return jsonify({"conversations": _chat_store.list_conversations(limit)})


@app.route("/api/chat/conversations", methods=["POST"])
def api_chat_conversations_create():
    if not _chat_store:
        return jsonify({"error": "not initialized"}), 500
    data = request.get_json(silent=True) or {}
    title = data.get("title")
    conv_id = _chat_store.create_conversation(title)
    return jsonify({"id": conv_id}), 201


@app.route("/api/chat/conversations/<conv_id>", methods=["GET"])
def api_chat_conversation_get(conv_id):
    if not _chat_store:
        return jsonify({"error": "not initialized"}), 500
    messages = _chat_store.get_conversation(conv_id)
    return jsonify({"conversation_id": conv_id, "messages": messages})


@app.route("/api/chat/conversations/<conv_id>", methods=["DELETE"])
def api_chat_conversation_delete(conv_id):
    if not _chat_store:
        return jsonify({"error": "not initialized"}), 500
    _chat_store.delete_conversation(conv_id)
    return jsonify({"ok": True})


@app.route("/api/chat/conversations/<conv_id>/title", methods=["PUT"])
def api_chat_conversation_title(conv_id):
    if not _chat_store:
        return jsonify({"error": "not initialized"}), 500
    data = request.get_json(silent=True) or {}
    title = data.get("title", "").strip()
    if not title:
        return jsonify({"error": "No title provided"}), 400
    _chat_store.update_title(conv_id, title)
    return jsonify({"ok": True})


@app.route("/api/chat/commands", methods=["GET"])
def api_chat_commands():
    """Return the slash command registry for frontend autocomplete."""
    if not _chat_engine:
        return jsonify({"error": "not initialized"}), 500
    return jsonify({"commands": _chat_engine.get_commands()})


@app.route("/api/chat/command", methods=["POST"])
def api_chat_command():
    """Execute a slash command."""
    if not _chat_engine:
        return jsonify({"error": "not initialized"}), 500
    data = request.get_json(silent=True) or {}
    conv_id = data.get("conversation_id")
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"error": "No command provided"}), 400
    return jsonify(_chat_engine.execute_command(conv_id, command))


@app.route("/api/chat/conversations/<conv_id>/state", methods=["GET"])
def api_chat_conversation_state(conv_id):
    """Get conversation state (focused projects, model, depth)."""
    if not _chat_store:
        return jsonify({"error": "not initialized"}), 500
    return jsonify({"state": _chat_store.get_state(conv_id)})


# ── Activity digest (Oracle UI) ───────────────────────────
@app.route("/api/activity/digest/latest", methods=["GET"])
def api_activity_digest_latest():
    """Most recent SCHEDULED digest (written by the review loop), or null.

    On-demand digests via /api/activity/digest are not persisted here; this
    serves ~/.c3/oracle/activity_digests/latest.json.
    """
    latest = ORACLE_DIR / "activity_digests" / "latest.json"
    try:
        if latest.is_file():
            return Response(latest.read_text(encoding="utf-8"),
                            mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"digest": None, "generated_at": None})


@app.route("/api/activity/digest", methods=["GET"])
def api_activity_digest():
    """Cross-project activity digest for the Oracle UI.

    Query params: date=YYYY-MM-DD, since, until, project (single-project path),
    narrate=true|false. Defaults to today (UTC) across all registered projects.
    """
    if not _activity_reporter:
        return jsonify({"error": "not initialized"}), 500
    narrate = str(request.args.get("narrate", "")).lower() in ("1", "true", "yes", "on")
    return jsonify(_activity_reporter.report(
        date=request.args.get("date", ""),
        since=request.args.get("since", ""),
        until=request.args.get("until", ""),
        project_path=request.args.get("project", ""),
        narrate=narrate,
    ))


# ── Discovery API (external LLM tool surface) ─────────────
@app.route("/api/discovery/tools", methods=["GET"])
def api_discovery_tools():
    """List available discovery tools with their JSON schemas and capability tier."""
    if not _tool_registry:
        return jsonify({"error": "not initialized"}), 500
    return jsonify({"tools": _tool_registry.list_tools(), "tier": _tool_registry.max_tier})


@app.route("/api/discovery/call", methods=["POST"])
def api_discovery_call():
    """Invoke any discovery tool: body {"tool": name, "args": {...}}."""
    if not _tool_registry:
        return jsonify({"error": "not initialized"}), 500
    data = request.get_json(silent=True) or {}
    name = (data.get("tool") or "").strip()
    if not name:
        return jsonify({"error": "missing 'tool'"}), 400
    args = data.get("args") or {}
    if not isinstance(args, dict):
        return jsonify({"error": "'args' must be an object"}), 400
    return jsonify(_tool_registry.call_tool(name, args))


@app.route("/api/discovery/tools/<name>", methods=["POST"])
def api_discovery_call_named(name):
    """Invoke a named tool with the request body as its arguments object."""
    if not _tool_registry:
        return jsonify({"error": "not initialized"}), 500
    args = request.get_json(silent=True) or {}
    if not isinstance(args, dict):
        return jsonify({"error": "request body must be a JSON object of arguments"}), 400
    return jsonify(_tool_registry.call_tool(name, args))


@app.route("/api/discovery/call/stream", methods=["POST"])
def api_discovery_call_stream():
    """Invoke a tool and stream {start, result|error, [DONE]} as SSE."""
    if not _tool_registry:
        return jsonify({"error": "not initialized"}), 500
    data = request.get_json(silent=True) or {}
    name = (data.get("tool") or "").strip()
    args = data.get("args") or {}
    if not name:
        return jsonify({"error": "missing 'tool'"}), 400
    if not isinstance(args, dict):
        return jsonify({"error": "'args' must be an object"}), 400

    def generate():
        yield f"data: {json.dumps({'type': 'start', 'tool': name})}\n\n"
        try:
            result = _tool_registry.call_tool(name, args)
            yield f"data: {json.dumps({'type': 'result', 'tool': name, 'result': result}, default=str)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/api/discovery/openapi.json", methods=["GET"])
def api_discovery_openapi():
    """OpenAPI 3.1 document describing every available discovery tool."""
    if not _tool_registry:
        return jsonify({"error": "not initialized"}), 500
    return jsonify(_tool_registry.openapi_spec(request.host_url))


@app.route("/api/discovery/mcp-info", methods=["GET"])
def api_discovery_mcp_info():
    """Connection details for the MCP transport (URL + auth scheme)."""
    host = _cfg.get("bind_host", "127.0.0.1")
    port = int(_cfg.get("mcp_port", 3332))
    return jsonify({
        "enabled": bool(_cfg.get("mcp_enabled", True)),
        "transport": "http",
        "url": mcp_url(host, port),
        "auth": "bearer",
        "rest_base": request.host_url.rstrip("/") + "/api/discovery",
    })


# ── Discovery API key management (mutations gated by _local_write_guard) ──
def _apikey_status(reveal: bool = False) -> dict:
    """Status payload for the Discovery API token + connection info.

    ``reveal`` controls whether the unmasked token is included. It is False for
    plain ``GET /api/apikey`` status reads (the raw key must never be returned
    over HTTP unauthenticated) and only True when the caller either presents a
    valid Bearer token or just created the key via an explicit generate/rotate
    POST. When False, ``key`` is empty and the snippet carries a placeholder.
    """
    key = api_auth.peek()
    host = _cfg.get("bind_host", "127.0.0.1")
    port = int(_cfg.get("mcp_port", 3332))
    url = mcp_url(host, port)
    rest_base = request.host_url.rstrip("/") + "/api/discovery"
    masked = ""
    if key:
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 12 else "••••"
    snippet_token = key if (key and reveal) else "<token>"
    snippet = {
        "mcpServers": {
            "c3-oracle": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {snippet_token}"},
            }
        }
    }
    return {
        "exists": bool(key),
        "key": (key or "") if reveal else "",
        "masked": masked,
        "require_auth": bool(_cfg.get("api_require_auth", True)),
        "api_enabled": bool(_cfg.get("api_enabled", True)),
        "mcp_enabled": bool(_cfg.get("mcp_enabled", True)),
        "mcp_url": url,
        "rest_base": rest_base,
        "openapi_url": rest_base + "/openapi.json",
        "snippet": snippet,
    }


@app.route("/api/apikey", methods=["GET"])
def api_apikey_get():
    """Return Discovery API token status + connection info.

    The unmasked token is only included when the caller presents a valid
    Bearer token or the dashboard session cookie; otherwise only the masked
    form is returned (never the raw key over HTTP unauthenticated).
    """
    try:
        reveal = verify_api_key(
            extract_bearer(request.headers.get("Authorization"))
        ) or local_session.verify(request.cookies.get(local_session.COOKIE_NAME))
        return jsonify(_apikey_status(reveal=reveal))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/apikey/generate", methods=["POST"])
def api_apikey_generate():
    """Create a token if none exists; returns the current status.

    Reveals the key in the response: this is an explicit local creation action,
    so the dashboard can show/copy the just-created token once.
    """
    try:
        api_auth.get_or_create_key()
        return jsonify(_apikey_status(reveal=True))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/apikey/rotate", methods=["POST"])
def api_apikey_rotate():
    """Replace the token with a fresh one (revealed once for copy)."""
    try:
        api_auth.rotate()
        return jsonify(_apikey_status(reveal=True))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/apikey/clear", methods=["POST"])
def api_apikey_clear():
    """Delete the stored token."""
    try:
        api_auth.clear()
        return jsonify(_apikey_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Error handlers ────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "method not allowed"}), 405


# ── Startup ───────────────────────────────────────────────
def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _find_free_port(start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        if _port_free(port):
            return port
    raise RuntimeError(f"No free port found near {start}")


def _is_oracle_running(port: int) -> bool:
    try:
        url = f"http://127.0.0.1:{port}/api/health"
        with urllib.request.urlopen(url, timeout=1) as r:
            data = json.loads(r.read())
            return data.get("service") == "c3-oracle"
    except Exception:
        return False


def _force_utf8_console() -> None:
    """Make stdout/stderr UTF-8 so banner/log output can't crash on legacy
    Windows code pages (cp1252 raises UnicodeEncodeError on chars like '→')."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def run_oracle(port: int = None, open_browser: bool = None):
    """Main entry point for Oracle server."""
    _force_utf8_console()
    _init_services()

    cfg = load_config()
    dedicated_port = port if port is not None else cfg.get("port", 3331)
    if open_browser is None:
        open_browser = cfg.get("auto_open_browser", True)

    # Single-instance check
    if not _port_free(dedicated_port):
        if _is_oracle_running(dedicated_port):
            url = f"http://localhost:{dedicated_port}"
            print(f"Oracle already running at {url}")
            if open_browser:
                webbrowser.open(url)
            return
        actual_port = _find_free_port(dedicated_port + 1)
        print(f"Warning: port {dedicated_port} in use. Using {actual_port} instead.")
    else:
        actual_port = dedicated_port

    # Set up logging
    ORACLE_DIR.mkdir(parents=True, exist_ok=True)
    log_level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(ORACLE_DIR / "oracle.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    url = f"http://localhost:{actual_port}"
    print(f"Oracle Memory Agent  →  {url}  (model: {cfg.get('model', 'gemma4:31b-cloud')})")

    # Start review agent if enabled
    if cfg.get("review_enabled", True) and _agent:
        _agent.start()
        atexit.register(_agent.stop)

    # Start the discovery MCP server on its own loopback port if enabled.
    if cfg.get("mcp_enabled", True) and _tool_registry is not None:
        try:
            from oracle.services.api_auth import get_or_create_key

            get_or_create_key()  # ensure a Bearer key exists for clients
            mcp_host = cfg.get("bind_host", "127.0.0.1")
            mcp_p = int(cfg.get("mcp_port", 3332))
            start_mcp_thread(
                _tool_registry,
                host=mcp_host,
                port=mcp_p,
                version=_c3_version(),
                require_auth=cfg.get("api_require_auth", True),
                allowed_hosts=cfg.get("allowed_hosts"),
            )
            print(f"Oracle Discovery MCP  →  {mcp_url(mcp_host, mcp_p)}  (auth: bearer)")
        except Exception as e:
            logging.getLogger("oracle").warning("MCP server not started: %s", e)

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    app.run(host=cfg.get("bind_host", "127.0.0.1"), port=actual_port, debug=False, use_reloader=False)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Oracle Memory Agent for C3")
    parser.add_argument("--port", type=int, default=None, help="Server port (default: 3331)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    args = parser.parse_args()
    run_oracle(port=args.port, open_browser=not args.no_browser)
