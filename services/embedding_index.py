"""
Incremental Embedding Index for semantic code search.

Embeds code chunks from CodeIndex into a chromadb collection using Ollama
embeddings. Tracks file content hashes to only re-embed changed files.
Falls back gracefully when Ollama or chromadb are unavailable.
"""

import gc
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("c3.embedding_index")
_SEARCH_INIT_WAIT_SECONDS = 0.25
# v2 collection: task-prefixed embeddings (2.108.0). The v1 collection is
# dropped best-effort on first init; its vectors are not comparable.
COLLECTION_NAME = "code_embeddings_v2"
LEGACY_COLLECTION_NAME = "code_embeddings"
DEFAULT_MIN_SCORE_NOMIC = 0.62
DEFAULT_MIN_SCORE_OTHER = 0.55
# Upper bound on how long a caller will park waiting for an in-flight build.
# A redundant build is worth far less than a responsive server, so we give up
# and degrade rather than block. See _acquire_build_lock().
_BUILD_LOCK_WAIT_SECONDS = 30.0

# ── Store safety (issue #180) ─────────────────────────────
# A damaged HNSW segment does not just fail to load. chromadb's loader
# allocates as it reads, so one bad segment made a single MCP server commit
# 140-200 GB before the load gave up, and every new session repeated it
# (measured 2026-10-02: count() on a copy of the store, 165 GB private, 0.25 GB
# working set, then "Error loading hnsw index"). An in-process probe cannot
# undo that commit, so an existing store is first opened in a child process
# under a memory cap, and only opened here if the child could read it.
_PROBE_TIMEOUT_SECONDS = 180.0
_PROBE_POLL_SECONDS = 0.1
_PROBE_MIN_CAP_BYTES = 4 * 1024 ** 3  # floor; scaled up for big stores below
_PROBE_CAP_STORE_MULTIPLE = 4
_PROBE_OK_FILE = "store_probe_ok.json"
_PROBE_OK_MARKER = "C3_STORE_PROBE_OK"
# Each quarantine keeps a full copy of the store for post-mortem. Unbounded,
# that reached 284 copies and 384 GB in one project; keep only the newest few.
_QUARANTINE_KEEP = 2
_OWNER_LOCK_FILE = "chromadb.owner.lock"

_PROBE_CHILD_CODE = """
import sys
import chromadb
from chromadb.config import Settings
client = chromadb.PersistentClient(
    path=sys.argv[1], settings=Settings(anonymized_telemetry=False))
listed = client.list_collections()
names = {c if isinstance(c, str) else getattr(c, "name", "") for c in listed}
if sys.argv[2] in names:
    client.get_collection(sys.argv[2]).count()
print("C3_STORE_PROBE_OK", flush=True)
"""


class StoreBusyError(RuntimeError):
    """Another C3 process owns this project's embedding store."""


def _private_bytes(proc) -> int:
    """Committed (Windows) or resident (POSIX) bytes of a psutil.Process."""
    info = proc.memory_info()
    return int(getattr(info, "private", 0) or info.rss)


