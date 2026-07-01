#!/usr/bin/env python3
"""PostToolUse/AfterTool hook for mcp__c3__c3_read.

After c3_read completes on a code/config file, records a sticky unlock so
the enforcement hook allows future native Edit calls on those files.
Unlocks land in the consolidated .c3/enforcement_state.json via the shared
state layer in cli/_hook_utils.py.

Directs the model to use c3_edit (preferred) or native Edit (unlocked).

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
    ".sh", ".bat", ".ps1",
}

# Legacy plain-text unlock list — informational only (no hook reads it).
UNLOCK_FILE = ".c3/unlocked_files.txt"


def _record_unlocks(editable: list[str], base: Path, session_id: str = ""):
    """Record file paths as unlocked for the enforcement hook."""
    unlock_path = base / UNLOCK_FILE
    try:
        existing = set(
            line.strip() for line in
            unlock_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ) if unlock_path.exists() else set()
        for fp in editable:
            existing.add(str(Path(fp).resolve()))
        unlock_path.parent.mkdir(parents=True, exist_ok=True)
        unlock_path.write_text(
            "\n".join(sorted(existing)) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    # The unlock the enforcer actually consults: consolidated state file.
    record_json_unlocks(editable, project_path=base, session_id=session_id)


def run(payload: dict, project_path: Path | None = None) -> dict | None:
    """Core logic — importable by the dispatcher and tests."""
    if payload.get("tool_name") != "mcp__c3__c3_read":
        return None

    tool_input = payload.get("tool_input", {}) or {}
    file_path = (tool_input.get("file_path") or "").strip()
    if not file_path:
        return None

    base = project_path if project_path is not None else Path.cwd()

    # Support comma-separated multi-file reads
    paths = [p.strip() for p in file_path.split(",") if p.strip()]
    editable = [p for p in paths if Path(p).suffix.lower() in EDITABLE_EXTS]
    if not editable:
        return None

    # Record sticky unlocks so Edit is allowed without Read(limit=1)
    _record_unlocks(editable, base, session_id=str(payload.get("session_id") or ""))

    files_str = ", ".join(f'"{p}"' for p in editable)
    return {
        "additionalContext": (
            f"[c3:edit-ready] {len(editable)} file(s) unlocked for editing: {files_str}. "
            f"Use c3_edit(file_path=..., old_string=..., new_string=..., summary=...) — preferred. "
            f"Native Edit is also unlocked for these files."
        )
    }


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        # Detect IDE format: Gemini wraps tool_response in a dict
        is_gemini = isinstance(data.get("tool_response", ""), dict)

        output = run(data)
        if output and output.get("additionalContext"):
            emit_additional_context(output["additionalContext"], is_gemini)
    except Exception as _e:
        log_hook_error("hook_c3read", _e)


if __name__ == "__main__":
    main()
