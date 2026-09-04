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

Output composition merges sub-hook results the same way for every host:
  - a permissionDecision "deny" from any sub-hook wins over allows;
  - additionalContext strings from all sub-hooks are concatenated;
  - a tool_result replacement (hook_filter) is captured.

Serialization then branches by host, because the three runtimes do NOT share
a wire contract (see _hook_utils.detect_host):
  - Claude Code — top-level tool_result / additionalContext; plain-text
    messages print raw when no structured JSON output exists;
  - Gemini CLI — no tool_result; context nests under hookSpecificOutput;
  - Codex CLI — every hook output schema is additionalProperties:false with a
    required hookEventName, so output is built from a per-event whitelist and
    Codex never sees a tool_result. Codex also skips hook_filter entirely:
    it is the only sub-hook that tokenizes the full tool payload.

Failure visibility: a sub-hook crash is logged to .c3/hook_errors.log and
never kills the remaining sub-hooks. CRITICAL failures — a sub-hook module
that cannot be imported, or corrupted enforcement-state JSON — additionally
emit a short "[c3:hook-error] ..." additionalContext warning so enforcement
cannot silently stop enforcing.
"""
import importlib
import inspect
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

from cli._hook_utils import (  # noqa: E402
    HOST_CLAUDE,
    HOST_CODEX,
    HOST_GEMINI,
    detect_host,
    log_hook_error,
    normalize_tool_name,
)

VALID_EVENTS = ("pretool", "posttool", "stop", "prompt")

# ── Codex wire contract ─────────────────────────────────────────────────────
# Codex validates hook output against additionalProperties:false schemas, so
# the Codex branch emits by WHITELIST rather than by "drop the keys we know
# about". A sub-hook that invents a new output key can then never break Codex
# — the key is simply not forwarded.
_CODEX_EVENT_NAMES = {
    "pretool": "PreToolUse",
    "posttool": "PostToolUse",
    "prompt": "UserPromptSubmit",
    "stop": "Stop",
}

# Keys each event's hookSpecificOutput accepts. "stop" is absent deliberately:
# stop.command.output has no hookSpecificOutput member at all.
_CODEX_HSO_KEYS = {
    "pretool": {
        "hookEventName", "additionalContext",
        "permissionDecision", "permissionDecisionReason", "updatedInput",
    },
    "posttool": {"hookEventName", "additionalContext", "updatedMCPToolOutput"},
    "prompt": {"hookEventName", "additionalContext"},
}

# ── Routing tables (parity with the pre-v2.42 per-matcher registration) ─────

# c3 tools whose completion refreshes the enforcement signal — exactly the
# set of mcp__c3__* matchers install-mcp previously attached hook_c3_signal to
# (deliberately NOT "every c3_* tool": c3_bitbucket/c3_project never signaled).
_SIGNAL_TOOLS = {
    "mcp__c3__c3_read", "mcp__c3__c3_shell", "mcp__c3__c3_search",
    "mcp__c3__c3_compress", "mcp__c3__c3_filter", "mcp__c3__c3_memory",
    "mcp__c3__c3_validate", "mcp__c3__c3_edit", "mcp__c3__c3_edits",
    "mcp__c3__c3_impact", "mcp__c3__c3_status", "mcp__c3__c3_delegate",
    "mcp__c3__c3_session", "mcp__c3__c3_agent", "mcp__c3__c3_shell_job",
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


def _routes(event: str, raw_tool: str, norm_tool: str, host: str = HOST_CLAUDE):
    """Yield sub-hook module names applicable to this event + tool, in the
    same relative order the separate hook commands used to run.

    `host` gates hooks that cannot pay their way on a given runtime — see the
    hook_filter carve-out below. Every other sub-hook is host-agnostic: they
    do bookkeeping (ledger, artifact, signal, unlock, ghost sweep) without
    parsing or tokenizing the tool payload, so they stay on for all hosts.
    """
    if event == "pretool":
        # Access Guard runs FIRST: merge_outputs keeps the first deny, so a
        # policy denial here cannot be readmitted by sticky-unlock allows.
        yield "hook_access_guard"
        # hook_pretool_enforce self-filters via its _PREREQS table.
        yield "hook_pretool_enforce"
    elif event == "posttool":
        # hook_filter is the ONE sub-hook that reads the whole tool payload:
        # it tiktoken-encodes the entire Bash output to measure savings, a
        # Rust-side allocation proportional to output size. On Codex that work
        # is also pointless — its schema has no tool_result, so the filtered
        # text could never replace the output anyway. Skip it entirely rather
        # than risk an allocation failure for a result we must discard.
        if norm_tool == "Bash" and host != HOST_CODEX:
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
        # Shell file writes land in the ledger after the fact (after the
        # ghost sweep, so a 0-byte redirect artifact is never logged).
        if norm_tool == "Bash":
            yield "hook_edit_ledger"
    elif event == "stop":
        yield "hook_session_stats"
        yield "hook_auto_snapshot"
        yield "hook_terse_advisor"
    elif event == "prompt":
        yield "hook_prompt_recall"


_RUN_CACHE: dict = {}

# PreToolUse sub-hooks that consult override grants. Under the dispatcher
# they are asked to DEFER consumption (`defer_consume=True`): each returns a
# provisional allow carrying an `_on_allow` callable, and `_settle_grants`
# runs those only once every sub-hook has voted allow. Before v2.102.0 the
# access guard consumed a grant first and a strict-mode discipline deny from
# the next sub-hook then won the merge — grant spent, nothing written, the
# user asked twice for one edit. The same hole ran the other way for a
# discipline grant followed by a policy deny.
_DEFER_CONSUME = {"hook_access_guard", "hook_pretool_enforce"}

# Modules whose failure must DENY rather than fall through (fail closed).
# Scoped to write-class tools + shell: a broken guard must not let mutations
# sail through, but read-class fail-open avoids bricking whole sessions.
_FAIL_CLOSED = {"hook_access_guard"}
_FAIL_CLOSED_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}


def _fail_closed_deny(module_name: str, err: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"[c3-access:error] {module_name} failed to run ({err}). "
                "Native write tools stay blocked until it loads — this is "
                "fail-closed by design. C3 MCP tools still work; see "
                ".c3/hook_errors.log and rerun `c3 install-mcp` if it "
                "persists."
            ),
        }
    }


def _is_deny(out) -> bool:
    hso = out.get("hookSpecificOutput") if isinstance(out, dict) else None
    return isinstance(hso, dict) and hso.get("permissionDecision") == "deny"


def _call_run(run_fn, module_name: str, event: str, payload: dict,
              project_path):
    """Invoke a sub-hook, asking the grant-aware ones to defer consumption.

    Signature-checked rather than assumed: tests stub these modules with
    two-argument lambdas, and a legacy single-file hook must keep working.
    """
    if event == "pretool" and module_name in _DEFER_CONSUME:
        try:
            accepts = "defer_consume" in inspect.signature(run_fn).parameters
        except (TypeError, ValueError):
            accepts = False
        if accepts:
            return run_fn(payload, project_path, defer_consume=True)
    return run_fn(payload, project_path)


def _grant_lost_deny() -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "[c3-override:spent] the override grant that would have "
                "allowed this call was used up between check and use (a "
                "concurrent call spent it). The rule stands — ask again "
                "with c3_override if it is still needed."
            ),
        }
    }


def _settle_grants(outputs: list) -> list:
    """Burn deferred grant uses — only when the merged verdict is allow.

    Any deny among the outputs means the call will not run, so every
    `_on_allow` is dropped unrun and the grants stay live for the retry. On
    an allow each consumer runs; the line it returns replaces the
    provisional one (it carries the post-consumption uses count). A consumer
    that finds nothing left — the grant was spent by a concurrent call
    between peek and settle — turns its allow into a deny: the call must
    not proceed on a grant that no longer exists.
    """
    denied = any(_is_deny(o) for o in outputs)
    settled = []
    for out in outputs:
        if not isinstance(out, dict):
            settled.append(out)
            continue
        consume = out.pop("_on_allow", None)
        if consume is None or denied:
            settled.append(out)
            continue
        try:
            line = consume()
        except Exception as exc:
            log_hook_error("hook_dispatch:settle", exc)
            line = None
        if line:
            out["additionalContext"] = str(line)
            settled.append(out)
        else:
            settled.append(_grant_lost_deny())
    return settled


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


def _codex_output(event: str, deny_hso, contexts: list, tool_result,
                  texts: list) -> dict | None:
    """Serialize merged sub-hook results into Codex's wire contract.

    Codex accepts neither a top-level `tool_result` nor a top-level
    `additionalContext`, and requires `hookEventName` inside
    hookSpecificOutput. Anything that has no Codex-legal home is folded into a
    channel that does exist rather than dropped:

    - a tool_result replacement becomes leading context (it cannot replace the
      output on Codex, but the filtered text is still worth reading);
    - "_text" becomes `systemMessage`, the only user-visible string field
      present on every Codex hook output schema — including Stop, which has no
      hookSpecificOutput at all, so its context lands there too.
    """
    if tool_result is not None:
        contexts.insert(0, str(tool_result))

    result: dict = {}
    hso_keys = _CODEX_HSO_KEYS.get(event)
    joined = "\n".join(c for c in contexts if c)

    if hso_keys is not None:
        hso = {k: v for k, v in (deny_hso or {}).items() if k in hso_keys}
        if joined:
            # A deny's own reason travels in permissionDecisionReason; `joined`
            # is the non-deny context, so the two never collide.
            hso["additionalContext"] = joined
        if hso:
            hso["hookEventName"] = _CODEX_EVENT_NAMES[event]
            result["hookSpecificOutput"] = hso
    elif joined:
        texts.insert(0, joined)

    if texts:
        result["systemMessage"] = "\n".join(texts)
    return result or None


def merge_outputs(outputs: list, warnings: list, is_gemini: bool = False,
                  event: str = "", host: str | None = None) -> dict | None:
    """Compose sub-hook outputs into ONE host-appropriate hook response.

    Collection is host-agnostic:
    - deny beats allow: the first hookSpecificOutput carrying a
      permissionDecision "deny" is kept;
    - additionalContext strings are concatenated with newlines
      (critical warnings appended last);
    - a tool_result replacement (hook_filter) is captured.

    Emission is not. Each host gets the shape its parser accepts — Claude
    takes top-level keys, Gemini nests context and has no tool_result, Codex
    validates against additionalProperties:false schemas (see _codex_output).

    `is_gemini` is retained for callers that predate `host`; `host` wins when
    both are given.
    """
    if host is None:
        host = HOST_GEMINI if is_gemini else HOST_CLAUDE

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

    if host == HOST_CODEX:
        return _codex_output(event, deny_hso, contexts, tool_result, texts)

    is_gemini = host == HOST_GEMINI
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
    host = detect_host(payload)

    outputs: list = []
    warnings: list = []
    # Drain any stale warnings from a previous dispatch in this process.
    _hook_utils.drain_state_warnings()

    for module_name in _routes(event, raw_tool, norm_tool, host):
        run_fn, err = _load_run(module_name)
        if run_fn is None:
            warnings.append(
                f"[c3:hook-error] {module_name}: {err}; see .c3/hook_errors.log"
            )
            if (module_name in _FAIL_CLOSED and event == "pretool"
                    and norm_tool in _FAIL_CLOSED_TOOLS):
                outputs.append(_fail_closed_deny(module_name, err))
            continue
        try:
            out = _call_run(run_fn, module_name, event, payload, project_path)
            if out:
                outputs.append(out)
        except Exception as exc:
            # Non-critical for most hooks: parity with the old behavior where
            # each hook swallowed and logged its own exceptions. Access Guard
            # is the exception — its failure denies write-class tools.
            log_hook_error(module_name, exc)
            if (module_name in _FAIL_CLOSED and event == "pretool"
                    and norm_tool in _FAIL_CLOSED_TOOLS):
                outputs.append(_fail_closed_deny(
                    module_name, f"{type(exc).__name__}: {exc}"))
        # Critical state-layer warnings (corrupt enforcement_state.json)
        # become visible instead of silently disabling enforcement.
        warnings.extend(_hook_utils.drain_state_warnings())
        # A PreToolUse deny is final — merge keeps the first deny and no
        # allow readmits it — so the sub-hooks after it have nothing to add,
        # and running them could only spend a grant or log a block for a
        # call that will not happen.
        if event == "pretool" and outputs and _is_deny(outputs[-1]):
            break

    if event == "pretool":
        outputs = _settle_grants(outputs)
    return merge_outputs(outputs, warnings, event=event, host=host)


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
        # The dispatcher itself failing is critical — say so. Route the notice
        # through merge_outputs so even the panic path speaks the host's
        # dialect: a raw {"additionalContext": ...} is itself invalid on Codex,
        # which would turn one crash into two error reports.
        log_hook_error("hook_dispatch", exc)
        notice = {
            "additionalContext": (
                f"[c3:hook-error] hook_dispatch: {type(exc).__name__}: {exc}; "
                "see .c3/hook_errors.log"
            )
        }
        try:
            output = merge_outputs([notice], [], event=event,
                                   host=detect_host(payload))
        except Exception:
            output = notice
        if output:
            print(json.dumps(output))
        return

    if not output:
        return

    text = output.pop("_text", None)
    if output:
        print(json.dumps(output))
    elif text:
        print(text)


if __name__ == "__main__":
    main()
