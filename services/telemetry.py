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
    target            what the call was ABOUT — a project-relative file path
                      when the args carried one, else "" (v2.95.0+). Answers
                      "which file cost me tokens", which tool+action alone
                      never could. Records written before 2.95.0 have no
                      target and aggregate under "" rather than being dropped.

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

import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

TELEMETRY_RELPATH = Path(".c3") / "tool_telemetry.jsonl"

# Rotated archives written by services.retention:
# tool_telemetry.<YYYY-MM-DD>[_HHMMSS][-N].jsonl[.gz]
_ARCHIVE_NAME_RE = re.compile(
    r"^tool_telemetry\.(\d{4}-\d{2}-\d{2})(?:_\d{6})?(?:-\d+)?\.jsonl(?:\.gz)?$"
)

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
    "target": "",
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
        _maybe_rotate(project_path, path)
        return True
    except Exception:
        return False


def _maybe_rotate(project_path, path: Path) -> None:
    """Cheap per-append size check; rotate the live log into the archive.

    The reader (``read_telemetry_records``) spans live file + archives, so
    aggregation windows keep working across rotations. Failure-safe.
    """
    try:
        from services.retention import (
            archive_dir_for,
            load_retention_config,
            mb_to_bytes,
            rotate_jsonl,
        )
        cfg = load_retention_config(project_path)
        if not cfg.get("enabled", True):
            return
        rotate_jsonl(
            path,
            mb_to_bytes(cfg.get("telemetry_max_mb", 5)),
            archive_dir_for(project_path),
        )
    except Exception:
        pass


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


