"""Background shell jobs (c3_shell remediation, S3).

Why this exists
---------------
Measured 2026-09-04: 44% of c3_shell wall time is in calls over 60 s, and the
MCP client kills a tool call at 120 s (c3_shell clamps to 115 s and says so).
So the full C3 test suite and every long build leave C3 for native Bash and
lose the ledger, the telemetry and the spill store for exactly the jobs that
matter most. A thread inside the MCP process is not durable — the server is
started per session and dies with it — so a job is run by a DETACHED
SUPERVISOR process that outlives the server; the MCP side only starts,
polls, tails and cancels it, and never waits on it.

Design decision (Cod, 2026-09-04): keep background execution, but as a
separate phase and API — c3_shell_job(start|status|tail|cancel) — never as an
auto-conversion of a synchronous timeout. Prefer c3_ci for known test
workflows; jobs cover arbitrary builds and device commands.

Layout
------
    <root>/<project_id>/<session_id|nosession>/jobs/<job-id>.json
    <root>/<project_id>/<session_id|nosession>/jobs/<job-id>.supervisor.log
    <root>/<project_id>/<session_id|nosession>/jobs/<job-id>.cancel   (marker)
    <root>/.spool/<output-id>.{stdout,stderr}.part                     while running
    <root>/<project_id>/<session_id|nosession>/<output-id>.{stdout,stderr,meta.json}

``<root>`` is the S1 spill store (``~/.c3/shell_out``, override
``C3_SHELL_OUT_DIR``; see services/shell_output.py). A job's output IS a
spill: it is promoted ALWAYS when the job ends — the output is the
deliverable even when it is small — and paged back with
``c3_shell(output_id=...)``. Job ids are ``j-`` + 12 hex from ``secrets``.

State machine
-------------
    queued ──► running ──► done | failed | timeout | cancelled
       │           │
       └───────────┴──► lost   (supervisor gone: child killed if provably still
                                ours, spool promoted if it exists)

The supervisor is the only writer of ``running`` and of the four ordinary
terminal states. ``cancel`` never edits the json while the supervisor is
alive: it touches a ``<job-id>.cancel`` marker, kills the child tree, and
lets the supervisor record ``cancelled`` — so two processes never race on
one file. ``lost`` is written by whichever caller notices the supervisor is
gone (status, tail, list, cancel, reap).

PID reuse
---------
A pid alone is not an identity: once the child exits the OS may hand the
number to an unrelated process. Every recorded pid is stored with the
process creation time as the OS reports it, plus how it was read:
Windows ``GetProcessTimes`` creation FILETIME via ctypes (fallback:
PowerShell ``(Get-Process -Id N).StartTime.ToFileTime()``), Linux
``/proc/<pid>/stat`` starttime, other POSIX ``ps -o lstart=``. A kill is
sent ONLY when the live creation time (read the same way) still matches;
a mismatch is reported and nothing is signalled.

Secrets
-------
Injected credentials never touch the command line, the environment of the
supervisor, or the job json. The supervisor receives them on its STDIN as
one JSON payload (the parent writes it and closes the pipe), registers the
values with ``services.credential_store`` so the streaming redactor scrubs
them before a byte reaches the spool, and keeps them only in memory. The S3
brief suggested a private env file deleted right after spawn; a pipe has no
on-disk window at all, which is why it is used instead.

Authorization
-------------
``status``/``tail``/``cancel`` resolve a job under the same rules
``ShellOutputStore.resolve`` applies to an output: same project, same
session, current Access Guard rules re-applied to the job's cwd and to the
paths its guard scan saw. Unknown, malformed, other-project and
other-session ids share ONE wording so a probe learns nothing.

Stdlib only. Windows-first; POSIX branches present.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.shell_output import (
    DEFAULT_READ_BYTES,
    RETENTION_DAYS,
    SWEEP_INTERVAL_S,
    CaptureStats,
    ShellCapture,
    ShellOutputStore,
    _clip_line,
    _display,
    _iso,
    _mkdir_private,
    _norm_project,
    _parse_iso,
    _restrict_file,
    _session_dir,
    _write_json_atomic,
    project_id,
    store_root,
)

__all__ = [
    "JOBS_DIRNAME", "DEFAULT_TIMEOUT_S", "MAX_TIMEOUT_S", "STATUSES", "TERMINAL",
    "JobState", "JobAccessError", "JobStore", "JOB_ROW_FIELDS", "job_row",
    "process_start_time", "process_alive", "kill_tree_by_pid",
    "new_job_id", "supervise", "main",
]

JOBS_DIRNAME = "jobs"
DEFAULT_TIMEOUT_S = 30 * 60
MAX_TIMEOUT_S = 6 * 3600
STATUSES = ("queued", "running", "done", "failed", "timeout", "cancelled", "lost")
TERMINAL = frozenset({"done", "failed", "timeout", "cancelled", "lost"})
PAYLOAD_TIMEOUT_S = 20.0       # the supervisor waits this long for its stdin payload
ACK_WAIT_S = 3.0               # start() waits at most this long to see the supervisor's 'running'
CANCEL_WAIT_S = 5.0            # cancel() waits at most this long for the supervisor to record it
QUEUED_GRACE_S = 60.0          # a queued job with no supervisor pid older than this is lost
JOBS_SWEEP_MARKER = ".last_jobs_sweep"
SUPERVISOR_LOG_TAIL = 1200     # bytes of the supervisor log quoted into an error

_JOB_ID_RE = re.compile(r"^j-[0-9a-f]{12}$")
_JOB_FILE_RE = re.compile(r"^(j-[0-9a-f]{12})\.json$")
_PID_DIR_RE = re.compile(r"^[0-9a-f]{12}$")
_NOT_FOUND = "job {jid}: not found for this project and session"
_STREAMS = ("stdout", "stderr")

# Popen handles of supervisors this process spawned. Polled (and dropped when
# finished) on every start/status so a POSIX supervisor never lingers as a
# zombie of the MCP server; nothing here ever WAITS on one.
_SPAWNED: list = []


def new_job_id() -> str:
    """``j-`` + 12 hex, opaque and unguessable."""
    return "j-" + secrets.token_hex(6)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Process identity: pid + creation time ───────────────────────────────────

def process_start_time(pid) -> tuple[str, str]:
    """``(creation time as the OS reports it, how it was read)``; ``("", "")`` when not alive.

    The value is kept as a string so the json stays uniform across sources
    and platforms; it is only ever compared for equality against a value
    read the same way.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return "", ""
    if pid <= 0:
        return "", ""
    if sys.platform == "win32":
        value = _win_creation_filetime(pid)
        if value is not None:
            return str(value), "GetProcessTimes"
        value = _win_creation_powershell(pid)
        if value is not None:
            return str(value), "powershell"
        return "", ""
    value = _proc_starttime(pid)
    if value is not None:
        return str(value), "procfs"
    value = _ps_lstart(pid)
    if value:
        return value, "ps"
    return "", ""


