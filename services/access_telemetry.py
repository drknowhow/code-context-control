"""Denial telemetry — makes "the guard is slowing me down" measurable.

``docs/access-guard.md`` §3 specified "denial logging: coalesced per (rule,
tool, session) with a hit counter" but it was never implemented, so a user who
felt friction had no way to find out which rule caused it. This module is that
missing piece, widened to cover BOTH enforcement layers:

``access``     — a path-policy denial from ``services/access_guard`` (Layer B).
``discipline`` — a tool-discipline block from ``hook_pretool_enforce`` (Layer C).

Separating them matters: the fix for a Layer C block is ``c3 enforce advisory``,
while the fix for a Layer B denial is a rule change (or accepting the refusal).
Aggregated output labels each so the user picks the right lever.

Coalescing happens at READ time, not write time. Hooks are short-lived
subprocesses that can run concurrently, so a read-modify-write counter file
would race; an append of one short line is the same pattern
``edit_ledger.jsonl`` already relies on. ``aggregate()`` does the grouping.

Stdlib-only and best-effort: every public function swallows its own errors.
Telemetry must never be the reason a tool call fails.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DENIAL_LOG = ".c3/denials.jsonl"

LAYER_ACCESS = "access"
LAYER_DISCIPLINE = "discipline"

#: Rotate past this many bytes so a denial storm cannot grow without bound.
_MAX_BYTES = 512 * 1024
#: Cap on any interpolated string, matching access_guard's refusal cap.
_FIELD_CAP = 200


def _cap(value, limit: int = _FIELD_CAP) -> str:
    s = str(value or "")
    if len(s) <= limit:
        return s
    keep = (limit - 3) // 2
    return f"{s[:keep]}...{s[-keep:]}"


def _log_path(project_path) -> Path:
    return Path(project_path or ".") / DENIAL_LOG


def _rotate_if_large(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def record(
    *,
    layer: str,
    rule: str,
    tool: str,
    operation: str = "",
    path: str = "",
    scope: str = "",
    session_id: str = "",
    project_path: str = ".",
) -> None:
    """Append one denial event. Never raises, never blocks a tool call.

    ``rule`` is the matched glob for Layer B, or the reason token
    (e.g. ``native-write-blocked``) for Layer C.
    """
    try:
        log = _log_path(project_path)
        if not log.parent.exists():
            return  # no .c3 dir → not a C3 project; nothing to record
        _rotate_if_large(log)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "layer": _cap(layer, 20),
            "rule": _cap(rule),
            "scope": _cap(scope, 20),
            "tool": _cap(tool, 60),
            "op": _cap(operation, 20),
            "path": _cap(path),
            "session": _cap(session_id, 64),
        }
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # telemetry is never load-bearing


def read_events(project_path: str = ".", limit: int = 20_000) -> list:
    """Parse denial events, newest last. Tolerates partial/corrupt lines."""
    out: list = []
    log = _log_path(project_path)
    for candidate in (log.with_name(log.name + ".1"), log):
        try:
            if not candidate.exists():
                continue
            with open(candidate, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(entry, dict):
                        out.append(entry)
        except OSError:
            continue
    return out[-limit:]


def aggregate(project_path: str = ".", *, session_id: str = "") -> dict:
    """Coalesce events per (layer, rule, tool) with a hit counter — the shape
    ``docs/access-guard.md`` §3 asked for.

    Returns::

        {"total": int, "by_layer": {layer: count},
         "rows": [{"layer","rule","scope","tool","hits","last_ts",
                   "sessions","example_path"}, ...],   # hits-descending
         "sessions": int}

    ``session_id`` filters to one session; omit for the whole retained log.
    """
    events = read_events(project_path)
    if session_id:
        events = [e for e in events if e.get("session") == session_id]

    buckets: dict = defaultdict(lambda: {
        "hits": 0, "last_ts": "", "sessions": set(), "example_path": "",
    })
    by_layer: dict = defaultdict(int)
    sessions: set = set()

    for e in events:
        layer = str(e.get("layer") or "")
        key = (layer, str(e.get("rule") or ""), str(e.get("tool") or ""))
        bucket = buckets[key]
        bucket["hits"] += 1
        ts = str(e.get("ts") or "")
        if ts > bucket["last_ts"]:
            bucket["last_ts"] = ts
        sess = str(e.get("session") or "")
        if sess:
            bucket["sessions"].add(sess)
            sessions.add(sess)
        if not bucket["example_path"] and e.get("path"):
            bucket["example_path"] = str(e["path"])
        if not bucket.get("scope"):
            bucket["scope"] = str(e.get("scope") or "")
        by_layer[layer] += 1

    rows = [
        {
            "layer": layer, "rule": rule, "tool": tool,
            "scope": data.get("scope", ""),
            "hits": data["hits"], "last_ts": data["last_ts"],
            "sessions": len(data["sessions"]),
            "example_path": data["example_path"],
        }
        for (layer, rule, tool), data in buckets.items()
    ]
    rows.sort(key=lambda r: (-r["hits"], r["layer"], r["rule"]))

    return {
        "total": len(events),
        "by_layer": dict(by_layer),
        "rows": rows,
        "sessions": len(sessions),
    }


def clear(project_path: str = ".") -> int:
    """Delete the retained denial log. Returns how many files were removed."""
    removed = 0
    log = _log_path(project_path)
    for candidate in (log, log.with_name(log.name + ".1")):
        try:
            if candidate.exists():
                os.unlink(candidate)
                removed += 1
        except OSError:
            pass
    return removed


def suggest(row: dict) -> str:
    """The lever that would clear this denial — the point of the whole module."""
    layer = row.get("layer")
    rule = row.get("rule") or ""
    if layer == LAYER_DISCIPLINE:
        return "c3 enforce advisory   (allows native writes; ledger still logs)"
    # Synthetic spelling rules (<8.3-alias>, <ads>, <unc>, …) report
    # scope='builtin' but are NOT in DISABLEABLE_BUILTINS, so they must be
    # matched BEFORE the builtin branch — otherwise this suggests a
    # `c3 access builtin disable` that the CLI would reject.
    if rule.startswith("<"):
        return ("path-spelling rule — rewrite the path (absolute, no UNC / "
                "8.3 / trailing dot) rather than disabling the guard")
    if row.get("scope") == "builtin":
        return f"c3 access builtin disable '{rule}'   (needs keyring attestation)"
    scope_flag = " --global" if row.get("scope") == "global" else ""
    return f"c3 access remove '{rule}'{scope_flag}   (or narrow the glob)"
