"""Tests for the CircuitBreaker primitive and the c3_delegate demote-on-failure wiring."""
import time

from services.circuit_breaker import CircuitBreaker


def test_closed_until_threshold_then_opens():
    br = CircuitBreaker("x", failure_threshold=3, cooldown_seconds=60)
    assert br.allow()
    assert br.record_failure() is False  # 1
    assert br.record_failure() is False  # 2
    assert br.allow()                    # still closed below threshold
    assert br.record_failure() is True   # 3 -> trips it open at threshold
    assert not br.allow()                # open, within cooldown
    assert br.is_open


def test_success_closes_breaker():
    br = CircuitBreaker("x", failure_threshold=2, cooldown_seconds=60)
    br.record_failure()
    assert br.record_failure() is True
    assert not br.allow()
    br.record_success()
    assert br.allow()
    assert br.cooldown_remaining() == 0
    assert not br.is_open


def test_half_open_probe_after_cooldown():
    br = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=0.05)
    assert br.record_failure() is True
    assert not br.allow()                # blocked within cooldown
    time.sleep(0.08)
    assert br.allow()                    # half-open: one probe allowed
    assert br.record_failure() is False  # failed probe re-arms, not a new trip
    assert not br.allow()


def test_cooldown_remaining_within_bounds():
    br = CircuitBreaker("x", failure_threshold=1, cooldown_seconds=30)
    br.record_failure()
    assert 0 < br.cooldown_remaining() <= 30


class _FakeNotifications:
    def __init__(self):
        self.entries = []

    def add(self, **kwargs):
        self.entries.append(kwargs)
        return kwargs


class _FakeSvc:
    def __init__(self):
        self.project_path = "."
        self.delegate_config = {
            "gemini_enabled": True,
            "auto_compress": False,
            "breaker_failure_threshold": 3,
            "breaker_cooldown_seconds": 60,
        }
        self.notifications = _FakeNotifications()
        self.compressor = None
        self._agent_progress_cb = None


def _finalize(_tool, _meta, _output, status):
    return status


def test_gemini_demotes_after_repeated_failures(monkeypatch):
    """The core bug fix: a broken backend must stop re-spawning the CLI every call."""
    from cli.tools import delegate

    # Isolate module-global breaker + cache + availability state.
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()
    monkeypatch.setattr(delegate, "_gemini_available", True, raising=False)

    calls = {"run": 0}

    def fake_run_gemini(*_a, **_k):
        calls["run"] += 1
        return ("boom", False, {})

    monkeypatch.setattr(delegate, "_run_gemini", fake_run_gemini)

    svc = _FakeSvc()
    dcfg = svc.delegate_config

    # Three real failures trip the breaker (threshold=3).
    for _ in range(3):
        assert delegate._handle_gemini_delegate("t", "ask", "", "", svc, dcfg, _finalize) == "error"
    assert calls["run"] == 3

    # Fourth call: breaker open -> short-circuit, NO subprocess re-spawn.
    assert delegate._handle_gemini_delegate("t", "ask", "", "", svc, dcfg, _finalize) == "degraded"
    assert calls["run"] == 3

    # The trip emitted exactly one degradation notification.
    degraded = [e for e in svc.notifications.entries if "degraded" in e.get("title", "").lower()]
    assert len(degraded) == 1
