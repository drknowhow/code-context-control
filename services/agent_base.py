"""BackgroundAgent — Base class for autonomous daemon threads.

Agents run periodic checks and surface findings via NotificationStore.
They cannot invoke Claude, but prepend warnings to the next tool response.

Each agent supports optional AI enhancement via OllamaClient. When AI is
unavailable or disabled, agents fall back to heuristic logic.
"""
import threading
import time
from abc import ABC, abstractmethod


class BackgroundAgent(ABC):
    """Base class for background analysis agents."""

    _IDLE_BACKOFF_MAX = 4

    def __init__(self, name: str, interval: int, notifications, enabled: bool = True,
                 use_ai: bool = False, ollama=None, ai_model: str = "gemma3n:latest"):
        self.name = name
        self.interval = interval
        self.notifications = notifications
        self.enabled = enabled
        self.use_ai = use_ai
        self.ollama = ollama
        self.ai_model = ai_model
        self._stop_event = threading.Event()
        self._thread = None
        self._last_check_time = 0.0
        self._check_count = 0
        self._error_count = 0
        self._idle_multiplier = 1

    @property
    def ai_available(self) -> bool:
        """True only if AI is enabled, client exists, and Ollama is reachable."""
        return bool(self.use_ai and self.ollama and self.ollama.is_available())

    def _ai_generate(self, prompt: str, system: str = "", max_tokens: int = 256) -> str | None:
        """Safe wrapper around ollama.generate. Returns None on any failure."""
        if not self.ollama:
            return None
        try:
            return self.ollama.generate(prompt, model=self.ai_model,
                                        system=system, max_tokens=max_tokens)
        except Exception:
            return None

    def start(self):
        """Launch daemon thread."""
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name=f"c3-agent-{self.name}", daemon=True)
        self._thread.start()

    def stop(self):
        """Signal stop and join with timeout."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def _loop(self):
        """Main loop: initial delay, then check() every interval.

        Idle backoff: subclasses may return ``False`` from ``check()`` to signal
        "no work this cycle". The next sleep is multiplied up to
        ``_IDLE_BACKOFF_MAX``. Any other return value (including ``None``)
        resets to the base interval, preserving legacy behavior.
        """
        if self._stop_event.wait(timeout=5):
            return
        while not self._stop_event.is_set():
            result = None
            try:
                self._last_check_time = time.time()
                self._check_count += 1
                result = self.check()
            except Exception:
                self._error_count += 1
            if result is False:
                self._idle_multiplier = min(self._idle_multiplier * 2, self._IDLE_BACKOFF_MAX)
            else:
                self._idle_multiplier = 1
            if self._stop_event.wait(timeout=self.interval * self._idle_multiplier):
                break

    @abstractmethod
    def check(self):
        """Override in subclasses to perform periodic analysis."""

    def notify(self, severity: str, title: str, message: str, ai_enhanced: bool = False,
               replace_if_unacked: bool = False):
        """Convenience wrapper for notifications.add()."""
        self.notifications.add(self.name, severity, title, message,
                               ai_enhanced=ai_enhanced, replace_if_unacked=replace_if_unacked)

    def get_status(self) -> dict:
        """Return agent runtime status for UI/API consumption."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "running": self._thread is not None and self._thread.is_alive() if self._thread else False,
            "interval": self.interval,
            "use_ai": self.use_ai,
            "ai_available": self.ai_available,
            "check_count": self._check_count,
            "error_count": self._error_count,
            "last_check": self._last_check_time,
        }

    def run_once(self) -> dict:
        """Execute a single check immediately for manual UI/API triggers."""
        self._last_check_time = time.time()
        self._check_count += 1
        try:
            self.check()
            return {"ok": True, "status": self.get_status()}
        except Exception as e:
            self._error_count += 1
            return {"ok": False, "error": str(e), "status": self.get_status()}
