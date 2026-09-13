"""Read Grok Build session usage.

A Grok hook payload's transcript_path points at
``~/.grok/sessions/<url-encoded cwd>/<session id>/updates.jsonl``. The same
directory holds ``usage.json``, whose ``session`` block carries Grok's own
cumulative token and cost totals (tests/fixtures/grok/session_usage_1.0.30.json).
Grok's ``inputTokens`` INCLUDES cache reads; C3's cross-provider rows count
them separately, as Claude's wire format does.
"""
import json
import os
from pathlib import Path

# Grok reports cost in ticks: costUsdTicks 527904400 == total_cost_usd 0.05279044.
_TICKS_PER_USD = 10_000_000_000


def _canonical(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def session_dir(transcript_path, sessions_root: Path | None = None) -> Path | None:
    """The session directory for a transcript path, only if it lies under Grok's sessions root."""
    if not transcript_path:
        return None
    if sessions_root is None:
        from services.grok_integration import grok_home
        sessions_root = grok_home() / "sessions"
    directory = Path(str(transcript_path)).parent
    root = _canonical(sessions_root)
    candidate = _canonical(directory)
    if not candidate.startswith(root.rstrip("\\/") + os.sep):
        return None
    return directory


def _as_int(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def read_usage(transcript_path, session_id: str, sessions_root: Path | None = None) -> dict | None:
    """Cumulative usage for one Grok session, or None when it cannot be verified."""
    directory = session_dir(transcript_path, sessions_root)
    if directory is None or not session_id:
        return None
    try:
        data = json.loads((directory / "usage.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or str(data.get("sessionId") or "") != str(session_id):
        return None
    session = data.get("session")
    if not isinstance(session, dict):
        return None
    input_total = _as_int(session.get("inputTokens"))
    cache_read = _as_int(session.get("cachedReadTokens"))
    ticks = session.get("costUsdTicks")
    return {
        "input_tokens": max(0, input_total - cache_read),
        "input_tokens_including_cache": input_total,
        "output_tokens": _as_int(session.get("outputTokens")),
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": _as_int(session.get("cacheCreationTokens")),
        "reasoning_tokens": _as_int(session.get("reasoningTokens")),
        "model_calls": _as_int(session.get("modelCalls")),
        "cost_usd": round(_as_int(ticks) / _TICKS_PER_USD, 8) if ticks is not None else None,
        "model": str(session.get("primaryModelId") or ""),
    }
