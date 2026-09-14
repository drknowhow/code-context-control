"""Per-tier thinking cap (MAX_THINKING_TOKENS) for the claude delegate.

--effort low had no measurable effect on Haiku 4.5 (2.137.0). A thinking cap
did: one Haiku review went from 4,831 output tokens / 54 s uncapped to 1,186 /
12 s at 1024 and 308 / 6 s at 0. These tests pin the plumbing; the defaults
come from the eval recorded in the CHANGELOG.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.tools import delegate
from core.config import DELEGATE_DEFAULTS
from services.bench import delegate_eval as de


def _svc(tmp, **cfg):
    return SimpleNamespace(project_path=str(tmp), delegate_config={**DELEGATE_DEFAULTS, **cfg},
                           notifications=None, compressor=None, ollama_client=None, session_mgr=None,
                           _agent_progress_cb=None)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: False)
    yield
    delegate._delegate_cache.clear()


@pytest.fixture
def runs(monkeypatch):
    calls = []

    def fake_run(prompt, system_prompt, model="", **kw):
        calls.append({"model": model, **kw})
        return "answer", True, {"model": model}

    monkeypatch.setattr(delegate, "_run_claude", fake_run)
    return calls


def test_defaults_cap_haiku_only(runs, tmp_path):
    assert DELEGATE_DEFAULTS["claude_thinking_tokens"] is None
    assert DELEGATE_DEFAULTS["claude_tier_thinking_tokens"] == {"small": 1024}
    finalize = lambda tool, meta, resp, status, **kw: resp  # noqa: E731
    delegate.handle_delegate("a", "ask", "ctx", "", _svc(tmp_path), finalize, backend="claude")
    delegate.handle_delegate("b", "ask", "ctx", "", _svc(tmp_path), finalize, backend="claude", tier="medium")
    delegate.handle_delegate("c", "ask", "", "", _svc(tmp_path), finalize, backend="claude", scout=True)
    assert [(r["model"], r["thinking_cap"]) for r in runs] == [("haiku", 1024), ("sonnet", None), ("sonnet", None)]


@pytest.mark.parametrize("cfg, tier, expected", [
    ({}, "small", None),
    ({"claude_tier_thinking_tokens": {"small": 1024}}, "small", 1024),
    ({"claude_tier_thinking_tokens": {"small": 1024}}, "medium", None),
    ({"claude_tier_thinking_tokens": {"small": 0}}, "small", 0),            # 0 = thinking off, not "unset"
    ({"claude_thinking_tokens": 4096, "claude_tier_thinking_tokens": {"small": 0}}, "small", 4096),
    ({"claude_thinking_tokens": 0}, "large", 0),
    ({"claude_tier_thinking_tokens": {"small": "2048"}}, "small", 2048),
    ({"claude_tier_thinking_tokens": {"small": -5}}, "small", None),
    ({"claude_tier_thinking_tokens": {"small": "lots"}}, "small", None),
    ({"claude_tier_thinking_tokens": {"small": True}}, "small", None),
    ({"claude_tier_thinking_tokens": [1024]}, "small", None),
])
def test_cap_resolution(cfg, tier, expected):
    assert delegate.claude_thinking_cap_for(tier, cfg) == expected


def test_env_sets_the_cap_and_never_inherits_one(monkeypatch):
    monkeypatch.setenv("MAX_THINKING_TOKENS", "99999")
    assert "MAX_THINKING_TOKENS" not in delegate._claude_env()
    assert delegate._claude_env(0)["MAX_THINKING_TOKENS"] == "0"
    assert delegate._claude_env(1024)["MAX_THINKING_TOKENS"] == "1024"


def test_handler_passes_the_cap_and_records_it(runs, tmp_path):
    store = {}

    def finalize(tool, meta, resp, status, **kw):
        store.update(meta=meta)
        return resp

    cfg = {"claude_tier_thinking_tokens": {"small": 512}}
    delegate.handle_delegate("q", "ask", "ctx", "", _svc(tmp_path, **cfg), finalize, backend="claude")
    assert runs[-1]["thinking_cap"] == 512 and store["meta"]["thinking_cap"] == 512
    delegate.handle_delegate("q2", "ask", "ctx", "", _svc(tmp_path, **cfg), finalize, backend="claude",
                             tier="large")
    assert runs[-1]["thinking_cap"] is None and "thinking_cap" not in store["meta"]


def test_cap_changes_the_cache_key(runs, tmp_path):
    finalize = lambda tool, meta, resp, status, **kw: resp  # noqa: E731
    delegate.handle_delegate("same", "ask", "ctx", "", _svc(tmp_path, claude_thinking_tokens=0), finalize,
                             backend="claude")
    delegate.handle_delegate("same", "ask", "ctx", "", _svc(tmp_path, claude_thinking_tokens=1024), finalize,
                             backend="claude")
    assert len(runs) == 2


def test_telemetry_detail_keeps_a_zero_cap():
    d = delegate.delegate_telemetry_detail({"backend": "claude", "tier": "small", "thinking_cap": 0}, "ok",
                                           requested_backend="host", task_type="ask", host="", wall_ms=1)
    assert d["thinking_cap"] == 0


def test_eval_target_flags():
    assert de.target_overrides("claude:small") == {}
    assert de.target_overrides("claude:small+think=1024") == {"claude_thinking_tokens": 1024}
    assert de.target_overrides("claude:medium+scout+think=0+effort=low") == {
        "claude_thinking_tokens": 0, "claude_effort": "low"}
    assert de.target_overrides("claude:small+colour=red") == {}
    assert de.parse_target("claude:small+think=0") == ("claude", "small")


def test_eval_live_case_applies_target_overrides_to_a_copy(monkeypatch):
    seen = []

    def fake_handle(task, task_type, context, file_path, svc, finalize, backend, **kw):
        seen.append(dict(svc.delegate_config))
        return finalize("c3_delegate", {"backend": backend}, "x", "ok")

    monkeypatch.setattr("cli.tools.delegate.handle_delegate", fake_handle)
    svc = SimpleNamespace(project_path=".", delegate_config={"claude_thinking_tokens": None})
    case = de.DelegateCase.from_dict({"id": "c", "task_type": "ask", "task": "t",
                                      "checks": {"must_match": ["x"]}})
    de.run_live_case(case, "claude:small+think=0", svc)
    de.run_live_case(case, "claude:small", svc)
    assert seen[0]["claude_thinking_tokens"] == 0
    assert seen[1]["claude_thinking_tokens"] is None
    assert svc.delegate_config == {"claude_thinking_tokens": None}  # the shared svc is untouched
