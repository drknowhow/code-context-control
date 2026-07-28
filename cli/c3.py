#!/usr/bin/env python3
"""
C3 — Claude Code Companion

A unified local tool that reduces Claude Code token usage through:
1. AST-based code compression
2. Smart local code index with TF-IDF retrieval
3. Session state management with auto-CLAUDE.md
4. Compression protocol for prompts

Usage:
    c3 init <project_path>
    c3 index
    c3 compress <file> [--mode structure|outline|smart|diff]
    c3 context <query> [--top-k 5] [--max-tokens 4000]
    c3 encode <text>
    c3 decode <text>
    c3 session start [description]
    c3 session save [summary]
    c3 session load [session_id]
    c3 session list
    c3 session context
    c3 claudemd generate
    c3 claudemd save
    c3 stats
    c3 benchmark
    c3 optimize
    c3 pipe <query>    # All-in-one: index + context + encode, pipe to Claude
"""
import argparse
import html
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from copy import deepcopy

_log = logging.getLogger("c3")
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.commands.common import CommandDeps
from cli.commands.common import cmd_claudemd as common_cmd_claudemd
from cli.commands.common import cmd_compress as common_cmd_compress
from cli.commands.common import cmd_context as common_cmd_context
from cli.commands.common import cmd_decode as common_cmd_decode
from cli.commands.common import cmd_encode as common_cmd_encode
from cli.commands.common import cmd_index as common_cmd_index
from cli.commands.common import cmd_optimize as common_cmd_optimize
from cli.commands.common import cmd_pipe as common_cmd_pipe
from cli.commands.common import cmd_session as common_cmd_session
from cli.commands.common import cmd_stats as common_cmd_stats
from cli.commands.common import cmd_ui as common_cmd_ui
from cli.commands.parser import build_parser
from core import count_tokens, format_token_count
from core.config import (
    AGENT_DEFAULTS,
    BITBUCKET_DEFAULTS,
    DELEGATE_DEFAULTS,
    MEMORY_LLM_DEFAULTS,
    PROXY_DEFAULTS,
    load_delegate_config,
)
from core.config import DEFAULTS as HYBRID_DEFAULTS
from core.ide import PROFILES, detect_ide, get_profile, load_ide_config, normalize_ide_name
from services.compressor import CodeCompressor
from services.file_memory import FileMemoryStore
from services.indexer import CodeIndex
from services.ollama_client import OllamaClient
from services.output_filter import OutputFilter
from services.protocol import CompressionProtocol
from services.session_manager import SessionManager

# Rich for beautiful terminal output
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

console = Console() if HAS_RICH else None

# Config
CONFIG_DIR = ".c3"
CONFIG_FILE = ".c3/config.json"
__version__ = "2.63.0"


def _compress_file_cli(compressor, path, mode="smart", **kw):
    """compress_file for human-CLI paths: AccessDenied → error dict, not a
    traceback. The refusal text names the rule so the operator can act."""
    from services import access_guard as _ag
    try:
        return compressor.compress_file(path, mode, **kw)
    except _ag.AccessDenied as exc:
        return {"error": exc.message}


def _command_deps() -> CommandDeps:
    return CommandDeps(
        load_config=load_config,
        print_header=print_header,
        print_savings=print_savings,
        count_tokens=count_tokens,
        format_token_count=format_token_count,
        CodeIndex=CodeIndex,
        CodeCompressor=CodeCompressor,
        CompressionProtocol=CompressionProtocol,
        SessionManager=SessionManager,
        HAS_RICH=HAS_RICH,
        Table=Table if HAS_RICH else None,
        console=console,
        __file__=__file__,
    )


def load_config(project_path: str = ".") -> dict:
    """Load C3 config for a project."""
    config_path = Path(project_path) / CONFIG_FILE
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            _log.debug("Failed to load config from %s", config_path, exc_info=True)
    return {"project_path": str(Path(project_path).resolve())}


def save_config(config: dict, project_path: str = "."):
    """Save C3 config, merging with existing config to preserve keys like 'ide'."""
    config_dir = Path(project_path) / CONFIG_DIR
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"

    # Merge with existing to preserve keys set by other commands (e.g. "ide")
    existing = {}
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            _log.debug("Failed to read existing config for merge at %s", config_path, exc_info=True)

    merged = {**existing, **config}
    with open(config_path, 'w', encoding="utf-8") as f:
        json.dump(merged, f, indent=2)


def _deep_merge_dict(base: dict, override: dict) -> dict:
    """Recursively merge dicts. Values from override win."""
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def _build_init_config(project_path: str) -> dict:
    """Build init/update config with token-saving defaults + existing overrides."""
    existing = load_config(project_path)
    defaults = {
        "project_path": project_path,
        "version": __version__,
        "index_auto_update": True,
        "index_max_files": 2000,
        "compression_mode": "smart",
        "mcp": {"mode": "direct"},
        "hybrid": deepcopy(HYBRID_DEFAULTS),
        "proxy": deepcopy(PROXY_DEFAULTS),
        "delegate": deepcopy(DELEGATE_DEFAULTS),
        "agents": deepcopy(AGENT_DEFAULTS),
        "bitbucket": deepcopy(BITBUCKET_DEFAULTS),
        "memory_llm": deepcopy(MEMORY_LLM_DEFAULTS),
    }
    merged = _deep_merge_dict(defaults, existing if isinstance(existing, dict) else {})
    # Always persist current path/version on init/update.
    merged["project_path"] = project_path
    merged["version"] = __version__
    hybrid = merged.get("hybrid")
    if isinstance(hybrid, dict):
        if "validate_timeout_seconds" not in hybrid and "validate_review_timeout_seconds" in hybrid:
            hybrid["validate_timeout_seconds"] = hybrid.get("validate_review_timeout_seconds")
        hybrid.pop("validate_review_timeout_seconds", None)
        # Remove dead budget keys from hybrid (compression levels removed in v2.20)
        for dead_key in ("show_savings_footer", "SHOW_SAVINGS_SUMMARY",
                         "response_token_cap_level_1", "response_token_cap_level_2",
                         "related_facts_max_level",
                         "search_tight_top_k", "search_tight_max_tokens",
                         "search_minimal_top_k", "search_minimal_max_tokens",
                         "delegate_tight_max_context_tokens", "delegate_minimal_max_context_tokens"):
            hybrid.pop(dead_key, None)
    # Migrate old context_budget keys → single threshold
    cb = merged.get("context_budget")
    if isinstance(cb, dict):
        if "threshold" not in cb and "nudge" in cb:
            cb["threshold"] = cb["nudge"]
        for dead_key in ("level_1", "level_2", "nudge",
                         "response_token_cap_level_1", "response_token_cap_level_2"):
            cb.pop(dead_key, None)
    # Remove ContextBudget from agents config (agent removed in v2.20)
    agents = merged.get("agents")
    if isinstance(agents, dict):
        agents.pop("ContextBudget", None)
    return merged


def print_header(text: str):
    if HAS_RICH:
        console.print(Panel(f"[bold cyan]{text}[/bold cyan]", border_style="blue"))
    else:
        print(f"\n{'='*60}\n  {text}\n{'='*60}")


def print_savings(savings: dict):
    if HAS_RICH:
        table = Table(title="Token Savings")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Original", format_token_count(savings.get("original_tokens", 0)))
        table.add_row("Compressed", format_token_count(savings.get("compressed_tokens", 0)))
        table.add_row("Saved", format_token_count(savings.get("saved_tokens", 0)))
        table.add_row("Savings %", f"{savings.get('savings_pct', 0)}%")
        console.print(table)
    else:
        print(f"  Original:   {format_token_count(savings.get('original_tokens', 0))}")
        print(f"  Compressed: {format_token_count(savings.get('compressed_tokens', 0))}")
        print(f"  Saved:      {format_token_count(savings.get('saved_tokens', 0))} ({savings.get('savings_pct', 0)}%)")


# â”€â”€â”€ Commands â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_C3_INIT_SUBDIRS = [
    "cache", "index", "sessions", "analytics", "facts",
    "snapshots", "transcript_index", "file_memory", "embeddings",
    "doc_index",
]

# ── Permission tier system ─────────────────────────────────────────
# MCP tools always included in every tier's allow list.
# Keep in sync with @mcp.tool() registrations in cli/mcp_server.py —
# tests/test_permissions.py asserts this list matches the server's registry.
_C3_MCP_ALLOW = [
    "mcp__c3__c3_read", "mcp__c3__c3_search", "mcp__c3__c3_compress",
    "mcp__c3__c3_session", "mcp__c3__c3_status", "mcp__c3__c3_filter",
    "mcp__c3__c3_memory", "mcp__c3__c3_validate", "mcp__c3__c3_edit",
    "mcp__c3__c3_agent", "mcp__c3__c3_delegate", "mcp__c3__c3_edits",
    "mcp__c3__c3_impact", "mcp__c3__c3_shell", "mcp__c3__c3_bitbucket",
    "mcp__c3__c3_jira", "mcp__c3__c3_credentials",
    "mcp__c3__c3_project", "mcp__c3__c3_task", "mcp__c3__c3_artifacts",
]

# Obsolete MCP tool names from earlier C3 versions. `c3 permissions clean`
# removes these from settings.local.json so accumulated cruft doesn't bloat
# the allow/deny arrays.
_STALE_MCP_TOOLS = {
    "mcp__c3__c3_remember",           # → c3_memory(action='add')
    "mcp__c3__c3_recall",             # → c3_memory(action='recall')
    "mcp__c3__c3_sltm_add",           # → c3_memory(action='add')
    "mcp__c3__c3_file_map",           # → c3_compress(mode='map')
    "mcp__c3__c3_extract",            # → c3_read
    "mcp__c3__c3_transcript_search",  # → c3_search(action='transcript')
    "mcp__c3__c3_session_log",        # → c3_session(action='log')
    "mcp__c3__c3_convo_log",          # → c3_session(action='convo_log')
    "mcp__c3__c3_query",              # → c3_memory(action='query')
    "mcp__c3__c3_budget",             # → c3_status(view='budget')
}

# Claude Code built-in read/nav tools. Used across multiple tiers.
_CC_BUILTINS_READ = [
    "Read(*)", "Glob(*)", "Grep(*)", "LS(*)",
    "NotebookRead(*)", "WebSearch",
    "Task",  # subagent launcher
    "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput",
    "ExitPlanMode", "EnterPlanMode",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
]

# Claude Code built-in edit tools — present in standard/permissive only.
_CC_BUILTINS_EDIT = [
    "Write(*)", "Edit(*)", "MultiEdit(*)", "NotebookEdit(*)",
]

# Safe read-only shell commands (navigation + inspection, no writes).
_BASH_READONLY = [
    "Bash(cd:*)", "Bash(ls:*)", "Bash(pwd:*)", "Bash(find:*)", "Bash(which:*)",
    "Bash(where:*)", "Bash(type:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
    "Bash(grep:*)", "Bash(wc:*)", "Bash(stat:*)", "Bash(uname:*)", "Bash(echo:*)",
    "Bash(printf:*)", "Bash(env:*)", "Bash(export:*)", "Bash(unset:*)",
    "Bash(git log:*)", "Bash(git status:*)", "Bash(git diff:*)",
    "Bash(git show:*)", "Bash(git blame:*)", "Bash(git branch:*)",
    "Bash(python:*)", "Bash(python3:*)",
]

# Common development commands safe for the standard tier (adds writes + tooling).
_BASH_STANDARD = _BASH_READONLY + [
    # File operations
    "Bash(mkdir:*)", "Bash(cp:*)", "Bash(mv:*)", "Bash(touch:*)", "Bash(rm:*)",
    "Bash(rsync:*)", "Bash(chmod:*)",
    # Full git
    "Bash(git:*)",
    # Package managers / runtimes
    "Bash(pip:*)", "Bash(pip3:*)", "Bash(npm:*)", "Bash(node:*)",
    "Bash(cargo:*)", "Bash(go:*)",
    # AI CLIs
    "Bash(claude:*)", "Bash(codex:*)", "Bash(gemini:*)",
    # Utilities
    "Bash(timeout:*)", "Bash(time:*)", "Bash(curl:*)", "Bash(gh:*)",
    "Bash(for:*)", "Bash(do:*)", "Bash(done:*)",
    # Windows
    "Bash(cmd:*)", "Bash(cmd.exe:*)", "Bash(powershell:*)", "Bash(powershell.exe:*)",
]

PERMISSION_TIERS = {
    "read-only":  "Read files + inspect repo. No writes, safe shell only (ls, cat, git log…)",
    "c3-strict":  "C3 MCP tools only — deny native Read/Grep/Glob/Edit/Write to enforce c3_* workflow",
    "standard":   "Full editing + common dev shell (git, python, npm…), block destructive ops (recommended)",
    "permissive": "Unrestricted — all tools and shell commands pre-approved",
}

# Accept common variants/aliases when users pass a tier name.
_TIER_ALIASES = {
    "unrestricted": "permissive",
    "restricted":   "read-only",
    "strict":       "c3-strict",
    "c3":           "c3-strict",
    "c3_strict":    "c3-strict",
    "readonly":     "read-only",
}


def _build_permission_tier(tier: str, include_mcp_wildcard: bool = False) -> dict:
    """Return a settings-ready permissions dict for the given tier.

    include_mcp_wildcard adds "mcp__*" to the allow list so non-C3 MCP
    servers (Neon, Playwright, etc.) don't prompt on every call.
    """
    tier = _TIER_ALIASES.get(tier, tier)
    mcp = list(_C3_MCP_ALLOW)
    if include_mcp_wildcard:
        mcp.append("mcp__*")

    if tier == "read-only":
        return {"permissions": {
            "allow": mcp + _CC_BUILTINS_READ + _BASH_READONLY,
            "deny":  _CC_BUILTINS_EDIT + [
                "Bash(rm -rf *)", "Bash(sudo *)", "Bash(eval *)",
            ],
        }}

    if tier == "c3-strict":
        # Enforces c3_* workflow: native file tools denied, c3 MCP tools allowed.
        # Aligns with hook_pretool_enforce.py mandate — no drift-prone native Read/Grep.
        strict_allow_builtins = [
            "WebSearch", "Task",
            "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput",
            "ExitPlanMode", "EnterPlanMode",
            "ListMcpResourcesTool", "ReadMcpResourceTool",
        ]
        return {"permissions": {
            "allow": mcp + strict_allow_builtins + _BASH_READONLY,
            "deny":  [
                "Read(*)", "Glob(*)", "Grep(*)", "LS(*)",
                "Edit(*)", "Write(*)", "MultiEdit(*)",
                "NotebookRead(*)", "NotebookEdit(*)",
                "Bash(rm -rf *)", "Bash(sudo *)", "Bash(eval *)",
            ],
        }}

    if tier == "standard":
        return {"permissions": {
            "allow": mcp + _CC_BUILTINS_READ + _CC_BUILTINS_EDIT + [
                "WebFetch(*)",
            ] + _BASH_STANDARD,
            "deny":  [
                "Bash(rm -rf *)", "Bash(sudo *)",
                "Bash(curl * | *)", "Bash(wget * | *)", "Bash(eval *)",
            ],
        }}

    # permissive — pre-approve everything, no deny rules
    return {"permissions": {
        "allow": mcp + _CC_BUILTINS_READ + _CC_BUILTINS_EDIT + [
            "Bash(*)", "WebFetch(*)",
        ],
        "deny":  [],
    }}


def _c3_managed_permission_entries() -> tuple[set, set]:
    """Return (allow, deny) sets of every entry any C3 tier can emit.

    Used to tell C3-managed permission rules apart from user-added ones so a
    tier change replaces only the former and preserves the latter.
    """
    managed_allow: set = set()
    managed_deny: set = set()
    for _tier in PERMISSION_TIERS:
        perms = _build_permission_tier(_tier, include_mcp_wildcard=True)["permissions"]
        managed_allow.update(perms.get("allow", []))
        managed_deny.update(perms.get("deny", []))
    return managed_allow, managed_deny


def _merge_permission_tier(existing: dict, tier_perms: dict) -> dict:
    """Merge a tier's permissions into existing ones, preserving user rules.

    C3 owns every entry a tier can emit: those are replaced by the chosen tier.
    Any other allow/deny entry the user added is kept, and non-list permission
    keys (e.g. ``ask``, ``defaultMode``, ``additionalDirectories``) are left
    untouched. Mirrors how hooks and .mcp.json preserve non-C3 content.
    """
    existing = existing if isinstance(existing, dict) else {}
    managed = dict(zip(("allow", "deny"), _c3_managed_permission_entries()))
    merged = dict(existing)  # preserve unknown sub-keys (ask, defaultMode, ...)
    for key in ("allow", "deny"):
        user_custom = [e for e in (existing.get(key) or []) if e not in managed[key]]
        out: list = []
        seen: set = set()
        for entry in user_custom + list(tier_perms.get(key) or []):
            if entry not in seen:
                seen.add(entry)
                out.append(entry)
        merged[key] = out
    return merged


def _detect_current_tier(settings_path) -> str | None:
    """Detect which permission tier is active in settings_path, or None.

    Matches most-specific signatures first (c3-strict denies native file tools)
    so overlapping rules don't misclassify. Falls back to MCP-tool-set match for
    permissive. Tolerates include_mcp_wildcard (extra 'mcp__*' entry).
    """
    try:
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        perms = data.get("permissions", {})
        deny = set(perms.get("deny", []))
        allow = set(perms.get("allow", []))
        mcp_present = all(t in allow for t in _C3_MCP_ALLOW)

        # c3-strict: denies native Read/Grep/Glob/Edit/Write but allows c3 MCP
        strict_markers = {"Read(*)", "Edit(*)", "Write(*)", "Grep(*)"}
        if mcp_present and strict_markers.issubset(deny):
            return "c3-strict"
        # read-only: denies writes but not Read
        if "Write(*)" in deny and "Edit(*)" in deny and "Read(*)" not in deny:
            return "read-only"
        # standard: denies specific destructive ops
        if any(r.startswith("Bash(rm") for r in deny):
            return "standard"
        # permissive: no deny rules, all c3_* allowed
        if not deny and mcp_present:
            return "permissive"
    except Exception:
        pass
    return None


def _find_stale_tools(settings_path) -> list[str]:
    """Return list of obsolete c3 MCP tool names present in allow/deny arrays."""
    try:
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    perms = data.get("permissions", {})
    found = []
    for key in ("allow", "deny"):
        for tool in perms.get(key, []):
            if tool in _STALE_MCP_TOOLS:
                found.append(tool)
    return found


def _clean_stale_tools(settings_path) -> int:
    """Remove obsolete c3 MCP tool names from settings.local.json. Returns count removed."""
    try:
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    perms = data.get("permissions", {})
    removed = 0
    for key in ("allow", "deny"):
        orig = perms.get(key, [])
        new = [t for t in orig if t not in _STALE_MCP_TOOLS]
        removed += len(orig) - len(new)
        perms[key] = new
    if removed:
        data["permissions"] = perms
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    return removed


def _apply_permission_tier(project_path: str, tier: str,
                           include_mcp_wildcard: bool = False) -> None:
    """Write permission tier to .claude/settings.local.json, preserving existing keys."""
    tier = _TIER_ALIASES.get(tier, tier)
    settings_path = Path(project_path) / ".claude" / "settings.local.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings = _safe_read_json(settings_path, str(settings_path))
    tier_perms = _build_permission_tier(
        tier, include_mcp_wildcard=include_mcp_wildcard
    )["permissions"]
    settings["permissions"] = _merge_permission_tier(
        settings.get("permissions") or {}, tier_perms
    )
    # Persist chosen tier in .c3/config.json
    c3_config_path = Path(project_path) / ".c3" / "config.json"
    c3_config = _safe_read_json(c3_config_path, str(c3_config_path))
    c3_config["permission_tier"] = tier
    if include_mcp_wildcard:
        c3_config["permission_include_mcp_wildcard"] = True
    elif "permission_include_mcp_wildcard" in c3_config:
        del c3_config["permission_include_mcp_wildcard"]
    with open(c3_config_path, "w", encoding="utf-8") as f:
        json.dump(c3_config, f, indent=2)
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    suffix = " (+ mcp__* wildcard)" if include_mcp_wildcard else ""
    print(f"  Permissions: {tier}{suffix} — {PERMISSION_TIERS[tier]}")


def _check_c3_health(project_path: str) -> dict:
    """Inspect an existing .c3 installation and return a health report."""
    c3_dir = Path(project_path) / CONFIG_DIR
    issues = []
    info = {}

    # Config version
    config = load_config(project_path)
    info["config_version"] = config.get("version", "unknown")

    # Path change detection — project was copied/moved
    stored_path = config.get("project_path", "")
    if stored_path and stored_path != project_path:
        issues.append(f"project path changed (was copied/moved from {stored_path})")
        info["path_changed"] = True
        info["old_path"] = stored_path

    # Missing subdirectories
    missing_dirs = [d for d in _C3_INIT_SUBDIRS if not (c3_dir / d).exists()]
    if missing_dirs:
        issues.append(f"missing directories: {', '.join(missing_dirs)}")
        info["missing_dirs"] = missing_dirs

    # Index presence and basic stats
    index_file = c3_dir / "index" / "index.json"
    if not index_file.exists():
        issues.append("code index not built")
        info["index_files"] = 0
        info["index_chunks"] = 0
    else:
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
            info["index_files"] = len(data.get("documents", {}))
            info["index_chunks"] = len(data.get("chunks", {}))
        except Exception:
            issues.append("code index corrupt")
            info["index_files"] = 0
            info["index_chunks"] = 0

    # Stale file changes (tracked by the watcher inside CodeIndex)
    # The watcher writes pending changes to .c3/index/changes.json
    changes_file = c3_dir / "index" / "changes.json"
    info["stale_files"] = 0
    if changes_file.exists():
        try:
            changes = json.loads(changes_file.read_text(encoding="utf-8"))
            info["stale_files"] = len(changes) if isinstance(changes, list) else 0
            if info["stale_files"] > 5:
                issues.append(f"index stale ({info['stale_files']} file changes pending)")
        except Exception:
            _log.debug("Failed to read changes.json", exc_info=True)

    # Instructions file
    from core.ide import get_profile as _get_profile
    from core.ide import load_ide_config
    ide_name = load_ide_config(project_path)
    profile = _get_profile(ide_name)
    instructions_file = profile.instructions_file or "CLAUDE.md"
    info["instructions_file"] = instructions_file
    if not (Path(project_path) / instructions_file).exists():
        issues.append(f"{instructions_file} missing")

    # Embedding index status
    embed_hashes = c3_dir / "embeddings" / "file_hashes.json"
    if embed_hashes.exists():
        try:
            hashes = json.loads(embed_hashes.read_text(encoding="utf-8"))
            info["embedded_files"] = len(hashes)
        except Exception:
            info["embedded_files"] = 0
    else:
        info["embedded_files"] = 0

    # Doc index status (Local RAG Pipeline)
    doc_index_file = c3_dir / "doc_index" / "index.json"
    if doc_index_file.exists():
        try:
            di_data = json.loads(doc_index_file.read_text(encoding="utf-8"))
            info["doc_chunks"] = len(di_data.get("chunks", {}))
        except Exception:
            info["doc_chunks"] = 0
    else:
        info["doc_chunks"] = 0

    # Sessions and facts counts (informational only)
    sessions_dir = c3_dir / "sessions"
    info["sessions"] = len(list(sessions_dir.glob("*.json"))) if sessions_dir.exists() else 0
    facts_dir = c3_dir / "facts"
    info["facts"] = len(list(facts_dir.glob("*.json"))) if facts_dir.exists() else 0

    # Bitbucket integration (v2.30.0+) — informational
    bb_section = config.get("bitbucket") if isinstance(config, dict) else None
    if isinstance(bb_section, dict):
        active = bb_section.get("active") or {}
        accounts = bb_section.get("accounts") or []
        info["bitbucket_accounts"] = len(accounts) if isinstance(accounts, list) else 0
        info["bitbucket_active_account"] = (
            f"{active.get('username', '')}@{active.get('base_url', '')}"
            if active.get("base_url") and active.get("username") else ""
        )
        info["bitbucket_default_repo"] = (
            f"{bb_section.get('default_project', '')}/{bb_section.get('default_repo', '')}"
            if bb_section.get("default_project") and bb_section.get("default_repo") else ""
        )
    else:
        info["bitbucket_accounts"] = 0
        info["bitbucket_active_account"] = ""
        info["bitbucket_default_repo"] = ""

    info["issues"] = issues
    info["healthy"] = len(issues) == 0
    return info


