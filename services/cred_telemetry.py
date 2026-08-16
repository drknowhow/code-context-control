"""Credential usage telemetry — when, where, and how often a credential
was actually used.

``cred_state.json`` keeps a {last_used, use_count} counter per name — a
summary, not a history. This module is the history: one appended line per
use (injection, template expansion, reveal, CLI --show), written to the
OWNING scope's ``.c3/cred_usage.jsonl`` — a global credential's usage from
any project lands in ``~/.c3``, a project credential's next to its registry.

The shape is a deliberate clone of ``services/access_telemetry.py``:
append-one-line (concurrent short-lived processes cannot race an append the
way they race a read-modify-write counter), every field capped, single-file
rotation at 512KB, coalescing at READ time, and every public function
swallows its own errors — telemetry must never be the reason a tool call
fails.

Privacy contract: events carry NAMES and metadata only. ``cmd`` is the RAW
template form of the command (the one every echo/log surface already
shows), capped hard — never the expanded string, never a decoded value.
Both filenames (live + rotation) are vault-write-protected and gitignored.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

USAGE_LOG = ".c3/cred_usage.jsonl"

#: Use actions — how the value left the vault.
ACTION_INJECT = "inject_env"    # env_creds injection into a subprocess
ACTION_TEMPLATE = "template"    # {{cred:NAME[.field]}} expansion
ACTION_REVEAL = "reveal"        # gated reveal into model context
ACTION_CLI_SHOW = "cli_show"    # c3 creds get --show at a terminal

#: Rotate past this many bytes so heavy use cannot grow without bound.
_MAX_BYTES = 512 * 1024
_FIELD_CAP = 200
_CMD_CAP = 120


def _cap(value, limit: int = _FIELD_CAP) -> str:
    s = str(value or "")
    if len(s) <= limit:
        return s
    keep = (limit - 3) // 2
    return f"{s[:keep]}...{s[-keep:]}"


def _rotate_if_large(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def _owning_base(name: str, project_path: str):
    """Base dir whose .c3/ holds this credential's registry, or None."""
    from services import credential_store as cs
    try:
        scope = cs._owning_scope(name, project_path)
        if not scope:
            return None
        return cs._scope_dir(scope, project_path)
    except Exception:
        return None


def record_use(
    refs,
    *,
    project_path: str = ".",
    action: str = ACTION_INJECT,
    surface: str = "shell",
    session_id: str = "",
    cmd_preview: str = "",
    exit_code=None,
) -> None:
    """Append one event per distinct ref. Never raises.

    A ref is a plain name (``NPM_TOKEN``) or a dotted field of a structured
    entry (``CARD.number``). ``cmd_preview`` MUST be the raw template form —
    callers own that invariant; this function only caps it.
    """
    try:
        proj = str(Path(project_path or ".").resolve())
    except Exception:
        proj = str(project_path or ".")
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for ref in sorted(set(refs or [])):
        try:
            name, _, field = str(ref).partition(".")
            base = _owning_base(name, project_path)
            if base is None or not (base / ".c3").exists():
                continue  # unknown name or not a C3 dir → nothing to record
            log = base / USAGE_LOG
            _rotate_if_large(log)
            entry = {
                "ts": ts,
                "name": _cap(name, 128),
                "field": _cap(field, 40),
                "action": _cap(action, 20),
                "surface": _cap(surface, 20),
                "session": _cap(session_id, 64),
                "project": _cap(proj),
                "cmd": _cap(cmd_preview, _CMD_CAP),
            }
            if exit_code is not None:
                entry["exit"] = int(exit_code)
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            continue  # telemetry is never load-bearing


def _read_one(base: Path) -> list:
    out: list = []
    log = base / USAGE_LOG
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
    return out


def read_events(project_path: str = ".", limit: int = 20_000) -> list:
    """Merged usage events from both scopes' logs, oldest first, newest
    last — mirrors ``read_usage_state``'s two-scope merge."""
    from services import credential_store as cs
    bases: list = []
    try:
        for scope in ("global", "project"):
            if scope == "project" and cs._project_is_home(project_path):
                continue
            base = cs._scope_dir(scope, project_path)
            if base is not None and base not in bases:
                bases.append(base)
    except Exception:
        bases = [Path(project_path or ".")]
    events: list = []
    for base in bases:
        events.extend(_read_one(base))
    events.sort(key=lambda e: str(e.get("ts") or ""))
    return events[-limit:]


