"""Stream D (P7): backend='auto' cascade routing tests for c3_delegate.

Covers the ordered-preference walk (heavy: codex -> gemini -> ollama; light:
ollama -> codex -> gemini), breaker/availability skips, the cascade note in
the response, the all-backends-down error, and the explicit-backend rule
(an explicit choice with an open breaker errors instead of silently rerouting).

No real subprocesses or network: backends and the Ollama client are mocked.
"""
import pytest

from cli.tools import delegate

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeOllama:
    def __init__(self, up: bool = True):
        self.up = up
        self.calls = []

    def is_available(self, timeout=None):
        return self.up

    def list_models(self):
        return ["llama3.2:3b", "gemma3n:latest"]

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return ("local model answer with enough concrete detail that the "
                "confidence estimator does not treat it as an empty reply")


class _FakeSvc:
    def __init__(self, ollama=None):
        self.project_path = "."
        self.delegate_config = {
            "enabled": True,
            "codex_enabled": True,
            "gemini_enabled": True,
            "auto_compress": False,
            "allow_model_fallback": False,
            "breaker_failure_threshold": 3,
            "breaker_cooldown_seconds": 60,
        }
        self.notifications = None
        self.compressor = None
        self.ollama_client = ollama
        self._agent_progress_cb = None


def _capture_finalize(store: dict):
    def finalize(tool, meta, resp, status, **kw):
        store.update({"tool": tool, "meta": meta, "resp": resp, "status": status})
        return resp
    return finalize


