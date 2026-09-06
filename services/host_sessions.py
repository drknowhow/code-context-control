"""One join key between a host session and the C3 session it drives.

Why this exists (D0b, v2.126.0)
-------------------------------
Two ids describe one conversation and, until now, never met on disk:

- the HOST id — the Claude Code UUID (``CLAUDE_CODE_SESSION_ID``, and the
  ``session_id`` of every hook payload), or the Codex thread id;
- the C3 id — ``SessionManager.start_session``'s ``YYYYMMDD_HHMMSS_<hex>``,
  which the MCP server stamps on ``session_start`` and on the saved
  ``.c3/sessions/session_*.json``.

Hooks write the first; the MCP server writes the second; ``tool_call``
activity rows carried neither. A desktop client that wants "the session
that ended was the one whose jobs I am showing" needs the two to join.

The link is a tiny per-session file the MCP server writes as soon as it
knows both ids and a hook reads once at session end:

    .c3/host_sessions/<provider>/<sha256(host id)>.link.json
    {"provider": "claude", "host_session_id": "...", "session_id": "...",
     "started": "<ISO UTC>"}

The directory and the hashed name follow the Codex handoff checkpoint that
already lives at ``.c3/host_sessions/codex/<sha256>.json``; the ``.link``
suffix keeps the two apart. Stdlib only, never raises: the join is a
convenience, and losing it must never cost the row it annotates.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["link_path", "write_link", "read_link", "linked_c3_session_id"]

_DIRNAME = "host_sessions"
_SUFFIX = ".link.json"


def _provider_dir(provider: str) -> str:
    name = str(provider or "").strip().lower()
    aliases = {"claude-code": "claude", "claude": "claude", "codex": "codex",
               "gemini": "gemini", "antigravity": "gemini"}
    return aliases.get(name, name or "unknown")


def link_path(project_path, provider: str, host_session_id: str) -> Path:
    key = hashlib.sha256(str(host_session_id).encode("utf-8")).hexdigest()
    return (Path(project_path) / ".c3" / _DIRNAME / _provider_dir(provider)
            / (key + _SUFFIX))


def write_link(project_path, provider: str, host_session_id: str,
               c3_session_id: str) -> Path | None:
    """Record ``host id -> C3 id`` for this project. Returns the path, or None."""
    host = str(host_session_id or "").strip()
    c3_id = str(c3_session_id or "").strip()
    if not host or not c3_id:
        return None
    try:
        from services.atomic_json import write_json_atomic
        path = link_path(project_path, provider, host)
        write_json_atomic(path, {
            "provider": _provider_dir(provider),
            "host_session_id": host,
            "session_id": c3_id,
            "started": datetime.now(timezone.utc).isoformat(),
        })
        return path
    except Exception:
        return None


def read_link(project_path, provider: str, host_session_id: str) -> dict:
    """The stored link, or ``{}`` when there is none (or it is unreadable)."""
    host = str(host_session_id or "").strip()
    if not host:
        return {}
    try:
        path = link_path(project_path, provider, host)
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("host_session_id") != host:
            return {}
        return data
    except Exception:
        return {}


def linked_c3_session_id(project_path, provider: str, host_session_id: str) -> str:
    """The C3 session id joined to ``host_session_id``, or ``""``."""
    return str(read_link(project_path, provider, host_session_id).get("session_id") or "")
