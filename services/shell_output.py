"""Shell output spill store and streaming capture (c3_shell remediation, S1).

Why this exists
---------------
Claude Code discards any MCP result over 25k tokens, so a c3_shell call whose
output is a 1.4M-token grep over a minified bundle returns an error and gets
re-run. Today's filter is lossy ("[N lines omitted]" is gone for good) and
``communicate()`` buffers the whole output in RAM. This module streams the
child's stdout/stderr to spool files while keeping bounded head/tail previews,
and — when the output is over the response budget — promotes the spool into a
store the agent can page through later by an opaque id.

Layout: ``<root>/<project_id>/<session_id or 'nosession'>/<id>.{stdout,stderr,meta.json}``
where ``<root>`` is ``~/.c3/shell_out`` (override: env ``C3_SHELL_OUT_DIR``),
i.e. OUTSIDE every project, so a spill can never be committed, indexed,
masked, or read back through a project-relative path.

Authorization model (read this before changing ``ShellOutputStore.resolve``)
--------------------------------------------------------------------------
A spill is the raw bytes a command produced under the Access Guard rules that
were in force *at that moment*. If a later, less-privileged reader could fetch
it, spilling would be less safe than truncation: the store would become a
durable exfiltration channel around the guard. So every retrieval goes through
``resolve()``, which refuses unless ALL of these hold:

1. **Same project.** ``project_id(caller's project) == meta.project_id``. Ids
   are looked up under the caller's own ``<project_id>/<session>`` directory,
   so an id minted by another project simply does not resolve.
2. **Same session.** ``session_id`` must equal the one recorded at promotion.
   A new session gets a new id space; it cannot inherit a previous session's
   spills (the previous session may have run under different overrides).
3. **Guard re-check.** The caller supplies ``guard_check(path)`` — a wrapper
   around ``services.access_guard.check(path, "read", project_path)`` with the
   CURRENT rules — and it is re-applied to the originating call's ``cwd`` and
   every path its guard scan looked at (``meta.guard``). A rule added after
   the command ran denies the stored bytes exactly as it would deny the file.
4. **Not expired, files present.** Retention is ``RETENTION_DAYS`` and a
   global ``RETENTION_BYTES`` cap (oldest-first eviction).

What a refusal says: ``OutputAccessError`` carries a one-line reason meant for
the agent. Unknown id, malformed id, another project's id and another
session's id all produce the SAME wording ("not found for this project and
session") so a probe cannot learn which ids exist. Only an id the caller owns
can get a more specific reason (expired, swept, or guard-denied path).

Ids are ``o-`` + 12 hex from ``secrets`` — opaque and unguessable; a path is
never handed to the agent.

Redaction: ``ShellCapture`` applies the caller's ``redact`` callable to every
piece BEFORE it is written, so the spool never holds an unredacted credential.
A line longer than ``_CHUNK`` is streamed in pieces; the last ``_HOLDBACK``
characters of a partial piece are held back and re-joined with the next piece
so a secret straddling two pieces is still seen whole. ``redact`` must
therefore be idempotent (``redact(redact(x)) == redact(x)``);
``services.credential_store.redact_text`` is.

Stdlib only. Windows-first (binary pipes, ``os.replace``, icacls best-effort).
"""
from __future__ import annotations

import codecs
import getpass
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "STORE_ROOT", "RETENTION_DAYS", "RETENTION_BYTES", "HEAD_BYTES", "TAIL_BYTES",
    "TEXT_MAX_BYTES", "DEFAULT_READ_BYTES", "LINE_CLIP_CHARS",
    "project_id", "new_output_id", "store_root",
    "StreamStats", "CaptureStats", "ShellCapture",
    "OutputMeta", "OutputAccessError", "ShellOutputStore",
]

STORE_ROOT = Path.home() / ".c3" / "shell_out"
ENV_ROOT = "C3_SHELL_OUT_DIR"
RETENTION_DAYS = 3
RETENTION_BYTES = 250 * 1024 * 1024          # global, oldest-first eviction
HEAD_BYTES = 64 * 1024                       # in-memory preview per stream while streaming
TAIL_BYTES = 64 * 1024
TEXT_MAX_BYTES = 1024 * 1024                 # ShellCapture.text() fast path ceiling
DEFAULT_READ_BYTES = 18 * 1024               # default budget for read/search/tail
LINE_CLIP_CHARS = 512                        # longer lines keep prefix + suffix
LINE_CLIP_PREFIX = 384
LINE_CLIP_SUFFIX = 128
SWEEP_INTERVAL_S = 10 * 60
SWEEP_MARKER = ".last_sweep"
ACL_MARKER = ".acl"
SPOOL_DIRNAME = ".spool"
NO_SESSION = "nosession"
ORPHAN_AGE_S = 24 * 3600                     # .part / meta-less files older than this are swept

