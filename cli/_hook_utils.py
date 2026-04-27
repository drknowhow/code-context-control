"""Shared utilities for C3 hook scripts — supports Claude Code and Gemini CLI."""
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Max size of hook_errors.log before it is rotated (50 KB)
_LOG_MAX_BYTES = 50 * 1024


def log_hook_error(hook_name: str, exc: BaseException) -> None:
    """Append a timestamped error entry to .c3/hook_errors.log.

    Never raises — hook scripts must not crash the IDE even in the error logger.
    Rotates the log (renames to hook_errors.log.bak) when it exceeds 50 KB.
    """
    try:
        c3_dir = Path.cwd() / ".c3"
        if not c3_dir.exists():
            return
        log_file = c3_dir / "hook_errors.log"
        # Rotate if too large
        try:
            if log_file.exists() and log_file.stat().st_size > _LOG_MAX_BYTES:
                log_file.replace(c3_dir / "hook_errors.log.bak")
        except Exception:
            pass
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tb = traceback.format_exc().strip()
        line = f"[{ts}] [{hook_name}] {type(exc).__name__}: {exc}\n{tb}\n---\n"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # Absolutely must not propagate

# Map Gemini CLI built-in tool names → canonical Claude Code equivalents
GEMINI_TOOL_MAP = {
    "run_shell_command": "Bash",
    "read_file": "Read",
    "edit_file": "Edit",
    "write_file": "Write",
    "list_directory": "FindFiles",
    "find_files": "FindFiles",
    "grep": "SearchText",
    "search_in_files_content": "SearchText",
    "find_in_files": "SearchText",
}


def normalize_tool_name(tool_name: str) -> str:
    """Normalize Gemini CLI tool names to their Claude Code equivalents."""
    return GEMINI_TOOL_MAP.get(tool_name, tool_name)


def get_tool_output(data: dict) -> tuple:
    """Extract the output text and detect IDE format from hook stdin data.

    Returns (output_text: str, is_gemini: bool).
    Claude passes tool_response as a plain string.
    Gemini wraps it in {llmContent, returnDisplay}.
    """
    resp = data.get("tool_response", "")
    if isinstance(resp, dict):
        content = resp.get("llmContent", "") or resp.get("returnDisplay", "")
        if isinstance(content, list):
            # llmContent can be a list of content-part dicts like {text: "..."}
            content = "\n".join(
                p.get("text", str(p)) if isinstance(p, dict) else str(p)
                for p in content
            )
        return str(content) if content is not None else "", True
    return resp if isinstance(resp, str) else "", False


def get_tool_input_path(data: dict) -> str:
    """Extract file path from tool_input, handling both Claude (file_path) and Gemini (path)."""
    tool_input = data.get("tool_input", {})
    return tool_input.get("file_path", "") or tool_input.get("path", "")


def emit_additional_context(text: str, is_gemini: bool) -> None:
    """Write additionalContext JSON to stdout in the correct format for the IDE."""
    if is_gemini:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"additionalContext": text}}))
    else:
        sys.stdout.write(json.dumps({"additionalContext": text}))


def emit_filtered_output(filtered: str, is_gemini: bool) -> None:
    """Write filtered tool output to stdout.

    Claude Code: replaces the tool result entirely via tool_result.
    Gemini CLI: no direct replacement — appends as additionalContext instead.
    """
    if is_gemini:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"additionalContext": filtered}}))
    else:
        sys.stdout.write(json.dumps({"tool_result": filtered}))
