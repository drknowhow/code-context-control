"""D2 of the delegate remediation: backend='host' and tiers on every provider.

- backend='host' routes to the calling agent's own provider (claude-code ->
  claude, codex -> codex, grok -> grok, antigravity -> gemini) at
  delegate.default_tier; a host without a backend, or whose backend the guard
  would block, falls back to auto with a note.
- auto tries the host backend first.
- tiers step Claude model size, Codex/Grok reasoning effort, Gemini model
  size; an explicit backend with no tier and no model is unchanged.
- task_type='ping' makes a live call and reports it as a probe.
- Codex failures carry the event-stream reason (2.132.0 saw 25 of 25 as a bare
  "exit code 1"); Codex usage reaches the meta.
- file_path is packed through the guard for every backend (masked refuses).
- Ollama never resolves to an Ollama Cloud tag unless configured by exact name (#182).

No real subprocess or network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cli.tools import delegate
from cli.tools.delegate import host_backend as REAL_HOST_BACKEND  # before conftest patches it
from services import access_guard


class _FakeOllama:
    def __init__(self, models):
        self.models = models
        self.calls = []

    def is_available(self, timeout=None):
        return True

    def list_models(self):
        return list(self.models)

    def generate(self, **kw):
        self.calls.append(kw)
        return "a local answer with enough words to count as a confident, complete reply here"


def _svc(tmp=".", ollama=None, **cfg):
    return SimpleNamespace(project_path=str(tmp),
                           delegate_config={"enabled": True, "codex_enabled": True, "gemini_enabled": True,
                                            "grok_enabled": True, "auto_compress": False,
                                            "allow_model_fallback": False, **cfg},
                           notifications=None, compressor=None, ollama_client=ollama,
                           session_mgr=None, activity_log=SimpleNamespace(get_recent=lambda limit=8: []),
                           _agent_progress_cb=None)


def _capture(store):
    def finalize(tool, meta, resp, status, **kw):
        store.update(tool=tool, meta=meta, resp=resp, status=status)
        return resp
    return finalize


def _recorder(name, calls):
    def handler(task, task_type, context, file_path, svc, dcfg, finalize, **kw):
        calls.append((name, kw))
        return finalize("c3_delegate", {"task_type": task_type, "backend": name, **(
            {"tier": kw["tier"]} if kw.get("tier") else {})}, f"{name} out", "ok")
    return handler


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()
    for flag in ("_codex_available", "_gemini_available", "_claude_available", "_grok_available"):
        monkeypatch.setattr(delegate, flag, True, raising=False)
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: False)
    yield
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()


@pytest.fixture
def handlers(monkeypatch):
    calls = []
    for name in ("claude", "codex", "gemini", "grok"):
        monkeypatch.setattr(delegate, f"_handle_{name}_delegate", _recorder(name, calls))
    return calls


def _host(monkeypatch, provider):
    monkeypatch.setattr(delegate, "host_backend",
                        lambda svc: (provider, delegate.HOST_BACKENDS.get(provider, "")))


# ── Routing ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("provider, backend", [
    ("claude-code", "claude"), ("codex", "codex"), ("grok", "grok"), ("antigravity", "gemini")])
def test_host_routes_to_the_same_provider_at_the_default_tier(monkeypatch, handlers, provider, backend):
    _host(monkeypatch, provider)
    store = {}
    delegate.handle_delegate("t", "ask", "c", "", _svc(), _capture(store), backend="host")
    assert handlers == [(backend, {"tier": "small"} if backend != "claude"
                         else {"tier": "small", "model": "", "scout": False})]
    assert store["meta"]["backend"] == backend and "cascade" not in store["meta"]


def test_host_respects_explicit_tier_model_and_default_tier(monkeypatch, handlers):
    _host(monkeypatch, "codex")
    delegate.handle_delegate("t", "ask", "", "", _svc(default_tier="medium"), _capture({}), backend="host")
    delegate.handle_delegate("t", "ask", "", "", _svc(), _capture({}), backend="host", tier="large")
    delegate.handle_delegate("t", "ask", "", "", _svc(), _capture({}), backend="host", model="gpt-x")
    assert [kw for _n, kw in handlers] == [{"tier": "medium"}, {"tier": "large"}, {"model": "gpt-x"}]


def test_host_without_a_backend_falls_back_to_auto(monkeypatch, handlers):
    _host(monkeypatch, "cursor")
    store = {}
    delegate.handle_delegate("t", "review", "", "", _svc(ollama=_FakeOllama(["llama3.2:3b"])),
                             _capture(store), backend="host")
    assert handlers[0][0] == "codex"
    assert "host cursor has no same-provider backend -> auto" in store["resp"]


def test_host_blocked_by_the_guard_falls_back_to_auto(monkeypatch, handlers):
    _host(monkeypatch, "antigravity")
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: True)
    store = {}
    delegate.handle_delegate("t", "review", "", "", _svc(), _capture(store), backend="host")
    assert handlers[0][0] == "codex" and store["status"] == "ok"
    assert "blocked by Access Guard" in store["resp"]


def test_auto_tries_the_host_backend_first(monkeypatch, handlers):
    _host(monkeypatch, "claude-code")
    store = {}
    delegate.handle_delegate("t", "review", "", "", _svc(), _capture(store), backend="auto")
    assert handlers == [("claude", {"tier": "", "model": "", "scout": False})]


def test_host_comes_from_the_runtime_not_the_project_config(monkeypatch, tmp_path):
    (tmp_path / ".c3").mkdir()
    (tmp_path / ".c3" / "config.json").write_text('{"ide": "codex"}', encoding="utf-8")
    for var in ("C3_HOST", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(var, raising=False)
    svc = _svc(tmp_path)
    assert REAL_HOST_BACKEND(svc) == ("codex", "codex")  # no runtime host: config fallback
    svc.ide_name = "claude-code"
    assert REAL_HOST_BACKEND(svc) == ("claude-code", "claude")
    svc.ide_name = "grok-build"
    assert REAL_HOST_BACKEND(svc) == ("grok", "grok")


def test_cascade_order_with_a_host():
    assert delegate._cascade_order("review", {}, host="claude") == ["claude", "codex", "gemini", "grok", "ollama"]
    assert delegate._cascade_order("ask", {}, host="grok") == ["grok", "ollama", "codex", "gemini"]
    assert delegate._cascade_order("ask", {}) == ["ollama", "codex", "gemini", "grok"]


def test_explicit_backend_without_tier_passes_no_tier(handlers):
    delegate.handle_delegate("t", "review", "", "", _svc(), _capture({}), backend="codex")
    assert handlers == [("codex", {})]


def test_unknown_backend_is_an_error_not_ollama():
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(), _capture(store), backend="gpt")
    assert store["status"] == "error" and "Unknown backend: gpt" in store["resp"]


def test_claude_is_healthy_in_the_cascade_only_when_enabled_and_installed(monkeypatch):
    assert delegate._cascade_skip_reason("claude", {}, _svc()) is None
    assert delegate._cascade_skip_reason("claude", {"claude_enabled": False}, _svc()) == "disabled"
    monkeypatch.setattr(delegate, "_claude_available", None)
    monkeypatch.setattr(delegate, "_is_claude_on_path", lambda: False)
    assert delegate._cascade_skip_reason("claude", {}, _svc()) == "not on PATH"


# ── Tiers per backend ───────────────────────────────────────────────────────


def test_tier_overrides_table():
    assert delegate._tier_overrides("codex", "small", "", {}) == ({"tier": "small", "effort": "low"}, "")
    assert delegate._tier_overrides("grok", "large", "", {}) == ({"tier": "large", "effort": "high"}, "")
    assert delegate._tier_overrides("gemini", "small", "", {}) == (
        {"tier": "small", "model": "gemini-2.5-flash-lite"}, "")
    assert delegate._tier_overrides("codex", "default", "", {}) == ({"tier": "default"}, "")
    assert delegate._tier_overrides("codex", "", "gpt-y", {}) == ({"model": "gpt-y", "tier": "custom"}, "")
    cfg = {"codex_tier_models": {"small": "my-mini"}, "codex_tier_reasoning": {"small": "minimal"}}
    assert delegate._tier_overrides("codex", "small", "", cfg) == (
        {"tier": "small", "model": "my-mini", "effort": "minimal"}, "")
    assert "unknown tier" in delegate._tier_overrides("grok", "tiny", "", {})[1]
    assert "not a valid model id" in delegate._tier_overrides("codex", "", "--yolo", {})[1]


def test_codex_tier_steps_reasoning_and_no_tier_keeps_config(monkeypatch):
    runs = []

    def fake_run(**kw):
        runs.append(kw)
        kw["stats"].update({"input_tokens": 100, "output_tokens": 5})
        return "OK", True

    monkeypatch.setattr(delegate, "_run_codex", fake_run)
    monkeypatch.setattr("cli.tools._grants.session_id", lambda svc: "s")
    store = {}
    delegate._handle_codex_delegate("t", "ask", "", "", _svc(), _svc().delegate_config, _capture(store),
                                    tier="small")
    assert runs[-1]["reasoning"] == "low" and runs[-1]["model"] == ""
    assert (store["meta"]["tier"], store["meta"]["effort"], store["meta"]["input_tokens"]) == ("small", "low", 100)
    cfg = {**_svc().delegate_config, "codex_reasoning_effort": "high"}
    delegate._handle_codex_delegate("t2", "ask", "", "", _svc(), cfg, _capture(store))
    assert runs[-1]["reasoning"] == "high" and "tier" not in store["meta"]


def test_grok_argv_carries_the_effort():
    cmd = delegate._grok_cmd("p.md", "", 4, "/tmp", reasoning_effort="low")
    assert cmd[cmd.index("--reasoning-effort") + 1] == "low"
    assert "--reasoning-effort" not in delegate._grok_cmd("p.md", "", 4, "/tmp")


def test_gemini_tier_picks_the_model(monkeypatch):
    seen = {}

    def fake_run(task, context, model, timeout=45, idle_timeout=15, cwd=None):
        seen["model"] = model
        return "ok answer", True, {"input_tokens": 1, "output_tokens": 1, "cached_tokens": 0}

    monkeypatch.setattr(delegate, "_run_gemini", fake_run)
    store = {}
    delegate._handle_gemini_delegate("t", "ask", "", "", _svc(), _svc().delegate_config, _capture(store),
                                     tier="large")
    assert seen["model"] == "gemini-2.5-pro" and store["meta"]["tier"] == "large"


# ── Codex failures and usage ────────────────────────────────────────────────


_REJECTED = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "01a09fc9-4ebb-7813-9fff-53286d643561"},
    {"type": "turn.started"},
    {"type": "error", "message": "The 'gpt-5.3-codex-spark' model is not supported when using Codex with a ChatGPT account."},
    {"type": "turn.failed", "error": {"message": "{\"type\":\"error\",\"status\":400}"}},
])
_ANSWERED = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "01a09fc9-61cc-7063-b903-daea98de4425"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
    {"type": "turn.completed", "usage": {"input_tokens": 24101, "cached_input_tokens": 13056,
                                         "output_tokens": 5, "reasoning_output_tokens": 0}},
])


def _fake_codex_process(monkeypatch, stdout, returncode):
    class Proc:
        def __init__(self, *a, **kw):
            self.returncode = returncode

    monkeypatch.setattr(delegate.subprocess, "Popen", Proc)
    monkeypatch.setattr(delegate, "_communicate_with_heartbeat",
                        lambda proc, timeout=0, idle_timeout=0, stdin_text=None: (stdout, "", "ok"))


def test_codex_nonzero_exit_reports_the_event_stream_reason(monkeypatch):
    _fake_codex_process(monkeypatch, _REJECTED, 1)
    stats = {}
    text, ok = delegate._execute_codex(["codex"], "p", 10, 0, ".", stats=stats)
    assert not ok and "not supported when using Codex with a ChatGPT account" in text
    assert text != "[codex:error] exit code 1"


def test_codex_usage_is_parsed(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate, "_delegate_binding",
                        lambda cwd, origin="": (tmp_path / "b.json", str(tmp_path), "o"))
    _fake_codex_process(monkeypatch, _ANSWERED, 0)
    stats = {}
    assert delegate._execute_codex(["codex"], "p", 10, 0, str(tmp_path), stats=stats) == ("OK", True)
    assert stats == {"input_tokens": 24101, "output_tokens": 5, "cached_tokens": 13056, "reasoning_tokens": 0}


# ── ping ────────────────────────────────────────────────────────────────────


def test_ping_is_a_live_call_reported_as_a_probe(monkeypatch):
    _host(monkeypatch, "claude-code")
    seen = {}

    def claude(task, task_type, context, file_path, svc, dcfg, finalize, tier="", model="", scout=False):
        seen.update(task=task, task_type=task_type, tier=tier)
        return finalize("c3_delegate", {"task_type": task_type, "backend": "claude", "tier": "small",
                                        "model": "claude-haiku-4-5", "elapsed": "2.9s",
                                        "cost_usd": 0.0021}, "OK", "ok")

    monkeypatch.setattr(delegate, "_handle_claude_delegate", claude)
    recorded = []
    svc = _svc()
    svc.session_mgr = SimpleNamespace(record_tool_tokens=lambda tool, **kw: recorded.append(kw["detail"]))
    store = {}
    delegate.handle_delegate("", "ping", "ignored", "ignored.py", svc, _capture(store), backend="host")
    assert seen == {"task": delegate.PING_TASK, "task_type": "ask", "tier": "small"}
    assert store["resp"] == ("[delegate:ping] ok backend=claude tier=small model=claude-haiku-4-5 "
                             "elapsed=2.9s cost=$0.0021\nOK")
    assert recorded[-1]["probe"] is True and recorded[-1]["task_type"] == "ping"


def test_available_names_the_host(monkeypatch):
    _host(monkeypatch, "claude-code")
    for name in ("codex", "gemini", "claude", "grok"):
        monkeypatch.setattr(delegate, f"check_{name}", lambda n=name: {"status": "ok", "version": n})
    store = {}
    delegate.handle_delegate("", "available", "", "", _svc(ollama=_FakeOllama(["llama3.2:3b"])), _capture(store))
    assert "  host=claude-code -> claude (default tier small)" in store["resp"]
    assert "(--version only)" in store["resp"]


# ── Guarded packing everywhere ──────────────────────────────────────────────


def test_every_backend_refuses_a_masked_file_path(monkeypatch, tmp_path):
    rule = SimpleNamespace(glob="data/**", scope="project", preset="redact")
    monkeypatch.setattr(delegate.access_guard, "verdict",
                        lambda p, op, root: access_guard.Verdict("masked", mask_rule=rule))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "x.csv").write_text("a\n", encoding="utf-8")
    svc = _svc(tmp_path, ollama=_FakeOllama(["llama3.2:3b"]), auto_compress=True)
    with pytest.raises(access_guard.AccessDenied):
        delegate.handle_delegate("t", "ask", "", "data/x.csv", svc, _capture({}), backend="ollama")
    monkeypatch.setattr(delegate, "_run_grok", lambda **kw: pytest.fail("grok must not run"))
    with pytest.raises(access_guard.AccessDenied):
        delegate._handle_grok_delegate("t", "ask", "", "data/x.csv", svc, svc.delegate_config, _capture({}))


def test_packed_file_reaches_ollama_whole(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate.access_guard, "verdict",
                        lambda p, op, root: access_guard.Verdict("allowed"))
    (tmp_path / "net.py").write_text("MAX_RETRIES = 7\n", encoding="utf-8")
    ollama = _FakeOllama(["llama3.2:3b"])
    delegate.handle_delegate("How many?", "ask", "", "net.py", _svc(tmp_path, ollama=ollama, auto_compress=True),
                             _capture({}), backend="ollama")
    assert "MAX_RETRIES = 7" in ollama.calls[-1]["prompt"]


# ── Ollama Cloud tags (#182) ────────────────────────────────────────────────


def test_is_cloud_tag():
    for tag in ("deepseek-v4-pro:cloud", "gpt-oss:20b-cloud", "kimi-k2.6:cloud", "GLM-5.1:CLOUD"):
        assert delegate.is_cloud_tag(tag), tag
    for tag in ("gemma3:12b", "qwen2.5:1.5b", "cloudy:latest", "soundcloud-embed:1b"):
        assert not delegate.is_cloud_tag(tag), tag


TAGS = ["deepseek-v4-pro:cloud", "gpt-oss:20b-cloud", "gemma4:12b", "nomic-embed-text:latest"]


def test_ollama_fallback_never_lands_on_a_cloud_tag():
    ollama = _FakeOllama(TAGS)
    store = {}
    delegate.handle_delegate("q", "ask", "ctx", "", _svc(ollama=ollama), _capture(store), backend="ollama")
    assert ollama.calls[-1]["model"] == "gemma4:12b"


def test_prefix_match_does_not_cross_to_cloud_but_an_exact_name_may():
    ollama = _FakeOllama(TAGS)
    delegate.handle_delegate("q", "ask", "ctx", "", _svc(ollama=ollama, preferred_model="gpt-oss:20b"),
                             _capture({}), backend="ollama")
    assert ollama.calls[-1]["model"] == "gemma4:12b"
    delegate._delegate_cache.clear()
    delegate.handle_delegate("q", "ask", "ctx", "", _svc(ollama=ollama, preferred_model="gpt-oss:20b-cloud"),
                             _capture({}), backend="ollama")
    assert ollama.calls[-1]["model"] == "gpt-oss:20b-cloud"


def test_only_cloud_tags_means_no_local_model():
    store = {}
    delegate.handle_delegate("q", "ask", "ctx", "", _svc(ollama=_FakeOllama(TAGS[:2])), _capture(store),
                             backend="ollama")
    assert store["status"] == "unavailable" and "No compatible local model" in store["resp"]
