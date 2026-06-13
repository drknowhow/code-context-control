"""Hybrid configuration loader for C3 v2.3 features.

Loads the "hybrid" section from .c3/config.json with sensible defaults.
All three tiers (output filter, router, SLTM) can be independently disabled.
"""
import json
from pathlib import Path

DEFAULTS = {
    "ollama_base_url": "http://localhost:11434",
    "HYBRID_DISABLE_TIER1": False,   # Output filter
    "HYBRID_DISABLE_TIER2": False,   # Router
    "HYBRID_DISABLE_SLTM": False,    # Vector memory
    "show_context_nudges": True,     # Append budget nudge when over threshold
    "prepend_notifications": True,   # Prepend agent notifications to tool/chat responses
    # Model assignments
    "embed_model": "nomic-embed-text",
    "filter_model": "gemma3n:latest",
    "summary_model": "gemma3n:latest",
    "simple_qa_model": "deepseek-r1:1.5b",
    "complex_model": "llama3.2:3b",
    # Router params
    "router_log_threshold": 500,     # tokens: route to log_summary
    "router_simple_threshold": 100,  # tokens: route to simple_qa
    "router_allow_model_fallback": True,
    "router_fallback_models": [],    # Optional ordered model fallbacks
    "router_retry_on_empty": True,   # Retry with fallback when first model returns no response
    "validate_timeout_seconds": 12,  # Hard timeout for c3_validate native syntax checks
    # Filter params
    "filter_llm_threshold": 500,     # tokens: trigger LLM pass
    # SLTM params
    "sltm_alpha": 0.5,               # TF-IDF weight in hybrid search (1-alpha = vector weight)
    "sltm_min_score": 0.3,           # Minimum similarity threshold for VectorStore search
    # Auto-memory: background learning from tool calls
    "auto_memory": {
        "enabled": True,             # Set False to disable all auto-extraction
    },
    # Local RAG Pipeline: auto-retrieve project docs on session start
    "rag": {
        "enabled": True,
        "max_precontext_tokens": 400,
    },
    # Edit Ledger: auto-log Edit/Write operations
    "edit_ledger": {
        "enabled": True,
        "tracking_level": "standard",  # "minimal", "standard", "detailed"
        "auto_tag": True,              # Auto-tag edits from hooks
    },
    # c3_agent compound workflows
    "agent_workflows": {
        "enabled": True,
        "prefetch_max_files": 3,          # Max files to auto-compress on search prefetch
        "batch_validate_max_files": 10,   # Max files in a single batch validate call
        "compound_max_compress": 5,       # Max files to compress in compound workflows
        "delegate_in_workflows": True,    # Allow Ollama delegation in compound workflows
    },
}


def load_hybrid_config(project_path: str) -> dict:
    """Load hybrid config from .c3/config.json, merged with defaults."""
    config_file = Path(project_path) / ".c3" / "config.json"
    hybrid = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
            hybrid = data.get("hybrid", {})
        except Exception:
            pass
    merged = {**DEFAULTS, **hybrid}
    if "show_savings_footer" not in hybrid and "SHOW_SAVINGS_SUMMARY" in hybrid:
        merged["show_savings_footer"] = bool(hybrid.get("SHOW_SAVINGS_SUMMARY"))
    if "validate_timeout_seconds" not in hybrid and "validate_review_timeout_seconds" in hybrid:
        merged["validate_timeout_seconds"] = int(hybrid.get("validate_review_timeout_seconds") or DEFAULTS["validate_timeout_seconds"])
    merged["SHOW_SAVINGS_SUMMARY"] = bool(merged.get("show_savings_footer", False))
    return merged


# ─── Agent Defaults ────────────────────────────────────────

FILE_MEMORY_DEFAULTS = {
    "enabled": True,
    "max_tracked_files": 200,
    "summary_model": "gemma3n:latest",
    "summary_max_tokens": 80,
    "nudge_threshold": 500,        # Min chars in Read result to trigger nudge
    "auto_index_on_read": True,    # Auto-index files when Read hook fires
}


