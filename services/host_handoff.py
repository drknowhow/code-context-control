"""Hand a running MCP session over to a new host session id (2.155.0).

Why
---
``/clear`` (and an in-session ``/resume``) in Claude Code starts a NEW host
session — a new session id and a SessionStart hook with ``source: "clear"``
— but keeps the MCP servers it already has. C3's server learned its host id
once, from ``CLAUDE_CODE_SESSION_ID`` at process start, and kept it. So
after a clear:

- no ``C3 connected <id>`` notification followed the hook's ``Session
  started <id>``, and C3 Desk's sentinel reported "C3 did not connect" for a
  session whose runtime was serving fine;
- grants, leases and confirm holds were filed by the hooks under the new id
  and consumed by the server under the old one ("belongs to another
  session");
- the heartbeat and the host↔C3 link file named a session that had ended.

How
---
Both sides can read the process tree. The MCP server records the pid of the
host process it serves — its nearest ancestor that is not a shell or a
Python/launcher wrapper, i.e. ``claude.exe`` / ``node`` / ``codex`` — in its
heartbeat as ``host_pid``. The SessionStart hook is a child of that same
host, so it computes the same pid, finds the heartbeat(s) that carry it, and
writes one handoff file per server::

    .c3/live/handoffs/<c3_session_id>.json
    {"host_session_id": "<new>", "previous_host_session_id": "<old>",
     "host_pid": N, "start_source": "clear", "ts": <epoch>}

The server polls that path every few seconds (and on every tool call),
rebinds, re-links, beats, and writes ``C3 connected <new>`` — well inside
the desktop sentinel's 30 s window (``cli/mcp_server._rebind_host_session``).

When the process tree cannot be read (an unknown platform, a locked-down
host) the hook falls back to the one unambiguous case: a ``clear`` /
``resume`` with exactly one live session of this provider in the project.

Handoff files live in a sub-directory of ``.c3/live`` so that
``session_live.live_sessions`` (which lists ``*.json`` files there) never
mistakes one for a heartbeat. Stdlib only; nothing here raises.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

__all__ = [
    "WRAPPER_STEMS", "process_table", "ancestors", "host_process_id",
    "handoff_dir", "handoff_path", "write_handoff", "read_handoff",
    "clear_handoff", "hand_off",
]

# Executable stems (lower-case, no ``.exe``) that sit between a host and the
# programs it runs: shells, interpreters and C3's own launchers. The host is
# the first ancestor that is NOT one of these. ``node`` is deliberately absent
# — a Claude Code installed through npm IS node.
WRAPPER_STEMS = frozenset({
    "python", "pythonw", "python3", "python3w", "py", "pyw",
    "cmd", "bash", "sh", "zsh", "dash", "fish", "powershell", "pwsh", "conhost",
    "c3-mcp", "c3", "uv", "uvx", "npx", "env", "sudo", "sh.exe",
})

_HANDOFF_DIRNAME = "handoffs"
_MAX_DEPTH = 16


def _stem(name: str) -> str:
    base = os.path.basename(str(name or "")).strip().lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


# ── process tree ────────────────────────────────────────────────────────────

def _table_windows() -> dict:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    invalid = wintypes.HANDLE(-1).value
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == invalid:
        return {}
    table: dict = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if k32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID),
                                                   str(entry.szExeFile))
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    return table


def _table_proc() -> dict:
    table: dict = {}
    root = Path("/proc")
    for d in root.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text(encoding="utf-8", errors="replace")
            # "<pid> (<comm, may contain spaces>) <state> <ppid> ..."
            close = stat.rfind(")")
            comm = stat[stat.find("(") + 1:close]
            ppid = int(stat[close + 1:].split()[1])
            table[int(d.name)] = (ppid, comm)
        except Exception:
            continue
    return table


def _table_ps() -> dict:
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="],
                         capture_output=True, text=True, timeout=5).stdout
    table: dict = {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
        except ValueError:
            continue
    return table


def process_table() -> dict:
    """``{pid: (ppid, exe name)}`` for every process, or ``{}`` when unreadable."""
    try:
        if sys.platform.startswith("win"):
            return _table_windows()
        if Path("/proc").is_dir():
            table = _table_proc()
            if table:
                return table
        return _table_ps()
    except Exception:
        return {}


def ancestors(pid: int | None = None, table: dict | None = None,
              limit: int = _MAX_DEPTH) -> list:
    """``[(pid, exe), ...]`` from ``pid``'s parent upward. Cycle-safe (pids are
    reused, and a dead parent's pid can already belong to a descendant)."""
    table = process_table() if table is None else table
    cur = os.getpid() if pid is None else int(pid)
    out: list = []
    seen = {cur}
    for _ in range(max(0, int(limit))):
        row = table.get(cur)
        if not row:
            break
        parent = int(row[0])
        if parent <= 0 or parent in seen or parent not in table:
            break
        out.append((parent, table[parent][1]))
        seen.add(parent)
        cur = parent
    return out


