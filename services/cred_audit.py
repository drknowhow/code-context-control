"""One timeline for a credential's whole life: changes AND uses.

C3 already recorded both halves, in two places with two schemas and no reader
that joined them:

  .c3/cred_usage.jsonl   — every USE. Written by services/cred_telemetry when
                           a value is injected into a subprocess, expanded from
                           a {{cred:NAME}} template, revealed to an agent, or
                           printed by `c3 creds get --show`.
  .c3/activity_log.jsonl — every CHANGE, as `cred_action` rows written by the
                           REST routes and the CLI: set, update, delete,
                           import, and the reduce-only batch actions.

Answering "who changed this key, and where has it been used since" meant
reading two logs by hand and merging them by eye. This module does the merge:
one normalized event shape, newest first, filterable, across BOTH scopes (a
global credential used from a project records into ~/.c3, not the project).

What is never in here, by construction: a value. The usage log stores a name,
an optional field name, and a command in its RAW TEMPLATE form ({{cred:X}},
never the substitution). The change log stores names only. There is nothing to
redact because nothing sensitive is written in the first place.
"""

from __future__ import annotations

from pathlib import Path

KIND_USE = "use"
KIND_CHANGE = "change"

#: Usage actions, from services.cred_telemetry.
USE_ACTIONS = ("inject_env", "template", "reveal", "cli_show")

#: Mutating actions written as `cred_action` rows. Prefix-matched for the
#: batch family so a new batch action shows up without editing this tuple.
CHANGE_ACTIONS = ("set", "update", "delete", "import")

#: Uses that put a plaintext value somewhere a human or a model can read it,
#: as opposed to handing it to a subprocess. These are the rows an audit is
#: usually looking for, so they are flagged rather than left to be spotted.
EXPOSING_ACTIONS = ("reveal", "cli_show")

AUDIT_NOTE = (
    "Names and command templates only — a value is never written to either "
    "log, so there is nothing here to redact. Change rows come from the live "
    "activity log; entries older than its last rotation live in the archive "
    "and are not merged here."
)


def _bases(project_path: str, *, include_global: bool = True) -> list:
    """The vault directories whose logs cover this project.

    Mirrors cred_telemetry.read_events — a global credential used from a
    project writes into ~/.c3, so reading only the project would silently
    lose exactly the entries a shared secret generates.

    ``include_global=False`` is for the CROSS-PROJECT roll-up, which walks
    every registered project and would otherwise count the one shared global
    log once per project. There the caller reads the global vault separately,
    exactly once.
    """
    from services import credential_store as cs
    bases: list = []
    try:
        scopes = ("global", "project") if include_global else ("project",)
        for scope in scopes:
            if scope == "project" and cs._project_is_home(project_path):
                continue
            base = cs._scope_dir(scope, project_path)
            if base is not None and base not in bases:
                bases.append(base)
    except Exception:
        bases = [Path(project_path or ".")]
    return bases


def _change_events(project_path: str, *, since: str = "", scan: int = 4000,
                   include_global: bool = True) -> list:
    """`cred_action` rows from the covering activity logs, normalized."""
    from services.activity_log import ActivityLog

    out: list = []
    for base in _bases(project_path, include_global=include_global):
        try:
            log = ActivityLog(str(base))
            rows = log.get_recent(limit=scan, event_type="cred_action",
                                  since=since or None)
        except Exception:
            continue  # one unreadable log must not blank the timeline
        for row in rows:
            out.append({
                "ts": str(row.get("timestamp") or ""),
                "kind": KIND_CHANGE,
                "name": str(row.get("name") or ""),
                "field": "",
                "action": str(row.get("action") or ""),
                "surface": str(row.get("via") or "cli"),
                "scope": str(row.get("scope") or ""),
                "project": str(base),
                "session": "",
                "cmd": "",
                "exit": None,
            })
    return out


def _matches(ev, *, name: str, action: str, surface: str, q: str,
             since: str) -> bool:
    """The filter semantics of cred_telemetry.search_events, reusable.

    Needed because the scope-limited path cannot go through search_events:
    that function merges both scopes and, in merging, discards which log each
    event came from. The event's own ``project`` field is where the credential
    was USED FROM, which is not the same thing — a global secret used from a
    project carries the project's path while living in ~/.c3.
    """
    if name and str(ev.get("name") or "") != name:
        return False
    if action and str(ev.get("action") or "") != action:
        return False
    if surface and str(ev.get("surface") or "") != surface:
        return False
    if since and str(ev.get("ts") or "") < since:
        return False
    terms = [t for t in str(q or "").lower().split() if t]
    if terms:
        hay = " ".join((str(ev.get("name") or ""), str(ev.get("field") or ""),
                        str(ev.get("cmd") or ""),
                        str(ev.get("project") or ""))).lower()
        if not all(t in hay for t in terms):
            return False
    return True


