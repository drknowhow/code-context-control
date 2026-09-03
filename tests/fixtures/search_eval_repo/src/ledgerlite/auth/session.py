"""Server-side session store."""
import time


class SessionStore:
    """In-memory sessions with inactivity expiry."""

    def __init__(self, ttl_seconds: int = 1800):
        self.ttl_seconds = ttl_seconds
        self._sessions: dict[str, dict] = {}

    def create(self, session_id: str, user_id: str) -> dict:
        record = {"user_id": user_id, "last_seen": time.time()}
        self._sessions[session_id] = record
        return record

    def touch(self, session_id: str) -> None:
        if session_id in self._sessions:
            self._sessions[session_id]["last_seen"] = time.time()

    def expire(self, now: float | None = None) -> int:
        """Drop sessions idle longer than ttl_seconds. Returns the count removed."""
        now = now or time.time()
        stale = [sid for sid, rec in self._sessions.items() if now - rec["last_seen"] > self.ttl_seconds]
        for sid in stale:
            del self._sessions[sid]
        return len(stale)
