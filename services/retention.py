"""Storage retention & rotation for .c3 data files (P5).

Every JSONL store under ``.c3/`` historically grew forever (append-only,
no TTL, no compaction). This module provides the shared retention
primitives plus a rate-limited ``RetentionManager`` sweep that existing
background agents invoke — no new agent thread is created.

Rotation model
--------------
``rotate_jsonl`` renames the live file into ``.c3/archive/`` (atomic on
Windows via ``os.replace``) and gzip-compresses it there as
``<name>.<UTC-date>.jsonl.gz``. Writers in this codebase open-append per
write (they never hold a long-lived handle), so a rename between writes is
safe: the next append simply recreates a fresh live file. If another
process happens to hold the file open at that instant, the rename fails
and rotation is retried on a later check. Records are never deleted by
rotation — they move to the archive; ``purge_archives`` applies the
long-term TTL (and the edit-ledger archive is exempt by default, because
the ledger is an audit trail).

Configuration lives in ``.c3/config.json`` under a ``"retention"`` section,
merged with ``RETENTION_DEFAULTS`` (same pattern as the other services).

All entry points are failure-safe: retention must never break a write
path, so errors are swallowed and the operation is retried later.
"""

import gzip
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

RETENTION_DEFAULTS = {
    "enabled": True,
    # Live-file size caps (rotation thresholds), in megabytes.
    "activity_log_max_mb": 5.0,
    "notifications_max_mb": 2.0,
    "telemetry_max_mb": 5.0,
    "edit_ledger_max_mb": 10.0,
    # Edit ledger: only entries older than this AND fully enriched are
    # eligible for archival (keeps the enricher's pending-patch flow intact).
    "edit_ledger_keep_days": 14,
    # Agent-artifact tracking (.c3/agent_artifacts/): history log cap,
    # per-artifact version cap, and orphan-blob GC age guard.
    "artifact_history_max_mb": 2.0,
    "artifact_max_versions": 20,
    "artifact_blob_orphan_days": 7,
    # Archive TTLs in days; 0 = keep forever.
    "archive_keep_days": 90,
    "edit_ledger_archive_keep_days": 0,   # audit trail — never purged by default
    # Session snapshot cap (.c3/sessions/session_*.json).
    "sessions_max_files": 50,
    "sessions_archive": True,             # gzip pruned sessions instead of deleting
    # Notifications: acknowledged entries younger than this are kept live so
    # the store's ack-cooldown suppression window still works.
    "notifications_min_age_minutes": 120,
    # Minimum seconds between RetentionManager sweeps.
    "sweep_interval_seconds": 300,
}

# <stem>.<YYYY-MM-DD>[_HHMMSS][-N].jsonl[.gz]
_ARCHIVE_DATE_RE = re.compile(
    r"\.(\d{4}-\d{2}-\d{2})(?:_\d{6})?(?:-\d+)?\.jsonl(?:\.gz)?$"
)

# Config cache: parsing .c3/config.json on every append would be wasteful,
# so loads are memoized per project with a short TTL.
_CONFIG_CACHE: dict = {}
_CONFIG_TTL_SECONDS = 30.0


def load_retention_config(project_path, ttl: float = _CONFIG_TTL_SECONDS) -> dict:
    """Load the ``retention`` section of .c3/config.json, merged with defaults.

    Results are cached per project for ``ttl`` seconds so per-append rotation
    checks stay cheap. Never raises.
    """
    key = str(project_path)
    now = time.monotonic()
    cached = _CONFIG_CACHE.get(key)
    if cached is not None and cached[0] > now and ttl > 0:
        return cached[1]
    section = {}
    try:
        config_file = Path(project_path) / ".c3" / "config.json"
        if config_file.exists():
            data = json.loads(config_file.read_text(encoding="utf-8"))
            raw = data.get("retention", {})
            if isinstance(raw, dict):
                section = raw
    except Exception:
        section = {}
    merged = {**RETENTION_DEFAULTS, **section}
    _CONFIG_CACHE[key] = (now + max(0.0, ttl), merged)
    return merged


def clear_config_cache() -> None:
    """Drop memoized retention configs (used by tests / config editors)."""
    _CONFIG_CACHE.clear()


