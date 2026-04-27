#!/usr/bin/env python3
"""PostToolUse/AfterTool hook for mcp__c3__c3_read.

After c3_read completes on a code/config file, records a sticky unlock so
the enforcement hook allows future native Edit calls on those files.

Directs the model to use c3_edit (preferred) or native Edit (unlocked).

Supports both Claude Code (PostToolUse) and Gemini CLI (AfterTool).
"""

import json
import sys
from pathlib import Path

_JSON_UNLOCK_FILE = ".c3/unlocked_files.json"

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import emit_additional_context, log_hook_error  # noqa: E402

EDITABLE_EXTS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".c", ".cpp", ".h", ".cs", ".html", ".css",
    ".json", ".yaml", ".yml", ".toml", ".sql", ".md", ".txt",
    ".sh", ".bat", ".ps1",
}

UNLOCK_FILE = ".c3/unlocked_files.txt"


def _record_unlocks(editable: list[str]):
    """Record file paths as unlocked for the enforcement hook."""
    unlock_path = Path.cwd() / UNLOCK_FILE
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
    # Fix 2: also write to unlocked_files.json (read by hook_pretool_enforce.py)
    _record_json_unlocks(editable)


def _record_json_unlocks(editable: list[str]):
    """Sync c3_read unlocks into unlocked_files.json for hook_pretool_enforce.py."""
    json_path = Path.cwd() / _JSON_UNLOCK_FILE
    try:
        existing: dict = {}
        if json_path.exists():
            try:
                existing = json.loads(json_path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        for fp in editable:
            normalized = str(Path(fp).resolve())
            cats = set(existing.get(normalized, []))
            cats.update({"read", "edit"})
            existing[normalized] = sorted(cats)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(existing), encoding="utf-8")
    except Exception:
        pass


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        if data.get("tool_name") != "mcp__c3__c3_read":
            return

        # Detect IDE format: Gemini wraps tool_response in a dict
        is_gemini = isinstance(data.get("tool_response", ""), dict)

        tool_input = data.get("tool_input", {})
        file_path = (tool_input.get("file_path") or "").strip()
        if not file_path:
            return

        # Support comma-separated multi-file reads
        paths = [p.strip() for p in file_path.split(",") if p.strip()]
        editable = [p for p in paths if Path(p).suffix.lower() in EDITABLE_EXTS]
        if not editable:
            return

        # Record sticky unlocks so Edit is allowed without Read(limit=1)
        _record_unlocks(editable)

        files_str = ", ".join(f'"{p}"' for p in editable)
        emit_additional_context(
            f"[c3:edit-ready] {len(editable)} file(s) unlocked for editing: {files_str}. "
            f"Use c3_edit(file_path=..., old_string=..., new_string=..., summary=...) — preferred. "
            f"Native Edit is also unlocked for these files.",
            is_gemini,
        )
    except Exception as _e:
        log_hook_error("hook_c3read", _e)


if __name__ == "__main__":
    main()