def host_process_id(pid: int | None = None, table: dict | None = None) -> int:
    """The pid of the host (IDE / agent CLI) above ``pid``, or 0 when unknown.

    The host is the nearest ancestor whose executable is not a wrapper
    (``WRAPPER_STEMS``). Both the MCP server and a hook subprocess resolve to
    the same answer under the same Claude Code / Codex process, which is the
    whole point.
    """
    try:
        for ppid, name in ancestors(pid, table):
            if _stem(name) not in WRAPPER_STEMS:
                return int(ppid)
    except Exception:
        pass
    return 0


# ── handoff files ───────────────────────────────────────────────────────────

def handoff_dir(project) -> Path:
    from services.session_live import live_dir
    return live_dir(project) / _HANDOFF_DIRNAME


def handoff_path(project, c3_session_id: str) -> Path:
    from services.session_live import _file_name
    return handoff_dir(project) / _file_name(c3_session_id)


def write_handoff(project, c3_session_id: str, *, host_session_id: str,
                  previous_host_session_id: str = "", host_pid: int = 0,
                  start_source: str = "") -> Path | None:
    """Tell the server running ``c3_session_id`` its host id is now
    ``host_session_id``. Returns the path, or None when nothing was written."""
    c3_id = str(c3_session_id or "").strip()
    host = str(host_session_id or "").strip()
    if not c3_id or not host:
        return None
    try:
        from services.atomic_json import write_json_atomic
        path = handoff_path(project, c3_id)
        write_json_atomic(path, {
            "host_session_id": host,
            "previous_host_session_id": str(previous_host_session_id or ""),
            "host_pid": int(host_pid or 0),
            "start_source": str(start_source or ""),
            "ts": time.time(),
        }, fsync=False)
        return path
    except Exception:
        return None


def read_handoff(project, c3_session_id: str) -> dict:
    """The pending handoff for this server, or ``{}``."""
    c3_id = str(c3_session_id or "").strip()
    if not c3_id:
        return {}
    try:
        path = handoff_path(project, c3_id)
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not str(data.get("host_session_id") or "").strip():
            return {}
        return data
    except Exception:
        return {}


def clear_handoff(project, c3_session_id: str) -> bool:
    try:
        handoff_path(project, c3_session_id).unlink()
        return True
    except Exception:
        return False


# ── the hook side ───────────────────────────────────────────────────────────

def _same_provider(ide: str, provider: str) -> bool:
    from services.host_sessions import _provider_dir
    return _provider_dir(ide) == _provider_dir(provider)


def hand_off(project, provider: str, host_session_id: str,
             start_source: str = "", host_pid: int | None = None) -> list:
    """Called by the SessionStart hook. Returns the C3 session ids handed off.

    Candidates are the live sessions of ``provider`` in this project whose
    heartbeat carries the hook's own host pid. Without a usable pid on either
    side, a ``clear`` / ``resume`` with exactly one live session of that
    provider is handed off too — the only case with no ambiguity. A session
    that already carries ``host_session_id`` is left alone.
    """
    host = str(host_session_id or "").strip()
    if not host:
        return []
    try:
        from services import session_live
        beats = [b for b in session_live.live_sessions(project, prune=False)
                 if _same_provider(str(b.get("ide") or ""), provider)]
    except Exception:
        return []
    if not beats:
        return []
    pid = host_process_id() if host_pid is None else int(host_pid or 0)
    matched = []
    if pid:
        for b in beats:
            try:
                if int(b.get("host_pid") or 0) == pid:
                    matched.append(b)
            except (TypeError, ValueError):
                continue
    if not matched and str(start_source or "").lower() in ("clear", "resume") and len(beats) == 1:
        matched = beats
    done = []
    for b in matched:
        c3_id = str(b.get("session_id") or "")
        previous = str(b.get("host_session_id") or "")
        if not c3_id or previous == host:
            continue
        if write_handoff(project, c3_id, host_session_id=host,
                         previous_host_session_id=previous, host_pid=pid,
                         start_source=start_source):
            done.append(c3_id)
    return done
