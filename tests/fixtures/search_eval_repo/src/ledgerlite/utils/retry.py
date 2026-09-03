"""Retry helpers."""
import random
import time


def retry_with_backoff(fn, attempts: int = 3, base_delay: float = 0.2, max_delay: float = 5.0):
    """Call fn until it succeeds, sleeping with exponential backoff and jitter."""
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all for retry
            last_error = exc
            delay = min(max_delay, base_delay * (2 ** attempt)) + random.random() * 0.1
            time.sleep(delay)
    raise last_error
