"""Rate limiting + audit logging for the Oracle Discovery API (#33).

The Discovery surface (``/api/discovery/*`` and the MCP transport on :3332) is
Bearer-gated but was unthrottled: a leaked token allowed unbounded tool calls,
and ``c3_search_cross`` fans out a full runtime per project, so the cost of one
call is not bounded by the request size.

Two independent pieces, deliberately kept in one module because they share the
caller-identity function:

* :class:`RateLimiter` — in-memory token bucket per caller. Refills
  continuously, so a burst is allowed up to the bucket size and the sustained
  rate converges on the configured per-minute budget.
* :func:`record` — one JSONL line per tool call under ``~/.c3/oracle/``.

PRIVACY: the audit log stores a *hash* of the arguments, never the arguments.
Discovery args routinely carry file paths, query strings, and project names;
an audit trail that leaks them is a worse liability than the missing throttle.
The caller is likewise a token fingerprint, never the token — enough to tell
two clients apart and to spot a leaked key's traffic, useless for replay.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from oracle.config import ORACLE_DIR

AUDIT_FILENAME = "discovery_audit.jsonl"

# Rotate at 8 MiB, keeping one previous generation. Bounded by construction:
# an unbounded audit log is its own denial-of-service.
_MAX_BYTES = 8 * 1024 * 1024

_ANON = "anon"


def caller_id(token: str | None, remote_addr: str | None) -> str:
    """Stable, non-reversible label for one client.

    Prefers the token fingerprint so a single client keeps one identity across
    addresses; falls back to the peer address when auth is disabled.
    """
    if token:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return f"key:{digest[:12]}"
    if remote_addr:
        return f"addr:{remote_addr}"
    return _ANON


def args_fingerprint(args: dict | None) -> str:
    """Hash of the argument object — correlates repeat calls without storing them."""
    try:
        canonical = json.dumps(args or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class RateLimiter:
    """Token bucket keyed by caller. Thread-safe; process-local by design.

    Process-local is the honest scope: the Oracle server is a single process,
    and a limiter that pretended to be global would be wrong the moment a
    second instance started.
    """

    def __init__(self, per_minute: int = 60, burst: int = 0,
                 clock=time.monotonic):
        self.per_minute = max(0, int(per_minute))
        # Default burst = a quarter-minute of budget, min 5, so a normal
        # client's opening flurry is not punished.
        self.burst = int(burst) if burst else max(5, self.per_minute // 4)
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.per_minute > 0

    def check(self, key: str) -> tuple[bool, float]:
        """Consume one token. Returns ``(allowed, retry_after_seconds)``."""
        if not self.enabled:
            return True, 0.0
        rate = self.per_minute / 60.0
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True, 0.0
            self._buckets[key] = (tokens, now)
            return False, max(0.0, (1.0 - tokens) / rate)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


def audit_path(base_dir: Path | None = None) -> Path:
    return (base_dir or ORACLE_DIR) / AUDIT_FILENAME


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size >= _MAX_BYTES:
            path.replace(path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def record(tool: str, *, caller: str, args: dict | None = None,
           duration_ms: float = 0.0, status: str = "ok",
           base_dir: Path | None = None) -> None:
    """Append one audit line. Never raises — auditing must not break a call."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "tool": tool,
        "caller": caller,
        "args_hash": args_fingerprint(args),
        "duration_ms": round(float(duration_ms), 1),
        "status": status,
    }
    path = audit_path(base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed(path)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_recent(limit: int = 100, base_dir: Path | None = None) -> list[dict]:
    """Most-recent-first audit entries, for the Activity tab."""
    path = audit_path(base_dir)
    if not path.exists():
        return []
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= limit:
            break
    return out
