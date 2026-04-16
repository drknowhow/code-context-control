#!/usr/bin/env python3
"""PreToolUse hook: two-mode enforcement for native tools.

Read-class tools (Read/Grep/Glob/FindFiles/SearchText) are **ADVISORY** —
if no c3_* tool was used first, the call proceeds with a selection-time
hint injected via additionalContext. Drift is still cheap to recover from
for read-only operations.

Write-class tools (Edit/Write) are **BLOCKED** — file mutations must go
through c3_edit so the ledger captures every change. Hard-deny with
redirect message.

This replaces the previous all-blocking behavior. Rationale: blocking read
tools treats Claude adversarially and creates cliffs at every edge case
(new tool variants, Windows quirks). Advisory read + blocked write keeps
the ledger intact without strangling the model's own good judgment.

Supports both Claude Code and Gemini CLI via _hook_utils.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from _hook_utils import normalize_tool_name, log_hook_error

# How many activity-log lines to scan backwards
LOOKBACK = 20  # Fix 1: increased from 3 — activity log only has c3_* entries

# Signal file written by hook_c3_signal.py after any c3_* tool completes
_SIGNAL_FILE = ".c3/last_c3_call.json"
_SIGNAL_MAX_AGE_SECS = 600  # 10 minutes

# Session-sticky unlock file: tracks files accessed via c3_* tools, per-category
_UNLOCK_FILE = ".c3/unlocked_files.json"

# Which unlock category each native tool requires
_TOOL_CATEGORY = {
    "Read": "read", "Grep": "read", "Glob": "read",
    "FindFiles": "read", "SearchText": "read", "Edit": "edit",
    "Write": "edit",
}

# Which unlock category each c3 tool grants
_C3_GRANTS = {
    "c3_search": "read", "c3_compress": "read", "c3_read": "read",
    "c3_filter": "read", "c3_validate": "read", "c3_impact": "read",
    "c3_edit": "edit", "c3_edits": "edit", "c3_agent": "both",
    "c3_delegate": "read", "c3_session": "read", "c3_memory": "read",
    "c3_status": "read",
}

# c3 tools that satisfy the "used c3 first" requirement per native tool
_PREREQS = {
    "Read":       {"c3_search", "c3_compress", "c3_read", "c3_filter",
                   "c3_validate", "c3_impact", "c3_edit", "c3_agent", "c3_delegate"},
    "Grep":       {"c3_search", "c3_compress", "c3_filter", "c3_validate",
                   "c3_impact", "c3_agent", "c3_delegate"},
    "Glob":       {"c3_search", "c3_filter", "c3_agent", "c3_delegate"},
    "FindFiles":  {"c3_search", "c3_filter", "c3_agent", "c3_delegate"},
    "SearchText": {"c3_search", "c3_compress", "c3_read", "c3_filter",
                   "c3_impact", "c3_agent", "c3_delegate"},
    "Edit":       {"c3_edit", "c3_edits", "c3_agent"},
    "Write":      {"c3_edit", "c3_edits", "c3_agent"},
    "MultiEdit":  {"c3_edit", "c3_edits", "c3_agent"},
}

# Read-class tools: advisory (allow + nudge when no c3 used first).
# Write-class tools: blocked (ledger integrity).
_ADVISORY_TOOLS = {"Read", "Grep", "Glob", "FindFiles", "SearchText"}
_BLOCKED_TOOLS = {"Edit", "Write", "MultiEdit"}

# Redirect messages per native tool
_REDIRECTS = {
    "Read": (
        "Use c3_compress(file_path='...', mode='map') to map the file first, "
        "then c3_read(file_path='...', symbols=['...']) for surgical extraction."
    ),
    "Grep": (
        "Use c3_search(query='...', action='code') for pattern matching, "
        "or c3_search(query='...', action='semantic') for concept search."
    ),
    "Glob": (
        "Use c3_search(query='...', action='files') for file discovery."
    ),
    "FindFiles": (
        "Use c3_search(query='...', action='files') for file discovery."
    ),
    "SearchText": (
        "Use c3_search(query='...', action='code') for code search."
    ),
    "Edit": (
        "Use c3_edit(file_path='...', old_string='...', new_string='...', summary='...') "
        "for file edits — it reads, patches, writes, and logs in one step."
    ),
    "Write": (
        "Use c3_edit(file_path='...', old_string='...', new_string='...', summary='...') "
        "for file modifications. For new files, use native Write only after c3_search/c3_compress."
    ),
}


def _tail_lines(path: Path, n: int) -> list[str]:
    """Read last n lines of a file without loading the whole file.

    Activity logs grow to megabytes over a session; the enforcer only inspects
    the tail window, so reading the whole file on every native tool call was
    pure overhead.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            block = 4096
            chunks = []
            seen_newlines = 0
            pos = size
            while pos > 0 and seen_newlines <= n:
                read_size = min(block, pos)
                pos -= read_size
                f.seek(pos)
                data = f.read(read_size)
                seen_newlines += data.count(b"\n")
                chunks.append(data)
        blob = b"".join(reversed(chunks))
        text = blob.decode("utf-8", errors="replace")
        return text.splitlines()[-n:]
    except Exception:
        return []


