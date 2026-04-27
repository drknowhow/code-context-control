"""Offline validation pipeline — background syntax checking with result caching."""

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class _CacheEntry:
    rel_path: str
    mtime: float
    size: int
    result: dict
    validated_at: float = field(default_factory=time.time)


# Extensions validated with pure-Python checkers (fast, no subprocess).
_FAST_EXTENSIONS = {
    ".py", ".json", ".yaml", ".yml", ".xml", ".svg",
    ".toml", ".html", ".htm", ".css",
}

# Extensions requiring subprocess checkers (slower, opt-in).
_SUBPROCESS_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".r", ".php", ".rb", ".pl", ".pm", ".lua", ".sh", ".bash",
}


class ValidationCache:
    """In-memory cache of syntax validation results keyed by (path, mtime, size)."""

    def __init__(self, project_path: str, config: Optional[dict] = None):
        self._project_path = str(Path(project_path).resolve())
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()
        cfg = config or {}
        self._enabled = cfg.get("enabled", True)
        self._bg_subprocess = cfg.get("background_subprocess_checkers", False)
        self._debounce_seconds = max(0.5, float(cfg.get("debounce_seconds", 2.0)))
        # Limit concurrent subprocess validations to 1.
        self._subprocess_sem = threading.Semaphore(1)

    # ── Public API ──────────────────────────────────────────────────

    def get(self, rel_path: str) -> Optional[dict]:
        """Return cached result if the file hasn't changed since last validation."""
        if not self._enabled:
            return None
        full = os.path.join(self._project_path, rel_path)
        try:
            st = os.stat(full)
        except OSError:
            self.evict(rel_path)
            return None
        with self._lock:
            entry = self._cache.get(rel_path)
            if entry and entry.mtime == st.st_mtime and entry.size == st.st_size:
                return entry.result
        return None

    def put(self, rel_path: str, result: dict, mtime: float, size: int) -> None:
        """Store a validation result."""
        with self._lock:
            self._cache[rel_path] = _CacheEntry(
                rel_path=rel_path,
                mtime=mtime,
                size=size,
                result=result,
            )

    def evict(self, rel_path: str) -> None:
        """Remove a cached entry (e.g. file was deleted)."""
        with self._lock:
            self._cache.pop(rel_path, None)

    def get_errors(self) -> List[dict]:
        """Return all cached entries that have syntax errors."""
        with self._lock:
            return [
                {"path": e.rel_path, "detail": e.result.get("detail", ""), "checker": e.result.get("checker", "")}
                for e in self._cache.values()
                if e.result.get("status") == "syntax_error"
            ]

    def summary(self) -> dict:
        """Return cache statistics."""
        with self._lock:
            entries = list(self._cache.values())
        total = len(entries)
        errors = sum(1 for e in entries if e.result.get("status") == "syntax_error")
        clean = sum(1 for e in entries if e.result.get("status") == "clean")
        return {"cached_files": total, "errors": errors, "clean": clean}

    # ── Background validation entry point ───────────────────────────

    def validate_file(self, rel_path: str) -> Optional[dict]:
        """Validate a file: return cached if fresh, else run checker and cache.

        Called by the watcher background worker. Returns the result dict
        or None if the extension is not eligible for background validation.
        """
        if not self._enabled:
            return None

        ext = Path(rel_path).suffix.lower()
        is_fast = ext in _FAST_EXTENSIONS
        is_subprocess = ext in _SUBPROCESS_EXTENSIONS

        if not is_fast and not is_subprocess:
            return None
        if is_subprocess and not self._bg_subprocess:
            return None

        # Check cache freshness first.
        cached = self.get(rel_path)
        if cached is not None:
            return cached

        full = os.path.join(self._project_path, rel_path)
        try:
            st = os.stat(full)
        except OSError:
            self.evict(rel_path)
            return None

        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return None

        from services.parser import check_syntax_native_with_timeout

        if is_subprocess:
            # Guard subprocess checkers with semaphore.
            acquired = self._subprocess_sem.acquire(timeout=0.1)
            if not acquired:
                return None  # Another subprocess validation is running; skip.
            try:
                result = check_syntax_native_with_timeout(content, ext, timeout_seconds=35)
            finally:
                self._subprocess_sem.release()
        else:
            result = check_syntax_native_with_timeout(content, ext, timeout_seconds=10)

        self.put(rel_path, result, st.st_mtime, st.st_size)
        return result

    @property
    def debounce_seconds(self) -> float:
        return self._debounce_seconds
