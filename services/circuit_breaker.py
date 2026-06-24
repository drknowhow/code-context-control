"""Lightweight thread-safe circuit breaker for flapping subsystems.

Borrowed in spirit from Headroom's TransformPipeline breaker: after N
consecutive failures a subsystem is treated as unhealthy and calls are
short-circuited for a cooldown window instead of re-running (and re-failing)
the expensive operation every time. A single success closes the breaker.

First consumer: c3_delegate, to stop re-spawning a broken-but-installed CLI
backend (a 90-120s subprocess timeout) on every call. Deliberately
dependency-free so any call-time subsystem (e.g. the Ollama embed/generate
path) can reuse it later.
"""
from __future__ import annotations

import threading
import time


class CircuitBreaker:
    """Consecutive-failure breaker: closed -> open (after N fails) -> half-open (after cooldown)."""

    def __init__(
        self,
        name: str = "",
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._lock = threading.Lock()
        self._failures = 0
        self._open = False
        self._opened_at = 0.0

    def allow(self) -> bool:
        """Return True if a call may proceed.

        An open breaker permits a single probe once the cooldown has elapsed
        (half-open); the next ``record_success``/``record_failure`` resolves it.
        """
        with self._lock:
            if not self._open:
                return True
            return (time.monotonic() - self._opened_at) >= self.cooldown_seconds

    def record_success(self) -> None:
        """Reset the breaker after a healthy call."""
        with self._lock:
            self._failures = 0
            self._open = False
            self._opened_at = 0.0

    def record_failure(self) -> bool:
        """Count a failed call.

        Returns True iff this failure *just* tripped the breaker open (callers
        can use that edge to emit a one-shot notification). A failed half-open
        probe restarts the cooldown but does not re-trip.
        """
        with self._lock:
            self._failures += 1
            if not self._open and self._failures >= self.failure_threshold:
                self._open = True
                self._opened_at = time.monotonic()
                return True
            if self._open:
                self._opened_at = time.monotonic()
            return False

    def cooldown_remaining(self) -> int:
        """Whole seconds left before the next probe is allowed (0 if closed/elapsed)."""
        with self._lock:
            if not self._open:
                return 0
            remaining = self.cooldown_seconds - (time.monotonic() - self._opened_at)
            return max(0, int(round(remaining)))

    @property
    def is_open(self) -> bool:
        """True while calls are actively being short-circuited (open and within cooldown)."""
        with self._lock:
            if not self._open:
                return False
            return (time.monotonic() - self._opened_at) < self.cooldown_seconds
