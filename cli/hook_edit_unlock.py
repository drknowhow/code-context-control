#!/usr/bin/env python3
"""PostToolUse hook for c3_read, c3_compress, and c3_agent.

Tracks which editable files have been explored via C3 tools but not yet
natively Read. Emits a batched nudge so the model can unlock Edit for all
pending files in one message with parallel Read(limit=1) calls.

Supports both Claude Code (PostToolUse) and Gemini CLI (AfterTool).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import emit_additional_context, log_hook_error  # noqa: E402

EDITABLE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".c", ".cpp", ".h", ".cs", ".html", ".css",
    ".json", ".yaml", ".yml", ".toml", ".sql", ".md", ".txt",
    ".sh", ".bat", ".ps1", ".r",
}

HANDLED_TOOLS = {
    "mcp__c3__c3_read",
    "mcp__c3__c3_compress",
    "mcp__c3__c3_agent",
}

PENDING_FILE = ".c3/edit_unlock_pending.txt"


def _get_pending_path() -> Path:
    return Path.cwd() / PENDING_FILE


def _load_pending() -> set:
    p = _get_pending_path()
    if not p.exists():
        return set()
    try:
        return set(line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return set()


def _save_pending(paths: set):
    p = _get_pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text("\n".join(sorted(paths)) + "\n" if paths else "", encoding="utf-8")
    except Exception:
        pass


def _extract_files_from_tool(tool_name: str, tool_input: dict, tool_response: str) -> list:
    """Extract file paths that were touched by the tool call."""
    files = []

    if tool_name in ("mcp__c3__c3_read", "mcp__c3__c3_compress"):
        file_path = (tool_input.get("file_path") or "").strip()
        if file_path:
            # Support comma-separated paths
            files.extend(p.strip() for p in file_path.split(",") if p.strip())

    elif tool_name == "mcp__c3__c3_agent":
        # Extract scope (comma-separated file paths) from agent workflows
        scope = (tool_input.get("scope") or "").strip()
        if scope:
            candidates = [p.strip() for p in scope.split(",") if p.strip()]
            # Only include if they look like file paths
            files.extend(p for p in candidates if "." in p)

        # Also parse file paths from the response for review_changes/investigate
        if isinstance(tool_response, str):
            for line in tool_response.split("\n"):
                line = line.strip()
                # Match "## path/to/file.py" from compress output
                if line.startswith("## ") and "." in line:
                    candidate = line[3:].split(" ")[0].strip()
                    if Path(candidate).suffix.lower() in EDITABLE_EXTS:
                        files.append(candidate)

    return files


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        tool_name = data.get("tool_name", "")

        if tool_name not in HANDLED_TOOLS:
            return

        # Detect IDE format
        is_gemini = isinstance(data.get("tool_response", ""), dict)

        tool_input = data.get("tool_input", {})
        tool_response = data.get("tool_response", "")
        if isinstance(tool_response, dict):
            tool_response = str(tool_response.get("llmContent", ""))

        # Extract file paths touched by this tool
        touched = _extract_files_from_tool(tool_name, tool_input, tool_response)

        # Filter to editable extensions only
        editable = [p for p in touched if Path(p).suffix.lower() in EDITABLE_EXTS]
        if not editable:
            return

        # Load existing pending set and add new files
        pending = _load_pending()
        new_files = [p for p in editable if p not in pending]
        if not new_files:
            # All files already pending — skip duplicate nudge
            return

        pending.update(new_files)
        _save_pending(pending)

        # Record sticky unlocks so enforcement hook allows future native
        # tool calls on these files without requiring a fresh c3_* call
        unlock_path = Path.cwd() / ".c3" / "unlocked_files.txt"
        try:
            existing = set(
                line.strip() for line in
                unlock_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ) if unlock_path.exists() else set()
            for fp in editable:
                resolved = str(Path(fp).resolve())
                existing.add(resolved)
            unlock_path.parent.mkdir(parents=True, exist_ok=True)
            unlock_path.write_text(
                "\n".join(sorted(existing)) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

        # Emit batched nudge with all pending files
        # Prefer c3_edit (no unlock needed). Native Edit is also unlocked via sticky file set.
        if len(pending) == 1:
            fp = next(iter(pending))
            msg = (
                f'[c3:edit-ready] "{fp}" unlocked for editing. '
                f'Use c3_edit(file_path="{fp}", old_string=..., new_string=...) — preferred. '
                f'Native Edit also unlocked (Read(limit=1) first if Claude Code requires it).'
            )
        else:
            files_list = ", ".join(f'"{p}"' for p in sorted(pending))
            msg = (
                f"[c3:edit-ready] {len(pending)} files unlocked for editing: {files_list}. "
                f"Use c3_edit(file_path=...) for each — preferred. "
                f"Native Edit also unlocked (Read(limit=1) first if Claude Code requires it)."
            )

        emit_additional_context(msg, is_gemini)
    except Exception as _e:
        log_hook_error("hook_edit_unlock", _e)


if __name__ == "__main__":
    main()