def search_events(project_path: str = ".", *, q: str = "", name: str = "",
                  action: str = "", surface: str = "", session: str = "",
                  since: str = "", limit: int = 200) -> dict:
    """Filter raw usage events, newest first — the backing for UI/CLI search.

    ``q`` is AND'd case-insensitive substrings over name/field/cmd/project;
    ``name``/``action``/``surface`` match exactly; ``session`` is a prefix
    match; ``since`` compares ISO-8601 strings. ``limit`` caps returned
    events (1..500); matching continues past it so ``matched`` stays honest.
    """
    terms = [t for t in str(q or "").lower().split() if t]
    name = str(name or "").strip()
    action = str(action or "").strip()
    surface = str(surface or "").strip()
    session = str(session or "").strip()
    since = str(since or "").strip()
    try:
        limit = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        limit = 200

    events = read_events(project_path)
    matched = 0
    out: list = []
    for ev in reversed(events):  # read_events returns newest LAST
        if name and str(ev.get("name") or "") != name:
            continue
        if action and str(ev.get("action") or "") != action:
            continue
        if surface and str(ev.get("surface") or "") != surface:
            continue
        if session and not str(ev.get("session") or "").startswith(session):
            continue
        if since and str(ev.get("ts") or "") < since:
            continue
        if terms:
            hay = " ".join((str(ev.get("name") or ""),
                            str(ev.get("field") or ""),
                            str(ev.get("cmd") or ""),
                            str(ev.get("project") or ""))).lower()
            if not all(t in hay for t in terms):
                continue
        matched += 1
        if len(out) < limit:
            out.append(dict(ev))

    return {"events": out, "matched": matched, "scanned": len(events),
            "truncated": matched > len(out)}


def aggregate(project_path: str = ".", *, name: str = "") -> dict:
    """Coalesce events per credential with counters — when/where/how often.

    Returns::

        {"total": int,
         "by_surface": {surface: count}, "by_action": {action: count},
         "rows": [{"name","hits","last_ts","first_ts","fields",
                   "surfaces","projects","sessions"}, ...],  # hits-desc
         "sessions": int}

    ``name`` filters to one credential; omit for the whole retained log.
    """
    events = read_events(project_path)
    if name:
        events = [e for e in events if str(e.get("name") or "") == name]

    buckets: dict = defaultdict(lambda: {
        "hits": 0, "last_ts": "", "first_ts": "", "fields": set(),
        "surfaces": defaultdict(int), "projects": set(), "sessions": set(),
    })
    by_surface: dict = defaultdict(int)
    by_action: dict = defaultdict(int)
    sessions: set = set()

    for e in events:
        bucket = buckets[str(e.get("name") or "")]
        bucket["hits"] += 1
        ts = str(e.get("ts") or "")
        if ts > bucket["last_ts"]:
            bucket["last_ts"] = ts
        if not bucket["first_ts"] or (ts and ts < bucket["first_ts"]):
            bucket["first_ts"] = ts
        if e.get("field"):
            bucket["fields"].add(str(e["field"]))
        surf = str(e.get("surface") or "")
        bucket["surfaces"][surf] += 1
        by_surface[surf] += 1
        by_action[str(e.get("action") or "")] += 1
        if e.get("project"):
            bucket["projects"].add(str(e["project"]))
        sess = str(e.get("session") or "")
        if sess:
            bucket["sessions"].add(sess)
            sessions.add(sess)

    rows = [
        {
            "name": cname, "hits": data["hits"],
            "last_ts": data["last_ts"], "first_ts": data["first_ts"],
            "fields": sorted(data["fields"]),
            "surfaces": dict(data["surfaces"]),
            "projects": len(data["projects"]),
            "sessions": len(data["sessions"]),
        }
        for cname, data in buckets.items()
    ]
    rows.sort(key=lambda r: (-r["hits"], r["name"]))

    return {
        "total": len(events),
        "by_surface": dict(by_surface),
        "by_action": dict(by_action),
        "rows": rows,
        "sessions": len(sessions),
    }


def clear(project_path: str = ".", *, scope: str = "project") -> int:
    """Delete one scope's retained usage log. Returns files removed.

    Global is explicit on purpose — clearing ``~/.c3`` erases every
    project's history for global credentials.
    """
    from services import credential_store as cs
    try:
        base = cs._scope_dir(scope, project_path)
    except Exception:
        base = None
    if base is None:
        return 0
    removed = 0
    log = base / USAGE_LOG
    for candidate in (log, log.with_name(log.name + ".1")):
        try:
            if candidate.exists():
                os.unlink(candidate)
                removed += 1
        except OSError:
            pass
    return removed