_CHUNK = 64 * 1024        # readline(limit): a longer line is streamed in pieces this big
_HOLDBACK = 4 * 1024      # unemitted tail of a partial piece, so a straddling secret is redacted whole
_KILL_GRACE_S = 2.0       # like communicate(timeout=2) after a kill
_ID_RE = re.compile(r"^o-[0-9a-f]{12}$")
_PID_RE = re.compile(r"^[0-9a-f]{12}$")
_META_RE = re.compile(r"^(o-[0-9a-f]{12})\.meta\.json$")
_STREAM_RE = re.compile(r"^(o-[0-9a-f]{12})\.(stdout|stderr)$")
_SESSION_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_STREAMS = ("stdout", "stderr")
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()


# ── Identity helpers ────────────────────────────────────────────────────────

def _norm_project(path: str, *, windows: bool) -> str:
    """Forward slashes, no trailing slash, casefolded on Windows."""
    s = str(path).replace("\\", "/")
    if windows:
        s = s.casefold()
    stripped = s.rstrip("/")
    return stripped or s


def project_id(project_path) -> str:
    """12 hex of sha1 over the normalized absolute project path.

    ``U:/A/B`` and ``u:\\a\\b\\`` collapse to one id on Windows (casefold);
    POSIX paths keep their case.
    """
    absolute = os.path.abspath(os.fspath(project_path))
    norm = _norm_project(absolute, windows=(os.name == "nt"))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def new_output_id() -> str:
    """``o-`` + 12 hex, opaque and unguessable."""
    return "o-" + secrets.token_hex(6)


def store_root() -> Path:
    override = os.environ.get(ENV_ROOT, "").strip()
    return Path(override) if override else STORE_ROOT


def _session_dir(session_id: str) -> str:
    sid = session_id or ""
    if not sid:
        return NO_SESSION
    if _SESSION_DIR_RE.match(sid):
        return sid
    return "s-" + hashlib.sha1(sid.encode("utf-8")).hexdigest()[:12]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _as_dt(now) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(float(now), tz=timezone.utc)


def _identity(s: str) -> str:
    return s


def _decode_head(b) -> str:
    """Decode a byte prefix, dropping an incomplete trailing multibyte sequence."""
    raw = bytes(b)
    for cut in range(4):
        try:
            return raw[: len(raw) - cut].decode("utf-8") if cut else raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _decode_tail(b) -> str:
    """Decode a byte suffix, dropping leading continuation bytes of a split char."""
    raw = bytes(b)
    i = 0
    while i < 3 and i < len(raw) and (raw[i] & 0xC0) == 0x80:
        i += 1
    return raw[i:].decode("utf-8", "replace")


def _clip_line(line: str) -> str:
    if len(line) <= LINE_CLIP_CHARS:
        return line
    dropped = len(line) - LINE_CLIP_PREFIX - LINE_CLIP_SUFFIX
    return f"{line[:LINE_CLIP_PREFIX]}…[+{dropped} chars]…{line[-LINE_CLIP_SUFFIX:]}"


def _display(cmd: str) -> str:
    first = (cmd or "").splitlines()[0] if (cmd or "").strip() else ""
    first = first.strip()
    if len(first) > 240:
        first = first[:239] + "…"
    return first


# ── Streaming capture ───────────────────────────────────────────────────────

@dataclass
class StreamStats:
    """Per-stream accounting, filled while streaming (post-redaction)."""
    bytes: int = 0
    lines: int = 0
    longest_line: int = 0          # characters, newline excluded
    sha256: str = _EMPTY_SHA
    head: str = ""
    tail: str = ""
    truncated_middle: bool = False

    def summary(self) -> dict:
        return {"bytes": self.bytes, "lines": self.lines,
                "longest_line": self.longest_line, "sha256": self.sha256}


@dataclass
class CaptureStats:
    stdout: StreamStats = field(default_factory=StreamStats)
    stderr: StreamStats = field(default_factory=StreamStats)


