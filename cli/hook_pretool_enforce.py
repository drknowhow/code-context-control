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

State (v2.42+): reads/writes the consolidated .c3/enforcement_state.json via
cli/_hook_utils (single writer module, atomic writes, session-scoped). The
legacy last_c3_call.json / unlocked_files.json pair is still READ as a
fallback for one release; only the new file is written. State recorded by a
different Claude Code session is treated as stale — reads fall back to the
advisory path instead of granting stale unlocks.

Supports both Claude Code and Gemini CLI via _hook_utils.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from _hook_utils import (
    load_enforcement_state,
    log_hook_error,
    normalize_tool_name,
    record_unlocked_files,
)

# How many activity-log lines to scan backwards
LOOKBACK = 20  # Fix 1: increased from 3 — activity log only has c3_* entries

# Max age of the last_c3_call signal in the consolidated state
_SIGNAL_MAX_AGE_SECS = 600  # 10 minutes

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


def _record_unlock(project_path: Path, file_path: str, category: str, session_id: str = ""):
    """Add a file path to the sticky unlock map for the given category."""
    if not file_path or not category:
        return
    cats = {"read", "edit"} if category == "both" else {category}
    record_unlocked_files(
        [file_path], cats, session_id=session_id, project_path=project_path,
    )


def _is_file_unlocked(state: dict, file_path: str, category: str) -> bool:
    """Check if a file is unlocked for the given operation category."""
    if not file_path:
        return False
    normalized = str(Path(file_path).resolve()) if file_path else ""
    cats = state.get("unlocked_files", {}).get(normalized, [])
    return category in cats or "both" in cats


def _check_signal(state: dict) -> tuple[bool, bool, str]:
    """Inspect the last_c3_call section of the consolidated state.

    Returns (recent, read_unlocked, c3_tool):
      recent:        True if a c3_* tool completed within _SIGNAL_MAX_AGE_SECS
      read_unlocked: True if that tool was c3_search/c3_compress/c3_filter/…
      c3_tool:       short name of the c3 tool that wrote the signal (e.g.
                     "c3_edit"), or "" if recent is False / unparseable.

    Fails closed: on any parse error, returns (False, False, "").
    """
    last_call = state.get("last_c3_call")
    if not isinstance(last_call, dict):
        return False, False, ""
    try:
        ts = datetime.fromisoformat(last_call["ts"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > _SIGNAL_MAX_AGE_SECS:
            return False, False, ""
        return True, bool(last_call.get("read_unlocked", False)), str(last_call.get("tool", ""))
    except Exception:
        return False, False, ""


def _check_c3_used(
    project_path: Path,
    state: dict,
    tool_name: str,
    tool_input: dict,
    session_id: str = "",
) -> tuple[bool, str]:
    """Check if a qualifying c3 tool was recently used.

    Returns (allowed, via) where via is one of:
      'signal'   -- fresh c3_* signal (within 10 min) — no reminder
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

    # ── Fix 4: signal — primary, fast, reliable ──────────────────────────────
    signal_recent, signal_read_unlocked, signal_tool = _check_signal(state)
    if signal_recent:
        # Bypass fix: for write-class tools (Edit/Write/MultiEdit), the signal
        # may only unlock them when the c3 tool that wrote it actually satisfies
        # this tool's prereqs (e.g. c3_edit/c3_edits/c3_agent). A read-class
        # signal (c3_status, c3_search, …) must NOT unlock a native write.
        if tool_name in _BLOCKED_TOOLS:
            if signal_tool in allowed:
                if native_target:
                    _record_unlock(project_path, native_target, required_cat, session_id)
                return True, "signal"
            # Fresh signal exists but it's not a write-prereq tool — fall through
        # Fix 5: Grep/Glob without file path needs a read-unlocking tool
        elif not native_target and tool_name in ("Grep", "Glob", "FindFiles", "SearchText"):
            if signal_read_unlocked:
                return True, "signal"
            # Signal exists but not read-unlocking (e.g. c3_memory) — fall through
        else:
            if native_target:
                _record_unlock(project_path, native_target, required_cat, session_id)
            return True, "signal"

    # ── Sticky file unlock (per-file, persists across turns) ─────────────────
    if native_target and _is_file_unlocked(state, native_target, required_cat):
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
            _record_unlock(project_path, native_target, grant, session_id)
        return True, "activity"

    return False, ""


def run(payload: dict, project_path: Path | None = None) -> dict | None:
    """Core enforcement logic — importable by the dispatcher and tests.

    Returns a hook-output dict ({"additionalContext": ...} or a deny
    {"hookSpecificOutput": ...}) or None when the call passes silently.
    """
    tool_name = normalize_tool_name(payload.get("tool_name", ""))

    if tool_name not in _PREREQS:
        return None  # Not a tool we enforce — pass through

    tool_input = payload.get("tool_input", {}) or {}
    session_id = str(payload.get("session_id") or "")
    base = project_path if project_path is not None else Path.cwd()

    # Session-scoped load: state written by a different session comes back
    # empty, so stale unlocks degrade to the advisory path below.
    state = load_enforcement_state(base, session_id=session_id)

    allowed, via = _check_c3_used(base, state, tool_name, tool_input, session_id)

    if allowed:
        # Sticky-unlock only: gentle drift-guard nudge, still allow.
        if via == "unlock":
            return {
                "additionalContext": (
                    f"[c3:drift-guard] {tool_name} allowed via sticky unlock "
                    f"— no recent c3_* call detected. "
                    f"Prefer c3_search/c3_compress to keep the ledger warm."
                )
            }
        return None  # satisfied prereq — allow

    # No c3_* prereq met. Advisory vs blocked split.
    redirect = _REDIRECTS.get(tool_name, "Prefer a c3_* tool.")

    if tool_name in _ADVISORY_TOOLS:
        # Read-class: allow, but inject a selection-time hint.
        return {
            "additionalContext": (
                f"[c3:hint] Native `{tool_name}` is running without a prior c3_* call. "
                f"For better index awareness next time: {redirect}"
            )
        }

    # Write-class: hard block. Ledger integrity matters more than flexibility.
    reason = (
        f"[c3:enforce] Native `{tool_name}` is blocked to preserve the edit ledger. "
        f"{redirect}"
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        output = run(json.loads(raw))
        if output:
            print(json.dumps(output))

    except Exception as _e:
        log_hook_error("hook_pretool_enforce", _e)


if __name__ == "__main__":
    main()
