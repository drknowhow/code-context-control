"""Delegation hints: say so at the moment the session's own work looks delegable.

The instructions already describe c3_delegate, and in the audit that started
the delegate work 19 of 44,038 C3 calls were delegations. A rule read at
session start does not fire mid-task. A hint does: one line appended to the
c3_* response that just did the delegable thing, naming the call to make
instead, then silent for ``delegate.hint_cooldown_minutes`` per kind.

Two kinds, both about the lead spending its own tokens on work a cheaper model
does as well (docs/delegate-auto.md has the numbers):

- ``write`` — the session has written a lot of edit text itself: at least
  ``WRITE_MIN_EDITS`` c3_edit calls over ``WRITE_MIN_FILES`` files and
  ``WRITE_MIN_CHARS`` characters of new text in ``WRITE_WINDOW_S``, or one
  edit over ``BIG_EDIT_CHARS``. Those characters are the lead's OUTPUT
  tokens, the most expensive kind. Suggests ``write_paths``.
- ``explore`` — at least ``EXPLORE_MIN_READS`` source reads and searches over
  ``EXPLORE_MIN_FILES`` files in ``EXPLORE_WINDOW_S`` with no edit. Suggests
  ``scout=True`` or an Explore subagent.

No hint while the session is already delegating that kind. Each hint shown
is a ``delegate_hint`` telemetry row; a matching c3_delegate call within
``FOLLOW_WINDOW_S`` of it is a ``delegate_hint_followed`` row, so whether
hints change behaviour is a number, not a hope.
"""

from __future__ import annotations

import threading
import time
from collections import deque

WRITE_WINDOW_S = 15 * 60
WRITE_MIN_EDITS = 5
WRITE_MIN_FILES = 2
WRITE_MIN_CHARS = 4000
BIG_EDIT_CHARS = 6000
EXPLORE_WINDOW_S = 10 * 60
EXPLORE_MIN_READS = 6
EXPLORE_MIN_FILES = 4
FOLLOW_WINDOW_S = 30 * 60
_KEEP_S = max(WRITE_WINDOW_S, EXPLORE_WINDOW_S, FOLLOW_WINDOW_S)

TAG = "[c3:delegate-hint]"


