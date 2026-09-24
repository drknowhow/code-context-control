"""Content-addressed pre/post images of c3_edit writes, for c3_edits revert.

Layout: ``.c3/edit_blobs/<sha[:2]>/<sha256>``, each the zlib of the raw file
bytes. Blobs are write-once: a second write of the same content only
refreshes its mtime, so a blob still in use stays out of eviction's way.

Eviction is oldest-mtime-first down to 90% of ``edit.blob_cap_mb`` (default
256 MB) and runs on the first store in a process, then every
``_SWEEP_EVERY_PUTS`` stores or once an hour, whichever comes first.

Blobs hold file contents. A path the Access Guard would not let an agent READ
(deny rule, mask rule, confirm-on-read) is never blobbed; its row still
carries the hashes, with ``blob: "skipped:<reason>"``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import zlib
from pathlib import Path

from services import access_guard
from services.atomic_json import write_bytes_atomic
from services.retention import mb_to_bytes

DEFAULT_CAP_MB = 256
DEFAULT_MAX_FILE_MB = 5
_SWEEP_EVERY_PUTS = 64
_SWEEP_EVERY_S = 3600.0
_EVICT_TO = 0.9

_sweep_lock = threading.Lock()
_puts_since_sweep: dict[str, int] = {}
_last_sweep: dict[str, float] = {}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_dir(project_path) -> Path:
    return Path(project_path) / ".c3" / "edit_blobs"


def _blob_path(project_path, sha: str) -> Path:
    return store_dir(project_path) / sha[:2] / sha


def _edit_config(project_path) -> dict:
    try:
        data = json.loads((Path(project_path) / ".c3" / "config.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    section = data.get("edit") if isinstance(data, dict) else None
    return section if isinstance(section, dict) else {}


def _limits(project_path) -> tuple[int, int]:
    """(store cap, per-file cap) in bytes."""
    cfg = _edit_config(project_path)
    return (mb_to_bytes(cfg.get("blob_cap_mb", DEFAULT_CAP_MB)),
            mb_to_bytes(cfg.get("blob_max_file_mb", DEFAULT_MAX_FILE_MB)))


def put(project_path, data: bytes) -> str:
    """Store ``data`` once and return its sha256. Raises OSError on I/O failure."""
    sha = sha256(data)
    path = _blob_path(project_path, sha)
    if path.exists():
        os.utime(path)
    else:
        write_bytes_atomic(path, zlib.compress(data))
    return sha


def get(project_path, sha: str) -> bytes | None:
    """The stored bytes for ``sha``, or None if absent, evicted or corrupt."""
    if not sha:
        return None
    try:
        data = zlib.decompress(_blob_path(project_path, sha).read_bytes())
    except (OSError, zlib.error):
        return None
    return data if sha256(data) == sha else None


def sweep(project_path) -> int:
    """Evict oldest blobs until the store is within 90% of its cap.

    Returns how many blobs were removed. Never raises.
    """
    cap, _ = _limits(project_path)
    root = store_dir(project_path)
    entries = []
    total = 0
    try:
        for sub in root.iterdir():
            if not sub.is_dir():
                continue
            for f in sub.iterdir():
                try:
                    st = f.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, f))
                total += st.st_size
    except OSError:
        return 0
    if total <= cap:
        return 0
    entries.sort(key=lambda e: e[0])
    target = int(cap * _EVICT_TO)
    removed = 0
    for _mtime, size, f in entries:
        if total <= target:
            break
        try:
            f.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
    return removed


def _maybe_sweep(project_path) -> None:
    key = str(project_path)
    now = time.monotonic()
    with _sweep_lock:
        n = _puts_since_sweep.get(key, 0) + 1
        last = _last_sweep.get(key)
        due = last is None or n >= _SWEEP_EVERY_PUTS or now - last >= _SWEEP_EVERY_S
        if not due:
            _puts_since_sweep[key] = n
            return
        _puts_since_sweep[key] = 0
        _last_sweep[key] = now
    sweep(project_path)


def _skip_reason(project_path, path, pre: bytes | None,
                 post: bytes | None) -> str:
    _, max_file = _limits(project_path)
    if max(len(pre or b""), len(post or b"")) > max_file:
        return "too-large"
    denial = access_guard.check(str(path), "read", str(project_path))
    if denial is not None:
        return "masked" if denial.kind == "mask" else "read-denied"
    return ""


def record(project_path, path, pre: bytes | None,
           post: bytes | None) -> dict:
    """Ledger ``detail`` fields for a write that took ``path`` from ``pre`` to
    ``post`` (None = file absent), storing both images when policy allows.

    Keys: ``pre_sha256``, ``post_sha256``, ``blob`` ("stored" or
    "skipped:<reason>"). Never raises.
    """
    detail = {"pre_sha256": sha256(pre) if pre is not None else None,
              "post_sha256": sha256(post) if post is not None else None}
    reason = _skip_reason(project_path, path, pre, post)
    if reason:
        detail["blob"] = f"skipped:{reason}"
        return detail
    try:
        for image in (pre, post):
            if image is not None:
                put(project_path, image)
    except OSError as exc:
        detail["blob"] = f"skipped:store-error ({type(exc).__name__})"
        return detail
    detail["blob"] = "stored"
    _maybe_sweep(project_path)
    return detail


def record_edit(project_path, path: Path, pre: bytes | None) -> dict:
    """``record`` for a write that just landed: reads the post-image from disk.

    A post-image that cannot be read leaves the row without hashes, so it is
    simply not revertible. Never raises.
    """
    try:
        post = Path(path).read_bytes()
    except OSError:
        return {"blob": "skipped:unreadable-after-write"}
    return record(project_path, path, pre, post)
