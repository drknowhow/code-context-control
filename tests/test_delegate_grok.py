"""c3_delegate backend='grok' (xAI Grok Build CLI) tests.

Covers the headless argv, the child env (identity stripped, compat imports
off), JSON/token parsing against a recorded 1.0.30 fixture, error/timeout
paths, both cwd choices (read-only: throwaway temp dir that is removed;
write mode: the project), the Access Guard gate (grok is write-capable only
in write mode), the 'available' health check and the auto cascade.

No real subprocesses or network: Popen and the heartbeat reader are mocked.
"""
import json
import os
import tempfile
from pathlib import Path

import pytest

from cli.tools import delegate
from core.config import DELEGATE_DEFAULTS

FIXTURE = Path(__file__).parent / "fixtures" / "grok" / "headless_json_1.0.30.json"
FAKE_EXE = "/usr/bin/grok"  # not a .cmd shim, so harden_win_argv leaves it a list

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
        return ["llama3.2:3b"]

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return ("local model answer with enough concrete detail that the "
                "confidence estimator does not treat it as an empty reply")


class _FakeSvc:
    def __init__(self, project_path=".", ollama=None, **cfg):
        self.project_path = project_path
        self.delegate_config = {
            "enabled": True,
            "codex_enabled": True,
            "gemini_enabled": True,
            "grok_enabled": True,
            "auto_compress": False,
            "allow_model_fallback": False,
            "breaker_failure_threshold": 3,
            "breaker_cooldown_seconds": 60,
            **cfg,
        }
        self.notifications = None
        self.compressor = None
        self.ollama_client = ollama
        self._agent_progress_cb = None


