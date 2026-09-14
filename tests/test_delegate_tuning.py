"""2.137.0 tuning from the first live run of the downshift (2026-09-14).

- A scout without tier/model defaults to Sonnet: the Haiku scout took 28 turns
  and $0.24 for a lookup Sonnet did in 8 turns for $0.046.
- delegate.claude_tier_effort passes --effort per tier. It has no default:
  measured, --effort low changed nothing on Haiku 4.5 (22 eval cases, $0.1271
  vs $0.1272, same mean output tokens).
- A guard refusal while packing is logged under the backend routing chose,
  not the requested 'host'.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from cli.tools import delegate
from core.config import DELEGATE_DEFAULTS
from services import access_guard


def _svc(tmp, **cfg):
    return SimpleNamespace(project_path=str(tmp), delegate_config={**DELEGATE_DEFAULTS, **cfg},
                           notifications=None, compressor=None, ollama_client=None, session_mgr=None,
                           _agent_progress_cb=None)


def _capture(store):
    def finalize(tool, meta, resp, status, **kw):
        store.update(meta=meta, resp=resp, status=status)
        return resp
    return finalize


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
        return "answer", True, {"model": model, "turns": 1}

    monkeypatch.setattr(delegate, "_run_claude", fake_run)
    return calls


def test_defaults():
    assert DELEGATE_DEFAULTS["claude_scout_default_tier"] == "medium"
    assert DELEGATE_DEFAULTS["claude_tier_effort"] == {}


def test_scout_defaults_to_sonnet_and_plain_calls_stay_on_haiku(runs, tmp_path):
    delegate.handle_delegate("q", "ask", "", "", _svc(tmp_path), _capture({}), backend="claude", scout=True)
    delegate.handle_delegate("q", "ask", "ctx", "", _svc(tmp_path), _capture({}), backend="claude")
    assert [c["model"] for c in runs] == ["sonnet", "haiku"]


def test_host_scout_routes_to_medium(monkeypatch, runs, tmp_path):
    monkeypatch.setattr(delegate, "host_backend", lambda svc: ("claude-code", "claude"))
    store = {}
    delegate.handle_delegate("q", "ask", "", "", _svc(tmp_path), _capture(store), backend="host", scout=True)
    assert runs[-1]["model"] == "sonnet" and store["meta"]["tier"] == "medium"


def test_explicit_tier_still_wins_for_a_scout(runs, tmp_path):
    delegate.handle_delegate("q", "ask", "", "", _svc(tmp_path), _capture({}), backend="claude",
                             scout=True, tier="small")
    assert runs[-1]["model"] == "haiku"


def test_effort_per_tier(runs, tmp_path):
    store = {}
    delegate.handle_delegate("a", "ask", "ctx", "", _svc(tmp_path), _capture(store), backend="claude")
    assert runs[-1]["effort"] == "" and "effort" not in store["meta"]  # no default
    cfg = {"claude_tier_effort": {"medium": "low"}}
    delegate.handle_delegate("b", "ask", "ctx", "", _svc(tmp_path, **cfg), _capture(store), backend="claude",
                             tier="medium")
    assert runs[-1]["effort"] == "low" and store["meta"]["effort"] == "low"
    delegate.handle_delegate("c", "ask", "ctx", "", _svc(tmp_path, **cfg), _capture(store), backend="claude")
    assert runs[-1]["effort"] == ""


@pytest.mark.parametrize("cfg, tier, expected", [
    ({"claude_effort": "high"}, "small", "high"),          # global override wins
    ({"claude_tier_effort": {"medium": "xhigh"}}, "medium", "xhigh"),
    ({"claude_tier_effort": {"small": "ultra"}}, "small", ""),  # unknown level ignored
    ({"claude_tier_effort": "low"}, "small", ""),          # malformed table ignored
    ({}, "large", ""),
])
def test_claude_effort_for(cfg, tier, expected):
    assert delegate.claude_effort_for(tier, cfg) == expected


def test_effort_changes_the_cache_key(runs, tmp_path):
    delegate.handle_delegate("same", "ask", "ctx", "", _svc(tmp_path), _capture({}), backend="claude")
    delegate.handle_delegate("same", "ask", "ctx", "", _svc(tmp_path, claude_tier_effort={"small": "low"}),
                             _capture({}), backend="claude")
    assert len(runs) == 2


def test_blocked_refusal_is_logged_under_the_routed_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate, "host_backend", lambda svc: ("claude-code", "claude"))
    denial = access_guard.Denial(rule="**/.env*", kind="deny", scope="builtin", reason="r")
    monkeypatch.setattr(delegate.access_guard, "verdict",
                        lambda p, op, root: access_guard.Verdict("denied", denial=denial))
    recorded, store = [], {}
    svc = _svc(tmp_path)
    svc.session_mgr = SimpleNamespace(record_tool_tokens=lambda tool, **kw: recorded.append(kw["detail"]))
    delegate.handle_delegate("q", "ask", "", ".env", svc, _capture(store), backend="host")
    assert store["status"] == "blocked"
    assert store["meta"] == {"task_type": "ask", "backend": "claude", "tier": "small"}
    detail = recorded[-1]
    assert (detail["backend"], detail["backend_requested"], detail["tier"], detail["outcome"]) == (
        "claude", "host", "small", "blocked")