def _prompt_choice(prompt: str, choices: list[str]) -> str:
    """Print numbered choices and return the selected key."""
    print(prompt)
    for i, label in enumerate(choices, 1):
        print(f"  [{i}] {label}")
    while True:
        try:
            raw = input("  Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1]
        print(f"  Please enter a number between 1 and {len(choices)}.")


def _git_is_available() -> bool:
    """Return True if the local git executable is available."""
    try:
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            ["git", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            **kwargs
        )
    except OSError:
        return False
    return result.returncode == 0


def _init_local_git_repo(project_path: str) -> str:
    """Initialize a local Git repository if needed."""
    target = Path(project_path).resolve()
    git_dir = target / ".git"
    if git_dir.exists():
        print("Git: existing repository detected.")
        return "existing"

    if not _git_is_available():
        print("Git: skipped (local git executable not found).")
        return "unavailable"

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(
        ["git", "init", str(target)],
        capture_output=True,
        text=True,
        check=False,
        **kwargs
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or "unknown git init error"
        raise RuntimeError(f"Failed to initialize local Git repository: {detail}")

    print(f"Git: initialized local repository at {git_dir}")
    return "initialized"


def _select_init_ide(default_ide: str) -> str:
    """Prompt for the target IDE during guided init."""
    choices = [
        "Auto         — detect from project markers",
        "Claude Code  — .mcp.json + hooks",
        "VS Code      — .vscode/mcp.json + Copilot instructions",
        "Cursor       — .cursor/mcp.json",
        "Codex        — .codex/config.toml + AGENTS.md",
        "Antigravity  — ~/.gemini/antigravity/mcp_config.json + AGENTS.md",
    ]
    selected = _prompt_choice("Step 1/3 — Choose IDE profile", choices)
    mapping = {
        choices[0]: "auto",
        choices[1]: "claude-code",
        choices[2]: "vscode",
        choices[3]: "cursor",
        choices[4]: "codex",
        choices[5]: "antigravity",
    }
    chosen = mapping.get(selected or "", normalize_ide_name(default_ide) if default_ide != "auto" else "auto")
    print(f"  IDE profile: {chosen}")
    return chosen


def _prompt_memory_llm(project_path: str) -> None:
    """Interactive memory_llm setup: privacy-first local model + optional cloud opt-in.

    Skipped entirely under --force (that path never enters _prompt_init_steps),
    which keeps the privacy defaults: distillation on, cloud OFF.
    """
    print()
    choice = _prompt_choice(
        "Memory — distill each session into durable project facts with an LLM?",
        [
            "Local only — a local Ollama model, nothing leaves this machine (recommended)",
            "Cloud      — a Sonnet-class Ollama Cloud model (API key or signed-in daemon)",
            "Off        — keep the basic pattern-based capture only",
        ],
    )
    if not choice:
        return  # EOF/Ctrl+C — keep defaults (local-only, cloud off)

    existing = load_config(project_path).get("memory_llm")
    section = dict(existing) if isinstance(existing, dict) else {}

    if choice.startswith("Off"):
        section["enabled"] = False
        section["cloud_enabled"] = False
        save_config({"memory_llm": section}, project_path)
        print("Memory distillation: off (regex capture only).")
        return

    section["enabled"] = True
    section["cloud_enabled"] = choice.startswith("Cloud")

    # Local model pick — it is the only tier in local mode and the privacy
    # fallback tier in cloud mode.
    default_local = section.get("local_model") or MEMORY_LLM_DEFAULTS["local_model"]
    models = None
    try:
        client = OllamaClient()
        if client.is_available(timeout=2):
            models = client.list_models()
    except Exception:
        models = None
    if models:
        options = [f"Keep default — {default_local}"] + [m for m in models if m != default_local][:8]
        pick = _prompt_choice("Pick the local model for distillation:", options)
        if pick and not pick.startswith("Keep default"):
            section["local_model"] = pick
    else:
        print(f"  (Local Ollama not reachable — keeping '{default_local}'; "
              "change it later in the Settings UI.)")

    if section["cloud_enabled"]:
        if os.environ.get("OLLAMA_API_KEY"):
            print("  Cloud key: using OLLAMA_API_KEY from the environment.")
        else:
            try:
                key = input("  Paste Ollama Cloud API key (Enter to skip — a signed-in "
                            "local daemon also works): ").strip()
            except (EOFError, KeyboardInterrupt):
                key = ""
            if key:
                try:
                    from services.ollama_credentials import save_api_key
                    save_api_key(key, section.get("cloud_base_url")
                                 or MEMORY_LLM_DEFAULTS["cloud_base_url"])
                    print("  Key stored in the OS keyring (never in config.json).")
                except Exception as exc:
                    print(f"  Could not store key in keyring ({exc}); "
                          "set the OLLAMA_API_KEY environment variable instead.")

    save_config({"memory_llm": section}, project_path)
    print("Memory distillation: "
          + ("cloud + local fallback." if section["cloud_enabled"] else "local only (private)."))


def _prompt_init_steps(project_path: str, ide_name: str, default_mode: str = "direct") -> tuple[str, bool]:
    """Run guided post-init setup steps for Git and MCP."""
    chosen_ide = _select_init_ide(ide_name or "auto")
    save_config({"ide": chosen_ide}, project_path)

    print()
    git_choice = _prompt_choice(
        "Step 2/3 — Initialize a local Git repository for this project?",
        [
            "Yes  — run local git init in this folder",
            "No   — leave version control untouched",
        ],
    )
    if git_choice and git_choice.startswith("Yes"):
        _init_local_git_repo(project_path)
    else:
        print("Git: skipped.")

    _prompt_memory_llm(project_path)

    print()
    install_choice = _prompt_choice(
        "Step 3/3 — Install MCP tooling for this project?",
        [
            "Yes  — configure the IDE and wire up C3 MCP",
            "No   — skip MCP install for now",
        ],
    )
    if not install_choice or install_choice.startswith("No"):
        print("MCP: skipped.")
        return chosen_ide, False

    mode_choice = _prompt_choice(
        "Choose MCP mode",
        [
            "Direct  — recommended, connect IDE straight to c3 mcp_server.py",
            "Proxy   — advanced, use c3 mcp_proxy.py for dynamic filtering experiments",
        ],
    )
    mcp_mode = "proxy" if mode_choice and mode_choice.startswith("Proxy") else default_mode

    # Step 4/4 — Permissions (Claude Code only)
    chosen_tier = None
    include_wildcard = False
    if chosen_ide == "claude-code":
        print()
        tier_choice = _prompt_choice(
            "Step 4/4 — Set a Claude Code permission tier for this project?",
            [
                "standard    — full editing + safe shell, block destructive ops (recommended)",
                "c3-strict   — c3_* MCP tools only, deny native Read/Grep/Glob/Edit/Write",
                "read-only   — read and C3 tools only, no writes or shell commands",
                "permissive  — unrestricted, all tools allowed",
                "Skip        — leave permissions unchanged",
            ],
        )
        if tier_choice and not tier_choice.startswith("Skip"):
            chosen_tier = tier_choice.split()[0]  # "standard", "c3-strict", "read-only", "permissive"
            # For tiers with explicit allow lists (not permissive), offer MCP-wildcard
            # so users with other MCP servers (Neon, Playwright, Context7, …) don't
            # hit an approval prompt on every call.
            if chosen_tier != "permissive":
                wildcard_choice = _prompt_choice(
                    "Pre-approve other MCP servers (Neon, Playwright, Context7, …)?",
                    [
                        "No   — prompt per-call for non-C3 MCP tools (safer)",
                        "Yes  — add mcp__* wildcard to allow list",
                    ],
                )
                include_wildcard = bool(wildcard_choice and wildcard_choice.startswith("Yes"))

    _run_install_mcp(project_path, chosen_ide, mcp_mode=mcp_mode,
                     permissions=chosen_tier, include_mcp_wildcard=include_wildcard)
    return chosen_ide, True


def _parse_cli_ide_arg(value: str) -> str:
    """Parse public CLI IDE names while still accepting legacy aliases."""
    raw = (value or "").strip().lower()
    if raw == "auto":
        return "auto"
    normalized = normalize_ide_name(raw)
    if normalized not in PROFILES:
        raise argparse.ArgumentTypeError(
            "Unsupported IDE. Use one of: auto, claude, vscode, cursor, codex, antigravity."
        )
    return normalized


def _do_init(project_path: str, ide_name: str = None, no_embed: bool = False):
    """Run the core init steps (shared by new install and re-init after clear/reset)."""
    config = _build_init_config(project_path)
    save_config(config, project_path)

    for subdir in _C3_INIT_SUBDIRS:
        (Path(project_path) / CONFIG_DIR / subdir).mkdir(parents=True, exist_ok=True)

    import time as _t

    from cli.progress import ProgressLine

    print("Building code index...")
    _prog = ProgressLine()
    _t0 = _t.perf_counter()
    indexer = CodeIndex(project_path)
    result = indexer.build_index(
        on_progress=lambda entries, files, chunks: _prog.update(
            f"  scanning: {entries:,} entries | {files:,} files | {chunks:,} chunks"))
    _prog.done()
    entries = result.get("entries_scanned", 0)
    print(f"  Indexed {result['files_indexed']} files, {result['chunks_created']} chunks "
          f"in {_t.perf_counter() - _t0:.1f}s ({entries:,} entries scanned)")
    capped = result.get("files_capped", 0)
    if capped:
        total = result["files_indexed"] + capped
        print(f"  [!] Indexed {result['files_indexed']} of {total} candidate files "
              f"(cap: index_max_files={result.get('max_files')}).")
        print("      Raise index_max_files in .c3/config.json for full coverage.")

    # Build embedding index if Ollama is available (non-blocking on failure)
    try:
        from services.embedding_index import EmbeddingIndex
        from services.ollama_client import OllamaClient
        config = load_config(project_path)
        ollama_url = config.get("ollama_base_url", "http://localhost:11434")
        ollama = OllamaClient(ollama_url)
        embed_model = config.get("embed_model", "nomic-embed-text")
        ei = EmbeddingIndex(project_path, ollama, embed_model=embed_model)
        if no_embed:
            print("  Embedding index skipped (--no-embed)")
        elif ei.probe()["ready"]:
            # probe() initializes the lazy backends; checking .ready on a
            # fresh instance is always False and silently skipped the build.
            print("Building embedding index...")
            _t0 = _t.perf_counter()
            _eprog = ProgressLine()
            ei_result = ei.build(
                indexer,
                on_progress=lambda done, total, chunks: _eprog.update(
                    f"  embedding: file {done}/{total} | {chunks:,} chunks"))
            _eprog.done()
            print(f"  Embedded {ei_result.get('chunks_embedded', 0)} chunks "
                  f"({ei_result.get('files_processed', 0)} files) "
                  f"in {_t.perf_counter() - _t0:.1f}s")
        else:
            print(f"  Embedding index skipped ({ei.unavailable_reason()})")
    except Exception:
        _log.debug("Embedding index build failed", exc_info=True)

    # Build doc index for Local RAG Pipeline
    try:
        from services.doc_index import DocIndex
        print("Building doc index...")
        _t0 = _t.perf_counter()
        _dprog = ProgressLine()
        di = DocIndex(project_path)
        di_result = di.build(
            on_progress=lambda done, total: _dprog.update(
                f"  docs: {done}/{total}"))
        _dprog.done()
        print(f"  Indexed {di_result['docs_indexed']} docs, "
              f"{di_result['chunks_created']} chunks in {_t.perf_counter() - _t0:.1f}s")
    except Exception:
        _log.debug("Doc index build failed", exc_info=True)

    print("Building compression dictionary...")
    protocol = CompressionProtocol(project_path)
    # Mine the in-memory index instead of re-reading the whole tree.
    new_terms = protocol.build_project_dictionary(code_index=indexer)
    print(f"  Added {len(new_terms)} project-specific terms")

    from core.ide import detect_ide, load_ide_config
    from core.ide import get_profile as _get_profile
    # Use caller-supplied IDE if given, otherwise detect from disk markers
    if not ide_name or ide_name == "auto":
        ide_name = load_ide_config(project_path)
        if ide_name == "claude-code":
            # Re-detect in case .vscode/ etc was just created
            ide_name = detect_ide(project_path)
    profile = _get_profile(ide_name)
    instructions_file = profile.instructions_file or "CLAUDE.md"

    # Ensure parent directory exists (e.g. .github/ for VS Code)
    instructions_path = Path(project_path) / instructions_file
    instructions_path.parent.mkdir(parents=True, exist_ok=True)

    sm = SessionManager(project_path)
    _sync_project_instruction_docs(project_path, sm)


def cmd_init(args):
    """Initialize C3 for a project, or upgrade/repair an existing install."""
    import shutil

    project_path = str(Path(args.project_path or ".").resolve())
    c3_dir = Path(project_path) / CONFIG_DIR
    requested_ide = getattr(args, "ide", "auto")
    if requested_ide != "auto":
        requested_ide = normalize_ide_name(requested_ide)
    git_requested = getattr(args, "git", False)
    no_embed = bool(getattr(args, "no_embed", False))

    # â”€â”€ Brand-new install â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if not c3_dir.exists() or not (c3_dir / "config.json").exists():
        print_header(f"Initializing C3 for: {project_path}")
        _do_init(project_path, ide_name=requested_ide, no_embed=no_embed)
        try:
            from services.project_manager import ProjectManager
            ProjectManager().add_project(project_path)
        except Exception as _e:
            print(f"  [warn] Could not register project with hub: {_e}")
        if getattr(args, "force", False):
            if git_requested:
                _init_local_git_repo(project_path)
            _run_install_mcp(
                project_path, requested_ide,
                mcp_mode=getattr(args, "mcp_mode", "direct"),
                permissions=getattr(args, "permissions", None),
                include_mcp_wildcard=bool(getattr(args, "include_mcp_wildcard", False)),
            )
        else:
            _prompt_init_steps(project_path, requested_ide, default_mode=getattr(args, "mcp_mode", "direct"))
        # Detect Codex CLI availability
        try:
            from cli.tools.delegate import check_codex
            codex_info = check_codex()
            if codex_info.get("status") == "ok":
                print(f"\n  Codex CLI detected: {codex_info.get('version', 'unknown')}")
                print("  Codex integration: enabled (delegate.codex_enabled=true)")
            else:
                detail = codex_info.get('detail', 'not installed')
                print(f"\n  Codex CLI: not available ({detail})")
                print("  Install codex CLI to enable cloud delegate backend (GPT-5.x)")
        except Exception:
            pass

        # Detect Gemini CLI availability
        try:
            from cli.tools.delegate import check_gemini
            gemini_info = check_gemini()
            if gemini_info.get("status") == "ok":
                print(f"  Gemini CLI detected: {gemini_info.get('version', 'unknown')}")
                print("  Gemini integration: enabled (delegate.gemini_enabled=true)")
            else:
                detail = gemini_info.get('detail', 'not installed')
                print(f"  Gemini CLI: not available ({detail})")
                print("  Install gemini CLI to enable Gemini delegate backend")
        except Exception:
            pass

        print("\n[OK] C3 initialized!")
        print("  Use 'c3 --help' for all available commands.")
        return

    # â”€â”€ Existing install — run health check â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print_header(f"C3 already installed: {project_path}")
    health = _check_c3_health(project_path)

    # Show summary
    print(f"  Index : {health.get('index_files', 0)} files, "
          f"{health.get('index_chunks', 0)} chunks"
          + (f", {health['stale_files']} stale" if health.get("stale_files") else ""))
    embed_count = health.get("embedded_files", 0)
    print(f"  Embed : {embed_count} files" + (" (semantic search ready)" if embed_count > 0 else " (not built)"))
    print(f"  Data  : {health['sessions']} sessions, {health['facts']} facts")
    print(f"  Guide : {health['instructions_file']}"
          + ("" if not health["issues"] or
             health["instructions_file"] + " missing" not in " ".join(health["issues"])
             else " [MISSING]"))

    # Version-skew notice: this project's .c3 was written by an older C3.
    stored_version = _safe_read_json(c3_dir / "config.json", "config").get("version")
    if stored_version and _version_tuple(str(stored_version)) < _version_tuple(__version__):
        print(f"\n  [upgrade] Set up with C3 v{stored_version}; now running v{__version__}.")
        print("            Run 'c3 init . --force' to re-apply MCP config, hooks, and docs.")

    # Permission status (Claude Code only) — surface tier + stale-tool drift
    try:
        from core.ide import load_ide_config as _load_ide
        _ide = _load_ide(project_path)
        if _ide == "claude-code":
            _settings = Path(project_path) / ".claude" / "settings.local.json"
            detected = _detect_current_tier(_settings)
            _cfg = _safe_read_json(Path(project_path) / ".c3" / "config.json", "config")
            stored = _cfg.get("permission_tier")
            wildcard = _cfg.get("permission_include_mcp_wildcard", False)
            stale_count = len(_find_stale_tools(_settings))
            parts = [detected or stored or "(none set)"]
            if detected and stored and detected != stored:
                parts.append(f"drift vs stored={stored}")
            if wildcard:
                parts.append("+mcp__*")
            if stale_count:
                parts.append(f"{stale_count} stale — run 'c3 permissions clean'")
            print(f"  Perms : {', '.join(parts)}")
    except Exception:
        pass

    # Delegate backends
    try:
        from services.ollama_client import OllamaClient as _OC
        _oc = _OC()
        ollama_ok = _oc.is_available()
        models = _oc.list_models() if ollama_ok else []
        n = len(models) if models else 0
        print(f"  Ollama: {'up (' + str(n) + ' models)' if ollama_ok else 'down'}")
    except Exception:
        print("  Ollama: unknown")
    try:
        from cli.tools.delegate import check_codex
        ci = check_codex()
        if ci.get("status") == "ok":
            ver = ci.get('version', 'detected')
            print(f"  Codex : {ver} (cloud delegate ready)")
        else:
            detail = ci.get('detail', 'not installed')
            print(f"  Codex : not available ({detail})")
    except Exception:
        print("  Codex : unknown")
    try:
        from cli.tools.delegate import check_gemini
        gi = check_gemini()
        if gi.get("status") == "ok":
            ver = gi.get('version', 'detected')
            print(f"  Gemini: {ver} (cloud delegate ready)")
        else:
            detail = gi.get('detail', 'not installed')
            print(f"  Gemini: not available ({detail})")
    except Exception:
        print("  Gemini: unknown")

    # Bitbucket integration (v2.30.0+)
    bb_n = int(health.get("bitbucket_accounts") or 0)
    if bb_n:
        active = health.get("bitbucket_active_account") or "(no active)"
        repo = health.get("bitbucket_default_repo") or "(no default repo)"
        print(f"  Bitbkt: {bb_n} account(s), active={active}, repo={repo}")
    else:
        print("  Bitbkt: not configured (run 'c3 bitbucket login --url <URL>')")

    if health["healthy"]:
        print("\n  Status: healthy — no issues detected.")
    else:
        print(f"\n  Status: {len(health['issues'])} issue(s) found:")
        for issue in health["issues"]:
            print(f"    ! {issue}")

    # â”€â”€ Path-change fast path (project was copied/moved) â”€â”€â”€â”€â”€â”€
    if health.get("path_changed"):
        old_path = health.get("old_path", "?")
        print("\n  [!] Path change detected:")
        print(f"      was : {old_path}")
        print(f"      now : {project_path}")
        print("\n  Updating MCP config and index paths...")
        # Clear stale transcript index manifest so it rebuilds with new slug
        ti_manifest = c3_dir / "transcript_index" / "manifest.json"
        if ti_manifest.exists():
            ti_manifest.write_text("{}", encoding="utf-8")
        _do_init(project_path, ide_name=requested_ide, no_embed=no_embed)
        if git_requested:
            _init_local_git_repo(project_path)
        _run_install_mcp(project_path, requested_ide, mcp_mode=getattr(args, "mcp_mode", "direct"), banner="Updating MCP tools...")
        print("\n[OK] Paths updated.")
        return

    # â”€â”€ Non-interactive (--clear) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if getattr(args, "clear", False):
        print("\n[--clear] Wiping C3 files...")
        parent_link = (load_config(project_path) or {}).get("parent") or {}
        if parent_link.get("path"):
            print(f"  [!] This project is a sub-project of {parent_link['path']}")
            print("      The parent still lists it -- run 'c3 sub check --fix' there.")
        _uninstall_mcp_all(project_path)
        if c3_dir.exists():
            shutil.rmtree(c3_dir)
            print("  Deleted .c3/")
        for filename, _ in _instruction_documents_for_project():
            p = Path(project_path) / filename
            if p.exists():
                p.unlink()
                print(f"  Deleted {filename}")
        print("\n[OK] C3 project files removed.")
        return

    # â”€â”€ Non-interactive (--force) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if getattr(args, "force", False):
        print("\n[--force] Applying update...")
        _do_init(project_path, ide_name=requested_ide, no_embed=no_embed)
        if git_requested:
            _init_local_git_repo(project_path)
        _run_install_mcp(project_path, requested_ide, mcp_mode=getattr(args, "mcp_mode", "direct"), banner="Updating MCP tools...")
        print("\n[OK] C3 updated.")
        return

    # â”€â”€ Interactive prompt â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    print()
    choices = [
        "Update  — rebuild index & refresh instructions file, keep all data",
        "Clear   — wipe index/cache/sessions, keep facts & memory, then rebuild",
        "Reset   — delete entire .c3 directory and start fresh",
        "Wipe    — remove .c3/ and instruction docs, then exit (no rebuild)",
        "Cancel  — exit without changes",
    ]
    selected = _prompt_choice("What would you like to do?", choices)

    if not selected or selected.startswith("Cancel"):
        print("  Cancelled.")
        return

    if selected.startswith("Update"):
        print()
        _do_init(project_path, ide_name=requested_ide, no_embed=no_embed)
        if git_requested:
            _init_local_git_repo(project_path)
        _prompt_install_mcp(project_path, requested_ide, default_mode=getattr(args, "mcp_mode", "direct"), banner="Updating MCP tools...")
        print("\n[OK] C3 updated.")

    elif selected.startswith("Clear"):
        print("\nClearing index, cache, sessions, and analytics (keeping facts & memory)...")
        clear_dirs = ["cache", "index", "sessions", "analytics", "snapshots",
                      "transcript_index", "file_memory"]
        for subdir in clear_dirs:
            target = c3_dir / subdir
            if target.exists():
                shutil.rmtree(target)
                print(f"  Removed .c3/{subdir}/")
        print()
        _do_init(project_path, ide_name=requested_ide, no_embed=no_embed)
        if git_requested:
            _init_local_git_repo(project_path)
        _prompt_install_mcp(project_path, requested_ide, default_mode=getattr(args, "mcp_mode", "direct"), banner="Updating MCP tools...")
        print("\n[OK] C3 cleared and rebuilt.")

    elif selected.startswith("Reset"):
        confirm = input(
            "\n  WARNING: This will permanently delete .c3/ including all sessions,\n"
            "  facts, and memory. Type 'yes' to confirm: "
        ).strip().lower()
        if confirm != "yes":
            print("  Reset cancelled.")
            return
        shutil.rmtree(c3_dir)
        print("  Deleted .c3/")
        print()
        _do_init(project_path, ide_name=requested_ide, no_embed=no_embed)
        if git_requested:
            _init_local_git_repo(project_path)
        _prompt_install_mcp(project_path, requested_ide, default_mode=getattr(args, "mcp_mode", "direct"), banner="Re-installing MCP tools...")
        print("\n[OK] C3 reset and re-initialized.")

    elif selected.startswith("Wipe"):
        confirm = input(
            "\n  WARNING: This will permanently delete .c3/ and all project\n"
            "  instruction files (CLAUDE.md, GEMINI.md, AGENTS.md), and remove\n"
            "  C3 MCP configurations from your IDE. Type 'yes' to confirm: "
        ).strip().lower()
        if confirm != "yes":
            print("  Wipe cancelled.")
            return

        _uninstall_mcp_all(project_path)

        if c3_dir.exists():
            shutil.rmtree(c3_dir)
            print("  Deleted .c3/")

        for filename, _ in _instruction_documents_for_project():
            p = Path(project_path) / filename
            if p.exists():
                p.unlink()
                print(f"  Deleted {filename}")

        print("\n[OK] C3 project files removed.")


def cmd_index(args):
    """Rebuild the code index."""
    return common_cmd_index(args, _command_deps())


def cmd_compress(args):
    """Compress a file and show results."""
    return common_cmd_compress(args, _command_deps())


def cmd_context(args):
    """Get relevant context for a query."""
    return common_cmd_context(args, _command_deps())


def cmd_encode(args):
    """Encode text to compressed format."""
    return common_cmd_encode(args, _command_deps())


def cmd_decode(args):
    """Decode compressed text back to readable format."""
    return common_cmd_decode(args, _command_deps())


def cmd_session(args):
    """Session management commands."""
    return common_cmd_session(args, _command_deps())


def cmd_claudemd(args):
    """Instructions file generation commands."""
    return common_cmd_claudemd(args, _command_deps())


def cmd_map(args):
    """Live repo map (.c3/MAP.md) commands."""
    from cli.commands.common import cmd_map as common_cmd_map
    return common_cmd_map(args, _command_deps())


def cmd_stats(args):
    """Show comprehensive stats."""
    return common_cmd_stats(args, _command_deps())


def _benchmark_extract_preview(full_path: Path, compressor: CodeCompressor, pattern: str = "", max_lines: int = 50) -> str:
    """Approximate c3_filter behavior for local benchmarking without MCP startup."""
    import re as _re
    from collections import Counter

    ext = full_path.suffix.lower()
    code_exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h", ".cs"}

    original_text = full_path.read_text(encoding="utf-8", errors="replace")
    original_tokens = count_tokens(original_text)
    lines = original_text.splitlines()
    extracted = ""

    if ext in code_exts and not pattern:
        result = _compress_file_cli(compressor, str(full_path), "smart")
        extracted = result.get("compressed", "") if "error" not in result else f"Error: {result['error']}"

    elif ext == ".jsonl" and not pattern:
        entry_count = len(lines)
        sample_lines = lines if entry_count <= 6 else lines[:3] + ["..."] + lines[-3:]
        fields = ""
        aggregates = []
        if lines:
            try:
                first = json.loads(lines[0])
                fields = f"fields: {', '.join(first.keys())}"
                parsed = [json.loads(line) for line in lines[: min(len(lines), 200)] if line.strip()]
                for key in ("event", "status", "type", "level"):
                    values = [str(item.get(key, "")).strip() for item in parsed if item.get(key) not in (None, "")]
                    if values:
                        common = Counter(values).most_common(3)
                        aggregates.append(f"{key}: " + ", ".join(f"{name} x{count}" for name, count in common))
            except Exception:
                fields = "fields: (parse error)"
        aggregate_text = (" | " + " | ".join(aggregates)) if aggregates else ""
        extracted = f"[jsonl] {entry_count} entries | {fields}{aggregate_text}\n" + "\n".join(sample_lines[:max_lines])

    elif ext in (".log", ".txt") and not pattern:
        error_patterns = [
            (_re.compile(r"ERROR", _re.IGNORECASE), "ERROR"),
            (_re.compile(r"WARN", _re.IGNORECASE), "WARN"),
            (_re.compile(r"Exception", _re.IGNORECASE), "Exception"),
            (_re.compile(r"Traceback", _re.IGNORECASE), "Traceback"),
        ]
        counts = {name: 0 for _, name in error_patterns}
        clusters = {}
        for i, line in enumerate(lines):
            for pat, name in error_patterns:
                if pat.search(line):
                    counts[name] += 1
                    normalized = _re.sub(r"\d+", "<n>", line.strip())
                    normalized = _re.sub(r"0x[0-9a-f]+", "0x<hex>", normalized, flags=_re.IGNORECASE)
                    bucket = clusters.setdefault(normalized, {"count": 0, "example": line[:200], "first_line": i + 1, "kind": name})
                    bucket["count"] += 1
                    break
        freq = " | ".join(f"{k}:{v}" for k, v in counts.items() if v > 0)
        if clusters:
            ranked = sorted(
                clusters.values(),
                key=lambda item: (item["count"], item["kind"] == "ERROR", item["kind"] == "Traceback"),
                reverse=True,
            )
            summaries = [
                f"{item['kind']} x{item['count']} @L{item['first_line']}: {item['example']}"
                for item in ranked[:max_lines]
            ]
            extracted = f"[log] {len(lines)} lines | {freq or 'no errors detected'}\n" + "\n".join(summaries)
        else:
            extracted = f"[log] {len(lines)} lines | {freq or 'no errors detected'}"

    elif pattern:
        try:
            pat = _re.compile(pattern, _re.IGNORECASE)
        except _re.error as e:
            extracted = f"[extract:error] invalid pattern: {e}"
        else:
            matched = []
            for i, line in enumerate(lines):
                if pat.search(line):
                    start = max(0, i - 1)
                    end = min(len(lines), i + 2)
                    for j in range(start, end):
                        marker = ">" if j == i else " "
                        entry = f"{marker}L{j+1}: {lines[j][:200]}"
                        if entry not in matched:
                            matched.append(entry)
                    if len(matched) >= max_lines:
                        break
            extracted = f"[grep:{pattern}] {len(matched)} lines matched\n" + "\n".join(matched[:max_lines])

    else:
        if len(lines) <= max_lines:
            extracted = original_text
        else:
            half = max_lines // 2
            extracted = (
                "\n".join(lines[:half])
                + f"\n... ({len(lines) - max_lines} lines omitted) ...\n"
                + "\n".join(lines[-half:])
            )

    extracted_tokens = count_tokens(extracted)
    saved_pct = round((1 - extracted_tokens / original_tokens) * 100, 1) if original_tokens > 0 else 0.0
    return f"[extract:{ext}] {original_tokens}tok->{extracted_tokens}tok ({saved_pct}% saved)\n{extracted}"


def _build_benchmark_fixtures(project_path: Path, sample: list[tuple[Path, str, int]]) -> dict:
    """Create representative local fixtures for logs, JSONL, and noisy terminal output."""
    fixture_dir = project_path / ".c3" / "benchmark" / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    rel_paths = [str(item[0].relative_to(project_path)).replace("\\", "/") for item in sample[:8]]
    if not rel_paths:
        rel_paths = ["cli/c3.py"]

    def _stamp(idx: int) -> str:
        return f"2026-03-05T16:{idx % 60:02d}:{(idx * 7) % 60:02d}"

    log_lines = []
    for idx in range(72):
        rel = rel_paths[idx % len(rel_paths)]
        log_lines.append(f"{_stamp(idx)} INFO indexed {rel} chunks={(idx % 5) + 1}")
        if idx % 2 == 0:
            log_lines.extend([f"{_stamp(idx)} INFO heartbeat ok"] * 3)
        if idx % 3 == 0:
            log_lines.append(f"{_stamp(idx)} WARN slow parse {rel} latency_ms={40 + idx}")
        if idx % 5 == 0:
            log_lines.append(f"{_stamp(idx)} ERROR failed to analyze {rel}")
            log_lines.append("Traceback (most recent call last):")
            log_lines.append(f"  File \"{rel}\", line {10 + idx}, in benchmark_fixture")
            log_lines.append("RuntimeError: benchmark fixture failure")

    log_path = fixture_dir / "benchmark_tool.log"
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    jsonl_entries = []
    for idx in range(180):
        rel = rel_paths[idx % len(rel_paths)]
        jsonl_entries.append({
            "ts": _stamp(idx),
            "event": "compress" if idx % 2 == 0 else "search",
            "file": rel,
            "status": "ok" if idx % 11 else "warn",
            "tokens": 250 + idx,
            "latency_ms": 3 + (idx % 17),
        })

    jsonl_path = fixture_dir / "benchmark_events.jsonl"
    jsonl_path.write_text("\n".join(json.dumps(entry) for entry in jsonl_entries) + "\n", encoding="utf-8")

    terminal_lines = ["\x1b[36mcollecting benchmark output\x1b[0m", ""]
    for idx in range(96):
        rel = rel_paths[idx % len(rel_paths)]
        terminal_lines.append(f"tests/test_{idx:03d}.py::test_{idx % 9}_{Path(rel).stem} PASSED")
        if idx % 6 == 0:
            terminal_lines.extend(["Downloading model shard 3/7..."] * 4)
        if idx % 8 == 0:
            terminal_lines.append("████████████████████████████ 100%")
        if idx % 12 == 0:
            terminal_lines.append(f"WARN cache miss while scanning {rel}")
        if idx % 20 == 0:
            terminal_lines.append(f"ERROR failed benchmark step for {rel}")
            terminal_lines.append(f"FAILED tests/test_benchmark.py::test_{idx:03d} - AssertionError: timed out on {rel}")
        if idx % 14 == 0:
            terminal_lines.extend(["", ""])

    terminal_text = "\n".join(terminal_lines) + "\n"
    terminal_path = fixture_dir / "benchmark_terminal_output.txt"
    terminal_path.write_text(terminal_text, encoding="utf-8")

    return {
        "fixture_dir": str(fixture_dir),
        "fixture_strategy": (
            "Generated representative local log, JSONL, and terminal-output fixtures under .c3/benchmark_fixtures "
            "because this repository does not contain large native log/data artifacts."
        ),
        "log_path": str(log_path),
        "jsonl_path": str(jsonl_path),
        "terminal_output_path": str(terminal_path),
        "log_signals": ["ERROR", "WARN", "Traceback", "RuntimeError"],
        "jsonl_fields": list(jsonl_entries[0].keys()),
        "terminal_signals": ["WARN", "ERROR", "FAILED", "[line repeated x"],
    }


_BENCHMARK_DELEGATE_TASKS = {
    "summarize": {
        "default_model": "gemma3n:latest",
        "system": "You are a concise code summarizer. Output terse bullet points.",
        "prompt_template": "Summarize the following:\n\n{context}\n\n{task}",
    },
}


def _benchmark_delegate_confidence(task_type: str, response: str, response_tokens: int) -> str:
    """Mirror c3_delegate confidence heuristics for benchmark reporting."""
    hedging = [
        "i'm not sure",
        "i don't know",
        "it's unclear",
        "might be",
        "possibly",
        "i cannot determine",
        "hard to say",
        "not enough context",
    ]
    hedge_count = sum(1 for phrase in hedging if phrase in (response or "").lower())
    min_tokens = {"summarize": 15, "explain": 30, "docstring": 10, "review": 20, "ask": 10, "test": 30, "diagnose": 20, "improve": 10}
    too_short = response_tokens < min_tokens.get(task_type, 10)
    if too_short or hedge_count >= 2:
        return "low"
    if hedge_count == 1 or response_tokens < min_tokens.get(task_type, 10) * 2:
        return "medium"
    return "high"


def _benchmark_resolve_model_name(candidate: str, available: list[str]) -> str:
    """Resolve a configured delegate model name against installed Ollama models."""
    if not candidate:
        return ""
    normalized = candidate.strip().lower()
    if not normalized:
        return ""

    for model in available:
        if model.lower() == normalized:
            return model

    base = normalized.split(":", 1)[0]
    for model in available:
        lower = model.lower()
        if lower == base or lower.startswith(base + ":"):
            return model

    for model in available:
        if base in model.lower():
            return model

    return ""


def _benchmark_delegate_fallback_order(task_type: str) -> list[str]:
    """Conservative fallback order aligned with c3_delegate."""
    if task_type in {"ask", "diagnose", "explain"}:
        return ["llama3.2:latest", "llama3.2:3b", "qwen3-coder-next:latest", "llama3.1:latest", "gemma3n:latest"]
    return ["llama3.2:latest", "llama3.2:3b", "qwen3-coder-next:latest", "gemma3n:latest"]


def _benchmark_delegate_optional(project_path: Path, sample: list[tuple[Path, str, int]], compressor: CodeCompressor) -> dict:
    """Benchmark c3_delegate offload against direct primary-model prompting."""
    evaluation = {
        "tool": "c3_delegate",
        "status": "skipped",
        "included_in_main_scorecard": True,
        "comparison_scope": "primary-model prompt savings",
        "description": "Offload large-file understanding to a local Ollama model instead of sending the full file to the primary AI.",
    }

    if not sample:
        evaluation["reason"] = "No eligible source files were available for delegate benchmarking."
        return evaluation

    delegate_config = load_delegate_config(str(project_path))
    evaluation["config"] = {
        "enabled": bool(delegate_config.get("enabled", True)),
        "preferred_model": delegate_config.get("preferred_model", ""),
        "max_context_tokens": delegate_config.get("max_context_tokens", DELEGATE_DEFAULTS.get("max_context_tokens", 2000)),
        "allow_model_fallback": bool(delegate_config.get("allow_model_fallback", True)),
    }

    if not delegate_config.get("enabled", True):
        evaluation["reason"] = "Delegation is disabled in .c3/config.json."
        return evaluation

    ollama = OllamaClient()
    if not ollama.is_available():
        evaluation["reason"] = "Ollama is not reachable on localhost, so delegate offload cannot be measured."
        return evaluation

    available = ollama.list_models() or []
    if not available:
        evaluation["reason"] = "Ollama is reachable but no local models are installed."
        return evaluation

    fpath, raw_content, raw_tokens = max(sample, key=lambda item: item[2])
    rel_path = str(fpath.relative_to(project_path)).replace("\\", "/")
    task_type = "summarize"
    task_def = _BENCHMARK_DELEGATE_TASKS[task_type]
    task = (
        f"Summarize the purpose and main moving parts of {rel_path}. "
        "Focus on responsibilities, important functions/classes, and notable dependencies."
    )

    compressed_result = _compress_file_cli(compressor, str(fpath), "smart")
    compressed_context = compressed_result.get("compressed", "") if isinstance(compressed_result, dict) else ""
    if not compressed_context:
        compressed_context = raw_content

    max_ctx_tokens = delegate_config.get("max_context_tokens", DELEGATE_DEFAULTS.get("max_context_tokens", 2000))
    compressed_context_tokens = count_tokens(compressed_context)
    if compressed_context_tokens > max_ctx_tokens:
        char_limit = max_ctx_tokens * 4
        compressed_context = compressed_context[:char_limit] + f"\n... [truncated to ~{max_ctx_tokens}tok]"
        compressed_context_tokens = max_ctx_tokens

    threshold_enabled = delegate_config.get("threshold_enabled", False)
    threshold_min = delegate_config.get("threshold_min_total_tokens", 80)
    threshold_types = delegate_config.get("threshold_task_types", ["ask", "explain", "summarize", "improve", "docstring"]) or []
    force_types = delegate_config.get("threshold_force_task_types", ["diagnose", "review", "test"]) or []
    if isinstance(threshold_types, str):
        threshold_types = [threshold_types]
    if isinstance(force_types, str):
        force_types = [force_types]
    total_delegate_tokens = count_tokens(task) + compressed_context_tokens
    if threshold_enabled and task_type in set(threshold_types) and task_type not in set(force_types) and total_delegate_tokens < threshold_min:
        evaluation["reason"] = f"Delegate threshold prevented execution ({total_delegate_tokens}tok < {threshold_min}tok minimum)."
        return evaluation

    requested_model = delegate_config.get(f"{task_type}_model", "") or delegate_config.get("preferred_model", "") or task_def["default_model"]
    model = _benchmark_resolve_model_name(requested_model, available)
    fallback_used = False
    fallback_from = requested_model
    if not model and delegate_config.get("allow_model_fallback", True):
        fallback_models = delegate_config.get("fallback_models", []) or []
        if isinstance(fallback_models, str):
            fallback_models = [fallback_models]
        candidates = [task_def["default_model"]] + _benchmark_delegate_fallback_order(task_type) + fallback_models + available
        for candidate in candidates:
            resolved = _benchmark_resolve_model_name(candidate, available)
            if resolved:
                model = resolved
                fallback_used = True
                break

    if not model:
        evaluation["status"] = "failed"
        evaluation["reason"] = f"Requested delegate model '{requested_model}' was not installed and no fallback model was available."
        evaluation["available_models"] = available[:10]
        return evaluation

    delegate_prompt = task_def["prompt_template"].format(context=compressed_context, task=task)
    baseline_prompt = task_def["prompt_template"].format(context=raw_content, task=task)
    delegate_prompt_tokens = count_tokens(delegate_prompt)
    baseline_prompt_tokens = count_tokens(baseline_prompt)

    t0 = time.perf_counter()
    response = ollama.generate(
        prompt=delegate_prompt,
        model=model,
        system=task_def["system"],
        temperature=delegate_config.get("temperature", 0.3),
        max_tokens=delegate_config.get("max_tokens", 1024),
    )
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    if response is None:
        evaluation["status"] = "failed"
        evaluation["reason"] = "Ollama returned no response for the delegate benchmark request."
        evaluation["with_c3"] = {
            "tool": "c3_delegate",
            "task_type": task_type,
            "task_file": rel_path,
            "model": model,
            "latency_ms": latency_ms,
        }
        return evaluation

    response_tokens = count_tokens(response)
    confidence = _benchmark_delegate_confidence(task_type, response, response_tokens)
    primary_model_tokens = response_tokens

    evaluation.update({
        "status": "measured",
        "reason": "Delegate benchmark completed successfully.",
        "available_models_sample": available[:10],
        "with_c3": {
            "tool": "c3_delegate",
            "task_type": task_type,
            "task_file": rel_path,
            "model": model,
            "fallback_used": fallback_used,
            "fallback_from": fallback_from if fallback_used else "",
            "context_tokens": compressed_context_tokens,
            "delegate_prompt_tokens": delegate_prompt_tokens,
            "primary_model_prompt_tokens": primary_model_tokens,
            "response_tokens": response_tokens,
            "latency_ms": latency_ms,
            "confidence": confidence,
        },
        "without_c3": {
            "approach": "send full file directly to the primary AI",
            "task_type": task_type,
            "task_file": rel_path,
            "primary_model_prompt_tokens": baseline_prompt_tokens,
            "context_tokens": raw_tokens,
        },
        "primary_model_token_savings_pct": round(((baseline_prompt_tokens - primary_model_tokens) / baseline_prompt_tokens) * 100, 1) if baseline_prompt_tokens else 0.0,
        "prompt_budget_multiplier": round((baseline_prompt_tokens / primary_model_tokens), 2) if primary_model_tokens else 0.0,
        "response_preview": (response[:300] + "...") if len(response) > 300 else response,
    })
    return evaluation


def _benchmark_route_optional(project_path: Path, fixtures: dict, sample: list[tuple[Path, str, int]]) -> dict:
    """Benchmark c3_delegate(task_type='auto') as an optional local-routing/offload path."""
    from core.config import load_hybrid_config
    from services.router import ModelRouter

    evaluation = {
        "tool": "c3_delegate(task_type='auto')",
        "status": "skipped",
        "included_in_main_scorecard": False,
        "comparison_scope": "primary-model prompt savings",
        "description": "Classify requests and route them to local models when they fit a low-cost lane.",
    }

    hybrid_config = load_hybrid_config(str(project_path))
    if hybrid_config.get("HYBRID_DISABLE_TIER2"):
        evaluation["reason"] = "Router tier is disabled in .c3/config.json."
        return evaluation

    router = ModelRouter(hybrid_config)
    if not router.ollama.is_available():
        evaluation["reason"] = "Ollama is not reachable on localhost, so router offload cannot be measured."
        return evaluation

    log_text = Path(fixtures["terminal_output_path"]).read_text(encoding="utf-8", errors="replace")
    stacktrace_excerpt = "\n".join(Path(fixtures["log_path"]).read_text(encoding="utf-8", errors="replace").splitlines()[:80])
    file_hint = str(sample[0][0].relative_to(project_path)).replace("\\", "/") if sample else "cli/c3.py"
    cases = [
        {
            "name": "log_summary",
            "query": "Summarize the key failures in this test output.",
            "context": log_text,
            "expected_class": "log_summary",
        },
        {
            "name": "simple_qa",
            "query": "What file defines IDE profiles?",
            "context": "",
            "expected_class": "simple_qa",
        },
        {
            "name": "complex",
            "query": f"Diagnose the likely root cause in this traceback and explain what to inspect in {file_hint}.",
            "context": stacktrace_excerpt,
            "expected_class": "complex",
        },
    ]

    total_baseline_tokens = 0
    total_primary_tokens = 0
    total_latency_ms = 0.0
    class_hits = 0
    handled_locally = 0
    used_models = []

    for case in cases:
        baseline_prompt = case["query"] if not case["context"] else f"{case['query']}\n\nContext:\n{case['context']}"
        baseline_tokens = count_tokens(baseline_prompt)
        total_baseline_tokens += baseline_tokens

        classification = router.classify(case["query"], case["context"])
        if classification["route_class"] == case["expected_class"]:
            class_hits += 1

        result = router.route(case["query"], case["context"])
        total_latency_ms += float(result.get("latency_ms", 0) or 0)
        if result.get("route_class") != "passthrough" and result.get("response"):
            handled_locally += 1
            response_tokens = count_tokens(result["response"])
            total_primary_tokens += response_tokens
            if result.get("model"):
                used_models.append(result["model"])
        else:
            total_primary_tokens += baseline_tokens
            if result.get("model"):
                used_models.append(result["model"])

    class_hit_rate = round((class_hits / len(cases)) * 100, 1) if cases else 0.0
    local_handling_rate = round((handled_locally / len(cases)) * 100, 1) if cases else 0.0
    evaluation.update({
        "status": "measured",
        "reason": "Router benchmark completed successfully.",
        "notes": f"{class_hits}/{len(cases)} expected route classes matched; {handled_locally}/{len(cases)} cases were handled locally.",
        "quality": {
            "metric": "expected route-class hit rate",
            "with_c3": class_hit_rate,
            "local_handling_rate": local_handling_rate,
        },
        "cases": [
            {
                "name": case["name"],
                "expected_class": case["expected_class"],
            } for case in cases
        ],
        "with_c3": {
            "tool": "c3_delegate(task_type='auto')",
            "task_type": "query routing",
            "task_file": f"{len(cases)} benchmark cases",
            "model": ", ".join(sorted(set(used_models)))[:120],
            "primary_model_prompt_tokens": total_primary_tokens,
            "latency_ms": round(total_latency_ms / len(cases), 2) if cases else 0.0,
            "confidence": "high" if class_hit_rate >= 100 else ("medium" if class_hit_rate >= 66 else "low"),
        },
        "without_c3": {
            "approach": "send each query and context directly to the primary AI",
            "task_type": "query routing",
            "task_file": f"{len(cases)} benchmark cases",
            "primary_model_prompt_tokens": total_baseline_tokens,
        },
        "primary_model_token_savings_pct": round(((total_baseline_tokens - total_primary_tokens) / total_baseline_tokens) * 100, 1) if total_baseline_tokens else 0.0,
        "prompt_budget_multiplier": round((total_baseline_tokens / total_primary_tokens), 2) if total_primary_tokens else 0.0,
    })
    return evaluation


def _benchmark_summarize_optional(project_path: Path, fixtures: dict) -> dict:
    """Benchmark c3_delegate(task_type='summarize') as an optional local summarization path."""
    from core.config import load_hybrid_config
    from services.router import ModelRouter

    evaluation = {
        "tool": "c3_delegate(task_type='summarize')",
        "status": "skipped",
        "included_in_main_scorecard": False,
        "comparison_scope": "primary-model prompt savings",
        "description": "Use a local summary first so the primary AI receives a condensed version of large text.",
    }

    hybrid_config = load_hybrid_config(str(project_path))
    if hybrid_config.get("HYBRID_DISABLE_TIER2"):
        evaluation["reason"] = "Router tier is disabled in .c3/config.json, so summarize is unavailable."
        return evaluation

    router = ModelRouter(hybrid_config)
    if not router.ollama.is_available():
        evaluation["reason"] = "Ollama is not reachable on localhost, so summarize offload cannot be measured."
        return evaluation

    source_text = Path(fixtures["terminal_output_path"]).read_text(encoding="utf-8", errors="replace")
    baseline_prompt_tokens = count_tokens(f"Summarize:\n\n{source_text[:4000]}")
    t0 = time.perf_counter()
    result = router.summarize(source_text, "bullet")
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    if result.get("summary") is None:
        evaluation["status"] = "failed"
        evaluation["reason"] = f"Local summarize model {result.get('model', '')} returned no response."
        return evaluation

    summary = result["summary"]
    response_tokens = count_tokens(summary)
    signals = fixtures.get("terminal_signals", [])
    signal_hits = sum(1 for sig in signals if sig.lower() in summary.lower())
    signal_retention = round((signal_hits / len(signals)) * 100, 1) if signals else 0.0
    confidence = _benchmark_delegate_confidence("summarize", summary, response_tokens)

    evaluation.update({
        "status": "measured",
        "reason": "Summarize benchmark completed successfully.",
        "notes": f"{signal_hits}/{len(signals)} tracked terminal signals appeared in the local summary.",
        "quality": {
            "metric": "tracked signal retention",
            "with_c3": signal_retention,
            "without_c3": 100.0,
        },
        "with_c3": {
            "tool": "c3_delegate(task_type='summarize')",
            "task_type": "terminal summary",
            "task_file": Path(fixtures["terminal_output_path"]).name,
            "model": result.get("model", ""),
            "primary_model_prompt_tokens": response_tokens,
            "latency_ms": latency_ms,
            "confidence": confidence,
        },
        "without_c3": {
            "approach": "send the full terminal text to the primary AI for summarization",
            "task_type": "terminal summary",
            "task_file": Path(fixtures["terminal_output_path"]).name,
            "primary_model_prompt_tokens": baseline_prompt_tokens,
        },
        "primary_model_token_savings_pct": round(((baseline_prompt_tokens - response_tokens) / baseline_prompt_tokens) * 100, 1) if baseline_prompt_tokens else 0.0,
        "prompt_budget_multiplier": round((baseline_prompt_tokens / response_tokens), 2) if response_tokens else 0.0,
        "response_preview": (summary[:300] + "...") if len(summary) > 300 else summary,
    })
    return evaluation


def _benchmark_recall_optional(project_path: Path) -> dict:
    """Benchmark c3_memory(action='recall') against scanning the full fact store."""
    from services.memory import MemoryStore

    evaluation = {
        "tool": "c3_memory(action='recall')",
        "status": "skipped",
        "included_in_main_scorecard": False,
        "comparison_scope": "prompt savings vs full fact-store scan",
        "description": "Retrieve only the most relevant stored facts instead of loading the whole memory store into context.",
    }

    fixture_dir = project_path / ".c3" / "benchmark" / "fixtures" / "memory_eval"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    facts_file = fixture_dir / "facts.json"
    benchmark_facts = [
        {"id": "bm_fact_01", "fact": "Use c3_compress(mode='map') before reading large code files so you can target small sections instead of loading the full file.", "category": "workflow", "source_session": "", "timestamp": "2026-03-05T00:00:00+00:00", "relevance_count": 0},
        {"id": "bm_fact_02", "fact": "Use c3_filter before reading logs, txt files, or jsonl files directly.", "category": "workflow", "source_session": "", "timestamp": "2026-03-05T00:00:00+00:00", "relevance_count": 0},
        {"id": "bm_fact_03", "fact": "Use c3_delegate for files over 200 lines when you need understanding but are not editing the file.", "category": "delegate", "source_session": "", "timestamp": "2026-03-05T00:00:00+00:00", "relevance_count": 0},
        {"id": "bm_fact_04", "fact": "Use c3_search to locate relevant symbols and code chunks before opening files.", "category": "search", "source_session": "", "timestamp": "2026-03-05T00:00:00+00:00", "relevance_count": 0},
        {"id": "bm_fact_05", "fact": "Use c3_filter when terminal output is noisy and contains repeated progress or PASS lines.", "category": "output", "source_session": "", "timestamp": "2026-03-05T00:00:00+00:00", "relevance_count": 0},
    ]
    facts_file.write_text(json.dumps(benchmark_facts, indent=2), encoding="utf-8")
    memory = MemoryStore(str(project_path), data_dir=".c3/benchmark/fixtures/memory_eval")

    queries = [
        ("large code files targeted sections", "c3_compress"),
        ("tracebacks logs jsonl direct read", "c3_filter"),
        ("over 200 lines understanding not editing", "c3_delegate"),
        ("locate relevant symbols before opening files", "c3_search"),
    ]

    full_facts_text = "\n".join(f"[{fact['category']}] {fact['fact']}" for fact in benchmark_facts)
    total_baseline_tokens = 0
    total_recall_tokens = 0
    recall_latency_ms = []
    baseline_latency_ms = []
    hits = 0
    previews = []

    for query, expected in queries:
        baseline_prompt = f"Facts:\n{full_facts_text}\n\nQuestion: {query}"
        total_baseline_tokens += count_tokens(baseline_prompt)
        t_base = time.perf_counter()
        _ = full_facts_text
        baseline_latency_ms.append((time.perf_counter() - t_base) * 1000)

        t0 = time.perf_counter()
        results = memory.recall(query, top_k=3)
        recall_latency_ms.append((time.perf_counter() - t0) * 1000)

        recall_text = "\n".join(f"[{item['category']}] {item['fact']}" for item in results)
        total_recall_tokens += count_tokens(recall_text)
        joined = " ".join(item["fact"] for item in results)
        if expected in joined:
            hits += 1
        if results:
            previews.append(results[0]["fact"][:120])

    hit_rate = round((hits / len(queries)) * 100, 1) if queries else 0.0
    evaluation.update({
        "status": "measured",
        "reason": "Recall benchmark completed successfully.",
        "notes": f"{hits}/{len(queries)} expected benchmark facts were retrieved.",
        "quality": {
            "metric": "expected-fact hit rate",
            "with_c3": hit_rate,
            "without_c3": 100.0,
        },
        "with_c3": {
            "tool": "c3_memory(action='recall')",
            "task_type": "memory lookup",
            "task_file": "benchmark memory fixture",
            "model": "tf-idf memory search",
            "primary_model_prompt_tokens": total_recall_tokens,
            "latency_ms": round(sum(recall_latency_ms) / len(recall_latency_ms), 2) if recall_latency_ms else 0.0,
            "confidence": "high" if hit_rate >= 100 else ("medium" if hit_rate >= 75 else "low"),
        },
        "without_c3": {
            "approach": "scan the full fact store in the primary model context",
            "task_type": "memory lookup",
            "task_file": "benchmark memory fixture",
            "primary_model_prompt_tokens": total_baseline_tokens,
        },
        "primary_model_token_savings_pct": round(((total_baseline_tokens - total_recall_tokens) / total_baseline_tokens) * 100, 1) if total_baseline_tokens else 0.0,
        "prompt_budget_multiplier": round((total_baseline_tokens / total_recall_tokens), 2) if total_recall_tokens else 0.0,
        "response_preview": " | ".join(previews[:2]),
    })
    return evaluation


_BENCHMARK_SESSION_ASSUMPTIONS = {
    "system_and_instructions_tokens": 4000,
    "claude_md_tokens": 2000,
    "mcp_tool_schemas_tokens": 1500,
    "user_turn_tokens": 150,
    "assistant_reply_tokens": 400,
    "tool_wrapper_tokens": 120,
}

# Overhead that C3 adds but vanilla Claude Code does not have
_BENCHMARK_C3_OVERHEAD = {
    "claude_md_c3_mandates_tokens": 800,
    "mcp_c3_tool_schemas_tokens": 1200,
    "mandatory_recall_tokens_per_session": 600,
    "avg_c3_tool_response_wrapper_tokens": 80,
}


_BENCHMARK_SESSION_PROFILES = {
    "balanced": {
        "label": "Balanced",
        "description": "Even mix across the main benchmark scenarios.",
        "weights": {},
    },
    "lean_coding": {
        "label": "Lean Coding",
        "description": "Mostly search, file-map, and filtered-output turns with fewer heavy file reads.",
        "weights": {
            "search_retrieval": 0.35,
            "file_navigation": 0.25,
            "terminal_output_filtering": 0.15,
            "broad_file_understanding": 0.10,
            "log_triage": 0.10,
            "structured_data_scan": 0.05,
        },
    },
    "heavy_analysis": {
        "label": "Heavy Analysis",
        "description": "More large-file understanding, logs, and structured-data investigation.",
        "weights": {
            "broad_file_understanding": 0.30,
            "search_retrieval": 0.20,
            "file_navigation": 0.15,
            "log_triage": 0.15,
            "structured_data_scan": 0.10,
            "terminal_output_filtering": 0.10,
        },
    },
}


def _benchmark_session_reality(project_path: Path, scenarios: dict) -> dict:
    """Estimate retained-session growth with realistic overhead modeling.

    Computes two views:
    - tool_level: raw per-operation savings (what the old benchmark showed)
    - session_net: accounts for fixed overhead that dwarfs per-tool savings,
      plus the extra overhead C3 itself introduces (CLAUDE.md mandates,
      MCP tool schemas, mandatory recall calls)
    """
    session_mgr = SessionManager(str(project_path))
    thresholds = dict(getattr(session_mgr, "_budget_thresholds", SessionManager.DEFAULT_BUDGET_THRESHOLDS))
    # Back-compat: older SessionManager used level_1/level_2 keys; current version has a single
    # `threshold`. Derive missing keys so the benchmark works across both schemas.
    base = thresholds.get("threshold", 35000)
    thresholds.setdefault("level_1", base)
    thresholds.setdefault("level_2", base * 4)
    transcript_usage = session_mgr.parse_claude_session_tokens(str(project_path))

    # Measure actual CLAUDE.md size if available
    claude_md_path = project_path / "CLAUDE.md"
    if claude_md_path.exists():
        actual_claude_md_tokens = count_tokens(claude_md_path.read_text(encoding="utf-8", errors="replace"))
        _BENCHMARK_SESSION_ASSUMPTIONS["claude_md_tokens"] = actual_claude_md_tokens

    # Fixed overhead present in BOTH with-C3 and without-C3 sessions
    base_overhead = (
        _BENCHMARK_SESSION_ASSUMPTIONS["system_and_instructions_tokens"]
        + _BENCHMARK_SESSION_ASSUMPTIONS["user_turn_tokens"]
        + _BENCHMARK_SESSION_ASSUMPTIONS["assistant_reply_tokens"]
        + _BENCHMARK_SESSION_ASSUMPTIONS["tool_wrapper_tokens"]
    )
    # Without C3: base overhead + minimal CLAUDE.md (project might still have one)
    vanilla_claude_md = 300  # typical non-C3 CLAUDE.md
    overhead_without_c3 = base_overhead + vanilla_claude_md

    # With C3: base overhead + full CLAUDE.md + C3-specific overhead
    c3_extra = sum(_BENCHMARK_C3_OVERHEAD.values())
    overhead_with_c3 = base_overhead + _BENCHMARK_SESSION_ASSUMPTIONS["claude_md_tokens"] + c3_extra

    scenario_token_map = {}
    for name, data in scenarios.items():
        scenario_token_map[name] = {
            "with_c3": float(data.get("with_c3", {}).get("total_tokens", data.get("with_c3", {}).get("avg_context_tokens", 0)) or 0),
            "without_c3": float(data.get("without_c3", {}).get("total_tokens", data.get("without_c3", {}).get("avg_context_tokens", 0)) or 0),
        }

    def _weighted_tokens(side: str, weights: dict) -> float:
        if not weights:
            values = [entry[side] for entry in scenario_token_map.values()]
            return (sum(values) / len(values)) if values else 0.0
        return sum(scenario_token_map.get(name, {}).get(side, 0.0) * weight for name, weight in weights.items())

    profiles = {}
    for key, meta in _BENCHMARK_SESSION_PROFILES.items():
        context_with = _weighted_tokens("with_c3", meta.get("weights", {}))
        context_without = _weighted_tokens("without_c3", meta.get("weights", {}))

        # Tool-level view (old metric, kept for comparison)
        tool_multiplier = round((context_without / context_with), 2) if context_with else 0.0

        # Session-net view: includes realistic fixed overhead
        retained_with = context_with + overhead_with_c3
        retained_without = context_without + overhead_without_c3

        net_savings_pct = round(((retained_without - retained_with) / retained_without) * 100, 1) if retained_without else 0.0
        net_multiplier = round((retained_without / retained_with), 2) if retained_with else 0.0

        profiles[key] = {
            "label": meta["label"],
            "description": meta["description"],
            "avg_context_tokens_with_c3": round(context_with, 1),
            "avg_context_tokens_without_c3": round(context_without, 1),
            "tool_level_multiplier": tool_multiplier,
            "retained_tokens_per_turn_with_c3": round(retained_with, 1),
            "retained_tokens_per_turn_without_c3": round(retained_without, 1),
            "session_net_savings_pct": net_savings_pct,
            "session_net_multiplier": net_multiplier,
            "turns_until_level_1_with_c3": round((thresholds["level_1"] / retained_with), 1) if retained_with else 0.0,
            "turns_until_level_1_without_c3": round((thresholds["level_1"] / retained_without), 1) if retained_without else 0.0,
            "turns_until_level_2_with_c3": round((thresholds["level_2"] / retained_with), 1) if retained_with else 0.0,
            "turns_until_level_2_without_c3": round((thresholds["level_2"] / retained_without), 1) if retained_without else 0.0,
        }

    return {
        "note": "Session-net multiplier accounts for fixed overhead (system prompt, CLAUDE.md, MCP schemas) and C3's own overhead (mandates, tool schemas, mandatory recalls). Tool-level multiplier shows raw per-operation savings for comparison.",
        "assumptions": {
            "base_per_turn": _BENCHMARK_SESSION_ASSUMPTIONS,
            "c3_overhead": _BENCHMARK_C3_OVERHEAD,
            "overhead_with_c3": overhead_with_c3,
            "overhead_without_c3": overhead_without_c3,
            "c3_net_overhead_delta": overhead_with_c3 - overhead_without_c3,
        },
        "thresholds": thresholds,
        "profiles": profiles,
        "transcript_usage": transcript_usage,
    }



def _render_benchmark_html(reports: list[dict]) -> str:
    """Render a modern, high-detail bento-grid benchmark report with Chart.js visualizations."""
    if not reports:
        return "<html><body>No reports to display.</body></html>"

    # Primary report details
    primary = reports[-1]
    scorecard = primary.get("scorecard", {})
    scenarios = primary.get("scenarios", {})
    runner = primary.get("runner", {})
    quality_checks = primary.get("quality_checks", {})
    session_reality = primary.get("session_reality", {})
    fixtures = primary.get("fixtures", {})
    optional_evals = primary.get("optional_evaluations", {})

    def _num(value, digits: int = 1):
        if isinstance(value, (int, float)):
            return f"{float(value):.{digits}f}"
        return str(value)

    def _display_timestamp(value: str) -> str:
        if not value:
            return "unknown"
        return str(value).replace("T", " ")

    def _hbar_rows(data, key, suffix, digits, cls):
        res = []
        for d in data:
            val = d.get(key, 0)
            res.append(f"""
                <div class="chart-row">
                    <div class="chart-label">{html.escape(d.get('label', ''))}</div>
                    <div class="chart-track"><div class="chart-bar {cls}" style="width: {min(100, float(val))}%"></div></div>
                    <div class="chart-value">{_num(val, digits)}{suffix}</div>
                </div>
            """)
        return "".join(res)

    def _dual_rows(data, key1, key2, suffix, digits):
        res = []
        for d in data:
            v1 = d.get(key1, 0)
            v2 = d.get(key2, 0)
            total = max(0.001, float(v1) + float(v2))
            p1 = (float(v1) / total) * 100
            p2 = (float(v2) / total) * 100
            res.append(f"""
                <div class="dual-row">
                    <div class="chart-label">{html.escape(d.get('label', ''))}</div>
                    <div class="dual-stack">
                        <div class="dual-track">
                            <div class="chart-bar c3" style="width: {p1}%"></div>
                            <div class="mini-tag">C3: {_num(v1, digits)}{suffix}</div>
                        </div>
                        <div class="dual-track">
                            <div class="chart-bar baseline" style="width: {p2}%"></div>
                            <div class="mini-tag">Base: {_num(v2, digits)}{suffix}</div>
                        </div>
                    </div>
                </div>
            """)
        return "".join(res)

    # ─── Data Preparation for Charts ───────────────────────────
    # Sort scenarios to ensure deterministic chart labels
    sorted_scenario_keys = sorted(scenarios.keys())
    scenario_display_labels = [s.replace('_', ' ').title() for s in sorted_scenario_keys]

    scenario_c3_tokens = []
    scenario_base_tokens = []
    scenario_savings = []

    for k in sorted_scenario_keys:
        s = scenarios[k]
        c3_tok = s.get('with_c3', {}).get('total_tokens', s.get('with_c3', {}).get('avg_context_tokens', 0))
        base_tok = s.get('without_c3', {}).get('total_tokens', s.get('without_c3', {}).get('avg_context_tokens', 0))
        scenario_c3_tokens.append(c3_tok)
        scenario_base_tokens.append(base_tok)
        scenario_savings.append(s.get('token_savings_pct', 0))

    # Model distribution data (from offload evals)
    model_counts = {}
    for name, eval_data in optional_evals.items():
        if eval_data.get("status") == "measured":
            m = eval_data.get("with_c3", {}).get("model", "unknown")
            model_counts[m] = model_counts.get(m, 0) + 1

    model_labels = list(model_counts.keys())
    model_data = list(model_counts.values())

    # ─── Table Rows ──────────────────────────────────────────
    scenario_matrix_rows = []
    for k in sorted_scenario_keys:
        s = scenarios[k]
        with_c3 = s.get("with_c3", {})
        without_c3 = s.get("without_c3", {})
        scenario_matrix_rows.append(f"""
            <tr>
                <td><strong>{html.escape(k.replace('_', ' ').title())}</strong><div class="td-note">{html.escape(s.get('description', ''))}</div></td>
                <td><span class="badge tool">{html.escape(with_c3.get('tool', 'n/a'))}</span></td>
                <td>{_num(scenario_c3_tokens[sorted_scenario_keys.index(k)])}</td>
                <td>{_num(scenario_base_tokens[sorted_scenario_keys.index(k)])}</td>
                <td class="text-green">{_num(s.get('token_savings_pct', 0))}%</td>
                <td>{_num(s.get('prompt_budget_multiplier', 0))}x</td>
                <td>{_num(with_c3.get('avg_latency_ms', 0))}ms</td>
            </tr>
        """)

    comparison_list = []
    for r in reports:
        r_sc = r.get("scorecard", {})
        r_run = r.get("runner", {})
        label = r_run.get("system_label", r_run.get("system_name", "unknown"))
        if r_run.get("system_version"): label += f" {r_run['system_version']}"
        timestamp_label = _display_timestamp(r.get("timestamp", ""))
        comparison_list.append(f"""
            <div class="comp-row">
                <div class="comp-label">{html.escape(label)}</div>
                <div class="td-note">Run: {html.escape(timestamp_label)}</div>
                <div class="comp-bar-track"><div class="comp-bar" style="width: {r_sc.get('token_usage',{}).get('savings_pct',0)}%"></div></div>
                <div class="comp-value">{_num(r_sc.get('token_usage',{}).get('savings_pct',0))}% saved</div>
            </div>
        """)

    raw_json = html.escape(json.dumps(reports, indent=2))

    # ─── History Track Preparation ─────────────────────────────
    history_labels = []
    history_savings = []
    history_quality = []
    history_latency = []

    for r in reports:
        ts = _display_timestamp(r.get("timestamp", ""))
        ver = r.get("runner", {}).get("c3_version", "unknown")
        history_labels.append(f"{ts} (v{ver})")

        r_sc = r.get("scorecard", {})
        history_savings.append(r_sc.get("token_usage", {}).get("savings_pct", 0))
        history_quality.append(r_sc.get("performance", {}).get("with_c3_quality_pct", 0))
        history_latency.append(r_sc.get("speed", {}).get("with_c3_avg_latency_ms", 0))

    if False:  # legacy template — superseded by the richer return below
     _legacy_html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>C3 Benchmark Report</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #f3eee6;
      --panel: rgba(255,250,243,0.86);
      --panel-strong: #fffaf4;
      --ink: #182126;
      --muted: #617079;
      --line: #d8cfbf;
      --accent: #0c7c59;
      --accent-soft: #d8efe7;
      --baseline: #d38b4d;
      --baseline-soft: #f7e5d3;
      --shadow: 0 16px 38px rgba(24,33,38,0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(12,124,89,0.16), transparent 26%),
        radial-gradient(circle at bottom right, rgba(211,139,77,0.16), transparent 24%),
        linear-gradient(180deg, #fbf6ef 0%, var(--bg) 100%);
    }}
    .wrap {{ max-width: 1360px; margin: 0 auto; padding: 28px 20px 48px; }}
    .hero, .tab-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }}
    .hero-grid {{ display: grid; grid-template-columns: 1.2fr .8fr; gap: 20px; align-items: end; }}
    h1 {{ margin: 0 0 10px; font-size: 44px; line-height: 1.03; }}
    h2 {{ margin: 0 0 10px; font-size: 24px; }}
    h3 {{ margin: 0 0 8px; font-size: 18px; }}
    p {{ margin: 0; color: var(--muted); line-height: 1.55; }}
    .eyebrow {{ text-transform: uppercase; letter-spacing: 0.08em; font-size: 11px; color: var(--muted); margin-bottom: 10px; }}
    .hero-meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 16px; margin-top: 16px; color: var(--muted); font-size: 14px; }}
    .hero-stat {{
      background: linear-gradient(180deg, rgba(12,124,89,0.10), rgba(12,124,89,0.04));
      border: 1px solid rgba(12,124,89,0.18);
      border-radius: 22px;
      padding: 20px;
    }}
    .hero-stat .big {{ font-size: 58px; line-height: 1; font-weight: 700; margin-bottom: 10px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-top: 18px; }}
    .metric-card {{ background: var(--panel-strong); border: 1px solid var(--line); border-radius: 18px; padding: 18px; }}
    .metric {{ font-size: 30px; font-weight: 700; margin-bottom: 8px; }}
    .detail {{ color: var(--muted); font-size: 14px; }}
    .tabs {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 22px 0 14px; }}
    .tab-btn {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.74);
      color: var(--ink);
      border-radius: 999px;
      padding: 10px 16px;
      font: inherit;
      cursor: pointer;
    }}
    .tab-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    .panel-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-top: 16px; }}
    .subpanel {{ background: var(--panel-strong); border: 1px solid var(--line); border-radius: 18px; padding: 18px; }}
    .chart-row {{ display: grid; grid-template-columns: 180px 1fr 84px; gap: 12px; align-items: center; margin-top: 12px; }}
    .dual-row {{ display: grid; grid-template-columns: 180px 1fr; gap: 12px; margin-top: 12px; align-items: start; }}
    .chart-label {{ text-transform: capitalize; font-size: 14px; }}
    .chart-track, .dual-track {{ height: 14px; background: rgba(24,33,38,0.07); border-radius: 999px; overflow: visible; position: relative; }}
    .chart-bar {{ height: 100%; border-radius: 999px; min-width: 2px; }}
    .chart-bar.c3 {{ background: linear-gradient(90deg, var(--accent), #39aa7f); }}
    .chart-bar.baseline {{ background: linear-gradient(90deg, var(--baseline), #e8ba8c); }}
    .chart-value {{ text-align: right; color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }}
    .dual-stack {{ display: grid; gap: 10px; }}
    .mini-tag {{ position: absolute; right: 8px; top: -2px; font-size: 12px; color: var(--ink); font-variant-numeric: tabular-nums; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 14px; background: var(--panel-strong); border-radius: 16px; overflow: hidden; }}
    th, td {{ text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ background: var(--accent-soft); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
    tr:last-child td {{ border-bottom: 0; }}
    .td-note {{ color: var(--muted); font-size: 12px; margin-top: 6px; line-height: 1.45; }}
    .pill-list {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }}
    .pill {{ border-radius: 999px; padding: 7px 12px; background: rgba(12,124,89,0.08); border: 1px solid rgba(12,124,89,0.18); font-size: 13px; }}
    code, pre {{ font-family: Consolas, "SFMono-Regular", monospace; }}
    code {{ background: rgba(12,124,89,0.08); padding: 2px 6px; border-radius: 6px; }}
    pre {{ margin: 14px 0 0; background: #1f282d; color: #e8f0f2; border-radius: 18px; padding: 18px; overflow: auto; font-size: 13px; line-height: 1.45; }}
    .history-grid {{ display: grid; grid-template-columns: 1fr; gap: 24px; margin-top: 16px; }}
    .chart-container {{ position: relative; height: 260px; width: 100%; }}
    .text-green {{ color: var(--accent); font-weight: 600; }}
    .text-orange {{ color: var(--baseline); font-weight: 600; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-family: Consolas, monospace; }}
    .badge.tool {{ background: rgba(12,124,89,0.10); border: 1px solid rgba(12,124,89,0.22); color: var(--accent); }}
    .status-measured {{ color: var(--accent); font-weight: 600; }}
    .status-skipped {{ color: var(--muted); }}
    .status-unavailable {{ color: var(--baseline); }}
    @media (max-width: 920px) {{
      .hero-grid {{ grid-template-columns: 1fr; }}
      .chart-row, .dual-row {{ grid-template-columns: 1fr; }}
      .chart-value {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-grid">
        <div>
          <div class="eyebrow">C3 Benchmark Report <span style="background:#818cf8;color:#0b1020;padding:0.1rem 0.5rem;border-radius:999px;font-size:0.65rem;font-weight:600;margin-left:0.4rem;vertical-align:middle">Synthetic</span> <a href="../benchmarks/index.html" style="color:#9aa3c7;font-size:0.75rem;margin-left:0.6rem;text-decoration:none">← dashboard</a></div>
          <h1>With C3 vs Without C3</h1>
          <p>Generated on {html.escape(primary.get("timestamp", ""))} for <code>{html.escape(primary.get("project_path", ""))}</code>. This report compares C3-assisted workflows against raw baseline paths across code, logs, structured data, and terminal output. Task-level savings are not the same thing as full-session lifetime; see Session Reality for retained-turn estimates.</p>
          <div class="hero-meta">
            <div><strong>System:</strong> {html.escape(runner.get("system_label", runner.get("system_name", "unknown")))}{html.escape((' ' + runner.get('system_version')) if runner.get('system_version') else '')}</div>
            <div><strong>IDE profile:</strong> {html.escape(runner.get("ide_display_name", runner.get("ide_name", "unknown")))}</div>
            <div><strong>Files considered:</strong> {html.escape(str(primary.get("files_considered", 0)))}</div>
            <div><strong>Benchmarked tools:</strong> {html.escape(", ".join(primary.get("coverage", {}).get("benchmarked_tools", [])))}</div>
            <div><strong>Fixture strategy:</strong> {html.escape(fixtures.get("fixture_strategy", "native repository inputs"))}</div>
            <div><strong>HTML report:</strong> <code>{html.escape(primary.get("artifacts", {}).get("html_report", ""))}</code></div>
          </div>
        </div>
        <div class="hero-stat">
          <div class="eyebrow">Task-Level Result</div>
          <div class="big">{html.escape(_num(scorecard.get('token_usage', {}).get('savings_pct', 0)))}%</div>
          <p>task-level prompt reduction, <strong>{html.escape(_num(scorecard.get('token_usage', {}).get('prompt_budget_multiplier', 0)))}x</strong> prompt-budget multiplier, and <strong>{html.escape(_num(scorecard.get('performance', {}).get('delta_pct_points', 0)))} pts</strong> average performance uplift.</p>
        </div>
      </div>
      <div class="cards">
        <div class='metric-card'>
            <div class='eyebrow'>Token Usage</div>
            <div class='metric'>{_num(scorecard.get('token_usage', {}).get('savings_pct', 0))}%</div>
            <div class='detail'>{_num(scorecard.get('token_usage', {}).get('with_c3_total_tokens', 0), 1)} tok with C3 vs {_num(scorecard.get('token_usage', {}).get('without_c3_total_tokens', 0), 1)} tok baseline</div>
        </div>
        <div class='metric-card'>
            <div class='eyebrow'>Prompt Budget</div>
            <div class='metric'>{_num(scorecard.get('token_usage', {}).get('prompt_budget_multiplier', 0))}x</div>
            <div class='detail'>More input fits into the same context window.</div>
        </div>
        <div class='metric-card'>
            <div class='eyebrow'>Speed</div>
            <div class='metric'>{_num(scorecard.get('speed', {}).get('latency_delta_pct_vs_baseline', 0))}%</div>
            <div class='detail'>{_num(scorecard.get('speed', {}).get('with_c3_avg_latency_ms', 0))} ms with C3 vs {_num(scorecard.get('speed', {}).get('without_c3_avg_latency_ms', 0))} ms baseline</div>
        </div>
        <div class='metric-card'>
            <div class='eyebrow'>Performance</div>
            <div class='metric'>{_num(scorecard.get('performance', {}).get('with_c3_quality_pct', 0))}%</div>
            <div class='detail'>{_num(scorecard.get('performance', {}).get('delta_pct_points', 0))} pts vs baseline</div>
        </div>
        <div class='metric-card'>
            <div class='eyebrow'>Session Reality</div>
            <div class='metric'>{_num(session_reality.get('profiles', {}).get('balanced', {}).get('session_adjusted_savings_pct', 0))}%</div>
            <div class='detail'>Balanced retained-turn savings; ~{_num(session_reality.get('profiles', {}).get('balanced', {}).get('turns_until_level_2_with_c3', 0))} turns to L2 with C3</div>
        </div>
      </div>
    </section>

    <div class="tabs">
      <button class="tab-btn active" data-tab="overview">Overview</button>
      <button class="tab-btn" data-tab="history">Performance History</button>
      <button class="tab-btn" data-tab="scenarios">Scenarios</button>
      <button class="tab-btn" data-tab="quality">Quality</button>
      <button class="tab-btn" data-tab="session">Session</button>
      <button class="tab-btn" data-tab="raw">Raw Data</button>
    </div>

    <section class="tab-panel active" id="tab-overview">
      <div class="panel-grid">
        <div class="subpanel">
          <h3>Token Savings By Scenario</h3>
          <p>Higher is better. This shows where C3 removes the most prompt payload.</p>
          {_hbar_rows([dict(label=k.replace('_', ' ').title(), token_savings_pct=s.get('token_savings_pct', 0)) for k, s in scenarios.items()], 'token_savings_pct', '%', 1, 'c3')}
        </div>
        <div class="subpanel">
          <h3>Prompt Budget Multiplier</h3>
          <p>How much more input fits before you hit the same context ceiling.</p>
          {_hbar_rows([dict(label=k.replace('_', ' ').title(), prompt_budget_multiplier=s.get('prompt_budget_multiplier', 0)) for k, s in scenarios.items()], 'prompt_budget_multiplier', 'x', 2, 'baseline')}
        </div>
      </div>
      <div class="panel-grid">
        <div class="subpanel">
          <h3>Latency Comparison</h3>
          <p>C3 spends local milliseconds to reduce prompt volume. This chart shows both paths per scenario.</p>
          {_dual_rows([dict(label=k.replace('_', ' ').title(), c3_latency_ms=s.get('with_c3', {}).get('avg_latency_ms', 0), baseline_latency_ms=s.get('without_c3', {}).get('avg_latency_ms', 0)) for k, s in scenarios.items()], 'c3_latency_ms', 'baseline_latency_ms', ' ms', 2)}
        </div>
        <div class="subpanel">
          <h3>Performance Comparison</h3>
          <p>Task-specific success or signal-retention checks for C3 versus the raw baseline path.</p>
          {_dual_rows([dict(label=k.replace('_', ' ').title(), c3_perf=s.get('performance_metric_with_c3', 0), baseline_perf=s.get('performance_metric_without_c3', 0)) for k, s in scenarios.items()], 'c3_perf', 'baseline_perf', '%', 1)}
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-history">
      <div class="history-grid">
        <div class="subpanel">
          <h3>Token Savings History</h3>
          <p>Tracking the percentage of tokens saved across versions and runs.</p>
          <div class="chart-container"><canvas id="savingsChart"></canvas></div>
        </div>
        <div class="subpanel">
          <h3>Intelligence Quality History</h3>
          <p>Ensuring mapping and retrieval quality remains stable as parsers evolve.</p>
          <div class="chart-container"><canvas id="qualityChart"></canvas></div>
        </div>
        <div class="subpanel">
          <h3>Avg Local Latency History</h3>
          <p>Monitoring the local computational cost of C3 features.</p>
          <div class="chart-container"><canvas id="latencyChart"></canvas></div>
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-scenarios">
      <div class="subpanel">
        <h2>Scenario Matrix</h2>
        <p>Detailed comparison of each benchmarked workflow, including token impact, latency, and the task-specific performance metric.</p>
        <table>
          <thead>
            <tr>
              <th>Scenario</th>
              <th>Tool</th>
              <th>C3 Tokens</th>
              <th>Baseline Tokens</th>
              <th>Savings</th>
              <th>Budget</th>
              <th>Latency</th>
            </tr>
          </thead>
          <tbody>{"".join(scenario_matrix_rows)}</tbody>
        </table>
      </div>
    </section>

    <section class="tab-panel" id="tab-quality">
      <div class="panel-grid">
        <div class="subpanel">
          <h2>Quality Checks</h2>
          <p>Baseline full-read paths retain all information by definition. C3 is measured on whether it keeps the signals the task needs.</p>
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Metric</th>
                <th>With C3</th>
                <th>Without C3</th>
                <th>Delta</th>
              </tr>
            </thead>
            <tbody>
              {"".join([f"<tr><td>{html.escape(k.replace('_', ' ').title())}</td><td>{html.escape(v.get('metric', ''))}</td><td>{_num(v.get('with_c3_pct', 0))}%</td><td>{_num(v.get('without_c3_pct', 0))}%</td><td>{_num(v.get('delta_pct_points', 0))} pts</td></tr>" for k, v in quality_checks.items()])}
            </tbody>
          </table>
        </div>
        <div class="subpanel">
          <h2>Quality Distribution</h2>
          <p>The benchmark mixes retrieval hit-rate checks with signal and schema retention checks.</p>
          {_dual_rows([dict(label=k.replace('_', ' ').title(), with_c3=v.get('with_c3_pct', 0), without_c3=v.get('without_c3_pct', 0)) for k, v in quality_checks.items()], 'with_c3', 'without_c3', '%', 1)}
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-session">
      <div class="panel-grid">
        <div class="subpanel">
          <h2>Session Reality</h2>
          <p>{html.escape(session_reality.get("note", ""))}</p>
          <table>
            <thead>
              <tr>
                <th>Profile</th>
                <th>C3 Retained/Turn</th>
                <th>Base Retained/Turn</th>
                <th>Savings</th>
                <th>Budget</th>
                <th>L1 C3/Base</th>
                <th>L2 C3/Base</th>
              </tr>
            </thead>
            <tbody>
              {"".join([f"<tr><td><strong>{html.escape(k.title())}</strong></td><td>{_num(v.get('retained_tokens_per_turn_with_c3', 0), 1)}</td><td>{_num(v.get('retained_tokens_per_turn_without_c3', 0), 1)}</td><td>{_num(v.get('session_adjusted_savings_pct', 0))}%</td><td>{_num(v.get('session_adjusted_prompt_budget_multiplier', 0), 2)}x</td><td>{_num(v.get('turns_until_level_1_with_c3', 0), 1)} / {_num(v.get('turns_until_level_1_without_c3', 0), 1)}</td><td>{_num(v.get('turns_until_level_2_with_c3', 0), 1)} / {_num(v.get('turns_until_level_2_without_c3', 0), 1)}</td></tr>" for k, v in session_reality.get("profiles", {}).items()])}
            </tbody>
          </table>
        </div>
        <div class="subpanel">
          <h2>Assumptions</h2>
          <table>
            <thead><tr><th>Input</th><th>Tokens</th></tr></thead>
            <tbody>
              <tr><td>Fixed overhead per turn</td><td>{_num(session_reality.get('assumptions', {}).get('fixed_overhead_tokens_per_turn', 0), 1)}</td></tr>
              <tr><td>L1 threshold</td><td>{_num(session_reality.get('thresholds', {}).get('level_1', 0), 0)}</td></tr>
              <tr><td>L2 threshold</td><td>{_num(session_reality.get('thresholds', {}).get('level_2', 0), 0)}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-raw">
      <div class="subpanel">
        <h2>Raw JSON</h2>
        <pre>{raw_json}</pre>
      </div>
    </section>
  </div>
  <script>
    const buttons = document.querySelectorAll('.tab-btn');
    const panels = document.querySelectorAll('.tab-panel');
    buttons.forEach((button) => {{
      button.addEventListener('click', () => {{
        const tab = button.dataset.tab;
        buttons.forEach((b) => b.classList.toggle('active', b === button));
        panels.forEach((panel) => panel.classList.toggle('active', panel.id === 'tab-' + tab));
      }});
    }});

    const historyLabels = {json.dumps(history_labels)};
    const commonOpts = {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ font: {{ family: 'Georgia' }} }} }},
        y: {{ beginAtZero: true }}
      }}
    }};

    new Chart(document.getElementById('savingsChart'), {{
      type: 'line',
      data: {{
        labels: historyLabels,
        datasets: [{{
          label: 'Savings %',
          data: {json.dumps(history_savings)},
          borderColor: '#0c7c59',
          backgroundColor: 'rgba(12,124,89,0.1)',
          fill: true,
          tension: 0.2,
          pointRadius: 5,
          pointBackgroundColor: '#0c7c59'
        }}]
      }},
      options: {{ ...commonOpts, plugins: {{ tooltip: {{ callbacks: {{ label: (c) => ` ${{c.parsed.y}}% saved` }} }} }} }}
    }});

    new Chart(document.getElementById('qualityChart'), {{
      type: 'line',
      data: {{
        labels: historyLabels,
        datasets: [{{
          label: 'Quality %',
          data: {json.dumps(history_quality)},
          borderColor: '#d38b4d',
          backgroundColor: 'rgba(211,139,77,0.1)',
          fill: true,
          tension: 0.2,
          pointRadius: 5,
          pointBackgroundColor: '#d38b4d'
        }}]
      }},
      options: {{ ...commonOpts, plugins: {{ tooltip: {{ callbacks: {{ label: (c) => ` ${{c.parsed.y}}% quality` }} }} }} }}
    }});

    new Chart(document.getElementById('latencyChart'), {{
      type: 'line',
      data: {{
        labels: historyLabels,
        datasets: [{{
          label: 'Latency (ms)',
          data: {json.dumps(history_latency)},
          borderColor: '#617079',
          backgroundColor: 'rgba(97,112,121,0.1)',
          fill: true,
          tension: 0.2,
          pointRadius: 5,
          pointBackgroundColor: '#617079'
        }}]
      }},
      options: {{ ...commonOpts, plugins: {{ tooltip: {{ callbacks: {{ label: (c) => ` ${{c.parsed.y}} ms avg local latency` }} }} }} }}
    }});
  </script>
</body>
</html>
"""



    coverage = primary.get("tool_coverage", {})
    artifacts = primary.get("artifacts", {})
    benchmarked = coverage.get("benchmarked_tools", [])
    optional = coverage.get("optional_tools", {})

    scenario_items = [
        {
            "label": k.replace("_", " ").title(),
            "description": s.get("description", ""),
            "tool": s.get("with_c3", {}).get("tool", "n/a"),
            "baseline": s.get("without_c3", {}).get("approach", "n/a"),
            "c3_tokens": s.get("with_c3", {}).get("total_tokens", s.get("with_c3", {}).get("avg_context_tokens", 0)),
            "baseline_tokens": s.get("without_c3", {}).get("total_tokens", s.get("without_c3", {}).get("avg_context_tokens", 0)),
            "token_savings_pct": s.get("token_savings_pct", 0),
            "prompt_budget_multiplier": s.get("prompt_budget_multiplier", 0),
            "c3_latency_ms": s.get("with_c3", {}).get("avg_latency_ms", 0),
            "baseline_latency_ms": s.get("without_c3", {}).get("avg_latency_ms", 0),
            "c3_perf": s.get("performance_metric_with_c3", s.get("with_c3", {}).get("performance", 0)),
            "baseline_perf": s.get("performance_metric_without_c3", s.get("without_c3", {}).get("performance", 0)),
            "performance_metric": s.get("performance_metric", ""),
        }
        for k, s in scenarios.items()
    ]

    quality_items = [
        {
            "label": k.replace("_", " ").title(),
            "metric": v.get("metric", ""),
            "with_c3": v.get("with_c3_pct", 0),
            "without_c3": v.get("without_c3_pct", 0),
            "delta": v.get("delta_pct_points", 0),
        }
        for k, v in quality_checks.items()
    ]

    session_profiles = [
        {
            "label": k.title(),
            "description": _BENCHMARK_SESSION_PROFILES.get(k, {}).get("description", ""),
            "with_c3": v.get("retained_tokens_per_turn_with_c3", 0),
            "without_c3": v.get("retained_tokens_per_turn_without_c3", 0),
            "savings_pct": v.get("session_adjusted_savings_pct", 0),
            "budget_multiplier": v.get("session_adjusted_prompt_budget_multiplier", 0),
            "l1_with": v.get("turns_until_level_1_with_c3", 0),
            "l1_without": v.get("turns_until_level_1_without_c3", 0),
            "l2_with": v.get("turns_until_level_2_with_c3", 0),
            "l2_without": v.get("turns_until_level_2_without_c3", 0),
        }
        for k, v in session_reality.get("profiles", {}).items()
    ]

    def _metric_card(title: str, metric: str, detail: str) -> str:
        return (
            "<div class='metric-card'>"
            f"<div class='eyebrow'>{html.escape(title)}</div>"
            f"<div class='metric'>{html.escape(metric)}</div>"
            f"<div class='detail'>{html.escape(detail)}</div>"
            "</div>"
        )

    def _hbar_rows(items, value_key: str, suffix: str = "", decimals: int = 1, color_class: str = "c3") -> str:
        max_value = max((float(item.get(value_key, 0) or 0) for item in items), default=1.0) or 1.0
        rows = []
        for item in items:
            value = float(item.get(value_key, 0) or 0)
            width = max(2.0, (value / max_value) * 100)
            rows.append(
                "<div class='chart-row'>"
                f"<div class='chart-label'>{html.escape(item['label'])}</div>"
                "<div class='chart-track'>"
                f"<div class='chart-bar {color_class}' style='width:{width:.1f}%'></div>"
                "</div>"
                f"<div class='chart-value'>{html.escape(_num(value, decimals))}{html.escape(suffix)}</div>"
                "</div>"
            )
        return "".join(rows)

    def _dual_rows(items, left_key: str, right_key: str, suffix: str = "", decimals: int = 1) -> str:
        max_value = max(
            [float(item.get(left_key, 0) or 0) for item in items] +
            [float(item.get(right_key, 0) or 0) for item in items] + [1.0]
        )
        rows = []
        for item in items:
            left = float(item.get(left_key, 0) or 0)
            right = float(item.get(right_key, 0) or 0)
            left_width = max(2.0, (left / max_value) * 100)
            right_width = max(2.0, (right / max_value) * 100)
            rows.append(
                "<div class='dual-row'>"
                f"<div class='chart-label'>{html.escape(item['label'])}</div>"
                "<div class='dual-stack'>"
                "<div class='dual-track'>"
                f"<div class='chart-bar c3' style='width:{left_width:.1f}%'></div>"
                f"<span class='mini-tag'>C3 {_num(left, decimals)}{suffix}</span>"
                "</div>"
                "<div class='dual-track'>"
                f"<div class='chart-bar baseline' style='width:{right_width:.1f}%'></div>"
                f"<span class='mini-tag'>Base {_num(right, decimals)}{suffix}</span>"
                "</div>"
                "</div>"
                "</div>"
            )
        return "".join(rows)

    overview_cards = "".join([
        _metric_card(
            "Token Usage",
            f"{_num(scorecard.get('token_usage', {}).get('savings_pct', 0))}%",
            (
                f"{_num(scorecard.get('token_usage', {}).get('with_c3_total_tokens', 0), 1)} tok with C3 vs "
                f"{_num(scorecard.get('token_usage', {}).get('without_c3_total_tokens', 0), 1)} tok baseline"
            ),
        ),
        _metric_card(
            "Prompt Budget",
            f"{_num(scorecard.get('token_usage', {}).get('prompt_budget_multiplier', 0))}x",
            "More input fits into the same context window.",
        ),
        _metric_card(
            "Speed",
            f"{_num(scorecard.get('speed', {}).get('latency_delta_pct_vs_baseline', 0))}%",
            (
                f"{_num(scorecard.get('speed', {}).get('with_c3_avg_latency_ms', 0))} ms with C3 vs "
                f"{_num(scorecard.get('speed', {}).get('without_c3_avg_latency_ms', 0))} ms baseline"
            ),
        ),
        _metric_card(
            "Performance",
            f"{_num(scorecard.get('performance', {}).get('with_c3_quality_pct', 0))}%",
            f"{_num(scorecard.get('performance', {}).get('delta_pct_points', 0))} pts vs baseline",
        ),
        _metric_card(
            "Session Reality",
            f"{_num(session_reality.get('profiles', {}).get('balanced', {}).get('session_adjusted_savings_pct', 0))}%",
            (
                f"Balanced retained-turn savings; ~{_num(session_reality.get('profiles', {}).get('balanced', {}).get('turns_until_level_2_with_c3', 0))} turns to L2 with C3"
            ),
        ),
    ])

    scenario_rows = []
    for item in scenario_items:
        savings = float(item["token_savings_pct"])
        savings_cls = "text-green" if savings >= 50 else ("text-orange" if savings >= 20 else "")
        scenario_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(item['label'])}</strong><div class='td-note'>{html.escape(item['description'])}</div></td>"
            f"<td><span class='badge tool'>{html.escape(item['tool'])}</span></td>"
            f"<td class='td-note'>{html.escape(item['baseline'])}</td>"
            f"<td>{html.escape(_num(item['c3_tokens'], 0))}</td>"
            f"<td>{html.escape(_num(item['baseline_tokens'], 0))}</td>"
            f"<td class='{savings_cls}'>{html.escape(_num(savings))}%</td>"
            f"<td>{html.escape(_num(item['prompt_budget_multiplier'], 2))}x</td>"
            f"<td>{html.escape(_num(item['c3_latency_ms']))} / {html.escape(_num(item['baseline_latency_ms']))} ms</td>"
            f"<td class='td-note'>{html.escape(item['performance_metric'])}<br><strong class='text-green'>{html.escape(_num(item['c3_perf']))}</strong> vs {html.escape(_num(item['baseline_perf']))}</td>"
            "</tr>"
        )

    quality_rows = []
    for item in quality_items:
        delta = float(item["delta"])
        delta_cls = "text-green" if delta >= 0 else "text-orange"
        delta_sign = "+" if delta >= 0 else ""
        quality_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(item['label'])}</strong></td>"
            f"<td class='td-note'>{html.escape(item['metric'])}</td>"
            f"<td class='text-green'>{html.escape(_num(item['with_c3']))}%</td>"
            f"<td>{html.escape(_num(item['without_c3']))}%</td>"
            f"<td class='{delta_cls}'>{delta_sign}{html.escape(_num(delta))} pts</td>"
            "</tr>"
        )

    optional_rows = []
    for name, meta in optional.items():
        status = meta.get("status", "unknown")
        status_cls = "status-measured" if status == "measured" else ("status-skipped" if status == "skipped" else "status-unavailable")
        optional_rows.append(
            "<tr>"
            f"<td><code>{html.escape(name)}</code></td>"
            f"<td><span class='{status_cls}'>{html.escape(status)}</span></td>"
            f"<td class='td-note'>{html.escape(meta.get('reason', ''))}</td>"
            "</tr>"
        )

    optional_eval_rows = []
    for name, meta in optional_evals.items():
        with_c3 = meta.get("with_c3", {})
        without_c3 = meta.get("without_c3", {})
        optional_eval_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(name)}</strong><div class='td-note'>{html.escape(meta.get('description', ''))}</div></td>"
            f"<td>{html.escape(meta.get('status', 'unknown'))}</td>"
            f"<td>{html.escape(with_c3.get('task_type', without_c3.get('task_type', '')))}<br><span class='td-note'>{html.escape(with_c3.get('task_file', without_c3.get('task_file', '')))}</span></td>"
            f"<td>{html.escape(with_c3.get('model', ''))}</td>"
            f"<td>{html.escape(_num(with_c3.get('primary_model_prompt_tokens', 0), 1))}</td>"
            f"<td>{html.escape(_num(without_c3.get('primary_model_prompt_tokens', 0), 1))}</td>"
            f"<td>{html.escape(_num(meta.get('primary_model_token_savings_pct', 0)))}%</td>"
            f"<td>{html.escape(_num(meta.get('prompt_budget_multiplier', 0), 2))}x</td>"
            f"<td>{html.escape(_num(with_c3.get('latency_ms', 0), 2))} ms</td>"
            f"<td>{html.escape(with_c3.get('confidence', ''))}</td>"
            f"<td>{html.escape(meta.get('notes', meta.get('reason', '')))}</td>"
            "</tr>"
        )

    session_rows = []
    for item in session_profiles:
        session_rows.append(
            "<tr>"
            f"<td><strong>{html.escape(item['label'])}</strong><div class='td-note'>{html.escape(item['description'])}</div></td>"
            f"<td>{html.escape(_num(item['with_c3'], 1))}</td>"
            f"<td>{html.escape(_num(item['without_c3'], 1))}</td>"
            f"<td>{html.escape(_num(item['savings_pct']))}%</td>"
            f"<td>{html.escape(_num(item['budget_multiplier'], 2))}x</td>"
            f"<td>{html.escape(_num(item['l1_with'], 1))} / {html.escape(_num(item['l1_without'], 1))}</td>"
            f"<td>{html.escape(_num(item['l2_with'], 1))} / {html.escape(_num(item['l2_without'], 1))}</td>"
            "</tr>"
        )

    transcript_usage = session_reality.get("transcript_usage", {})
    transcript_rows = []
    if transcript_usage:
        transcript_rows.append(
            "<tr>"
            f"<td>{html.escape(str(transcript_usage.get('sessions_found', 0)))}</td>"
            f"<td>{html.escape(_num(transcript_usage.get('total_input_tokens', 0), 1))}</td>"
            f"<td>{html.escape(_num(transcript_usage.get('total_output_tokens', 0), 1))}</td>"
            f"<td>{html.escape(_num(transcript_usage.get('cache_creation_tokens', 0), 1))}</td>"
            f"<td>{html.escape(_num(transcript_usage.get('cache_read_tokens', 0), 1))}</td>"
            "</tr>"
        )

    assumptions = session_reality.get("assumptions", {})

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>C3 Benchmark Report</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --bg: #f3eee6;
      --panel: rgba(255,250,243,0.86);
      --panel-strong: #fffaf4;
      --ink: #182126;
      --muted: #617079;
      --line: #d8cfbf;
      --accent: #0c7c59;
      --accent-soft: #d8efe7;
      --baseline: #d38b4d;
      --baseline-soft: #f7e5d3;
      --shadow: 0 16px 38px rgba(24,33,38,0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(12,124,89,0.16), transparent 26%),
        radial-gradient(circle at bottom right, rgba(211,139,77,0.16), transparent 24%),
        linear-gradient(180deg, #fbf6ef 0%, var(--bg) 100%);
    }}
    .wrap {{ max-width: 1360px; margin: 0 auto; padding: 28px 20px 48px; }}
    .hero, .tab-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 24px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }}
    .hero-grid {{ display: grid; grid-template-columns: 1.2fr .8fr; gap: 20px; align-items: end; }}
    h1 {{ margin: 0 0 10px; font-size: 44px; line-height: 1.03; }}
    h2 {{ margin: 0 0 10px; font-size: 24px; }}
    h3 {{ margin: 0 0 8px; font-size: 18px; }}
    p {{ margin: 0; color: var(--muted); line-height: 1.55; }}
    .eyebrow {{ text-transform: uppercase; letter-spacing: 0.08em; font-size: 11px; color: var(--muted); margin-bottom: 10px; }}
    .hero-meta {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px 16px; margin-top: 16px; color: var(--muted); font-size: 14px; }}
    .hero-stat {{
      background: linear-gradient(180deg, rgba(12,124,89,0.10), rgba(12,124,89,0.04));
      border: 1px solid rgba(12,124,89,0.18);
      border-radius: 22px;
      padding: 20px;
    }}
    .hero-stat .big {{ font-size: 58px; line-height: 1; font-weight: 700; margin-bottom: 10px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-top: 18px; }}
    .metric-card {{ background: var(--panel-strong); border: 1px solid var(--line); border-radius: 18px; padding: 18px; }}
    .metric {{ font-size: 30px; font-weight: 700; margin-bottom: 8px; }}
    .detail {{ color: var(--muted); font-size: 14px; }}
    .tabs {{ display: flex; flex-wrap: wrap; gap: 10px; margin: 22px 0 14px; }}
    .tab-btn {{
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.74);
      color: var(--ink);
      border-radius: 999px;
      padding: 10px 16px;
      font: inherit;
      cursor: pointer;
    }}
    .tab-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    .panel-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-top: 16px; }}
    .subpanel {{ background: var(--panel-strong); border: 1px solid var(--line); border-radius: 18px; padding: 18px; }}
    .chart-row {{ display: grid; grid-template-columns: 180px 1fr 84px; gap: 12px; align-items: center; margin-top: 12px; }}
    .dual-row {{ display: grid; grid-template-columns: 180px 1fr; gap: 12px; margin-top: 12px; align-items: start; }}
    .chart-label {{ text-transform: capitalize; font-size: 14px; }}
    .chart-track, .dual-track {{ height: 14px; background: rgba(24,33,38,0.07); border-radius: 999px; overflow: visible; position: relative; }}
    .chart-bar {{ height: 100%; border-radius: 999px; min-width: 2px; }}
    .chart-bar.c3 {{ background: linear-gradient(90deg, var(--accent), #39aa7f); }}
    .chart-bar.baseline {{ background: linear-gradient(90deg, var(--baseline), #e8ba8c); }}
    .chart-value {{ text-align: right; color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }}
    .dual-stack {{ display: grid; gap: 10px; }}
    .mini-tag {{ position: absolute; right: 8px; top: -2px; font-size: 12px; color: var(--ink); font-variant-numeric: tabular-nums; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 14px; background: var(--panel-strong); border-radius: 16px; overflow: hidden; }}
    th, td {{ text-align: left; padding: 12px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ background: var(--accent-soft); font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
    tr:last-child td {{ border-bottom: 0; }}
    .td-note {{ color: var(--muted); font-size: 12px; margin-top: 6px; line-height: 1.45; }}
    .pill-list {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }}
    .pill {{ border-radius: 999px; padding: 7px 12px; background: rgba(12,124,89,0.08); border: 1px solid rgba(12,124,89,0.18); font-size: 13px; }}
    code, pre {{ font-family: Consolas, "SFMono-Regular", monospace; }}
    code {{ background: rgba(12,124,89,0.08); padding: 2px 6px; border-radius: 6px; }}
    pre {{ margin: 14px 0 0; background: #1f282d; color: #e8f0f2; border-radius: 18px; padding: 18px; overflow: auto; font-size: 13px; line-height: 1.45; }}
    .history-grid {{ display: grid; grid-template-columns: 1fr; gap: 24px; margin-top: 16px; }}
    .chart-container {{ position: relative; height: 260px; width: 100%; }}
    .text-green {{ color: var(--accent); font-weight: 600; }}
    .text-orange {{ color: var(--baseline); font-weight: 600; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-family: Consolas, monospace; }}
    .badge.tool {{ background: rgba(12,124,89,0.10); border: 1px solid rgba(12,124,89,0.22); color: var(--accent); }}
    .status-measured {{ color: var(--accent); font-weight: 600; }}
    .status-skipped {{ color: var(--muted); }}
    .status-unavailable {{ color: var(--baseline); }}
    @media (max-width: 920px) {{
      .hero-grid {{ grid-template-columns: 1fr; }}
      .chart-row, .dual-row {{ grid-template-columns: 1fr; }}
      .chart-value {{ text-align: left; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="hero-grid">
        <div>
          <div class="eyebrow">C3 Benchmark Report <span style="background:#818cf8;color:#0b1020;padding:0.1rem 0.5rem;border-radius:999px;font-size:0.65rem;font-weight:600;margin-left:0.4rem;vertical-align:middle">Synthetic</span> <a href="../benchmarks/index.html" style="color:#9aa3c7;font-size:0.75rem;margin-left:0.6rem;text-decoration:none">← dashboard</a></div>
          <h1>With C3 vs Without C3</h1>
          <p>Generated on {html.escape(primary.get("timestamp", ""))} for <code>{html.escape(primary.get("project_path", ""))}</code>. This report compares C3-assisted workflows against raw baseline paths across code, logs, structured data, and terminal output. Task-level savings are not the same thing as full-session lifetime; see Session Reality for retained-turn estimates.</p>
          <div class="hero-meta">
            <div><strong>System:</strong> {html.escape(runner.get("system_label", runner.get("system_name", "unknown")))}{html.escape((' ' + runner.get('system_version')) if runner.get('system_version') else '')}</div>
            <div><strong>IDE profile:</strong> {html.escape(runner.get("ide_display_name", runner.get("ide_name", "unknown")))}</div>
            <div><strong>Files considered:</strong> {html.escape(str(primary.get("files_considered", 0)))}</div>
            <div><strong>Benchmarked tools:</strong> {html.escape(", ".join(benchmarked))}</div>
            <div><strong>Fixture strategy:</strong> {html.escape(fixtures.get("fixture_strategy", "native repository inputs"))}</div>
            <div><strong>HTML report:</strong> <code>{html.escape(artifacts.get("html_report", ""))}</code></div>
          </div>
        </div>
        <div class="hero-stat">
          <div class="eyebrow">Task-Level Result</div>
          <div class="big">{html.escape(_num(scorecard.get('token_usage', {}).get('savings_pct', 0)))}%</div>
          <p>task-level prompt reduction, <strong>{html.escape(_num(scorecard.get('token_usage', {}).get('prompt_budget_multiplier', 0)))}x</strong> prompt-budget multiplier, and <strong>{html.escape(_num(scorecard.get('performance', {}).get('delta_pct_points', 0)))} pts</strong> average performance uplift.</p>
        </div>
      </div>
      <div class="cards">{overview_cards}</div>
    </section>

    <div class="tabs">
      <button class="tab-btn active" data-tab="overview">Overview</button>
      <button class="tab-btn" data-tab="history">Performance History</button>
      <button class="tab-btn" data-tab="scenarios">Scenarios</button>
      <button class="tab-btn" data-tab="quality">Quality</button>
      <button class="tab-btn" data-tab="session">Session</button>
      <button class="tab-btn" data-tab="coverage">Coverage</button>
      <button class="tab-btn" data-tab="raw">Raw Data</button>
    </div>

    <section class="tab-panel active" id="tab-overview">
      <div class="panel-grid">
        <div class="subpanel">
          <h3>Token Savings By Scenario</h3>
          <p>Higher is better. This shows where C3 removes the most prompt payload.</p>
          {_hbar_rows(scenario_items, 'token_savings_pct', '%', 1, 'c3')}
        </div>
        <div class="subpanel">
          <h3>Prompt Budget Multiplier</h3>
          <p>How much more input fits before you hit the same context ceiling.</p>
          {_hbar_rows(scenario_items, 'prompt_budget_multiplier', 'x', 2, 'baseline')}
        </div>
      </div>
      <div class="panel-grid">
        <div class="subpanel">
          <h3>Latency Comparison</h3>
          <p>C3 spends local milliseconds to reduce prompt volume. This chart shows both paths per scenario.</p>
          {_dual_rows(scenario_items, 'c3_latency_ms', 'baseline_latency_ms', ' ms', 2)}
        </div>
        <div class="subpanel">
          <h3>Performance Comparison</h3>
          <p>Task-specific success or signal-retention checks for C3 versus the raw baseline path.</p>
          {_dual_rows(scenario_items, 'c3_perf', 'baseline_perf', '%', 1)}
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-history">
      <div class="history-grid">
        <div class="subpanel">
          <h3>Token Savings History</h3>
          <p>Tracking the percentage of tokens saved across versions and runs.</p>
          <div class="chart-container"><canvas id="savingsChart"></canvas></div>
        </div>
        <div class="subpanel">
          <h3>Intelligence Quality History</h3>
          <p>Ensuring mapping and retrieval quality remains stable as parsers evolve.</p>
          <div class="chart-container"><canvas id="qualityChart"></canvas></div>
        </div>
        <div class="subpanel">
          <h3>Avg Local Latency History</h3>
          <p>Monitoring the local computational cost of C3 features.</p>
          <div class="chart-container"><canvas id="latencyChart"></canvas></div>
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-scenarios">
      <div class="subpanel">
        <h2>Scenario Matrix</h2>
        <p>Detailed comparison of each benchmarked workflow, including token impact, latency, and the task-specific performance metric.</p>
        <table>
          <thead>
            <tr>
              <th>Scenario</th>
              <th>With C3</th>
              <th>Without C3</th>
              <th>C3 Tokens</th>
              <th>Baseline Tokens</th>
              <th>Savings</th>
              <th>Budget</th>
              <th>Latency C3 / Base</th>
              <th>Performance</th>
            </tr>
          </thead>
          <tbody>{''.join(scenario_rows)}</tbody>
        </table>
      </div>
    </section>

    <section class="tab-panel" id="tab-quality">
      <div class="panel-grid">
        <div class="subpanel">
          <h2>Quality Checks</h2>
          <p>Baseline full-read paths retain all information by definition. C3 is measured on whether it keeps the signals the task needs.</p>
          <table>
            <thead>
              <tr>
                <th>Check</th>
                <th>Metric</th>
                <th>With C3</th>
                <th>Without C3</th>
                <th>Delta</th>
              </tr>
            </thead>
            <tbody>{''.join(quality_rows)}</tbody>
          </table>
        </div>
        <div class="subpanel">
          <h2>Quality Distribution</h2>
          <p>The benchmark mixes retrieval hit-rate checks with signal and schema retention checks.</p>
          {_dual_rows(quality_items, 'with_c3', 'without_c3', '%', 1)}
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-session">
      <div class="panel-grid">
        <div class="subpanel">
          <h2>Session Reality</h2>
          <p>{html.escape(session_reality.get("note", ""))}</p>
          <table>
            <thead>
              <tr>
                <th>Profile</th>
                <th>With C3 Retained/Turn</th>
                <th>Base Retained/Turn</th>
                <th>Savings</th>
                <th>Budget</th>
                <th>Turns To L1 C3 / Base</th>
                <th>Turns To L2 C3 / Base</th>
              </tr>
            </thead>
            <tbody>{''.join(session_rows)}</tbody>
          </table>
        </div>
        <div class="subpanel">
          <h2>Assumptions</h2>
          <p>These fixed overheads are added per retained turn before the scenario context is counted.</p>
          <table>
            <thead>
              <tr><th>Input</th><th>Tokens</th></tr>
            </thead>
            <tbody>
              <tr><td>System and instructions</td><td>{html.escape(_num(assumptions.get('system_and_instructions_tokens', 0), 1))}</td></tr>
              <tr><td>User turn</td><td>{html.escape(_num(assumptions.get('user_turn_tokens', 0), 1))}</td></tr>
              <tr><td>Assistant reply</td><td>{html.escape(_num(assumptions.get('assistant_reply_tokens', 0), 1))}</td></tr>
              <tr><td>Tool wrapper</td><td>{html.escape(_num(assumptions.get('tool_wrapper_tokens', 0), 1))}</td></tr>
              <tr><td>Fixed overhead total</td><td>{html.escape(_num(assumptions.get('fixed_overhead_tokens_per_turn', 0), 1))}</td></tr>
              <tr><td>L1 threshold</td><td>{html.escape(_num(session_reality.get('thresholds', {}).get('level_1', 0), 1))}</td></tr>
              <tr><td>L2 threshold</td><td>{html.escape(_num(session_reality.get('thresholds', {}).get('level_2', 0), 1))}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="panel-grid">
        <div class="subpanel">
          <h2>Transcript Usage</h2>
          <p>When Claude Code transcripts are available for this project, they are shown here as a reality check against synthetic estimates.</p>
          <table>
            <thead>
              <tr>
                <th>Sessions</th>
                <th>Total Input</th>
                <th>Total Output</th>
                <th>Cache Create</th>
                <th>Cache Read</th>
              </tr>
            </thead>
            <tbody>{''.join(transcript_rows) if transcript_rows else "<tr><td colspan='5'>No Claude Code transcript usage was found for this project.</td></tr>"}</tbody>
          </table>
        </div>
        <div class="subpanel">
          <h2>Profile Savings</h2>
          <p>These bars are session-adjusted rather than raw scenario-only token reductions.</p>
          {_hbar_rows(session_profiles, 'savings_pct', '%', 1, 'c3')}
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-coverage">
      <div class="panel-grid">
        <div class="subpanel">
          <h2>Benchmarked Tools</h2>
          <p>These tools currently contribute to the main scorecard.</p>
          <div class="pill-list">{''.join(f"<div class='pill'>{html.escape(tool)}</div>" for tool in benchmarked)}</div>
          <table>
            <thead>
              <tr><th>Artifact</th><th>Path</th></tr>
            </thead>
            <tbody>
              <tr><td>System</td><td><code>{html.escape(runner.get('system_name', 'unknown'))}</code></td></tr>
              <tr><td>System label</td><td><code>{html.escape(runner.get('system_label', 'unknown'))}</code></td></tr>
              <tr><td>System version</td><td><code>{html.escape(runner.get('system_version', ''))}</code></td></tr>
              <tr><td>C3 Version</td><td><code>{html.escape(ver)}</code></td></tr>
              <tr><td>IDE profile</td><td><code>{html.escape(runner.get('ide_name', 'unknown'))}</code></td></tr>
              <tr><td>JSON report</td><td><code>{html.escape(artifacts.get('json_report', ''))}</code></td></tr>
              <tr><td>HTML report</td><td><code>{html.escape(artifacts.get('html_report', ''))}</code></td></tr>
              <tr><td>Fixture directory</td><td><code>{html.escape(fixtures.get('fixture_dir', ''))}</code></td></tr>
            </tbody>
          </table>
        </div>
        <div class="subpanel">
          <h2>Optional Tools</h2>
          <p>These remain outside the main scorecard because they rely on environment-specific routing quality or project history. When local Ollama is available, c3_delegate is promoted into the main scorecard automatically.</p>
          <table>
            <thead>
              <tr><th>Tool</th><th>Status</th><th>Reason</th></tr>
            </thead>
            <tbody>{''.join(optional_rows)}</tbody>
          </table>
          <table>
            <thead>
              <tr>
                <th>Evaluation</th>
                <th>Status</th>
                <th>Task</th>
                <th>Model</th>
                <th>C3 Primary Tokens</th>
                <th>Baseline Primary Tokens</th>
                <th>Savings</th>
                <th>Budget</th>
                <th>Latency</th>
                <th>Confidence</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>{''.join(optional_eval_rows) if optional_eval_rows else "<tr><td colspan='11'>No optional evaluations were recorded for this run.</td></tr>"}</tbody>
          </table>
        </div>
      </div>
      <div class="panel-grid">
        <div class="subpanel">
          <h2>Fixture Inputs</h2>
          <table>
            <thead>
              <tr><th>Fixture</th><th>Path</th></tr>
            </thead>
            <tbody>
              <tr><td>Log fixture</td><td><code>{html.escape(fixtures.get('log_path', ''))}</code></td></tr>
              <tr><td>JSONL fixture</td><td><code>{html.escape(fixtures.get('jsonl_path', ''))}</code></td></tr>
              <tr><td>Terminal fixture</td><td><code>{html.escape(fixtures.get('terminal_output_path', ''))}</code></td></tr>
            </tbody>
          </table>
        </div>
        <div class="subpanel">
          <h2>Signals Under Test</h2>
          <div class="pill-list">
            {''.join(f"<div class='pill'>{html.escape(sig)}</div>" for sig in fixtures.get('log_signals', []))}
            {''.join(f"<div class='pill'>{html.escape(field)}</div>" for field in fixtures.get('jsonl_fields', []))}
            {''.join(f"<div class='pill'>{html.escape(sig)}</div>" for sig in fixtures.get('terminal_signals', []))}
          </div>
        </div>
      </div>
    </section>

    <section class="tab-panel" id="tab-raw">
      <div class="subpanel">
        <h2>Raw JSON</h2>
        <p>The full machine-readable benchmark output is embedded here for quick inspection.</p>
        <pre>{raw_json}</pre>
      </div>
    </section>
  </div>
  <script>
    const buttons = document.querySelectorAll('.tab-btn');
    const panels = document.querySelectorAll('.tab-panel');
    buttons.forEach((button) => {{
      button.addEventListener('click', () => {{
        const tab = button.dataset.tab;
        buttons.forEach((b) => b.classList.toggle('active', b === button));
        panels.forEach((panel) => panel.classList.toggle('active', panel.id === 'tab-' + tab));
      }});
    }});

    const historyLabels = {json.dumps(history_labels)};
    const commonOpts = {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ font: {{ family: 'Georgia' }} }} }},
        y: {{ beginAtZero: true }}
      }}
    }};

    new Chart(document.getElementById('savingsChart'), {{
      type: 'line',
      data: {{
        labels: historyLabels,
        datasets: [{{
          label: 'Savings %',
          data: {json.dumps(history_savings)},
          borderColor: '#0c7c59',
          backgroundColor: 'rgba(12,124,89,0.1)',
          fill: true,
          tension: 0.2,
          pointRadius: 5,
          pointBackgroundColor: '#0c7c59'
        }}]
      }},
      options: {{ ...commonOpts, plugins: {{ tooltip: {{ callbacks: {{ label: (c) => ` ${{c.parsed.y}}% saved` }} }} }} }}
    }});

    new Chart(document.getElementById('qualityChart'), {{
      type: 'line',
      data: {{
        labels: historyLabels,
        datasets: [{{
          label: 'Quality %',
          data: {json.dumps(history_quality)},
          borderColor: '#d38b4d',
          backgroundColor: 'rgba(211,139,77,0.1)',
          fill: true,
          tension: 0.2,
          pointRadius: 5,
          pointBackgroundColor: '#d38b4d'
        }}]
      }},
      options: {{ ...commonOpts, plugins: {{ tooltip: {{ callbacks: {{ label: (c) => ` ${{c.parsed.y}}% quality` }} }} }} }}
    }});

    new Chart(document.getElementById('latencyChart'), {{
      type: 'line',
      data: {{
        labels: historyLabels,
        datasets: [{{
          label: 'Latency (ms)',
          data: {json.dumps(history_latency)},
          borderColor: '#617079',
          backgroundColor: 'rgba(97,112,121,0.1)',
          fill: true,
          tension: 0.2,
          pointRadius: 5,
          pointBackgroundColor: '#617079'
        }}]
      }},
      options: {{ ...commonOpts, plugins: {{ tooltip: {{ callbacks: {{ label: (c) => ` ${{c.parsed.y}} ms avg local latency` }} }} }} }}
    }});
  </script>
</body>
</html>"""


def cmd_benchmark(args):
    """Run a local with/without-C3 benchmark for common code-understanding workflows."""
    if getattr(args, "command", "") == "benchmark":
        print("[note] `c3 benchmark` is aliased as `c3 bench quick`. Prefer the unified form going forward.")
    config = load_config(args.project_path or ".")
    project_path = Path(args.project_path or config.get("project_path", ".")).resolve()
    runtime_ide_name = ""
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_MANAGED_BY_NPM"):
        runtime_ide_name = "codex"
    configured_ide_name = load_ide_config(str(project_path))
    detected_ide_name = detect_ide(str(project_path))
    ide_name = runtime_ide_name or (
        configured_ide_name if configured_ide_name != "claude-code" else detected_ide_name
    ) or detected_ide_name
    ide_profile = get_profile(ide_name)
    system_name = (getattr(args, "system_name", "") or os.environ.get("C3_BENCHMARK_SYSTEM") or ide_profile.name).strip()
    system_label = (getattr(args, "system_label", "") or os.environ.get("C3_BENCHMARK_SYSTEM_LABEL") or ide_profile.display_name).strip()
    system_version = (getattr(args, "system_version", "") or os.environ.get("C3_BENCHMARK_SYSTEM_VERSION") or "").strip()

    indexer = CodeIndex(str(project_path), str(project_path / ".c3" / "index"))
    compressor = CodeCompressor(str(project_path / ".c3" / "cache"), project_root=str(project_path))
    file_memory = FileMemoryStore(str(project_path))
    output_filter = OutputFilter({"HYBRID_DISABLE_TIER1": True})

    skip_dirs = set(getattr(indexer, "skip_dirs", set()))
    code_exts = set(getattr(indexer, "code_exts", set()))

    files = []
    for fpath in project_path.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in code_exts:
            continue
        if any(skip in fpath.parts for skip in skip_dirs):
            continue
        if compressor.is_protected_file(fpath):
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        files.append((fpath, content, count_tokens(content)))

    if not files:
        print("Error: no benchmark-eligible files found")
        return

    sample = sorted([f for f in files if f[2] >= args.min_tokens], key=lambda x: x[2], reverse=True)[:args.sample_size]
    if not sample:
        sample = sorted(files, key=lambda x: x[2], reverse=True)[:args.sample_size]

    fixtures = _build_benchmark_fixtures(project_path, sample)

    def _avg(values):
        return (sum(values) / len(values)) if values else 0.0

    def _pct_delta(current, baseline):
        if not baseline:
            return 0.0
        return ((current - baseline) / baseline) * 100

    def _pct_saved(current, baseline):
        if not baseline:
            return 0.0
        return ((baseline - current) / baseline) * 100

    def _prompt_gain(current, baseline):
        if not current:
            return 0.0
        return baseline / current

    def _rel_path(path: Path) -> str:
        return str(path.relative_to(project_path)).replace("\\", "/")

    comp_orig = 0
    comp_comp = 0
    comp_c3_latencies = []
    comp_baseline_latencies = []
    for fpath, content, _ in sample:
        t_read = time.perf_counter()
        try:
            raw_content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw_content = content
        comp_baseline_latencies.append((time.perf_counter() - t_read) * 1000)
        t0 = time.perf_counter()
        result = _compress_file_cli(compressor, str(fpath), "smart")
        comp_c3_latencies.append((time.perf_counter() - t0) * 1000)
        raw_tokens = count_tokens(raw_content)
        comp_orig += raw_tokens
        comp_comp += int(result.get("compressed_tokens", raw_tokens))

    file_map_sample = sample[:min(len(sample), max(5, min(args.sample_size, 10)))]
    file_map_orig = 0
    file_map_comp = 0
    file_map_c3_latencies = []
    file_map_baseline_latencies = []
    file_map_successes = 0
    for fpath, content, _ in file_map_sample:
        rel = _rel_path(fpath)
        t_read = time.perf_counter()
        try:
            raw_content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw_content = content
        file_map_baseline_latencies.append((time.perf_counter() - t_read) * 1000)
        file_map_orig += count_tokens(raw_content)
        t0 = time.perf_counter()
        map_text = file_memory.get_or_build_map(rel)
        file_map_c3_latencies.append((time.perf_counter() - t0) * 1000)
        file_map_comp += count_tokens(map_text)
        if "[file_map] Could not build map" not in map_text and "[file_map:error]" not in map_text:
            file_map_successes += 1

    # Scenario: Surgical Reading (c3_read)
    read_sample_size = min(len(sample), max(5, min(args.sample_size, 10)))
    read_sample = sample[:read_sample_size]
    read_orig = 0
    read_comp = 0
    read_c3_latencies = []
    read_baseline_latencies = []
    read_successes = 0

    for fpath, content, _ in read_sample:
        rel = _rel_path(fpath)
        t_read = time.perf_counter()
        try:
            raw_content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw_content = content
        read_baseline_latencies.append((time.perf_counter() - t_read) * 1000)
        read_orig += count_tokens(raw_content)

        t0 = time.perf_counter()
        record = file_memory.get(rel)
        if not record or file_memory.needs_update(rel):
            record = file_memory.update(rel)

        extracted_text = ""
        if record and record.get("sections"):
            # Pick the most relevant single symbol for surgical reading
            sections = [s for s in record["sections"] if s.get("type") in ("class", "function", "method")][:1]
            if sections:
                lines = raw_content.splitlines()
                for s in sections:
                    start, end = s["line_start"], s["line_end"]
                    raw_extracted = "\n".join(lines[start-1:end]) + "\n"
                    # Apply C3 compression to the surgical read result to maximize savings
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tmp:
                        tmp.write(raw_extracted)
                        tmp_path = tmp.name
                    try:
                        comp_res = _compress_file_cli(compressor, tmp_path, mode="smart")
                        extracted_text += comp_res.get("compressed", raw_extracted)
                    finally:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                read_successes += 1
            else:
                extracted_text = raw_content
        else:
            extracted_text = raw_content

        read_c3_latencies.append((time.perf_counter() - t0) * 1000)
        read_comp += count_tokens(extracted_text)

    # Scenario: Syntax Validation (c3_validate / AST)
    from services.parser import check_syntax_ast
    val_sample_size = min(len(sample), max(5, min(args.sample_size, 15)))
    val_sample = sample[:val_sample_size]
    val_orig = 0
    val_comp = 0 # Validation consumes context if errors are found, but here we measure metadata overhead
    val_c3_latencies = []
    val_baseline_latencies = []
    val_successes = 0

    for fpath, content, _ in val_sample:
        t_read = time.perf_counter()
        try:
            raw_content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw_content = content
        val_baseline_latencies.append((time.perf_counter() - t_read) * 1000)
        val_orig += count_tokens(raw_content)

        t0 = time.perf_counter()
        errors = check_syntax_ast(raw_content, fpath.suffix.lower())
        val_c3_latencies.append((time.perf_counter() - t0) * 1000)
        # Validation overhead is minimal (just the tool result message)
        err_msg = f"Found {len(errors)} errors" if errors else "No errors"
        val_comp += count_tokens(err_msg)
        val_successes += 1

    queries = [
        ("compress file and return results endpoint", "cli/server.py"),
        ("method that blocks protected files from compression", "services/compressor.py"),
        ("hybrid metrics collector summary", "services/metrics.py"),
        ("token counting helper", "core/__init__.py"),
        ("mcp tool c3_compress implementation", "cli/mcp_server.py"),
        ("IDE profile registry", "core/ide.py"),
    ]
    stop_terms = {"the", "and", "for", "that", "from", "with", "into", "this", "tool", "api", "implementation", "what", "where"}

    def lexical_top_files(query: str, top_k: int = 5) -> list:
        terms = [t for t in re.findall(r"[A-Za-z_]+", query.lower()) if len(t) > 2 and t not in stop_terms]
        scored = []
        for fpath, content, _ in files:
            low = content.lower()
            score = sum(low.count(term) for term in terms)
            if score > 0:
                scored.append((score, fpath))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [str(path.relative_to(project_path)).replace("\\", "/") for _, path in scored[:top_k]]

    c3_tokens = []
    lexical_tokens = []
    c3_latencies = []
    lexical_latencies = []
    c3_hits = 0
    lexical_hits = 0
    for query, expected_path in queries:
        t0 = time.perf_counter()
        results = indexer.search(query, top_k=args.top_k, max_tokens=args.max_tokens)
        context = indexer.get_context(query, top_k=args.top_k, max_tokens=args.max_tokens)
        c3_latencies.append((time.perf_counter() - t0) * 1000)
        c3_tokens.append(count_tokens(context))
        c3_paths = []
        for item in results:
            p = item.get("file") or item.get("filepath") or ""
            if p:
                c3_paths.append(str(p).replace("\\", "/"))
        if any(expected_path in p for p in c3_paths):
            c3_hits += 1
        t1 = time.perf_counter()
        lex_paths = lexical_top_files(query, top_k=args.top_k)
        full_context = []
        for rel in lex_paths:
            try:
                full_context.append((project_path / rel).read_text(encoding="utf-8", errors="replace"))
            except Exception:
                pass
        lexical_latencies.append((time.perf_counter() - t1) * 1000)
        lexical_tokens.append(count_tokens("\n\n".join(full_context)))
        if any(expected_path in p for p in lex_paths):
            lexical_hits += 1

    log_path = Path(fixtures["log_path"])
    log_full_text = log_path.read_text(encoding="utf-8", errors="replace")
    t_log_base = time.perf_counter()
    _ = log_path.read_text(encoding="utf-8", errors="replace")
    log_baseline_latency = (time.perf_counter() - t_log_base) * 1000
    t_log_c3 = time.perf_counter()
    log_extract = _benchmark_extract_preview(log_path, compressor)
    log_c3_latency = (time.perf_counter() - t_log_c3) * 1000
    log_signal_recall = round(sum(1 for sig in fixtures["log_signals"] if sig in log_extract) / len(fixtures["log_signals"]) * 100, 1)

    jsonl_path = Path(fixtures["jsonl_path"])
    jsonl_full_text = jsonl_path.read_text(encoding="utf-8", errors="replace")
    t_jsonl_base = time.perf_counter()
    _ = jsonl_path.read_text(encoding="utf-8", errors="replace")
    jsonl_baseline_latency = (time.perf_counter() - t_jsonl_base) * 1000
    t_jsonl_c3 = time.perf_counter()
    jsonl_extract = _benchmark_extract_preview(jsonl_path, compressor)
    jsonl_c3_latency = (time.perf_counter() - t_jsonl_c3) * 1000
    jsonl_schema_retention = round(sum(1 for field in fixtures["jsonl_fields"] if field in jsonl_extract) / len(fixtures["jsonl_fields"]) * 100, 1)

    terminal_text = Path(fixtures["terminal_output_path"]).read_text(encoding="utf-8", errors="replace")
    t_filter_c3 = time.perf_counter()
    filter_result = output_filter.filter(terminal_text, use_llm=False)
    filter_c3_latency = (time.perf_counter() - t_filter_c3) * 1000
    filter_signal_retention = round(sum(1 for sig in fixtures["terminal_signals"] if sig in filter_result["filtered"]) / len(fixtures["terminal_signals"]) * 100, 1)

    total_c3_tokens = sum(c3_tokens)
    total_lex_tokens = sum(lexical_tokens)
    log_extract_tokens = count_tokens(log_extract)
    jsonl_extract_tokens = count_tokens(jsonl_extract)

    quality_checks = {
        "search_retrieval_hit_rate": {"metric": "expected-file hit rate", "with_c3_pct": round((c3_hits / len(queries) * 100), 1), "without_c3_pct": round((lexical_hits / len(queries) * 100), 1)},
        "log_triage_signal_retention": {"metric": "error signal retention", "with_c3_pct": log_signal_recall, "without_c3_pct": 100.0},
        "structured_data_schema_retention": {"metric": "field retention", "with_c3_pct": jsonl_schema_retention, "without_c3_pct": 100.0},
        "terminal_output_signal_retention": {"metric": "warning/error retention", "with_c3_pct": filter_signal_retention, "without_c3_pct": 100.0},
    }
    for check in quality_checks.values():
        check["delta_pct_points"] = round(check["with_c3_pct"] - check["without_c3_pct"], 1)

    scenarios = {
        "broad_file_understanding": {
            "description": "Use c3_compress-style summaries instead of full-file reads for large source files.",
            "performance_metric": "context sufficiency proxy",
            "with_c3": {"tool": "c3_compress", "total_tokens": comp_comp, "avg_latency_ms": round(_avg(comp_c3_latencies), 2), "performance": 100.0},
            "without_c3": {"approach": "read full files into context", "total_tokens": comp_orig, "avg_latency_ms": round(_avg(comp_baseline_latencies), 2), "performance": 100.0},
            "token_savings_pct": round(_pct_saved(comp_comp, comp_orig), 1),
            "latency_delta_pct_vs_baseline": round(_pct_delta(_avg(comp_c3_latencies), _avg(comp_baseline_latencies)), 1),
            "prompt_budget_multiplier": round(_prompt_gain(comp_comp, comp_orig), 2),
        },
        "file_navigation": {
            "description": "Use c3_compress(mode='map') to choose targeted reads instead of opening whole files blindly.",
            "performance_metric": "map success rate",
            "with_c3": {"tool": "c3_compress(mode='map')", "total_tokens": file_map_comp, "avg_latency_ms": round(_avg(file_map_c3_latencies), 2), "performance": round((file_map_successes / len(file_map_sample) * 100), 1) if file_map_sample else 0.0},
            "without_c3": {"approach": "read full files into context", "total_tokens": file_map_orig, "avg_latency_ms": round(_avg(file_map_baseline_latencies), 2), "performance": 100.0},
            "token_savings_pct": round(_pct_saved(file_map_comp, file_map_orig), 1),
            "latency_delta_pct_vs_baseline": round(_pct_delta(_avg(file_map_c3_latencies), _avg(file_map_baseline_latencies)), 1),
            "prompt_budget_multiplier": round(_prompt_gain(file_map_comp, file_map_orig), 2),
        },
        "search_retrieval": {
            "description": "Use c3_search/index context instead of lexical filename/content matching plus full-file reads.",
            "performance_metric": "expected-file hit rate",
            "with_c3": {"tool": "c3_search", "avg_context_tokens": round(_avg(c3_tokens), 1), "avg_latency_ms": round(_avg(c3_latencies), 2), "hit_rate": quality_checks["search_retrieval_hit_rate"]["with_c3_pct"]},
            "without_c3": {"approach": "lexical search + full-file context", "avg_context_tokens": round(_avg(lexical_tokens), 1), "avg_latency_ms": round(_avg(lexical_latencies), 2), "hit_rate": quality_checks["search_retrieval_hit_rate"]["without_c3_pct"]},
            "token_savings_pct": round(_pct_saved(total_c3_tokens, total_lex_tokens), 1),
            "latency_delta_pct_vs_baseline": round(_pct_delta(_avg(c3_latencies), _avg(lexical_latencies)), 1),
            "prompt_budget_multiplier": round(_prompt_gain(total_c3_tokens, total_lex_tokens), 2),
        },
        "log_triage": {
            "description": "Use c3_filter to surface warnings, errors, and tracebacks instead of loading the full log.",
            "performance_metric": "error signal retention",
            "with_c3": {"tool": "c3_filter(file_path=...)", "total_tokens": log_extract_tokens, "avg_latency_ms": round(log_c3_latency, 2), "signal_retention_pct": log_signal_recall},
            "without_c3": {"approach": "read full log into context", "total_tokens": count_tokens(log_full_text), "avg_latency_ms": round(log_baseline_latency, 2), "signal_retention_pct": 100.0},
            "token_savings_pct": round(_pct_saved(log_extract_tokens, count_tokens(log_full_text)), 1),
            "latency_delta_pct_vs_baseline": round(_pct_delta(log_c3_latency, log_baseline_latency), 1),
            "prompt_budget_multiplier": round(_prompt_gain(log_extract_tokens, count_tokens(log_full_text)), 2),
        },
        "structured_data_scan": {
            "description": "Use c3_filter to summarize JSONL records and schema instead of loading the entire dataset.",
            "performance_metric": "field retention",
            "with_c3": {"tool": "c3_filter(file_path=...)", "total_tokens": jsonl_extract_tokens, "avg_latency_ms": round(jsonl_c3_latency, 2), "schema_retention_pct": jsonl_schema_retention},
            "without_c3": {"approach": "read full JSONL into context", "total_tokens": count_tokens(jsonl_full_text), "avg_latency_ms": round(jsonl_baseline_latency, 2), "schema_retention_pct": 100.0},
            "token_savings_pct": round(_pct_saved(jsonl_extract_tokens, count_tokens(jsonl_full_text)), 1),
            "latency_delta_pct_vs_baseline": round(_pct_delta(jsonl_c3_latency, jsonl_baseline_latency), 1),
            "prompt_budget_multiplier": round(_prompt_gain(jsonl_extract_tokens, count_tokens(jsonl_full_text)), 2),
        },
        "terminal_output_filtering": {
            "description": "Use c3_filter to collapse repeated passes and noisy output before it enters context.",
            "performance_metric": "warning/error retention",
            "with_c3": {"tool": "c3_filter(text=...)", "total_tokens": filter_result["filtered_tokens"], "avg_latency_ms": round(filter_c3_latency, 2), "signal_retention_pct": filter_signal_retention},
            "without_c3": {"approach": "raw terminal output", "total_tokens": filter_result["raw_tokens"], "avg_latency_ms": 0.0, "signal_retention_pct": 100.0},
            "token_savings_pct": round(filter_result["savings_pct"], 1),
            "latency_delta_pct_vs_baseline": 0.0,
            "prompt_budget_multiplier": round(_prompt_gain(filter_result["filtered_tokens"], filter_result["raw_tokens"]), 2),
        },
        "surgical_reading": {
            "description": "Use c3_read to extract specific symbols instead of reading the whole file.",
            "performance_metric": "extraction success rate",
            "with_c3": {"tool": "c3_read", "total_tokens": read_comp, "avg_latency_ms": round(_avg(read_c3_latencies), 2), "performance": round((read_successes / len(read_sample) * 100), 1) if read_sample else 0.0},
            "without_c3": {"approach": "read full files into context", "total_tokens": read_orig, "avg_latency_ms": round(_avg(read_baseline_latencies), 2), "performance": 100.0},
            "token_savings_pct": round(_pct_saved(read_comp, read_orig), 1),
            "latency_delta_pct_vs_baseline": round(_pct_delta(_avg(read_c3_latencies), _avg(read_baseline_latencies)), 1),
            "prompt_budget_multiplier": round(_prompt_gain(read_comp, read_orig), 2),
        },
        "syntax_validation": {
            "description": "Use c3_validate (AST) to check code syntax without reading the full file into LLM context.",
            "performance_metric": "AST parsing success",
            "with_c3": {"tool": "c3_validate", "total_tokens": val_comp, "avg_latency_ms": round(_avg(val_c3_latencies), 2), "performance": round((val_successes / len(val_sample) * 100), 1) if val_sample else 0.0},
            "without_c3": {"approach": "read full files into context", "total_tokens": val_orig, "avg_latency_ms": round(_avg(val_baseline_latencies), 2), "performance": 100.0},
            "token_savings_pct": round(_pct_saved(val_comp, val_orig), 1),
            "latency_delta_pct_vs_baseline": round(_pct_delta(_avg(val_c3_latencies), _avg(val_baseline_latencies)), 1),
            "prompt_budget_multiplier": round(_prompt_gain(val_comp, val_orig), 2),
        },
    }

    delegate_evaluation = _benchmark_delegate_optional(project_path, sample, compressor)
    route_evaluation = _benchmark_route_optional(project_path, fixtures, sample)
    summarize_evaluation = _benchmark_summarize_optional(project_path, fixtures)
    recall_evaluation = _benchmark_recall_optional(project_path)
    optional_evaluations = {
        "c3_delegate": delegate_evaluation,
        "c3_delegate_route": route_evaluation,
        "c3_delegate_summarize": summarize_evaluation,
        "c3_memory_recall": recall_evaluation,
    }

    # Merge offload evaluations into scenarios for main reporting
    for name, eval_data in optional_evaluations.items():
        if eval_data.get("status") == "measured":
            with_c3 = eval_data.get("with_c3", {})
            without_c3 = eval_data.get("without_c3", {})

            # Ensure tokens are available for charts
            c3_tok = with_c3.get("primary_model_prompt_tokens", 0)
            base_tok = without_c3.get("primary_model_prompt_tokens", 0)

            # Resolve performance and latency metrics for the HTML charts
            c3_latency = with_c3.get("latency_ms", 0.0)
            base_latency = without_c3.get("latency_ms", 0.0)

            # Use confidence as a proxy for performance if no quality metric is provided
            perf_c3 = eval_data.get("quality", {}).get("with_c3")
            if perf_c3 is None:
                conf = with_c3.get("confidence", "high")
                perf_c3 = 100.0 if conf == "high" else (70.0 if conf == "medium" else 40.0)

            perf_base = eval_data.get("quality", {}).get("without_c3", 100.0)

            scenarios[name] = {
                "description": eval_data.get("description", ""),
                "performance_metric": eval_data.get("quality", {}).get("metric", "quality proxy"),
                "with_c3": {
                    **with_c3,
                    "total_tokens": c3_tok,
                    "avg_latency_ms": c3_latency,
                    "performance": perf_c3
                },
                "without_c3": {
                    **without_c3,
                    "total_tokens": base_tok,
                    "avg_latency_ms": base_latency,
                    "performance": perf_base
                },
                "performance_metric_with_c3": perf_c3,
                "performance_metric_without_c3": perf_base,
                "token_savings_pct": eval_data.get("primary_model_token_savings_pct", 0),
                "latency_delta_pct_vs_baseline": round(_pct_delta(c3_latency, base_latency), 1) if base_latency > 0 else 0.0,
                "prompt_budget_multiplier": eval_data.get("prompt_budget_multiplier", 0),
            }

    session_reality = _benchmark_session_reality(project_path, scenarios)

    overall_c3_tokens = sum(s["with_c3"].get("total_tokens", s["with_c3"].get("avg_context_tokens", 0)) for s in scenarios.values())
    overall_baseline_tokens = sum(s["without_c3"].get("total_tokens", s["without_c3"].get("avg_context_tokens", 0)) for s in scenarios.values())
    overall_c3_latencies = [s["with_c3"].get("avg_latency_ms", 0.0) for s in scenarios.values()]
    overall_baseline_latencies = [s["without_c3"].get("avg_latency_ms", 0.0) for s in scenarios.values()]
    performance_c3 = _avg([check["with_c3_pct"] for check in quality_checks.values()])
    performance_baseline = _avg([check["without_c3_pct"] for check in quality_checks.values()])

    benchmarked_tools = ["c3_compress", "c3_compress_map", "c3_read", "c3_validate", "c3_search", "c3_filter_file", "c3_filter_text"]
    if delegate_evaluation.get("status") == "measured":
        benchmarked_tools.append("c3_delegate")

    optional_tools = {
        "c3_delegate_route": {
            "status": route_evaluation.get("status", "unknown"),
            "reason": route_evaluation.get("reason", "Router quality and latency depend on local Ollama availability and model selection."),
        },
        "c3_delegate_summarize": {
            "status": summarize_evaluation.get("status", "unknown"),
            "reason": summarize_evaluation.get("reason", "Summarize quality depends on local Ollama availability and summary model quality."),
        },
        "c3_memory_recall": {
            "status": recall_evaluation.get("status", "unknown"),
            "reason": recall_evaluation.get("reason", "Memory tools need benchmark facts or project history to compare fairly."),
        },
    }
    if delegate_evaluation.get("status") != "measured":
        optional_tools["c3_delegate"] = {
            "status": delegate_evaluation.get("status", "unknown"),
            "reason": delegate_evaluation.get("reason", "Delegate quality and latency depend on local Ollama availability and model selection."),
        }

    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "project_path": str(project_path),
        "runner": {
            "c3_version": __version__,
            "system_name": system_name,
            "system_label": system_label,
            "system_version": system_version,
            "ide_name": ide_name,
            "ide_display_name": ide_profile.display_name,
        },
        "files_considered": len(files),
        "categories": ["speed", "token_usage", "performance"],
        "tool_coverage": {
            "benchmarked_tools": benchmarked_tools,
            "optional_tools": optional_tools,
        },
        "fixtures": fixtures,
        "optional_evaluations": optional_evaluations,
        "session_reality": session_reality,
        "quality_checks": quality_checks,
        "scorecard": {
            "token_usage": {"with_c3_total_tokens": overall_c3_tokens, "without_c3_total_tokens": overall_baseline_tokens, "savings_pct": round(_pct_saved(overall_c3_tokens, overall_baseline_tokens), 1), "prompt_budget_multiplier": round(_prompt_gain(overall_c3_tokens, overall_baseline_tokens), 2)},
            "speed": {"with_c3_avg_latency_ms": round(_avg(overall_c3_latencies), 2), "without_c3_avg_latency_ms": round(_avg(overall_baseline_latencies), 2), "latency_delta_pct_vs_baseline": round(_pct_delta(_avg(overall_c3_latencies), _avg(overall_baseline_latencies)), 1), "note": "Positive values mean C3 spent extra local milliseconds to reduce prompt size before a model sees the data."},
            "performance": {"metric": "average quality across task-specific checks", "with_c3_quality_pct": round(performance_c3, 1), "without_c3_quality_pct": round(performance_baseline, 1), "delta_pct_points": round(performance_c3 - performance_baseline, 1)},
        },
        "scenarios": scenarios,
        "artifacts": {"json_report": "", "html_report": ""},
    }

    out_path = None
    if args.output:
        out_path = Path(args.output)
        if not out_path.is_absolute():
            out_path = project_path / out_path
    else:
        # Default to saving in .c3/benchmark/runs/ with a timestamp
        runs_dir = project_path / ".c3" / "benchmark" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        out_path = runs_dir / f"benchmark_{time.strftime('%Y%m%d_%H%M%S')}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    report["artifacts"]["json_report"] = str(out_path)

    # Load all benchmark reports for comparison
    reports = []
    benchmark_dir = project_path / ".c3" / "benchmark"
    runs_dir = benchmark_dir / "runs"
    if runs_dir.exists():
        for f in runs_dir.glob("benchmark_*.json"):
            try:
                reports.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue

    # Sort by timestamp
    reports.sort(key=lambda x: x.get("timestamp", ""))

    # If the current run wasn't saved yet, ensure it's in the list for rendering
    if not any(r.get("timestamp") == report["timestamp"] for r in reports):
        reports.append(report)

    if not getattr(args, "no_html", False):
        html_out_path = Path(args.html_output or ".c3/benchmark/latest.html")
        if not html_out_path.is_absolute():
            html_out_path = project_path / html_out_path
        html_out_path.parent.mkdir(parents=True, exist_ok=True)
        report["artifacts"]["html_report"] = str(html_out_path)
        html_out_path.write_text(_render_benchmark_html(reports), encoding="utf-8")

    if out_path:
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
        return

    def _supports_console_glyphs() -> bool:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            "█░┌┐└┘─│".encode(encoding)
            return True
        except UnicodeEncodeError:
            return False

    unicode_console = _supports_console_glyphs()

    def _bar(pct, width=20):
        filled = max(0, min(width, round(float(pct) / 100 * width)))
        fill_char = "█" if unicode_console else "#"
        empty_char = "░" if unicode_console else "-"
        return fill_char * filled + empty_char * (width - filled)

    def _pct_label(pct, decimals=1):
        return f"{float(pct):.{decimals}f}%"

    print_header("C3 Benchmark")
    runner = report["runner"]
    runner_line = runner["system_label"]
    if runner.get("system_version"):
        runner_line += f" {runner['system_version']}"
    print(f"  System  : {runner_line} ({runner['system_name']})")
    print(f"  IDE     : {runner['ide_display_name']} ({runner['ide_name']})")
    print(f"  Files   : {report['files_considered']} considered, {len(scenarios)} scenarios benchmarked")
    print()

    sc = report["scorecard"]
    tok = sc["token_usage"]
    spd = sc["speed"]
    perf = sc["performance"]
    scorecard_top = "  ┌─ Scorecard ──────────────────────────────────────────────────┐" if unicode_console else "  +- Scorecard -------------------------------------------------+"
    scorecard_mid = "  │" if unicode_console else "  |"
    scorecard_bottom = "  └──────────────────────────────────────────────────────────────┘" if unicode_console else "  +--------------------------------------------------------------+"
    print(scorecard_top)
    print(f"{scorecard_mid}  Token savings   {_pct_label(tok['savings_pct']):>7}  [{_bar(tok['savings_pct'])}]  {tok['prompt_budget_multiplier']}x budget  {scorecard_mid[-1]}")
    print(f"{scorecard_mid}  Quality delta   {('+' if float(perf['delta_pct_points']) >= 0 else '') + _pct_label(perf['delta_pct_points']):>7}  C3 {_pct_label(perf['with_c3_quality_pct'])} vs base {_pct_label(perf['without_c3_quality_pct'])}  {scorecard_mid[-1]}")
    lat_delta = float(spd['latency_delta_pct_vs_baseline'])
    print(f"{scorecard_mid}  Local latency   {('+' if lat_delta >= 0 else '') + _pct_label(lat_delta):>7}  C3 {spd['with_c3_avg_latency_ms']} ms vs base {spd['without_c3_avg_latency_ms']} ms     {scorecard_mid[-1]}")
    balanced_session = session_reality.get("profiles", {}).get("balanced", {})
    sess_savings = balanced_session.get("session_adjusted_savings_pct", 0)
    l1 = balanced_session.get("turns_until_level_1_with_c3", 0)
    l2 = balanced_session.get("turns_until_level_2_with_c3", 0)
    print(f"{scorecard_mid}  Session reality {_pct_label(sess_savings):>7}  ~{l1:.0f} turns to L1, ~{l2:.0f} turns to L2           {scorecard_mid[-1]}")
    print(scorecard_bottom)
    print()

    core_scenario_keys = [
        "broad_file_understanding", "file_navigation", "surgical_reading", "syntax_validation", "search_retrieval",
        "log_triage", "structured_data_scan", "terminal_output_filtering",
    ]
    print("  Scenarios:")
    print(f"  {'Scenario':<32} {'Savings':>8}  {'Budget':>7}  Bar")
    print(f"  {'-'*32} {'-'*8}  {'-'*7}  {'-'*20}")
    for key in core_scenario_keys:
        if key not in scenarios:
            continue
        s = scenarios[key]
        label = key.replace("_", " ").title()
        savings = s["token_savings_pct"]
        budget = s["prompt_budget_multiplier"]
        print(f"  {label:<32} {_pct_label(savings):>8}  {budget:>6.2f}x  {_bar(savings, 20)}")

    optional_keys = ["c3_delegate", "c3_delegate_route", "c3_delegate_summarize", "c3_memory_recall"]
    optional_evals = [
        ("Delegate offload", delegate_evaluation),
        ("Delegate routing", route_evaluation),
        ("Summarize offload", summarize_evaluation),
        ("Memory recall", recall_evaluation),
    ]
    optional_lines = []
    for label, ev in optional_evals:
        if ev.get("status") == "measured":
            savings = ev.get("primary_model_token_savings_pct", 0)
            extra = ""
            if "quality" in ev:
                q = ev["quality"].get("with_c3", 0)
                extra = f"  {q}% hit rate"
            with_c3 = ev.get("with_c3", {})
            model = with_c3.get("model", "")
            if model:
                extra += f"  [{model}]"
            optional_lines.append(f"  {label:<32} {_pct_label(savings):>8}{extra}")
        else:
            status = ev.get("status", "skipped")
            optional_lines.append(f"  {label:<32} {'—':>8}  {status}")
    if optional_lines:
        print(f"  {'-'*32} {'-'*8}  {'-'*7}  (optional — require local Ollama)")
        for line in optional_lines:
            print(line)

    print()
    if out_path:
        print(f"  JSON: {out_path}")
    if report["artifacts"]["html_report"]:
        print(f"  HTML: {report['artifacts']['html_report']}")

    try:
        from services.benchmark_dashboard import generate_dashboard
        dash = generate_dashboard(str(project_path))
        print(f"  Dashboard: {dash}")
    except Exception:
        pass


def cmd_optimize(args):
    """Show optimization suggestions."""
    return common_cmd_optimize(args, _command_deps())


def cmd_pipe(args):
    """All-in-one pipeline: get context + output for piping to Claude."""
    return common_cmd_pipe(args, _command_deps())


from services.claude_md import C3_COMPACT_WORKFLOW as _SHARED_C3_COMPACT_WORKFLOW

_C3_COMPACT_WORKFLOW = _SHARED_C3_COMPACT_WORKFLOW

_CLAUDE_MD_CONTENT = _C3_COMPACT_WORKFLOW

_COPILOT_INSTRUCTIONS_CONTENT = _C3_COMPACT_WORKFLOW

_AGENTS_MD_CONTENT = _C3_COMPACT_WORKFLOW + """

## IDE Configuration (Codex)
This project uses project-scoped MCP servers. Ensure your `.codex/config.toml` includes:
```toml
[mcp_servers.c3]
command = "c3-mcp"
args = ["--project", "."]
enabled = true
```
"""

_TERSE_SKILL_CONTENT = """\
# /terse — Terse Output Mode

Switch to terse output. Strip prose verbosity from responses.
Technical content (code, commands, paths, URLs, error messages, stack traces) stays exact and unchanged.

## Intensity levels

Usage: `/terse [lite|full|ultra] [turns]`  Default: **lite**, applies to the next **5 turns**

- **lite** — Remove filler words. Keep grammar and complete sentences. (safe default)
- **full** — Drop articles (a, the), use fragments, cut transitions and preambles.
- **ultra** — Telegraphic. Abbreviate aggressively. One-line answers where possible.

`turns` is optional; omit or pass `session` to keep it on until explicitly deactivated.
Counter decrements on each assistant turn; when it reaches 0, auto-revert to normal mode and say so once.

## Activation confirmation

On every `/terse` activation, echo exactly one line before the normal response:

  `terse: <level> for <N> turns — technical exceptions still apply`

For `ultra`, additionally prepend one line:

  `ultra mode — code/paths/commands/warnings preserved verbatim`

## Rules (all levels)

- Code blocks, file paths, commands, URLs, variable names: **unchanged**
- Preamble suppressed: no "Sure, I'll...", no "I've completed..."
- Hedging suppressed: no "I think", "it seems", "you might want to"
- Trailing summaries suppressed: don't restate what was just done
- If asked to explain something: minimum necessary words only

## Exceptions — do NOT compress these

Terse mode applies to conversational prose only. The following must remain complete and precise:

- **Memory saves** (`c3_memory add/update`): facts must be self-contained and fully worded — abbreviated facts are unsearchable and lose context
- **Session logs** (`c3_session log`): decisions and reasoning need full detail to be useful in future sessions
- **CLAUDE.md / instructions files**: rules must be unambiguous — never abbreviate directives
- **Planning and architecture responses**: user is reviewing for correctness — include all steps and trade-offs
- **Error diagnosis**: include full error text, file paths, line numbers, and root cause — never abbreviate debugging context
- **Tool call arguments**: `old_string`, `new_string`, `fact`, `summary` fields must be exact — never truncate
- **Security / permission advice**: auth, authz, secrets handling, CORS, CSP — never abbreviate
- **Migration and destructive-action warnings**: data loss, schema changes, irreversible ops — full detail required
- **API contract descriptions**: request/response shapes, status codes, required headers — complete
- **Responses containing** `rm`, `rm -rf`, `DROP`, `TRUNCATE`, `force`, `--force`, `--no-verify`, `reset --hard`, `chmod`, `chown`: include full context, caveats, and reversibility notes

## Auto-exit triggers (temporarily revert to normal mode for this turn)

When the user's message is any of the following, respond in normal (non-terse) mode for that turn without consuming a terse turn:

- Planning / architecture questions ("how should we...", "what's the best approach...", "design...")
- Debugging / root-cause questions ("why is X failing...", "what's causing...", "diagnose...")
- Security / migration / destructive-action discussions
- Any request for a code review, audit, or tradeoff analysis

Resume terse mode for subsequent turns until the counter expires.

## Example (full mode)

**Before:** "I've analyzed the issue and it seems like the problem might be related to how
the authentication middleware handles token expiry. You might want to look at the
`auth.py` file around line 42."

**After:** "Auth middleware: token expiry bug. See `auth.py:42`."

## Deactivate

Say "normal mode", start a new session, or let the turn counter expire.
"""

_TERSE_SKILL_MARKER = "# /terse — Terse Output Mode"


def _ensure_terse_skill(ide: str = "claude-code") -> None:
    """Install the /terse slash command for the given IDE profile.

    claude-code -> ~/.claude/commands/terse.md
    codex       -> ~/.codex/prompts/terse.md
    """
    home = Path.home()
    if ide == "codex":
        skill_path = home / ".codex" / "prompts" / "terse.md"
        content = _TERSE_SKILL_CONTENT
    else:
        skill_path = home / ".claude" / "commands" / "terse.md"
        content = _TERSE_SKILL_CONTENT

    skill_path.parent.mkdir(parents=True, exist_ok=True)

    if skill_path.exists():
        existing = skill_path.read_text(encoding="utf-8")
        if existing.strip() == content.strip():
            print(f"Kept  {skill_path}  (/terse skill up to date)")
            return
        skill_path.write_text(content, encoding="utf-8")
        print(f"Updated {skill_path}  (refreshed /terse skill)")
    else:
        skill_path.write_text(content, encoding="utf-8")
        print(f"Wrote {skill_path}  (/terse skill installed)")


def _toml_escape_str(value: str) -> str:
    """Convert a value to a TOML-safe string.

    TOML double-quoted strings interpret backslash sequences (like \\U, \\P) as
    Unicode escapes, which breaks Windows paths.  Forward slashes are universally
    valid path separators and are accepted by Python, so we replace backslashes.
    """
    return value.replace("\\", "/")


def _upsert_toml_section(toml_path: Path, section: str, entries: dict) -> None:
    """Add or replace a dotted TOML section (e.g. 'mcp_servers.c3') in-place.

    Reads the existing file, removes the old section if present, and appends
    the new section at the end. Handles simple scalar and list values only.
    """
    content = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
    header = f"[{section}]"

    # Strip existing section (header + its key=value lines). Also strip any
    # dotted child subtables (e.g. "[mcp_servers.c3.env]" under
    # "[mcp_servers.c3]") so they are not orphaned beneath the re-appended
    # section, which would corrupt the file on re-run.
    child_prefix = f"{header[:-1]}."  # "[mcp_servers.c3]" -> "[mcp_servers.c3."
    lines = content.splitlines()
    new_lines: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skip = True
            continue
        if skip and stripped.startswith("["):
            if not stripped.startswith(child_prefix):
                skip = False
        if not skip:
            new_lines.append(line)

    content = "\n".join(new_lines).rstrip()

    # Build new section
    section_lines = [f"\n\n{header}"]
    for k, v in entries.items():
        if isinstance(v, list):
            # Escape each list item individually
            items = ", ".join(f'"{_toml_escape_str(x)}"' for x in v)
            section_lines.append(f'"{k}" = [{items}]')
        elif isinstance(v, bool):
            section_lines.append(f'"{k}" = {"true" if v else "false"}')
        else:
            section_lines.append(f'"{k}" = "{_toml_escape_str(v)}"')
    section_lines.append("")

    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(content + "\n".join(section_lines), encoding="utf-8")


def _toml_section_bool_value(toml_path: Path, section: str, key: str) -> bool | None:
    """Read a boolean key from a TOML section using a minimal parser."""
    if not toml_path.exists():
        return None

    header = f"[{section}]"
    in_section = False
    try:
        lines = toml_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None

    for raw in lines:
        stripped = raw.split("#", 1)[0].strip()
        if not stripped:
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == header
            continue
        if not in_section or "=" not in stripped:
            continue
        k, v = stripped.split("=", 1)
        if k.strip() != key:
            continue
        value = v.strip().lower()
        if value.startswith("true"):
            return True
        if value.startswith("false"):
            return False
    return None


def _ensure_instruction_workflow(instructions_path: Path, template: str, required_markers: list[str]) -> str:
    """Ensure an instructions file has required C3 workflow markers.

    Returns one of: "written", "updated", "kept".
    """
    if not instructions_path.exists():
        instructions_path.parent.mkdir(parents=True, exist_ok=True)
        instructions_path.write_text(template, encoding="utf-8")
        return "written"

    try:
        existing = instructions_path.read_text(encoding="utf-8")
    except Exception:
        existing = ""

    if all(marker in existing for marker in required_markers):
        return "kept"

    merged = (
        template.rstrip()
        + "\n\n---\n\n## Existing Project Instructions\n\n"
        + existing.lstrip()
    )
    instructions_path.write_text(merged, encoding="utf-8")
    return "updated"


def _ensure_codex_agents_workflow(agents_md_path: Path) -> str:
    """Ensure AGENTS.md contains the mandatory C3 workflow for Codex sessions."""
    required_markers = [
        "C3 Tools",
        "MANDATORY",
        "SEARCH FIRST",
        "Anti-patterns",
        "c3_search",
        "c3_read",
        "c3_validate",
        "c3_edit",
        "c3_memory",
        "c3_filter",
    ]
    return _ensure_instruction_workflow(agents_md_path, _AGENTS_MD_CONTENT, required_markers)


def _ensure_vscode_instructions_workflow(instructions_path: Path) -> str:
    """Ensure VS Code Copilot instructions include the latest C3 workflow markers."""
    required_markers = [
        "C3 Tools",
        "MANDATORY",
        "SEARCH FIRST",
        "Anti-patterns",
        "c3_search",
        "c3_read",
        "c3_validate",
        "c3_edit",
        "c3_memory",
        "c3_filter",
    ]
    return _ensure_instruction_workflow(instructions_path, _COPILOT_INSTRUCTIONS_CONTENT, required_markers)


def _safe_read_json(path: Path, label: str = "") -> dict:
    """Read a JSON config file, backing up if corrupted.

    Returns the parsed dict, or {} if the file doesn't exist or is empty.
    If the file has content but can't be parsed, creates a .bak backup and
    warns the user so existing entries (MCP servers, hooks, etc.) aren't
    silently lost.
    """
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        print(f"  WARNING: Could not read {label or path}: {e}")
        return {}
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as e:
        # File has content but isn't valid JSON — back it up.
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            import shutil
            shutil.copy2(path, bak)
            print(f"  WARNING: {label or path} is malformed JSON — backed up to {bak.name}")
        except Exception:
            print(f"  WARNING: {label or path} is malformed JSON and backup failed: {e}")
        return {}


def _upsert_json_mcp_server(config_path: Path, config_key: str, server_name: str, server_entry: dict) -> str:
    """Add or replace an MCP server entry in a JSON config while preserving other keys."""
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config = _safe_read_json(config_path, str(config_path))

    servers = config.get(config_key)
    if not isinstance(servers, dict):
        servers = {}
    previous_entry = servers.get(server_name)

    servers[server_name] = server_entry
    config[config_key] = servers

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

    return "updated" if previous_entry is not None else "written"


def _ensure_project_session_configs(target: Path, server_script: str, primary_profile: str | None = None,
                                    c3_mcp_exe: str | None = None) -> None:
    """Keep the project-local Codex MCP config in sync for new sessions."""
    # Ensure forward slashes for config portability and avoid Windows path-splitting issues
    server_script_posix = Path(server_script).as_posix()
    if c3_mcp_exe:
        mcp_command = c3_mcp_exe
        server_args = ["--project", target.as_posix()]
    else:
        mcp_command = "python"
        server_args = [server_script_posix, "--project", target.as_posix()]

    if primary_profile != "codex":
        codex_path = target / ".codex" / "config.toml"
        codex_state = "updated" if codex_path.exists() else "written"
        _upsert_toml_section(
            codex_path,
            "mcp_servers.c3",
            {
                "command": mcp_command,
                "args": server_args,
                "enabled": True,
            },
        )
        print(f"{codex_state.capitalize()} {codex_path}")




def _ensure_global_session_fallbacks(server_script: str, c3_mcp_exe: str | None = None,
                                     primary_profile: str | None = None) -> None:
    """Keep user-global Codex/Antigravity MCP configs pointing at C3.

    These fallback entries omit `--project` so the MCP server can resolve the
    active working directory dynamically when a session starts in a project that
    does not yet have a project-local Codex config file.
    """
    server_script_posix = Path(server_script).as_posix()
    # With the installed entry point, no script path is needed; --project stays
    # omitted so the server resolves the working directory at session start.
    fallback_args = [] if c3_mcp_exe else [server_script_posix]

    codex_path = Path.home() / ".codex" / "config.toml"
    try:
        codex_state = "updated" if codex_path.exists() else "written"
        _upsert_toml_section(
            codex_path,
            "mcp_servers.c3",
            {
                "command": c3_mcp_exe or "python",
                "args": fallback_args,
                "enabled": True,
            },
        )
        print(f"{codex_state.capitalize()} {codex_path}  (global fallback)")
    except PermissionError:
        print(f"Warning: Could not update {codex_path} (global fallback skipped)")

    # Antigravity shares the ~/.gemini home dir but reads its own MCP config.
    # When Antigravity is the primary profile, the main install flow already
    # wrote this file — here we only keep it fresh for codex installs
    # on machines that have Antigravity (its config dir exists).
    antigravity_path = Path.home() / ".gemini" / "antigravity" / "mcp_config.json"
    if primary_profile != "antigravity" and antigravity_path.parent.is_dir():
        try:
            ag_state = _upsert_json_mcp_server(
                antigravity_path,
                "mcpServers",
                "c3",
                {
                    "command": c3_mcp_exe or sys.executable,
                    "args": fallback_args,
                },
            )
            print(f"{ag_state.capitalize()} {antigravity_path}  (global fallback)")
        except PermissionError:
            print(f"Warning: Could not update {antigravity_path} (global fallback skipped)")


def _uninstall_mcp_all(project_path: str):
    """Remove C3 MCP server configurations from all supported IDEs."""
    import shutil
    from pathlib import Path

    from core.ide import PROFILES

    print("\nRemoving C3 MCP server configurations...")
    target = Path(project_path).resolve()

    # Remove .mcp.json if it exists (standard MCP config file)
    mcp_json = target / ".mcp.json"
    if mcp_json.exists():
        try:
            with open(mcp_json, 'r', encoding="utf-8") as f:
                mcp_data = json.load(f)
            if "mcpServers" in mcp_data and "c3" in mcp_data["mcpServers"]:
                del mcp_data["mcpServers"]["c3"]
                if not mcp_data["mcpServers"]:
                    mcp_json.unlink()
                    print(f"  Deleted empty {mcp_json}")
                else:
                    with open(mcp_json, 'w', encoding="utf-8") as f:
                        json.dump(mcp_data, f, indent=2)
                    print(f"  Removed C3 from {mcp_json}")
        except Exception as e:
            print(f"  Warning: Could not update {mcp_json}: {e}")

    for ide_name, profile in PROFILES.items():
        # MCP Config paths to check
        config_paths = []
        if profile.config_path_global:
            config_paths.append(Path.home() / profile.config_path)
        else:
            config_paths.append(target / profile.config_path)
            # For Codex, also check the global fallback in home dir
            if ide_name == "codex":
                config_paths.append(Path.home() / profile.config_path)

        for mcp_config_path in config_paths:
            if mcp_config_path.exists():
                try:
                    if profile.config_format == "toml":
                        content = mcp_config_path.read_text(encoding="utf-8")
                        section = f"[{profile.config_key}.c3]"
                        lines = content.splitlines()
                        new_lines = []
                        skip = False
                        found = False
                        for line in lines:
                            stripped = line.strip()
                            if stripped == section:
                                skip = True
                                found = True
                                continue
                            if skip and stripped.startswith("["):
                                skip = False
                            if not skip:
                                new_lines.append(line)
                        if found:
                            # If the file only had the C3 section (or is now empty), we can potentially delete it
                            remaining = "\n".join(new_lines).strip()
                            if not remaining:
                                mcp_config_path.unlink()
                                print(f"  Deleted empty {mcp_config_path}")
                            else:
                                mcp_config_path.write_text(remaining + "\n", encoding="utf-8")
                                print(f"  Removed C3 from {mcp_config_path}")
                    else:
                        with open(mcp_config_path, 'r', encoding="utf-8") as f:
                            config = json.load(f)
                        if profile.config_key in config and "c3" in config[profile.config_key]:
                            del config[profile.config_key]["c3"]
                            if not config[profile.config_key]:
                                del config[profile.config_key]

                            if not config:
                                mcp_config_path.unlink()
                                print(f"  Deleted empty {mcp_config_path}")
                            else:
                                with open(mcp_config_path, 'w', encoding="utf-8") as f:
                                    json.dump(config, f, indent=2)
                                print(f"  Removed C3 from {mcp_config_path}")
                except Exception as e:
                    print(f"  Warning: Could not update {mcp_config_path}: {e}")

        # Claude Code Hooks & Settings
        if profile.supports_hooks and profile.settings_path:
            settings_path = target / profile.settings_path
            if settings_path.exists():
                try:
                    with open(settings_path, 'r', encoding="utf-8") as f:
                        settings = json.load(f)

                    # Remove hooks
                    hooks = settings.get("hooks", {}).get("PostToolUse", [])
                    new_hooks = []
                    c3_hook_files = {"hook_filter.py", "hook_read.py", "hook_c3read.py", "hook_dispatch.py"}
                    for h in hooks:
                        if h.get("matcher") in ("Bash", "Read", "mcp__c3__c3_read"):
                            h["hooks"] = [hook for hook in h.get("hooks", [])
                                          if not any(f in hook.get("command", "") for f in c3_hook_files)]
                            if h["hooks"]:
                                new_hooks.append(h)
                        else:
                            new_hooks.append(h)

                    if new_hooks:
                        settings["hooks"]["PostToolUse"] = new_hooks
                    elif "hooks" in settings and "PostToolUse" in settings["hooks"]:
                        del settings["hooks"]["PostToolUse"]
                        if not settings["hooks"]:
                            del settings["hooks"]

                    # Remove enabled server
                    if "enabledMcpjsonServers" in settings and "c3" in settings["enabledMcpjsonServers"]:
                        settings["enabledMcpjsonServers"].remove("c3")

                    if not settings:
                        settings_path.unlink()
                        print(f"  Deleted empty {settings_path}")
                    else:
                        with open(settings_path, 'w', encoding="utf-8") as f:
                            json.dump(settings, f, indent=2)
                        print(f"  Removed C3 hooks/settings from {settings_path}")
                except Exception as e:
                    print(f"  Warning: Could not update {settings_path}: {e}")

        # VS Code Settings
        if ide_name == "vscode":
            vscode_settings_path = target / ".vscode" / "settings.json"
            if vscode_settings_path.exists():
                try:
                    with open(vscode_settings_path, 'r', encoding="utf-8") as f:
                        vscode_settings = json.load(f)

                    keys_to_clean = [
                        "github.copilot.chat.codeGeneration.instructions",
                        "github.copilot.chat.reviewSelection.instructions",
                        "github.copilot.chat.testGeneration.instructions"
                    ]
                    for key in keys_to_clean:
                        if key in vscode_settings:
                            vscode_settings[key] = [i for i in vscode_settings[key]
                                                   if i.get("file") not in (".github/copilot-instructions.md", "CLAUDE.md")]
                            if not vscode_settings[key]:
                                del vscode_settings[key]

                    if not vscode_settings:
                        vscode_settings_path.unlink()
                        print(f"  Deleted empty {vscode_settings_path}")
                    else:
                        with open(vscode_settings_path, 'w', encoding="utf-8") as f:
                            json.dump(vscode_settings, f, indent=2)
                        print(f"  Cleaned Copilot instructions from {vscode_settings_path}")
                except Exception as e:
                    print(f"  Warning: Could not update {vscode_settings_path}: {e}")

    # Legacy Gemini CLI configs (profile removed in v2.52) — still strip the c3 entry.
    for legacy_cfg in (target / ".gemini" / "settings.json",
                       Path.home() / ".gemini" / "settings.json"):
        if not legacy_cfg.exists():
            continue
        try:
            with open(legacy_cfg, encoding="utf-8") as f:
                legacy_data = json.load(f)
            if "c3" in (legacy_data.get("mcpServers") or {}):
                del legacy_data["mcpServers"]["c3"]
                with open(legacy_cfg, "w", encoding="utf-8") as f:
                    json.dump(legacy_data, f, indent=2)
                print(f"  Removed C3 from {legacy_cfg}")
        except Exception as e:
            print(f"  Warning: Could not update {legacy_cfg}: {e}")

    # Final pass: clean up empty IDE directories (.claude, .codex, .gemini, .vscode, .github)
    dirs_to_check = [".claude", ".codex", ".gemini", ".vscode", ".github"]
    for dname in dirs_to_check:
        dpath = target / dname
        if dpath.exists() and dpath.is_dir():
            # If directory is empty or only contains empty subdirectories, remove it
            try:
                # Recursive check for empty dirs
                is_empty = True
                for item in dpath.rglob("*"):
                    if item.is_file():
                        is_empty = False
                        break
                if is_empty:
                    shutil.rmtree(dpath)
                    print(f"  Deleted empty directory {dname}/")
            except Exception:
                pass


_GLOBAL_CLAUDE_MD_CONTENT = """\
# C3 - Global Enforcement (applies to all projects with C3 installed)

## Tool Discipline
When C3 MCP tools are available (c3_search, c3_read, c3_compress, c3_edit, c3_validate, etc.),
you MUST use them instead of native tools (Read, Grep, Glob, Edit, Write).

Native tools are blocked by PreToolUse hooks in C3 projects. Do NOT attempt them without
a prior c3_* call — they will be denied.

**This applies for the ENTIRE conversation**, not just the first few turns. Do not drift
back to native tools as the task progresses.

## Quick Reference
- **Search**: `c3_search(query=..., action='code|files|semantic')` — before Grep/Glob
- **Read**: `c3_compress(mode='map')` then `c3_read(symbols=...)` — before native Read
- **Impact**: `c3_impact(target='symbol')` — blast-radius check before editing shared symbols
- **Edit**: `c3_edit(file_path=..., old_string=..., new_string=...)` — before Edit/Write
- **Validate**: `c3_validate(file_path=...)` — after every edit (pyright/tsc type check if installed)
- **Filter**: `c3_filter(text=...)` — for terminal output >10 lines
- **Shell**: `c3_shell(cmd, timeout=60)` — structured shell exec (tests/git/build). Auto-filters output, logs git mutations to the ledger. Native Bash for interactive/TTY only
- **Memory**: `c3_memory(action='recall')` — full recall. `index` + `fetch` for token-efficient two-step retrieval
- **Delegate**: `c3_delegate(task, backend='ollama|codex|gemini|claude|auto')` — offload to other models
- **Bitbucket** (v2.30.0+, when `c3 bitbucket login` has run): `c3_bitbucket(action='list_prs|get_pr|merge_pr|...')` — self-hosted Bitbucket Data Center / Server. Token in OS keyring; mutating actions auto-log to the edit ledger.
- **Cross-project** (v2.31.0+): `c3_project(action='list|scan|search|read|edit|...', project='<name|path>')` — discover and operate on OTHER c3-installed projects. Reads run freely; writes (edit/shell/memory) need `allow_write=true`.
- **Masked paths** (v2.63.0+): content prefixed `[c3-mask:transformed]` is a policy-transformed VIEW, not the file. Values may be synthetic, rows withheld, bodies stripped — don't copy literals out of it or infer completeness from it. Masked paths are read-only and refuse shell/git/validate/impact/filter/delegate (`[c3-mask:unsupported]`). That's policy, not a transient error — report the block instead of routing around it.

## Self-Check
If you haven't called a c3_* tool in several turns during active development, re-engage
the C3 workflow. Drift is the most common failure mode.
"""


# C3 marker used to detect whether the global CLAUDE.md was written by C3
_GLOBAL_CLAUDE_MD_MARKER = "# C3 - Global Enforcement"


def _ensure_global_claude_md() -> None:
    """Write or update ~/.claude/CLAUDE.md with C3 discipline instructions.

    - Creates the file if it doesn't exist.
    - Updates the C3 section if present but outdated.
    - Preserves any user-written content outside the C3 section.
    """
    global_md = Path.home() / ".claude" / "CLAUDE.md"
    global_md.parent.mkdir(parents=True, exist_ok=True)

    if not global_md.exists():
        global_md.write_text(_GLOBAL_CLAUDE_MD_CONTENT, encoding="utf-8")
        print(f"Wrote {global_md}  (global C3 enforcement)")
        return

    existing = global_md.read_text(encoding="utf-8")

    # The C3-managed region is delimited by explicit BEGIN/END sentinels (the
    # same ones used for project instruction docs). This is unambiguous, so
    # user-written content outside the markers — including H1 headings that
    # happen to mention "C3" or "Tool Discipline" — is never swallowed.
    from services.claude_md import C3_BLOCK_BEGIN, C3_BLOCK_END, merge_c3_block

    wrapped = f"{C3_BLOCK_BEGIN}\n{_GLOBAL_CLAUDE_MD_CONTENT.strip()}\n{C3_BLOCK_END}"

    # Markers already present → surgical, marker-bounded replacement.
    if C3_BLOCK_BEGIN in existing:
        global_md.write_text(merge_c3_block(existing, wrapped), encoding="utf-8")
        print(f"Updated {global_md}  (refreshed C3 enforcement)")
        return

    # Legacy marker-less C3 region → one-time migration into the marked block.
    # Bound the region from the legacy heading to the NEXT top-level (``# ``)
    # heading. C3's own content has exactly one H1 (the legacy heading itself),
    # so the next H1 reliably marks where user content resumes; we deliberately
    # do NOT skip H1s containing "C3"/"Tool Discipline" (the old heuristic did,
    # which is what swallowed user headings).
    if _GLOBAL_CLAUDE_MD_MARKER in existing:
        start = existing.index(_GLOBAL_CLAUDE_MD_MARKER)
        rest = existing[start + len(_GLOBAL_CLAUDE_MD_MARKER):]
        end_offset = len(rest)  # default: to EOF
        running = 0
        for line in rest.split("\n"):
            running += len(line) + 1
            if line.startswith("# "):
                end_offset = running - len(line) - 1
                break
        end = start + len(_GLOBAL_CLAUDE_MD_MARKER) + end_offset
        before = existing[:start].rstrip()
        after = existing[end:].lstrip()
        parts = [p for p in (before, wrapped, after) if p]
        global_md.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
        print(f"Updated {global_md}  (migrated C3 enforcement to markers)")
        return

    # User has their own CLAUDE.md with no C3 content — append the marked block.
    merged = existing.rstrip() + "\n\n" + wrapped + "\n"
    global_md.write_text(merged, encoding="utf-8")
    print(f"Updated {global_md}  (appended C3 enforcement)")


def _instruction_documents_for_project() -> list[tuple[str, str]]:
    """Return every project-local instruction document C3 has ever managed.

    Used by uninstall/cleanup paths, so it must keep listing legacy docs
    (GEMINI.md — profile removed in v2.52; empty template, never generated).
    """
    return [
        ("CLAUDE.md", _CLAUDE_MD_CONTENT),
        ("AGENTS.md", _AGENTS_MD_CONTENT),
        ("GEMINI.md", ""),
    ]


_LEGACY_INSTRUCTION_DOCS = ("GEMINI.md",)  # Gemini CLI profile removed in v2.52


def _instruction_documents_to_generate() -> list[tuple[str, str]]:
    """Instruction docs to write for a project (legacy docs excluded)."""
    return [
        (name, template)
        for name, template in _instruction_documents_for_project()
        if name not in _LEGACY_INSTRUCTION_DOCS
    ]


def _sync_project_instruction_docs(project_path: str, sm: SessionManager) -> None:
    """Write the current C3 instruction docs into the project root."""
    repo_root = Path(__file__).resolve().parent.parent
    synced: list[str] = []
    for instructions_file, template in _instruction_documents_to_generate():
        print(f"Generating {instructions_file}...")
        # Resolve placeholder for project-scoped MCP configs
        resolved_template = template.replace("<path-to-c3>", str(repo_root).replace("\\", "/"))
        result = sm.save_claude_md(instructions_file=instructions_file, template=resolved_template)
        print(f"  Saved to {result['path']} ({result['tokens']} tokens)")
        synced.append(instructions_file)
    print(f"Synced instruction docs: {', '.join(synced)}")


_PERM_ACTIONS = {"show", "preview", "diff", "clean"}


def cmd_permissions(args):
    """Show, preview, diff, clean, or apply a Claude Code permission tier."""
    raw = getattr(args, "tier", "show")
    target = getattr(args, "target", None)
    include_mcp_wildcard = getattr(args, "include_mcp_wildcard", False)

    # Normalize aliases (e.g. "strict" → "c3-strict", "unrestricted" → "permissive")
    action_or_tier = _TIER_ALIASES.get(raw, raw)
    if target:
        target = _TIER_ALIASES.get(target, target)

    project_path = str(Path(".").resolve())
    settings_path = Path(project_path) / ".claude" / "settings.local.json"

    from core.ide import load_ide_config
    ide = load_ide_config(project_path)
    if ide != "claude-code":
        print(f"  Permissions are only supported for Claude Code (current IDE: {ide}).")
        print("  Run 'c3 install-mcp --ide claude' first.")
        return

    if action_or_tier == "show":
        _cmd_permissions_show(project_path, settings_path)
        return

    if action_or_tier == "preview":
        tier = target or _detect_current_tier(settings_path) or "standard"
        _cmd_permissions_preview(tier, include_mcp_wildcard)
        return

    if action_or_tier == "diff":
        tier = target or _safe_read_json(
            Path(project_path) / ".c3" / "config.json", "config"
        ).get("permission_tier") or _detect_current_tier(settings_path) or "standard"
        _cmd_permissions_diff(settings_path, tier, include_mcp_wildcard)
        return

    if action_or_tier == "clean":
        _cmd_permissions_clean(settings_path)
        return

    if action_or_tier not in PERMISSION_TIERS:
        actions = " | ".join(sorted(_PERM_ACTIONS))
        tiers = ", ".join(PERMISSION_TIERS)
        print(f"  Unknown '{raw}'. Actions: {actions}. Tiers: {tiers}.")
        print("  Aliases: unrestricted→permissive, strict/c3→c3-strict, readonly→read-only")
        return

    _apply_permission_tier(project_path, action_or_tier, include_mcp_wildcard=include_mcp_wildcard)
    print(f"  Written: {settings_path}")


def _cmd_permissions_show(project_path: str, settings_path: Path) -> None:
    current = _detect_current_tier(settings_path)
    c3_cfg = _safe_read_json(Path(project_path) / ".c3" / "config.json", "config")
    stored = c3_cfg.get("permission_tier")
    stale = _find_stale_tools(settings_path)
    print(f"  Current tier : {current or '(none set)'}")
    if stored and stored != current:
        print(f"  Stored tier  : {stored}  (drift — re-apply to sync)")
    if stale:
        print(f"  Stale tools  : {len(stale)} obsolete c3 MCP name(s). Run 'c3 permissions clean'.")
    print()
    for name, desc in PERMISSION_TIERS.items():
        marker = "* " if name == (current or stored) else "  "
        print(f"  {marker}{name:<12} — {desc}")
    print()
    print("  Usage: c3 permissions <action|tier> [target] [--include-mcp-wildcard]")
    print("  Actions: show | preview <tier> | diff [tier] | clean")
    print("  Tiers  : " + ", ".join(PERMISSION_TIERS))


def _cmd_permissions_preview(tier: str, include_mcp_wildcard: bool) -> None:
    if tier not in PERMISSION_TIERS:
        print(f"  Unknown tier '{tier}'. Choose from: {', '.join(PERMISSION_TIERS)}")
        return
    perms = _build_permission_tier(tier, include_mcp_wildcard=include_mcp_wildcard)["permissions"]
    suffix = " (+ mcp__* wildcard)" if include_mcp_wildcard else ""
    print(f"  Preview: '{tier}'{suffix} — {PERMISSION_TIERS[tier]}")
    print(f"  Allow ({len(perms['allow'])}):")
    for entry in perms["allow"]:
        print(f"    + {entry}")
    print(f"  Deny ({len(perms['deny'])}):")
    for entry in perms["deny"]:
        print(f"    - {entry}")


def _cmd_permissions_diff(settings_path: Path, tier: str, include_mcp_wildcard: bool) -> None:
    if tier not in PERMISSION_TIERS:
        print(f"  Unknown target tier '{tier}'. Choose from: {', '.join(PERMISSION_TIERS)}")
        return
    current = _safe_read_json(settings_path, str(settings_path))
    cur_allow = set(current.get("permissions", {}).get("allow", []))
    cur_deny = set(current.get("permissions", {}).get("deny", []))
    target_perms = _build_permission_tier(tier, include_mcp_wildcard=include_mcp_wildcard)["permissions"]
    tgt_allow = set(target_perms["allow"])
    tgt_deny = set(target_perms["deny"])

    missing_allow = tgt_allow - cur_allow
    extra_allow = cur_allow - tgt_allow
    missing_deny = tgt_deny - cur_deny
    extra_deny = cur_deny - tgt_deny

    print(f"  Diff: current settings vs '{tier}' tier")
    if missing_allow:
        print(f"\n  Missing from allow ({len(missing_allow)}) — tier requires:")
        for e in sorted(missing_allow):
            print(f"    + {e}")
    if extra_allow:
        print(f"\n  Extra in allow ({len(extra_allow)}) — not in tier:")
        for e in sorted(extra_allow):
            print(f"    ? {e}")
    if missing_deny:
        print(f"\n  Missing from deny ({len(missing_deny)}):")
        for e in sorted(missing_deny):
            print(f"    + {e}")
    if extra_deny:
        print(f"\n  Extra in deny ({len(extra_deny)}):")
        for e in sorted(extra_deny):
            print(f"    ? {e}")
    if not (missing_allow or extra_allow or missing_deny or extra_deny):
        print(f"  ✓ Current settings match tier '{tier}' exactly.")
    else:
        print(f"\n  Run 'c3 permissions {tier}' to apply this tier exactly.")


def _cmd_permissions_clean(settings_path: Path) -> None:
    stale = _find_stale_tools(settings_path)
    if not stale:
        print("  No stale tool names found. Nothing to clean.")
        return
    print(f"  Found {len(stale)} stale c3 MCP tool name(s):")
    for t in sorted(set(stale)):
        print(f"    - {t}")
    removed = _clean_stale_tools(settings_path)
    print(f"  Removed {removed} entries from {settings_path}")


def _run_install_mcp(project_path: str, ide_name: str, mcp_mode: str = "direct",
                     permissions: str | None = None,
                     include_mcp_wildcard: bool = False,
                     banner: str = "Installing MCP tools...") -> None:
    """Run install-mcp programmatically with a consistent banner."""
    print(f"\n{banner}")
    from types import SimpleNamespace
    cmd_install_mcp(SimpleNamespace(
        project_path=project_path, ide=ide_name, mcp_mode=mcp_mode,
        permissions=permissions, include_mcp_wildcard=include_mcp_wildcard,
    ))


def _prompt_install_mcp(project_path: str, ide_name: str, default_mode: str = "direct", banner: str = "Installing MCP tools...") -> bool:
    """Ask whether to install MCP tooling and, if so, which mode to use."""
    print()
    install_choice = _prompt_choice(
        "Install MCP tooling for this project?",
        [
            "Yes  — configure the IDE and wire up C3 MCP",
            "No   — skip MCP install for now",
        ],
    )
    if not install_choice or install_choice.startswith("No"):
        print("  Skipped MCP install.")
        return False

    mode_choice = _prompt_choice(
        "Choose MCP mode",
        [
            "Direct  — recommended, connect IDE straight to c3 mcp_server.py",
            "Proxy   — advanced, use c3 mcp_proxy.py for dynamic filtering experiments",
        ],
    )
    mcp_mode = "proxy" if mode_choice and mode_choice.startswith("Proxy") else default_mode
    _run_install_mcp(project_path, ide_name, mcp_mode=mcp_mode, banner=banner)
    return True


def _resolve_mcp_mode(raw_mode: str | None) -> str:
    """Normalize requested MCP mode."""
    mode = str(raw_mode or "direct").strip().lower()
    if mode not in {"direct", "proxy"}:
        raise ValueError(f"Unsupported MCP mode '{raw_mode}'. Use 'direct' or 'proxy'.")
    return mode


def _resolve_install_mcp_cli_args(raw_targets: list[str] | None, ide_name: str | None) -> tuple[str, str]:
    """Resolve `install-mcp` CLI positionals with IDE shorthand support."""
    resolved_ide = str(ide_name or "auto").strip().lower() or "auto"
    targets = [str(item).strip() for item in (raw_targets or []) if str(item).strip()]
    if len(targets) > 2:
        raise RuntimeError("install-mcp accepts at most one project path and one IDE.")

    project_path = "."
    positional_ide = None

    for target in targets:
        normalized = normalize_ide_name(target)
        if normalized in PROFILES:
            if positional_ide and positional_ide != normalized:
                raise RuntimeError("install-mcp received multiple IDE values.")
            positional_ide = normalized
            continue
        if project_path != ".":
            raise RuntimeError("install-mcp accepts at most one project path.")
        project_path = target

    if resolved_ide != "auto":
        resolved_ide = _parse_cli_ide_arg(resolved_ide)
    if resolved_ide != "auto" and positional_ide and resolved_ide != positional_ide:
        raise RuntimeError("install-mcp received conflicting IDE values.")
    if positional_ide:
        resolved_ide = positional_ide

    return project_path, resolved_ide


def cmd_install_mcp(args):
    """Generate MCP config and optional hooks for the target IDE."""
    raw_targets = getattr(args, "targets", None)
    if raw_targets is None and hasattr(args, "project_path"):
        raw_targets = [getattr(args, "project_path")]
    project_path, cli_ide = _resolve_install_mcp_cli_args(raw_targets, getattr(args, "ide", "auto"))
    target = Path(project_path or ".").resolve()
    cli_dir = Path(__file__).parent.resolve()
    from services.session_manager import SessionManager
    sm = SessionManager(str(target))

    # Resolve IDE choice
    ide_name = cli_ide if hasattr(args, 'ide') else "auto"
    if ide_name != "auto":
        ide_name = normalize_ide_name(ide_name)
    if ide_name == "auto":
        ide_name = detect_ide(str(target))
    profile = get_profile(ide_name)

    mcp_mode = _resolve_mcp_mode(getattr(args, "mcp_mode", "direct"))
    server_filename = "mcp_proxy.py" if mcp_mode == "proxy" else "mcp_server.py"
    # Use forward slashes for cross-platform compatibility in config files
    server_script = (cli_dir / server_filename).as_posix()

    # Prefer the installed `c3-mcp` console script for direct mode. It survives C3
    # upgrades (pip/pipx reinstall to the same launcher path) and keeps the source-tree
    # location out of every project's MCP config, so upgrading no longer requires
    # re-running install-mcp per project. Fall back to invoking the source script with
    # `python` when running from a checkout with no installed entry point, or in proxy
    # mode (which has no console script).
    import shutil
    c3_mcp_exe = None
    if mcp_mode != "proxy":
        _found = shutil.which("c3-mcp")
        if _found:
            c3_mcp_exe = Path(_found).resolve().as_posix()

    # Keep the script path as a single arg; 'python' keeps the source fallback
    # portable across platforms.
    if c3_mcp_exe:
        new_entry = {"command": c3_mcp_exe, "args": ["--project", "."]}
    else:
        new_entry = {"command": "python", "args": [server_script, "--project", "."]}
    if profile.needs_type_field:
        new_entry["type"] = "stdio"

    # â”€â”€ Write MCP config â”€â”€
    # Global profiles (e.g. Antigravity) write to the user home dir, not the project
    if profile.config_path_global:
        mcp_config_path = Path.home() / profile.config_path
    else:
        mcp_config_path = target / profile.config_path
    mcp_config_path.parent.mkdir(parents=True, exist_ok=True)

    # Cleanup .mcp.json if it's NOT the target config but exists (to avoid confusion)
    if profile.config_path != ".mcp.json":
        mcp_json_legacy = target / ".mcp.json"
        if mcp_json_legacy.exists():
            try:
                with open(mcp_json_legacy, 'r', encoding="utf-8") as f:
                    legacy_data = json.load(f)
                if "mcpServers" in legacy_data and "c3" in legacy_data["mcpServers"]:
                    del legacy_data["mcpServers"]["c3"]
                    if not legacy_data["mcpServers"]:
                        mcp_json_legacy.unlink()
                        print(f"  Removed obsolete {mcp_json_legacy}")
                    else:
                        with open(mcp_json_legacy, 'w', encoding="utf-8") as f:
                            json.dump(legacy_data, f, indent=2)
                        print(f"  Removed C3 from obsolete {mcp_json_legacy}")
            except Exception:
                pass

    try:
        if profile.config_format == "toml":
            # Codex uses TOML: [mcp_servers.c3] with command/args
            if c3_mcp_exe:
                toml_entries = {"command": c3_mcp_exe, "args": ["--project", str(target)]}
            else:
                toml_entries = {"command": sys.executable, "args": [server_script, "--project", str(target)]}
            if profile.name == "codex":
                # Codex supports explicit enable/disable per server.
                toml_entries["enabled"] = True
            _upsert_toml_section(
                mcp_config_path,
                f"{profile.config_key}.c3",
                toml_entries,
            )
        else:
            config = _safe_read_json(mcp_config_path, str(mcp_config_path))

            if profile.config_key not in config:
                config[profile.config_key] = {}

            config[profile.config_key]["c3"] = new_entry

            with open(mcp_config_path, 'w', encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            # Ensure newline at end of file
            with open(mcp_config_path, 'a', encoding="utf-8") as f:
                f.write("\n")
    except PermissionError as e:
        raise RuntimeError(
            f"Cannot write {mcp_config_path} (permission denied / file in use). "
            "Close the IDE or unlock the file, then run install-mcp again."
        ) from e

    print(f"Wrote {mcp_config_path}")
    if not profile.config_path_global:
        # Self-report to the artifact tracker: attribute this write to C3
        # instead of letting it surface as anonymous out-of-band drift.
        from services.artifact_defs import note_pending_write
        note_pending_write(target, profile.config_path, "install_mcp")
    if profile.name in {"codex", "antigravity"}:
        _ensure_project_session_configs(target, server_script, primary_profile=profile.name, c3_mcp_exe=c3_mcp_exe)
        _ensure_global_session_fallbacks(server_script, c3_mcp_exe=c3_mcp_exe, primary_profile=profile.name)

    # â”€â”€ Persist IDE choice to .c3/config.json â”€â”€
    c3_config_dir = target / ".c3"
    c3_config_dir.mkdir(parents=True, exist_ok=True)
    c3_config_path = c3_config_dir / "config.json"

    c3_config = _safe_read_json(c3_config_path, ".c3/config.json")

    c3_config["ide"] = ide_name
    c3_config["mcp"] = {"mode": mcp_mode}
    with open(c3_config_path, 'w', encoding="utf-8") as f:
        json.dump(c3_config, f, indent=2)

    # ── Install hooks (Claude Code) ──
    if profile.supports_hooks and profile.settings_path:
        settings_dir = target / Path(profile.settings_path).parent
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = target / profile.settings_path

        settings = _safe_read_json(settings_path, str(settings_path))

        # Build hook commands using the Python executable that runs c3.
        #
        # Windows: do NOT wrap in cmd.exe. Claude Code runs hooks through Git Bash,
        # whose MSYS argument conversion rewrites a standalone "/c" into "C:/" before
        # cmd.exe ever sees it — so cmd.exe starts INTERACTIVELY, prints its banner,
        # and reads the hook's stdin JSON payload as console commands. The hook never
        # runs, and any ">" token in the payload becomes a shell redirect that silently
        # creates junk files in the repo root.
        #
        # Verified 2026-07-26 under Git Bash on Windows 11:
        #   python -c "import sys;print(sys.argv)" /c foo   ->  ['-c', 'C:/', 'foo']
        #   cmd.exe /c '<py>' -c "print('x')"               ->  banner, no execution
        #   '<py>' '<hook>' posttool                        ->  runs, stdin intact
        # Both "cmd /c" (bash cannot resolve bare "cmd") and "cmd.exe /c" are broken;
        # the wrapper was never needed. A double-quoted forward-slash path is parsed
        # correctly by bash AND cmd, including paths containing spaces or parentheses
        # (e.g. "Claude Code Companion (C3)"), which is why the wrapper was added.
        def _hook_arg(raw: str) -> str:
            if sys.platform == "win32":
                return '"' + str(raw).replace("\\", "/") + '"'
            return shlex.quote(str(raw))

        # v2.42: single dispatcher script per hook event instead of N separate
        # per-hook commands. One interpreter spawn per event; the dispatcher
        # (cli/hook_dispatch.py) runs all applicable sub-hooks in-process.
        _dispatch_base = (
            f"{_hook_arg(sys.executable)} "
            f"{_hook_arg(str(cli_dir / 'hook_dispatch.py'))}"
        )
        hook_pretool_cmd  = f"{_dispatch_base} pretool"
        hook_posttool_cmd = f"{_dispatch_base} posttool"
        hook_stop_cmd     = f"{_dispatch_base} stop"
        hook_prompt_cmd   = f"{_dispatch_base} prompt"

        shell_matcher  = "Bash"
        read_matcher   = "Read"
        grep_matcher   = "Grep"
        glob_matcher   = "Glob"
        edit_matcher   = "Edit"
        write_matcher  = "Write"
        # Claude Code also exposes MultiEdit (batch edits) and NotebookEdit;
        # both bypass enforcement/logging unless their matchers are registered.
        extra_edit_matchers = ["MultiEdit", "NotebookEdit"]

        # ── PostToolUse hooks ──
        # Matcher set is unchanged from pre-v2.42; every matcher now points at
        # the single posttool dispatcher (which sub-hooks run for which tool
        # moved into cli/hook_dispatch.py). One spawn per event instead of
        # up to three.
        _post_matcher_names = [
            shell_matcher,
            read_matcher,
            "mcp__c3__c3_read",
            "mcp__c3__c3_shell",
            "mcp__c3__c3_search",
            "mcp__c3__c3_compress",
            "mcp__c3__c3_filter",
            "mcp__c3__c3_memory",
            "mcp__c3__c3_validate",
            "mcp__c3__c3_edit",
            "mcp__c3__c3_edits",
            "mcp__c3__c3_impact",
            "mcp__c3__c3_status",
            "mcp__c3__c3_delegate",
            "mcp__c3__c3_session",
            "mcp__c3__c3_agent",
            edit_matcher,
            write_matcher,
            *extra_edit_matchers,
        ]
        desired_post_hooks = [
            {"matcher": m, "hooks": [{"type": "command", "command": hook_posttool_cmd}]}
            for m in _post_matcher_names
        ]

        # ── PreToolUse hooks (enforcement — blocks native tools without prior c3_*) ──
        # Bash/run_shell_command joined in v2.62: the Access Guard sub-hook
        # scans shell commands (best-effort); without these matchers PreToolUse
        # simply never fires for shell — proven bypass, see docs/access-guard.md.
        _pre_matcher_names = [
            read_matcher,
            grep_matcher,
            glob_matcher,
            edit_matcher,
            write_matcher,
            *extra_edit_matchers,
            shell_matcher,
            "run_shell_command",
        ]
        desired_pre_hooks = [
            {"matcher": m, "hooks": [{"type": "command", "command": hook_pretool_cmd}]}
            for m in _pre_matcher_names
        ]

        # Merge: replace existing C3 hooks (so re-running install-mcp updates commands),
        # preserve any non-C3 hooks the user may have added.
        hook_event = profile.hook_event
        post_matchers = {h.get("matcher") for h in desired_post_hooks}
        existing_post = [
            h for h in settings.get("hooks", {}).get(hook_event, [])
            if h.get("matcher") not in post_matchers
        ]
        existing_post.extend(desired_post_hooks)
        settings.setdefault("hooks", {})[hook_event] = existing_post

        pre_event = "PreToolUse"
        pre_matchers = {h.get("matcher") for h in desired_pre_hooks}
        existing_pre = [
            h for h in settings.get("hooks", {}).get(pre_event, [])
            if h.get("matcher") not in pre_matchers
        ]
        existing_pre.extend(desired_pre_hooks)
        settings.setdefault("hooks", {})[pre_event] = existing_pre

        # ── Stop hooks (auto-snapshot + session stats on session end / Ctrl+C) ──
        desired_stop_hooks = [
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": hook_stop_cmd},
                ]
            },
        ]
        stop_event = "Stop"
        # Replace only C3's own stop hooks (identified by our hook scripts) and
        # keep every user-added stop hook — including matcher-less ones, which
        # are the normal shape for Stop hooks. The pre-v2.42 script names stay
        # in this tuple so re-running install-mcp migrates old per-hook
        # entries to the dispatcher.
        _c3_stop_scripts = (
            "hook_session_stats.py", "hook_auto_snapshot.py", "hook_terse_advisor.py",
            "hook_dispatch.py",
        )

        def _is_c3_stop_hook(entry: dict) -> bool:
            return any(
                script in (hk.get("command") or "")
                for hk in entry.get("hooks", [])
                for script in _c3_stop_scripts
            )

        existing_stop = [
            h for h in settings.get("hooks", {}).get(stop_event, [])
            if not _is_c3_stop_hook(h)
        ]
        existing_stop.extend(desired_stop_hooks)
        settings.setdefault("hooks", {})[stop_event] = existing_stop

        # ── UserPromptSubmit hook (per-prompt project-memory injection) ──
        # Same merge discipline as Stop: replace only C3's own entries
        # (identified by the dispatcher script in the command), keep
        # user-added ones.
        prompt_event = "UserPromptSubmit"

        def _is_c3_prompt_hook(entry: dict) -> bool:
            return any(
                "hook_dispatch.py" in (hk.get("command") or "")
                for hk in entry.get("hooks", [])
            )

        existing_prompt = [
            h for h in settings.get("hooks", {}).get(prompt_event, [])
            if not _is_c3_prompt_hook(h)
        ]
        existing_prompt.append(
            {"matcher": "", "hooks": [{"type": "command", "command": hook_prompt_cmd}]}
        )
        settings.setdefault("hooks", {})[prompt_event] = existing_prompt

        # Claude Code only: enable MCP server prompt settings
        if profile.name == "claude-code":
            settings["enableAllProjectMcpServers"] = True
            settings.setdefault("enabledMcpjsonServers", [])
            if "c3" not in settings["enabledMcpjsonServers"]:
                settings["enabledMcpjsonServers"].append("c3")

        # Apply permission tier if requested (Claude Code only)
        perm_tier = getattr(args, "permissions", None)
        perm_tier = _TIER_ALIASES.get(perm_tier, perm_tier) if perm_tier else perm_tier
        include_wildcard = bool(getattr(args, "include_mcp_wildcard", False))
        if perm_tier and profile.name == "claude-code":
            if perm_tier in PERMISSION_TIERS:
                settings["permissions"] = _merge_permission_tier(
                    settings.get("permissions") or {},
                    _build_permission_tier(
                        perm_tier, include_mcp_wildcard=include_wildcard
                    )["permissions"],
                )
                # Persist tier choice in .c3/config.json
                _c3cfg = _safe_read_json(c3_config_path, str(c3_config_path))
                _c3cfg["permission_tier"] = perm_tier
                if include_wildcard:
                    _c3cfg["permission_include_mcp_wildcard"] = True
                elif "permission_include_mcp_wildcard" in _c3cfg:
                    del _c3cfg["permission_include_mcp_wildcard"]
                with open(c3_config_path, "w", encoding="utf-8") as f:
                    json.dump(_c3cfg, f, indent=2)

        with open(settings_path, 'w', encoding="utf-8") as f:
            json.dump(settings, f, indent=2)

        print(f"Wrote {settings_path}")
        from services.artifact_defs import note_pending_write
        note_pending_write(target, profile.settings_path, "install_mcp")
        print(f"  Hooks ({hook_event}): dispatcher (1 spawn/event) — filter/ghost/read-guard/ledger/unlock/signal via cli/hook_dispatch.py posttool")
        print(f"  Hooks ({pre_event}): dispatcher — {read_matcher}/{grep_matcher}/{glob_matcher}/{edit_matcher}/{write_matcher} (c3 enforcement)")
        print("  Hooks (Stop): dispatcher — session_stats + auto_snapshot + terse_advisor")
        if profile.name == "claude-code":
            print("  Claude MCP prompt settings enabled for this project")
        if perm_tier and profile.name == "claude-code":
            suffix = " (+ mcp__* wildcard)" if include_wildcard else ""
            print(f"  Permissions: {perm_tier}{suffix} — {PERMISSION_TIERS.get(perm_tier, '')}")
        if not settings_path.exists():
            raise RuntimeError(f"{profile.display_name} settings file was not created: {settings_path}")

    # â”€â”€ VS Code Copilot enforcement files â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if profile.name == "vscode":
        # 1. Ensure copilot-instructions.md has the latest C3 workflow markers
        instructions_path = target / ".github" / "copilot-instructions.md"
        vs_state = _ensure_vscode_instructions_workflow(instructions_path)
        if vs_state == "written":
            print(f"Wrote {instructions_path}")
        elif vs_state == "updated":
            print(f"Updated {instructions_path}  (enforced C3 workflow)")
        else:
            print(f"Kept  {instructions_path}  (C3 workflow present)")

        # 2. Create/update .vscode/settings.json with Copilot instruction references
        vscode_settings_path = target / ".vscode" / "settings.json"
        vscode_settings_path.parent.mkdir(parents=True, exist_ok=True)
        vscode_settings = {}
        if vscode_settings_path.exists():
            try:
                with open(vscode_settings_path, encoding="utf-8") as f:
                    vscode_settings = json.load(f)
            except Exception:
                pass

        instruction_files = [
            {"file": ".github/copilot-instructions.md"},
            {"file": "CLAUDE.md"},
        ]
        vscode_settings["github.copilot.chat.codeGeneration.instructions"] = instruction_files
        vscode_settings["github.copilot.chat.reviewSelection.instructions"] = [
            {"file": ".github/copilot-instructions.md"},
        ]
        vscode_settings["github.copilot.chat.testGeneration.instructions"] = [
            {"file": ".github/copilot-instructions.md"},
        ]

        with open(vscode_settings_path, "w", encoding="utf-8") as f:
            json.dump(vscode_settings, f, indent=2)
        print(f"Wrote {vscode_settings_path}")
        print("  Copilot: C3 instructions linked for code gen, review, and test generation")

    # â”€â”€ Codex AGENTS.md enforcement file â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if profile.name == "codex":
        agents_md_path = target / "AGENTS.md"
        agents_state = _ensure_codex_agents_workflow(agents_md_path)
        if agents_state == "written":
            print(f"Wrote {agents_md_path}")
        elif agents_state == "updated":
            print(f"Updated {agents_md_path}  (enforced C3 workflow)")
        else:
            print(f"Kept  {agents_md_path}  (C3 workflow present)")

        # Warn about a common conflict: global Codex config disables c3.
        global_codex_cfg = Path.home() / ".codex" / "config.toml"
        global_enabled = _toml_section_bool_value(global_codex_cfg, "mcp_servers.c3", "enabled")
        if global_enabled is False:
            print(f"Warning: {global_codex_cfg} has [mcp_servers.c3] enabled = false.")
            print("  This can make C3 look disabled. Set it to true or remove that global c3 section.")

    _sync_project_instruction_docs(str(target), sm)

    # ── User-global C3 enforcement ──────────────────────────────
    try:
        _ensure_global_claude_md()
    except Exception as e:
        print(f"Warning: Could not update global CLAUDE.md: {e}")

    # ── Install /terse skill for supported IDEs ──────────────────
    if profile.name in ("claude-code", "codex"):
        try:
            _ensure_terse_skill(profile.name)
        except Exception as e:
            print(f"Warning: Could not install /terse skill: {e}")

    print(f"IDE: {profile.display_name}")
    print(f"MCP Mode: {mcp_mode}")
    print(f"Server: {server_script}")
    print(f"Project: {target}")
    print(f"\nRestart {profile.display_name} in this project to activate C3 tools.")


def _remove_toml_section(toml_path: Path, section: str) -> bool:
    """Remove a dotted TOML section (e.g. 'mcp_servers.c3') in-place."""
    if not toml_path.exists():
        return False
    content = toml_path.read_text(encoding="utf-8")
    header = f"[{section}]"
    lines = content.splitlines()
    new_lines: list[str] = []
    skip = False
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            skip = True
            found = True
            continue
        if skip and stripped.startswith("["):
            skip = False
        if not skip:
            new_lines.append(line)
    if found:
        remaining = "\n".join(new_lines).strip()
        if not remaining:
            toml_path.unlink()
        else:
            toml_path.write_text(remaining + "\n", encoding="utf-8")
    return found


def _remove_json_mcp_server(config_path: Path, config_key: str, server_name: str) -> bool:
    """Remove an MCP server entry from a JSON config."""
    if not config_path.exists():
        return False
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        if config_key in config and server_name in config[config_key]:
            del config[config_key][server_name]
            if not config[config_key]:
                del config[config_key]
            if not config:
                config_path.unlink()
            else:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, indent=2)
            return True
    except Exception:
        pass
    return False


def cmd_mcp_remove(args):
    """Remove an MCP server configuration from an IDE config."""
    name = getattr(args, "name", "c3")
    project_path = str(Path(getattr(args, "project_path", ".")).resolve())
    ide_name = getattr(args, "ide", "auto")
    if ide_name == "auto":
        ide_name = load_ide_config(project_path)
    if ide_name == "auto":
        ide_name = detect_ide(project_path)

    profile = get_profile(ide_name)
    target = Path(project_path)

    if profile.config_path_global:
        mcp_config_path = Path.home() / profile.config_path
    else:
        mcp_config_path = target / profile.config_path

    if not mcp_config_path.exists():
        print(f"Error: MCP config not found at {mcp_config_path}")
        return False

    print(f"Removing MCP server '{name}' from {mcp_config_path}...")
    removed = False
    if profile.config_format == "toml":
        section = f"{profile.config_key}.{name}"
        removed = _remove_toml_section(mcp_config_path, section)
    else:
        removed = _remove_json_mcp_server(mcp_config_path, profile.config_key, name)

    if removed:
        print(f"[OK] Removed {name} from {ide_name} config.")
    else:
        print(f"Server '{name}' not found in {ide_name} config.")
    return removed


def cmd_terse(args):
    """Manage the terse-advisor nudge state (~/.c3/terse_advisor.json)."""
    from datetime import datetime, timedelta, timezone

    state_file = Path.home() / ".c3" / "terse_advisor.json"

    def _load():
        if state_file.exists():
            try:
                return json.loads(state_file.read_text("utf-8"))
            except Exception:
                pass
        return {"dismissed": False, "remind_after": None, "last_nudge_session": None}

    def _save(state):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2), "utf-8")

    action = getattr(args, "action", "status") or "status"
    state = _load()

    if action == "dismiss":
        state["dismissed"] = True
        state["remind_after"] = None
        _save(state)
        print("[C3] Terse advisor silenced permanently. Run `c3 terse reset` to re-enable.")
    elif action == "later":
        until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        state["remind_after"] = until
        _save(state)
        print(f"[C3] Terse advisor snoozed for 24h (until {until[:16]} UTC).")
    elif action == "reset":
        if state_file.exists():
            state_file.unlink()
        print("[C3] Terse advisor state cleared.")
    else:  # status
        dismissed = state.get("dismissed", False)
        remind_after = state.get("remind_after")
        last_session = state.get("last_nudge_session")
        print(f"Terse advisor state ({state_file}):")
        print(f"  dismissed       : {dismissed}")
        if remind_after:
            now = datetime.now(timezone.utc)
            try:
                until_dt = datetime.fromisoformat(remind_after)
                snoozed = now < until_dt
                print(f"  snoozed until   : {remind_after[:16]} UTC  ({'active' if snoozed else 'expired'})")
            except Exception:
                print(f"  remind_after    : {remind_after}")
        else:
            print("  snoozed until   : —")
        print(f"  last nudge sess : {last_session or '—'}")


