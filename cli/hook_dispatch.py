#!/usr/bin/env python3
"""Per-event hook dispatcher: ONE process per Claude Code / Gemini hook event.

Usage: python hook_dispatch.py <pretool|posttool|stop|prompt>

Before v2.42 every hook was registered as its own "cmd /c python <hook>.py"
subprocess, so a single native Read fired up to three interpreter spawns
(~450 ms of process overhead on Windows). The dispatcher reads the hook JSON
payload from stdin ONCE, then runs every applicable sub-hook IN-PROCESS by
importing the hook modules' `run(payload, project_path=None)` functions.

Matcher granularity that previously lived in .claude/settings.local.json
(which hook commands were attached to which tool matchers) now lives in the
routing tables below — install-mcp registers the dispatcher once per matcher,
and the dispatcher decides which sub-hook logic applies.

Output composition follows Claude Code hook semantics:
  - a permissionDecision "deny" from any sub-hook wins over allows;
  - additionalContext strings from all sub-hooks are concatenated;
  - a tool_result replacement (hook_filter) is preserved;
  - plain-text (user-visible) messages are printed only when no structured
    JSON output exists, matching the old per-hook stdout behavior.

Failure visibility: a sub-hook crash is logged to .c3/hook_errors.log and
never kills the remaining sub-hooks. CRITICAL failures — a sub-hook module
that cannot be imported, or corrupted enforcement-state JSON — additionally
emit a short "[c3:hook-error] ..." additionalContext warning so enforcement
cannot silently stop enforcing.
"""
import importlib
import json
import sys
from pathlib import Path