class _FakePopen:
    instances = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = 0
        cwd = kwargs.get("cwd")
        self.cwd_existed = bool(cwd) and os.path.isdir(cwd)
        pf = argv[argv.index("--prompt-file") + 1]
        self.prompt_file = pf
        self.prompt = Path(pf).read_text(encoding="utf-8")
        _FakePopen.instances.append(self)


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


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()
    _FakePopen.instances = []
    for flag in ("_codex_available", "_gemini_available", "_claude_available", "_grok_available"):
        monkeypatch.setattr(delegate, flag, True, raising=False)
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: False)
    yield
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    """Mock Popen + heartbeat; record the temp dirs _run_grok creates."""
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def _mkdtemp(**kw):
        path = real_mkdtemp(dir=str(tmp_path), **kw)
        created.append(path)
        return path

    state = {"stdout": FIXTURE.read_text(encoding="utf-8"), "stderr": "",
             "status": "ok", "returncode": 0, "heartbeat": {}}

    def _popen(argv, **kwargs):
        proc = _FakePopen(argv, **kwargs)
        proc.returncode = state["returncode"]
        return proc

    def _heartbeat(proc, timeout=45, idle_timeout=15, stdin_text=None):
        state["heartbeat"] = {"timeout": timeout, "idle_timeout": idle_timeout}
        return state["stdout"], state["stderr"], state["status"]

    monkeypatch.setattr(delegate, "_which", lambda name: FAKE_EXE if name == "grok" else None)
    monkeypatch.setattr(delegate.tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(delegate.subprocess, "Popen", _popen)
    monkeypatch.setattr(delegate, "_communicate_with_heartbeat", _heartbeat)
    state["created"] = created
    return state


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------

def test_cmd_read_only_defaults(monkeypatch):
    monkeypatch.setattr(delegate, "_which", lambda name: FAKE_EXE)
    cmd = delegate._grok_cmd("/tmp/x/prompt.md", "", 8, "/tmp/x")
    assert cmd[0] == FAKE_EXE
    assert cmd[cmd.index("--prompt-file") + 1] == "/tmp/x/prompt.md"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--tools") + 1] == "read_file,grep,list_dir"
    assert cmd[cmd.index("--max-turns") + 1] == "8"
    assert cmd[cmd.index("--cwd") + 1] == "/tmp/x"
    assert "-m" not in cmd          # empty model never pins a name
    assert "--yolo" not in cmd


def test_cmd_model_is_passed_when_set(monkeypatch):
    monkeypatch.setattr(delegate, "_which", lambda name: FAKE_EXE)
    cmd = delegate._grok_cmd("p.md", "grok-custom", 3, "/w")
    assert cmd[cmd.index("-m") + 1] == "grok-custom"


def test_cmd_allow_write_uses_yolo_without_tools(monkeypatch):
    monkeypatch.setattr(delegate, "_which", lambda name: FAKE_EXE)
    cmd = delegate._grok_cmd("p.md", "", 8, "/proj", allow_write=True)
    assert "--yolo" in cmd
    assert "--tools" not in cmd


# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------

def test_child_env_strips_grok_identity(monkeypatch):
    for name in ("GROK_SESSION_ID", "GROK_HOOK_EVENT", "GROK_HOOK_NAME", "GROK_WORKSPACE_ROOT"):
        monkeypatch.setenv(name, "inherited")
    env = delegate._child_host_env("grok")
    for name in ("GROK_SESSION_ID", "GROK_HOOK_EVENT", "GROK_HOOK_NAME", "GROK_WORKSPACE_ROOT"):
        assert name not in env
    assert env["C3_HOST"] == "grok"


def test_grok_env_disables_updater_and_compat_imports(monkeypatch):
    monkeypatch.setenv("GROK_SESSION_ID", "inherited")
    env = delegate._grok_env()
    assert "GROK_SESSION_ID" not in env
    assert env["GROK_DISABLE_AUTOUPDATER"] == "1"
    for vendor in ("CLAUDE", "CURSOR"):
        for kind in ("AGENTS", "HOOKS", "MCPS", "RULES", "SKILLS"):
            assert env[f"GROK_{vendor}_{kind}_ENABLED"] == "0"


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def test_parse_fixture_and_token_stats():
    data = delegate._grok_json(FIXTURE.read_text(encoding="utf-8"))
    assert data["text"].startswith("I'll run the plumbing checks")
    stats = delegate._grok_token_stats(data)
    assert stats == {
        "input_tokens": 23526,
        "output_tokens": 1673,
        "cached_tokens": 196352,
        "reasoning_tokens": 1484,
        "cost_usd": 0.05279044,
    }


def test_parse_tolerates_leading_non_json_lines():
    raw = "Update available: 1.0.31\nwarning: something\n" + json.dumps({"text": "hi"})
    assert delegate._grok_json(raw) == {"text": "hi"}
    assert delegate._grok_json("not json at all") is None
    assert delegate._grok_json("") is None


def test_token_stats_without_optional_fields():
    stats = delegate._grok_token_stats({"usage": {"input_tokens": 5, "output_tokens": 2}})
    assert stats == {"input_tokens": 5, "output_tokens": 2, "cached_tokens": 0}


# ---------------------------------------------------------------------------
# _run_grok
# ---------------------------------------------------------------------------

def test_run_success_uses_temp_cwd_and_removes_it(fake_run):
    out, ok, stats = delegate._run_grok("do the thing", "some context", "", timeout=77)

    assert ok is True
    assert out.startswith("I'll run the plumbing checks")
    assert stats["input_tokens"] == 23526 and stats["cached_tokens"] == 196352
    assert stats["cost_usd"] == pytest.approx(0.05279044)

    proc = _FakePopen.instances[-1]
    [workdir] = fake_run["created"]
    assert proc.kwargs["cwd"] == workdir
    assert proc.cwd_existed                                  # existed while grok ran
    assert proc.argv[proc.argv.index("--cwd") + 1] == workdir
    assert Path(proc.prompt_file).parent == Path(workdir)    # prompt lives inside it
    assert "--tools" in proc.argv and "--yolo" not in proc.argv
    assert "Context:\nsome context" in proc.prompt and "Task:\ndo the thing" in proc.prompt
    assert proc.kwargs["stdin"] == delegate.subprocess.DEVNULL
    assert proc.kwargs["env"]["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert fake_run["heartbeat"] == {"timeout": 77, "idle_timeout": 0}
    assert not os.path.exists(workdir)                       # removed in finally


def test_run_write_mode_uses_project_cwd(fake_run, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    out, ok, _stats = delegate._run_grok("edit it", "", "", allow_write=True, cwd=str(project))

    assert ok is True
    proc = _FakePopen.instances[-1]
    [workdir] = fake_run["created"]
    assert proc.kwargs["cwd"] == str(project)
    assert proc.argv[proc.argv.index("--cwd") + 1] == str(project)
    assert "--yolo" in proc.argv and "--tools" not in proc.argv
    assert Path(proc.prompt_file).parent == Path(workdir)    # prompt never lands in the project
    assert not os.path.exists(workdir)
    assert list(project.iterdir()) == []


def test_run_write_mode_without_cwd_refuses(fake_run):
    out, ok, _ = delegate._run_grok("edit it", "", "", allow_write=True)
    assert ok is False and out.startswith("[grok:error]")
    assert _FakePopen.instances == []


def test_run_nonzero_exit_reports_stderr(fake_run):
    fake_run.update(stdout="", stderr="auth expired", returncode=1)
    out, ok, stats = delegate._run_grok("t", "", "")
    assert ok is False
    assert out == "[grok:error] auth expired"
    assert stats["input_tokens"] == 0
    assert not os.path.exists(fake_run["created"][0])


def test_run_nonzero_exit_falls_back_to_json_error(fake_run):
    fake_run.update(stdout=json.dumps({"error": "rate limited"}), stderr="", returncode=1)
    out, ok, _ = delegate._run_grok("t", "", "")
    assert (out, ok) == ("[grok:error] rate limited", False)


@pytest.mark.parametrize("status,prefix", [("timeout", "[grok:timeout]"),
                                           ("idle_timeout", "[grok:idle_timeout]")])
def test_run_timeouts(fake_run, status, prefix):
    fake_run.update(status=status, stdout="")
    out, ok, _ = delegate._run_grok("t", "", "", timeout=5, idle_timeout=3)
    assert ok is False and out.startswith(prefix)
    assert not os.path.exists(fake_run["created"][0])


def test_run_empty_text_is_an_error(fake_run):
    fake_run.update(stdout=json.dumps({"text": "", "stopReason": "max_turns"}))
    out, ok, _ = delegate._run_grok("t", "", "")
    assert ok is False and "max_turns" in out


def test_run_popen_failure_still_removes_temp_dir(fake_run, monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("spawn failed")
    monkeypatch.setattr(delegate.subprocess, "Popen", _boom)
    out, ok, _ = delegate._run_grok("t", "", "")
    assert (ok, out) == (False, "[grok:error] spawn failed")
    assert not os.path.exists(fake_run["created"][0])


def test_run_not_on_path(monkeypatch):
    monkeypatch.setattr(delegate, "_which", lambda name: None)
    out, ok, _ = delegate._run_grok("t", "", "")
    assert ok is False and "not found" in out


# ---------------------------------------------------------------------------
# Handler + routing
# ---------------------------------------------------------------------------

def _mock_run_grok(monkeypatch, calls, result=("grok answer", True, {"input_tokens": 3,
                                                                     "output_tokens": 4,
                                                                     "cached_tokens": 0})):
    def _run(**kwargs):
        calls.append(kwargs)
        return result
    monkeypatch.setattr(delegate, "_run_grok", _run)


def test_handler_read_only_passes_no_cwd(monkeypatch, tmp_path):
    calls = []
    _mock_run_grok(monkeypatch, calls)
    svc = _FakeSvc(project_path=str(tmp_path), grok_timeout=33, grok_max_turns=4)
    store = {}

    delegate.handle_delegate("t", "review", "ctx", "", svc, _capture_finalize(store), backend="grok")

    assert store["status"] == "ok" and store["resp"] == "grok answer"
    assert calls[0]["allow_write"] is False and calls[0]["cwd"] is None
    assert calls[0]["model"] == "" and calls[0]["timeout"] == 33 and calls[0]["max_turns"] == 4
    assert store["meta"]["mode"] == "read-only" and store["meta"]["output_tokens"] == 4


def test_handler_write_mode_passes_project_cwd(monkeypatch, tmp_path):
    calls = []
    _mock_run_grok(monkeypatch, calls)
    svc = _FakeSvc(project_path=str(tmp_path), grok_allow_write=True)
    store = {}

    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="grok")

    assert calls[0]["allow_write"] is True and calls[0]["cwd"] == str(tmp_path)
    assert store["meta"]["mode"] == "write"


def test_handler_disabled(monkeypatch):
    calls = []
    _mock_run_grok(monkeypatch, calls)
    svc = _FakeSvc(grok_enabled=False)
    store = {}
    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="grok")
    assert store["status"] == "disabled" and calls == []


def test_handler_failures_trip_breaker(monkeypatch):
    calls = []
    _mock_run_grok(monkeypatch, calls, result=("[grok:error] boom", False,
                                               {"input_tokens": 0, "output_tokens": 0,
                                                "cached_tokens": 0}))
    svc = _FakeSvc()
    for i in range(3):
        store = {}
        delegate.handle_delegate(f"t{i}", "review", "", "", svc, _capture_finalize(store),
                                 backend="grok")
        assert store["status"] == "error"
    store = {}
    delegate.handle_delegate("t9", "review", "", "", svc, _capture_finalize(store), backend="grok")
    assert store["status"] == "degraded" and len(calls) == 3


def test_grok_check_task_type(monkeypatch):
    monkeypatch.setattr(delegate, "check_grok", lambda: {"status": "ok", "version": "1.0.30"})
    store = {}
    delegate.handle_delegate("", "grok_check", "", "", _FakeSvc(), _capture_finalize(store))
    assert store["resp"] == "[delegate:grok_check] status=ok 1.0.30"


# ---------------------------------------------------------------------------
# Access Guard gate
# ---------------------------------------------------------------------------

def test_guard_active_read_only_grok_is_not_blocked(monkeypatch):
    calls = []
    _mock_run_grok(monkeypatch, calls)
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: True)
    store = {}
    delegate.handle_delegate("t", "review", "", "", _FakeSvc(), _capture_finalize(store),
                             backend="grok")
    assert store["status"] == "ok" and len(calls) == 1


def test_guard_active_write_mode_grok_is_blocked_without_opt_in(monkeypatch):
    calls = []
    _mock_run_grok(monkeypatch, calls)
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: True)
    svc = _FakeSvc(grok_allow_write=True)
    store = {}
    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="grok")
    assert store["status"] == "blocked" and calls == []
    assert "[delegate:blocked]" in store["resp"] and ".grok" in store["resp"]

    store = {}
    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="grok",
                             allow_write_delegation=True)
    assert store["status"] == "ok" and len(calls) == 1