def process_alive(pid, start_time: str, source: str = "") -> bool:
    """True when ``pid`` is alive AND its creation time still equals ``start_time``.

    A recorded value read one way and a live value read another are not
    comparable, so that case is "not provably ours" and answers False —
    the conservative side for a kill decision.
    """
    if not start_time:
        return False
    live, live_source = process_start_time(pid)
    if not live:
        return False
    if source and live_source != source:
        return False
    return str(live) == str(start_time)


def _win_creation_filetime(pid: int):
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        process_query_limited_information = 0x1000
        handle = k32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
            ok = k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                     ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            if exited.dwLowDateTime or exited.dwHighDateTime:
                return None          # exited; an open handle elsewhere keeps the pid reserved
            return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def _win_creation_powershell(pid: int):
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-Process -Id {int(pid)} -ErrorAction Stop).StartTime.ToFileTime()"],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    out = (res.stdout or "").strip()
    return int(out) if res.returncode == 0 and out.isdigit() else None


def _proc_starttime(pid: int):
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("ascii", "replace")
    except OSError:
        return None
    _, _, rest = raw.rpartition(")")
    fields = rest.split()
    if len(fields) < 20 or fields[0] == "Z":
        return None
    return fields[19]                                 # starttime, clock ticks since boot


def _ps_lstart(pid: int):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    except OSError:
        return None
    try:
        res = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=10, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    out = (res.stdout or "").strip()
    return out or None


def kill_tree_by_pid(pid) -> str:
    """Best-effort kill of ``pid`` and its descendants; returns a short note.

    Callers MUST have checked ``process_alive`` with the recorded creation
    time first — this function trusts the number it is given.
    """
    pid = int(pid)
    if sys.platform == "win32":
        try:
            res = subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True,
                                 timeout=15, stdin=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "taskkill /F /T" if res.returncode == 0 else f"taskkill rc={res.returncode}"
        except Exception as exc:
            return f"taskkill failed: {type(exc).__name__}"
    import signal
    try:
        os.killpg(pid, signal.SIGKILL)               # children start in their own session (pgid == pid)
        return "SIGKILL to the process group"
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGKILL)
            return "SIGKILL"
        except OSError as exc:
            return f"kill failed: {type(exc).__name__}"


# ── State ───────────────────────────────────────────────────────────────────

class JobAccessError(Exception):
    """Refusal from ``JobStore.resolve`` — ``str(exc)`` is the one-line reason."""


@dataclass
class JobState:
    id: str
    project_id: str
    project_path: str
    session_id: str
    cmd_sha256: str
    cmd_display: str
    cwd: str
    timeout_s: int
    created_at: str
    status: str = "queued"
    started_at: str = ""
    finished_at: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int | None = None
    supervisor_pid: int | None = None
    supervisor_start_time: str = ""
    supervisor_start_source: str = ""
    supervisor_argv: list = field(default_factory=list)
    child_pid: int | None = None
    child_start_time: str = ""
    child_start_source: str = ""
    shell: str = ""
    output_id: str = ""
    spool: dict = field(default_factory=dict)        # {"stdout": path, "stderr": path} while running
    stdout: dict = field(default_factory=dict)       # bytes/lines/longest_line/sha256 once finished
    stderr: dict = field(default_factory=dict)
    guard: dict = field(default_factory=dict)        # {"cwd": ..., "paths": [...]} — re-checked on every resolve
    acl_applied: bool = False
    creds: list = field(default_factory=list)        # credential NAMES only, never values
    cancel_requested: bool = False
    error: str = ""
    dir: str = field(default="", repr=False, compare=False)   # jobs dir; not persisted

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in _JOB_KEYS}

    @classmethod
    def from_dict(cls, data: dict, directory: Path | str = "") -> "JobState":
        kwargs = {k: data[k] for k in _JOB_KEYS if k in data}
        return cls(dir=str(directory), **kwargs)

    @property
    def path(self) -> Path:
        return Path(self.dir) / f"{self.id}.json"

    @property
    def log_path(self) -> Path:
        return Path(self.dir) / f"{self.id}.supervisor.log"

    @property
    def cancel_marker(self) -> Path:
        return Path(self.dir) / f"{self.id}.cancel"

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def elapsed_s(self, now=None) -> float:
        start = _parse_iso(self.started_at or self.created_at)
        if start is None:
            return 0.0
        end = _parse_iso(self.finished_at) if self.finished_at else None
        end = end or (now if isinstance(now, datetime) else _now())
        return max((end - start).total_seconds(), 0.0)


