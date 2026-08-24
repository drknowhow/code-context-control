"""Stop hook: capture Claude Code session token stats to .c3/session_stats.jsonl.

Triggered by the Claude Code 'Stop' event, which delivers on stdin::

    {
      "session_id": "...",
      "transcript_path": "...",
      "hook_event_name": "Stop",
      "stop_hook_active": false,
      "cwd": "..."
    }

Note what is NOT in that payload: ``usage`` and ``cost_usd``. This hook used
to read both directly and wrote a row of zeros every time — 497 consecutive
all-zero rows in this repo before v2.95.0, with nothing failing, because a
missing key reads as 0 just as convincingly as a real 0.

The numbers live in the transcript the payload points at: every assistant
message carries a ``message.usage`` block. So the hook reads the transcript
and sums it, and falls back to the payload only if a future Claude Code
version starts sending usage directly.

Each row is a CUMULATIVE snapshot of the session so far, because Stop fires
once per turn and the transcript is re-summed each time. The newest row for a
given session_id is that session's total; earlier rows are its history. Do not
add rows together.

Cost is not recorded: the transcript carries no price, and multiplying tokens
by a rate hardcoded here would age into a confidently wrong number. Tokens are
measured, so tokens are what gets written.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import log_hook_error  # noqa: E402

_USAGE_FIELDS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
)


def _as_int(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def read_transcript_usage(transcript_path) -> dict:
    """Sum ``message.usage`` across a Claude Code transcript.

    Returns the four token totals plus the model and how many assistant
    messages carried usage. A transcript that cannot be read yields zeros and
    ``messages: 0`` — distinguishable from a real zero-token session, which
    would still have messages.
    """
    totals = {out: 0 for _, out in _USAGE_FIELDS}
    totals["messages"] = 0
    totals["model"] = ""
    if not transcript_path:
        return totals
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return totals
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue  # a partially-flushed final line is normal
                message = rec.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                totals["messages"] += 1
                for src, out in _USAGE_FIELDS:
                    totals[out] += _as_int(usage.get(src))
                model = message.get("model")
                if model and model != "<synthetic>":
                    totals["model"] = str(model)
    except OSError:
        return totals
    return totals


def run(payload: dict, project_path: Path | None = None):
    """Core logic — importable by the dispatcher and tests. Returns None."""
    usage = payload.get("usage") or {}
    if isinstance(usage, dict) and any(usage.get(src) for src, _ in _USAGE_FIELDS):
        # A future Claude Code that does send usage wins over re-reading.
        totals = {out: _as_int(usage.get(src)) for src, out in _USAGE_FIELDS}
        totals["messages"] = 0
        totals["model"] = str(payload.get("model") or "")
        source = "payload"
    else:
        totals = read_transcript_usage(payload.get("transcript_path"))
        source = "transcript" if totals["messages"] else "none"

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": payload.get("session_id"),
        "stop_reason": payload.get("stop_reason"),
        "cost_usd": payload.get("cost_usd"),
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_creation_tokens": totals["cache_creation_tokens"],
        "cache_read_tokens": totals["cache_read_tokens"],
        "assistant_messages": totals["messages"],
        "model": totals["model"],
        "source": source,
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