def archive_dir_for(project_path) -> Path:
    """The project's archive directory (.c3/archive). Not created here."""
    return Path(project_path) / ".c3" / "archive"


def mb_to_bytes(mb) -> int:
    """Convert a (possibly fractional) megabyte knob to bytes; 0 on bad input."""
    try:
        return max(0, int(float(mb) * 1024 * 1024))
    except (TypeError, ValueError):
        return 0


def _unique_archive_path(archive_dir: Path, stem: str,
                         now: Optional[datetime] = None) -> Path:
    """Pick a collision-free ``<stem>.<UTC-date>[_HHMMSS][-N].jsonl.gz`` path.

    Also avoids colliding with an uncompressed ``.jsonl`` staging/fallback
    file of the same name (left behind if a previous gzip step failed).
    """
    moment = now or datetime.now(timezone.utc)
    date = moment.strftime("%Y-%m-%d")

    def _taken(gz: Path) -> bool:
        raw = gz.with_name(gz.name[:-3])  # strip ".gz"
        return gz.exists() or raw.exists()

    candidate = archive_dir / f"{stem}.{date}.jsonl.gz"
    if not _taken(candidate):
        return candidate
    ts = moment.strftime("%H%M%S")
    candidate = archive_dir / f"{stem}.{date}_{ts}.jsonl.gz"
    n = 0
    while _taken(candidate):
        n += 1
        candidate = archive_dir / f"{stem}.{date}_{ts}-{n}.jsonl.gz"
    return candidate


def rotate_jsonl(path, max_bytes: int, archive_dir) -> Optional[Path]:
    """Rotate a grow-forever JSONL file into a gzip archive when oversized.

    Atomic and Windows-safe: the live file is moved with ``os.replace`` into
    the archive dir (writers open-append per write, so their next append
    recreates a fresh live file), then gzip-compressed in place. If the gzip
    step fails, the uncompressed ``.jsonl`` stays in the archive dir — data
    is never lost. If a writer in another process holds the file open at
    rename time, rotation is skipped and retried on a later check.

    Returns the archive path (``.jsonl.gz``, or ``.jsonl`` on gzip failure),
    or None when no rotation happened. Never raises.
    """
    try:
        path = Path(path)
        try:
            if max_bytes <= 0 or not path.exists() or path.stat().st_size < max_bytes:
                return None
        except OSError:
            return None
        archive = Path(archive_dir)
        archive.mkdir(parents=True, exist_ok=True)
        name = path.name
        stem = name[:-6] if name.endswith(".jsonl") else path.stem
        gz_path = _unique_archive_path(archive, stem)
        raw_path = gz_path.with_name(gz_path.name[:-3])  # plain .jsonl staging
        try:
            os.replace(path, raw_path)
        except OSError:
            return None  # another process holds the handle — retry later
        return _compress_archive(raw_path, gz_path)
    except Exception:
        return None


def _compress_archive(raw_path: Path, gz_path: Path) -> Path:
    """gzip ``raw_path`` to ``gz_path``. On failure the raw file survives."""
    tmp = gz_path.with_name(gz_path.name + ".tmp")
    try:
        with open(raw_path, "rb") as src, gzip.open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, gz_path)
        raw_path.unlink()
        return gz_path
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return raw_path  # uncompressed archive — data preserved


def write_archive_lines(archive_dir, stem: str, lines: Iterable[str]) -> Optional[Path]:
    """Write raw JSONL lines into a new gzip archive. Returns None on failure.

    Used by structure-aware rotations (edit ledger, notifications) that must
    confirm the archive exists on disk BEFORE removing anything from the
    live file.
    """
    lines = [ln for ln in lines if ln and ln.strip()]
    if not lines:
        return None
    try:
        archive = Path(archive_dir)
        archive.mkdir(parents=True, exist_ok=True)
        gz_path = _unique_archive_path(archive, stem)
        tmp = gz_path.with_name(gz_path.name + ".tmp")
        try:
            with gzip.open(tmp, "wt", encoding="utf-8", newline="\n") as f:
                for line in lines:
                    f.write(line.rstrip("\n") + "\n")
            os.replace(tmp, gz_path)
            return gz_path
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            return None
    except Exception:
        return None


