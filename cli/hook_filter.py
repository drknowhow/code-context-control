#!/usr/bin/env python3
"""PostToolUse/AfterTool hook for Bash/run_shell_command — filter noisy terminal output via C3.

Reads tool result JSON from stdin. If filtering yields meaningful savings, writes a
replacement `tool_result` (Claude) or `additionalContext` (Gemini). Otherwise emits
compact hints to encourage token-safe follow-up actions.

Claude Code — register in .claude/settings.local.json:
  "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python cli/hook_filter.py"}]}]}

Gemini CLI — register in .gemini/settings.json:
  "hooks": {"AfterTool": [{"matcher": "run_shell_command", "hooks": [{"type": "command", "command": "python cli/hook_filter.py"}]}]}
"""
import json
import re
import sys
from pathlib import Path

# Lines threshold to suggest c3_delegate summarize
LONG_OUTPUT_LINES = 80
# Lines threshold to nudge explicit c3_filter usage
FILTER_HINT_LINES = 20

# Patterns that indicate an error/failure worth diagnosing
_ERROR_PATTERNS = re.compile(
    r"(Traceback \(most recent call last\)|\bError:|\bException:|\bFAILED\b|\bERROR\b)",
    re.MULTILINE,
)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import (  # noqa: E402
    emit_additional_context,
    emit_filtered_output,
    get_tool_output,
    log_hook_error,
    normalize_tool_name,
)


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)

        # Normalize Gemini tool names to Claude equivalents
        tool_name = normalize_tool_name(data.get("tool_name", ""))
        if tool_name != "Bash":
            return

        output, is_gemini = get_tool_output(data)
        if not output or not isinstance(output, str):
            return
        line_count = output.count("\n") + 1

        from core.config import load_hybrid_config
        from services.output_filter import OutputFilter

        # Determine project path from cwd
        project_path = str(Path.cwd())
        config = load_hybrid_config(project_path)

        if config.get("HYBRID_DISABLE_TIER1"):
            return

        # Filter medium/large outputs; short outputs still get hints.
        if len(output) >= 200:
            filt = OutputFilter(config)
            result = filt.filter(output, use_llm=True)

            # Only replace if meaningful savings (>10%)
            if result["savings_pct"] > 10:
                # Store original for c3_raw retrieval
                raw_cache = Path(project_path) / ".c3" / "last_raw_output.txt"
                raw_cache.parent.mkdir(parents=True, exist_ok=True)
                raw_cache.write_text(output, encoding="utf-8")

                filtered = result["filtered"]
                if not filtered.startswith("[c3:filter"):
                    prefix = f"[c3:filter:pass{result['pass_used']}|{result['savings_pct']}%saved] "
                    filtered = prefix + filtered

                emit_filtered_output(filtered, is_gemini)
                return

        # Output was not replaced - provide hints.
        hints = _build_hints(output, line_count=line_count)
        if hints:
            emit_additional_context(hints, is_gemini)

    except Exception as _e:
        log_hook_error("hook_filter", _e)


def _build_hints(output: str, line_count: int | None = None) -> str:
    """Return [c3:hint] lines suggesting token-safe next actions."""
    hints = []
    if line_count is None:
        line_count = output.count("\n") + 1

    if line_count >= FILTER_HINT_LINES:
        hints.append(
            f"[c3:hint:filter] Output is {line_count} lines. "
            "Run c3_filter(text='<raw output>') before further analysis to reduce token noise."
        )

    # Error/traceback detected - suggest diagnose
    if _ERROR_PATTERNS.search(output):
        hints.append(
            "[c3:hint:delegate] Error output detected. "
            "Use c3_delegate(task_type='diagnose', task='<describe the error>') "
            "to root-cause it with a local LLM and save Claude tokens."
        )

    # Very long output - suggest summarize
    if line_count >= LONG_OUTPUT_LINES and not _ERROR_PATTERNS.search(output):
        hints.append(
            f"[c3:hint:delegate] Output is {line_count} lines. "
            "Use c3_delegate(task_type='summarize', task='summarize this output', context='<paste key lines>')."
        )

    return "\n".join(hints)


if __name__ == "__main__":
    main()
