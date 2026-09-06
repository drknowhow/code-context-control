"""SessionEnd hook: record that a host session ended (D0b, v2.126.0).

Claude Code's ``SessionEnd`` event delivers on stdin::

    {
      "session_id": "<Claude Code UUID>",
      "transcript_path": "...",
      "cwd": "...",
      "hook_event_name": "SessionEnd",
      "reason": "clear" | "logout" | "prompt_input_exit" | "other"
    }

Codex's ``SessionEnd`` carries its thread id in the same ``session_id``
field; the dispatcher routes both here (``hook_dispatch._routes``, event
``end``).

Why this exists: this repo's activity log held 525 ``session_start`` rows
and 14 ``session_save`` rows — ``session_save`` is only written when a human
ends a session from the hub, so for a client the end of a conversation was
invisible. Two records fix that:

- ``{type: "session_end", session_id: <C3 id if known>, host_session_id,
  reason, source}`` appended to ``.c3/activity_log.jsonl``. The C3 id comes
  from the link file the MCP server wrote at session start
  (``services.host_sessions``); it is ``""`` when the server never learned
  the host id.
- a ``session`` notification (``kind="session"``, ``ref_id=host id``,
  severity ``info``, title ``Session ended <short id>``) in
  ``.c3/notifications.jsonl`` — the write that wakes a ``/feed?wait=``
  client.

Runs in a short-lived subprocess with a few seconds of budget; every step is
best-effort and nothing here can fail the session that is already ending.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import detect_host, find_project, log_hook_error  # noqa: E402

SHORT_ID_CHARS = 8


def short_id(session_id: str) -> str:
    return str(session_id or "")[:SHORT_ID_CHARS] or "?"


def run(payload: dict, project_path: Path | None = None):
    """Core logic — importable by the dispatcher and tests. Returns None."""
    project = find_project(payload, project_path)
    if project is None:
        return None
    host = detect_host(payload)
    host_sid = str(payload.get("session_id") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    c3_sid = ""
    try:
        from services.host_sessions import linked_c3_session_id
        c3_sid = linked_c3_session_id(project, host, host_sid)
    except Exception:
        c3_sid = ""
    try:
        from services.activity_log import ActivityLog
        ActivityLog(str(project)).log("session_end", {
            "session_id": c3_sid,
            "host_session_id": host_sid,
            "reason": reason,
            "source": host,
        })
    except Exception as exc:
        log_hook_error("hook_session_end", exc)
    try:
        from services.notifications import notify
        notify(project, agent="session", severity="info",
               title=f"Session ended {short_id(host_sid)}",
               message=(f"{host} session {host_sid or '?'} ended"
                        + (f" ({reason})" if reason else "")
                        + (f"; C3 session {c3_sid}" if c3_sid else "")),
               kind="session", ref_id=host_sid)
    except Exception:
        pass
    return None


def main() -> None:
    import json
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        log_hook_error("hook_session_end", exc)
        sys.exit(0)
    try:
        run(data)
    except Exception as exc:
        log_hook_error("hook_session_end", exc)
    sys.exit(0)


if __name__ == "__main__":
    main()
