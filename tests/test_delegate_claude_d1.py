"""D1 of the delegate remediation: the claude backend as a locked-down tier downshift.

Before 2.133.0, backend='claude' ran ``claude -p <prompt> --output-format text``
in the project: the user's default model (Opus), CLAUDE.md, every hook and MCP
server, the permission allowlist. Measured 2026-09-14 on a one-sentence answer:
46,191 prompt tokens, 9.1 s, $0.4637 — and Access Guard blocked it on any
machine with rules, because it could write. These tests pin the replacement:

- tiers resolve to CLI aliases (small=haiku, medium=sonnet, large=opus,
  default = no --model), config-overridable, flag-injection-safe;
- the argv has no tools, no MCP, no customizations, JSON output, prompt on stdin;
- it runs in a throwaway directory with Claude Code's nesting variables removed;
- JSON usage (tokens, cost, turns, the model that answered) reaches the meta;
- file_path is packed by C3 through the Access Guard read verdict — a denial
  raises, a masked path refuses, a big file travels as its map;
- being tool-less, it is not write-capable, so an active guard no longer blocks it.

Recorded outputs are from Claude Code 2.1.270. No real subprocess.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.tools import delegate
from services import access_guard

FIXTURES = Path(__file__).parent / "fixtures" / "claude"
FAKE_EXE = "/usr/local/bin/claude"  # not a .cmd shim, so harden_win_argv keeps the list


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class _FakePopen:
    instances = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4343
        self.returncode = 0
        cwd = kwargs.get("cwd")
        self.cwd = cwd
        self.cwd_existed = bool(cwd) and os.path.isdir(cwd)
        _FakePopen.instances.append(self)


def _svc(tmp, **cfg):
    return SimpleNamespace(project_path=str(tmp),
                           delegate_config={"enabled": True, "auto_compress": False, **cfg},
                           notifications=None, compressor=None, ollama_client=None,
                           session_mgr=None, _agent_progress_cb=None)


def _capture(store):
    def finalize(tool, meta, resp, status, **kw):
        store.update(tool=tool, meta=meta, resp=resp, status=status)
        return resp
    return finalize


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()
    _FakePopen.instances = []
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: False)
    yield
    delegate._backend_breakers.clear()
    delegate._delegate_cache.clear()


@pytest.fixture
def fake_claude(monkeypatch, tmp_path):
    state = {"record": _fixture("haiku_ok"), "status": "ok", "calls": []}
    real_mkdtemp = delegate.tempfile.mkdtemp

    def _mkdtemp(**kw):
        return real_mkdtemp(dir=str(tmp_path), **kw)

    def _popen(argv, **kwargs):
        proc = _FakePopen(argv, **kwargs)
        proc.returncode = state["record"]["returncode"]
        return proc

    def _heartbeat(proc, timeout=45, idle_timeout=15, stdin_text=None):
        state["calls"].append({"timeout": timeout, "idle_timeout": idle_timeout,
                               "stdin": stdin_text, "proc": proc})
        rec = state["record"]
        return json.dumps(rec["stdout"]), rec["stderr"], state["status"]

    monkeypatch.setattr(delegate, "_which", lambda name: FAKE_EXE if name == "claude" else None)
    monkeypatch.setattr(delegate.tempfile, "mkdtemp", _mkdtemp)
    monkeypatch.setattr(delegate.subprocess, "Popen", _popen)
    monkeypatch.setattr(delegate, "_communicate_with_heartbeat", _heartbeat)
    return state


# ── Tiers ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tier, expected", [
    ("", ("small", "haiku")),
    ("small", ("small", "haiku")),
    ("MEDIUM", ("medium", "sonnet")),
    ("large", ("large", "opus")),
    ("default", ("default", "")),
    ("parent", ("default", "")),
    ("sonnet", ("medium", "sonnet")),
])
def test_tier_resolution(tier, expected):
    resolved, model, problem = delegate.resolve_claude_tier(tier, "", {})
    assert (resolved, model, problem) == (*expected, "")


def test_tier_table_and_default_tier_come_from_config():
    dcfg = {"claude_default_tier": "medium",
            "claude_tier_models": {"medium": "claude-sonnet-5", "small": "claude-haiku-4-5"}}
    assert delegate.resolve_claude_tier("", "", dcfg)[:2] == ("medium", "claude-sonnet-5")
    assert delegate.resolve_claude_tier("small", "", dcfg)[:2] == ("small", "claude-haiku-4-5")
    assert delegate.resolve_claude_tier("large", "", dcfg)[:2] == ("large", "opus")


def test_explicit_model_wins_and_is_validated():
    assert delegate.resolve_claude_tier("small", "claude-opus-5[1m]", {}) == (
        "custom", "claude-opus-5[1m]", "")
    for bad in ("--dangerously-skip-permissions", "opus; rm", "a b", "-m"):
        assert delegate.resolve_claude_tier("", bad, {})[2]


def test_unknown_tier_and_bad_config_model_are_errors():
    assert "unknown tier" in delegate.resolve_claude_tier("huge", "", {})[2]
    assert "claude_tier_models" in delegate.resolve_claude_tier(
        "small", "", {"claude_tier_models": {"small": "--tools default"}})[2]


# ── argv, env, JSON ─────────────────────────────────────────────────────────


def test_argv_is_locked_down():
    cmd = delegate._claude_cmd("claude", "haiku", "SYS")
    assert cmd[:2] == ["claude", "-p"]
    for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--bare" not in cmd  # --bare would move auth off the subscription
    assert "--effort" not in cmd and "--max-budget-usd" not in cmd
    assert "--mcp-config" not in cmd


def test_argv_optional_flags():
    cmd = delegate._claude_cmd("claude", "", "S", effort="low", max_budget_usd=0.25)
    assert "--model" not in cmd
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.25"
    assert "--max-budget-usd" not in delegate._claude_cmd("claude", "", "S", max_budget_usd=0)


def test_env_drops_nesting_and_session_identity(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    env = delegate._claude_env()
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert env["C3_HOST"] == "claude-code"


def test_parse_haiku_answer():
    text, ok, stats = delegate.parse_claude_json(json.dumps(_fixture("haiku_ok")["stdout"]), "haiku")
    assert ok and text == "51"
    assert stats["model"] == "claude-haiku-4-5-20251001"
    assert stats["cost_usd"] == pytest.approx(0.002047)
    assert stats["turns"] == 1 and stats["output_tokens"] > 0
    assert "models_used" not in stats


def test_parse_picks_the_requested_model_over_the_housekeeping_haiku_call():
    text, ok, stats = delegate.parse_claude_json(json.dumps(_fixture("sonnet_ok")["stdout"]), "sonnet")
    assert ok and text == "51"
    assert stats["model"] == "claude-sonnet-5"
    assert stats["models_used"] == ["claude-haiku-4-5-20251001", "claude-sonnet-5"]
    assert stats["cache_write_tokens"] == 1028
    # without an alias to match, the most expensive entry answered
    assert delegate.parse_claude_json(json.dumps(_fixture("sonnet_ok")["stdout"]), "")[2]["model"] == \
        "claude-sonnet-5"


def test_parse_errors():
    text, ok, _ = delegate.parse_claude_json(json.dumps(_fixture("unknown_model")["stdout"]), "x")
    assert not ok and text.startswith("[claude:error] There's an issue with the selected model")
    assert delegate.parse_claude_json("not json", "")[1] is False
    assert delegate.parse_claude_json(json.dumps({"result": "", "is_error": False}), "")[:2] == (
        "[claude:error] empty result", False)
    noisy = "update available\n" + json.dumps({"result": "ok", "is_error": False})
    assert delegate.parse_claude_json(noisy, "")[:2] == ("ok", True)


def test_run_uses_stdin_a_temp_cwd_and_no_idle_kill(fake_claude, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    text, ok, stats = delegate._run_claude("PROMPT", "SYS", "haiku", timeout=77)
    assert ok and text == "51"
    call = fake_claude["calls"][-1]
    assert call["stdin"] == "PROMPT" and call["idle_timeout"] == 0 and call["timeout"] == 77
    proc = call["proc"]
    assert proc.cwd_existed and not os.path.exists(proc.cwd)  # created, then removed
    assert proc.kwargs["stdin"] == delegate.subprocess.PIPE
    assert "PROMPT" not in proc.argv  # never in argv (Windows 32k limit, process listings)
    assert "CLAUDECODE" not in proc.kwargs["env"]


def test_run_error_paths(fake_claude):
    fake_claude["record"] = _fixture("unknown_model")
    text, ok, _ = delegate._run_claude("p", "s", "nonexistent-model-x")
    assert not ok and "unrecognized_model" in text
    fake_claude["record"] = _fixture("haiku_ok")
    fake_claude["status"] = "timeout"
    text, ok, _ = delegate._run_claude("p", "s", "haiku", timeout=5)
    assert (text, ok) == ("[claude:timeout] No response after 5s", False)


def test_run_without_the_cli(monkeypatch):
    monkeypatch.setattr(delegate, "_which", lambda name: None)
    assert delegate._run_claude("p", "s")[:2] == ("[claude:error] claude CLI not found on PATH", False)


# ── File packing ────────────────────────────────────────────────────────────


class _Compressor:
    def __init__(self, protected=()):
        self.protected = set(protected)

    def is_protected_file(self, path):
        return Path(path).name in self.protected

    def compress_file(self, path, mode):
        assert mode == "map"
        return {"compressed": f"MAP OF {Path(path).name}"}


def test_pack_inlines_small_files_and_maps_big_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(delegate.access_guard, "verdict",
                        lambda p, op, root: access_guard.Verdict("allowed"))
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (tmp_path / "big.py").write_text("value = 12345\n" * 400, encoding="utf-8")
    (tmp_path / "secret.pem").write_text("k", encoding="utf-8")
    svc = _svc(tmp_path)
    svc.compressor = _Compressor(protected={"secret.pem"})
    packed = delegate._pack_file_context("a.py, big.py,secret.pem,gone.py", svc, file_max_tokens=500)
    assert "--- file: a.py (2 lines) ---\nx = 1\ny = 2" in packed
    assert "--- file: big.py (400 lines) ---\n[file map only" in packed and "MAP OF big.py" in packed
    assert "[not included: protected file]" in packed
    assert "--- file: gone.py ---\n[not included: file not found]" in packed


def test_pack_raises_on_a_denied_path(tmp_path, monkeypatch):
    denial = access_guard.Denial(rule="**/.env*", kind="deny", scope="builtin", reason="r")
    monkeypatch.setattr(delegate.access_guard, "verdict",
                        lambda p, op, root: access_guard.Verdict("denied", denial=denial))
    with pytest.raises(access_guard.AccessDenied):
        delegate._pack_file_context(".env", _svc(tmp_path), file_max_tokens=500)


def test_pack_refuses_a_masked_path(tmp_path, monkeypatch):
    rule = SimpleNamespace(glob="data/**", scope="project", preset="redact")
    monkeypatch.setattr(delegate.access_guard, "verdict",
                        lambda p, op, root: access_guard.Verdict("masked", mask_rule=rule))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "rows.csv").write_text("a,b\n", encoding="utf-8")
    with pytest.raises(access_guard.AccessDenied) as exc:
        delegate._pack_file_context("data/rows.csv", _svc(tmp_path), file_max_tokens=500)
    assert exc.value.message.startswith(access_guard.TAG_MASK_UNSUPPORTED)


# ── The handler ─────────────────────────────────────────────────────────────


def test_guard_active_no_longer_blocks_claude(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setattr(delegate.access_guard, "has_active_rules", lambda _p: True)
    store = {}
    out = delegate.handle_delegate("What is 17*3?", "ask", "ctx", "", _svc(tmp_path),
                                   _capture(store), backend="claude")
    assert out == "51" and store["status"] == "ok"
    meta = store["meta"]
    assert (meta["backend"], meta["tier"], meta["model"]) == ("claude", "small", "claude-haiku-4-5-20251001")
    assert meta["cost_usd"] == pytest.approx(0.002047)
    argv = fake_claude["calls"][-1]["proc"].argv
    assert argv[argv.index("--model") + 1] == "haiku"


def test_prompt_uses_the_task_template_and_delegate_rules(fake_claude, tmp_path):
    delegate.handle_delegate("Why?", "diagnose", "Traceback ...", "", _svc(tmp_path), _capture({}),
                             backend="claude", tier="medium")
    call = fake_claude["calls"][-1]
    argv = call["proc"].argv
    system = argv[argv.index("--system-prompt") + 1]
    assert "diagnose failures" in system and "no tools" in system
    assert "Traceback ..." in call["stdin"] and "Problem:\nWhy?" in call["stdin"]
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_file_path_is_packed_into_the_prompt(fake_claude, tmp_path, monkeypatch):
    monkeypatch.setattr(delegate.access_guard, "verdict",
                        lambda p, op, root: access_guard.Verdict("allowed"))
    (tmp_path / "net.py").write_text("MAX_RETRIES = 7\n", encoding="utf-8")
    delegate.handle_delegate("How many retries?", "ask", "", "net.py", _svc(tmp_path), _capture({}),
                             backend="claude")
    assert "--- file: net.py (1 lines) ---\nMAX_RETRIES = 7" in fake_claude["calls"][-1]["stdin"]


def test_bad_tier_spawns_nothing(fake_claude, tmp_path):
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(tmp_path), _capture(store), backend="claude",
                             tier="enormous")
    assert store["status"] == "error" and "unknown tier" in store["resp"]
    assert fake_claude["calls"] == []


def test_disabled_and_breaker(fake_claude, tmp_path):
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(tmp_path, claude_enabled=False), _capture(store),
                             backend="claude")
    assert store["status"] == "disabled"
    br = delegate._backend_breaker("claude", {})
    for _ in range(br.failure_threshold):
        br.record_failure()
    delegate.handle_delegate("t", "ask", "", "", _svc(tmp_path), _capture(store), backend="claude")
    assert store["status"] == "degraded" and fake_claude["calls"] == []


def test_failure_counts_against_the_breaker_and_success_is_cached(fake_claude, tmp_path):
    store = {}
    fake_claude["record"] = _fixture("unknown_model")
    delegate.handle_delegate("t", "ask", "", "", _svc(tmp_path), _capture(store), backend="claude",
                             model="nonexistent-model-x")
    assert store["status"] == "error" and store["meta"]["tier"] == "custom"

    fake_claude["record"] = _fixture("haiku_ok")
    delegate.handle_delegate("same", "ask", "", "", _svc(tmp_path), _capture(store), backend="claude")
    delegate.handle_delegate("same", "ask", "", "", _svc(tmp_path), _capture(store), backend="claude")
    assert store["status"] == "cached" and store["meta"]["tier"] == "small"
    assert len(fake_claude["calls"]) == 2  # the error run + one real answer


def test_auto_task_type_is_inferred(fake_claude, tmp_path):
    store = {}
    delegate.handle_delegate("summarize this log", "auto", "log", "", _svc(tmp_path), _capture(store),
                             backend="claude")
    assert store["meta"]["task_type"] == "summarize"
