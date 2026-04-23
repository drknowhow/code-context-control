"""PostToolUse hook: record c3_* tool call signal for enforcement.

Fires after: c3_search, c3_compress, c3_filter, c3_memory, c3_validate,
             c3_session, c3_status, c3_impact, c3_agent, c3_shell.

Writes .c3/last_c3_call.json:
  {
    "timestamp": "...",           ISO UTC timestamp
    "tool": "c3_search",          short tool name (without mcp__c3__ prefix)
    "read_unlocked": true/false   true for search/compress/filter
  }

hook_pretool_enforce.py reads this file as the primary recency check.
It replaces the fragile LOOKBACK-3 activity-log scan in long sessions.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import log_hook_error  # noqa: E402

# Tools that unlock generic read operations (Grep/Glob without a file path)
_READ_UNLOCK_TOOLS = {"c3_search", "c3_compress", "c3_filter", "c3_read", "c3_impact", "c3_validate"}

_SIGNAL_FILE = ".c3/last_c3_call.json"


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        raw_tool = data.get("tool_name", "")

        # Strip mcp__c3__ prefix → short name (e.g. "c3_search")
        short_name = raw_tool.replace("mcp__c3__", "") if "mcp__c3__" in raw_tool else raw_tool

        if not short_name.startswith("c3_"):
            return

        signal = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": short_name,
            "read_unlocked": short_name in _READ_UNLOCK_TOOLS,
        }

        signal_path = Path.cwd() / _SIGNAL_FILE
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.write_text(json.dumps(signal, indent=2), encoding="utf-8")

    except Exception as exc:
        log_hook_error("hook_c3_signal", exc)


if __name__ == "__main__":
    main()
