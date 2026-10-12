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

It also brings up the project's UI server when one is not already running
(``ensure_ui`` below, v2.128.0) — the only place in C3 where "a session
started" causes "the UI is up". It needs the project to be registered with
the hub, and it skips a project that already has a live UI port, so a
``resume`` or ``clear`` never stacks a second server. Opt out with
``C3_NO_UI_AUTOSTART=1`` or
``.c3/config.json → {"ui": {"autostart_on_session": false}}``.

A ``clear`` (or an in-session ``resume``) keeps the MCP server it already
has, under a host session id that just ended. This hook finds that server
by the host pid both of them share and hands it the new id
(``services.host_handoff``, v2.155.0); the server then writes the
``C3 connected <new id>`` row the desktop sentinel is waiting for.

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

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli._hook_utils import detect_host, find_project, log_hook_error  # noqa: E402
from cli.hook_session_end import short_id  # noqa: E402

_SILENT_SOURCES = {"compact"}
# How long one session's UI launch holds the project against the next. A
# server lands in the registry only after it binds its port, a few seconds
# after Popen; this covers that gap with room to spare. After it, the live
# registry entry (``_ui_running``) is the guard again.
_LAUNCH_GRACE_S = 90.0


def _ui_autostart_enabled(project: Path) -> bool:
    """Opt-outs, in the order a user reaches for them."""
    if os.environ.get("C3_NO_UI_AUTOSTART") or os.environ.get("C3_BENCHMARK_MODE"):
        return False
    try:
        cfg = json.loads((project / ".c3" / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return True  # no readable config is not a reason to withhold the UI
    ui = cfg.get("ui")
    if isinstance(ui, dict) and "autostart_on_session" in ui:
        return bool(ui.get("autostart_on_session"))
    return True


def _is_registered(pm, project: Path) -> bool:
    """True when the hub knows this project.

    The UI server is a hub-level surface: a directory the user never
    registered gets nothing automatic (``c3 ui`` still works). This is also
    what keeps a test fixture or a scratch clone from growing a web server.
    """
    target = os.path.normcase(str(project))
    for entry in pm._read_projects():
        raw = str(entry.get("path") or "")
        if raw and os.path.normcase(raw) == target:
            return True
        try:
            if raw and os.path.normcase(str(Path(raw).resolve())) == target:
                return True
        except Exception:
            continue
    return False


def _ui_running(pm, project: Path) -> bool:
    """True when a live UI server is already registered for this project.

    This is what keeps `resume` / `clear` — both real SessionStart events —
    from stacking a second server on every reconnect. A registered server
    whose port is quiet but whose pid is running is still "running": it is
    starting up, or busy, and a second one would only be the leak this
    check exists to prevent (2.155.0).
    """
    target = os.path.normcase(str(project))
    for entry in pm._read_registry():
        raw = str(entry.get("project_path") or "")
        if not raw:
            continue
        try:
            same = os.path.normcase(str(Path(raw).resolve())) == target
        except Exception:
            same = os.path.normcase(raw) == target
        if not same or not entry.get("port"):
            continue
        if pm._port_alive(entry["port"]):
            return True
        if entry.get("pid") and pm._pid_alive(entry.get("pid")):
            return True
    return False


def _launch_lock(project: Path) -> Path:
    return project / ".c3" / "ui_launch.lock"


def _claim_launch(project: Path) -> bool:
    """Atomically claim the right to launch this project's UI server.

    ``_ui_running`` only sees a server once it has registered its port, a
    few seconds after launch. Sessions that open inside that window (an
    agent spawning several at once, or a resume racing a startup) each saw
    no live UI and each launched one, which is how a project ended up with
    two servers. The first claimant creates ``.c3/ui_launch.lock`` with
    O_EXCL; anyone else inside ``_LAUNCH_GRACE_S`` stands down.

    A lock older than the grace window is stale (the launch died, or the
    server since exited). It is taken over by renaming it to a unique name,
    which only one contender can do, then the create is retried. If the
    lock cannot be handled at all the answer is True, which is the
    behaviour from before this lock existed: a missing UI is worse than a
    rare duplicate.
    """
    lock = _launch_lock(project)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return True
    for _ in range(3):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue  # released between our create and stat; retry
            except Exception:
                return True
            if age < _LAUNCH_GRACE_S:
                return False
            stale = lock.with_name(f"ui_launch.stale-{uuid.uuid4().hex}")
            try:
                os.rename(str(lock), str(stale))
            except FileNotFoundError:
                continue  # another contender took it over first
            except Exception:
                return False
            try:
                stale.unlink()
            except Exception:
                pass
            continue
        except Exception:
            return True
        try:
            os.write(fd, f"{os.getpid()} {time.time():.0f}\n".encode("ascii"))
        finally:
            os.close(fd)
        return True
    return False


def ensure_ui(project: Path, host_session_id: str = "") -> str:
    """Bring up this project's UI server for the session that just opened.

    Nothing else in C3 connected "a session started" to "the UI is running":
    the hub's sweep only chases projects flagged `autostart_ui`, and before
    2.128.0 it ran once at hub startup — so a UI was only ever there because
    an earlier session had left one behind.

    Returns a short reason string (tests and diagnostics read it). Never
    raises and never prints — SessionStart stdout becomes model context.
    """
    try:
        project = Path(project).resolve()
    except Exception:
        project = Path(project)
    if not _ui_autostart_enabled(project):
        return "disabled"
    try:
        from services.project_manager import ProjectManager
        pm = ProjectManager()
        if not _is_registered(pm, project):
            return "unregistered"
        if _ui_running(pm, project):
            return "already-running"
        if not _claim_launch(project):
            return "launch-pending"
        # launch_session copies os.environ into the detached child, which is
        # where cli/server._register_session reads the ownership from.
        os.environ["C3_UI_OWNER"] = "session"
        if host_session_id:
            os.environ["C3_UI_OWNER_SESSION"] = host_session_id
        result = pm.launch_session(str(project))
        if result.get("launched"):
            return "launched"
        return f"failed: {result.get('error', '')}".strip()
    except Exception as exc:
        log_hook_error("hook_session_open", exc)
        return "error"


def hand_off_runtime(project: Path, host: str, host_sid: str, start_source: str) -> list:
    """Point the MCP server this host already runs at its new session id.

    Returns the C3 session ids handed off (usually one, often none: a
    ``startup`` has no server yet). Never raises.
    """
    if not host_sid:
        return []
    try:
        from services.host_handoff import hand_off
        return hand_off(project, host, host_sid, start_source)
    except Exception as exc:
        log_hook_error("hook_session_open", exc)
        return []


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
    hand_off_runtime(project, host, host_sid, start_source)
    ensure_ui(project, host_sid)
    return None


def main() -> None:
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