# Keep in sync with services/agents.py:create_agents factory defaults.
# Core (always-on by default): IndexStaleness, FileMemory, EditLedgerEnricher.
# Everything else defaults off; users opt in via the UI / config.
AGENT_DEFAULTS = {
    "IndexStaleness": {
        "enabled": True, "interval": 60, "use_ai": False,
        "ai_model": "gemma3n:latest", "warn_threshold": 5, "rebuild_threshold": 15,
    },
    "MemoryPruner": {
        "enabled": False, "interval": 300, "use_ai": True,
        "ai_model": "gemma3n:latest", "embed_model": "nomic-embed-text",
        "similarity_threshold": 0.8,
    },
    "ClaudeMdDrift": {
        "enabled": False, "interval": 120, "use_ai": False, "ai_model": "gemma3n:latest",
    },
    "SessionInsight": {
        "enabled": False, "interval": 600, "use_ai": True,
        "ai_model": "gemma3n:latest", "min_tool_calls": 10,
    },
    "AutonomyPlanner": {
        "enabled": False, "interval": 240, "use_ai": True,
        "ai_model": "gemma3n:latest", "lookback_tool_calls": 30,
        "cooldown_seconds": 600, "min_signal_score": 2, "max_actions": 3,
    },
    "ClaudeMdUpdater": {
        "enabled": False, "interval": 900, "use_ai": True,
        "ai_model": "gemma3n:latest", "auto_apply": True,
        "min_facts_for_promote": 2,
    },
    "FileMemory": {
        "enabled": True, "interval": 120, "use_ai": False,
        "ai_model": "gemma3n:latest", "max_files_per_cycle": 5,
    },
    "DelegateCoach": {
        "enabled": False, "interval": 300, "use_ai": False,
        "ai_model": "gemma3n:latest", "lookback_lines": 100,
    },
    "KeyFileVersion": {
        "enabled": False, "interval": 180, "use_ai": False,
        "ai_model": "gemma3n:latest", "agent_target": "current",
        "max_changes_per_notice": 4,
    },
    "EditLedgerEnricher": {
        "enabled": True, "interval": 10, "use_ai": False,
    },
    "BranchWatch": {
        "enabled": True, "interval": 30, "max_queue": 200,
    },
    "VersionCheck": {
        "enabled": True, "interval": 3600, "check_every_hours": 24,
    },
}


# ─── Proxy Defaults ────────────────────────────────────────

PROXY_DEFAULTS = {
    "always_visible": ["core"],      # Categories always visible when proxy filtering is enabled
    "max_tools": 12,
    "filter_tools": True,            # True = dynamic filtering by category, False = show all
    "use_slm": True,
    "slm_model": "gemma3n:latest",
    "context_window_size": 10,
    "inject_context_summary": False,
    "PROXY_DISABLE": False,
}


def load_proxy_config(project_path: str) -> dict:
    """Load proxy config from .c3/config.json, merged with defaults."""
    config_file = Path(project_path) / ".c3" / "config.json"
    proxy = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
            proxy = data.get("proxy", {})
        except Exception:
            pass
    merged = {**PROXY_DEFAULTS, **proxy}
    always_visible = merged.get("always_visible", ["core"])
    if isinstance(always_visible, str):
        always_visible = [always_visible]
    merged["always_visible"] = always_visible or ["core"]
    return merged


def load_mcp_config(project_path: str) -> dict:
    """Load MCP mode config from .c3/config.json, merged with defaults."""
    config_file = Path(project_path) / ".c3" / "config.json"
    mcp = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
            mcp = data.get("mcp", {})
        except Exception:
            pass
    mode = str(mcp.get("mode", "direct") or "direct").strip().lower()
    if mode not in {"direct", "proxy"}:
        mode = "direct"
    return {"mode": mode}


# ─── Delegate Defaults ────────────────────────────────────