def _telemetry_archives(project_path, since: Optional[datetime]) -> list:
    """Rotated telemetry archives that can still contain in-window records.

    An archive rotated on day D only holds records with ``ts`` <= its
    rotation instant, so files whose encoded date is before ``since``'s day
    are skipped entirely (per-record filtering handles the boundary day).
    ``since=None`` (all-records queries) includes every archive.
    Returned oldest-first. Never raises.
    """
    out = []
    try:
        archive_dir = Path(project_path) / ".c3" / "archive"
        if not archive_dir.exists():
            return out
        since_day = None
        if since is not None:
            aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            since_day = aware.astimezone(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0)
        for f in archive_dir.iterdir():
            m = _ARCHIVE_NAME_RE.match(f.name)
            if not m or not f.is_file():
                continue
            try:
                fdate = datetime.strptime(m.group(1), "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except ValueError:
                continue
            if since_day is not None and fdate < since_day:
                continue
            out.append(f)
        out.sort(key=lambda p: p.name)
    except Exception:
        return out
    return out


def _read_jsonl_records(path: Path, since: Optional[datetime]) -> list:
    """Read one live or archived (.gz) JSONL file, filtered to ``ts >= since``."""
    records = []
    try:
        if path.name.endswith(".gz"):
            handle = gzip.open(path, "rt", encoding="utf-8", errors="replace")
        else:
            handle = open(path, encoding="utf-8", errors="replace")
        with handle as f:
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


def read_telemetry_records(project_path, since: Optional[datetime] = None) -> list:
    """Read telemetry records, optionally filtered to ``ts >= since``.

    Spans the live file AND rotated archives in ``.c3/archive`` when the
    requested window extends past the live file (archives whose rotation
    date precedes the window are skipped without being opened). Malformed
    lines and records with unparseable timestamps (when a window is
    requested) are skipped. Never raises for missing/corrupt files.
    """
    records: list = []
    for archive in _telemetry_archives(project_path, since):
        records.extend(_read_jsonl_records(archive, since))
    records.extend(_read_jsonl_records(telemetry_path(project_path), since))
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


def aggregate_tool_telemetry(project_path, days: int = 7, *,
                             top_targets: int = 25,
                             top_sessions: int = 25) -> dict:
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
    by_day: dict = {}
    by_session: dict = {}
    by_target: dict = {}
    shell_classes: dict = {}
    map_backends: dict = {}
    chain = _MapReadChain(window=MAP_CHAIN_WINDOW)
    total_calls = 0
    total_response = 0
    total_saved = 0

    for rec in read_telemetry_records(project_path, since=since):
        tool = str(rec.get("tool") or "unknown")
        if tool == "c3_shell" and isinstance(rec.get("detail"), dict):
            _fold_shell_detail(shell_classes, rec)
        if tool in ("c3_compress", "c3_read"):
            if isinstance(rec.get("detail"), dict):
                _fold_map_detail(map_backends, rec)
            chain.observe(rec)
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

        # The three dimensions the per-tool view cannot answer: WHEN, WHICH
        # session, and WHAT the call was about.
        day = str(rec.get("ts") or "")[:10]
        if day:
            slot = by_day.setdefault(day, {"calls": 0, "response_tokens": 0})
            slot["calls"] += 1
            slot["response_tokens"] += resp

        sid = str(rec.get("session_id") or "")
        if sid:
            slot = by_session.setdefault(
                sid, {"calls": 0, "response_tokens": 0, "first_ts": "", "last_ts": ""})
            slot["calls"] += 1
            slot["response_tokens"] += resp
            ts = str(rec.get("ts") or "")
            if ts:
                if not slot["first_ts"] or ts < slot["first_ts"]:
                    slot["first_ts"] = ts
                if ts > slot["last_ts"]:
                    slot["last_ts"] = ts

        target = str(rec.get("target") or "")
        if target:
            slot = by_target.setdefault(target, {"calls": 0, "response_tokens": 0})
            slot["calls"] += 1
            slot["response_tokens"] += resp

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
    for slot in shell_classes.values():
        durs = sorted(slot.pop("_durations"))
        slot["p50_duration_ms"] = durs[len(durs) // 2] if durs else None
        slot["p95_duration_ms"] = durs[min(len(durs) - 1, int(0.95 * len(durs)))] if durs else None

    def _top(mapping, limit):
        rows = sorted(mapping.items(),
                      key=lambda kv: (-kv[1]["response_tokens"], kv[0]))
        return [dict(name=k, **v) for k, v in rows[:limit]]

    return {
        "days": days,
        "since": since_iso,
        "total_calls": total_calls,
        "total_response_tokens": total_response,
        "estimated_saved_vs_full_read": total_saved,
        "baseline_note": BASELINE_NOTE,
        "by_tool": by_tool,
        # Sorted lists, not dicts: these are read in rank order and a dict
        # would push that ordering decision into every consumer.
        "by_day": [dict(name=k, **by_day[k]) for k in sorted(by_day)],
        "by_session": _top(by_session, top_sessions),
        "by_target": _top(by_target, top_targets),
        "targets_tracked": len(by_target),
        # c3_shell by command class — only records that carry a `detail`
        # (2.111.0+). This is the before/after instrument for the shell
        # remediation: how many calls, how many tokens, how many would
        # exceed the S1 budget, how many were filtered, how long they ran.
        "shell_by_class": {k: shell_classes[k] for k in sorted(shell_classes)},
        "shell_budget_bytes": SHELL_BUDGET_BYTES,
        # File maps by backend/parser — only records that carry a `detail`
        # (2.120.0+). The before/after instrument for the c3_compress
        # remediation: how many maps were served, by which extractor, how
        # many tokens each cost against the full-read baseline.
        "map_by_backend": {k: _finish_map_slot(map_backends[k])
                           for k in sorted(map_backends)},
        # Did a map lead to a targeted read, and was a targeted read preceded
        # by a map? Counted per session within MAP_CHAIN_WINDOW calls; works
        # on any record with a `target` (2.111.0+), detail or not.
        "map_read_chain": chain.result(),
    }


# The response-byte budget the S1 phase of the shell remediation enforces
# (18 KiB default, 22 KiB ceiling). Named here so the measurement of how
# many calls WOULD exceed it exists before the cap does.
SHELL_BUDGET_BYTES = 18 * 1024


def _fold_shell_detail(classes: dict, rec: dict) -> None:
    """Accumulate one c3_shell record's `detail` into its command class."""
    detail = rec.get("detail") or {}
    cls = str(detail.get("cmd_class") or "other")
    slot = classes.setdefault(cls, {
        "calls": 0, "response_tokens": 0, "response_bytes": 0,
        "stdout_bytes": 0, "stderr_bytes": 0, "over_budget": 0,
        "filtered": 0, "spilled": 0, "timeouts": 0, "failures": 0,
        "longest_line_max": 0, "_durations": [],
    })
    slot["calls"] += 1
    slot["response_tokens"] += _as_int(rec.get("response_tokens")) or 0
    resp_bytes = _as_int(detail.get("response_bytes")) or 0
    slot["response_bytes"] += resp_bytes
    if resp_bytes > SHELL_BUDGET_BYTES:
        slot["over_budget"] += 1
    slot["stdout_bytes"] += _as_int(detail.get("stdout_bytes")) or 0
    slot["stderr_bytes"] += _as_int(detail.get("stderr_bytes")) or 0
    if detail.get("filtered"):
        slot["filtered"] += 1
    if detail.get("spilled"):
        slot["spilled"] += 1
    if detail.get("timed_out"):
        slot["timeouts"] += 1
    exit_code = detail.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        slot["failures"] += 1
    longest = _as_int(detail.get("longest_line")) or 0
    if longest > slot["longest_line_max"]:
        slot["longest_line_max"] = longest
    dur = rec.get("duration_ms")
    if dur is not None and not isinstance(dur, bool):
        try:
            slot["_durations"].append(float(dur))
        except (ValueError, TypeError):
            pass


# How many tool calls apart a map and a read on the same file still count as
# one "map → read" chain. Five is the window the 2026-09-06 evaluation used.
MAP_CHAIN_WINDOW = 5


def _is_map_record(rec: dict) -> bool:
    """A record that served a file MAP rather than source lines.

    c3_compress always maps (every mode is a structural summary). c3_read
    maps only when it fell back — its detail says so (2.120.0+); a read
    without detail is assumed to be a source read.
    """
    tool = str(rec.get("tool") or "")
    if tool == "c3_compress":
        return True
    if tool == "c3_read":
        detail = rec.get("detail") or {}
        return bool(detail.get("fallback"))
    return False


def _fold_map_detail(slots: dict, rec: dict) -> None:
    """Accumulate one c3_compress / c3_read record's `detail` by backend."""
    detail = rec.get("detail") or {}
    backend = str(detail.get("backend") or "unknown")
    if backend == "file_memory":
        parser = str(detail.get("parser") or "unknown")
        key = f"file_memory/{parser}"
    elif backend == "compressor":
        key = f"compressor/{detail.get('actual_mode') or detail.get('requested_mode') or '?'}"
    else:
        key = backend
    slot = slots.setdefault(key, {
        "calls": 0, "response_tokens": 0, "raw_tokens": 0, "optimized_tokens": 0,
        "measured_calls": 0, "cache_hits": 0, "symbol_reads": 0,
        "map_fallbacks": 0, "_ratios": [],
    })
    slot["calls"] += 1
    slot["response_tokens"] += _as_int(rec.get("response_tokens")) or 0
    raw = _as_int(rec.get("raw_tokens"))
    opt = _as_int(rec.get("optimized_tokens"))
    if raw and opt is not None:
        slot["raw_tokens"] += raw
        slot["optimized_tokens"] += opt
        slot["measured_calls"] += 1
        slot["_ratios"].append(opt / raw)
    if detail.get("cache_hit"):
        slot["cache_hits"] += 1
    if detail.get("symbols"):
        slot["symbol_reads"] += 1
    if detail.get("fallback"):
        slot["map_fallbacks"] += 1


def _finish_map_slot(slot: dict) -> dict:
    ratios = sorted(slot.pop("_ratios"))
    slot["ratio_p50"] = round(ratios[len(ratios) // 2], 3) if ratios else None
    slot["ratio_p95"] = (round(ratios[min(len(ratios) - 1, int(0.95 * len(ratios)))], 3)
                         if ratios else None)
    return slot


class _MapReadChain:
    """Map → read adjacency, per session, within a call window.

    `maps_followed_by_read`: a map on file F followed within `window` calls
    by a source read of F (the map did its job). `reads_preceded_by_map`: a
    source read of F with a map of F in the previous `window` calls (the
    model needed the map to pick its target). Reads with no map before them
    found their symbols some other way — usually a c3_search hit.
    """

    def __init__(self, window: int):
        self.window = window
        self._recent: dict = {}   # session_id -> list[(kind, target)]
        self.maps = 0
        self.maps_followed = 0
        self.reads = 0
        self.reads_preceded = 0

    def observe(self, rec: dict) -> None:
        tool = str(rec.get("tool") or "")
        if tool not in ("c3_compress", "c3_read"):
            return
        sid = str(rec.get("session_id") or "")
        targets = [t for t in str(rec.get("target") or "").split(",") if t]
        if not targets:
            return
        kind = "map" if _is_map_record(rec) else "read"
        recent = self._recent.setdefault(sid, [])
        if kind == "read":
            self.reads += 1
            if any(k == "map" and t in targets for k, t, _ in recent):
                self.reads_preceded += 1
            # A read that resolves an earlier map's target closes that map.
            for entry in recent:
                if entry[0] == "map" and entry[1] in targets and not entry[2]:
                    entry[2] = True
                    self.maps_followed += 1
        else:
            self.maps += 1
        for t in targets:
            recent.append([kind, t, False])
        del recent[:-self.window]

    def result(self) -> dict:
        return {
            "window": self.window,
            "maps": self.maps,
            "maps_followed_by_read": self.maps_followed,
            "reads": self.reads,
            "reads_preceded_by_map": self.reads_preceded,
            "map_follow_rate": (round(self.maps_followed / self.maps, 3)
                                if self.maps else None),
            "read_mapped_first_rate": (round(self.reads_preceded / self.reads, 3)
                                       if self.reads else None),
        }


# ── Claude Code session stats (Stop-hook rows) ───────────────────────────────
# A different measurement from the per-tool log above: tool telemetry counts
# what C3's own tools returned, while these rows are the WHOLE conversation's
# billable usage as reported by the transcript. Neither one is a subset of the
# other, so the UI shows both rather than pretending one explains the other.

SESSION_STATS_RELPATH = Path(".c3") / "session_stats.jsonl"

_SESSION_TOKEN_FIELDS = ("input_tokens", "output_tokens",
                         "cache_creation_tokens", "cache_read_tokens")


def session_stats_path(project_path) -> Path:
    return Path(project_path) / SESSION_STATS_RELPATH


def aggregate_session_stats(project_path, days: int = 0,
                            limit: int = 50) -> dict:
    """Roll up ``.c3/session_stats.jsonl`` into per-session totals.

    Each row is a CUMULATIVE snapshot written on one Stop event, so a session
    appears once per turn and its rows must not be summed. The newest row per
    ``session_id`` is that session's total; that is the one kept.

    ``all_zero_rows`` counts rows carrying no tokens at all. Before v2.95.0 the
    Stop hook read fields the event never sends and every row was zero — a
    non-zero count here dates the log rather than implying idle sessions.
    """
    path = session_stats_path(project_path)
    since = None
    if days and days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=days)

    latest: dict = {}
    rows_seen = 0
    all_zero = 0
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    rows_seen += 1
                    if rec.get("usage_available") is not False and not any(_as_int(rec.get(f)) for f in _SESSION_TOKEN_FIELDS):
                        all_zero += 1
                    ts = str(rec.get("ts") or "")
                    if since is not None:
                        parsed = _parse_ts(ts)
                        if parsed is None or parsed < since:
                            continue
                    sid = str(rec.get("session_id") or "")
                    if not sid:
                        continue
                    key = (str(rec.get("provider") or "claude"), sid)
                    prev = latest.get(key)
                    if prev is None or ts >= str(prev.get("ts") or ""):
                        latest[key] = rec
        except OSError:
            pass

    sessions = []
    totals = {f: 0 for f in _SESSION_TOKEN_FIELDS}
    for (provider, sid), rec in latest.items():
        row = {"session_id": sid, "ts": str(rec.get("ts") or ""),
               "model": str(rec.get("model") or ""),
               "assistant_messages": _as_int(rec.get("assistant_messages")),
               "source": str(rec.get("source") or ""),
               "provider": provider, "usage_available": rec.get("usage_available", True)}
        for f in _SESSION_TOKEN_FIELDS:
            value = _as_int(rec.get(f)) or 0
            row[f] = value if row["usage_available"] else None
            if row["usage_available"]:
                totals[f] += value
        row["total_tokens"] = sum(row[f] or 0 for f in _SESSION_TOKEN_FIELDS) if row["usage_available"] else None
        sessions.append(row)
    sessions.sort(key=lambda r: r["ts"], reverse=True)

    return {
        "days": days,
        "sessions": sessions[:limit],
        "session_count": len(sessions),
        "unavailable_sessions": sum(not row["usage_available"] for row in sessions),
        "rows_seen": rows_seen,
        "all_zero_rows": all_zero,
        "totals": totals,
        "total_tokens": sum(totals.values()),
        "cost_note": (
            "Token counts are measured from the transcript. Cost is not "
            "recorded: the transcript carries no price, and a rate hardcoded "
            "here would age into a confidently wrong number."
        ),
    }