_JOB_KEYS = tuple(f.name for f in dataclasses.fields(JobState) if f.name != "dir")


def _load_job(path: Path) -> JobState | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return JobState.from_dict(data, path.parent)
    except (OSError, ValueError, KeyError, TypeError):
        return None


class _SpoolCapture:
    """Duck-typed stand-in for ``ShellCapture`` so a lost job's spool can be promoted."""

    def __init__(self, output_id: str, stdout_path, stderr_path):
        self.finished = True
        self.output_id = output_id
        self.stdout_path = Path(stdout_path)
        self.stderr_path = Path(stderr_path)
        self.stats = CaptureStats()
        for name, path in (("stdout", self.stdout_path), ("stderr", self.stderr_path)):
            st = getattr(self.stats, name)
            st.bytes, st.lines, st.longest_line, st.sha256 = _scan_file(path)

    def discard(self) -> None:
        for path in (self.stdout_path, self.stderr_path):
            try:
                path.unlink()
            except OSError:
                pass


def _scan_file(path: Path) -> tuple[int, int, int, str]:
    h = hashlib.sha256()
    size = lines = longest = 0
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                h.update(raw)
                size += len(raw)
                lines += 1
                n = len(raw.rstrip(b"\r\n").decode("utf-8", "replace"))
                if n > longest:
                    longest = n
    except OSError:
        pass
    return size, lines, longest, h.hexdigest()


def _tail_file(path: Path, lines: int, max_bytes: int) -> tuple[list[tuple[int, str]], int, int]:
    """Last ``lines`` lines of a (possibly still growing) file, budgeted newest-first.

    Returns ``(kept [(lineno, text)], total_lines, requested_available)``.
    """
    window: deque = deque(maxlen=max(int(lines), 1))
    n = 0
    try:
        with open(path, "rb") as fh:
            for n, raw in enumerate(fh, 1):
                window.append((n, _clip_line(raw.decode("utf-8", "replace").rstrip("\r\n"))))
    except OSError:
        return [], 0, 0
    kept: list[tuple[int, str]] = []
    used = 0
    for m, line in reversed(window):
        size = len(line.encode("utf-8")) + 1
        if used + size > max_bytes and kept:
            break
        kept.append((m, line))
        used += size
    kept.reverse()
    return kept, n, len(window)


def _log_tail(path: Path, limit: int = SUPERVISOR_LOG_TAIL) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace").strip()


def _reap_spawned() -> None:
    for proc in list(_SPAWNED):
        try:
            if proc.poll() is not None:
                _SPAWNED.remove(proc)
        except Exception:
            try:
                _SPAWNED.remove(proc)
            except ValueError:
                pass


# ── Store ───────────────────────────────────────────────────────────────────

