"""D0 of the delegate remediation: every c3_delegate exit writes a telemetry detail.

Measured 2026-09-14 over 65 projects: 19 delegate calls since July, and no
record could say which backend answered, which model, what it cost or how it
ended — the outcome sat in the activity log for some, nowhere for the Ollama
paths that returned without finalize. These tests pin:

- delegate_telemetry_detail builds a flat, stable detail from the handler's
  meta and status (usage copied only when reported);
- handle_delegate records it on every exit, including the early Ollama
  returns that used to bypass finalize;
- aggregate_tool_telemetry folds the details into delegate_by_backend.

No subprocess or network.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools import delegate  # noqa: E402
from services.session_manager import SessionManager  # noqa: E402
from services.telemetry import aggregate_tool_telemetry, append_telemetry_record  # noqa: E402


class _RecordingSessionMgr:
    def __init__(self):
        self.calls = []

    def record_tool_tokens(self, tool_name, **kw):
        self.calls.append((tool_name, kw))


def _svc(tmp=".", ollama=None, session_mgr=None, **cfg):
    return SimpleNamespace(
        project_path=tmp,
        delegate_config={"enabled": True, "codex_enabled": True, "gemini_enabled": True,
                         "grok_enabled": True, "auto_compress": False,
                         "allow_model_fallback": False, **cfg},
        notifications=None, compressor=None, ollama_client=ollama,
        session_mgr=session_mgr, _agent_progress_cb=None)


def _finalize(store):
    def finalize(tool, meta, resp, status, **kw):
        store.update(tool=tool, meta=meta, resp=resp, status=status)
        return resp
    return finalize


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: False)
    yield
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()


# ── The detail itself ───────────────────────────────────────────────────────


def test_detail_from_a_grok_answer():
    meta = {"task_type": "review", "backend": "grok", "model": "grok-build", "mode": "read-only",
            "elapsed": "3.1s", "input_tokens": 1200, "output_tokens": 80, "cached_tokens": 900,
            "cost_usd": 0.0042}
    d = delegate.delegate_telemetry_detail(meta, "ok", requested_backend="auto",
                                           task_type="review", host="claude-code", wall_ms=3123.46)
    assert d == {"host": "claude-code", "backend": "grok", "backend_requested": "auto",
                 "task_type": "review", "outcome": "ok", "wall_ms": 3123.5, "model": "grok-build",
                 "mode": "read-only", "input_tokens": 1200, "output_tokens": 80,
                 "cached_tokens": 900, "cost_usd": 0.0042}


def test_detail_ollama_confidence_cached_and_cascade():
    d = delegate.delegate_telemetry_detail({"task": "explain", "backend": "ollama", "model": "m"},
                                           "low", requested_backend="ollama", task_type="auto",
                                           host="codex", wall_ms=10)
    assert (d["task_type"], d["outcome"], d["confidence"]) == ("explain", "ok", "low")
    d = delegate.delegate_telemetry_detail({"backend": "codex", "cached": True, "cascade": "x"},
                                           "cached", requested_backend="auto", task_type="review",
                                           host="", wall_ms=1)
    assert d["outcome"] == "cached" and d["cascade"] is True


def test_detail_probe_and_unknown_status():
    d = delegate.delegate_telemetry_detail({"task_type": "available"}, "3/5 up",
                                           requested_backend="ollama", task_type="available",
                                           host="", wall_ms=5)
    assert d["backend"] == "probe" and d["probe"] is True and d["outcome"] == "3/5 up"


def test_detail_leaves_absent_usage_absent():
    d = delegate.delegate_telemetry_detail({"backend": "codex", "model": "gpt"}, "error",
                                           requested_backend="codex", task_type="review",
                                           host="", wall_ms=1)
    assert not any(k in d for k in delegate._USAGE_KEYS)
    assert d["outcome"] == "error"


# ── handle_delegate records it ──────────────────────────────────────────────


def test_blocked_claude_is_recorded(monkeypatch):
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: True)
    sm = _RecordingSessionMgr()
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(session_mgr=sm), _finalize(store), backend="claude")
    assert store["status"] == "blocked"
    ((tool, kw),) = sm.calls
    assert tool == "c3_delegate"
    assert kw["detail"]["backend"] == "claude"
    assert kw["detail"]["outcome"] == "blocked"
    assert kw["duration_ms"] >= 0


def test_disabled_and_ollama_early_exits_go_through_finalize():
    sm = _RecordingSessionMgr()
    store = {}
    out = delegate.handle_delegate("t", "ask", "", "", _svc(session_mgr=sm, enabled=False),
                                   _finalize(store))
    assert out == "[delegate:disabled]" and store["status"] == "disabled"

    down = SimpleNamespace(is_available=lambda timeout=None: False)
    out = delegate.handle_delegate("t", "ask", "", "", _svc(session_mgr=sm, ollama=down), _finalize(store))
    assert "Ollama unavailable" in out and store["status"] == "unavailable"

    out = delegate.handle_delegate("t", "poem", "", "", _svc(session_mgr=sm), _finalize(store))
    assert "Unknown type" in out and store["status"] == "error"

    empty = SimpleNamespace(is_available=lambda timeout=None: True, list_models=lambda: [])
    out = delegate.handle_delegate("t", "ask", "", "", _svc(session_mgr=sm, ollama=empty), _finalize(store))
    assert "No compatible local model" in out
    assert [kw["detail"]["outcome"] for _t, kw in sm.calls] == [
        "disabled", "unavailable", "error", "unavailable"]
    assert all(kw["detail"]["backend"] == "ollama" for _t, kw in sm.calls[1:])


def test_cascade_reroute_lands_in_the_detail(monkeypatch):
    def fake(name):
        def handler(task, task_type, context, file_path, svc, dcfg, finalize):
            return finalize("c3_delegate", {"task_type": task_type, "backend": name,
                                            "cost_usd": 0.01}, f"{name} out", "ok")
        return handler

    monkeypatch.setattr(delegate, "_handle_gemini_delegate", fake("gemini"))
    monkeypatch.setattr(delegate, "_codex_available", True, raising=False)
    monkeypatch.setattr(delegate, "_gemini_available", True, raising=False)
    br = delegate._backend_breaker("codex", {})
    for _ in range(br.failure_threshold):
        br.record_failure()
    sm = _RecordingSessionMgr()
    delegate.handle_delegate("t", "review", "", "", _svc(session_mgr=sm), _finalize({}), backend="auto")
    detail = sm.calls[-1][1]["detail"]
    assert (detail["backend"], detail["backend_requested"], detail["cascade"], detail["cost_usd"]) == (
        "gemini", "auto", True, 0.01)


def test_missing_session_mgr_and_raising_recorder_never_break_the_response():
    class Boom:
        def record_tool_tokens(self, *a, **kw):
            raise RuntimeError("disk full")

    store = {}
    assert delegate.handle_delegate("t", "ask", "", "", _svc(session_mgr=Boom(), enabled=False),
                                    _finalize(store)) == "[delegate:disabled]"
    assert delegate.handle_delegate("t", "ask", "", "", _svc(enabled=False),
                                    _finalize(store)) == "[delegate:disabled]"


def test_real_session_manager_writes_the_detail_to_tool_telemetry():
    with tempfile.TemporaryDirectory() as tmp:
        sm = SessionManager(tmp)
        sm.start_session("t")
        svc = _svc(tmp, session_mgr=sm, enabled=False)

        def finalize(name, args, resp, summ, **kw):
            sm.log_tool_call(name, args, summ)
            sm.track_response(name, resp, kw.get("response_tokens", 0))
            return resp

        delegate.handle_delegate("t", "ask", "", "", svc, finalize, backend="host")
        row = json.loads((Path(tmp) / ".c3" / "tool_telemetry.jsonl").read_text(encoding="utf-8")
                         .splitlines()[-1])
        assert row["tool"] == "c3_delegate"
        assert row["detail"]["outcome"] == "disabled"
        assert row["detail"]["backend_requested"] == "host"
        assert row["duration_ms"] is not None


# ── Aggregation ─────────────────────────────────────────────────────────────


def test_aggregate_folds_delegate_details():
    with tempfile.TemporaryDirectory() as tmp:
        def rec(detail, ts="2026-09-14T10:00:00+00:00"):
            append_telemetry_record(tmp, {"ts": ts, "tool": "c3_delegate", "response_tokens": 10,
                                          "detail": detail})

        rec({"backend": "claude", "tier": "small", "outcome": "ok", "model": "claude-haiku-4-5",
             "task_type": "review", "cost_usd": 0.002, "input_tokens": 1000, "output_tokens": 50,
             "wall_ms": 3000})
        rec({"backend": "claude", "tier": "small", "outcome": "error", "task_type": "review",
             "wall_ms": 1000})
        rec({"backend": "claude", "tier": "small", "outcome": "cached", "task_type": "ask",
             "wall_ms": 5, "cascade": True})
        rec({"backend": "probe", "task_type": "available", "outcome": "5/5 up", "probe": True})
        rec({"backend": "codex", "outcome": "ok", "model": "gpt", "wall_ms": 9000})
        append_telemetry_record(tmp, {"ts": "2026-09-14T10:00:00+00:00", "tool": "c3_delegate",
                                      "response_tokens": 3})  # pre-2.132.0 row, no detail

        agg = aggregate_tool_telemetry(tmp, days=0)
        rows = agg["delegate_by_backend"]
        assert set(rows) == {"claude:small", "codex", "probe"}
        small = rows["claude:small"]
        assert small["calls"] == 3
        assert small["outcomes"] == {"ok": 1, "error": 1, "cached": 1}
        assert small["ok_rate"] == round(2 / 3, 4)
        assert small["cost_usd"] == 0.002 and small["priced_calls"] == 1
        assert (small["input_tokens"], small["output_tokens"]) == (1000, 50)
        assert small["models"] == {"claude-haiku-4-5": 1}
        assert small["task_types"] == {"review": 2, "ask": 1}
        assert small["cascaded"] == 1
        assert small["wall_ms_p50"] == 1000.0
        assert rows["probe"]["calls"] == 0 and rows["probe"]["probes"] == 1
        assert rows["codex"]["ok_rate"] == 1.0
        assert agg["by_tool"]["c3_delegate"]["calls"] == 6
