"""
Single-line TTY progress for long CLI phases (index/embedding builds).

Quiet when stdout is piped - CI logs and command substitutions see only
the final summary lines, never carriage-return spam.
"""
import sys
import time


class ProgressLine:
    """Rewrites one status line in place; time-throttled; TTY-only."""

    def __init__(self, stream=None, min_interval: float = 0.1):
        self.stream = stream if stream is not None else sys.stdout
        self.min_interval = min_interval
        self._last = 0.0
        self._width = 0
        try:
            self._tty = bool(self.stream.isatty())
        except Exception:
            self._tty = False

    def update(self, text: str) -> None:
        if not self._tty:
            return
        now = time.monotonic()
        if now - self._last < self.min_interval:
            return
        self._last = now
        pad = ' ' * max(0, self._width - len(text))
        try:
            self.stream.write('\r' + text + pad)
            self.stream.flush()
        except Exception:
            self._tty = False
            return
        self._width = len(text)

    def done(self) -> None:
        """Clear the in-place line so the following prints start clean."""
        if not self._tty or not self._width:
            return
        try:
            self.stream.write('\r' + ' ' * self._width + '\r')
            self.stream.flush()
        except Exception:
            pass
        self._width = 0