def test_write_capable_table():
    assert delegate._write_capable("gemini", {}) and delegate._write_capable("claude", {})
    assert not delegate._write_capable("grok", {})
    assert delegate._write_capable("grok", {"grok_allow_write": True})
    assert not delegate._write_capable("codex", {"grok_allow_write": True})


# ---------------------------------------------------------------------------
# available + cascade
# ---------------------------------------------------------------------------

def test_available_lists_grok_and_counts_all_backends(monkeypatch):
    for name in ("codex", "gemini", "claude", "grok"):
        monkeypatch.setattr(delegate, f"check_{name}",
                            lambda n=name: {"status": "ok", "version": f"{n}-1"})
    store = {}
    delegate.handle_delegate("", "available", "", "", _FakeSvc(ollama=_FakeOllama()),
                             _capture_finalize(store))
    assert store["resp"].startswith("[delegate:available] 5/5 backends up")
    assert "  grok=ok grok-1" in store["resp"]
    assert store["status"] == "5/5 up"


def test_auto_routes_to_grok_after_codex_and_gemini(monkeypatch):
    calls = []
    _mock_run_grok(monkeypatch, calls)
    svc = _FakeSvc(ollama=_FakeOllama())
    _trip_breaker("codex", svc.delegate_config)
    _trip_breaker("gemini", svc.delegate_config)
    store = {}
    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")
    assert len(calls) == 1
    assert "routed to grok" in store["meta"]["cascade"]