class _Pump:
    """One reader thread: pipe → redact → spool file + hash + head/tail + counts."""

    def __init__(self, name: str, pipe, path: Path, redact, head_bytes: int, tail_bytes: int,
                 live: bool = False):
        self.name = name
        self.pipe = pipe
        self.path = path
        self._redact = redact
        self.head_bytes = head_bytes
        self.tail_bytes = tail_bytes
        self.live = live               # flush after every piece so a reader sees the file grow
        self.stats = StreamStats()
        self.error = ""
        self._fh = None
        self._hash = hashlib.sha256()
        self._head = bytearray()
        self._tail = bytearray()
        self._cur = 0            # chars of the current unterminated line
        self._last = ""          # last char emitted (CR detection across pieces)
        self._lock = threading.Lock()
        self._finalized = False
        self.thread = threading.Thread(target=self.run, name=f"c3-shell-{name}", daemon=True)

    # -- thread body --
    def run(self) -> None:
        try:
            self._fh = open(self.path, "wb")
        except OSError as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        try:
            self._pump()
        except Exception as exc:                      # never let the reader die silently
            self.error = self.error or f"{type(exc).__name__}: {exc}"
        finally:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
            try:
                self.pipe.close()
            except OSError:
                pass
            self.finalize()

    def _pump(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        held = ""
        while True:
            raw = self.pipe.readline(_CHUNK)
            if not raw:
                text = held + decoder.decode(b"", True)
                if text:
                    self._emit(self._redact(text))
                return
            text = self._redact(held + decoder.decode(raw))
            if raw.endswith(b"\n"):
                held = ""
                self._emit(text)
            else:                                    # partial piece of a long line
                held = text[-_HOLDBACK:]
                self._emit(text[:-_HOLDBACK])

    def _emit(self, text: str) -> None:
        if not text:
            return
        data = text.encode("utf-8")
        if self._fh is not None:
            try:
                self._fh.write(data)
                if self.live:
                    self._fh.flush()
            except OSError as exc:                    # keep draining so the child never blocks
                self.error = f"{type(exc).__name__}: {exc}"
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None
        self._hash.update(data)
        self.stats.bytes += len(data)
        if len(self._head) < self.head_bytes:
            self._head += data[: self.head_bytes - len(self._head)]
        if len(data) >= self.tail_bytes:
            self._tail = bytearray(data[-self.tail_bytes:])
        else:
            self._tail += data
            if len(self._tail) > self.tail_bytes:
                del self._tail[: len(self._tail) - self.tail_bytes]
        self._count(text)

    def _count(self, text: str) -> None:
        start, size = 0, len(text)
        while start < size:
            nl = text.find("\n", start)
            if nl < 0:
                self._cur += size - start
                break
            prev = text[nl - 1] if nl > 0 else self._last
            n = self._cur + (nl - start) - (1 if prev == "\r" else 0)
            self.stats.lines += 1
            if n > self.stats.longest_line:
                self.stats.longest_line = n
            self._cur = 0
            start = nl + 1
        self._last = text[-1]

    def finalize(self) -> None:
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            if self._cur:                             # trailing line without newline
                n = self._cur - (1 if self._last == "\r" else 0)
                self.stats.lines += 1
                if n > self.stats.longest_line:
                    self.stats.longest_line = n
                self._cur = 0
            st = self.stats
            st.sha256 = self._hash.hexdigest()
            st.head = _decode_head(self._head)
            st.tail = _decode_tail(self._tail) if st.bytes > self.tail_bytes else bytes(self._tail).decode("utf-8", "replace")
            st.truncated_middle = st.bytes > self.head_bytes + self.tail_bytes


class ShellCapture:
    """Streams a Popen's stdout and stderr to two spool files with bounded previews.

    ``proc`` must have BINARY pipes (``Popen(..., stdout=PIPE, stderr=PIPE)``
    without ``text=True``). Reader threads start immediately. Memory per stream
    is bounded by ``head_bytes + tail_bytes`` plus one ``_CHUNK`` piece in
    flight; a 3 MB single line never lives in RAM whole.

    ``redact(piece) -> piece`` runs before any byte hits disk; ``kill_tree(proc)``
    is invoked on timeout (defaults to ``proc.kill()``). ``live=True`` flushes the
    spool after every piece so another process can tail it while the command
    runs (background jobs, services/shell_jobs.py); the synchronous path keeps
    the default buffering.
    """

    def __init__(self, proc, spool_dir, *, redact=None, kill_tree=None,
                 head_bytes: int = HEAD_BYTES, tail_bytes: int = TAIL_BYTES, live: bool = False):
        self.proc = proc
        self.output_id = new_output_id()
        self.spool_dir = Path(spool_dir)
        _mkdir_private(self.spool_dir)
        self.stdout_path = self.spool_dir / f"{self.output_id}.stdout.part"
        self.stderr_path = self.spool_dir / f"{self.output_id}.stderr.part"
        self._redact = redact or _identity
        self._kill_tree = kill_tree
        self._t0 = time.monotonic()
        self.exit_code: int | None = None
        self.duration_ms = 0
        self.timed_out = False
        self.finished = False
        self.stats = CaptureStats()
        self._pumps: dict[str, _Pump] = {}
        for name, pipe, path in (("stdout", proc.stdout, self.stdout_path),
                                 ("stderr", proc.stderr, self.stderr_path)):
            if pipe is None:
                path.write_bytes(b"")                 # keep the triplet complete
                continue
            if isinstance(pipe, io.TextIOBase):
                raise TypeError("ShellCapture needs binary pipes: Popen without text=True/encoding")
            pump = _Pump(name, pipe, path, self._redact, head_bytes, tail_bytes, live=live)
            self._pumps[name] = pump
            setattr(self.stats, name, pump.stats)
        for pump in self._pumps.values():
            pump.thread.start()

    @property
    def errors(self) -> dict:
        """Reader-side I/O errors by stream (empty when the spool is complete)."""
        return {n: p.error for n, p in self._pumps.items() if p.error}

    def wait(self, timeout: float) -> bool:
        """Wait for exit + pipe EOF within ``timeout`` seconds; True when it timed out.

        Mirrors ``communicate(timeout)``: a child that exits while a grandchild
        keeps the pipe open still counts as a timeout, and the tree is killed.
        """
        deadline = time.monotonic() + float(timeout)
        timed_out = False
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill()
        else:
            remaining = max(deadline - time.monotonic(), _KILL_GRACE_S)
            if not self._join(remaining):
                timed_out = True
                self._kill()
        if timed_out:
            self._join(_KILL_GRACE_S)
        for pump in self._pumps.values():             # no-op when the thread already did it
            pump.finalize()
        rc = self.proc.poll()
        self.timed_out = timed_out
        self.exit_code = -1 if timed_out else (rc if rc is not None else -1)
        self.duration_ms = round((time.monotonic() - self._t0) * 1000)
        self.finished = True
        return timed_out

    def _kill(self) -> None:
        try:
            if self._kill_tree is not None:
                self._kill_tree(self.proc)
            else:
                self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            pass

    def _join(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        for pump in self._pumps.values():
            pump.thread.join(max(deadline - time.monotonic(), 0.0))
        return not any(p.thread.is_alive() for p in self._pumps.values())

    def text(self, stream: str = "stdout") -> str:
        """Whole decoded stream — the small-output fast path (≤ ``TEXT_MAX_BYTES``)."""
        if stream not in _STREAMS:
            raise ValueError(f"stream must be one of {_STREAMS}")
        if not self.finished:
            raise RuntimeError("call wait() before text()")
        st = getattr(self.stats, stream)
        if st.bytes > TEXT_MAX_BYTES:
            raise ValueError(f"{stream} is {st.bytes} bytes; text() serves at most "
                             f"{TEXT_MAX_BYTES} — promote() it to the store instead")
        path = self.stdout_path if stream == "stdout" else self.stderr_path
        try:
            return path.read_bytes().decode("utf-8", "replace")
        except FileNotFoundError:
            return ""

    def discard(self) -> None:
        """Delete the spool files (output stayed under budget, nothing to keep)."""
        for path in (self.stdout_path, self.stderr_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


# ── Store ───────────────────────────────────────────────────────────────────

class OutputAccessError(Exception):
    """Refusal from ``ShellOutputStore.resolve`` — ``str(exc)`` is the one-line reason."""


_META_KEYS = ("id", "project_id", "project_path", "session_id", "created_at", "expires_at",
              "cmd_sha256", "cmd_display", "cwd", "exit_code", "timed_out", "duration_ms",
              "stdout", "stderr", "guard", "acl_applied")


@dataclass
class OutputMeta:
    id: str
    project_id: str
    project_path: str
    session_id: str
    created_at: str
    expires_at: str
    cmd_sha256: str
    cmd_display: str
    cwd: str
    exit_code: int
    timed_out: bool
    duration_ms: int
    stdout: dict
    stderr: dict
    guard: dict
    acl_applied: bool = False
    dir: str = field(default="", repr=False, compare=False)   # where the triplet lives; not persisted

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in _META_KEYS}

    @classmethod
    def from_dict(cls, data: dict, directory: Path | str = "") -> "OutputMeta":
        kwargs = {k: data[k] for k in _META_KEYS if k != "acl_applied"}
        kwargs["acl_applied"] = bool(data.get("acl_applied", False))
        return cls(dir=str(directory), **kwargs)

    def stream_path(self, stream: str) -> Path:
        if stream not in _STREAMS:
            raise ValueError(f"stream must be one of {_STREAMS}")
        return Path(self.dir) / f"{self.id}.{stream}"

    @property
    def meta_path(self) -> Path:
        return Path(self.dir) / f"{self.id}.meta.json"


_NOT_FOUND = "output {oid}: not found for this project and session"


class ShellOutputStore:
    """Promotes spools into ``<root>/<project_id>/<session>/`` and pages them back out.

    ``apply_acl=False`` skips the user-only permission step (tests).
    """

    def __init__(self, root: Path | str | None = None, *, apply_acl: bool = True):
        self.root = Path(root) if root is not None else store_root()
        self.apply_acl = apply_acl

    # -- layout --
    def spool_dir(self) -> Path:
        """Same-volume spool directory for ``ShellCapture`` (so promote() is a rename)."""
        self._ensure_root()
        d = self.root / SPOOL_DIRNAME
        _mkdir_private(d)
        return d

    def _ensure_root(self) -> bool:
        _mkdir_private(self.root)
        return _secure_root(self.root) if self.apply_acl else False

    def _session_root(self, project_path, session_id) -> tuple[Path, str, str]:
        pid = project_id(project_path)
        sid = "" if session_id is None else str(session_id)
        return self.root / pid / _session_dir(sid), pid, sid

    # -- write side --
    def promote(self, capture: ShellCapture, *, project_path, session_id, cmd, cwd,
                guard_paths, exit_code, timed_out, duration_ms) -> OutputMeta:
        """Move the spool into the store and write its meta; sweeps at most every 10 min.

        ``cmd`` must be the DISPLAY form (before credential expansion): only its
        first line (≤240 chars) and sha256 are recorded.
        """
        if not capture.finished:
            raise RuntimeError("call capture.wait() before promote()")
        acl_ok = self._ensure_root()
        directory, pid, sid = self._session_root(project_path, session_id)
        _mkdir_private(directory.parent)
        _mkdir_private(directory)
        oid = capture.output_id
        out_path = directory / f"{oid}.stdout"
        err_path = directory / f"{oid}.stderr"
        meta_path = directory / f"{oid}.meta.json"
        _move(capture.stdout_path, out_path)
        _move(capture.stderr_path, err_path)
        if self.apply_acl:
            acl_ok = _restrict_file(out_path) and _restrict_file(err_path) and acl_ok
        now = datetime.now(timezone.utc)
        cmd_text = cmd or ""
        meta = OutputMeta(
            id=oid, project_id=pid, project_path=str(project_path), session_id=sid,
            created_at=_iso(now), expires_at=_iso(now + timedelta(days=RETENTION_DAYS)),
            cmd_sha256=hashlib.sha256(cmd_text.encode("utf-8")).hexdigest(),
            cmd_display=_display(cmd_text), cwd=str(cwd),
            exit_code=int(exit_code), timed_out=bool(timed_out), duration_ms=int(duration_ms),
            stdout=capture.stats.stdout.summary(), stderr=capture.stats.stderr.summary(),
            guard={"cwd": str(cwd), "paths": [str(p) for p in (guard_paths or [])]},
            acl_applied=bool(acl_ok), dir=str(directory),
        )
        _write_json_atomic(meta_path, meta.to_dict())
        if self.apply_acl:
            _restrict_file(meta_path)
        self._maybe_sweep()
        return meta

    # -- the authorization gate --
    def resolve(self, output_id, *, project_path, session_id, guard_check) -> OutputMeta:
        """Resolve an id for THIS project + session under the CURRENT guard rules.

        See the module docstring for the model. Raises ``OutputAccessError``.
        """
        oid = output_id if isinstance(output_id, str) else ""
        shown = oid[:20] if oid else "<empty>"
        if not _ID_RE.match(oid):
            raise OutputAccessError(_NOT_FOUND.format(oid=shown))
        if guard_check is None:                       # fail closed: no verdict, no bytes
            raise OutputAccessError(f"output {oid}: no access-guard check supplied; refusing")
        directory, pid, sid = self._session_root(project_path, session_id)
        meta = _load_meta(directory / f"{oid}.meta.json")
        if meta is None or meta.project_id != pid or (meta.session_id or "") != sid or meta.id != oid:
            raise OutputAccessError(_NOT_FOUND.format(oid=oid))
        expires = _parse_iso(meta.expires_at)
        if expires is None or expires <= datetime.now(timezone.utc):
            raise OutputAccessError(f"output {oid}: expired (retention {RETENTION_DAYS} days); re-run the command")
        if not (meta.stream_path("stdout").is_file() and meta.stream_path("stderr").is_file()):
            raise OutputAccessError(f"output {oid}: swept or deleted; re-run the command")
        for path in [meta.guard.get("cwd", "")] + list(meta.guard.get("paths", []) or []):
            if not path:
                continue
            denial = guard_check(path)
            if denial:
                rule = getattr(denial, "rule", "") or ""
                kind = getattr(denial, "kind", "") or "deny"
                detail = f" ({kind} rule {rule})" if rule else f" ({kind})"
                raise OutputAccessError(f"output {oid}: {path} is no longer readable under the "
                                        f"current access rules{detail}; re-run the command")
        return meta

    # -- read side --
    def read(self, meta: OutputMeta, stream: str = "stdout", *, lines=None,
             max_bytes: int = DEFAULT_READ_BYTES) -> str:
        """1-based inclusive line window (like c3_read); clipped to ``max_bytes``."""
        path = meta.stream_path(stream)
        total = int((getattr(meta, stream) or {}).get("lines", 0))
        start, end = _window(lines)
        out: list[str] = []
        used = 0
        first = last = 0
        stopped_at = 0
        n = 0
        with open(path, "rb") as fh:
            for n, raw in enumerate(fh, 1):
                if n < start:
                    continue
                if end is not None and n > end:
                    break
                line = _clip_line(raw.decode("utf-8", "replace").rstrip("\r\n"))
                size = len(line.encode("utf-8")) + 1
                if used + size > max_bytes and out:
                    stopped_at = n
                    break
                if used + size > max_bytes:            # even one line is over budget: keep a clipped head
                    line = line[: max(max_bytes - 64, 0)] + "…"
                    size = len(line.encode("utf-8")) + 1
                out.append(line)
                used += size
                first = first or n
                last = n
        if not out:
            return f"[{stream}: no lines in {start}-{end if end is not None else 'end'}; stream has {total} lines]"
        text = f"[{stream} L{first}-{last} of {total}]\n" + "\n".join(out)
        if stopped_at:
            tail_end = end if end is not None else total
            text += f"\n…[{max_bytes} B budget reached at L{stopped_at}; continue with lines=({stopped_at}, {tail_end})]"
        return text

    def search(self, meta: OutputMeta, pattern: str, stream: str = "stdout", *, context: int = 2,
               max_matches: int = 50, max_bytes: int = DEFAULT_READ_BYTES, flags: int = 0) -> str:
        """Regex search; hits as ``>L{n}: line``, context as `` L{n}: line``, ``---`` between groups."""
        try:
            rx = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"bad pattern {pattern!r}: {exc}") from exc
        path = meta.stream_path(stream)
        context = max(int(context), 0)
        before: deque = deque(maxlen=context)
        out: list[str] = []
        used = 0
        shown = extra = 0
        after_left = 0
        last_emitted = 0
        budget_hit_at = 0

        def emit(s: str) -> bool:
            nonlocal used
            size = len(s.encode("utf-8")) + 1
            if used + size > max_bytes:
                return False
            out.append(s)
            used += size
            return True

        n = 0
        with open(path, "rb") as fh:
            for n, raw in enumerate(fh, 1):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                hit = rx.search(line) is not None
                if hit and shown >= max_matches:      # over the cap: count it, still usable as context
                    extra += 1
                    hit = False
                if budget_hit_at:
                    before.append((n, line))
                    continue
                if hit:
                    pending = [(m, s) for m, s in before if m > last_emitted]
                    first_line = pending[0][0] if pending else n
                    if out and last_emitted and first_line - last_emitted > 1:
                        emit("---")
                    ok = True
                    for m, s in pending:
                        ok = emit(f" L{m}: {_clip_line(s)}")
                        if not ok:
                            break
                    if ok:
                        ok = emit(f">L{n}: {_clip_line(line)}")
                    if not ok:
                        budget_hit_at = n
                    else:
                        shown += 1
                        last_emitted = n
                        after_left = context
                elif after_left > 0:
                    if emit(f" L{n}: {_clip_line(line)}"):
                        last_emitted = n
                        after_left -= 1
                    else:
                        budget_hit_at = n
                before.append((n, line))
        if not out:
            return f"[{stream}: no match for {pattern!r} in {n} lines]"
        if budget_hit_at:
            out.append(f"…[{max_bytes} B budget reached at L{budget_hit_at}; {shown} matches shown]")
        elif extra:
            out.append(f"…[{extra} more matches beyond max_matches={max_matches}; narrow the pattern]")
        return "\n".join(out)

    def tail(self, meta: OutputMeta, stream: str = "stdout", *, lines: int = 50,
             max_bytes: int = DEFAULT_READ_BYTES) -> str:
        """Last ``lines`` lines, newest kept when the byte budget clips."""
        path = meta.stream_path(stream)
        total = int((getattr(meta, stream) or {}).get("lines", 0))
        window: deque = deque(maxlen=max(int(lines), 1))
        n = 0
        with open(path, "rb") as fh:
            for n, raw in enumerate(fh, 1):
                window.append((n, _clip_line(raw.decode("utf-8", "replace").rstrip("\r\n"))))
        if not window:
            return f"[{stream}: empty; 0 lines]"
        kept: list[tuple[int, str]] = []
        used = 0
        for m, line in reversed(window):
            size = len(line.encode("utf-8")) + 1
            if used + size > max_bytes and kept:
                break
            kept.append((m, line))
            used += size
        kept.reverse()
        first, last = kept[0][0], kept[-1][0]
        text = f"[{stream} L{first}-{last} of {total or n}]\n" + "\n".join(s for _, s in kept)
        if len(kept) < len(window):
            text = f"…[{max_bytes} B budget: showing the last {len(kept)} of {len(window)} requested lines]\n" + text
        return text

    def delete(self, meta: OutputMeta) -> None:
        for path in (meta.stream_path("stdout"), meta.stream_path("stderr"), meta.meta_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        _prune_dirs(self.root, Path(meta.dir))

    def list(self, *, project_path, session_id) -> list[OutputMeta]:
        """Only this caller's outputs (same project + session), unexpired, files present."""
        directory, pid, sid = self._session_root(project_path, session_id)
        if not directory.is_dir():
            return []
        now = datetime.now(timezone.utc)
        found: list[OutputMeta] = []
        for entry in sorted(os.listdir(directory)):
            if not _META_RE.match(entry):
                continue
            meta = _load_meta(directory / entry)
            if meta is None or meta.project_id != pid or (meta.session_id or "") != sid:
                continue
            expires = _parse_iso(meta.expires_at)
            if expires is None or expires <= now:
                continue
            if not (meta.stream_path("stdout").is_file() and meta.stream_path("stderr").is_file()):
                continue
            found.append(meta)
        found.sort(key=lambda m: m.created_at)
        return found

    # -- retention --
    def _maybe_sweep(self) -> None:
        marker = self.root / SWEEP_MARKER
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
        """Expired first, then oldest-first to ``RETENTION_BYTES``; touches only our own files."""
        now_dt = _as_dt(now)
        now_ts = now_dt.timestamp()
        deleted = freed = 0
        keep: list[_Entry] = []
        for entry in self._entries():
            if entry.expires is None or entry.expires <= now_dt:
                freed += entry.remove()
                deleted += 1
            else:
                keep.append(entry)
        keep.sort(key=lambda e: e.created)
        total = sum(e.size for e in keep)
        while keep and total > RETENTION_BYTES:
            entry = keep.pop(0)
            total -= entry.size
            freed += entry.remove()
            deleted += 1
        # Orphans: stream files without a meta, and stale spool pieces.
        for path in self._orphans():
            try:
                if now_ts - path.stat().st_mtime >= ORPHAN_AGE_S:
                    freed += path.stat().st_size
                    path.unlink()
                    deleted += 1
            except OSError:
                pass
        for directory in self._session_dirs():
            _prune_dirs(self.root, directory)
        try:
            _mkdir_private(self.root)
            marker = self.root / SWEEP_MARKER
            marker.write_text(_iso(now_dt), encoding="utf-8")
            os.utime(marker, None)
        except OSError:
            pass
        return {"deleted": deleted, "freed_bytes": freed}

    def _session_dirs(self):
        if not self.root.is_dir():
            return
        for pid in sorted(os.listdir(self.root)):
            pdir = self.root / pid
            if not _PID_RE.match(pid) or not pdir.is_dir():
                continue
            for sdir in sorted(os.listdir(pdir)):
                d = pdir / sdir
                if d.is_dir():
                    yield d

    def _entries(self) -> list["_Entry"]:
        found = []
        for d in self._session_dirs():
            for name in os.listdir(d):
                m = _META_RE.match(name)
                if not m:
                    continue
                found.append(_Entry.build(d, m.group(1)))
        return found

    def _orphans(self):
        for d in self._session_dirs():
            for name in os.listdir(d):
                m = _STREAM_RE.match(name)
                if m and not (d / f"{m.group(1)}.meta.json").exists():
                    yield d / name
        spool = self.root / SPOOL_DIRNAME
        if spool.is_dir():
            for name in os.listdir(spool):
                if name.endswith(".part") and _ID_RE.match(name.split(".", 1)[0]):
                    yield spool / name


@dataclass
class _Entry:
    directory: Path
    oid: str
    created: datetime
    expires: datetime | None
    size: int

    @classmethod
    def build(cls, directory: Path, oid: str) -> "_Entry":
        meta_path = directory / f"{oid}.meta.json"
        meta = _load_meta(meta_path)
        size = 0
        for suffix in ("stdout", "stderr", "meta.json"):
            try:
                size += (directory / f"{oid}.{suffix}").stat().st_size
            except OSError:
                pass
        if meta is None:                              # corrupt meta of ours: order by mtime, treat as expired
            try:
                created = datetime.fromtimestamp(meta_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                created = datetime.now(timezone.utc)
            return cls(directory, oid, created, None, size)
        created = _parse_iso(meta.created_at) or datetime.now(timezone.utc)
        return cls(directory, oid, created, _parse_iso(meta.expires_at), size)

    def remove(self) -> int:
        freed = 0
        for suffix in ("stdout", "stderr", "meta.json"):
            path = self.directory / f"{self.oid}.{suffix}"
            try:
                freed += path.stat().st_size
                path.unlink()
            except OSError:
                pass
        return freed


# ── Filesystem helpers ──────────────────────────────────────────────────────

def _window(lines) -> tuple[int, int | None]:
    if lines is None:
        return 1, None
    try:
        start, end = lines
    except (TypeError, ValueError):
        raise ValueError("lines must be a (start, end) pair, 1-based inclusive") from None
    start = int(start)
    end = None if end is None else int(end)
    if start < 1 or (end is not None and end < start):
        raise ValueError(f"bad line window {lines!r}: need 1 <= start <= end")
    return start, end


def _load_meta(path: Path) -> OutputMeta | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return OutputMeta.from_dict(data, path.parent)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _move(src: Path, dst: Path) -> None:
    """Rename; fall back to copy when the volume differs or a handle is still open."""
    try:
        os.replace(src, dst)
        return
    except FileNotFoundError:
        dst.write_bytes(b"")
        return
    except OSError:
        pass
    shutil.copyfile(src, dst)
    try:
        os.remove(src)
    except OSError:
        pass


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


def _restrict_file(path: Path) -> bool:
    """User-only file permissions. Windows files inherit the root ACL (see _secure_root)."""
    if os.name == "nt":
        return True
    try:
        os.chmod(path, 0o600)
        return True
    except OSError:
        return False


def _secure_root(root: Path) -> bool:
    """Make ``root`` user-only once (marker ``.acl``); children inherit.

    Windows: ``icacls <root> /inheritance:r /grant:r "<user>:(OI)(CI)F"`` — wrapped,
    never raises, so promotion succeeds even when the ACL step does not.
    """
    marker = root / ACL_MARKER
    if marker.exists():
        return True
    ok = False
    if os.name == "nt":
        user = os.environ.get("USERNAME") or ""
        if not user:
            try:
                user = getpass.getuser()
            except Exception:
                user = ""
        if user:
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                res = subprocess.run(
                    ["icacls", str(root), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                    capture_output=True, timeout=15, creationflags=flags,
                    stdin=subprocess.DEVNULL,
                )
                ok = res.returncode == 0
            except Exception:
                ok = False
    else:
        try:
            os.chmod(root, 0o700)
            ok = True
        except OSError:
            ok = False
    if ok:
        try:
            marker.write_text("ok", encoding="utf-8")
        except OSError:
            pass
    return ok


def _prune_dirs(root: Path, directory: Path) -> None:
    """rmdir the session dir and then its project dir when empty; never the root."""
    d = directory
    for _ in range(2):
        if d == root or root not in d.parents:
            return
        try:
            os.rmdir(d)                               # only succeeds when empty — foreign files keep it
        except OSError:
            return
        d = d.parent