DELEGATE_DEFAULTS = {
    "enabled": True,
    "preferred_model": "",           # Empty = auto-select per task type
    "allow_model_fallback": True,    # If preferred/default missing, pick best available local model
    "fallback_models": [],           # Optional ordered list of fallback model names
    "max_tokens": 512,
    "temperature": 0.3,
    "auto_compress": True,           # Auto-compress file_path content as context
    "auto_search": True,             # Auto-search index for 'ask' tasks
    "auto_vector_search": True,      # Auto-search vector store if available
    "auto_activity_log": True,       # Auto-inject recent activity for diagnose
    "search_top_k": 2,
    "max_context_tokens": 1400,
    # Delegation threshold policy
    "threshold_enabled": True,       # Token-saving mode: delegate by default once threshold is met
    "threshold_min_total_tokens": 60,
    "threshold_task_types": ["ask", "explain", "summarize", "improve", "docstring"],
    "threshold_force_task_types": ["diagnose", "review", "test"],
    # Per-task model overrides (empty = use preferred_model or task default)
    "summarize_model": "",
    "explain_model": "",
    "docstring_model": "",
    "review_model": "",
    "ask_model": "",
    "test_model": "",
    "diagnose_model": "",
    "improve_model": "",
    # Codex CLI integration (cloud delegate backend)
    "codex_enabled": True,               # Master switch — enable Codex as delegate backend
    "codex_default_model": "gpt-5.3-codex-spark",  # Default Codex model
    "codex_default_sandbox": "read-only", # Default sandbox: read-only, workspace-write, danger-full-access
    "codex_reasoning_effort": "high",     # xhigh, high, medium, low
    "codex_timeout": 90,                  # Subprocess timeout in seconds (idle watchdog kills MCP hangs at 20s)
    "codex_max_context_tokens": 4000,     # Context truncation limit for Codex calls
    "codex_task_types": ["review", "diagnose", "improve", "test"],  # Task types auto-routed to Codex
    "codex_verify_edits": False,          # EditLedgerEnricher sends diffs to Codex for review
    "codex_memory_bridge": True,          # Auto-extract findings from Codex into c3_memory
    # Gemini CLI integration (cloud delegate backend)
    "gemini_enabled": True,               # Master switch — enable Gemini as delegate backend
    "gemini_default_model": "gemini-2.5-flash",  # Default Gemini model
    "gemini_timeout": 45,                 # Subprocess timeout in seconds (idle watchdog kills MCP hangs at 15s)
    "gemini_max_context_tokens": 8000,    # Context truncation limit (Gemini has large context windows)
    "gemini_task_types": ["review", "diagnose", "improve", "test"],  # Tasks auto-routed to Gemini
    "gemini_memory_bridge": True,         # Auto-extract findings from Gemini into c3_memory
}


def load_delegate_config(project_path: str) -> dict:
    """Load delegate config from .c3/config.json, merged with defaults."""
    config_file = Path(project_path) / ".c3" / "config.json"
    delegate = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
            delegate = data.get("delegate", {})
        except Exception:
            pass
    return {**DELEGATE_DEFAULTS, **delegate}


def load_agent_config(project_path: str) -> dict:
    """Load agent config from .c3/config.json, merged with AGENT_DEFAULTS."""
    config_file = Path(project_path) / ".c3" / "config.json"
    overrides = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
            overrides = data.get("agents", {})
        except Exception:
            pass
    # Merge per-agent: defaults ← overrides
    result = {}
    for name, defaults in AGENT_DEFAULTS.items():
        result[name] = {**defaults, **overrides.get(name, {})}
    return result


# ─── Bitbucket Defaults (v2.30.0) ──────────────────────────

BITBUCKET_DEFAULTS = {
    # Active account pointer; both fields empty when no login has run.
    "active": {"base_url": "", "username": ""},
    # Non-secret index of accounts whose PATs live in the OS keyring.
    "accounts": [],
    # Default project key + repo slug for tool calls when not provided.
    "default_project": "",
    "default_repo": "",
    # Set False for self-signed enterprise certificates.
    "verify_tls": True,
}


def load_bitbucket_config(project_path: str) -> dict:
    """Load Bitbucket config from .c3/config.json, merged with defaults."""
    config_file = Path(project_path) / ".c3" / "config.json"
    overrides = {}
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
            overrides = data.get("bitbucket", {})
        except Exception:
            pass
    return {**BITBUCKET_DEFAULTS, **overrides}
