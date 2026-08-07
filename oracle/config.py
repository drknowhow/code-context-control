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
    "api_rate_limit_per_min": 60,   # per-caller tool-call budget; 0 disables
    "api_rate_burst": 0,            # bucket size; 0 = quarter-minute of budget
    "api_audit_enabled": True,      # JSONL audit line per discovery tool call
    "mcp_enabled": True,            # start FastMCP HTTP/SSE discovery server
    "mcp_port": 3332,               # discovery MCP transport port (loopback)
    "mobile_api_enabled": True,     # /api/mobile/* companion-app gateway (Bearer on ALL methods)
    # ── Mobile security surface (credentials + Access Guard) ──
    # The mobile gateway is the first NETWORK-REACHABLE surface for either
    # subsystem; every other one is loopback-only. Each switch removes both the
    # routes (404) and the capability from /api/mobile/info, so a client gates
    # its UI off the capability list rather than probing for 404s.
    "mobile_credentials_enabled": True,   # read: list / describe / check
    "mobile_credentials_write": True,     # set / delete / metadata update
    # Raising agent_readable makes a stored secret readable INTO model context
    # and transcripts, and cannot be undone for anything already disclosed.
    # Lowering it is always allowed; raising it is off by default because a
    # typed confirmation stops fat-fingers, not a leaked Bearer token.
    "mobile_creds_agent_readable_raise": False,
    "mobile_access_enabled": True,        # read: rules / check / denials / enforcement
    "mobile_access_write": True,          # rule + mask + enforcement mutations
    # A global-scope access rule applies to EVERY project on this machine —
    # blast radius beyond the projects the token can even enumerate.
    "mobile_access_global_scope": False,
    # ── Override Requests (docs/override-requests.md §3.2) ──
    # The approval inbox. `override` serves the read routes (list / one /
    # policy); `override_write` is what turns a phone tap into a real grant,
    # so it is the switch that matters. Both default ON to match the access
    # pair — the feature is already off by default one layer down, in the
    # PROJECT policy (`override.enabled: false`, every layer false), which is
    # what decides whether a request can exist at all.
    "mobile_override_enabled": True,      # read: request list / one / policy
    "mobile_override_write": True,        # decide / mute / policy edit
    # Second, tighter budget for security mutations, on top of the shared
    # api_rate_limit_per_min bucket. 0 = share the main bucket only.
    "mobile_security_rate_limit_per_min": 12,
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