def _use_events(project_path: str, *, name: str = "", action: str = "",
                surface: str = "", q: str = "", since: str = "",
                limit: int = 500, include_global: bool = True) -> tuple:
    from services import cred_telemetry as ct

    if include_global:
        res = ct.search_events(project_path, q=q, name=name, action=action,
                               surface=surface, since=since, limit=limit)
        raw, scanned = res.get("events") or [], res.get("scanned", 0)
    else:
        # Read the project's own log so provenance is the FILE we opened, not
        # a field inside the record.
        raw, scanned = [], 0
        for base in _bases(project_path, include_global=False):
            got = ct._read_one(base)
            scanned += len(got)
            raw.extend(got)
        raw = [ev for ev in raw if _matches(ev, name=name, action=action,
                                            surface=surface, q=q, since=since)]
        raw.sort(key=lambda e: str(e.get("ts") or ""), reverse=True)
        raw = raw[:limit]

    out = []
    for ev in raw:
        out.append({
            "ts": str(ev.get("ts") or ""),
            "kind": KIND_USE,
            "name": str(ev.get("name") or ""),
            "field": str(ev.get("field") or ""),
            "action": str(ev.get("action") or ""),
            "surface": str(ev.get("surface") or ""),
            "scope": "",
            "project": str(ev.get("project") or ""),
            "session": str(ev.get("session") or ""),
            "cmd": str(ev.get("cmd") or ""),
            "exit": ev.get("exit"),
        })
    return out, scanned


def audit_events(project_path: str = ".", *, name: str = "", kind: str = "",
                 action: str = "", surface: str = "", q: str = "",
                 since: str = "", limit: int = 200,
                 include_global: bool = True) -> dict:
    """Merged change+use timeline, newest first.

    ``kind`` narrows to ``use`` or ``change``; the other filters apply to both
    halves so ``name=NPM_TOKEN`` returns that key's whole history. ``q`` is
    AND'd case-insensitive substrings over name/field/cmd/project.

    Returns ``{events, matched, returned, truncated, counts, actions,
    names, note}``. ``matched`` counts everything that passed the filters even
    when ``limit`` cut the list, so "showing 200 of 4,312" stays honest.
    """
    try:
        limit = max(1, min(1000, int(limit)))
    except (TypeError, ValueError):
        limit = 200
    kind = str(kind or "").strip().lower()

    events: list = []
    scanned = 0
    if kind != KIND_CHANGE:
        used, scanned = _use_events(
            project_path, name=name, action=action, surface=surface, q=q,
            since=since, limit=500, include_global=include_global)
        events.extend(used)
    if kind != KIND_USE:
        changes = _change_events(project_path, since=since,
                                 include_global=include_global)
        terms = [t for t in str(q or "").lower().split() if t]
        for ev in changes:
            if name and ev["name"] != name:
                continue
            if action and ev["action"] != action:
                continue
            if surface and ev["surface"] != surface:
                continue
            if terms:
                hay = f"{ev['name']} {ev['project']} {ev['action']}".lower()
                if not all(t in hay for t in terms):
                    continue
            events.append(ev)

    events.sort(key=lambda e: e["ts"], reverse=True)
    matched = len(events)
    shown = events[:limit]

    counts = {"use": 0, "change": 0, "exposing": 0}
    actions: dict = {}
    names: dict = {}
    for ev in events:
        counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1
        if ev["action"] in EXPOSING_ACTIONS:
            counts["exposing"] += 1
        actions[ev["action"]] = actions.get(ev["action"], 0) + 1
        if ev["name"]:
            names[ev["name"]] = names.get(ev["name"], 0) + 1

    return {
        "events": shown,
        "matched": matched,
        "returned": len(shown),
        "truncated": matched > len(shown),
        "scanned": scanned,
        "counts": counts,
        "actions": [{"name": k, "count": v} for k, v in
                    sorted(actions.items(), key=lambda kv: -kv[1])],
        "names": [{"name": k, "count": v} for k, v in
                  sorted(names.items(), key=lambda kv: -kv[1])][:50],
        "note": AUDIT_NOTE,
    }
