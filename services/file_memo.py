"""Memoize a value derived from one file, keyed by the file's stat signature.

The hub and Oracle poll every registered project's ``.c3`` files several
times a minute, and between two polls almost none of those files change. An
entry is reused only while the file's (inode, size, mtime_ns) still matches,
so a write shows up on the next call. A file modified within the last
``RACY_S`` seconds is recomputed on every call instead: two writes inside one
timestamp tick can leave the size and mtime equal.
"""

import os
import threading
import time

RACY_S = 2.0


class FileMemo:
    """``compute(path, *args)``, reused while ``path`` has not changed."""

    def __init__(self, compute, max_entries: int = 1024):
        self._compute = compute
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict = {}

    def get(self, path, *args):
        key = (os.fspath(path), *args)
        try:
            st = os.stat(path)
        except OSError:
            return self._compute(path, *args)  # missing: the compute's own answer
        sig = (st.st_ino, st.st_size, st.st_mtime_ns)
        with self._lock:
            hit = self._entries.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        # Stat before compute: a write landing in between gets a newer
        # signature than the one stored here, so it is never served stale.
        value = self._compute(path, *args)
        if time.time() - st.st_mtime >= RACY_S:
            with self._lock:
                if len(self._entries) >= self._max:
                    self._entries.clear()
                self._entries[key] = (sig, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
