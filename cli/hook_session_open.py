"""SessionStart hook: record that a host session opened (D0b, v2.126.0).

Claude Code's ``SessionStart`` event delivers on stdin::

    {
      "session_id": "<Claude Code UUID>",
      "transcript_path": "...",
      "cwd": "...",
      "hook_event_name": "SessionStart",
      "source": "startup" | "resume" | "clear" | "compact"
    }

Codex's ``SessionStart`` carries its thread id in the same ``session_id``
field and no ``source``; the dispatcher routes both here (event ``start``).

Records:

- ``{type: "session_open", host_session_id, source: <host>, start_source}``
  in ``.c3/activity_log.jsonl``. ``source`` is the HOST (``claude`` /
  ``codex`` / ``gemini``), matching ``session_end``; the payload's own
  ``source`` (startup / resume / clear) is kept as ``start_source``.
- a ``session`` notification (``kind="session"``, ``ref_id=host id``,
  ``info``, title ``Session started <short id>``).

``source: "compact"`` writes NOTHING: a compaction is the same session and
the same MCP process, and a "Session started" toast on every compaction
would teach the user to ignore the real ones.

The MCP server's own ``session_start`` row is untouched — it carries the C3
id and, when the host put its id in the environment, ``host_session_id``
too. This hook is what lets a client see the session before the runtime has
finished starting (~seconds), and it is the row the desktop's "MCP failed to
connect" sentinel starts its 30 s clock from.

Returns None always: SessionStart hook stdout becomes model context, and
this hook has nothing to tell the model.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import detect_host, find_project, log_hook_error  # noqa: E402
from cli.hook_session_end import short_id  # noqa: E402

_SILENT_SOURCES = {"compact"}


def run(payload: dict, project_path: Path | None = None):
    """Core logic — importable by the dispatcher and tests. Returns None."""
    start_source = str(payload.get("source") or "").strip().lower()
    if start_source in _SILENT_SOURCES:
        return None
    project = find_project(payload, project_path)
    if project is None:
        return None
    host = detect_host(payload)
    host_sid = str(payload.get("session_id") or "").strip()
    try:
        from services.activity_log import ActivityLog
        ActivityLog(str(project)).log("session_open", {
            "host_session_id": host_sid,
            "source": host,
            "start_source": start_source,
        })
    except Exception as exc:
        log_hook_error("hook_session_open", exc)
    try:
        from services.notifications import notify
        notify(project, agent="session", severity="info",
               title=f"Session started {short_id(host_sid)}",
               message=(f"{host} session {host_sid or '?'} started"
                        + (f" ({start_source})" if start_source else "")),
               kind="session", ref_id=host_sid)
    except Exception:
        pass
    return None


def main() -> None:
    import json
    try:
        data = json.load(sys.stdin)
    except Exception as exc:
        log_hook_error("hook_session_open", exc)
        sys.exit(0)
    try:
        run(data)
    except Exception as exc:
        log_hook_error("hook_session_open", exc)
    sys.exit(0)


if __name__ == "__main__":
    main()