def test_auto_skips_disabled_grok(monkeypatch):
    calls = []
    _mock_run_grok(monkeypatch, calls)
    ollama = _FakeOllama()
    svc = _FakeSvc(ollama=ollama, grok_enabled=False)
    _trip_breaker("codex", svc.delegate_config)
    _trip_breaker("gemini", svc.delegate_config)
    store = {}
    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")
    assert calls == [] and len(ollama.calls) == 1
    assert "grok disabled" in store["meta"]["cascade"]


def test_auto_guard_blocks_only_write_mode_grok(monkeypatch):
    calls = []
    _mock_run_grok(monkeypatch, calls)
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: True)

    # Read-only grok: gemini is blocked (write-capable), grok is chosen.
    svc = _FakeSvc(ollama=_FakeOllama())
    _trip_breaker("codex", svc.delegate_config)
    store = {}
    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")
    assert len(calls) == 1 and "routed to grok" in store["meta"]["cascade"]

    # Write-mode grok: blocked too, cascade falls through to ollama.
    delegate._backend_breakers.clear()
    ollama = _FakeOllama()
    svc = _FakeSvc(ollama=ollama, grok_allow_write=True)
    _trip_breaker("codex", svc.delegate_config)
    store = {}
    delegate.handle_delegate("t", "review", "", "", svc, _capture_finalize(store), backend="auto")
    assert len(calls) == 1 and len(ollama.calls) == 1
    assert "grok blocked by Access Guard" in store["meta"]["cascade"]


def test_auto_grok_not_on_path_is_skipped(monkeypatch):
    monkeypatch.setattr(delegate, "_grok_available", None)
    monkeypatch.setattr(delegate, "_is_grok_on_path", lambda: False)
    assert delegate._cascade_skip_reason("grok", {"grok_enabled": True}, _FakeSvc()) == "not on PATH"


def test_config_defaults():
    assert DELEGATE_DEFAULTS["grok_enabled"] is True
    assert DELEGATE_DEFAULTS["grok_model"] == ""
    assert DELEGATE_DEFAULTS["grok_timeout"] == 120
    assert DELEGATE_DEFAULTS["grok_max_turns"] == 8
    assert DELEGATE_DEFAULTS["grok_allow_write"] is False