def _windows_job_memory_cap(pid: int, cap_bytes: int):
    """Put *pid* in a Job Object whose per-process commit limit is *cap_bytes*.

    Polling alone is not enough here: the corrupt-segment loader committed
    ~100 GB between two 100 ms polls (measured 2026-10-02). Under a job limit
    the allocation fails inside the child instead. Returns the job handle
    (keep it open while the child runs) or None when unsupported.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001

        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        info.ProcessMemoryLimit = cap_bytes
        if not k32.SetInformationJobObject(
                wintypes.HANDLE(job), JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(wintypes.HANDLE(job))
            return None
        proc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not proc:
            k32.CloseHandle(wintypes.HANDLE(job))
            return None
        try:
            assigned = k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(proc))
        finally:
            k32.CloseHandle(wintypes.HANDLE(proc))
        if not assigned:
            k32.CloseHandle(wintypes.HANDLE(job))
            return None
        return job
    except Exception:
        return None


def _close_job(job) -> None:
    if job is None:
        return
    try:
        import ctypes
        from ctypes import wintypes
        ctypes.WinDLL("kernel32").CloseHandle(wintypes.HANDLE(job))
    except Exception:
        pass


def run_isolated_probe(
    args: list,
    *,
    cap_bytes: int,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
    marker: str = _PROBE_OK_MARKER,
) -> tuple:
    """Run ``python <args>`` in a child that is killed if it outgrows *cap_bytes*.

    Returns ``(ok, reason)``. ``ok`` needs a zero exit AND *marker* on stdout,
    so a child that dies quietly is a failure, not a pass. The cap is enforced
    by polling the child's memory (psutil when installed; RLIMIT_AS on POSIX
    otherwise), which bounds the damage to the cap plus one poll interval.
    """
    try:
        import psutil  # optional: C3 does not declare it
    except Exception:
        psutil = None

    preexec = None
    if psutil is None and os.name != "nt":
        def preexec():  # pragma: no cover - POSIX without psutil
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (cap_bytes, cap_bytes))

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    import tempfile

    # Files, not pipes: a child that logs more than a pipe buffer while nobody
    # reads it would block and be misreported as a timeout.
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        child = subprocess.Popen(
            [sys.executable, *args],
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            preexec_fn=preexec,
            creationflags=creationflags,
        )
        job = _windows_job_memory_cap(child.pid, cap_bytes)
        try:
            reason = _watch_probe(child, psutil, cap_bytes, timeout)
        finally:
            _close_job(job)
        if reason:
            return False, reason
        out_f.seek(0)
        err_f.seek(0)
        text = out_f.read().decode("utf-8", "replace")
        err = err_f.read().decode("utf-8", "replace")
    if child.returncode == 0 and marker in text:
        return True, ""
    tail = err.strip().splitlines()
    detail = tail[-1] if tail else f"exit code {child.returncode}"
    capped = " (under a hard memory cap)" if job is not None else ""
    return False, f"probe failed{capped}: {detail[:300]}"


def _watch_probe(child, psutil, cap_bytes, timeout) -> str:
    """Wait for *child*; kill it on the cap or the deadline. Returns a reason or ""."""
    watched = None
    if psutil is not None:
        try:
            watched = psutil.Process(child.pid)
        except Exception:
            watched = None
    deadline = time.monotonic() + timeout
    peak = 0
    reason = ""
    while child.poll() is None:
        if watched is not None:
            try:
                peak = max(peak, _private_bytes(watched))
            except Exception:
                pass
            if peak > cap_bytes:
                reason = (f"probe exceeded its memory cap "
                          f"({peak / 1024 ** 3:.1f} GB > {cap_bytes / 1024 ** 3:.1f} GB)")
                break
        if time.monotonic() >= deadline:
            reason = f"probe timed out after {timeout:.0f}s"
            break
        time.sleep(_PROBE_POLL_SECONDS)
    if reason:
        child.kill()
    try:
        child.wait(timeout=30)
    except Exception:
        pass
    return reason


class _OwnerLock:
    """Non-blocking, process-lifetime exclusive lock on one file.

    chromadb's PersistentClient is not safe for several processes at once, yet
    every C3 MCP server for a project opened the same store. The OS drops the
    lock when the holder dies, so a crashed server never strands it.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def try_acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "ab")  # never read/written; fd exists to hold the lock
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
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
        finally:
            fh.close()