class JobStore:
    """Job records under ``<root>/<project_id>/<session>/jobs/``; spawns and tracks supervisors.

    ``apply_acl=False`` skips the user-only permission step (tests).
    """

    def __init__(self, root: Path | str | None = None, *, apply_acl: bool = True):
        self.root = Path(root) if root is not None else store_root()
        self.apply_acl = apply_acl
        self.output_store = ShellOutputStore(self.root, apply_acl=apply_acl)

    # -- layout --
    def _jobs_dir(self, project_path, session_id) -> tuple[Path, str, str]:
        pid = project_id(project_path)
        sid = "" if session_id is None else str(session_id)
        return self.root / pid / _session_dir(sid) / JOBS_DIRNAME, pid, sid

    def locate(self, job_id: str) -> Path | None:
        """The json of ``job_id`` anywhere under the root (supervisor entry; no authorization)."""
        if not _JOB_ID_RE.match(job_id or ""):
            return None
        if not self.root.is_dir():
            return None
        for pid in os.listdir(self.root):
            pdir = self.root / pid
            if not _PID_DIR_RE.match(pid) or not pdir.is_dir():
                continue
            for sdir in os.listdir(pdir):
                candidate = pdir / sdir / JOBS_DIRNAME / f"{job_id}.json"
                if candidate.is_file():
                    return candidate
        return None

    def load(self, job: JobState) -> JobState | None:
        return _load_job(job.path)

    def save(self, job: JobState) -> None:
        _mkdir_private(Path(job.dir))
        _write_json_atomic(job.path, job.to_dict())
        if self.apply_acl:
            _restrict_file(job.path)

    # -- write side --
    def create(self, *, project_path, session_id, cmd: str, cwd: str, timeout_s: int,
               guard_paths=None, cred_names=None) -> JobState:
        """A ``queued`` record; ``cmd`` is the DISPLAY form (before credential expansion)."""
        acl_ok = self.output_store._ensure_root()
        directory, pid, sid = self._jobs_dir(project_path, session_id)
        _mkdir_private(directory.parent.parent)
        _mkdir_private(directory.parent)
        _mkdir_private(directory)
        timeout_s = max(1, min(int(timeout_s or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S))
        cmd_text = cmd or ""
        job = JobState(
            id=new_job_id(), project_id=pid, project_path=str(project_path), session_id=sid,
            cmd_sha256=hashlib.sha256(cmd_text.encode("utf-8")).hexdigest(),
            cmd_display=_display(cmd_text), cwd=str(cwd), timeout_s=timeout_s,
            created_at=_iso(_now()), guard={"cwd": str(cwd), "paths": [str(p) for p in (guard_paths or [])]},
            acl_applied=bool(acl_ok), creds=sorted(cred_names or []), dir=str(directory),
        )
        self.save(job)
        self._maybe_sweep()
        return job

    def spawn_supervisor(self, job: JobState, payload: dict) -> JobState:
        """Start the detached supervisor for ``job`` and hand it ``payload`` on stdin.

        The payload (raw cmd, expanded cmd, injected env, secret values) is
        the only channel that carries a decoded credential: never argv,
        never the environment, never a file.
        """
        _reap_spawned()
        pkg_root = Path(__file__).resolve().parents[1]
        argv = [sys.executable, "-m", "services.shell_jobs", "--supervise", job.id, "--root", str(self.root)]
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONPATH"] = os.pathsep.join([str(pkg_root)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
        env["C3_SHELL_OUT_DIR"] = str(self.root)
        log_fh = open(job.log_path, "ab")
        kwargs: dict = {"stdin": subprocess.PIPE, "stdout": log_fh, "stderr": subprocess.STDOUT,
                        "env": env, "cwd": str(pkg_root), "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(argv, **kwargs)
        except (OSError, ValueError) as exc:
            log_fh.close()
            job.status = "failed"
            job.finished_at = _iso(_now())
            job.exit_code = 126
            job.error = f"supervisor could not be spawned: {type(exc).__name__}: {exc}"
            self.save(job)
            return job
        finally:
            try:
                log_fh.close()
            except OSError:
                pass
        _SPAWNED.append(proc)
        job.supervisor_pid = proc.pid
        job.supervisor_start_time, job.supervisor_start_source = process_start_time(proc.pid)
        job.supervisor_argv = list(argv)
        self.save(job)                                # pids on disk BEFORE the payload wakes the supervisor
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            job.status = "failed"
            job.finished_at = _iso(_now())
            job.exit_code = 126
            job.error = (f"supervisor exited before reading its payload ({type(exc).__name__})"
                         + (f"; log: {_log_tail(job.log_path)}" if _log_tail(job.log_path) else ""))
            self.save(job)
            return job
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
        return job

    def start(self, *, project_path, session_id, cmd: str, cwd: str, timeout_s: int, payload: dict,
              guard_paths=None, cred_names=None, wait_s: float = ACK_WAIT_S) -> JobState:
        """create + spawn + a bounded wait for the supervisor's ``running`` (never for the job)."""
        job = self.create(project_path=project_path, session_id=session_id, cmd=cmd, cwd=cwd,
                          timeout_s=timeout_s, guard_paths=guard_paths, cred_names=cred_names)
        job = self.spawn_supervisor(job, payload)
        if job.terminal:
            return job
        return self._wait_for(job, lambda j: j.status != "queued", wait_s)

    def _wait_for(self, job: JobState, predicate, timeout: float) -> JobState:
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            fresh = self.load(job) or job
            if predicate(fresh):
                return fresh
            if time.monotonic() >= deadline:
                return fresh
            if fresh.supervisor_pid and not _supervisor_alive(fresh):
                return self._reap_one(fresh)
            time.sleep(0.05)

    # -- the authorization gate --
    def resolve(self, job_id, *, project_path, session_id, guard_check) -> JobState:
        """Resolve an id for THIS project + session under the CURRENT guard rules.

        Same model as ``ShellOutputStore.resolve``; raises ``JobAccessError``.
        A running job whose supervisor has died is marked ``lost`` on the way out.
        """
        jid = job_id if isinstance(job_id, str) else ""
        shown = jid[:20] if jid else "<empty>"
        if not _JOB_ID_RE.match(jid):
            raise JobAccessError(_NOT_FOUND.format(jid=shown))
        if guard_check is None:                       # fail closed: no verdict, no state
            raise JobAccessError(f"job {jid}: no access-guard check supplied; refusing")
        directory, pid, sid = self._jobs_dir(project_path, session_id)
        job = _load_job(directory / f"{jid}.json")
        if job is None or job.project_id != pid or (job.session_id or "") != sid or job.id != jid:
            raise JobAccessError(_NOT_FOUND.format(jid=jid))
        for path in [job.guard.get("cwd", "")] + list(job.guard.get("paths", []) or []):
            if not path:
                continue
            denial = guard_check(path)
            if denial:
                rule = getattr(denial, "rule", "") or ""
                kind = getattr(denial, "kind", "") or "deny"
                detail = f" ({kind} rule {rule})" if rule else f" ({kind})"
                raise JobAccessError(f"job {jid}: {path} is no longer readable under the "
                                     f"current access rules{detail}")
        _reap_spawned()
        return self._reap_one(job)

    # -- read side --
    def tail(self, job: JobState, stream: str = "stdout", *, lines: int = 50,
             max_bytes: int = DEFAULT_READ_BYTES, guard_check=None) -> str:
        """Last ``lines`` lines: the growing spool while running, the promoted stream after.

        Header ``[stream La-b of N, running|done|…]``; budgeted newest-first like the store.
        """
        if stream not in _STREAMS:
            raise ValueError(f"stream must be one of {_STREAMS}")
        state = job.status
        if job.terminal and job.output_id:
            meta = self.output_store.resolve(job.output_id, project_path=job.project_path,
                                             session_id=job.session_id, guard_check=guard_check)
            path = meta.stream_path(stream)
        elif job.status == "queued" or not job.spool.get(stream):
            return f"[{stream}: job {state}, no output yet]"
        else:
            path = Path(job.spool[stream])
        kept, total, available = _tail_file(path, lines, max_bytes)
        if not kept:
            return f"[{stream}: empty; 0 lines, {state}]"
        first, last = kept[0][0], kept[-1][0]
        text = f"[{stream} L{first}-{last} of {total}, {state}]\n" + "\n".join(s for _, s in kept)
        if len(kept) < available:
            text = (f"…[{max_bytes} B budget: showing the last {len(kept)} of {available} "
                    f"requested lines]\n" + text)
        return text

    def list(self, *, project_path, session_id) -> list[JobState]:
        """Only this caller's jobs (same project + session), oldest first; dead supervisors reaped."""
        directory, pid, sid = self._jobs_dir(project_path, session_id)
        if not directory.is_dir():
            return []
        _reap_spawned()
        found: list[JobState] = []
        for entry in sorted(os.listdir(directory)):
            if not _JOB_FILE_RE.match(entry):
                continue
            job = _load_job(directory / entry)
            if job is None or job.project_id != pid or (job.session_id or "") != sid:
                continue
            found.append(self._reap_one(job))
        found.sort(key=lambda j: j.created_at)
        return found

    # -- cancel --
    def cancel(self, job: JobState) -> tuple[JobState, str]:
        """Kill the child tree — only when its recorded creation time still matches — and record it.

        Returns ``(job, note)``. A reused pid is never signalled: the note says so
        and the state is left to the supervisor, whose child has already gone.
        """
        job = self._reap_one(job)
        if job.terminal:
            return job, f"already {job.status}"
        note = ""
        if job.child_pid:
            if process_alive(job.child_pid, job.child_start_time, job.child_start_source):
                self._touch_cancel(job)
                note = f"child pid {job.child_pid} tree killed ({kill_tree_by_pid(job.child_pid)})"
            else:
                live, _ = process_start_time(job.child_pid)
                if live and not job.child_start_time:
                    return job, (f"refused: pid {job.child_pid} has no recorded creation time, so it "
                                 f"cannot be proven to be this job's child; nothing was killed")
                if live:
                    return job, (f"refused: pid {job.child_pid} is now a different process "
                                 f"(creation time {live} != recorded {job.child_start_time}); "
                                 f"nothing was killed — the supervisor is recording the outcome")
                note = f"child pid {job.child_pid} had already exited; the supervisor is recording the outcome"
        else:
            self._touch_cancel(job)                   # queued: the supervisor checks the marker before spawning
            note = "cancel requested before the command started"
            # It may have spawned the child in the same instant; if a pid
            # appears, kill it under the same creation-time rule.
            fresh = self._wait_for(job, lambda j: j.terminal or bool(j.child_pid), CANCEL_WAIT_S)
            if not fresh.terminal and fresh.child_pid and process_alive(
                    fresh.child_pid, fresh.child_start_time, fresh.child_start_source):
                note = f"child pid {fresh.child_pid} tree killed ({kill_tree_by_pid(fresh.child_pid)})"
            job = fresh
        fresh = self._wait_for(job, lambda j: j.terminal, CANCEL_WAIT_S)
        if fresh.terminal:
            return fresh, note
        return self._reap_one(fresh), note + "; the supervisor has not recorded it yet"

    def _touch_cancel(self, job: JobState) -> None:
        try:
            job.cancel_marker.write_text(_iso(_now()), encoding="utf-8")
        except OSError:
            pass

    # -- reap --
    def reap(self, *, project_path=None, session_id=None) -> list[JobState]:
        """Mark jobs whose supervisor is gone as ``lost``; all sessions when no project is given."""
        lost: list[JobState] = []
        if project_path is not None:
            dirs = [self._jobs_dir(project_path, session_id)[0]]
        else:
            dirs = list(self._job_dirs())
        for directory in dirs:
            if not directory.is_dir():
                continue
            for entry in os.listdir(directory):
                if not _JOB_FILE_RE.match(entry):
                    continue
                job = _load_job(directory / entry)
                if job is None or job.terminal:
                    continue
                after = self._reap_one(job)
                if after.status == "lost":
                    lost.append(after)
        return lost

    def _reap_one(self, job: JobState) -> JobState:
        """A non-terminal job with no live supervisor becomes ``lost`` (child killed if provably ours)."""
        if job.terminal:
            return job
        if job.supervisor_pid:
            if _supervisor_alive(job):
                return job
        else:
            created = _parse_iso(job.created_at)
            if created is not None and (_now() - created).total_seconds() < QUEUED_GRACE_S:
                return job
        note = ""
        if job.child_pid and process_alive(job.child_pid, job.child_start_time, job.child_start_source):
            note = f"child pid {job.child_pid} tree killed ({kill_tree_by_pid(job.child_pid)})"
        fresh = self.load(job)                        # it may have finished between the two checks
        if fresh is not None and fresh.terminal:
            return fresh
        job = fresh or job
        job.status = "lost"
        job.finished_at = _iso(_now())
        job.cancel_requested = job.cancel_marker.exists()
        job.error = "supervisor process is gone" + (f"; {note}" if note else "")
        if job.spool:                                 # whatever the child wrote is still the deliverable
            try:
                capture = _SpoolCapture(_spool_id(job), job.spool.get("stdout", ""), job.spool.get("stderr", ""))
                meta = self.output_store.promote(
                    capture, project_path=job.project_path, session_id=job.session_id,
                    cmd=job.cmd_display, cwd=job.cwd, guard_paths=job.guard.get("paths", []),
                    exit_code=-1, timed_out=False, duration_ms=int(job.elapsed_s() * 1000))
                job.output_id = meta.id
                job.stdout, job.stderr = meta.stdout, meta.stderr
                job.spool = {}
            except Exception as exc:
                job.error += f"; spool not kept ({type(exc).__name__}: {exc})"
        self.save(job)
        _notify_job(job)                              # whoever noticed the loss reports it
        return job

    # -- read-only enumeration (mobile/desktop gateway) --
    def list_all(self, project_path=None, limit: int = 50) -> list[dict]:
        """Every job record under the root, newest first, WITHOUT touching anything.

        The gateway's ``GET /api/mobile/jobs`` reads this. Unlike ``list`` /
        ``reap`` it never promotes a spool, never writes ``lost`` and never
        polls a supervisor — a remote reader must not change what the owning
        session sees, and a stat-only walk cannot. Rows are the wire shape
        (``job_row``): identity, timing and status; never creds, env or the
        guard paths. ``project_path`` narrows to one project.
        """
        rows: list[dict] = []
        # A caller may hand us the registered spelling of the path OR its
        # resolved form (macOS `/var` -> `/private/var`, Windows 8.3 temp
        # names), while a job was filed under the id of whichever spelling
        # the owning session used. Ids cannot be reconciled from the resolved
        # side, so match on the canonical real path each record carries.
        want_key = _canonical_project_key(project_path) if project_path else ""
        for directory in self._job_dirs():
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            for entry in entries:
                if not _JOB_FILE_RE.match(entry):
                    continue
                job = _load_job(directory / entry)
                if job is None:
                    continue
                if want_key and _canonical_project_key(job.project_path) != want_key:
                    continue
                rows.append(job_row(job))
        rows.sort(key=lambda r: (r.get("created_at") or ""), reverse=True)
        return rows[:max(1, int(limit or 50))]

    # -- retention --
    def _job_dirs(self):
        if not self.root.is_dir():
            return
        for pid in sorted(os.listdir(self.root)):
            pdir = self.root / pid
            if not _PID_DIR_RE.match(pid) or not pdir.is_dir():
                continue
            for sdir in sorted(os.listdir(pdir)):
                d = pdir / sdir / JOBS_DIRNAME
                if d.is_dir():
                    yield d

    def _maybe_sweep(self) -> None:
        marker = self.root / JOBS_SWEEP_MARKER
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            age = float("inf")
        if age < SWEEP_INTERVAL_S:
            return
        try:
            self.sweep()
        except Exception:
            pass

    def sweep(self, *, now=None) -> dict:
        """Delete terminal job records older than ``RETENTION_DAYS``; never a live one."""
        now_dt = now if isinstance(now, datetime) else _now()
        cutoff = now_dt - timedelta(days=RETENTION_DAYS)
        deleted = 0
        for directory in self._job_dirs():
            for entry in os.listdir(directory):
                m = _JOB_FILE_RE.match(entry)
                if not m:
                    continue
                job = _load_job(directory / entry)
                if job is None:
                    continue
                if not job.terminal:
                    continue
                finished = _parse_iso(job.finished_at) or _parse_iso(job.created_at)
                if finished is None or finished > cutoff:
                    continue
                for path in (job.path, job.log_path, job.cancel_marker):
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError:
                        pass
        try:
            _mkdir_private(self.root)
            (self.root / JOBS_SWEEP_MARKER).write_text(_iso(now_dt), encoding="utf-8")
        except OSError:
            pass
        return {"deleted": deleted}


def _supervisor_alive(job: JobState) -> bool:
    """Strict pid + creation-time match; plain pid liveness only when no time was recorded."""
    if not job.supervisor_pid:
        return False
    if job.supervisor_start_time:
        return process_alive(job.supervisor_pid, job.supervisor_start_time, job.supervisor_start_source)
    return bool(process_start_time(job.supervisor_pid)[0])


def _spool_id(job: JobState) -> str:
    """The output id a running job's spool was minted under (``<id>.stdout.part``)."""
    for path in job.spool.values():
        name = Path(str(path)).name
        if name.startswith("o-"):
            return name.split(".", 1)[0]
    return job.output_id


# Wire shape of one job for a remote reader. Deliberately a fixed tuple, not
# ``to_dict()``: creds (names), env, guard paths, spool paths and pids stay
# on this machine.
JOB_ROW_FIELDS = ("id", "project_path", "session_id", "status", "cmd_display", "cwd",
                  "created_at", "started_at", "finished_at", "exit_code", "duration_ms",
                  "output_id", "timed_out", "cancel_requested", "error")


def job_row(job: JobState) -> dict:
    """The allowlisted view of ``job`` that may leave the machine."""
    return {k: getattr(job, k, None) for k in JOB_ROW_FIELDS}



def _canonical_project_key(path) -> str:
    """One spelling per directory: realpath (symlinks, 8.3 names) then the
    store's own normalisation (slashes, trailing slash, casefold on Windows)."""
    try:
        real = os.path.realpath(os.path.abspath(os.fspath(path)))
    except (OSError, ValueError):
        real = os.path.abspath(os.fspath(path))
    return _norm_project(real, windows=(os.name == "nt"))

def _notify_job(job: JobState) -> None:
    """Terminal-state notification: ``kind="shell_job"``, ``ref_id=job.id``.

    Lands in ``.c3/notifications.jsonl`` — one of the four files the
    gateway's ``/feed?wait=`` watches — so a finished background job wakes a
    waiting desktop client. The title carries the job id so two jobs that end
    the same way never collapse into one record. Severity ``info`` for
    ``done``, ``warning`` for every other terminal state. Best-effort: the
    supervisor is a detached process and a notification failure must never
    fail the job it reports on.
    """
    try:
        if not job.terminal:
            return
        from services.notifications import notify
        severity = "info" if job.status == "done" else "warning"
        cmd = (job.cmd_display or "")[:120]
        secs = (job.duration_ms or 0) / 1000.0
        parts = [cmd or "(no command)", f"exit {job.exit_code}", f"{secs:.1f}s"]
        if job.error:
            parts.append(str(job.error)[:200])
        notify(job.project_path, agent="shell_job", severity=severity,
               title=f"Job {job.status}: {job.id}",
               message=" — ".join(parts),
               kind="shell_job", ref_id=job.id)
    except Exception:
        pass


# ── Supervisor ──────────────────────────────────────────────────────────────

def _read_payload(timeout: float) -> tuple[dict | None, str]:
    """One JSON document from stdin, or ``(None, reason)`` after ``timeout`` seconds."""
    box: dict = {}

    def _reader():
        try:
            stream = getattr(sys.stdin, "buffer", None)
            if stream is not None:
                data = stream.read()
            else:
                chunks = []
                while True:
                    chunk = os.read(0, 65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
            box["data"] = data
        except Exception as exc:
            box["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_reader, name="c3-job-payload", daemon=True)
    thread.start()
    thread.join(timeout)
    if "error" in box:
        return None, f"payload unreadable: {box['error']}"
    if "data" not in box:
        return None, f"no payload on stdin within {timeout:.0f} s"
    try:
        payload = json.loads(box["data"].decode("utf-8"))
    except ValueError as exc:
        return None, f"payload is not JSON: {exc}"
    return (payload if isinstance(payload, dict) else {}), ""


def _make_redactor(secret_values: dict):
    """Register the decoded values with the credential store so its redactor scrubs them."""
    try:
        from services import credential_store as _creds
        for ref, value in (secret_values or {}).items():
            if isinstance(value, str):
                _creds.register_active_secret(str(ref), value)
        return _creds.redact_text
    except Exception:
        values = [v for v in (secret_values or {}).values() if isinstance(v, str) and len(v) >= 4]
        names = {v: k for k, v in (secret_values or {}).items() if isinstance(v, str)}

        def _redact(text: str) -> str:
            for v in values:
                if v in text:
                    text = text.replace(v, f"[cred:{names[v]}]")
            return text
        return _redact


def _finish(store: JobStore, job: JobState, status: str, *, exit_code=None, timed_out=False,
            duration_ms=None, error: str = "", touched=None, payload: dict | None = None) -> None:
    """Terminal transition + the records c3_shell would have written for a synchronous call."""
    fresh = store.load(job) or job
    job.cancel_requested = fresh.cancel_requested or job.cancel_marker.exists()
    job.status = status
    job.finished_at = _iso(_now())
    job.exit_code = exit_code
    job.timed_out = bool(timed_out)
    job.duration_ms = duration_ms if duration_ms is not None else int(job.elapsed_s() * 1000)
    if error:
        job.error = (job.error + "; " if job.error else "") + error
    store.save(job)
    payload = payload or {}
    raw_cmd = str(payload.get("cmd") or "")
    try:
        from services.activity_log import ActivityLog
        ActivityLog(job.project_path).log("shell_exec", {
            "cmd": raw_cmd[:200] if raw_cmd else job.cmd_display[:200],
            "cwd": job.cwd,
            "exit_code": job.exit_code,
            "duration_ms": job.duration_ms,
            "timed_out": job.timed_out,
            "touched_files": list(touched or []),
            "creds": list(job.creds),
            "job_id": job.id,
            "job_status": job.status,
            "output_id": job.output_id,
            "session_id": job.session_id,
        })
    except Exception:
        pass
    try:
        from services.telemetry import append_telemetry_record
        append_telemetry_record(job.project_path, {
            "session_id": job.session_id,
            "tool": "c3_shell",
            "action": "job",
            "response_tokens": 0,
            "duration_ms": job.duration_ms,
            "source": "supervisor",
            "target": job.id,
            "detail": {
                "cmd_class": "job", "job_id": job.id, "job_status": job.status,
                "exit_code": job.exit_code, "timed_out": job.timed_out,
                "stdout_bytes": int((job.stdout or {}).get("bytes", 0)),
                "stderr_bytes": int((job.stderr or {}).get("bytes", 0)),
                "longest_line": max(int((job.stdout or {}).get("longest_line", 0)),
                                    int((job.stderr or {}).get("longest_line", 0))),
                "duration_ms": job.duration_ms, "output_id": job.output_id,
                "spilled": bool(job.output_id),
            },
        })
    except Exception:
        pass
    if job.creds:
        try:
            from services import cred_telemetry as _ct
            tmpl = set(payload.get("tmpl_used") or [])
            common = dict(project_path=job.project_path, surface="shell_job", session_id=job.session_id,
                          cmd_preview=raw_cmd, exit_code=job.exit_code)
            _ct.record_use([r for r in job.creds if r in tmpl], action=_ct.ACTION_TEMPLATE, **common)
            _ct.record_use([r for r in job.creds if r not in tmpl], action=_ct.ACTION_INJECT, **common)
        except Exception:
            pass
    # Last, so a reader woken by the notification finds the status, the
    # activity row and the telemetry row already on disk (2.126.1).
    _notify_job(job)


def supervise(job_id: str, root=None) -> int:
    """Detached entry point: run one job to its end and record everything about it.

    Spawns the command exactly the way ``cli.tools.shell._run_sync`` does
    (Git Bash / cmd / sh selection, ``_popen_kwargs`` env plus the injected
    credentials from the stdin payload), streams both pipes through
    ``ShellCapture`` with the credential redactor, enforces the job's own
    timeout (≤ 6 h), promotes the spool ALWAYS, and appends the activity-log
    and telemetry records a synchronous call would have produced.
    """
    store = JobStore(root)
    payload, why = _read_payload(PAYLOAD_TIMEOUT_S)
    path = store.locate(job_id)
    if path is None:
        print(f"[supervisor] job {job_id}: record not found under {store.root}", flush=True)
        return 2
    job = _load_job(path)
    if job is None:
        print(f"[supervisor] job {job_id}: record unreadable", flush=True)
        return 2
    if job.status != "queued":
        print(f"[supervisor] job {job_id}: already {job.status}; not running it twice", flush=True)
        return 3
    if payload is None:
        _finish(store, job, "failed", exit_code=126, error=why)
        return 4
    if job.cancel_marker.exists():
        _finish(store, job, "cancelled", exit_code=None, error="cancelled before the command started",
                payload=payload)
        return 0
    try:
        from cli.tools import shell as shell_mod
    except Exception as exc:
        _finish(store, job, "failed", exit_code=126,
                error=f"supervisor cannot import cli.tools.shell: {type(exc).__name__}: {exc}", payload=payload)
        return 5
    redact = _make_redactor(payload.get("secrets") or {})
    raw_cmd = str(payload.get("cmd") or "")
    exec_cmd = str(payload.get("exec_cmd") or raw_cmd)
    extra_env = payload.get("env") or None
    git_before = None
    try:
        if shell_mod._GIT_MUTATING.search(raw_cmd):
            git_before = shell_mod._capture_git_state(job.cwd)
    except Exception:
        git_before = None

    bash = shell_mod._select_bash()
    if bash:
        popen_target: object = [bash, "-c", exec_cmd]
        use_shell = False
    else:
        popen_target = exec_cmd
        use_shell = True
    shell_name = "git-bash" if bash else ("cmd" if sys.platform == "win32" else "sh")
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            popen_target, shell=use_shell, cwd=job.cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **shell_mod._popen_kwargs(extra_env),
        )
    except (OSError, ValueError) as exc:
        _finish(store, job, "failed", exit_code=127 if isinstance(exc, FileNotFoundError) else 126,
                duration_ms=round((time.monotonic() - t0) * 1000),
                error=redact(f"{type(exc).__name__}: {exc}"), payload=payload)
        return 0
    capture = ShellCapture(proc, store.output_store.spool_dir(), redact=redact,
                           kill_tree=shell_mod._kill_tree, live=True)
    job.status = "running"
    job.started_at = _iso(_now())
    job.child_pid = proc.pid
    job.child_start_time, job.child_start_source = process_start_time(proc.pid)
    job.shell = shell_name
    job.output_id = capture.output_id
    job.spool = {"stdout": str(capture.stdout_path), "stderr": str(capture.stderr_path)}
    store.save(job)

    timed_out = capture.wait(job.timeout_s)
    result = {"exit_code": capture.exit_code, "stdout": "", "stderr": "",
              "duration_ms": capture.duration_ms, "timed_out": timed_out, "shell": shell_name}
    touched: list[str] = []
    try:
        if git_before is not None or shell_mod._GIT_MUTATING.search(raw_cmd):
            from types import SimpleNamespace

            from services.edit_ledger import EditLedger
            svc = SimpleNamespace(project_path=job.project_path, edit_ledger=EditLedger(job.project_path))
            touched = shell_mod._maybe_refresh_ledger(raw_cmd, result, svc, before=git_before, cwd=job.cwd)
    except Exception:
        touched = []
    error = ""
    try:
        meta = store.output_store.promote(
            capture, project_path=job.project_path, session_id=job.session_id, cmd=raw_cmd or job.cmd_display,
            cwd=job.cwd, guard_paths=job.guard.get("paths", []), exit_code=capture.exit_code,
            timed_out=timed_out, duration_ms=capture.duration_ms)
        job.output_id = meta.id
        job.stdout, job.stderr = meta.stdout, meta.stderr
        job.acl_applied = bool(meta.acl_applied)
    except Exception as exc:
        error = f"output not kept ({type(exc).__name__}: {exc})"
        job.output_id = ""
        job.stdout, job.stderr = capture.stats.stdout.summary(), capture.stats.stderr.summary()
        try:
            capture.discard()
        except Exception:
            pass
    job.spool = {}
    if capture.errors:
        error = (error + "; " if error else "") + "spool errors: " + ", ".join(
            f"{k}: {v}" for k, v in capture.errors.items())
    cancelled = job.cancel_marker.exists()
    if cancelled:
        status = "cancelled"
    elif timed_out:
        status = "timeout"
    elif capture.exit_code == 0:
        status = "done"
    else:
        status = "failed"
    _finish(store, job, status, exit_code=capture.exit_code, timed_out=timed_out,
            duration_ms=capture.duration_ms, error=error, touched=touched, payload=payload)
    return 0


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python -m services.shell_jobs",
                                     description="c3_shell_job supervisor (internal)")
    parser.add_argument("--supervise", metavar="JOB_ID", required=True)
    parser.add_argument("--root", metavar="STORE_ROOT", default="")
    args = parser.parse_args(argv)
    root = args.root or None
    try:
        return supervise(args.supervise, root)
    except Exception:
        traceback.print_exc()
        try:                                          # leave a terminal state behind, never a phantom 'running'
            store = JobStore(root)
            path = store.locate(args.supervise)
            job = _load_job(path) if path else None
            if job is not None and not job.terminal:
                _finish(store, job, "failed", exit_code=126,
                        error="supervisor crashed: " + traceback.format_exc().strip().splitlines()[-1])
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
