#!/usr/bin/env python3
"""PostToolUse hook for c3_compress and c3_agent (c3_read has its own hook).

Tracks which editable files have been explored via C3 tools but not yet
natively Read. Emits a batched nudge so the model can unlock Edit for all
pending files in one message with parallel Read(limit=1) calls.

Sticky unlocks land in the consolidated .c3/enforcement_state.json via the
shared state layer in cli/_hook_utils.py.

Supports both Claude Code (PostToolUse) and Gemini CLI (AfterTool).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import (  # noqa: E402
    emit_additional_context,
    log_hook_error,
    record_json_unlocks,
)

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


def _load_pending(base: Path) -> set:
    p = base / PENDING_FILE
    if not p.exists():
        return set()
    try:
        return set(line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:
        return set()


def _save_pending(base: Path, paths: set):
    p = base / PENDING_FILE
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


def run(payload: dict, project_path: Path | None = None) -> dict | None:
    """Core logic — importable by the dispatcher and tests."""
    tool_name = payload.get("tool_name", "")

    if tool_name not in HANDLED_TOOLS:
        return None

    base = project_path if project_path is not None else Path.cwd()

    tool_input = payload.get("tool_input", {}) or {}
    tool_response = payload.get("tool_response", "")
    if isinstance(tool_response, dict):
        tool_response = str(tool_response.get("llmContent", ""))

    # Extract file paths touched by this tool
    touched = _extract_files_from_tool(tool_name, tool_input, tool_response)

    # Filter to editable extensions only
    editable = [p for p in touched if Path(p).suffix.lower() in EDITABLE_EXTS]
    if not editable:
        return None

    # Load existing pending set and add new files
    pending = _load_pending(base)
    new_files = [p for p in editable if p not in pending]
    if not new_files:
        # All files already pending — skip duplicate nudge
        return None

    pending.update(new_files)
    _save_pending(base, pending)

    # Record sticky unlocks (legacy .txt list kept for one release — no hook
    # reads it; the enforcer consults the consolidated state file below)
    unlock_path = base / ".c3" / "unlocked_files.txt"
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

    # The unlock map hook_pretool_enforce.py actually consults —
    # consolidated .c3/enforcement_state.json via the shared state layer.
    record_json_unlocks(
        editable, project_path=base,
        session_id=str(payload.get("session_id") or ""),
    )

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

    return {"additionalContext": msg}


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        # Detect IDE format
        is_gemini = isinstance(data.get("tool_response", ""), dict)

        output = run(data)
        if output and output.get("additionalContext"):
            emit_additional_context(output["additionalContext"], is_gemini)
    except Exception as _e:
        log_hook_error("hook_edit_unlock", _e)


if __name__ == "__main__":
    main()
