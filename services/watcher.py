"""
File Watcher Service

Watches project files for changes and tracks modifications:
- Daemon thread monitors file system events
- Filters by code extensions, skips node_modules/.git/etc.
- Accumulates changes for session logging
- Triggers index rebuild when enough changes accumulate
"""
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from services.scanner import SKIP_DIRS, is_nested_checkout, make_dir_pruner

# Extensions to watch
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.r', '.R',
    '.css', '.html', '.json', '.yaml', '.yml', '.md',
    '.sh', '.sql', '.go', '.rs', '.java', '.cpp', '.c', '.h',
}

# SKIP_DIRS is re-exported from services.scanner: the watcher and the index
# walker must agree on what is not part of the project (see _ChangeHandler).
__all__ = ['CODE_EXTENSIONS', 'SKIP_DIRS', 'CodeWatcher']


class _ChangeHandler(FileSystemEventHandler):
    """Collects file change events.

    The filter is the index scanner's, not a private one. A change only
    counts when the file could be IN the index: the same directory pruning
    as ``scanner.iter_files`` (SKIP_DIRS, the root .gitignore's directory
    entries, nested checkouts such as linked worktrees), then the
    sub-project excluder, then the extension allowlist.

    Before 2.129.1 the watcher kept its own shorter skip list and ignored
    .gitignore and worktrees, so state files a daemon rewrites every few
    seconds under a gitignored ``logs/`` counted as source changes. On one
    project that tripped IndexStalenessAgent's rebuild threshold (15) every
    single minute — the "Index auto-rebuilt" notification reached a count of
    75,968 — and each refresh burst left the MCP server's process heap with
    more pinned 16 MB segments it never gave back: 28 GB of commit charge
    per server with under 1 GB touched, three servers on the project, and
    the C3 Hub dying of MemoryError when the box ran out of commit.
    """

    def __init__(self, excluder=None, root=None):
        super().__init__()
        self._lock = threading.Lock()
        self._changes = []
        self._excluder = excluder  # sub-project folders tracked by their own .c3
        self._root = Path(root) if root else None
        self._pruned = make_dir_pruner(root) if root else (lambda d: d in SKIP_DIRS)
        # dir path -> is another checkout; one stat per directory, not per event
        self._checkout_cache: dict = {}

    def _reload_pruner(self):
        """Re-read the root .gitignore (called when it changes)."""
        if self._root is not None:
            self._pruned = make_dir_pruner(self._root)

    def _rel_parts(self, p: Path) -> tuple:
        if self._root is None:
            return p.parts
        try:
            return p.relative_to(self._root).parts
        except ValueError:
            try:
                return p.resolve().relative_to(self._root).parts
            except (ValueError, OSError):
                return p.parts

    def _under_nested_checkout(self, rel_dirs: tuple) -> bool:
        if self._root is None:
            return False
        cur = str(self._root)
        for d in rel_dirs:
            cur = os.path.join(cur, d)
            hit = self._checkout_cache.get(cur)
            if hit is None:
                if len(self._checkout_cache) > 4096:
                    self._checkout_cache.clear()
                hit = self._checkout_cache[cur] = is_nested_checkout(cur)
            if hit:
                return True
        return False

    def _should_track(self, path: str) -> bool:
        p = Path(path)
        if p.suffix.lower() not in CODE_EXTENSIONS:
            return False
        parts = self._rel_parts(p)
        rel_dirs = parts[:-1]
        if any(self._pruned(d) for d in rel_dirs):
            return False
        if self._under_nested_checkout(rel_dirs):
            return False
        if self._excluder is not None and self._excluder(path):
            return False
        return True

    def _record(self, event_type: str, path: str):
        if self._root is not None and Path(path).name == '.gitignore':
            try:
                if Path(path).parent.samefile(self._root):
                    self._reload_pruner()
            except OSError:
                pass
        if not self._should_track(path):
            return
        with self._lock:
            self._changes.append({
                "type": event_type,
                "path": path,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    def on_modified(self, event):
        if not event.is_directory:
            self._record("modified", event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._record("created", event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._record("deleted", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._record("moved", event.src_path)

    def get_and_clear(self) -> list:
        with self._lock:
            changes = list(self._changes)
            self._changes.clear()
            return changes

    @property
    def change_count(self) -> int:
        with self._lock:
            return len(self._changes)


class CodeWatcher:
    """Watches project files for changes on a daemon thread."""

    def __init__(self, project_path: str):
        self.project_path = str(Path(project_path).resolve())
        try:
            from services.subprojects import make_excluder
            excluder = make_excluder(self.project_path)
        except Exception:
            excluder = None
        self._handler = _ChangeHandler(excluder, root=self.project_path)
        self._observer = Observer()
        self._observer.daemon = True
        self._file_memory = None
        self._compressor = None
        self._worker_thread = None
        self._stop_event = threading.Event()
        self._update_queue = set()
        self._queue_lock = threading.Lock()

    def set_backends(self, file_memory, compressor, validation_cache=None):
        """Set backends for proactive background updates."""
        self._file_memory = file_memory
        self._compressor = compressor
        self._validation_cache = validation_cache

    def _background_worker(self):
        import time
        from pathlib import Path
        # Debounce tracking: {abs_path: last_enqueue_time}
        pending_validation: dict[str, float] = {}
        while not self._stop_event.is_set():
            time.sleep(1.0)
            now = time.time()
            paths_to_update = []
            with self._queue_lock:
                if self._update_queue:
                    paths_to_update = list(self._update_queue)
                    self._update_queue.clear()

            for path in paths_to_update:
                if self._stop_event.is_set():
                    break
                try:
                    rel_path = str(Path(path).resolve().relative_to(self.project_path))
                    # Pre-emptively update structural map
                    if self._file_memory:
                        self._file_memory.update(rel_path)
                    # Pre-emptively compress
                    if self._compressor:
                        self._compressor.compress_file(str(Path(path)), "smart")
                except Exception:
                    pass
                # Track for debounced validation
                if self._validation_cache:
                    pending_validation[path] = now

            # Run debounced validation for files that haven't changed recently
            if self._validation_cache and pending_validation:
                debounce = self._validation_cache.debounce_seconds
                ready = [p for p, t in pending_validation.items() if now - t >= debounce]
                for path in ready:
                    if self._stop_event.is_set():
                        break
                    pending_validation.pop(path, None)
                    try:
                        rel_path = str(Path(path).resolve().relative_to(self.project_path))
                        self._validation_cache.validate_file(rel_path)
                    except Exception:
                        pass

    def start(self):
        """Start watching (non-blocking, daemon thread)."""
        self._observer.schedule(self._handler, self.project_path, recursive=True)
        self._observer.start()

        # Start background worker for proactive mapping
        self._worker_thread = threading.Thread(target=self._background_worker, daemon=True)
        self._worker_thread.start()

    def stop(self):
        """Stop watching."""
        self._stop_event.set()
        self._observer.stop()
        self._observer.join(timeout=2)
        if self._worker_thread:
            self._worker_thread.join(timeout=2)

    def get_changes(self) -> list:
        """Return accumulated changes and clear the buffer."""
        changes = self._handler.get_and_clear()

        # Enqueue modified files for background update
        with self._queue_lock:
            for c in changes:
                if c["type"] in ("modified", "created"):
                    self._update_queue.add(c["path"])
                elif c["type"] == "deleted" and self._validation_cache:
                    try:
                        rel = str(Path(c["path"]).resolve().relative_to(self.project_path))
                        self._validation_cache.evict(rel)
                    except Exception:
                        pass

        return changes

    def rebuild_if_needed(self, indexer, threshold: int = 10) -> dict | None:
        """Refresh the index once enough changes have accumulated.

        Since 2.107.0 this is incremental: the changed paths go to
        ``CodeIndex.refresh``, which re-chunks only files whose content hash
        moved and drops deleted ones. Before, every tenth change rebuilt the
        whole index (~12 s on a 500-file project). An indexer without
        ``refresh`` (a stub in tests) still gets ``build_index``.
        """
        if self._handler.change_count >= threshold:
            changes = self.get_changes()
            paths = [c.get("path") for c in changes if isinstance(c, dict) and c.get("path")]
            refresh = getattr(indexer, "refresh", None)
            if callable(refresh) and paths:
                result = refresh(paths=paths)
            else:
                result = indexer.build_index()
            result["triggered_by_changes"] = len(changes)
            return result
        return None