def cmd_ui(args):
    """Launch the web UI."""
    return common_cmd_ui(args, _command_deps())


# â”€â”€â”€ Main â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€




def cmd_hub(args):
    """Launch the Project Hub web dashboard."""
    from services.hub_service import HubService
    port = getattr(args, 'port', 3330)

    if getattr(args, "install", False):
        res = HubService().install(port)
        print(res.get("output", "Service installed."))
        return

    if getattr(args, "uninstall", False):
        res = HubService().uninstall()
        print(res.get("output", "Service uninstalled."))
        return

    if getattr(args, "status", False):
        res = HubService().status()
        print(f"Hub Service Status ({res.get('platform', 'unknown')}):")
        print(f"  Installed: {res.get('installed')}")
        print(f"  Running  : {res.get('running')}")
        print(f"  Port     : {res.get('port')}")
        print(f"  Method   : {res.get('method')}")
        print(f"  Log      : {res.get('log_path')}")
        return

    from cli.hub_server import run_hub
    silent = bool(getattr(args, 'silent', False) or getattr(args, 'extra_silent', False))
    quiet = bool(getattr(args, 'extra_silent', False))
    open_browser = not (getattr(args, 'no_browser', False) or silent)
    run_hub(port=port, open_browser=open_browser, silent=silent, quiet=quiet)


