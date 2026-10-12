"""The UI server registry (``~/.c3/registry.json``) — one writer at a time.

Why this module exists (2.155.0)
--------------------------------
Three processes used to rewrite the registry from their own snapshot, with
no lock between them: every ``c3 ui`` server at startup (``cli/server``),
the hub's 60 s sweep, and — hottest of all — every ``GET /api/projects``
(``ProjectManager.list_projects``), which the hub page and C3 Desk poll.
Each one read the file, probed every port, and wrote back whatever it still
believed in. Two things followed:

- a server that registered between another writer's read and its write was
  overwritten away;
- a server that was merely busy (re-indexing after a burst of file changes)
  failed a 0.3 s connect or a 0.8 s ``/api/health`` and was dropped as dead.

An entry that is gone from the registry is invisible: the hub's sweep cannot
reap the server, and the SessionStart hook, seeing no UI, launches another.
On one machine that left six ``c3 ui`` processes (3.3 GB of commit) that
nothing owned, next to the three that were registered — and the box ran out
of commit, which is how the hub itself came to answer ``MemoryError``.

Rules now
---------
- every write holds ``registry.lock`` (msvcrt / flock), bounded;
- writers PATCH — upsert one entry, remove named ports — against a fresh read
  taken under the lock, never from a snapshot taken earlier;
- a reader never writes (``list_projects`` reports, the sweep decides);
- an entry is dead only when its port does not answer AND its pid is gone.
  A live pid behind a quiet port is a server starting up or busy; it stays.

Stdlib only; every function is best-effort and documents what it returns on
failure. The file format is unchanged: a JSON list of entries with ``port``,
``project_path``, ``project_name``, ``started_at``, ``pid``, ``owner`` and
``owner_session``.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from pathlib import Path

__all__ = [
    "default_path", "lock_path", "locked", "read", "write", "upsert", "ensure",
    "remove_ports", "pid_alive", "port_alive", "entry_alive",
]

LOCK_TIMEOUT_S = 10.0
_STILL_ACTIVE = 259


def default_path() -> Path:
    return Path.home() / ".c3" / "registry.json"


def lock_path(path: Path | None = None) -> Path:
    return Path(path or default_path()).with_suffix(".lock")


@contextlib.contextmanager
def locked(path: Path | None = None, timeout_s: float | None = None):
    """Hold the registry's cross-process lock for one read→patch→write.

    Raises ``TimeoutError`` when another writer keeps it past ``timeout_s``
    (default ``LOCK_TIMEOUT_S``); a caller that can retry later (the
    server's periodic re-assert) should let that pass rather than write
    unlocked and recreate the race.
    """
    if timeout_s is None:
        timeout_s = LOCK_TIMEOUT_S
    lock = lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "ab")  # never read or written; the fd holds the lock
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    try:
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not lock {lock.name} within {timeout_s:.0f}s")
                time.sleep(0.03)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def read(path: Path | None = None) -> list:
    """The registry's entries, or ``[]`` when the file is missing or unreadable."""
    target = Path(path or default_path())
    try:
        if not target.exists():
            return []
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def write(entries: list, path: Path | None = None) -> None:
    """Replace the file atomically. Callers hold ``locked()``."""
    target = Path(path or default_path())
    from services.atomic_json import write_json_atomic
    write_json_atomic(target, list(entries), ensure_ascii=False, fsync=False)


def _same_port(a, b) -> bool:
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return False


def upsert(entry: dict, path: Path | None = None) -> bool:
    """Add ``entry`` or replace the entry with the same port. Never raises.

    Returns False when the lock could not be taken or the write failed; the
    caller's next re-assert will try again.
    """
    port = entry.get("port")
    if not port:
        return False
    try:
        with locked(path):
            current = [e for e in read(path) if not _same_port(e.get("port"), port)]
            current.append(dict(entry))
            write(current, path)
        return True
    except Exception:
        return False


def ensure(entry: dict, path: Path | None = None) -> bool:
    """Re-assert ``entry``: write only when it is missing or names another pid.

    The periodic call from a running server. Returns True when the registry
    already carried the entry or now does.
    """
    port = entry.get("port")
    if not port:
        return False
    for e in read(path):
        if _same_port(e.get("port"), port) and e.get("pid") == entry.get("pid"):
            return True
    return upsert(entry, path)


def remove_ports(ports, path: Path | None = None, *, drop_portless: bool = False) -> int:
    """Remove the entries whose port is in ``ports``. Returns how many went.

    Decisions are made by the caller from its own probes; the removal is
    applied to a FRESH read under the lock, so an entry that was added in the
    meantime survives. Never raises; a lock timeout removes nothing.
    """
    wanted = set()
    for p in ports or ():
        try:
            wanted.add(int(p))
        except (TypeError, ValueError):
            continue
    if not wanted and not drop_portless:
        return 0
    try:
        with locked(path):
            current = read(path)
            kept = []
            for e in current:
                port = e.get("port")
                if not port:
                    if drop_portless:
                        continue
                    kept.append(e)
                    continue
                try:
                    if int(port) in wanted:
                        continue
                except (TypeError, ValueError):
                    pass
                kept.append(e)
            removed = len(current) - len(kept)
            if removed:
                write(kept, path)
            return removed
    except Exception:
        return 0


def pid_alive(pid) -> bool:
    """Best-effort: is this pid a running process? Unknown reads as ALIVE.

    A false "dead" drops a live server from the registry (the bug this module
    exists to end); a false "alive" keeps a dead entry one more sweep.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION — no elevation needed.
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == _STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def port_alive(port, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def entry_alive(entry: dict, *, port_probe=None, pid_probe=None) -> bool:
    """A registered server is alive when its port answers, or — failing
    that — when its pid is still running (starting up, or busy)."""
    port = entry.get("port")
    probe_port = port_probe or port_alive
    probe_pid = pid_probe or pid_alive
    if port and probe_port(port):
        return True
    return probe_pid(entry.get("pid"))