def write_archive_entries(archive_dir, stem: str, entries: Iterable[dict]) -> Optional[Path]:
    """Serialize dict entries into a new gzip JSONL archive (or None)."""
    try:
        lines = [json.dumps(e) for e in entries]
    except Exception:
        return None
    return write_archive_lines(archive_dir, stem, lines)


def _archive_file_date(path: Path) -> Optional[datetime]:
    """Date encoded in an archive filename, as an aware UTC datetime."""
    m = _ARCHIVE_DATE_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def purge_archives(archive_dir, keep_days: int = 90, prefix: Optional[str] = None,
                   exclude_prefixes: tuple = ()) -> list:
    """Delete archive files older than ``keep_days``. ``keep_days <= 0`` = keep forever.

    Age comes from the date in the filename when present, else file mtime.
    ``prefix`` restricts the purge to matching names; ``exclude_prefixes``
    protects families (e.g. the edit-ledger audit archives) from a general
    purge. Returns the paths removed. Never raises.
    """
    removed: list = []
    try:
        if not keep_days or int(keep_days) <= 0:
            return removed
        archive = Path(archive_dir)
        if not archive.exists():
            return removed
        cutoff = datetime.now(timezone.utc) - timedelta(days=int(keep_days))
        for f in archive.iterdir():
            try:
                if not f.is_file():
                    continue
                name = f.name
                if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")
                        or name.endswith(".json.gz")):
                    continue
                if prefix and not name.startswith(prefix):
                    continue
                if any(name.startswith(p) for p in exclude_prefixes):
                    continue
                aged = _archive_file_date(f)
                if aged is None:
                    aged = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
                if aged < cutoff:
                    f.unlink()
                    removed.append(f)
            except OSError:
                continue
    except Exception:
        pass
    return removed