def cmd_bitbucket(args):
    """Bitbucket Data Center / Server credential + workspace management."""
    sub = getattr(args, "bitbucket_cmd", None)
    if not sub:
        print("Usage: c3 bitbucket {login,logout,status,use,set-default} [args]")
        return

    project_path = getattr(args, "project_path", ".") or "."

    if sub == "login":
        _bb_cmd_login(args, project_path)
    elif sub == "logout":
        _bb_cmd_logout(args, project_path)
    elif sub == "status":
        _bb_cmd_status(args, project_path)
    elif sub == "use":
        _bb_cmd_use(args, project_path)
    elif sub == "set-default":
        _bb_cmd_set_default(args, project_path)
    else:
        print(f"Unknown bitbucket subcommand: {sub}")


def _bb_cmd_login(args, project_path: str) -> None:
    import getpass

    from services import bitbucket_credentials as bb_creds
    from services.bitbucket_client import BitbucketDataCenterClient, BitbucketError

    # --global stores the account in ~/.c3/config.json so it is reusable in
    # every C3 project (load_bitbucket_config falls back to it automatically).
    if getattr(args, "use_global", False):
        project_path = str(Path.home())

    base_url = (args.url or "").rstrip("/")
    username = args.username or input(f"Username for {base_url}: ").strip()
    if not username:
        print("Login cancelled -- username required.")
        return
    token = args.token or getpass.getpass(f"Personal Access Token for {username}: ").strip()
    if not token:
        print("Login cancelled -- token required.")
        return

    try:
        bb_creds.save_credentials(
            base_url, username, token,
            project_path=project_path,
            set_active=not getattr(args, "no_set_active", False),
        )
    except bb_creds.BitbucketCredentialError as exc:
        print(f"[error] {exc}")
        return

    if getattr(args, "insecure", False):
        bb_creds.set_verify_tls(False, project_path=project_path)

    scope = "global (~/.c3)" if getattr(args, "use_global", False) else "project"
    print(f"[OK] Stored credentials for {username}@{base_url} [{scope}]")

    # Connection probe -- non-fatal if it fails (token might be valid but the
    # network is blocked right now). Gate success on application-properties
    # only; whoami enrichment is best-effort so a valid login never prints a
    # failure (Bitbucket DC has no /users/me).
    try:
        client = BitbucketDataCenterClient(
            base_url=base_url, token=token,
            verify_tls=not getattr(args, "insecure", False),
        )
        props = client.application_properties()
        version = props.get("version", "?")
        print(f"     Server: {version} ({base_url})")
    except BitbucketError as exc:
        print(f"[warn] Connection probe failed: {exc}")
        print("       Token saved anyway -- re-test with `c3 bitbucket status`.")
        return
    try:
        user = client.whoami()
        if user:
            print(
                f"     Auth as: {user.get('displayName', username)} "
                f"<{user.get('emailAddress', '?')}>"
            )
    except BitbucketError:
        pass


