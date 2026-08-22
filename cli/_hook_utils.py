"""Shared utilities for C3 hook scripts — supports Claude Code and Gemini CLI.

Also owns the consolidated enforcement state (.c3/enforcement_state.json):
a single file replacing the previous trio of last_c3_call.json,
unlocked_files.json, and ad-hoc writers spread across four hook scripts.
All hook reads/writes of enforcement state MUST go through this module.
"""
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Max size of hook_errors.log before it is rotated (50 KB)
_LOG_MAX_BYTES = 50 * 1024

# ── Consolidated enforcement state ───────────────────────────────────────────
# Canonical file (the ONLY file written from v2.42 on):
#   {
#     "session_id": "<claude session id or ''>",
#     "last_c3_call": {"ts": "<ISO UTC>", "tool": "c3_search", "read_unlocked": true},
#     "unlocked_files": {"<resolved path>": ["read", "edit"]}
#   }
# Legacy files (READ as fallback for one release; never written anymore):
ENFORCEMENT_STATE_FILE = ".c3/enforcement_state.json"
LEGACY_SIGNAL_FILE = ".c3/last_c3_call.json"
LEGACY_UNLOCK_FILE = ".c3/unlocked_files.json"

# Critical state-layer warnings (e.g. corrupted state JSON) surfaced by the
# dispatcher as an additionalContext line so enforcement never silently stops
# enforcing. Drained via drain_state_warnings().
STATE_WARNINGS: list = []