class HintTracker:
    """Per-session event window. Thread-safe; the clock is injectable for tests."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._events: deque = deque()  # (ts, kind, file, chars)
        self._shown: dict[str, float] = {}
        self._followed: dict[str, float] = {}  # kind -> the hint time already counted
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] > _KEEP_S:
            self._events.popleft()

    def _window(self, now: float, seconds: float, kinds: tuple) -> list:
        return [e for e in self._events if now - e[0] <= seconds and e[1] in kinds]

    def _cooled(self, kind: str, now: float, cooldown_s: float) -> bool:
        last = self._shown.get(kind)
        return last is None or now - last >= cooldown_s

    def note_edit(self, file_path: str, chars: int, cooldown_s: float) -> str:
        """Record one successful c3_edit; returns a hint line or ''."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._events.append((now, "edit", str(file_path or ""), max(0, int(chars))))
            if not self._cooled("write", now, cooldown_s):
                return ""
            if self._window(now, WRITE_WINDOW_S, ("delegate_write",)):
                return ""
            edits = self._window(now, WRITE_WINDOW_S, ("edit",))
            total = sum(e[3] for e in edits)
            files = {e[2] for e in edits if e[2]}
            big = chars >= BIG_EDIT_CHARS
            if not big and (len(edits) < WRITE_MIN_EDITS or len(files) < WRITE_MIN_FILES
                            or total < WRITE_MIN_CHARS):
                return ""
            self._shown["write"] = now
            span = max(1, round((now - edits[0][0]) / 60)) if edits else 1
            if big and len(edits) < WRITE_MIN_EDITS:
                what = f"one edit of ~{chars // 4} tokens"
            else:
                what = f"~{total // 4} tokens of edits across {len(files)} files in {span} min"
            return (f"{TAG} {what}, written at your own model's output rate. For the next change "
                    "you have already decided, c3_delegate(task=<what to change, where, how it should "
                    "behave>, write_paths='a.py,b.py') has Sonnet make it and returns the diff to "
                    "review (~$0.03 a change).")

    def note_read(self, file_path: str, cooldown_s: float) -> str:
        """Record one source read or search; returns a hint line or ''."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._events.append((now, "read", str(file_path or ""), 0))
            if not self._cooled("explore", now, cooldown_s):
                return ""
            if self._window(now, EXPLORE_WINDOW_S, ("edit", "delegate_scout")):
                return ""
            reads = self._window(now, EXPLORE_WINDOW_S, ("read",))
            files = {e[2] for e in reads if e[2]}
            if len(reads) < EXPLORE_MIN_READS or len(files) < EXPLORE_MIN_FILES:
                return ""
            self._shown["explore"] = now
            return (f"{TAG} {len(reads)} reads and searches across {len(files)} files with no edit "
                    "yet. For a lookup, c3_delegate(task=<the question>, scout=True) has Sonnet read "
                    "the repo and answer with file:line, or use an Explore subagent; either keeps "
                    "the reading out of your context.")

    def note_delegate(self, *, write: bool, scout: bool) -> tuple[str, float] | None:
        """Record a c3_delegate call; ``(kind, seconds_after_hint)`` when it follows a hint."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if write:
                self._events.append((now, "delegate_write", "", 0))
            if scout:
                self._events.append((now, "delegate_scout", "", 0))
            kind = "write" if write else ("explore" if scout else "")
            shown = self._shown.get(kind) if kind else None
            if (shown is not None and now - shown <= FOLLOW_WINDOW_S
                    and self._followed.get(kind) != shown):
                self._followed[kind] = shown  # one follow per hint
                return kind, round(now - shown, 1)
            return None


def edit_chars(new_string, edits) -> int:
    """Characters of new text in one c3_edit call, single or batch."""
    total = len(new_string or "")
    if edits:
        try:
            import json
            items = json.loads(edits) if isinstance(edits, str) else edits
            total += sum(len(str(e.get("new_string") or "")) for e in items if isinstance(e, dict))
        except (ValueError, TypeError):
            pass
    return total


def tracker_for(svc) -> HintTracker:
    tracker = getattr(svc, "_delegate_hints", None)
    if tracker is None:
        tracker = HintTracker()
        try:
            svc._delegate_hints = tracker
        except Exception:
            pass
    return tracker


def _cfg(svc) -> tuple[bool, float]:
    dcfg = getattr(svc, "delegate_config", None) or {}
    enabled = bool(dcfg.get("hints", True)) and bool(dcfg.get("enabled", True))
    try:
        cooldown = max(1.0, float(dcfg.get("hint_cooldown_minutes", 45))) * 60
    except (TypeError, ValueError):
        cooldown = 45 * 60.0
    return enabled, cooldown


def _record(svc, tool: str, detail: dict) -> None:
    try:
        from services.telemetry import append_telemetry_record
        append_telemetry_record(svc.project_path, {"tool": tool, "detail": detail})
    except Exception:
        pass


def after_edit(svc, file_path: str, chars: int) -> str:
    enabled, cooldown = _cfg(svc)
    if not enabled:
        return ""
    hint = tracker_for(svc).note_edit(file_path, chars, cooldown)
    if hint:
        _record(svc, "delegate_hint", {"kind": "write"})
    return hint


def after_read(svc, file_path: str) -> str:
    enabled, cooldown = _cfg(svc)
    if not enabled:
        return ""
    hint = tracker_for(svc).note_read(file_path, cooldown)
    if hint:
        _record(svc, "delegate_hint", {"kind": "explore"})
    return hint


def after_delegate(svc, *, write: bool, scout: bool) -> None:
    followed = tracker_for(svc).note_delegate(write=write, scout=scout)
    if followed:
        _record(svc, "delegate_hint_followed", {"kind": followed[0], "after_s": followed[1]})