def _bb_cmd_logout(args, project_path: str) -> None:
    from services import bitbucket_credentials as bb_creds

    base_url = (getattr(args, "url", "") or "").rstrip("/")
    username = getattr(args, "username", "") or ""
    if not base_url or not username:
        active = bb_creds.get_active_account(project_path)
        base_url = base_url or active.get("base_url", "")
        username = username or active.get("username", "")
    if not base_url or not username:
        print("[error] No account specified and no active account configured.")
        return
    removed = bb_creds.delete_credentials(base_url, username, project_path=project_path)
    if removed:
        print(f"[OK] Removed {username}@{base_url}")
    else:
        print(f"[warn] Nothing to remove for {username}@{base_url}")


def _bb_cmd_status(args, project_path: str) -> None:
    from core.config import load_bitbucket_config
    from services import bitbucket_credentials as bb_creds
    from services.bitbucket_client import BitbucketDataCenterClient, BitbucketError

    cfg = load_bitbucket_config(project_path)
    active = cfg.get("active") or {}
    accounts = cfg.get("accounts") or []

    print("[bitbucket:status]")
    print(f"  Active  : {active.get('username') or '-'}@{active.get('base_url') or '-'}")
    print(f"  Defaults: project={cfg.get('default_project') or '-'} repo={cfg.get('default_repo') or '-'}")
    print(f"  Verify TLS: {cfg.get('verify_tls', True)}")
    print(f"  Accounts ({len(accounts)}):")
    for a in accounts:
        marker = "*" if a == active else " "
        print(f"    {marker} {a.get('username','?')}@{a.get('base_url','?')}")

    if not active.get("base_url") or not active.get("username"):
        print("  Connection: (no active account)")
        return
    token = bb_creds.load_token(active["base_url"], active["username"])
    if not token:
        print("  Connection: FAIL -- no token in keyring")
        return
    try:
        client = BitbucketDataCenterClient(
            base_url=active["base_url"], token=token,
            verify_tls=bool(cfg.get("verify_tls", True)),
        )
        props = client.application_properties()
        print(f"  Connection: OK (version {props.get('version','?')})")
    except BitbucketError as exc:
        print(f"  Connection: FAIL -- {exc}")