def ensure_utf8_stdio() -> None:
    """Force UTF-8 on the hook process's stdio pipes.

    On Windows a hook subprocess gets cp1252 stdout/stdin (piped, non-TTY),
    so printing user-visible text containing box-drawing chars ("─") or emoji
    raises UnicodeEncodeError and surfaces as a "Stop hook error" in the IDE.
    Claude Code reads hook stdout as UTF-8. Call this at the top of every
    entrypoint that prints raw (non-json.dumps) text. Never raises.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    except Exception:
        pass


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

def drain_state_warnings() -> list:
    """Return and clear accumulated critical state warnings.

    Called by the dispatcher after each sub-hook so corruption events become
    a visible "[c3:hook-error] ..." additionalContext line instead of a
    silent enforcement gap.
    """
    warnings = STATE_WARNINGS[:]
    STATE_WARNINGS.clear()
    return warnings


def _empty_state(session_id: str = "") -> dict:
    return {"session_id": session_id or "", "last_c3_call": None, "unlocked_files": {}}


#: os.replace retries for when Windows briefly holds a handle on the target —
#: AV scanner, Search indexer, or a concurrent hook reading the same state.
_REPLACE_ATTEMPTS = 4
_REPLACE_BACKOFF_S = 0.02


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON durably: temp file in the same directory + os.replace.

    Three failure modes this guards against, all observed in the wild
    (10 orphaned .tmp<pid> files and a truncated enforcement_state.json on a
    Windows box before v2.66):

    1. **Torn write.** Without fsync, os.replace can publish a file whose
       bytes are still in the OS cache. A crash or power loss then leaves a
       zero-length or half-written JSON that every later read quarantines —
       which silently drops all sticky unlocks and makes enforcement *more*
       aggressive, not less.
    2. **Orphaned temp files.** If os.replace raised, the temp file was
       simply abandoned. They accumulate forever in .c3/ and are easy to
       mistake for real state.
    3. **Transient Windows sharing violations.** Another process holding a
       read handle on the target makes os.replace raise PermissionError; a
       single attempt turned that into a lost write.
    """
    import time

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())  # durability before publish (fixes #1)

        last_exc = None
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:  # fixes #3
                last_exc = exc
                if attempt < _REPLACE_ATTEMPTS - 1:
                    time.sleep(_REPLACE_BACKOFF_S * (2 ** attempt))
        if last_exc is not None:
            raise last_exc
    finally:
        # fixes #2 — a successful replace already consumed tmp, so this only
        # fires on the failure paths.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def sweep_stale_temps(project_path: Path | None = None) -> int:
    """Remove orphaned *.tmp<pid> files left by older C3 builds.

    Only touches temp files whose owning PID is gone, so a concurrent hook
    mid-write is never disturbed. Returns how many were removed; never raises.
    """
    removed = 0
    try:
        base = project_path if project_path is not None else Path.cwd()
        c3_dir = base / ".c3"
        if not c3_dir.is_dir():
            return 0
        for tmp in c3_dir.glob("*.tmp*"):
            suffix = tmp.name.rsplit(".tmp", 1)[-1]
            if not suffix.isdigit():
                continue
            if _pid_alive(int(suffix)):
                continue
            try:
                tmp.unlink()
                removed += 1
            except OSError:
                pass
    except Exception:
        pass
    return removed


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. On error assume ALIVE (don't delete)."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            # PROCESS_QUERY_LIMITED_INFORMATION — works without elevation.
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours
    except Exception:
        return True  # unknown → conservative


def _read_legacy_state(base: Path) -> dict:
    """Build a state view from the pre-v2.42 files (read-only fallback)."""
    state = _empty_state()
    signal_path = base / LEGACY_SIGNAL_FILE
    if signal_path.exists():
        try:
            data = json.loads(signal_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("timestamp"):
                state["last_c3_call"] = {
                    "ts": str(data.get("timestamp")),
                    "tool": str(data.get("tool", "")),
                    "read_unlocked": bool(data.get("read_unlocked", False)),
                }
        except Exception:
            pass  # Legacy file corruption is not critical — new file supersedes it
    unlock_path = base / LEGACY_UNLOCK_FILE
    if unlock_path.exists():
        try:
            data = json.loads(unlock_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state["unlocked_files"] = {
                    str(k): list(v) for k, v in data.items() if isinstance(v, list)
                }
        except Exception:
            pass
    return state


def load_enforcement_state(project_path: Path | None = None, session_id: str = "") -> dict:
    """Load consolidated enforcement state with legacy fallback + session scoping.

    - Missing new file → read legacy last_c3_call.json / unlocked_files.json
      (one-release migration path; writes only ever go to the new file).
    - Corrupted new file → quarantine to *.corrupt, log, push a critical
      warning to STATE_WARNINGS, and return empty state (fail-open to the
      advisory path, never a hard-deny surprise).
    - session_id mismatch → state written by another session is STALE:
      return empty state for the current session.
    """
    base = project_path if project_path is not None else Path.cwd()
    state_path = base / ENFORCEMENT_STATE_FILE
    state = None
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("enforcement_state.json root is not an object")
            state = _empty_state()
            state["session_id"] = str(data.get("session_id") or "")
            last_call = data.get("last_c3_call")
            state["last_c3_call"] = last_call if isinstance(last_call, dict) else None
            unlocked = data.get("unlocked_files")
            state["unlocked_files"] = unlocked if isinstance(unlocked, dict) else {}
        except Exception as exc:
            log_hook_error("enforcement_state", exc)
            try:
                state_path.replace(state_path.with_name(state_path.name + ".corrupt"))
            except Exception:
                pass
            # Corruption is the one reliable signal that a previous write went
            # wrong, so it is also the moment to clear any temp files that
            # write orphaned (pre-v2.66 _atomic_write_json had no cleanup).
            sweep_stale_temps(base)
            STATE_WARNINGS.append(
                "[c3:hook-error] enforcement_state: corrupted "
                f"{ENFORCEMENT_STATE_FILE} quarantined ({type(exc).__name__}); "
                "see .c3/hook_errors.log"
            )
            return _empty_state(session_id)
    if state is None:
        state = _read_legacy_state(base)
    # Session scoping: hook payloads carry session_id; state from a different
    # session must not grant unlocks (signal files used to survive /clear).
    if session_id and state.get("session_id") and state["session_id"] != session_id:
        return _empty_state(session_id)
    # Migrate unlock keys written before canonical_key (raw resolve() form):
    # lookups are canonical now, so re-key at load — old and new forms merge.
    migrated: dict = {}
    for k, cats in state.get("unlocked_files", {}).items():
        try:
            key = canonical_key(k)
        except OSError:
            continue
        merged = set(migrated.get(key, [])) | set(cats)
        migrated[key] = sorted(merged)
    state["unlocked_files"] = migrated
    return state


def save_enforcement_state(state: dict, project_path: Path | None = None) -> None:
    """Atomically persist the consolidated enforcement state."""
    base = project_path if project_path is not None else Path.cwd()
    try:
        _atomic_write_json(base / ENFORCEMENT_STATE_FILE, state)
    except Exception as exc:
        log_hook_error("enforcement_state", exc)


def record_c3_signal(
    tool: str,
    read_unlocked: bool,
    session_id: str = "",
    project_path: Path | None = None,
) -> None:
    """Record 'a c3_* tool just completed' in the consolidated state."""
    state = load_enforcement_state(project_path, session_id=session_id)
    if session_id:
        state["session_id"] = session_id
    state["last_c3_call"] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "read_unlocked": bool(read_unlocked),
    }
    save_enforcement_state(state, project_path)


def canonical_key(path) -> str:
    """Canonical unlock-map key: resolved, forward-slash, casefolded.

    Must stay form-identical to services.access_guard.canonicalize()'s canon
    string for existing paths (parity-tested) — the unlock map and the
    access evaluator must never disagree about path identity on Windows.
    """
    return str(Path(path).resolve()).replace("\\", "/").casefold()


def record_unlocked_files(
    paths,
    categories,
    session_id: str = "",
    project_path: Path | None = None,
) -> None:
    """Merge sticky per-file unlock categories into the consolidated state."""
    cats_to_add = {c for c in categories if c}
    if not cats_to_add:
        return
    state = load_enforcement_state(project_path, session_id=session_id)
    if session_id:
        state["session_id"] = session_id
    changed = False
    for fp in paths:
        if not fp:
            continue
        try:
            normalized = canonical_key(fp)
        except OSError:
            continue
        cats = set(state["unlocked_files"].get(normalized, []))
        merged = sorted(cats | cats_to_add)
        if merged != state["unlocked_files"].get(normalized):
            state["unlocked_files"][normalized] = merged
            changed = True
    if changed:
        save_enforcement_state(state, project_path)


# ── Host (IDE runtime) detection ─────────────────────────────────────────────
# Three hosts drive C3 hooks, and their OUTPUT contracts differ enough that one
# shared payload shape cannot satisfy all of them:
#
#   claude — top-level "tool_result" / "additionalContext" accepted.
#   gemini — no tool_result; context nests under hookSpecificOutput.
#   codex  — every hook output schema is additionalProperties:false and
#            hookSpecificOutput.hookEventName is REQUIRED. Unknown keys are a
#            hard deserialization error, not a warning, so emitting the Claude
#            shape makes Codex discard the whole hook response.
HOST_CLAUDE = "claude"
HOST_GEMINI = "gemini"
HOST_CODEX = "codex"


def detect_host(data: dict) -> str:
    """Identify which agent runtime sent this hook payload.

    Codex is detected by `turn_id`, which its wire schema marks required on
    every turn-scoped event C3 hooks (pre/post tool use, user prompt submit,
    stop) and documents as "Codex extension". Claude Code never sends it.

    Codex MUST be checked before Gemini: Codex declares `tool_response` as
    free-form, so an MCP tool that returns an object would otherwise trip the
    isinstance-dict test and be mistaken for Gemini.

    The CODEX_* env fallback covers a future Codex that drops `turn_id`. A
    false positive there degrades gracefully — Claude Code also accepts
    hookSpecificOutput.additionalContext — so guessing "codex" is the safe
    direction to be wrong in.
    """
    if not isinstance(data, dict):
        return HOST_CLAUDE
    if data.get("turn_id"):
        return HOST_CODEX
    if isinstance(data.get("tool_response"), dict):
        return HOST_GEMINI
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_MANAGED_BY_NPM"):
        return HOST_CODEX
    return HOST_CLAUDE


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


# A c3 tool that failed did not read, search or edit anything, so its
# completion is not evidence that c3 was used (field report 2026-08-22,
# ISSUE-3: c3_compress on a missing path -> "Error: File not found" -> the
# sticky unlock and the signal both fired -> native Write allowed). Tools
# report failure as a leading "Error" / "[<tool>:error]" line, the masked-path
# refusal tag, or an MCP isError flag.
_FAILED_RESPONSE_RE = re.compile(
    r"^\s*(?:Error\b|\[[a-z0-9_\-]+:error\]|\[c3-mask:unsupported\])", re.IGNORECASE)


def response_text_failed(text) -> bool:
    """True when a c3 tool's response text reads as a failure."""
    return bool(_FAILED_RESPONSE_RE.match(str(text or "")))


def tool_response_failed(data: dict) -> bool:
    """True when the PostToolUse payload carries a failed c3 tool response.

    Handles Claude Code's plain string, an MCP-style dict with ``isError`` /
    ``content`` parts, a bare list of parts, and Gemini's ``llmContent``.
    """
    resp = data.get("tool_response")
    if isinstance(resp, dict):
        if resp.get("isError") is True or resp.get("is_error") is True:
            return True
        content = resp.get("content")
        if isinstance(content, list):
            text = "\n".join((p.get("text") or "") for p in content if isinstance(p, dict))
            return response_text_failed(text)
        text, _ = get_tool_output(data)
        return response_text_failed(text)
    if isinstance(resp, list):
        text = "\n".join((p.get("text") or "") if isinstance(p, dict) else str(p) for p in resp)
        return response_text_failed(text)
    return response_text_failed(resp if isinstance(resp, str) else "")


def get_tool_input_path(data: dict) -> str:
    """Extract file path from tool_input, handling Claude (file_path),
    Gemini (path), and NotebookEdit (notebook_path)."""
    tool_input = data.get("tool_input", {})
    return (
        tool_input.get("file_path", "")
        or tool_input.get("path", "")
        or tool_input.get("notebook_path", "")
    )


def record_json_unlocks(
    editable: list,
    project_path: Path | None = None,
    session_id: str = "",
) -> None:
    """Record file paths as read+edit unlocked in the enforcement state.

    Compatibility wrapper kept for existing callers (hook_edit_unlock,
    hook_c3read): unlocks now land in .c3/enforcement_state.json via the
    consolidated state layer instead of the legacy unlocked_files.json.
    Fails silently on I/O errors.
    """
    try:
        record_unlocked_files(
            editable, {"read", "edit"},
            session_id=session_id, project_path=project_path,
        )
    except Exception:
        pass


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