def _load_unlocked(project_path: Path) -> dict:
    """Load per-category unlock map: {normalized_path: ["read"], ...}."""
    unlock_path = project_path / _UNLOCK_FILE
    if not unlock_path.exists():
        return {}
    try:
        data = json.loads(unlock_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_unlocked(project_path: Path, unlocked: dict):
    """Persist the per-category unlock map."""
    unlock_path = project_path / _UNLOCK_FILE
    unlock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        unlock_path.write_text(json.dumps(unlocked), encoding="utf-8")
    except Exception:
        pass


def _record_unlock(project_path: Path, file_path: str, category: str):
    """Add a file path to the sticky unlock map for the given category."""
    if not file_path or not category:
        return
    unlocked = _load_unlocked(project_path)
    normalized = str(Path(file_path).resolve()) if file_path else ""
    if not normalized:
        return
    cats = set(unlocked.get(normalized, []))
    if category == "both":
        cats.update({"read", "edit"})
    else:
        cats.add(category)
    unlocked[normalized] = sorted(cats)
    _save_unlocked(project_path, unlocked)


def _is_file_unlocked(project_path: Path, file_path: str, category: str) -> bool:
    """Check if a file is unlocked for the given operation category."""
    if not file_path:
        return False
    unlocked = _load_unlocked(project_path)
    normalized = str(Path(file_path).resolve()) if file_path else ""
    cats = unlocked.get(normalized, [])
    return category in cats or "both" in cats


def _check_signal_file(project_path: Path) -> tuple[bool, bool]:
    """Read last_c3_call.json written by hook_c3_signal.py.

    Returns (recent, read_unlocked):
      recent:        True if a c3_* tool completed within _SIGNAL_MAX_AGE_SECS
      read_unlocked: True if that tool was c3_search/c3_compress/c3_filter
    """
    signal_path = project_path / _SIGNAL_FILE
    if not signal_path.exists():
        return False, False
    try:
        data = json.loads(signal_path.read_text(encoding="utf-8"))
        ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        recent = age <= _SIGNAL_MAX_AGE_SECS
        return recent, bool(data.get("read_unlocked", False)) and recent
    except Exception:
        return False, False


def _check_c3_used(project_path: Path, tool_name: str, tool_input: dict) -> tuple[bool, str]:
    """Check if a qualifying c3 tool was recently used.

    Returns (allowed, via) where via is one of:
      'signal'   -- fresh c3_* signal file (within 10 min) — no reminder
      'unlock'   -- sticky file unlock only, no fresh signal — emit reminder
      'activity' -- activity log hit within last LOOKBACK entries — no reminder
      ''         -- not allowed
    """
    allowed = _PREREQS.get(tool_name, set())
    if not allowed:
        return True, "signal"  # No prereqs defined → allow without reminder

    native_target = (
        tool_input.get("file_path", "")
        or tool_input.get("path", "")
        or tool_input.get("pattern", "")
        or tool_input.get("query", "")
        or ""
    )
    required_cat = _TOOL_CATEGORY.get(tool_name, "read")

    # ── Fix 4: signal file — primary, fast, reliable ─────────────────────────
    signal_recent, signal_read_unlocked = _check_signal_file(project_path)
    if signal_recent:
        # Fix 5: Grep/Glob without file path needs a read-unlocking tool
        if not native_target and tool_name in ("Grep", "Glob", "FindFiles", "SearchText"):
            if signal_read_unlocked:
                return True, "signal"
            # Signal exists but not read-unlocking (e.g. c3_memory) — fall through
        else:
            if native_target:
                _record_unlock(project_path, native_target, required_cat)
            return True, "signal"

    # ── Sticky file unlock (per-file, persists across turns) ─────────────────
    if native_target and _is_file_unlocked(project_path, native_target, required_cat):
        return True, "unlock"  # allowed but no fresh signal — emit reminder

    # ── Fix 1: activity log scan (LOOKBACK increased to 20) ──────────────────
    log_file = project_path / ".c3" / "activity_log.jsonl"
    if not log_file.exists():
        return False, ""

    try:
        lines = _tail_lines(log_file, LOOKBACK)
    except Exception:
        return False, ""

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if entry.get("type") != "tool_call":
            continue

        tool = entry.get("tool", "")
        if tool not in allowed:
            continue

        if native_target:
            grant = _C3_GRANTS.get(tool, required_cat)
            _record_unlock(project_path, native_target, grant)
        return True, "activity"

    return False, ""


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        tool_name = normalize_tool_name(data.get("tool_name", ""))

        if tool_name not in _PREREQS:
            return  # Not a tool we enforce — pass through

        tool_input = data.get("tool_input", {})
        project_path = Path.cwd()

        allowed, via = _check_c3_used(project_path, tool_name, tool_input)

        if allowed:
            # Sticky-unlock only: gentle drift-guard nudge, still allow.
            if via == "unlock":
                print(json.dumps({
                    "additionalContext": (
                        f"[c3:drift-guard] {tool_name} allowed via sticky unlock "
                        f"— no recent c3_* call detected. "
                        f"Prefer c3_search/c3_compress to keep the ledger warm."
                    )
                }))
            return  # satisfied prereq — allow

        # No c3_* prereq met. Advisory vs blocked split.
        redirect = _REDIRECTS.get(tool_name, "Prefer a c3_* tool.")

        if tool_name in _ADVISORY_TOOLS:
            # Read-class: allow, but inject a selection-time hint.
            print(json.dumps({
                "additionalContext": (
                    f"[c3:hint] Native `{tool_name}` is running without a prior c3_* call. "
                    f"For better index awareness next time: {redirect}"
                )
            }))
            return

        # Write-class: hard block. Ledger integrity matters more than flexibility.
        reason = (
            f"[c3:enforce] Native `{tool_name}` is blocked to preserve the edit ledger. "
            f"{redirect}"
        )
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(response))

    except Exception as _e:
        log_hook_error("hook_pretool_enforce", _e)


if __name__ == "__main__":
    main()