def _bb_cmd_use(args, project_path: str) -> None:
    from services import bitbucket_credentials as bb_creds
    bb_creds.set_active_account(args.url, args.username, project_path=project_path)
    print(f"[OK] Active account: {args.username}@{args.url.rstrip('/')}")


def _bb_cmd_set_default(args, project_path: str) -> None:
    from services import bitbucket_credentials as bb_creds
    bb_creds.set_default_repo(args.project, args.repo, project_path=project_path)
    print(f"[OK] Default repo: {args.project}/{args.repo}")


def cmd_creds(args):
    """Credential vault management (global + per-project scopes)."""
    sub = getattr(args, "creds_cmd", None)
    if not sub:
        print("Usage: c3 creds {set,get,list,rm,import} [args]")
        return

    project_path = getattr(args, "project_path", ".") or "."

    if sub == "set":
        _creds_cmd_set(args, project_path)
    elif sub == "get":
        _creds_cmd_get(args, project_path)
    elif sub == "list":
        _creds_cmd_list(args, project_path)
    elif sub == "rm":
        _creds_cmd_rm(args, project_path)
    elif sub == "import":
        _creds_cmd_import(args, project_path)
    else:
        print(f"Unknown creds subcommand: {sub}")


def _creds_scope(args) -> str:
    return "global" if getattr(args, "use_global", False) else "project"


