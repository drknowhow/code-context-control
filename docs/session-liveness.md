# Session liveness and the project UI

*Since 2.128.0.*

Two different things are called a "session" in C3, and before 2.128.0 they
never met:

| | what it is | who starts it |
| --- | --- | --- |
| **agent session** | the MCP server serving your IDE | the IDE, when it loads the `c3` MCP server |
| **UI session** | the per-project web UI (`c3 ui`, its own port) | you, or the hub |

Starting an IDE session did not start a UI, and the hub's "live" light was
inferred from activity rows rather than from the process. This document is the
contract that replaced both.

## A session says it is alive

The MCP server writes one heartbeat file per session:

```
.c3/live/<c3_session_id>.json   {session_id, host_session_id, ide, pid, ts}
```

It is written once right after the `session_start` row and refreshed every
60 s (`services/session_live.HEARTBEAT_INTERVAL_S`). A reader treats a file
whose `ts` is within 180 s (`HEARTBEAT_TTL_S` — three missed beats) as a live
session. Clean shutdown removes the file before the slow teardown steps run,
so a reader polling during shutdown already sees the session as gone; a crash
or a `taskkill` leaves a stale file, which the next reader ignores and prunes.

`pid` is recorded for diagnostics only. Liveness is decided by the timestamp:
pids are reused across reboots, and there is no cheap portable "is this pid
alive" call (C3 does not depend on psutil).

One file per session id, not per project — so two IDEs, or a repo and its
worktrees, each report themselves. `ProjectManager.list_projects` exposes
`sessions` and `session_count`; the hub card shows `MCP×N`.

### Why not just read the activity log

That was the old behaviour, and it is still the fallback for a session with no
heartbeat (an older C3 in another checkout, or a process killed without
cleanup). Its two failure modes are the reason the heartbeat exists:

- **a closed IDE read "live" for up to 20 minutes** — a session ended only on
  a `session_save` row, which is written when a human clicks "end session" in
  the hub. The `session_end` row the SessionEnd hook has written since 2.126.0
  was read by nothing. It is honoured now (matched on `session_id` *or*
  `host_session_id`, because the hook writes an empty C3 id when no link file
  exists yet);
- **a quiet session read dead** — no tool call for 20 minutes meant "gone",
  even with the MCP process right there. A fresh heartbeat now outranks any
  amount of silence, and it also outranks a `session_save`: the process is
  still serving, and a bookkeeping row does not kill it;
- **a busy session read dead too**, for the opposite reason — the lookup used
  `get_recent(limit=1, event_type=...)`, which scans only the last 100 lines
  of the log. In this repo the running session's `session_start` sat 319 lines
  from the end of a 20,825-line log, so nothing was found at all. Rare-event
  lookups go through `ActivityLog.find_last` since 2.128.1, which scans
  backwards until it finds the row (and is faster than the call it replaced).

## The UI follows the session

The SessionStart hook (`cli/hook_session_open.ensure_ui`) launches the
project's UI server when one is not already running. In order, it declines to
do anything when:

1. `C3_NO_UI_AUTOSTART=1` or `C3_BENCHMARK_MODE` is set;
2. `.c3/config.json` says `{"ui": {"autostart_on_session": false}}`;
3. the project is not registered with the hub (a scratch clone gets nothing —
   `c3 ui` still works);
4. the registry already has a live port for this project. This is what keeps
   `resume` and `clear` — both real SessionStart events — from stacking a
   second server.

`source: compact` is not a new session and never reaches any of this.

## Ownership and reaping

Registry entries (`~/.c3/registry.json`) carry `pid`, `owner` and
`owner_session`. `owner` is `session` when a session start launched the server
and `user` for `c3 ui`, the hub's **Open UI** button and the TUI.

The hub sweeps every 60 s (`ProjectManager.sweep_registry`): dead entries
are dropped, and a **session**-owned server is stopped once its project has
no heartbeat left *and* its activity log has not been touched for
`ui_reap_minutes`.

An entry is dead only when its port does not answer **and** its pid is gone
(2.155.0). A quiet port behind a live pid is a server that is still starting
or busy re-indexing; it stays. Until 2.155.0 one missed 0.3 s connect was
enough to drop it — and three writers (every `c3 ui` at startup, the sweep,
and every `GET /api/projects`, which the hub page and C3 Desk poll) rewrote
the whole file from their own snapshot with no lock, so a server that
registered while another writer was probing was simply written out. A
dropped server is invisible: nothing reaps it, and the next SessionStart
launches another. One machine had six such orphans (3.3 GB) beside three
registered servers, and ran out of commit.

The registry is one writer at a time now (`services/ui_registry.py`): every
write holds `~/.c3/registry.lock`, writers patch a fresh read (upsert one
entry, remove named ports) rather than replace the file, `list_projects`
only reads, and a running server re-asserts its own entry every 60 s so a
lost one comes back on its own. The SessionStart hook also takes a launch
lock (`.c3/ui_launch.lock`, created with O_EXCL, honoured for 90 s) so two
sessions opening together do not each start a server before the first has
bound its port.

```jsonc
// ~/.c3/hub_config.json
{ "ui_reap_minutes": 30 }   // 0 = never reap
```

A `user`-owned server is never stopped automatically. Without this, every
session that ever ran left a web server behind until the next reboot.

The same sweep launches the UI for projects with `autostart_ui` that have a
live session but no UI — so a hub started *after* the IDE, or a UI that died
mid-session, recovers on its own. Before 2.128.0 that autostart ran once, two
seconds after the hub started, and never again.

## A session changes host id: `/clear`

`/clear` (and an in-session `/resume`) in Claude Code starts a NEW host
session — a new session id, a SessionStart hook with `source: "clear"` — but
keeps the MCP servers it already has. Until 2.155.0 C3's server kept the id
it had read from `CLAUDE_CODE_SESSION_ID` at process start, so after a clear
no `C3 connected <new id>` row followed the hook's `Session started <new
id>`, and C3 Desk's sentinel reported "C3 did not connect" for a runtime that
was serving fine; grants and confirm holds filed by the hooks under the new
id were consumed under the old one ("belongs to another session").

Both sides can read the process tree. The server records the pid of the host
it serves (its nearest ancestor that is not a shell or a Python/launcher
wrapper, i.e. `claude.exe` / `node` / `codex`) in its heartbeat as
`host_pid`. The SessionStart hook is a child of that same host, computes the
same pid, and writes one handoff file per matching server:

```
.c3/live/handoffs/<c3_session_id>.json
{"host_session_id": "<new>", "previous_host_session_id": "<old>",
 "host_pid": N, "start_source": "clear", "ts": <epoch>}
```

The server's heartbeat thread looks for that file every 5 s (and every tool
call looks once, before the access guard binds the session), rebinds in
place — session record, host env var, link file, heartbeat, a
`session_rebind` activity row — and writes `C3 connected <new id>`, inside
the desktop sentinel's 30 s window. Without a readable process tree the hook
falls back to the one unambiguous case: a clear / resume with exactly one
live session of that provider in the project.
(`services/host_handoff.py`, `cli/mcp_server._rebind_host_session`.)

## Checking it by hand

```bash
ls .c3/live                      # one file per live session in this project
ls .c3/live/handoffs             # a /clear the server has not picked up yet
python -c "from services import session_live as s; print(s.live_sessions('.'))"
python -c "import json,pathlib; print(json.loads((pathlib.Path.home()/'.c3'/'registry.json').read_text()))"
```

The hooks and the MCP server load their code at process start, so a change
here is not live in the session that made it: a new IDE session is required,
and the hub must be restarted.
