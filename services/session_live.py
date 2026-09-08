"""Heartbeat files that prove an agent session is alive right now.

Why this module exists
---------------------
``ProjectManager._get_live_session_info`` used to INFER liveness from the
activity log: newest ``session_start`` row, ended by a ``session_save`` row
(written only when a human clicks "end session" in the hub) or by 20 minutes
of tool-call silence. Both halves are wrong in practice:

- a closed IDE kept reading "live" for up to 20 minutes, because nothing
  consumed the ``session_end`` row the SessionEnd hook writes;
- an open session that was simply quiet — a long model turn, a human reading
  — dropped off the hub after 20 minutes while its MCP process was right
  there, serving.

The MCP server process IS the session. So it says so: one small file per live
session under ``.c3/live/``, refreshed every ``HEARTBEAT_INTERVAL_S`` and
removed on shutdown. A reader treats a file whose ``ts`` is within
``HEARTBEAT_TTL_S`` as a live session, and one stale file as a session whose
process died without cleanup (crash, taskkill, reboot).

A pid is recorded for diagnostics only. Liveness is decided by the timestamp,
never by probing the pid: pids are reused across reboots, and there is no
portable, cheap "is this pid alive" call (``os.kill(pid, 0)`` is not reliable
on Windows and C3 does not depend on psutil).

One file per session id — not one file per project — so two IDEs, or a repo
and its worktrees, each report themselves instead of collapsing into one.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

# Written by the MCP server's heartbeat thread; read by the hub and the CLI.
HEARTBEAT_INTERVAL_S = 60
# Three missed beats. Generous on purpose: a machine under load can stall a
# daemon thread for longer than one interval, and a false "dead" is worse
# than a stale entry lingering a minute or two.
HEARTBEAT_TTL_S = 180

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def live_dir(project) -> Path:
    """``<project>/.c3/live`` — the directory holding one file per session."""
    return Path(project) / ".c3" / "live"


def _file_name(session_id: str) -> str:
    """Filesystem-safe name for a session id (ids are C3-generated, but a
    host id can arrive from anywhere, so never trust it as a path)."""
    cleaned = _SAFE_NAME.sub("_", str(session_id or "").strip())[:120]
    return f"{cleaned or 'unknown'}.json"


def heartbeat_path(project, session_id: str) -> Path:
    return live_dir(project) / _file_name(session_id)


def beat(project, session_id: str, *, host_session_id: str = "",
         ide: str = "", pid: int | None = None) -> bool:
    """Record/refresh the heartbeat for one session. Never raises."""
    if not str(session_id or "").strip():
        return False
    try:
        from services.atomic_json import write_json_atomic
        target = heartbeat_path(project, session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(target, {
            "session_id": str(session_id),
            "host_session_id": str(host_session_id or ""),
            "ide": str(ide or ""),
            "pid": int(pid if pid is not None else os.getpid()),
            "ts": time.time(),
        })
        return True
    except Exception:
        return False


def clear(project, session_id: str) -> bool:
    """Remove one session's heartbeat (clean shutdown). Never raises."""
    try:
        heartbeat_path(project, session_id).unlink()
        return True
    except Exception:
        return False


def live_sessions(project, ttl: float = HEARTBEAT_TTL_S,
                  prune: bool = True) -> list[dict]:
    """Sessions whose heartbeat is fresher than ``ttl``, newest first.

    Stale files are deleted when ``prune`` is set — the next reader would
    reach the same verdict, and leaving them means ``.c3/live`` grows one
    file per crashed session forever.
    """
    out: list[dict] = []
    try:
        entries = list(live_dir(project).iterdir())
    except Exception:
        return out
    now = time.time()
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ts = float(data.get("ts") or 0)
        except Exception:
            # Unreadable or half-written: treat as absent, leave it alone.
            continue
        age = now - ts
        if 0 <= age <= ttl or age < 0:
            data["age_seconds"] = max(0.0, age)
            out.append(data)
        elif prune:
            try:
                path.unlink()
            except OSError:
                pass
    out.sort(key=lambda d: float(d.get("ts") or 0), reverse=True)
    return out


def is_live(project, session_id: str, ttl: float = HEARTBEAT_TTL_S) -> bool:
    """True when THIS session id has a fresh heartbeat."""
    sid = str(session_id or "").strip()
    if not sid:
        return False
    return any(
        str(s.get("session_id") or "") == sid
        or str(s.get("host_session_id") or "") == sid
        for s in live_sessions(project, ttl=ttl, prune=False)
    )