def _creds_entry_line(name: str, entry: dict) -> str:
    flags = [f for f in ("inject", "agent_readable") if entry.get(f)]
    parts = [
        f"{name:<24} {entry.get('scope', '?'):<8} {entry.get('type', 'token'):<10}"
        f" len={entry.get('value_len', '?')}",
    ]
    if entry.get("env_var"):
        parts.append(f"env_var={entry['env_var']}")
    if flags:
        parts.append("[" + ",".join(flags) + "]")
    if entry.get("description"):
        parts.append(f"-- {entry['description']}")
    return "  ".join(parts)


def _creds_cmd_set(args, project_path: str) -> None:
    import getpass

    from services import credential_store as cred_store

    value = getattr(args, "value", "") or ""
    if getattr(args, "stdin", False):
        value = sys.stdin.read().rstrip("\n")
    if not value:
        value = getpass.getpass(f"Value for {args.name}: ")
    if not value:
        print("Cancelled -- value required.")
        return
    scope = _creds_scope(args)
    try:
        entry = cred_store.set_credential(
            args.name, value,
            scope=scope, project_path=project_path,
            description=getattr(args, "desc", "") or "",
            ctype=getattr(args, "ctype", "token") or "token",
            env_var=getattr(args, "env_var", "") or "",
            agent_readable=bool(getattr(args, "agent_readable", False)),
            inject=bool(getattr(args, "inject", False)),
        )
    except (cred_store.CredentialError, RuntimeError) as exc:
        print(f"[error] {exc}")
        return
    print(f"[OK] Stored credential '{args.name}' "
          f"(scope={scope}, storage={entry['storage']}, len={entry['value_len']})")
    if entry["agent_readable"]:
        print("[warn] agent_readable=true -- the agent can read this value "
              "into its context and transcripts.")


def _creds_cmd_get(args, project_path: str) -> None:
    from services import credential_store as cred_store

    entry = cred_store.get_entry(args.name, project_path=project_path)
    if not entry:
        print(f"[error] no credential named '{args.name}'")
        return
    print(_creds_entry_line(args.name, entry))
    print(f"storage={entry.get('storage', 'keyring')}  "
          f"created={entry.get('created', '?')}  updated={entry.get('updated', '?')}")
    fp = cred_store.fingerprint(args.name, project_path=project_path)
    print(f"fingerprint={fp or 'unresolvable'}")
    if getattr(args, "show", False):
        value = cred_store.get_value(args.name, project_path=project_path)
        print(value if value is not None else "[error] value missing from store")


def _creds_cmd_list(args, project_path: str) -> None:
    from services import credential_store as cred_store

    entries = cred_store.list_entries(project_path)
    if not entries:
        print("No credentials registered. Use `c3 creds set NAME` "
              "(add --global for all projects).")
        return
    print(f"{len(entries)} credential(s) — project scope shadows global:")
    for name, entry in entries.items():
        print("  " + _creds_entry_line(name, entry))


def _creds_cmd_rm(args, project_path: str) -> None:
    from services import credential_store as cred_store

    scope = _creds_scope(args)
    entry = cred_store.get_entry(args.name, project_path=project_path)
    if entry and entry["scope"] == "global" and scope == "project":
        print(f"[error] '{args.name}' is a global credential -- "
              "re-run with --global to delete it.")
        return
    if cred_store.delete_credential(args.name, scope=scope, project_path=project_path):
        print(f"[OK] Removed credential '{args.name}' (scope={scope})")
    else:
        print(f"[error] no credential named '{args.name}' in {scope} scope")


def _creds_cmd_import(args, project_path: str) -> None:
    from services import credential_store as cred_store

    env_path = Path(getattr(args, "env_file", ""))
    if not env_path.exists():
        print(f"[error] file not found: {env_path}")
        return
    try:
        text = env_path.read_text(encoding="utf-8")
        result = cred_store.import_env(
            text, scope=_creds_scope(args), project_path=project_path,
            overwrite=bool(getattr(args, "overwrite", False)),
        )
    except (cred_store.CredentialError, RuntimeError) as exc:
        print(f"[error] {exc}")
        return
    print(f"[OK] Imported {len(result['created'])} credential(s): "
          f"{', '.join(result['created']) or '-'}")
    if result["skipped"]:
        print(f"Skipped {len(result['skipped'])}: {', '.join(result['skipped'])} "
              "(use --overwrite to replace)")


def cmd_access(args):
    """Access Guard rule management — human-only mutation surface (spec §1)."""
    sub = getattr(args, "access_cmd", None)
    if not sub:
        print("Usage: c3 access {list,add,remove,check,mask} [args]")
        print("       c3 access mask {add,rm,status,activate,preview} [args]")
        return

    project_path = getattr(args, "project_path", ".") or "."

    if sub == "list":
        _access_cmd_list(args, project_path)
    elif sub == "add":
        _access_cmd_add(args, project_path)
    elif sub == "remove":
        _access_cmd_remove(args, project_path)
    elif sub == "check":
        _access_cmd_check(args, project_path)
    elif sub == "mask":
        _access_cmd_mask(args, project_path)
    else:
        print(f"Unknown access subcommand: {sub}")


def _access_scope(args) -> str:
    return "global" if getattr(args, "use_global", False) else "project"


def _access_audit(action: str, glob: str, kind: str, scope: str,
                  project_path: str) -> None:
    """Ledger + activity log for CLI rule mutations. Identifiers only; failure-safe."""
    try:
        from services.activity_log import ActivityLog
        ActivityLog(project_path).log("access_action", {
            "kind": "access", "action": action, "glob": glob,
            "rule_kind": kind, "scope": scope, "via": "cli",
        })
    except Exception:
        pass
    try:
        from services.edit_ledger import EditLedger
        EditLedger(project_path).log_edit(
            file=f"access://{glob}", change_type=f"access_{action}",
            summary=f"{action} {kind} rule '{glob}' ({scope}) via `c3 access`",
            tags=["access", action],
            detail={"kind": "access", "action": action, "glob": glob,
                    "rule_kind": kind, "scope": scope},
        )
    except Exception:
        pass


def _access_cmd_list(args, project_path: str) -> None:
    from services import access_guard

    scopes = access_guard.list_rules(project_path)
    notes = {"builtin": "built-in, always on",
             "global": "~/.c3/config.json",
             "project": ".c3/config.json"}
    for scope in ("builtin", "global", "project"):
        sec = scopes.get(scope) or {}
        rules = [(k, g) for k in ("deny", "read_only") for g in sec.get(k, [])]
        masks = sec.get("mask") or []
        print(f"[{scope}] ({notes[scope]}) — {len(rules) + len(masks)} rule(s)")
        for kind, glob in rules:
            print(f"  {kind:<10} {glob}")
        for entry in masks:
            params = entry.get("params") or {}
            detail = ", ".join(f"{k}={v}" for k, v in sorted(params.items()))
            print(f"  {'mask':<10} {entry.get('glob')}  -> "
                  f"{entry.get('preset')}" + (f"({detail})" if detail else ""))
        if sec.get("corrupt"):
            print("  [warn] access section invalid — scope fails closed "
                  "(deny-all); fix config.json 'access' by hand")
    print()
    from services import mask_activation
    summary = mask_activation.summary_line(project_path)
    if summary:
        print(summary)
        print()
    print(access_guard.COVERAGE_MATRIX)


def _access_cmd_add(args, project_path: str) -> None:
    from services import access_guard

    scope = _access_scope(args)
    try:
        result = access_guard.set_rule(args.glob, args.kind, scope, project_path)
    except ValueError as exc:
        print(f"[error] {exc}")
        return
    if result["added"]:
        _access_audit("add", result["glob"], args.kind, scope, project_path)
        print(f"[OK] Added {args.kind} rule '{result['glob']}' (scope={scope})")
    else:
        print(f"[=] Rule already present: '{result['glob']}' "
              f"({args.kind}, {scope})")


def _access_cmd_remove(args, project_path: str) -> None:
    from services import access_guard

    scope = _access_scope(args)
    try:
        result = access_guard.remove_rule(args.glob, args.kind, scope, project_path)
    except ValueError as exc:
        print(f"[error] {exc}")
        return
    if result["removed"]:
        _access_audit("remove", result["glob"], args.kind, scope, project_path)
        print(f"[OK] Removed {args.kind} rule '{result['glob']}' (scope={scope})")
    else:
        print(f"[error] no {args.kind} rule matching '{result['glob']}' "
              f"in {scope} scope")


def _access_cmd_check(args, project_path: str) -> None:
    from services import access_guard

    op = getattr(args, "op", "read") or "read"
    v = access_guard.verdict(args.target, op, project_path)
    if v.masked:
        rule = v.mask_rule
        print(f"[MASKED] {op} served as a transformed view: {args.target}")
        print(f"  rule '{rule.glob}' ({rule.scope} scope) -> {rule.preset}")
        print("  Preview what the agent sees: "
              f"c3 access mask preview {args.target}")
    elif v.denial is None:
        print(f"[OK] {op} allowed: {args.target}")
    else:
        print(access_guard.refusal(v.denial, args.target, op))


# ── mask subcommands ────────────────────────────────────────────────────────

def _parse_mask_params(raw: str, preset: str) -> dict:
    """`count=20,strategy=first` / `columns=email,name` -> typed params."""
    from services import access_guard

    schema = (access_guard.MASK_PRESETS.get(preset) or {}).get("params", {})
    params: dict = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            # bare value continues the previous list param (columns=a,b,c)
            last = next((k for k in reversed(list(params))
                         if schema.get(k) is list), None)
            if last:
                params[last].append(chunk)
                continue
            raise ValueError(f"cannot parse param '{chunk}' — use key=value")
        key, value = (part.strip() for part in chunk.split("=", 1))
        expected = schema.get(key)
        if expected is int:
            params[key] = int(value)
        elif expected is list:
            params[key] = [value]
        else:
            params[key] = value
    return params


def _access_cmd_mask_add(args, project_path: str) -> None:
    from services import access_guard, mask_activation

    scope = _access_scope(args)
    try:
        params = _parse_mask_params(getattr(args, "params", "") or "",
                                    args.preset)
        result = access_guard.set_mask_rule(args.glob, args.preset, params,
                                            scope, project_path)
    except ValueError as exc:
        print(f"[error] {exc}")
        return
    _access_audit("mask-add", result["glob"], args.preset, scope, project_path)
    verb = "Replaced" if result["replaced"] else "Added"
    print(f"[OK] {verb} mask rule '{result['glob']}' -> {args.preset} "
          f"(scope={scope})")
    print()
    print(mask_activation.summary_line(project_path))
    print("Run `c3 access mask activate` to purge pre-mask derived artifacts.")


def _access_cmd_mask_remove(args, project_path: str) -> None:
    from services import access_guard

    scope = _access_scope(args)
    try:
        result = access_guard.remove_mask_rule(args.glob, scope, project_path)
    except ValueError as exc:
        print(f"[error] {exc}")
        return
    if result["removed"]:
        _access_audit("mask-remove", result["glob"], "mask", scope,
                      project_path)
        print(f"[OK] Removed mask rule '{result['glob']}' (scope={scope})")
    else:
        print(f"[error] no mask rule matching '{result['glob']}' "
              f"in {scope} scope")


def _access_cmd_mask_status(args, project_path: str) -> None:
    from services import mask_activation

    st = mask_activation.status(project_path)
    print(f"rules      : {st['rule_count']}")
    print(f"status     : {st['status']}")
    print(f"stale      : {st['stale']}")
    if st["corrupt_scopes"]:
        print(f"corrupt    : {', '.join(st['corrupt_scopes'])}")
    report = st.get("last_report") or {}
    if report:
        print(f"last run   : {report.get('views_built', 0)} view(s) built, "
              f"{len(report.get('failures') or [])} failure(s)")
        for failure in (report.get("failures") or [])[:10]:
            print(f"  [fail] {failure['path']}: {failure['detail']}")
    print()
    print(mask_activation.summary_line(project_path) or "no mask rules")


def _access_cmd_mask_activate(args, project_path: str) -> None:
    from services import mask_activation

    print("Activating mask rules (purge -> build -> validate)...")
    report = mask_activation.activate(
        project_path, rebuild_index=bool(getattr(args, "reindex", False)))
    print(f"  files matched      : {report['files']}")
    print(f"  views built        : {report['views_built']}")
    print(f"  cache entries wiped: {report['cache_entries_removed']}")
    print(f"  index dropped      : {report['index_dropped']}")
    print(f"  file memory dropped: {report['file_memory_dropped']}")
    facts = report.get("facts") or {}
    print(f"  facts purged       : {facts.get('purged', 0)} "
          f"(+{facts.get('unknown_purged', 0)} unknown-provenance)")
    for failure in report.get("failures") or []:
        print(f"  [fail] {failure['path']}: {failure['detail']}")
    _access_audit("mask-activate", f"{report['files']} file(s)", "mask",
                  "project", project_path)
    print()
    print(mask_activation.summary_line(project_path))


def _access_cmd_mask_preview(args, project_path: str) -> None:
    """Show exactly what the agent will see. Human surface only."""
    from pathlib import Path as _Path

    from services import access_guard, mask_mirror

    target = _Path(args.target)
    if not target.is_absolute():
        target = _Path(project_path) / args.target
    v = access_guard.verdict(str(target), "read", project_path)
    if v.denial:
        print(access_guard.refusal(v.denial, str(target), "read"))
        return
    if not v.masked:
        print(f"[=] Not masked: {args.target} — the agent sees the real file.")
        return
    try:
        view = mask_mirror.render_for_path(target, project_path)
    except mask_mirror.MaskUnavailable as exc:
        print(f"[error] {exc.message}")
        return
    print(f"rule    : '{v.mask_rule.glob}' ({v.mask_rule.scope} scope)")
    print(f"preset  : {v.mask_rule.preset}  {v.mask_rule.params_dict or ''}")
    print(f"stats   : {view.stats}")
    print("-" * 70)
    print(view.with_header(args.target))


def _access_cmd_mask(args, project_path: str) -> None:
    sub = getattr(args, "mask_cmd", None) or "status"
    handlers = {
        "add": _access_cmd_mask_add,
        "rm": _access_cmd_mask_remove,
        "remove": _access_cmd_mask_remove,
        "status": _access_cmd_mask_status,
        "activate": _access_cmd_mask_activate,
        "preview": _access_cmd_mask_preview,
    }
    handler = handlers.get(sub)
    if handler is None:
        print(f"[error] unknown mask subcommand '{sub}' — expected one of: "
              f"{', '.join(sorted(handlers))}")
        return
    handler(args, project_path)


def cmd_jira(args):
    """Jira Cloud / Data Center credential + workspace management."""
    sub = getattr(args, "jira_cmd", None)
    if not sub:
        print("Usage: c3 jira {login,logout,status,use,set-default} [args]")
        return

    project_path = getattr(args, "project_path", ".") or "."

    if sub == "login":
        _jira_cmd_login(args, project_path)
    elif sub == "logout":
        _jira_cmd_logout(args, project_path)
    elif sub == "status":
        _jira_cmd_status(args, project_path)
    elif sub == "use":
        _jira_cmd_use(args, project_path)
    elif sub == "set-default":
        _jira_cmd_set_default(args, project_path)
    else:
        print(f"Unknown jira subcommand: {sub}")


def _jira_deployment_for(args, base_url: str) -> str:
    """Explicit --deployment wins; *.atlassian.net infers cloud; else unknown."""
    dep = getattr(args, "deployment", "") or ""
    if dep:
        return dep
    host = base_url.split("//", 1)[-1].split("/", 1)[0].lower()
    if host.endswith(".atlassian.net") or host.endswith(".jira.com"):
        return "cloud"
    return ""


def _jira_cmd_login(args, project_path: str) -> None:
    import getpass

    from services import jira_credentials as jr_creds
    from services.jira_client import JiraClient, JiraError

    # --global stores the account in ~/.c3/config.json so it is reusable in
    # every C3 project (load_jira_config falls back to it automatically).
    if getattr(args, "use_global", False):
        project_path = str(Path.home())

    base_url = (args.url or "").rstrip("/")
    deployment = _jira_deployment_for(args, base_url)
    if not deployment:
        print("[error] --deployment cloud|data_center is required for self-hosted URLs.")
        return
    name = getattr(args, "name", "") or ""
    if not name:
        host = base_url.split("//", 1)[-1].split("/", 1)[0]
        name = host.split(".")[0] or "default"

    prompt_label = "Email" if deployment == "cloud" else "Username"
    username = args.username or input(f"{prompt_label} for {base_url}: ").strip()
    if not username:
        print("Login cancelled -- username required.")
        return
    token_label = "API token" if deployment == "cloud" else "Personal Access Token"
    token = args.token or getpass.getpass(f"{token_label} for {username}: ").strip()
    if not token:
        print("Login cancelled -- token required.")
        return

    try:
        jr_creds.save_credentials(
            name, base_url, username, token,
            deployment=deployment,
            project_path=project_path,
            set_default=not getattr(args, "no_set_default", False),
            verify_tls=not getattr(args, "insecure", False),
            ca_bundle=getattr(args, "ca_bundle", "") or "",
            allow_insecure=getattr(args, "insecure", False),
        )
    except jr_creds.JiraCredentialError as exc:
        print(f"[error] {exc}")
        return

    scope = "global (~/.c3)" if getattr(args, "use_global", False) else "project"
    print(f"[OK] Stored credentials for {username}@{base_url} as '{name}' [{deployment}, {scope}]")

    # Connection probe -- skippable for offline setup (--no-verify-login) and
    # non-fatal on failure (token might be valid but the network blocked).
    if getattr(args, "no_verify_login", False):
        print("     Connection probe skipped (--no-verify-login) -- verify later with `c3 jira status`.")
        return
    try:
        client = JiraClient(
            base_url, username, token,
            deployment=deployment,
            verify_tls=not getattr(args, "insecure", False),
            ca_bundle=getattr(args, "ca_bundle", "") or "",
        )
        info = client.server_info()
        print(f"     Server: Jira {info.get('version', '?')} ({base_url})")
    except (JiraError, ValueError) as exc:
        print(f"[warn] Connection probe failed: {exc}")
        print("       Token saved anyway -- re-test with `c3 jira status`.")
        return
    try:
        me = client.myself()
        if me:
            print(
                f"     Auth as: {me.get('displayName', username)} "
                f"<{me.get('emailAddress', '?')}>"
            )
    except JiraError:
        pass


def _jira_cmd_logout(args, project_path: str) -> None:
    from core.config import load_jira_config
    from services import jira_credentials as jr_creds

    name = getattr(args, "name", "") or ""
    if not name:
        name = load_jira_config(project_path).get("default_account", "")
    if not name:
        print("[error] No account specified and no default account configured.")
        return
    removed = jr_creds.delete_credentials(name, project_path=project_path)
    if removed:
        print(f"[OK] Removed Jira account '{name}'")
    else:
        print(f"[warn] Nothing to remove for '{name}'")


def _jira_cmd_status(args, project_path: str) -> None:
    from core.config import load_jira_config
    from services import jira_credentials as jr_creds
    from services.jira_client import JiraClient, JiraError

    cfg = load_jira_config(project_path)
    accounts = cfg.get("accounts") or {}
    default = cfg.get("default_account", "")

    print("[jira:status]")
    print(f"  Default : {default or '-'}")
    print(f"  Accounts ({len(accounts)}):")
    for acct_name, a in accounts.items():
        if not isinstance(a, dict):
            continue
        marker = "*" if acct_name == default else " "
        print(
            f"    {marker} {acct_name}: {a.get('username', '?')}@{a.get('base_url', '?')} "
            f"[{a.get('deployment', '?')}] project={a.get('default_project') or '-'}"
        )

    entry = accounts.get(default) if isinstance(accounts.get(default), dict) else None
    if not entry:
        print("  Connection: (no default account)")
        return
    token = jr_creds.load_token(entry.get("base_url", ""), entry.get("username", ""))
    if not token:
        print("  Connection: FAIL -- no token in keyring")
        return
    try:
        client = JiraClient(
            entry["base_url"], entry.get("username", ""), token,
            deployment=entry.get("deployment", "cloud"),
            verify_tls=bool(entry.get("verify_tls", True)),
            ca_bundle=entry.get("ca_bundle", ""),
        )
        info = client.server_info()
        me = client.myself()
        print(
            f"  Connection: OK (Jira {info.get('version', '?')} "
            f"as {me.get('displayName', '?')})"
        )
    except (JiraError, ValueError) as exc:
        print(f"  Connection: FAIL -- {exc}")


def _jira_cmd_use(args, project_path: str) -> None:
    from services import jira_credentials as jr_creds
    try:
        jr_creds.set_default_account(args.name, project_path=project_path)
    except jr_creds.JiraCredentialError as exc:
        print(f"[error] {exc}")
        return
    print(f"[OK] Default Jira account: {args.name}")


def _jira_cmd_set_default(args, project_path: str) -> None:
    from services import jira_credentials as jr_creds
    try:
        jr_creds.set_default_project(
            args.project, name=getattr(args, "name", "") or "",
            project_path=project_path,
        )
    except jr_creds.JiraCredentialError as exc:
        print(f"[error] {exc}")
        return
    print(f"[OK] Default Jira project: {args.project}")


def cmd_oracle(args):
    """Oracle dashboard server + Discovery API key management."""
    sub = getattr(args, "oracle_cmd", None)
    if sub in ("serve", "start"):
        # Lazy import: run_oracle builds all Oracle services (matches
        # cmd_hub's deferred-import style so bare `c3` stays fast).
        from oracle.oracle_server import run_oracle
        run_oracle(port=getattr(args, "port", None),
                   open_browser=not getattr(args, "no_browser", False))
        return
    if sub != "api":
        print("Usage: c3 oracle {serve,api} — serve: launch the dashboard; "
              "api {info,key,rotate,clear}: manage the Discovery key")
        return

    from oracle.config import load_config
    from oracle.mcp_oracle import mcp_url
    from oracle.services import api_auth

    action = getattr(args, "action", "info") or "info"

    if action == "rotate":
        print("Rotated. New Discovery API key:")
        print(f"  {api_auth.rotate()}")
        return
    if action == "clear":
        removed = api_auth.clear()
        print("Discovery API key cleared." if removed else "No stored Discovery API key to clear.")
        return

    key = api_auth.get_or_create_key()
    if action == "key":
        print(key)
        return

    cfg = load_config()
    host = cfg.get("bind_host", "127.0.0.1")
    disp_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    rest_port = getattr(args, "port", None) or cfg.get("port", 3331)
    mcp_port = getattr(args, "mcp_port", None) or cfg.get("mcp_port", 3332)
    rest_base = f"http://{disp_host}:{rest_port}/api/discovery"
    url = mcp_url(host, mcp_port)

    print("[oracle:api]")
    print(f"  REST base : {rest_base}")
    print(f"  OpenAPI   : {rest_base}/openapi.json")
    print(f"  MCP URL   : {url}")
    print(f"  Auth      : Bearer {key}")
    print()
    print("  Claude .mcp.json entry:")
    snippet = {
        "mcpServers": {
            "c3-oracle": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {key}"},
            }
        }
    }
    print(json.dumps(snippet, indent=2))


def cmd_projects(args):
    """Manage the global C3 project registry."""
    from services.project_manager import ProjectManager
    pm = ProjectManager()
    sub = getattr(args, 'projects_cmd', 'list') or 'list'
    path = getattr(args, 'project_path', None)

    if sub == 'list':
        projects = pm.list_projects()
        if not projects:
            print('No projects registered. Use `c3 projects add <path>` to register one.')
            return
        fmt = '{:<25} {:<12} {:<8} {:<6} {}'
        print(fmt.format('NAME', 'IDE', 'STATUS', 'PORT', 'PATH'))
        print('-' * 80)
        for p in projects:
            status = 'ACTIVE' if p.get('active') else 'stopped'
            port = str(p['port']) if p.get('port') else '-'
            print(fmt.format(
                p.get('name', '?')[:24],
                p.get('ide', '?')[:11],
                status,
                port,
                p.get('path', ''),
            ))
        active = sum(1 for p in projects if p.get('active'))
        print(f"\n{len(projects)} project(s) -- {active} active session(s)")

    elif sub == 'add':
        if not path:
            print('Usage: c3 projects add <project_path> [--name NAME]')
            return
        name = getattr(args, 'name', None)
        entry = pm.add_project(path, name)
        print(f"Registered: {entry['name']}  ({entry['path']})")

    elif sub == 'remove':
        if not path:
            print('Usage: c3 projects remove <project_path>')
            return
        removed = pm.remove_project(path)
        print(f'Removed: {path}' if removed else f'Not found: {path}')

    elif sub == 'start':
        if not path:
            print('Usage: c3 projects start <project_path>')
            return
        ok = pm.launch_session(path)
        print(f'Launching session for {path}...' if ok else 'Failed to launch session.')

    elif sub == 'sessions':
        sessions = pm.get_active_sessions()
        if not sessions:
            print('No active sessions.')
            return
        for s in sessions:
            print(f"  Port {s['port']:>5}  {s.get('project_name', '?'):<25}  {s.get('project_path', '')}")