_CLI_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _CLI_DIR.parent
for _p in (str(_PROJECT_ROOT), str(_CLI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cli import _hook_utils  # noqa: E402

# hook_pretool_enforce imports plain `_hook_utils`; alias so both import
# spellings share ONE module instance (state warnings must not split).
sys.modules.setdefault("_hook_utils", _hook_utils)

from cli._hook_utils import log_hook_error, normalize_tool_name  # noqa: E402

VALID_EVENTS = ("pretool", "posttool", "stop", "prompt")

# ── Routing tables (parity with the pre-v2.42 per-matcher registration) ─────

# c3 tools whose completion refreshes the enforcement signal — exactly the
# set of mcp__c3__* matchers install-mcp previously attached hook_c3_signal to
# (deliberately NOT "every c3_* tool": c3_bitbucket/c3_project never signaled).
_SIGNAL_TOOLS = {
    "mcp__c3__c3_read", "mcp__c3__c3_shell", "mcp__c3__c3_search",
    "mcp__c3__c3_compress", "mcp__c3__c3_filter", "mcp__c3__c3_memory",
    "mcp__c3__c3_validate", "mcp__c3__c3_edit", "mcp__c3__c3_edits",
    "mcp__c3__c3_impact", "mcp__c3__c3_status", "mcp__c3__c3_delegate",
    "mcp__c3__c3_session", "mcp__c3__c3_agent",
}

# hook_edit_unlock was registered only for c3_compress and c3_agent
# (c3_read has its own dedicated hook_c3read).
_EDIT_UNLOCK_TOOLS = {"mcp__c3__c3_compress", "mcp__c3__c3_agent"}

# hook_edit_ledger matchers (normalized names). MultiEdit was registered but
# is a no-op inside the hook — kept for parity.
_LEDGER_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# hook_ghost_files trigger matchers (raw names, both IDEs).
_GHOST_TOOLS = {
    "Bash", "run_shell_command",
    "mcp__c3__c3_shell",
    "mcp__c3__c3_read", "Read", "read_file",
}


def _routes(event: str, raw_tool: str, norm_tool: str):
    """Yield sub-hook module names applicable to this event + tool, in the
    same relative order the separate hook commands used to run."""
    if event == "pretool":
        # hook_pretool_enforce self-filters via its _PREREQS table.
        yield "hook_pretool_enforce"
    elif event == "posttool":
        if norm_tool == "Bash":
            yield "hook_filter"
        if norm_tool == "Read":
            yield "hook_read"
        if raw_tool == "mcp__c3__c3_read":
            yield "hook_c3read"
        if raw_tool in _EDIT_UNLOCK_TOOLS:
            yield "hook_edit_unlock"
        if raw_tool in _SIGNAL_TOOLS:
            yield "hook_c3_signal"
        if norm_tool in _LEDGER_TOOLS:
            yield "hook_edit_ledger"
            yield "hook_artifact"
        if raw_tool in _GHOST_TOOLS:
            yield "hook_ghost_files"
    elif event == "stop":
        yield "hook_session_stats"
        yield "hook_auto_snapshot"
        yield "hook_terse_advisor"
    elif event == "prompt":
        yield "hook_prompt_recall"


_RUN_CACHE: dict = {}


def _load_run(module_name: str):
    """Import cli.<module_name> and return its run() (cached).

    Returns (run_fn, error_line). Import/attribute failures are CRITICAL:
    they mean an installed hook silently vanished, so the caller surfaces
    them as an additionalContext warning.
    """
    if module_name in _RUN_CACHE:
        return _RUN_CACHE[module_name]
    try:
        module = importlib.import_module(f"cli.{module_name}")
        run_fn = getattr(module, "run")
        result = (run_fn, "")
    except Exception as exc:  # ImportError, AttributeError, syntax errors …
        log_hook_error(f"hook_dispatch:{module_name}", exc)
        result = (None, f"{type(exc).__name__}: {exc}".splitlines()[0])
    _RUN_CACHE[module_name] = result
    return result


def merge_outputs(outputs: list, warnings: list, is_gemini: bool = False,
                  event: str = "") -> dict | None:
    """Compose sub-hook outputs per Claude Code hook semantics.

    - deny beats allow: the first hookSpecificOutput carrying a
      permissionDecision "deny" is kept verbatim;
    - additionalContext strings are concatenated with newlines
      (critical warnings appended last);
    - tool_result replacement (hook_filter) is preserved;
    - "_text" plain messages ride along under "_text" — main() converts
      them to a JSON systemMessage so Codex never receives raw hook stdout.
    """
    deny_hso = None
    contexts: list = []
    texts: list = []
    tool_result = None

    for out in outputs:
        if not isinstance(out, dict):
            continue
        hso = out.get("hookSpecificOutput")
        if isinstance(hso, dict):
            if deny_hso is None and hso.get("permissionDecision") == "deny":
                deny_hso = hso
            nested_ctx = hso.get("additionalContext")
            if nested_ctx and hso.get("permissionDecision") != "deny":
                contexts.append(str(nested_ctx))
        ctx = out.get("additionalContext")
        if ctx:
            contexts.append(str(ctx))
        if out.get("tool_result") is not None and tool_result is None:
            tool_result = out["tool_result"]
        if out.get("_text"):
            texts.append(str(out["_text"]))

    contexts.extend(warnings)

    result: dict = {}
    if deny_hso is not None:
        result["hookSpecificOutput"] = deny_hso
    if tool_result is not None:
        if is_gemini:
            # Gemini has no tool_result replacement — degrade to context,
            # mirroring _hook_utils.emit_filtered_output.
            contexts.insert(0, str(tool_result))
        else:
            result["tool_result"] = tool_result

    joined = "\n".join(c for c in contexts if c)
    if joined:
        if is_gemini:
            hso = result.setdefault("hookSpecificOutput", {})
            hso.setdefault("additionalContext", joined)
        else:
            result["additionalContext"] = joined

    if texts:
        result["_text"] = "\n".join(texts)

    # UserPromptSubmit expects context under hookSpecificOutput (the
    # top-level additionalContext key is a PostToolUse shape).
    if event == "prompt" and result.get("additionalContext"):
        result["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": result.pop("additionalContext"),
        }

    return result or None


def dispatch(event: str, payload: dict, project_path: Path | None = None) -> dict | None:
    """Run all applicable sub-hooks in-process and merge their outputs."""
    raw_tool = str(payload.get("tool_name", ""))
    norm_tool = normalize_tool_name(raw_tool)
    is_gemini = isinstance(payload.get("tool_response", ""), dict)

    outputs: list = []
    warnings: list = []
    # Drain any stale warnings from a previous dispatch in this process.
    _hook_utils.drain_state_warnings()

    for module_name in _routes(event, raw_tool, norm_tool):
        run_fn, err = _load_run(module_name)
        if run_fn is None:
            warnings.append(
                f"[c3:hook-error] {module_name}: {err}; see .c3/hook_errors.log"
            )
            continue
        try:
            out = run_fn(payload, project_path)
            if out:
                outputs.append(out)
        except Exception as exc:
            # Non-critical: parity with the old behavior where each hook
            # swallowed and logged its own exceptions. Other sub-hooks
            # continue to run.
            log_hook_error(module_name, exc)
        # Critical state-layer warnings (corrupt enforcement_state.json)
        # become visible instead of silently disabling enforcement.
        warnings.extend(_hook_utils.drain_state_warnings())

    return merge_outputs(outputs, warnings, is_gemini=is_gemini, event=event)


def main() -> None:
    # Windows hook subprocesses get cp1252 pipes; sub-hook _text may carry
    # box-drawing chars / emoji, so print(text) below would UnicodeEncodeError.
    _hook_utils.ensure_utf8_stdio()
    event = sys.argv[1].strip().lower() if len(sys.argv) > 1 else ""
    if event not in VALID_EVENTS:
        log_hook_error(
            "hook_dispatch",
            ValueError(f"unknown or missing event arg: {event!r} (expected {VALID_EVENTS})"),
        )
        return

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        log_hook_error("hook_dispatch", exc)
        return

    try:
        output = dispatch(event, payload)
    except Exception as exc:
        # The dispatcher itself failing is critical — say so.
        log_hook_error("hook_dispatch", exc)
        print(json.dumps({
            "additionalContext": (
                f"[c3:hook-error] hook_dispatch: {type(exc).__name__}: {exc}; "
                "see .c3/hook_errors.log"
            )
        }))
        return

    if not output:
        return

    text = output.pop("_text", None)
    if text:
        existing = output.get("systemMessage")
        output["systemMessage"] = (
            f"{existing}\n{text}" if existing else str(text)
        )
    if output:
        print(json.dumps(output))


if __name__ == "__main__":
    main()
