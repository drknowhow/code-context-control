"""Stop hook: capture Claude Code session token/cost stats to .c3/session_stats.jsonl.

Triggered by the Claude Code 'Stop' event. Receives JSON on stdin:
  {
    "session_id": "...",
    "transcript_path": "...",
    "stop_reason": "end_turn" | "max_turns" | ...,
    "cost_usd": 0.042,
    "usage": {
      "input_tokens": 12400,
      "output_tokens": 850,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 11200
    }
  }

Appends one JSON line per session to .c3/session_stats.jsonl.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import log_hook_error  # noqa: E402


def run(payload: dict, project_path: Path | None = None):
    """Core logic — importable by the dispatcher and tests. Returns None."""
    usage = payload.get("usage") or {}
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": payload.get("session_id"),
        "stop_reason": payload.get("stop_reason"),
        "cost_usd": payload.get("cost_usd"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
    }

    # Claude Code runs hooks from the project root, so .c3/ is relative to CWD.
    base = project_path if project_path is not None else Path.cwd()
    stats_dir = base / ".c3"
    if stats_dir.exists():
        stats_path = stats_dir / "session_stats.jsonl"
        with open(stats_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        log_hook_error("hook_session_stats", exc)
        sys.exit(0)

    try:
        run(data)
    except Exception as exc:
        log_hook_error("hook_session_stats", exc)

    sys.exit(0)


if __name__ == "__main__":
    main()
