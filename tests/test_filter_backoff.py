"""Stream D (P7): adaptive pass-2 backoff tests for OutputFilter.

A slow Ollama must not stall every filtered tool output: each pass-2 call gets
a hard per-call timeout, and after N consecutive slow calls pass-2 is
suspended for a cooldown window (noted once in the filter output). Ollama is
stubbed — no network.
"""
import time

from services.output_filter import OutputFilter

SUSPEND_NOTE = "[filter:fast] pass2 suspended, slow ollama"


class _StubOllama:
    """Stand-in for OllamaClient.generate. delay simulates a slow model;
    result=None mirrors the real client's timeout/failure behavior."""

    def __init__(self, delay: float = 0.0, result: str | None = "short summary"):
        self.delay = delay
        self.result = result
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        return self.result


def _noisy_text() -> str:
    """Distinct lines that survive pass-1 with more than a couple of tokens."""
    return "\n".join(
        f"processing item {i} with payload value {i * 17} for stage {i % 5}"
        for i in range(40)
    )


def _make_filter(stub: _StubOllama, **cfg) -> OutputFilter:
    base = {"filter_llm_threshold": 1, "filter_pass2_timeout": 0.1}
    base.update(cfg)
    filt = OutputFilter(base)
    filt.ollama = stub
    return filt


# ---------------------------------------------------------------------------
# Fast path unaffected
# ---------------------------------------------------------------------------

def test_fast_pass2_unaffected():
    stub = _StubOllama(delay=0.0, result="quick run summary")
    filt = _make_filter(stub)

    res = filt.filter(_noisy_text(), use_llm=True)

    assert res["llm_used"] is True
    assert res["pass_used"] == 2
    assert res["pass2_suspended"] is False
    assert res["filtered"].startswith("[c3:filter:llm]")
    assert SUSPEND_NOTE not in res["filtered"]

    metrics = filt.get_metrics()
    assert metrics["pass2_calls"] == 1
    assert metrics["pass2_timeouts"] == 0
    assert metrics["pass2_suspended"] == 0

    # The per-call timeout is actually forwarded to the Ollama client.
    assert stub.calls[0]["timeout"] == 0.1


def test_mixed_latency_does_not_suspend():
    stub = _StubOllama(delay=0.0, result=None)
    filt = _make_filter(stub)
    text = _noisy_text()

    # slow, fast, slow, fast — never 3 consecutive slow calls.
    for delay in (0.25, 0.0, 0.25, 0.0):
        stub.delay = delay
        filt.filter(text, use_llm=True)

    metrics = filt.get_metrics()
    assert len(stub.calls) == 4              # pass-2 never suspended
    assert metrics["pass2_calls"] == 4
    assert metrics["pass2_timeouts"] == 2
    assert metrics["pass2_suspended"] == 0


# ---------------------------------------------------------------------------
# Suspension after consecutive slow calls
# ---------------------------------------------------------------------------

def test_slow_pass2_triggers_suspension_and_notes_once():
    stub = _StubOllama(delay=0.25, result=None)  # slower than the 0.1s budget
    filt = _make_filter(stub, filter_pass2_suspend_seconds=300.0)
    text = _noisy_text()

    for _ in range(3):
        res = filt.filter(text, use_llm=True)
        assert res["llm_used"] is False

    metrics = filt.get_metrics()
    assert metrics["pass2_calls"] == 3
    assert metrics["pass2_timeouts"] == 3
    assert metrics["pass2_suspended"] == 1

    # 4th call: pass-2 skipped entirely, note emitted once in the header.
    res4 = filt.filter(text, use_llm=True)
    assert len(stub.calls) == 3
    assert res4["pass2_suspended"] is True
    assert res4["llm_used"] is False
    assert res4["filtered"].startswith(SUSPEND_NOTE)

    # 5th call: still skipped, but the note is not repeated.
    res5 = filt.filter(text, use_llm=True)
    assert len(stub.calls) == 3
    assert res5["pass2_suspended"] is True
    assert SUSPEND_NOTE not in res5["filtered"]

    # Counters unchanged while suspended (no calls, no timeouts).
    metrics = filt.get_metrics()
    assert metrics["pass2_calls"] == 3
    assert metrics["pass2_timeouts"] == 3
    assert metrics["pass2_suspended"] == 1


def test_pass2_recovers_after_cooldown():
    stub = _StubOllama(delay=0.25, result=None)
    filt = _make_filter(stub, filter_pass2_suspend_seconds=0.15)
    text = _noisy_text()

    for _ in range(3):
        filt.filter(text, use_llm=True)
    assert filt.get_metrics()["pass2_suspended"] == 1
    assert filt.filter(text, use_llm=True)["pass2_suspended"] is True
    assert len(stub.calls) == 3

    # Ollama recovers; after the cooldown pass-2 resumes.
    stub.delay = 0.0
    stub.result = "recovered summary"
    time.sleep(0.2)

    res = filt.filter(text, use_llm=True)
    assert len(stub.calls) == 4
    assert res["llm_used"] is True
    assert res["pass_used"] == 2
    assert res["pass2_suspended"] is False
    # Window was cleared on suspension: one fast call keeps pass-2 healthy.
    assert filt.get_metrics()["pass2_suspended"] == 1


def test_empty_input_reports_no_suspension():
    filt = _make_filter(_StubOllama())
    res = filt.filter("", use_llm=True)
    assert res["pass2_suspended"] is False
    assert res["pass_used"] == 0
