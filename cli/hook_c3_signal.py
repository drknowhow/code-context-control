"""PostToolUse hook: record c3_* tool call signal for enforcement.

Fires after: c3_search, c3_compress, c3_filter, c3_memory, c3_validate,
             c3_session, c3_status, c3_impact, c3_agent, c3_shell,
             c3_read, c3_edit, c3_edits, c3_delegate.

Writes the "last_c3_call" section of .c3/enforcement_state.json (via the
consolidated state layer in cli/_hook_utils.py):
  {
    "session_id": "...",
    "last_c3_call": {
      "ts": "...",                  ISO UTC timestamp
      "tool": "c3_search",          short tool name (without mcp__c3__ prefix)
      "read_unlocked": true/false   true for search/compress/filter/read/impact/validate
    },
    ...
  }

hook_pretool_enforce.py reads this as the primary recency check. Pre-v2.42
installs wrote .c3/last_c3_call.json; that file is still read as a fallback
for one release but is no longer written.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import log_hook_error, record_c3_signal, tool_response_failed  # noqa: E402

# Tools that unlock generic read operations (Grep/Glob without a file path)
_READ_UNLOCK_TOOLS = {"c3_search", "c3_compress", "c3_filter", "c3_read", "c3_impact", "c3_validate"}


def run(payload: dict, project_path: Path | None = None):
    """Record the c3_* signal in the consolidated enforcement state.

    Returns None — this hook produces no output for the model.
    """
    raw_tool = payload.get("tool_name", "")

    # Strip mcp__c3__ prefix → short name (e.g. "c3_search")
    short_name = raw_tool.replace("mcp__c3__", "") if "mcp__c3__" in raw_tool else raw_tool

    if not short_name.startswith("c3_"):
        return None

    # A failed call read nothing and edited nothing: no signal (ISSUE-3).
    if tool_response_failed(payload):
        return None

    record_c3_signal(
        short_name,
        short_name in _READ_UNLOCK_TOOLS,
        session_id=str(payload.get("session_id") or ""),
        project_path=project_path,
    )
    return None


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        run(json.loads(raw))
    except Exception as exc:
        log_hook_error("hook_c3_signal", exc)


if __name__ == "__main__":
    main()