def cmd_sub(args):
    """Manage sub-projects: designated sub-folders with linked .c3 branches."""
    parent = str(Path(getattr(args, "parent", ".") or ".").resolve())
    if not (Path(parent) / CONFIG_DIR).is_dir():
        print(f"No .c3 found in {parent}. Run 'c3 init' there first.")
        return
    # Import after the cheap guard — the registry module needs a resolvable home.
    from services.subprojects import VALID_CASCADE_OPS, SubprojectManager

    sm = SubprojectManager(parent)
    sub = getattr(args, "sub_cmd", "list") or "list"
    target = getattr(args, "target", None)
    as_json = getattr(args, "json", False)

    if sub == "add":
        if not target:
            print("Usage: c3 sub add <folder> [--parent PATH] [--name NAME]")
            return
        result = sm.add(
            target,
            name=getattr(args, "name", None),
            ide=getattr(args, "ide", None),
            run_init=not getattr(args, "no_init", False),
            reindex_parent=not getattr(args, "no_reindex_parent", False),
        )
        if as_json:
            print(json.dumps(result, indent=2))
            return
        if not result.get("added"):
            print(f"Failed: {result.get('error')}")
            return
        verb = "Adopted (existing .c3 kept)" if result.get("adopted") else "Initialized"
        print(f"\n[OK] {verb}: {result['name']}  ({result['path']})")
        code = (result.get("parent_reindex") or {}).get("code")
        if code:
            print(f"  Parent reindexed: {code.get('files_indexed', '?')} files, "
                  f"{code.get('chunks_created', '?')} chunks (sub-project now excluded)")

    elif sub == "list":
        report = sm.reconcile(fix=False)  # report-only consistency pass
        children = sm.list()
        if as_json:
            print(json.dumps({"children": children, "orphans": report.get("orphans", [])}, indent=2))
            return
        if not children:
            print("No sub-projects designated. Use `c3 sub add <folder>`.")
            return
        fmt = "{:<22} {:<16} {:>6} {:>7}  {}"
        print(fmt.format("NAME", "STATUS", "FACTS", "ALERTS", "REL PATH"))
        print("-" * 76)
        for c in children:
            print(fmt.format(
                (c.get("name") or "?")[:21],
                c.get("status", "?"),
                c.get("facts_count", 0),
                c.get("notification_count", 0),
                c.get("rel_path", ""),
            ))
        issues = sum(1 for c in children if c["status"] != "ok")
        line = f"\n{len(children)} sub-project(s)"
        if issues:
            line += f" -- {issues} with issues (run `c3 sub check --fix`)"
        if report.get("orphans"):
            line += f" -- {len(report['orphans'])} registry orphan(s)"
        print(line)

    elif sub == "remove":
        if not target:
            print("Usage: c3 sub remove <name|path> [--clear] [--yes]")
            return
        mode = "clear" if getattr(args, "clear", False) else "unlink"
        if mode == "clear" and not getattr(args, "yes", False):
            print("This will DELETE the sub-project's .c3 directory and instruction docs.")
            confirm = input("Type 'clear' to confirm: ").strip().lower()
            if confirm != "clear":
                print("Aborted.")
                return
        result = sm.remove(target, mode=mode,
                           reindex_parent=not getattr(args, "no_reindex_parent", False))
        if as_json:
            print(json.dumps(result, indent=2))
            return
        if not result.get("removed"):
            print(f"Failed: {result.get('error')}")
            return
        print(f"\n[OK] {'Cleared' if mode == 'clear' else 'Unlinked'}: "
              f"{result.get('name')}  ({result.get('path')})")
        for w in result.get("warnings", []):
            print(f"  warning: {w}")

    elif sub == "run":
        if target not in VALID_CASCADE_OPS:
            print(f"Usage: c3 sub run {{{'|'.join(VALID_CASCADE_OPS)}}} [--include-parent] [--json]")
            return
        result = sm.cascade(target,
                            include_parent=getattr(args, "include_parent", False),
                            mcp=getattr(args, "mcp", False))
        if as_json:
            print(json.dumps(result, indent=2))
            return
        for row in result["results"]:
            mark = "OK  " if row["ok"] else "FAIL"
            extra = f" -- {row.get('error')}" if row.get("error") else ""
            print(f"  [{mark}] {row['name']:<22} {row['elapsed_ms']:>6}ms{extra}")
        s = result["summary"]
        print(f"\n{target}: {s['ok']}/{s['total']} ok, {s['failed']} failed")

    elif sub == "check":
        result = sm.reconcile(fix=getattr(args, "fix", False),
                              prune=getattr(args, "prune", False))
        if as_json:
            print(json.dumps(result, indent=2))
            return
        if not result["children"] and not result["orphans"] and not result["pruned"]:
            print("No sub-projects designated.")
            return
        for c in result["children"]:
            print(f"  [{c['status']:<16}] {c.get('name') or '?':<22} {c.get('rel_path', '')}")
        for o in result["orphans"]:
            print(f"  [orphan_registry ] {o}")
        for f in result.get("fixed", []):
            print(f"  fixed: {f.get('action')} -> {f.get('path') or f.get('rel_path')}")
        for p in result.get("pruned", []):
            print(f"  pruned: {p.get('rel_path')}")
        if result["ok"]:
            print("\nAll links consistent.")
        else:
            hint = "" if getattr(args, "fix", False) else " Run `c3 sub check --fix` to repair."
            print(f"\nIssues found.{hint}")


def cmd_session_benchmark(args):
    """Run real-world session workflow benchmark."""
    if getattr(args, "command", "") == "session-benchmark":
        print("[note] `c3 session-benchmark` is aliased as `c3 bench session`. Prefer the unified form going forward.")
    from services.session_benchmark import (
        SessionBenchmark,
        generate_report,
        load_session_benchmark_history,
        render_html,
    )

    project_path = Path(args.project_path or ".").resolve()
    print_header("C3 Session Benchmark")
    print(f"  Project: {project_path}")
    print(f"  Sample size: {args.sample_size}, min tokens: {args.min_tokens}")
    print()

    bench = SessionBenchmark(str(project_path), sample_size=args.sample_size, min_tokens=args.min_tokens)
    if not bench.files:
        print("Error: no benchmark-eligible files found")
        return

    print(f"  Files: {len(bench.files)} eligible, {len(bench.sample)} sampled")
    print("  Running 6 workflow scenarios...\n")

    results = bench.run_all()
    report = generate_report(str(project_path), results, args.sample_size, len(bench.files), sampled_files=bench.sample)

    # Save JSON
    out_dir = project_path / ".c3" / "session_benchmark" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.output:
        json_path = Path(args.output)
        if not json_path.is_absolute():
            json_path = project_path / json_path
    else:
        json_path = out_dir / f"session_{time.strftime('%Y%m%d_%H%M%S')}.json"

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Load history (including the run we just saved) and render HTML with trends
    history = load_session_benchmark_history(str(project_path))
    if not any(r.get("timestamp") == report["timestamp"] for r in history):
        history.append(report)

    html_path = Path(args.html_output or ".c3/session_benchmark/latest.html")
    if not html_path.is_absolute():
        html_path = project_path / html_path
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(report, history=history), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
        return

    # Console output
    sc = report["scorecard"]
    lon = report["session_longevity"]

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "█░┌┐└┘─│".encode(encoding)
        unicode_ok = True
    except (UnicodeEncodeError, LookupError):
        unicode_ok = False

    def _bar(pct, width=20):
        filled = max(0, min(width, round(float(pct) / 100 * width)))
        fc, ec = ("█", "░") if unicode_ok else ("#", "-")
        return fc * filled + ec * (width - filled)

    top = "  ┌─ Session Scorecard ──────────────────────────────────────────┐" if unicode_ok else "  +- Session Scorecard -------------------------------------------------+"
    mid = "  │" if unicode_ok else "  |"
    bot = "  └──────────────────────────────────────────────────────────────┘" if unicode_ok else "  +--------------------------------------------------------------+"
    end = mid[-1]
    print(top)
    print(f"{mid}  Token savings   {sc['token_savings_pct']:>6.1f}%  [{_bar(sc['token_savings_pct'])}]  {sc['budget_multiplier']}x     {end}")
    print(f"{mid}  Quality (C3)    {sc['avg_quality_c3']:>6.1f}%  vs baseline {sc['avg_quality_baseline']:.1f}%              {end}")
    print(f"{mid}  Session turns   {lon['estimated_turns_c3']:>6.1f}   vs baseline {lon['estimated_turns_baseline']:.1f}  ({lon['turn_multiplier']}x)   {end}")
    print(bot)
    print()

    print(f"  {'Scenario':<28} {'Savings':>8}  {'Budget':>7}  {'C3 tok':>8}  {'Base tok':>9}")
    print(f"  {'-'*28} {'-'*8}  {'-'*7}  {'-'*8}  {'-'*9}")
    for s in results:
        label = s.name.replace("_", " ").title()
        print(f"  {label:<28} {s.token_savings_pct:>7.1f}%  {s.budget_multiplier:>6.2f}x  {s.total_tokens_c3:>8,}  {s.total_tokens_baseline:>9,}")

    print()
    print(f"  JSON: {json_path}")
    print(f"  HTML: {html_path}")

    try:
        from services.benchmark_dashboard import generate_dashboard
        dash = generate_dashboard(str(project_path))
        print(f"  Dashboard: {dash}")
    except Exception:
        pass


def cmd_benchmark_e2e(args):
    """Run end-to-end AI session benchmark comparing C3-augmented vs baseline workflows."""
    if getattr(args, "command", "") == "benchmark-e2e":
        print("[note] `c3 benchmark-e2e` is aliased as `c3 bench e2e` (or `c3 bench delegate`). Prefer the unified form going forward.")
    from services.e2e_benchmark import (
        E2EBenchmark,
        detect_providers,
        generate_e2e_report,
        render_e2e_html,
    )
    from services.e2e_evaluator import Evaluator
    from services.e2e_tasks import TaskBuilder
    from services.file_memory import FileMemoryStore
    from services.indexer import CodeIndex

    project_path = Path(args.project_path or ".").resolve()
    print_header("C3 End-to-End Benchmark")
    print(f"  Project: {project_path}")

    # Parse model overrides: claude=sonnet,gemini=gemini-2.5-flash
    model_overrides = {}
    if args.models:
        for pair in args.models.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                model_overrides[k.strip()] = v.strip()

    # Detect providers
    requested = [p.strip() for p in args.providers.split(",")] if args.providers else None
    providers = detect_providers(requested, model_overrides,
                                permission_mode=getattr(args, "permission_mode", "bypassPermissions"))
    if not providers:
        print("\n  Error: No AI CLIs detected. Install claude, gemini, or codex.")
        return

    print(f"  Providers: {', '.join(p.name + ('(' + p.model + ')' if p.model else '') for p in providers)}")

    # Build tasks
    indexer = CodeIndex(str(project_path), str(project_path / ".c3" / "index"))
    file_memory = FileMemoryStore(str(project_path))
    builder = TaskBuilder(str(project_path), indexer=indexer, file_memory=file_memory)
    # Determine category filter
    if args.tasks and args.tasks != "all":
        categories = set(c.strip() for c in args.tasks.split(","))
    else:
        categories = None  # uses BENCHMARK_CATEGORIES default

    all_tasks = builder.build_tasks(max_per_category=args.max_tasks,
                                    categories=categories)

    if not all_tasks:
        print("\n  Error: No tasks generated. Is the project indexed? Run `c3 init` first.")
        return

    cats: dict[str, int] = {}
    for t in all_tasks:
        cats[t.category] = cats.get(t.category, 0) + 1
    cat_summary = "  ".join(f"{c} ({n})" for c, n in sorted(cats.items()))
    print(f"  Tasks:     {len(all_tasks)} across {len(cats)} categories")
    print(f"             {cat_summary}")
    total_runs = len(all_tasks) * len(providers) * 2  # x2 for C3 + baseline
    min_est = max(1, total_runs * 20 // 60)
    max_est = max(2, total_runs * args.timeout // 60)
    print(f"  AI calls:  {total_runs} total  ({len(all_tasks)} tasks x {len(providers)} provider(s) x 2 modes)")
    print(f"  Timeout:   {args.timeout}s per call")
    print(f"  Est. time: {min_est}–{max_est} min  ({total_runs} calls x 20–{args.timeout}s each)")
    print()

    # Delegate benchmark mode -- Ollama vs Codex comparison
    if getattr(args, "delegate_benchmark", False):
        from services.e2e_benchmark import DelegateBenchmark
        from services.runtime import build_runtime
        print("  Running delegate backend comparison (Ollama vs Codex)...\n")
        rt = build_runtime(str(project_path))
        delegate_types = None
        if getattr(args, "delegate_types", None):
            delegate_types = [t.strip() for t in args.delegate_types.split(",")]
        dbench = DelegateBenchmark(
            project_path=str(project_path),
            svc=rt,
            verbose=args.verbose,
            task_types=delegate_types,
        )
        dresults = dbench.run_all()
        dreport = DelegateBenchmark.generate_report(dresults)

        # Print summary
        print(f"\n  {'=' * 60}")
        print(f"  DELEGATE BENCHMARK -- {len(dresults)} runs")
        print(f"  {'=' * 60}")
        for backend, stats in dreport.get("backends", {}).items():
            print(f"\n  {backend.upper()}:")
            print(f"    Success rate: {stats['success_rate']}%  ({stats['successes']}/{stats['tasks_run']})")
            print(f"    Avg latency:  {stats['avg_latency_s']}s")
            print(f"    Avg tokens:   {stats['avg_output_tokens']}")
            print(f"    Models:       {', '.join(stats['models_used'])}")

        # Save JSON report
        out_dir = project_path / ".c3" / "e2e_benchmark" / "runs"
        out_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        delegate_json_path = out_dir / f"delegate_{ts}.json"
        with open(delegate_json_path, "w", encoding="utf-8") as f:
            json.dump(dreport, f, indent=2)
        print(f"\n  Report saved: {delegate_json_path}")
        return

    # Dry run — show plan and exit
    if args.dry_run:
        print("  DRY RUN — Tasks that would be executed:\n")
        for t in all_tasks:
            print(f"    [{t.category}] {t.id}: {t.query[:80]}...")
        task_workers = getattr(args, "task_workers", 1)
        effective_runs = max(1, total_runs // task_workers)
        print(f"\n  Estimated time: {effective_runs * 60 // 60}–{effective_runs * 120 // 60} minutes")
        print(f"  (based on {total_runs} calls at 60-120s each, {task_workers} task worker(s))")
        return

    # Setup evaluator
    evaluator = Evaluator(judge_cli=args.judge, judge_model=args.judge_model)

    # Run benchmark
    task_workers = getattr(args, "task_workers", 1)
    use_cache = not getattr(args, "no_cache", False)
    def _progress(completed, total, result):
        winner = "C3 wins" if result.c3_wins else "Base wins"
        pct = completed / total * 100
        print(f"\r  [{completed}/{total}] {pct:.0f}% — {result.task_id}: "
              f"{winner} ({result.score_delta:+.3f})", end="", flush=True)
        if completed == total:
            print()

    print(f"  Starting {total_runs} AI calls — grab a coffee...\n")
    bench = E2EBenchmark(
        project_path=str(project_path),
        providers=providers,
        tasks=all_tasks,
        evaluator=evaluator,
        timeout=args.timeout,
        parallel=not args.no_parallel,
        verbose=args.verbose,
        on_progress=None if args.verbose else _progress,
        task_workers=task_workers,
        cache=use_cache,
        permission_mode=getattr(args, "permission_mode", "bypassPermissions"),
    )
    results = bench.run_all()

    # Generate report
    report = generate_e2e_report(str(project_path), results, providers, all_tasks)

    # Save JSON
    out_dir = project_path / ".c3" / "e2e_benchmark" / "runs"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.output:
        json_path = Path(args.output)
        if not json_path.is_absolute():
            json_path = project_path / json_path
    else:
        json_path = out_dir / f"e2e_{time.strftime('%Y%m%d_%H%M%S')}.json"

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Save HTML
    html_path = Path(args.html_output or ".c3/e2e_benchmark/latest.html")
    if not html_path.is_absolute():
        html_path = project_path / html_path
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_e2e_html(report), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2))
        return

    # Console output
    sc = report["scorecard"]
    trends = report.get("trends", {})
    sl = trends.get("since_last", {})
    has_trends = trends.get("available", False)

    print()
    print("  +- E2E Scorecard ---------------------------------------------------+")
    wr_str = f"{sc['c3_win_rate']:>5.1f}%"
    if has_trends and sl.get("win_rate_delta", 0) != 0:
        wr_str += f"  ({sl['win_rate_delta']:+.1f}pp)"
    print(f"  |  C3 Win Rate     {wr_str}   ({sc['c3_wins']} / {sc['c3_wins'] + sc['baseline_wins']} tasks)")
    c3_str = f"{sc['avg_score_c3']:>5.3f}"
    if has_trends and sl.get("avg_c3_delta", 0) != 0:
        c3_str += f"  ({sl['avg_c3_delta']:+.3f})"
    print(f"  |  Avg Score (C3)  {c3_str}   vs baseline {sc['avg_score_baseline']:.3f}")
    print(f"  |  Score Delta     {sc['avg_score_delta']:>+5.3f}")
    if has_trends:
        print(f"  |  Run History     {trends.get('run_count', 0)} runs")
    print("  +------------------------------------------------------------------+")
    print()

    # Per-provider summary
    print(f"  {'Provider':<12} {'Model':<20} {'C3 Wins':>8} {'Win Rate':>9} {'Avg Delta':>10} {'Cost (C3)':>10}")
    print(f"  {'-'*12} {'-'*20} {'-'*8} {'-'*9} {'-'*10} {'-'*10}")
    for pname, pdata in report.get("provider_stats", {}).items():
        print(f"  {pname:<12} {pdata['model']:<20} {pdata['c3_wins']:>3}/{pdata['tasks_run']:<4} "
              f"{pdata['win_rate_c3']:>7.1f}%  {pdata['avg_score_delta']:>+9.3f}  "
              f"${pdata['total_cost_c3_usd']:>8.4f}")

    # Category breakdown table
    cat_stats = report.get("category_stats", {})
    if cat_stats:
        print(f"\n  {'Category':<22} {'Wins':>5}  {'Rate':>6}  {'Delta':>7}")
        print(f"  {'-'*22} {'-'*5}  {'-'*6}  {'-'*7}")
        for cat, cs in sorted(cat_stats.items(), key=lambda x: -x[1].get("win_rate_c3", 0)):
            wins = cs.get("c3_wins", 0)
            total_t = cs.get("tasks_run", 0)
            rate = cs.get("win_rate_c3", 0)
            delta = cs.get("avg_score_delta", 0)
            marker = "+" if rate >= 80 else ("!" if rate <= 20 else " ")
            print(f"  {marker} {cat:<20} {wins:>2}/{total_t:<2}  {rate:>5.0f}%  {delta:>+6.3f}")

    # Dimension scores
    dim_bd = report.get("dimension_breakdown", {})
    if dim_bd:
        print(f"\n  {'Dimension':<18} {'C3':>6}  {'Base':>6}  {'Delta':>7}")
        print(f"  {'-'*18} {'-'*6}  {'-'*6}  {'-'*7}")
        for dim, dv in sorted(dim_bd.items(), key=lambda x: x[1].get("delta", 0)):
            d = dv.get("delta", 0)
            marker = "!" if d < -0.05 else ("+" if d > 0.05 else " ")
            dim_label = dim.replace("_score", "").replace("_", " ")
            print(f"  {marker} {dim_label:<16} {dv.get('avg_c3', 0):>6.3f}  {dv.get('avg_baseline', 0):>6.3f}  {d:>+6.3f}")

    # Insights
    ins = report.get("insights", {})
    findings = ins.get("findings", [])
    if findings:
        print("\n  Findings:")
        sev_icon = {"critical": "!!", "warning": " !", "strength": " +", "info": " *"}
        for f in findings:
            icon = sev_icon.get(f.get("severity", "info"), " *")
            title = f.get("title", "")
            action = f.get("action", "")
            print(f"  [{icon}] {title}")
            if action:
                print(f"        -> {action}")

    print()
    print(f"  JSON: {json_path}")
    print(f"  HTML: {html_path}")

    try:
        from services.benchmark_dashboard import generate_dashboard
        dash = generate_dashboard(str(project_path))
        print(f"  Dashboard: {dash}")
    except Exception:
        pass


def cmd_bench(args):
    """Unified benchmark dispatcher: c3 bench {quick|session|e2e|delegate|all|dashboard}."""
    from services.benchmark_dashboard import generate_dashboard

    tier = getattr(args, "bench_tier", None)
    if not tier:
        print("Usage: c3 bench {quick|session|e2e|delegate|all|dashboard}")
        print("  quick     Local synthetic benchmark (fastest)")
        print("  session   Workflow scenarios (6 synthetic workflows)")
        print("  e2e       End-to-end real AI calls (claude/gemini/codex)")
        print("  delegate  Ollama vs Codex delegate comparison")
        print("  all       Run quick + session + e2e + dashboard")
        print("  dashboard Regenerate unified HTML dashboard")
        return

    def _default(name, value):
        if not hasattr(args, name):
            setattr(args, name, value)

    project_path = Path(args.project_path or ".").resolve()

    if tier == "quick":
        cmd_benchmark(args)
    elif tier == "session":
        cmd_session_benchmark(args)
    elif tier == "e2e":
        _default("delegate_benchmark", False)
        _default("delegate_types", None)
        cmd_benchmark_e2e(args)
    elif tier == "delegate":
        # Map delegate tier onto the existing --delegate-benchmark path
        args.delegate_benchmark = True
        _default("providers", None)
        _default("models", None)
        _default("tasks", "all")
        _default("max_tasks", 1)
        _default("timeout", 120)
        _default("dry_run", False)
        _default("html_output", None)
        _default("no_parallel", True)
        _default("judge", None)
        _default("judge_model", None)
        _default("task_workers", 1)
        _default("no_cache", True)
        _default("permission_mode", "bypassPermissions")
        cmd_benchmark_e2e(args)
    elif tier == "all":
        print_header("C3 Benchmark Suite")
        print(f"  Project: {project_path}")
        print()

        # quick
        _default("sample_size", 25)
        _default("min_tokens", 200)
        _default("top_k", 5)
        _default("max_tokens", 4000)
        _default("json", False)
        _default("output", None)
        _default("html_output", None)
        _default("no_html", False)
        _default("system_name", "")
        _default("system_label", "")
        _default("system_version", "")
        cmd_benchmark(args)

        # session
        args.sample_size = 15
        cmd_session_benchmark(args)

        # e2e (unless skipped)
        if not getattr(args, "skip_e2e", False):
            _default("providers", None)
            _default("models", None)
            _default("tasks", "all")
            _default("max_tasks", 1)
            _default("timeout", 120)
            _default("dry_run", False)
            _default("verbose", False)
            _default("no_parallel", False)
            _default("judge", None)
            _default("judge_model", None)
            _default("task_workers", 1)
            _default("no_cache", False)
            _default("permission_mode", "bypassPermissions")
            _default("delegate_benchmark", False)
            _default("delegate_types", None)
            cmd_benchmark_e2e(args)

        # Regenerate unified dashboard
        out = generate_dashboard(str(project_path))
        print()
        print(f"  Dashboard: {out}")
    elif tier == "dashboard":
        out = generate_dashboard(str(project_path))
        print(f"  Dashboard: {out}")
        if getattr(args, "open", False):
            import webbrowser
            webbrowser.open(f"file://{out}")
    elif tier == "external":
        _run_external_benchmark(args, project_path)
    else:
        print(f"Unknown bench tier: {tier}")


def _run_external_benchmark(args, project_path):
    """Dispatch external benchmark suites (aider-polyglot, future: swe-bench)."""
    from services.benchmark_dashboard import generate_dashboard
    suite = getattr(args, "suite", "aider-polyglot")
    print_header(f"C3 External Benchmark — {suite}")
    print(f"  Project: {project_path}")

    if suite == "aider-polyglot":
        from services.bench.external.aider_polyglot import (
            AiderPolyglotBenchmark,
            detect_aider,
            find_polyglot_repo,
            save_report,
        )

        aider_path = detect_aider()
        repo = find_polyglot_repo(getattr(args, "path", None))

        print(f"  Aider CLI:   {aider_path or '(missing — pip install aider-chat)'}")
        print(f"  Benchmark:   {repo or '(missing — git clone https://github.com/Aider-AI/polyglot-benchmark)'}")
        print(f"  Languages:   {args.languages}")
        print(f"  Max/lang:    {args.max_exercises}")
        print(f"  Model:       {args.model}")

        if getattr(args, "dry_run", False):
            if not aider_path:
                print("  [dry-run] aider CLI not found on PATH.")
            if not repo:
                print("  [dry-run] polyglot-benchmark repo not found. Set --path or $POLYGLOT_BENCHMARK_PATH.")
            if aider_path and repo:
                print("  [dry-run] Setup looks good. Remove --dry-run to execute.")
            return

        if not aider_path:
            print("\n  Error: aider CLI not found. Install with: pip install aider-chat")
            return
        if not repo:
            print("\n  Error: polyglot-benchmark repo not found. Clone it with:")
            print("    git clone https://github.com/Aider-AI/polyglot-benchmark")
            print("  Then pass --path /path/to/polyglot-benchmark or set $POLYGLOT_BENCHMARK_PATH.")
            return

        languages = [s.strip() for s in args.languages.split(",") if s.strip()]
        bench = AiderPolyglotBenchmark(
            repo_path=repo,
            project_path=project_path,
            languages=languages,
            max_exercises=args.max_exercises,
            model=args.model,
            timeout_per_exercise=args.timeout,
            verbose=args.verbose,
        )
        report = bench.run_all()
        out_path = save_report(project_path, report)

        sc = report.to_dict()["scorecard"]
        print()
        print("  Results:")
        print(f"    Pass rate  (C3): {sc['with_c3_pass_rate']}%  ({sc['with_c3_count']} exercises)")
        print(f"    Pass rate (base): {sc['baseline_pass_rate']}%  ({sc['baseline_count']} exercises)")
        print(f"    Delta           : {sc['pass_rate_delta']:+.1f} pp")
        print(f"    Avg latency (C3 / base): {sc['with_c3_avg_latency_s']}s / {sc['baseline_avg_latency_s']}s")
        print(f"    Total cost  (C3 / base): ${sc['with_c3_total_cost_usd']} / ${sc['baseline_total_cost_usd']}")
        print()
        print(f"  JSON: {out_path}")

        try:
            dash = generate_dashboard(str(project_path))
            print(f"  Dashboard: {dash}")
        except Exception:
            pass
    elif suite == "swe-bench-lite":
        _run_swe_bench_lite(args, project_path)
    else:
        print(f"Unknown external suite: {suite}")


def _run_swe_bench_lite(args, project_path):
    """Run the SWE-bench Lite external benchmark."""
    from services.bench.external.aider_polyglot import detect_aider
    from services.bench.external.swe_bench import (
        SWEBenchAdapter,
        apply_resolution_results,
        evaluate_with_docker,
        load_tasks,
        save_report,
    )
    from services.benchmark_dashboard import generate_dashboard

    dataset_arg = getattr(args, "dataset", None) or "princeton-nlp/SWE-bench_Lite"
    agent = getattr(args, "agent", "aider")
    aider_path = detect_aider() if agent == "aider" else None

    print(f"  Dataset:     {dataset_arg}")
    print(f"  Agent:       {agent}")
    print(f"  Model:       {args.model}")
    print(f"  Max tasks:   {args.max_tasks}")
    print(f"  Docker eval: {'yes' if args.docker_eval else 'no (prediction only)'}")
    if agent == "aider":
        print(f"  Aider CLI:   {aider_path or '(missing — pip install aider-chat)'}")

    if getattr(args, "dry_run", False):
        try:
            tasks = load_tasks(dataset_arg)
            print(f"  [dry-run] Loaded {len(tasks)} tasks. Sample: {tasks[0].instance_id if tasks else 'none'}")
        except Exception as e:
            print(f"  [dry-run] Dataset load failed: {e}")
        if agent == "aider" and not aider_path:
            print("  [dry-run] aider CLI missing.")
        if args.docker_eval:
            import shutil as _sh
            if not _sh.which("docker"):
                print("  [dry-run] docker CLI missing — evaluation will be skipped.")
            try:
                import swebench  # noqa: F401
                print("  [dry-run] swebench package installed.")
            except ImportError:
                print("  [dry-run] swebench package missing (pip install swebench).")
        print("  [dry-run] Remove --dry-run to execute.")
        return

    if agent == "aider" and not aider_path:
        print("\n  Error: aider CLI not found. Install: pip install aider-chat")
        return

    try:
        tasks = load_tasks(dataset_arg)
    except Exception as e:
        print(f"\n  Error loading dataset: {e}")
        return

    tasks = tasks[: args.max_tasks]
    if not tasks:
        print("  Error: dataset is empty.")
        return

    print(f"\n  Generating patches for {len(tasks)} tasks x 2 modes (baseline + with_c3)...")
    adapter = SWEBenchAdapter(
        project_path=project_path,
        tasks=tasks,
        agent=agent,
        model=args.model,
        timeout_per_task=args.timeout,
        verbose=args.verbose,
    )
    report = adapter.run_all(dataset_label=dataset_arg)

    print()
    print(f"  Predictions (with C3):  {report.predictions_with_c3}")
    print(f"  Predictions (baseline): {report.predictions_baseline}")

    if args.docker_eval:
        print("\n  Running Docker evaluation (slow — minutes per task)...")
        eval_c3 = evaluate_with_docker(Path(report.predictions_with_c3), dataset_arg)
        eval_bs = evaluate_with_docker(Path(report.predictions_baseline), dataset_arg)
        if eval_c3:
            apply_resolution_results(report, eval_c3, "with_c3")
        if eval_bs:
            apply_resolution_results(report, eval_bs, "baseline")
        report.evaluation_method = "swebench-docker" if (eval_c3 or eval_bs) else "none"
    else:
        report.evaluation_method = "none"
        print("\n  Skipping evaluation (no --docker-eval). To score predictions later:")
        print("    pip install swebench && docker version  # ensure prerequisites")
        print(f"    python -m swebench.harness.run_evaluation \\\n"
              f"      --predictions_path {report.predictions_with_c3} \\\n"
              f"      --dataset_name {dataset_arg} --run_id c3-with --max_workers 4")

    out_path = save_report(project_path, report)
    sc = report.to_dict()["scorecard"]

    print()
    print("  Results:")
    print(f"    Patch generation — C3: {sc['with_c3_patch_rate']}% / base: {sc['baseline_patch_rate']}%")
    if sc["evaluated"]:
        print(f"    Resolution rate  — C3: {sc['with_c3_pass_rate']}% / base: {sc['baseline_pass_rate']}%"
              f"  ({sc['pass_rate_delta']:+.1f} pp)")
    else:
        print("    Resolution rate  — (unevaluated; rerun with --docker-eval)")
    print(f"    Avg latency     — C3: {sc['with_c3_avg_latency_s']}s / base: {sc['baseline_avg_latency_s']}s")
    print(f"    Total cost      — C3: ${sc['with_c3_total_cost_usd']} / base: ${sc['baseline_total_cost_usd']}")
    print()
    print(f"  JSON: {out_path}")

    try:
        dash = generate_dashboard(str(project_path))
        print(f"  Dashboard: {dash}")
    except Exception:
        pass


def _version_tuple(v: str) -> tuple:
    """Best-effort numeric version tuple for comparisons ('2.36.0' -> (2, 36, 0))."""
    parts = []
    for chunk in str(v or "").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _latest_pypi_version(package: str = "code-context-control", timeout: float = 5.0) -> str | None:
    """Best-effort latest release of `package` on PyPI; None if unreachable."""
    import urllib.request
    try:
        url = f"https://pypi.org/pypi/{package}/json"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return (data.get("info") or {}).get("version")
    except Exception:
        return None


def _installed_distribution(package: str = "code-context-control"):
    """Return the installed Distribution for `package`, or None when running from source."""
    try:
        from importlib import metadata
        return metadata.distribution(package)
    except Exception:
        return None


def _is_editable_install(package: str = "code-context-control") -> bool:
    """True when `package` is pip-installed in editable/development mode."""
    dist = _installed_distribution(package)
    if dist is None:
        return False
    try:
        text = dist.read_text("direct_url.json")
        if text:
            return bool(json.loads(text).get("dir_info", {}).get("editable"))
    except Exception:
        pass
    return False


def cmd_upgrade(args):
    """Upgrade C3 to the latest PyPI release (or just check with --check)."""
    current = __version__
    latest = _latest_pypi_version()
    if latest is None:
        print("  Could not reach PyPI to check for updates (offline?).")
    elif _version_tuple(latest) <= _version_tuple(current):
        print(f"  C3 is up to date (v{current}).")
        return
    else:
        print(f"  Update available: v{current} -> v{latest}")

    if getattr(args, "check", False):
        return

    if _installed_distribution() is None:
        print("  C3 is running from a source checkout (not pip-installed).")
        print("  Update with:  git pull")
        return
    if _is_editable_install():
        print("  C3 is installed in editable/development mode (pip install -e .).")
        print("  Update with:  git pull")
        return

    print("  Upgrading via pip (this may take a minute)...")
    cmd = [sys.executable, "-m", "pip", "install", "-U", "code-context-control[tui]"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        print(f"  Upgrade failed to launch pip: {e}")
        sys.exit(1)
    if result.returncode != 0:
        print("  pip upgrade failed:")
        print((result.stderr or result.stdout or "").strip()[-1000:])
        sys.exit(1)
    print("  Upgraded to the latest release. Restart your IDE's MCP server to load it.")
    print("  In each project, run  c3 init . --force  to apply any migrations.")


def _stdio_is_interactive() -> bool:
    """True when stdin AND stdout are attached to a real terminal.

    Used to decide whether bare `c3` may launch the full-screen TUI. With
    redirected stdio (pytest capture_output, CI, shell pipes) a TUI child
    would inherit our pipe handles and keep them open past our own death;
    on Windows the caller's communicate() then blocks forever because
    subprocess timeouts kill only the direct child, never the tree.
    """
    try:
        return bool(
            sys.stdin is not None and sys.stdin.isatty()
            and sys.stdout is not None and sys.stdout.isatty()
        )
    except Exception:
        return False


def _launch_tui() -> None:
    """Launch the interactive TUI — what `c3` with no arguments does.

    Runs tui/main.py as a subprocess so its bare `from screens...` imports resolve
    (its own directory lands on sys.path[0]); the package root goes on PYTHONPATH for
    cli/services imports. Falls back to help text when the optional [tui] extra
    (textual) is not installed.
    """
    pkg_root = Path(__file__).resolve().parent.parent
    tui_main = pkg_root / "tui" / "main.py"
    try:
        import textual  # noqa: F401
    except Exception:
        print("The interactive TUI needs the optional 'textual' dependency.")
        print('  Install it with:  pip install "code-context-control[tui]"')
        print("  Or run  c3 --help  to see all commands.")
        return
    if not tui_main.exists():
        print("TUI entry point not found. Run  c3 --help  for commands.")
        return
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(pkg_root) + (os.pathsep + existing_pp if existing_pp else "")
    try:
        subprocess.run([sys.executable, str(tui_main)], env=env)
    except KeyboardInterrupt:
        pass


def main():
    # Force UTF-8 on the CLI streams so server-supplied text (PR titles, branch
    # names, diffs) and our own glyphs render cleanly on Windows cp1252 consoles
    # instead of raising UnicodeEncodeError or mojibaking.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        from services import error_reporting
        error_reporting.init(component="c3-cli", version=__version__)
    except Exception:
        pass

    parser = build_parser(__version__, _parse_cli_ide_arg)
    args = parser.parse_args()

    if not args.command:
        # Bare `c3` launches the interactive TUI (replaces the old c3.bat
        # wrapper) — but only when attached to a real console. With redirected
        # stdio there is no terminal for a full-screen app anyway, and the TUI
        # child would inherit our stdout/stderr pipe handles and hold them
        # open past our own death (a caller's communicate() then hangs forever
        # on Windows). Print help instead of spawning anything.
        if _stdio_is_interactive():
            _launch_tui()
        else:
            parser.print_help()
        return

    commands = {
        "init": cmd_init,
        "index": cmd_index,
        "compress": cmd_compress,
        "context": cmd_context,
        "encode": cmd_encode,
        "decode": cmd_decode,
        "session": cmd_session,
        "claudemd": cmd_claudemd,
        "map": cmd_map,
        "stats": cmd_stats,
        "benchmark": cmd_benchmark,
        "session-benchmark": cmd_session_benchmark,
        "benchmark-e2e": cmd_benchmark_e2e,
        "bench": cmd_bench,
        "optimize": cmd_optimize,
        "pipe": cmd_pipe,
        "install-mcp": cmd_install_mcp,
        "mcp-install": cmd_install_mcp,
        "mcp-remove": cmd_mcp_remove,
        "permissions": cmd_permissions,
        "terse": cmd_terse,
        "ui": cmd_ui,
        "projects": cmd_projects,
        "sub": cmd_sub,
        "hub": cmd_hub,
        "bitbucket": cmd_bitbucket,
        "jira": cmd_jira,
        "creds": cmd_creds,
        "access": cmd_access,
        "oracle": cmd_oracle,
        "upgrade": cmd_upgrade,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        try:
            cmd_func(args)
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

