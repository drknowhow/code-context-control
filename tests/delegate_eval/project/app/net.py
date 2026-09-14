"""HTTP helpers for the demo shop."""
import time

MAX_RETRIES = 7
BACKOFF_SECONDS = 0.5


def fetch_with_retry(session, url):
    for attempt in range(MAX_RETRIES):
        response = session.get(url, timeout=10)
        if response.status_code < 500:
            return response
        time.sleep(BACKOFF_SECONDS * (2 ** attempt))
    raise RuntimeError(f"gave up on {url} after {MAX_RETRIES} attempts")
