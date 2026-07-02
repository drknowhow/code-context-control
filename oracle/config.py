"""Oracle configuration loader."""

import json
from pathlib import Path

ORACLE_DIR = Path.home() / ".c3" / "oracle"

CONFIG_FILE = ORACLE_DIR / "config.json"

DEFAULTS = {
    "port": 3331,
    # ── Discovery API (external LLM tool surface, v2.32.0) ──
    "bind_host": "127.0.0.1",       # loopback only by default (was 0.0.0.0)
    "api_enabled": True,            # expose /api/discovery/* REST surface
    "api_require_auth": True,       # require Bearer token on /api/discovery/*
    "api_max_tier": "action",       # cap exposed tools: "read" | "action"
    "mcp_enabled": True,            # start FastMCP HTTP/SSE discovery server
    "mcp_port": 3332,               # discovery MCP transport port (loopback)
    "ollama_base_url": "https://ollama.com",
    "ollama_api_key": "",
    "llm_cache_ttl_sec": 86400,     # disk cache TTL for generate() responses
    "model": "gemma4:31b-cloud",
    "hub_url": "http://localhost:3330",
    "scanner_ttl_seconds": 20,
    "review_interval_seconds": 1800,
    "review_enabled": True,
    # ── Scheduled activity digest (runs inside the review loop) ──
    "digest_enabled": False,          # off = current behavior (on-demand only)
    "digest_interval_seconds": 86400, # daily cadence once enabled
    "digest_narrate": False,          # LLM prose costs a cloud call; opt-in
    "digest_notify_file": "",         # "" = disabled; else JSONL sink path
    "digest_retention_days": 14,      # prune stored digests
    "auto_open_browser": True,
    "theme": "dark",
    "ui_last_tab": "chat",          # persisted UI preference (hub parity)
    "max_facts_per_analysis": 100,
    "insight_confidence_threshold": 0.5,
    "log_level": "INFO",
    "federated_graph_ttl_sec": 3600,
    "cross_sim_threshold": 0.75,
    "cross_max_facts_per_project": 200,
    "cross_top_k_neighbors": 3,
    "embedding_model": "nomic-embed-text",
    "agents": [
        {
            "id": "architect",
            "name": "Architect",
            "description": "Expert in system architecture, design patterns, and cross-project structure. Best for high-level analysis.",
            "system_prompt": "You are the Architect. Focus on structural integrity, design patterns, and the big picture. Provide high-level recommendations before diving into code.",
            "model": "gemma4:31b-cloud",
            "backend": "ollama",
            "active": True
        },
        {
            "id": "code_explorer",
            "name": "Code Explorer",
            "description": "Specializes in deep code analysis, bug hunting, and tracing execution paths.",
            "system_prompt": "You are the Code Explorer. Be incredibly precise, cite specific lines of code, and focus on the technical implementation details. Trace logic thoroughly.",
            "model": "gemma4:31b-cloud",
            "backend": "ollama",
            "active": True
        },
        {
            "id": "memory_analyst",
            "name": "Memory Analyst",
            "description": "Focuses on analyzing project memory, facts, and insights.",
            "system_prompt": "You are the Memory Analyst. Rely heavily on memory tools and facts to spot trends. Connect current issues to past context.",
            "model": "gemma4:31b-cloud",
            "backend": "ollama",
            "active": True
        }
    ],
}


def load_config() -> dict:
    """Load Oracle config, merged with defaults."""
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict):
    """Write Oracle config to disk."""
    ORACLE_DIR.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update(cfg)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
