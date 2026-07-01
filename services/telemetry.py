"""Per-tool token telemetry (honest measurement layer).

Appends one JSONL record per MCP tool call to ``.c3/tool_telemetry.jsonl``
and answers aggregation queries over that log ("how many tokens did
c3_filter save this week?").

Record schema (one JSON object per line)::

    ts                ISO-8601 UTC timestamp of the tool call
    session_id        C3 session id ("" if unknown)
    tool              MCP tool name, e.g. "c3_read"
    action            tool sub-action/mode when known ("" otherwise)
    response_tokens   tokens actually returned to the model
    raw_tokens        measured tokens of the un-optimized source, or None
    optimized_tokens  measured tokens after C3 optimization, or None
    duration_ms       tool handler duration if reported, or None
    source            "structured" (explicit accounting), "summary"
                      (legacy regex-scraped from the summary string), or None

Honesty note: ``raw_tokens`` is a *full-read baseline* — what ingesting the
entire un-optimized source would have cost. Savings derived from
``raw_tokens - optimized_tokens`` are estimates versus that counterfactual
baseline (a model would not necessarily have read the full file), which is
why aggregates label them ``estimated_saved_vs_full_read``.

All write paths are failure-safe: telemetry errors must never break a tool
response, so ``append_telemetry_record`` swallows every exception and
returns False instead of raising.

Note: this is LOCAL-ONLY measurement data written inside the project's
``.c3`` directory. It is unrelated to the opt-in Sentry crash reporting in
``services/error_reporting.py`` (``C3_TELEMETRY_OPT_IN``); nothing here
leaves the machine.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

TELEMETRY_RELPATH = Path(".c3") / "tool_telemetry.jsonl"

# Fields every record should carry (missing ones are filled with defaults).
_RECORD_DEFAULTS = {
    "ts": "",
    "session_id": "",
    "tool": "",
    "action": "",
    "response_tokens": 0,
    "raw_tokens": None,
    "optimized_tokens": None,
    "duration_ms": None,
    "source": None,
}

BASELINE_NOTE = (
    "Savings are estimates vs a full-file-read baseline (the token cost of "
    "ingesting the entire un-optimized source), not measured against real "
    "agent behavior."
)


def telemetry_path(project_path) -> Path:
    """Absolute path of the telemetry JSONL for a project."""
    return Path(project_path) / TELEMETRY_RELPATH


def append_telemetry_record(project_path, record: dict) -> bool:
    """Append one telemetry record. Failure-safe: never raises.

    Returns True when the record was written, False on any error.
    """
    try:
        path = telemetry_path(project_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        full = dict(_RECORD_DEFAULTS)
        full.update(record or {})
        if not full["ts"]:
            full["ts"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(full, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception:
        return False


def _parse_ts(value) -> Optional[datetime]:
    """Parse an ISO timestamp; return None when unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def read_telemetry_records(project_path, since: Optional[datetime] = None) -> list:
    """Read telemetry records, optionally filtered to ``ts >= since``.

    Malformed lines and records with unparseable timestamps (when a window
    is requested) are skipped. Never raises for missing/corrupt files.
    """
    path = telemetry_path(project_path)
    records = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if since is not None:
                    ts = _parse_ts(rec.get("ts"))
                    if ts is None or ts < since:
                        continue
                records.append(rec)
    except Exception:
        return records
    return records


def _as_int(value) -> Optional[int]:
    """Coerce to a non-negative int, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (ValueError, TypeError):
        return None
    return n if n >= 0 else None


def aggregate_tool_telemetry(project_path, days: int = 7) -> dict:
    """Aggregate per-tool telemetry over the last ``days`` days.

    This is the public query API for the honest measurement layer. It
    answers: per-tool call counts, response tokens delivered, and estimated
    savings vs the full-read baseline.

    Args:
        project_path: project root containing ``.c3/tool_telemetry.jsonl``.
        days: window size in days; ``days <= 0`` means "all records".

    Returns a dict::

        {
          "days": 7,
          "since": "<ISO timestamp or None when days<=0>",
          "total_calls": int,
          "total_response_tokens": int,
          "estimated_saved_vs_full_read": int,   # sum over measured calls
          "baseline_note": BASELINE_NOTE,
          "by_tool": {
            "c3_read": {
              "calls": int,
              "response_tokens": int,
              "raw_tokens": int,               # sum over measured calls
              "optimized_tokens": int,         # sum over measured calls
              "measured_calls": int,           # calls with a raw/optimized pair
              "estimated_saved_vs_full_read": int,
              "avg_duration_ms": float | None,
            }, ...
          },
        }
    """
    since = None
    since_iso = None
    if days and days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        since_iso = since.isoformat()

    by_tool: dict = {}
    total_calls = 0
    total_response = 0
    total_saved = 0

    for rec in read_telemetry_records(project_path, since=since):
        tool = str(rec.get("tool") or "unknown")
        entry = by_tool.setdefault(tool, {
            "calls": 0,
            "response_tokens": 0,
            "raw_tokens": 0,
            "optimized_tokens": 0,
            "measured_calls": 0,
            "estimated_saved_vs_full_read": 0,
            "_duration_total": 0.0,
            "_duration_count": 0,
        })
        entry["calls"] += 1
        total_calls += 1

        resp = _as_int(rec.get("response_tokens")) or 0
        entry["response_tokens"] += resp
        total_response += resp

        raw = _as_int(rec.get("raw_tokens"))
        opt = _as_int(rec.get("optimized_tokens"))
        if raw is not None and opt is not None:
            entry["raw_tokens"] += raw
            entry["optimized_tokens"] += opt
            entry["measured_calls"] += 1
            saved = max(0, raw - opt)
            entry["estimated_saved_vs_full_read"] += saved
            total_saved += saved

        dur = rec.get("duration_ms")
        if dur is not None and not isinstance(dur, bool):
            try:
                entry["_duration_total"] += float(dur)
                entry["_duration_count"] += 1
            except (ValueError, TypeError):
                pass

    for entry in by_tool.values():
        count = entry.pop("_duration_count")
        total = entry.pop("_duration_total")
        entry["avg_duration_ms"] = round(total / count, 1) if count else None

    return {
        "days": days,
        "since": since_iso,
        "total_calls": total_calls,
        "total_response_tokens": total_response,
        "estimated_saved_vs_full_read": total_saved,
        "baseline_note": BASELINE_NOTE,
        "by_tool": by_tool,
    }