class EmbeddingIndex:
    """Semantic code search via embeddings over CodeIndex chunks."""

    def __init__(
        self,
        project_path: str,
        ollama_client,
        embed_model: str = "nomic-embed-text",
        batch_size: int = 32,
        min_score: "float | None" = None,
    ):
        self.project_path = Path(project_path)
        self.ollama = ollama_client
        self.embed_model = embed_model
        self.batch_size = batch_size
        # Admission floor on cosine similarity (`search_dense_min_score`).
        # A dense index always has nearest neighbours; without a floor a
        # query with no valid answer returns ten of them, and fusion would
        # carry them into `code` results (measured: zero-result accuracy
        # 1.0 -> 0.4 on the fixture). None -> the model default.
        self._min_score_override = None if min_score is None else float(min_score)

        self._index_dir = self.project_path / ".c3" / "embeddings"
        self._index_dir.mkdir(parents=True, exist_ok=True)
        # v2 (2.108.0): documents are embedded with the model's task prefix
        # (nomic: `search_document:` / `search_query:`), so vectors from the
        # unprefixed v1 collection are not comparable and are rebuilt lazily
        # under a new name. The hash file moves with it.
        self._hash_file = self._index_dir / "file_hashes_v2.json"

        self._chroma_client = None
        self._collection = None
        self._available = False
        self._unavailable_why = ""  # set when chromadb is installed but unusable
        self._owner_lock = _OwnerLock(self._index_dir / _OWNER_LOCK_FILE)
        self._ollama_ok = False
        self._ollama_up = False
        self._model_ok = False
        self._file_hashes: dict[str, str] = {}  # doc_id -> content hash
        self._lock = threading.Lock()
        self._lock_warned = False  # WARN once, not once per blocked caller
        self._chunk_map: dict[str, dict] = {}  # chunk_id -> metadata

        # Heavy backend init (chromadb import/client + ollama probe) and hash
        # load are deferred to first use so build_runtime stays fast and the MCP
        # handshake doesn't time out. See _ensure_ready().
        self._initialized = False
        self._init_lock = threading.Lock()

    # ── Backend init ──────────────────────────────────────

    def _ensure_ready(self, wait_timeout: float | None = None) -> bool:
        """Lazily init chromadb/ollama backends + file hashes on first use.

        Deferred from __init__ so build_runtime (and the MCP handshake) stays
        fast. Idempotent and thread-safe via double-checked locking.
        """
        if self._initialized:
            return True
        if wait_timeout is None:
            acquired = self._init_lock.acquire()
        else:
            acquired = self._init_lock.acquire(timeout=max(0.0, wait_timeout))
        if not acquired:
            return False
        try:
            if self._initialized:
                return True
            self._init_backends()
            self._load_hashes()
            self._initialized = True
            return True
        finally:
            self._init_lock.release()

    def warm(self):
        """Pre-initialize backends (used for background warm-up)."""
        self._ensure_ready()

    def _init_backends(self):
        """Initialize chromadb collection and check Ollama."""
        try:
            self._open_chroma()
            self._available = True
            self._drop_legacy_collection()
        except StoreBusyError as e:
            log.info("embedding index disabled in this process: %s", e)
            self._unavailable_why = str(e)
            self._available = False
        except ImportError as e:
            log.debug("chromadb unavailable for embedding index: %s", e)
            self._available = False
        except Exception as e:
            log.warning("embedding store unusable: %s", e)
            self._unavailable_why = f"embedding store unusable: {e}"
            self._available = False

        try:
            self._ollama_up = self.ollama.is_available(timeout=2)
            self._model_ok = (
                self._ollama_up and self.ollama.has_model(self.embed_model)
            )
        except Exception:
            self._ollama_up = False
            self._model_ok = False
        self._ollama_ok = self._ollama_up and self._model_ok

    def _open_chroma(self, _retried: bool = False) -> None:
        """Open the persistent collection, quarantining a corrupt store once.

        A damaged HNSW segment does not surface at open time: the Rust backend
        accepts the client and the collection handle, then faults on the first
        real read. When that read happens on the background index thread it has
        taken the whole MCP process down with an access violation, which the
        host reports only as a dead server (seen 2026-07-17, 2026-09-06).

        So probe here, on the init path, where the failure is still a catchable
        Python exception, and treat a failed probe as a corrupt store: close the
        client, move the directory aside, and reopen empty. The store is a
        rebuildable cache, so the cost is one re-embed, not lost data.
        """
        import chromadb
        from chromadb.config import Settings

        persist_dir = self._index_dir / "chromadb"
        if not self._owner_lock.try_acquire():
            raise StoreBusyError(
                "embedding store is in use by another C3 server for this "
                "project; dense search is off in this one")
        persist_dir.mkdir(parents=True, exist_ok=True)
        if not _retried and self._store_has_segments(persist_dir):
            ok, why = self._probe_store_isolated(persist_dir)
            if not ok:
                log.warning(
                    "embedding store failed its isolated probe (%s); "
                    "quarantining and rebuilding", why)
                self._quarantine_store(persist_dir)
                persist_dir.mkdir(parents=True, exist_ok=True)
        self._chroma_client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        self._collection = self._chroma_client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        try:
            self._collection.count()
        except Exception as e:
            if _retried:
                raise
            log.warning(
                "embedding store failed its health probe (%s); "
                "quarantining and rebuilding",
                e,
            )
            self._quarantine_store(persist_dir)
            self._open_chroma(_retried=True)

    def _close_chroma(self) -> None:
        """Drop the client so Windows releases its handle on chroma.sqlite3."""
        client = self._chroma_client
        self._collection = None
        self._chroma_client = None
        try:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        gc.collect()

    @staticmethod
    def _store_has_segments(persist_dir: Path) -> bool:
        """True when the store holds at least one on-disk HNSW segment."""
        try:
            return any(persist_dir.glob("*/header.bin"))
        except OSError:
            return False

    @staticmethod
    def _store_signature(persist_dir: Path) -> dict:
        sig = {}
        for f in sorted(persist_dir.rglob("*")):
            try:
                if f.is_file():
                    st = f.stat()
                    sig[f.relative_to(persist_dir).as_posix()] = [st.st_size, st.st_mtime_ns]
            except OSError:
                continue
        return sig

    def _probe_store_isolated(self, persist_dir: Path) -> tuple:
        """Open the store in a capped child first; skip if unchanged since a pass.

        Returns ``(ok, reason)``. A pass is remembered against the files' sizes
        and mtimes, so an untouched store is not re-probed on every start.
        """
        sig = self._store_signature(persist_dir)
        ok_file = self._index_dir / _PROBE_OK_FILE
        try:
            if json.loads(ok_file.read_text(encoding="utf-8")) == sig:
                return True, ""
        except Exception:
            pass
        store_bytes = sum(v[0] for v in sig.values())
        cap = max(_PROBE_MIN_CAP_BYTES, _PROBE_CAP_STORE_MULTIPLE * store_bytes)
        started = time.monotonic()
        try:
            ok, why = run_isolated_probe(
                ["-c", _PROBE_CHILD_CODE, str(persist_dir), COLLECTION_NAME],
                cap_bytes=cap,
            )
        except Exception as e:  # could not spawn: the in-process probe still runs
            log.warning("isolated store probe could not run (%s); skipping", e)
            return True, ""
        log.info("isolated store probe: ok=%s in %.1fs %s",
                 ok, time.monotonic() - started, why)
        if ok:
            try:
                # The child may have compacted the store; record what it left.
                ok_file.write_text(json.dumps(self._store_signature(persist_dir)),
                                   encoding="utf-8")
            except OSError:
                pass
        return ok, why

    def _prune_quarantines(self) -> None:
        olds = sorted(self._index_dir.glob("quarantine_corrupt_*"))
        for old in (olds[:-_QUARANTINE_KEEP] if _QUARANTINE_KEEP else olds):
            shutil.rmtree(old, ignore_errors=True)

    def _quarantine_store(self, persist_dir: Path) -> None:
        """Move a corrupt store and its hash file aside for post-mortem.

        The hashes go with it: they claim vectors that the fresh store does not
        have, so leaving them behind would suppress the very rebuild this is
        meant to trigger.

        Rename only, never copy. When another process still had the files open,
        ``shutil.move`` fell back to copying the whole store and then failed to
        delete the original, so every session left one more full copy behind
        and reopened the same corrupt store (issue #180). A failed rename now
        raises and the index degrades for this process.
        """
        self._close_chroma()
        dest = self._index_dir / f"quarantine_corrupt_{datetime.now():%Y%m%d_%H%M%S_%f}"
        dest.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(str(persist_dir), str(dest / persist_dir.name))
        except OSError as e:
            try:
                dest.rmdir()
            except OSError:
                pass
            raise RuntimeError(
                f"could not move the corrupt embedding store aside ({e})") from e
        (self._index_dir / _PROBE_OK_FILE).unlink(missing_ok=True)
        try:
            if self._hash_file.exists():
                shutil.move(str(self._hash_file), str(dest / self._hash_file.name))
        except OSError:
            self._hash_file.unlink(missing_ok=True)
        self._file_hashes = {}
        log.warning("quarantined corrupt embedding store to %s", dest)
        self._prune_quarantines()

    def _drop_legacy_collection(self) -> None:
        """Best-effort removal of the pre-2.108.0 collection and hash file.

        Its vectors were embedded without task prefixes, so they cannot be
        queried alongside v2 ones, and they cost ~200 MB on a 500-file repo.
        ``delete_collection`` is a metadata call, but this backend has hung
        inside its Rust bindings before (see ``_remove_file_chunks``), so the
        call runs in a daemon thread with a bound: a hang is abandoned, never
        waited on.
        """
        client = self._chroma_client
        if client is None:
            return
        try:
            listed = client.list_collections()
            names = {c if isinstance(c, str) else getattr(c, "name", "") for c in listed}
        except Exception:
            return
        legacy_hashes = self._index_dir / "file_hashes.json"
        if LEGACY_COLLECTION_NAME not in names:
            try:
                legacy_hashes.unlink(missing_ok=True)
            except OSError:
                pass
            return

        def _drop():
            try:
                client.delete_collection(LEGACY_COLLECTION_NAME)
                log.info("dropped legacy embedding collection %s", LEGACY_COLLECTION_NAME)
            except Exception as exc:
                log.debug("legacy collection drop failed: %s", exc)

        worker = threading.Thread(target=_drop, name="c3-drop-legacy-embeddings", daemon=True)
        worker.start()
        worker.join(2.0)
        try:
            legacy_hashes.unlink(missing_ok=True)
        except OSError:
            pass

    # ── Task prefixes ─────────────────────────────────────

    def _uses_task_prefixes(self) -> bool:
        """nomic-embed-text (v1/v1.5) is trained with task prefixes and loses
        measurable retrieval quality without them; other models get none."""
        return self.embed_model.lower().startswith("nomic-embed")

    @property
    def min_score(self) -> float:
        """Cosine similarity below which a neighbour is not a candidate.

        Measured on the relevance fixture with nomic-embed-text v1.5 and task
        prefixes: queries with no valid answer top out at 0.47-0.58, real
        answers start at 0.70. 0.62 splits them with margin on both sides.
        Other models get a looser 0.55 until measured; override with
        `search_dense_min_score` in the hybrid config.
        """
        if self._min_score_override is not None:
            return self._min_score_override
        return DEFAULT_MIN_SCORE_NOMIC if self._uses_task_prefixes() else DEFAULT_MIN_SCORE_OTHER

    def _doc_text(self, text: str) -> str:
        return f"search_document: {text}" if self._uses_task_prefixes() else text

    def _query_text(self, query: str) -> str:
        return f"search_query: {query}" if self._uses_task_prefixes() else query

    @property
    def ready(self) -> bool:
        """True when both chromadb and Ollama embeddings are available."""
        return self._available and self._ollama_ok

    def probe(self) -> dict:
        """Explicitly initialize backends and report readiness.

        ``ready`` alone never triggers backend init (status reporters must
        stay cheap for build_runtime/MCP handshake), so gating a build on a
        fresh instance's ``ready`` always skipped. Init/CLI flows call this
        instead: it pays the one-time backend cost, then reports truthfully.
        """
        self._ensure_ready()
        return {
            "ready": self.ready,
            "chromadb": self._available,
            "ollama": self._ollama_up,
            "model": self._model_ok,
        }

    def unavailable_reason(self) -> str:
        """Human-readable reason ``ready`` is False (after probe/init)."""
        if not self._available:
            return self._unavailable_why or "chromadb not installed"
        if not self._ollama_up:
            return "Ollama not reachable"
        if not self._model_ok:
            return (f"embed model '{self.embed_model}' not pulled "
                    f"(run: ollama pull {self.embed_model})")
        return ""

    # ── Hash tracking ─────────────────────────────────────

    def _load_hashes(self):
        """Load persisted file content hashes."""
        if self._hash_file.exists():
            try:
                with open(self._hash_file, encoding='utf-8') as f:
                    self._file_hashes = json.load(f)
            except Exception:
                self._file_hashes = {}

    def _save_hashes(self):
        """Persist file content hashes."""
        try:
            with open(self._hash_file, "w", encoding='utf-8') as f:
                json.dump(self._file_hashes, f)
        except Exception:
            pass

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode(errors="replace")).hexdigest()[:16]

    # ── Build / Update ────────────────────────────────────

    def _acquire_build_lock(self, timeout: float | None = None) -> bool:
        """Acquire the build lock with a bound. Mirrors ``_ensure_ready``.

        An unbounded ``with self._lock`` turns any slow backend call into a
        dead server: whoever holds the lock never returns, every later caller
        parks behind it forever, and the MCP client kills the tool call at its
        own timeout while the event loop still looks perfectly healthy. A
        bounded acquire degrades instead — the caller skips the embedding work
        and serves its request without it.

        Returns True when the lock is held (caller MUST release it).
        """
        wait = _BUILD_LOCK_WAIT_SECONDS if timeout is None else timeout
        if self._lock.acquire(timeout=max(0.0, wait)):
            return True
        if not self._lock_warned:
            self._lock_warned = True
            log.warning(
                "Embedding index build lock still held after %.1fs — skipping "
                "this build. Semantic search keeps serving whatever is already "
                "indexed; this is logged once per index instance.",
                wait,
            )
        return False

    def _busy_result(self) -> dict:
        """Build stats shaped like a normal return, marked degraded."""
        return {
            "error": "Embedding index busy (build already in flight); skipped",
            "available": True,
            "degraded": True,
            "files_processed": 0,
            "files_skipped": 0,
            "chunks_embedded": 0,
            "chunks_skipped": 0,
            "errors": 0,
            "total_embedded": 0,
        }

    def build(self, code_index, force: bool = False, on_progress=None) -> dict:
        """Build or incrementally update the embedding index from CodeIndex chunks.

        Args:
            code_index: A CodeIndex instance with populated chunks/documents.
            force: If True, re-embed all files regardless of hash.
            on_progress: callable(files_done, files_total, chunks_embedded),
                invoked per file (skipped files count as done).

        Returns:
            Stats dict with files_processed, chunks_embedded, chunks_skipped, etc.
        """
        self._ensure_ready()
        if not self.ready:
            return {"error": "Embedding backends unavailable", "available": False}

        if not code_index.chunks:
            code_index._load_index()
        if not code_index.chunks:
            return {"error": "No code index chunks found. Build code index first."}

        # Group chunks by doc_id (file)
        chunks_by_file: dict[str, list[tuple[str, dict]]] = {}
        for chunk_id, chunk in code_index.chunks.items():
            doc_id = chunk.get("doc_id", "")
            if doc_id:
                chunks_by_file.setdefault(doc_id, []).append((chunk_id, chunk))

        files_processed = 0
        chunks_embedded = 0
        chunks_skipped = 0
        files_skipped = 0
        errors = 0
        stale_ids = []
        files_total = len(chunks_by_file)

        def _report():
            if on_progress is not None:
                try:
                    on_progress(files_processed + files_skipped, files_total,
                                chunks_embedded)
                except Exception:
                    pass

        if not self._acquire_build_lock():
            return self._busy_result()
        try:
            # Detect deleted files — remove their embeddings
            indexed_files = set(self._file_hashes.keys())
            current_files = set(chunks_by_file.keys())
            for removed_file in indexed_files - current_files:
                self._remove_file_chunks(removed_file)
                del self._file_hashes[removed_file]

            for doc_id, file_chunks in chunks_by_file.items():
                # Check if file content changed
                content = "".join(c.get("content", "") for _, c in file_chunks)
                new_hash = self._content_hash(content)

                if not force and self._file_hashes.get(doc_id) == new_hash:
                    files_skipped += 1
                    chunks_skipped += len(file_chunks)
                    _report()
                    continue

                # Remove old chunks for this file before re-embedding
                self._remove_file_chunks(doc_id)

                # Batch embed
                batch_ids = []
                batch_texts = []
                batch_metas = []
                for chunk_id, chunk in file_chunks:
                    text = chunk.get("content", "").strip()
                    if not text or len(text) < 20:
                        chunks_skipped += 1
                        continue

                    # Prefix with file path + symbol for richer embeddings
                    name = chunk.get("name", "")
                    prefix = f"File: {doc_id}"
                    if name:
                        prefix += f" | {chunk.get('type', 'symbol')}: {name}"
                    # Task prefix lands on the header line; search() strips
                    # that first line when it hands content back.
                    embed_text = self._doc_text(f"{prefix}\n{text}")

                    batch_ids.append(chunk_id)
                    batch_texts.append(embed_text)
                    batch_metas.append({
                        "doc_id": doc_id,
                        "name": name or "",
                        "type": chunk.get("type", "chunk"),
                        "line_start": chunk.get("line_start", 0),
                        "line_end": chunk.get("line_end", 0),
                    })

                    if len(batch_ids) >= self.batch_size:
                        ok = self._embed_batch(batch_ids, batch_texts, batch_metas)
                        if ok:
                            chunks_embedded += len(batch_ids)
                        else:
                            errors += len(batch_ids)
                        batch_ids, batch_texts, batch_metas = [], [], []

                # Flush remaining batch
                if batch_ids:
                    ok = self._embed_batch(batch_ids, batch_texts, batch_metas)
                    if ok:
                        chunks_embedded += len(batch_ids)
                    else:
                        errors += len(batch_ids)

                self._file_hashes[doc_id] = new_hash
                files_processed += 1
                _report()

            self._save_hashes()
        finally:
            self._lock.release()

        return {
            "files_processed": files_processed,
            "files_skipped": files_skipped,
            "chunks_embedded": chunks_embedded,
            "chunks_skipped": chunks_skipped,
            "errors": errors,
            "total_embedded": self._collection.count() if self._collection else 0,
        }

    def _embed_batch(self, ids: list, texts: list, metas: list) -> bool:
        """Embed and store a batch of chunks. Returns True on success."""
        try:
            embeddings = self.ollama.embed_batch(texts, model=self.embed_model)
            if not embeddings or len(embeddings) != len(ids):
                return False
            self._collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=texts,
                metadatas=metas,
            )
            return True
        except Exception as e:
            log.debug("Embedding batch failed: %s", e)
            return False

    def _remove_file_chunks(self, doc_id: str):
        """Remove all embedded chunks belonging to a file.

        Resolves the ids first and deletes by id. It never calls
        ``delete(where=...)``.

        ``collection.delete(where=...)`` has been observed to never return
        inside the chromadb Rust bindings (``chromadb/api/rust.py``,
        ``RustBindingsAPI._delete``): two py-spy dumps four minutes apart with
        byte-identical frames, 0.031s of CPU over 3s, and no writes to
        chroma.sqlite3 for ten hours. Because this is a *hang* and not an
        exception, the ``except``-guarded fallback that used to live here could
        never fire — the comment right below it already suspected the
        where-delete, but an ``except`` clause cannot catch a thread that never
        comes back. Worse, build() called this while holding the build lock, so
        one wedged delete took every later caller down with it.

        Deleting by explicit id keeps the metadata filtering on ``get()``,
        which does return, and leaves ``delete()`` with the one argument shape
        that has never been seen to stall.
        """
        if not self._collection:
            return
        try:
            try:
                # include=[] skips fetching documents/embeddings we throw away.
                results = self._collection.get(
                    where={"doc_id": doc_id}, include=[])
            except Exception:
                # Older chromadb (we support >=0.4.24) may reject include=[].
                results = self._collection.get(where={"doc_id": doc_id})
            ids = (results or {}).get("ids") or []
            if ids:
                self._collection.delete(ids=ids)
        except Exception as e:
            log.debug("Removing chunks for %s failed: %s", doc_id, e)

    # ── Search ────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        max_tokens: int = 2000,
    ) -> list[dict]:
        """Semantic search over embedded code chunks.

        Returns list of dicts with: file, lines, name, type, content, score, tokens.
        """
        if not self._ensure_ready(wait_timeout=_SEARCH_INIT_WAIT_SECONDS):
            return []
        if not self.ready or not self._collection or self._collection.count() == 0:
            return []

        try:
            query_embedding = self.ollama.embed(self._query_text(query), model=self.embed_model)
            if not query_embedding:
                return []

            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k * 2, self._collection.count()),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            log.debug("Semantic search failed: %s", e)
            return []

        if not results or not results.get("ids") or not results["ids"][0]:
            return []

        ids = results["ids"][0]
        documents = results["documents"][0] if results.get("documents") else []
        metadatas = results["metadatas"][0] if results.get("metadatas") else []
        distances = results["distances"][0] if results.get("distances") else []

        from core import count_tokens

        output = []
        total_tokens = 0
        for i, chunk_id in enumerate(ids):
            meta = metadatas[i] if i < len(metadatas) else {}
            doc = documents[i] if i < len(documents) else ""
            dist = distances[i] if i < len(distances) else 1.0

            # chromadb cosine distance: 0 = identical, 2 = opposite
            score = max(0.0, 1.0 - dist)
            if score < self.min_score:
                continue  # a neighbour, not an answer

            # Strip the prefix we added during embedding
            content = doc
            if "\n" in content:
                content = content.split("\n", 1)[1]

            tok = count_tokens(content)
            if total_tokens + tok > max_tokens and output:
                break

            line_start = meta.get("line_start", 0)
            line_end = meta.get("line_end", 0)
            lines_str = f"{line_start}-{line_end}" if line_start else "?"

            output.append({
                "file": meta.get("doc_id", "?"),
                "lines": lines_str,
                "name": meta.get("name", ""),
                "type": meta.get("type", "chunk"),
                "content": content,
                "score": round(score, 4),
                "tokens": tok,
            })
            total_tokens += tok

            if len(output) >= top_k:
                break

        return output

    def candidates(self, query: str, limit: int = 40) -> list[tuple[str, float]]:
        """``[(chunk_id, similarity)]`` best first — the raw ranked list that
        ``CodeIndex`` fuses with its lexical candidates (services/retrieval).

        Chunk ids are the CodeIndex chunk ids (``build`` upserts under them),
        so the caller can join back to its own chunks and apply filters.
        """
        if not self._ensure_ready(wait_timeout=_SEARCH_INIT_WAIT_SECONDS):
            return []
        if not self.ready or not self._collection or self._collection.count() == 0:
            return []
        try:
            query_embedding = self.ollama.embed(self._query_text(query), model=self.embed_model)
            if not query_embedding:
                return []
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=min(max(1, int(limit)), self._collection.count()),
                include=["distances"],
            )
        except Exception as e:
            log.debug("Dense candidates failed: %s", e)
            return []
        ids = (results.get("ids") or [[]])[0] if results else []
        distances = (results.get("distances") or [[]])[0] if results else []
        floor = self.min_score
        out = []
        for i, cid in enumerate(ids):
            dist = distances[i] if i < len(distances) else 1.0
            score = max(0.0, 1.0 - float(dist))
            if score < floor:
                break  # sorted by distance: everything after is further away
            out.append((cid, score))
        return out

    # ── Stats ─────────────────────────────────────────────

    def get_stats(self) -> dict:
        count = self._collection.count() if self._collection else 0
        return {
            "ready": self.ready,
            "chromadb_available": self._available,
            "ollama_available": self._ollama_ok,
            "embed_model": self.embed_model,
            "total_embedded_chunks": count,
            "files_tracked": len(self._file_hashes),
        }