def cap_session_files(sessions_dir, max_files: int = 50,
                      archive_dir=None) -> list:
    """Keep the newest ``max_files`` session_*.json files; archive or delete the rest.

    Session filenames embed UTC timestamps, so a name sort is a time sort.
    With ``archive_dir`` set, each pruned session is gzip'd there as
    ``<name>.json.gz`` before the original is removed (skipped on archive
    failure, so nothing is lost); without it, older files are deleted.
    Context-snapshot restore (.c3/snapshots/snap_*.json) is a separate store
    and is never touched here. Returns the pruned source paths. Never raises.
    """
    pruned: list = []
    try:
        sessions = Path(sessions_dir)
        if max_files <= 0 or not sessions.exists():
            return pruned
        files = sorted(sessions.glob("session_*.json"))  # oldest first
        if len(files) <= max_files:
            return pruned
        for f in files[: len(files) - max_files]:
            try:
                if archive_dir is not None:
                    archive = Path(archive_dir)
                    archive.mkdir(parents=True, exist_ok=True)
                    gz = archive / f"{f.name}.gz"
                    if gz.exists():
                        gz = archive / f"{f.stem}.{int(time.time() * 1000)}.json.gz"
                    tmp = gz.with_name(gz.name + ".tmp")
                    with open(f, "rb") as src, gzip.open(tmp, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(tmp, gz)
                f.unlink()
                pruned.append(f)
            except OSError:
                continue
    except Exception:
        pass
    return pruned


class RetentionManager:
    """Rate-limited retention sweep for a project's ``.c3`` directory.

    Invoked from an EXISTING background agent (EditLedgerEnricherAgent runs
    every ~10s); ``maybe_run`` costs one monotonic comparison per call and
    only performs the sweep every ``sweep_interval_seconds``. Each step is
    independently failure-safe.

    Live-file rotation for activity_log.jsonl and tool_telemetry.jsonl
    happens at write time in their own modules; this sweep owns the
    structure-aware rotations (edit ledger, notifications), the session
    cap, file-memory pruning, and archive TTL purging.
    """

    def __init__(self, project_path, edit_ledger=None, notifications=None,
                 file_memory=None, sessions_dir=None, artifact_store=None):
        self.project_path = Path(project_path)
        self.edit_ledger = edit_ledger
        self.notifications = notifications
        self.file_memory = file_memory
        self.artifact_store = artifact_store
        self.sessions_dir = (Path(sessions_dir) if sessions_dir
                             else self.project_path / ".c3" / "sessions")
        self._next_run = 0.0

    def maybe_run(self) -> Optional[dict]:
        """Run the sweep if the interval has elapsed. Returns its summary or None."""
        now = time.monotonic()
        if now < self._next_run:
            return None
        cfg = load_retention_config(self.project_path)
        try:
            interval = max(30, int(cfg.get("sweep_interval_seconds", 300)))
        except (TypeError, ValueError):
            interval = 300
        self._next_run = now + interval
        if not cfg.get("enabled", True):
            return None
        return self.run_sweep(cfg)

    def run_sweep(self, cfg: Optional[dict] = None) -> dict:
        """Execute one full retention sweep. Never raises."""
        if cfg is None:
            cfg = load_retention_config(self.project_path)
        archive = archive_dir_for(self.project_path)
        summary: dict = {}

        # 1. Edit ledger — structure-aware rotation (audit trail: archive, never drop).
        if self.edit_ledger is not None:
            try:
                res = self.edit_ledger.rotate_if_needed(
                    mb_to_bytes(cfg.get("edit_ledger_max_mb", 10)),
                    archive,
                    keep_days=int(cfg.get("edit_ledger_keep_days", 14)),
                )
                if res:
                    summary["edit_ledger"] = res
            except Exception:
                pass

        # 2. Notifications — move old acknowledged entries into the archive
        #    once the live file exceeds its cap (unacked entries stay put).
        if self.notifications is not None:
            try:
                nf = self.project_path / ".c3" / "notifications.jsonl"
                max_bytes = mb_to_bytes(cfg.get("notifications_max_mb", 2))
                if max_bytes > 0 and nf.exists() and nf.stat().st_size >= max_bytes:
                    moved = self.notifications.rotate_acknowledged(
                        lambda entries: write_archive_entries(
                            archive, "notifications", entries) is not None,
                        min_age_minutes=int(
                            cfg.get("notifications_min_age_minutes", 120)),
                    )
                    if moved:
                        summary["notifications_archived"] = moved
            except Exception:
                pass

        # 3. Session snapshot cap (.c3/sessions). Snapshot restore uses
        #    .c3/snapshots/snap_*.json — untouched by this step.
        try:
            max_files = int(cfg.get("sessions_max_files", 50))
            dest = archive if cfg.get("sessions_archive", True) else None
            pruned = cap_session_files(self.sessions_dir, max_files, dest)
            if pruned:
                summary["sessions_pruned"] = len(pruned)
        except Exception:
            pass

        # 4. File-memory stale records (tracked files deleted from the repo).
        if self.file_memory is not None:
            try:
                pruned = self.file_memory.prune_stale()
                if pruned:
                    summary["file_memory_pruned"] = len(pruned)
            except Exception:
                pass

        # 5. Agent-artifact store — history log rotation (plain rotation is
        #    restore-safe: the manifest carries the version index) + version
        #    cap + age-guarded orphan-blob GC.
        if self.artifact_store is not None:
            try:
                rotated = rotate_jsonl(
                    self.project_path / ".c3" / "agent_artifacts" / "history.jsonl",
                    mb_to_bytes(cfg.get("artifact_history_max_mb", 2)),
                    archive,
                )
                if rotated:
                    summary["artifact_history_rotated"] = rotated.name
                pruned = self.artifact_store.prune(
                    max_versions=int(cfg.get("artifact_max_versions", 20)),
                    blob_orphan_days=int(cfg.get("artifact_blob_orphan_days", 7)),
                )
                if pruned.get("versions_trimmed") or pruned.get("blobs_deleted"):
                    summary["artifact_prune"] = pruned
            except Exception:
                pass

        # 6. Archive TTL. Edit-ledger archives are excluded from the general
        #    purge and governed by their own knob (0 = keep forever).
        try:
            removed = purge_archives(
                archive, int(cfg.get("archive_keep_days", 90)),
                exclude_prefixes=("edit_ledger",))
            removed += purge_archives(
                archive, int(cfg.get("edit_ledger_archive_keep_days", 0)),
                prefix="edit_ledger")
            if removed:
                summary["archives_purged"] = len(removed)
        except Exception:
            pass

        return summary
