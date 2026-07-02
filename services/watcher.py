"""
File Watcher Service

Watches project files for changes and tracks modifications:
- Daemon thread monitors file system events
- Filters by code extensions, skips node_modules/.git/etc.
- Accumulates changes for session logging
- Triggers index rebuild when enough changes accumulate
"""
import threading
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Extensions to watch
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.r', '.R',
    '.css', '.html', '.json', '.yaml', '.yml', '.md',
    '.sh', '.sql', '.go', '.rs', '.java', '.cpp', '.c', '.h',
}

# Directories to skip
SKIP_DIRS = {
    'node_modules', '.git', '__pycache__', '.c3', 'venv',
    'env', '.venv', 'dist', 'build', '.next', '.cache',
    'coverage', '.pytest_cache',
}


class _ChangeHandler(FileSystemEventHandler):
    """Collects file change events."""

    def __init__(self, excluder=None):
        super().__init__()
        self._lock = threading.Lock()
        self._changes = []
        self._excluder = excluder  # sub-project folders tracked by their own .c3

    def _should_track(self, path: str) -> bool:
        p = Path(path)
        if any(skip in p.parts for skip in SKIP_DIRS):
            return False
        if self._excluder is not None and self._excluder(path):
            return False
        return p.suffix.lower() in CODE_EXTENSIONS

    def _record(self, event_type: str, path: str):
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
        self._handler = _ChangeHandler(excluder)
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
        """Trigger index rebuild if enough changes have accumulated."""
        if self._handler.change_count >= threshold:
            changes = self.get_changes()
            result = indexer.build_index()
            result["triggered_by_changes"] = len(changes)
            return result
        return None