def _trip_breaker(name: str, dcfg: dict):
    br = delegate._backend_breaker(name, dcfg)
    for _ in range(br.failure_threshold):
        br.record_failure()
    assert not br.allow()
    return br


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Isolate module-global breaker/cache/availability state per test.

    Also pins the Access Guard to inactive: handle_delegate consults
    has_active_rules() against the REAL filesystem, and a host with seeded
    global rules (every install since v2.86.0) skips write-capable backends
    (gemini/claude) unless allow_write_delegation=true — which silently
    rewrote these tests' expected routes on such hosts while CI's clean
    home kept passing. The guard posture is a test INPUT, never ambient
    state; the guard-specific tests below set it explicitly.
    """
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()
    monkeypatch.setattr(delegate, "_codex_available", True, raising=False)
    monkeypatch.setattr(delegate, "_gemini_available", True, raising=False)
    monkeypatch.setattr(delegate, "_claude_available", True, raising=False)
    monkeypatch.setattr(delegate.access_guard, "has_active_rules",
                        lambda _path: False)
    yield
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()


def _fake_handler(name: str, calls: list):
    def handler(task, task_type, context, file_path, svc, dcfg, finalize):
        calls.append(name)
        return finalize("c3_delegate", {"task_type": task_type, "backend": name},
                        f"{name} output", "ok")
    return handler


# ---------------------------------------------------------------------------
# Cascade order table
# ---------------------------------------------------------------------------

def test_cascade_order_table():
    # Heavy tasks: cloud CLIs first, local last.
    for heavy in ("review", "diagnose", "improve", "test"):
        assert delegate._cascade_order(heavy, {}) == ["codex", "gemini", "ollama"]
    # Light tasks: local first, cloud only as fallback.
    for light in ("ask", "explain", "summarize", "docstring"):
        assert delegate._cascade_order(light, {}) == ["ollama", "codex", "gemini"]
    # Config-driven heavy sets are respected per backend.
    dcfg = {"codex_task_types": ["review"], "gemini_task_types": ["review", "summarize"]}
    assert delegate._cascade_order("summarize", dcfg) == ["gemini", "ollama"]
    assert delegate._cascade_order("review", dcfg) == ["codex", "gemini", "ollama"]


# ---------------------------------------------------------------------------
# Auto routing: selection matrix
# ---------------------------------------------------------------------------

def test_auto_heavy_prefers_codex_when_all_healthy(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _fake_handler("codex", calls))
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))
    svc = _FakeSvc(ollama=_FakeOllama(up=True))
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")

    assert calls == ["codex"]
    assert store["meta"]["backend"] == "codex"
    assert "cascade" not in store["meta"]  # default choice, no reroute note


def test_auto_codex_breaker_open_routes_to_gemini(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _fake_handler("codex", calls))
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))
    svc = _FakeSvc(ollama=_FakeOllama(up=True))
    _trip_breaker("codex", svc.delegate_config)
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")

    assert calls == ["gemini"]
    assert "codex breaker open" in store["meta"]["cascade"]
    assert "routed to gemini" in store["meta"]["cascade"]
    assert store["resp"].startswith("[delegate] codex breaker open")
    assert "gemini output" in store["resp"]


def test_auto_codex_and_gemini_open_routes_to_ollama(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _fake_handler("codex", calls))
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))
    ollama = _FakeOllama(up=True)
    svc = _FakeSvc(ollama=ollama)
    _trip_breaker("codex", svc.delegate_config)
    _trip_breaker("gemini", svc.delegate_config)
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")

    assert calls == []                      # no cloud CLI touched
    assert len(ollama.calls) == 1           # local model actually ran
    assert "routed to ollama" in store["meta"]["cascade"]
    assert "codex breaker open" in store["meta"]["cascade"]
    assert "gemini breaker open" in store["meta"]["cascade"]
    assert store["resp"].startswith("[delegate] ")
    assert store["meta"]["model"] == "llama3.2:3b"


def test_auto_no_healthy_backend_returns_clear_error(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _fake_handler("codex", calls))
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))
    svc = _FakeSvc(ollama=None)  # ollama down too
    _trip_breaker("codex", svc.delegate_config)
    _trip_breaker("gemini", svc.delegate_config)
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")

    assert calls == []
    assert store["status"] == "unavailable"
    assert "No healthy backend" in store["resp"]
    for name in ("codex", "gemini", "ollama"):
        assert name in store["resp"]


def test_auto_skips_disabled_backend(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _fake_handler("codex", calls))
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))
    svc = _FakeSvc(ollama=_FakeOllama(up=True))
    svc.delegate_config["codex_enabled"] = False
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")

    assert calls == ["gemini"]
    assert "codex disabled" in store["meta"]["cascade"]
    assert "routed to gemini" in store["meta"]["cascade"]


def test_auto_light_task_prefers_ollama(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("cloud backend must not be used for a light task with healthy ollama")
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _boom)
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _boom)
    ollama = _FakeOllama(up=True)
    svc = _FakeSvc(ollama=ollama)
    store = {}

    delegate.handle_delegate("q", "ask", "", "", svc, _capture_finalize(store), backend="auto")

    assert len(ollama.calls) == 1
    assert "cascade" not in store["meta"]


def test_auto_light_task_falls_to_cloud_when_ollama_down(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _fake_handler("codex", calls))
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))
    svc = _FakeSvc(ollama=_FakeOllama(up=False))
    store = {}

    delegate.handle_delegate("q", "ask", "", "", svc, _capture_finalize(store), backend="auto")

    assert calls == ["codex"]
    assert "ollama unreachable" in store["meta"]["cascade"]
    assert "routed to codex" in store["meta"]["cascade"]


# ---------------------------------------------------------------------------
# Explicit backend choice must NOT silently reroute
# ---------------------------------------------------------------------------

def test_explicit_backend_with_open_breaker_errors_without_reroute(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))

    def _no_spawn(*_a, **_k):
        raise AssertionError("open breaker must short-circuit before spawning codex")
    monkeypatch.setattr(delegate, "_run_codex", _no_spawn)

    svc = _FakeSvc(ollama=_FakeOllama(up=True))
    _trip_breaker("codex", svc.delegate_config)
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="codex")

    assert calls == []                       # no silent reroute to gemini
    assert store["status"] == "degraded"
    assert "Codex skipped after repeated failures" in store["resp"]
    assert "retrying in" in store["resp"]    # cooldown surfaced to the user
    assert "cascade" not in store["meta"]


# ---------------------------------------------------------------------------
# Access Guard posture — the input the autouse fixture pins to inactive.
# These two tests set it explicitly and assert BOTH sides of the gate, so
# the host's real rule state can never silently rewrite a route again.
# ---------------------------------------------------------------------------

def test_auto_guard_active_blocks_gemini_without_opt_in(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _fake_handler("codex", calls))
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: True)
    svc = _FakeSvc(ollama=_FakeOllama(up=True))
    _trip_breaker("codex", svc.delegate_config)
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store),
                             backend="auto")

    # gemini is write-capable: with rules active and no opt-in the cascade
    # must fall through to ollama, and say why.
    assert calls == []
    assert "Access Guard" in store["meta"]["cascade"]
    assert "ollama" in store["meta"]["cascade"]


def test_auto_guard_active_with_opt_in_routes_to_gemini(monkeypatch):
    calls = []
    monkeypatch.setattr(delegate, "_handle_codex_delegate", _fake_handler("codex", calls))
    monkeypatch.setattr(delegate, "_handle_gemini_delegate", _fake_handler("gemini", calls))
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: True)
    svc = _FakeSvc(ollama=_FakeOllama(up=True))
    _trip_breaker("codex", svc.delegate_config)
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store),
                             backend="auto", allow_write_delegation=True)

    assert calls == ["gemini"]
