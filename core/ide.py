"""IDE Profile Registry for cross-IDE MCP support.

Defines profiles for each supported IDE with their config paths, capabilities,
and instructions file locations. Enables graceful degradation of Claude-specific
features in non-Claude IDEs.
"""
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class IDEProfile:
    """Describes an IDE's MCP configuration requirements and capabilities."""
    name: str                              # "claude-code", "vscode", "cursor", "codex"
    display_name: str                      # "Claude Code", "VS Code Copilot"
    config_path: str                       # ".mcp.json", ".vscode/mcp.json"
    config_key: str                        # "mcpServers", "servers", or "mcp_servers"
    needs_type_field: bool                 # VS Code requires "type": "stdio"
    instructions_file: Optional[str]       # "CLAUDE.md", ".github/copilot-instructions.md"
    instructions_line_limit: Optional[int] # 200 for Claude, None for others
    supports_hooks: bool                   # Only Claude Code
    supports_transcripts: bool             # Only Claude Code (reads ~/.claude/)
    supports_clear: bool                   # Only Claude Code has /clear
    settings_path: Optional[str]           # ".claude/settings.local.json" or None
    config_format: str = "json"            # "json" or "toml"
    config_path_global: bool = False       # True = config_path resolved from home dir, not project
    hook_event: str = "PostToolUse"        # Claude="PostToolUse", Gemini="AfterTool"


# ─── IDE Profiles ─────────────────────────────────────────

PROFILES = {
    "claude-code": IDEProfile(
        name="claude-code",
        display_name="Claude Code",
        config_path=".mcp.json",
        config_key="mcpServers",
        needs_type_field=False,
        instructions_file="CLAUDE.md",
        instructions_line_limit=200,
        supports_hooks=True,
        supports_transcripts=True,
        supports_clear=True,
        settings_path=".claude/settings.local.json",
    ),
    "vscode": IDEProfile(
        name="vscode",
        display_name="VS Code Copilot",
        config_path=".vscode/mcp.json",
        config_key="servers",
        needs_type_field=True,
        instructions_file=".github/copilot-instructions.md",
        instructions_line_limit=None,
        supports_hooks=False,
        supports_transcripts=False,
        supports_clear=False,
        settings_path=None,
    ),
    "cursor": IDEProfile(
        name="cursor",
        display_name="Cursor",
        config_path=".cursor/mcp.json",
        config_key="mcpServers",
        needs_type_field=False,
        instructions_file=".cursorrules",
        instructions_line_limit=None,
        supports_hooks=False,
        supports_transcripts=False,
        supports_clear=False,
        settings_path=None,
    ),
    "codex": IDEProfile(
        name="codex",
        display_name="OpenAI Codex",
        config_path=".codex/config.toml",  # project-scoped; global is ~/.codex/config.toml
        config_key="mcp_servers",           # TOML: [mcp_servers.<name>]
        needs_type_field=False,
        instructions_file="AGENTS.md",
        instructions_line_limit=None,
        supports_hooks=False,
        supports_transcripts=False,
        supports_clear=False,
        settings_path=None,
        config_format="toml",
    ),
    "gemini": IDEProfile(
        name="gemini",
        display_name="Gemini CLI",
        config_path=".gemini/settings.json",  # project-scoped; global is ~/.gemini/settings.json
        config_key="mcpServers",
        needs_type_field=False,
        instructions_file="GEMINI.md",
        instructions_line_limit=None,
        supports_hooks=True,
        supports_transcripts=False,
        supports_clear=False,
        settings_path=".gemini/settings.json",  # same file as MCP config
        hook_event="AfterTool",
    ),
    "antigravity": IDEProfile(
        name="antigravity",
        display_name="Google Antigravity",
        config_path=".gemini/antigravity/mcp_config.json",  # resolved from home dir
        config_key="mcpServers",
        needs_type_field=False,
        instructions_file="GEMINI.md",
        instructions_line_limit=None,
        supports_hooks=False,
        supports_transcripts=False,
        supports_clear=False,
        settings_path=None,
        config_path_global=True,  # ~/.gemini/antigravity/mcp_config.json
    ),
}


IDE_ALIASES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
}


def normalize_ide_name(ide_name: str | None) -> str:
    """Normalize external IDE names and aliases to canonical profile keys."""
    raw = (ide_name or "").strip().lower()
    if not raw:
        return "claude-code"
    return IDE_ALIASES.get(raw, raw)


def get_profile(ide_name: str) -> IDEProfile:
    """Get IDE profile by name. Returns claude-code profile if unknown."""
    return PROFILES.get(normalize_ide_name(ide_name), PROFILES["claude-code"])


def detect_ide(project_path: str) -> str:
    """Auto-detect IDE from project directory markers.
    Prioritizes explicit IDE folders over general .mcp.json to avoid mis-detection.
    """
    p = Path(project_path)

    # 1. Direct config matches (strongest)
    if (p / ".vscode" / "mcp.json").exists():
        return "vscode"
    if (p / ".cursor" / "mcp.json").exists():
        return "cursor"
    if (p / ".codex" / "config.toml").exists():
        return "codex"
    if (p / ".gemini" / "settings.json").exists():
        # Prefer antigravity if its user-global dir already exists
        if (Path.home() / ".gemini" / "antigravity").is_dir():
            return "antigravity"
        return "gemini"

    # 2. IDE directory markers (even without config yet)
    if (p / ".codex").is_dir():
        return "codex"
    if (p / ".gemini").is_dir():
        if (Path.home() / ".gemini" / "antigravity").is_dir():
            return "antigravity"
        return "gemini"
    if (p / ".vscode").is_dir():
        return "vscode"
    if (p / ".cursor").is_dir():
        return "cursor"

    # 3. Fallback to Claude markers
    if (p / ".claude").is_dir() or (p / ".mcp.json").exists():
        return "claude-code"

    return "claude-code"


def load_ide_config(project_path: str) -> str:
    """Read 'ide' from .c3/config.json. Returns 'claude-code' if not set."""
    config_file = Path(project_path) / ".c3" / "config.json"
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
            ide = normalize_ide_name(data.get("ide", "claude-code"))
            if ide in PROFILES:
                return ide
        except Exception:
            pass
    return "claude-code"
