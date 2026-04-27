"""Opt-in crash + error reporting via Sentry.

Off by default. Enabled only when ALL of the following are true:

  1. The ``sentry-sdk`` package is installed (pulled in by the optional
     ``code-context-control[telemetry]`` extra).
  2. ``SENTRY_DSN`` is set in the environment.
  3. The user has opted in by setting ``C3_TELEMETRY_OPT_IN=1`` in the
     environment OR by creating ``~/.c3/telemetry.json`` with
     ``{"opt_in": true}``.

When enabled, only unhandled exceptions and explicit ``capture_error``
calls are transmitted. We strip query strings, file paths, and any
``args``/``kwargs`` payloads via a ``before_send`` hook to avoid
leaking source code or prompts. No performance / tracing data is sent.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_INITIALIZED = False
_TELEMETRY_FILE = Path.home() / ".c3" / "telemetry.json"


def _user_opted_in() -> bool:
    if os.environ.get("C3_TELEMETRY_OPT_IN") == "1":
        return True
    try:
        if _TELEMETRY_FILE.exists():
            data = json.loads(_TELEMETRY_FILE.read_text(encoding="utf-8"))
            return bool(data.get("opt_in"))
    except Exception:
        return False
    return False


def _scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Strip potentially-sensitive payloads from Sentry events."""
    try:
        # Remove request body / query string if present
        request = event.get("request") or {}
        request.pop("data", None)
        request.pop("query_string", None)
        request.pop("cookies", None)
        request.pop("headers", None)
        if request:
            event["request"] = request

        # Remove local variables from stack frames (often contain file content,
        # prompts, or model output).
        for thread in event.get("threads", {}).get("values", []) or []:
            for frame in thread.get("stacktrace", {}).get("frames", []) or []:
                frame.pop("vars", None)
        for exc in event.get("exception", {}).get("values", []) or []:
            for frame in exc.get("stacktrace", {}).get("frames", []) or []:
                frame.pop("vars", None)

        # Strip extra/contexts payloads
        event.pop("extra", None)
        event["contexts"] = {
            k: v for k, v in (event.get("contexts") or {}).items()
            if k in ("runtime", "os", "device")
        }
    except Exception:
        # Never let scrubbing crash the reporter
        pass
    return event


def init(component: str = "c3", version: str = "unknown") -> bool:
    """Initialize Sentry if all opt-in conditions are met. Idempotent.

    Returns True if Sentry was initialized in this call (or already was),
    False if any precondition was missing.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return True

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    if not _user_opted_in():
        return False

    try:
        import sentry_sdk  # type: ignore[import-not-found]
    except ImportError:
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            release=f"{component}@{version}",
            environment=os.environ.get("C3_ENV", "production"),
            traces_sample_rate=0.0,    # no perf tracing
            profiles_sample_rate=0.0,  # no profiling
            send_default_pii=False,
            attach_stacktrace=True,
            before_send=_scrub_event,
        )
        sentry_sdk.set_tag("c3.component", component)
        _INITIALIZED = True
        return True
    except Exception:
        return False


def capture_error(exc: BaseException, *, component: str | None = None) -> None:
    """Best-effort error capture; no-op if Sentry is not initialized."""
    if not _INITIALIZED:
        return
    try:
        import sentry_sdk  # type: ignore[import-not-found]
        with sentry_sdk.push_scope() as scope:
            if component:
                scope.set_tag("c3.component", component)
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass
