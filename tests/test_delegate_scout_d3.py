"""D3 of the delegate remediation: scout=true, a read-only claude delegate that looks things up.

The eval's lookup cases (no context, no file_path) failed on every tool-less
backend by construction. A scout runs ``claude -p --restricted`` in the
project with Read/Grep/Glob only. Measured on Claude Code 2.1.270 before
building it (a .env canary, a repository-wide Grep):

- no controls: the value leaked;
- a PreToolUse hook alone: leaked — a hook sees a Grep's arguments, not its hits;
- ``Read(**/.env*)`` / ``Read(./secrets/**)`` permission denies: no leak;
- an absolute ``Read(//C:/...)`` deny: leaked, so absolute globs are rewritten
  project-relative (or dropped when outside the project, which --restricted
  keeps the tools out of anyway);
- CLAUDE.md is not loaded under --restricted.

So a scout carries both: permission denies derived from Access Guard (deny,
read-holding confirm, every mask rule) and the guard itself as its only hook.
No real subprocess except the guard hook's own entry point.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli.tools import delegate
from services import access_guard

REPO = Path(__file__).resolve().parents[1]


def _svc(tmp, **cfg):
    return SimpleNamespace(project_path=str(tmp),
                           delegate_config={"enabled": True, "auto_compress": False, **cfg},
                           notifications=None, compressor=None, ollama_client=None,
                           session_mgr=None, _agent_progress_cb=None)


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


# ── Guard rules -> permission denies ────────────────────────────────────────


@pytest.mark.parametrize("glob, expected", [
    ("**/.env*", "**/.env*"),
    ("id_rsa", "**/id_rsa"),
    ("secrets/**", "./secrets/**"),
    ("./secrets/**", "./secrets/**"),
    ("c:/work/proj/data/**", "./data/**"),
    ("C:/Work/Proj/data/**", "./data/**"),
    ("c:/elsewhere/**", ""),
    ("/etc/**", ""),
    ("", ""),
])
def test_permission_pattern(glob, expected):
    assert delegate._permission_pattern(glob, "c:/work/proj") == expected


def test_scout_denies_come_from_deny_confirm_all_and_mask_rules(monkeypatch, tmp_path):
    def compile_rule(glob, kind, confirm_ops="write"):
        return SimpleNamespace(glob=glob, kind=kind, confirm_ops=confirm_ops)

    rules = [compile_rule("**/.env*", "deny"), compile_rule("**/.git/**", "read_only"),
             compile_rule("**/claude.md", "confirm"), compile_rule("vault/**", "confirm", "all"),
             compile_rule("**/.env*", "deny")]
    masks = [SimpleNamespace(glob="data/*.csv")]
    monkeypatch.setattr(delegate.access_guard, "load_all", lambda p: (rules, masks, []))
    assert delegate.scout_read_denies(tmp_path) == [
        "Read(**/.env*)", "Read(./vault/**)", "Read(./data/*.csv)"]


def test_corrupt_guard_config_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate.access_guard, "load_all", lambda p: ([], [], ["project"]))
    with pytest.raises(ValueError, match="unreadable"):
        delegate.scout_read_denies(tmp_path)


def test_real_builtin_rules_deny_env_and_c3_secrets(tmp_path):
    denies = delegate.scout_read_denies(tmp_path)
    assert "Read(**/.env*)" in denies
    assert "Read(**/.c3/secrets.enc)" in denies
    assert not any(".git" in d for d in denies)  # write-deny tier: reads stay open


def test_scout_settings_hook_runs_only_the_guard(tmp_path):
    settings = delegate.scout_settings(tmp_path)
    (entry,) = settings["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Read|Grep|Glob"
    command = entry["hooks"][0]["command"]
    assert "hook_access_guard.py" in command and "--project" in command
    assert "hook_dispatch" not in command
    assert "Read(**/.env*)" in settings["permissions"]["deny"]


# ── argv and run ────────────────────────────────────────────────────────────


def test_scout_argv():
    settings = {"permissions": {"deny": ["Read(**/.env*)"]}}
    cmd = delegate._claude_cmd("claude", "haiku", "SYS", settings=settings)
    assert "--restricted" in cmd and "--safe-mode" not in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert json.loads(cmd[cmd.index("--settings") + 1]) == settings
    assert "--strict-mcp-config" in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_scout_runs_in_the_project_and_leaves_it(monkeypatch, tmp_path):
    seen = {}

    class Proc:
        def __init__(self, argv, **kw):
            seen.update(argv=argv, cwd=kw.get("cwd"))
            self.returncode = 0

    monkeypatch.setattr(delegate, "_which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(delegate.subprocess, "Popen", Proc)
    monkeypatch.setattr(delegate, "_communicate_with_heartbeat",
                        lambda proc, timeout=0, idle_timeout=0, stdin_text=None: (
                            json.dumps({"result": "found it", "is_error": False, "num_turns": 3}), "", "ok"))
    text, ok, stats = delegate._run_claude("p", "s", "haiku", scout_project=str(tmp_path),
                                           settings={"permissions": {"deny": []}})
    assert (text, ok, stats["turns"]) == ("found it", True, 3)
    assert seen["cwd"] == str(tmp_path) and tmp_path.exists()
    assert "--restricted" in seen["argv"]


# ── Handler and routing ─────────────────────────────────────────────────────


def _fake_run(calls, text="checkout() in app/checkout.py:6"):
    def run(prompt, system_prompt, model="", **kw):
        calls.append(dict(prompt=prompt, system=system_prompt, model=model, **kw))
        return text, True, {"model": "claude-haiku-4-5", "turns": 4, "cost_usd": 0.01}
    return run


def test_handler_scout_mode(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(delegate, "_run_claude", _fake_run(calls))
    store = {}
    for _ in range(2):
        delegate.handle_delegate("Who calls charge_card?", "ask", "", "", _svc(tmp_path), _capture(store),
                                 backend="claude", scout=True)
    assert len(calls) == 2  # never cached
    call = calls[-1]
    assert call["scout_project"] == str(tmp_path)
    assert "Read(**/.env*)" in call["settings"]["permissions"]["deny"]
    assert "read-only scout" in call["system"]
    assert call["timeout"] == 240
    assert store["meta"]["mode"] == "scout" and store["status"] == "ok"


def test_handler_scout_refuses_on_corrupt_guard_config(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate.access_guard, "load_all", lambda p: ([], [], ["global"]))
    monkeypatch.setattr(delegate, "_run_claude", lambda *a, **k: pytest.fail("must not run"))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(tmp_path), _capture(store), backend="claude", scout=True)
    assert store["status"] == "blocked" and "unreadable" in store["resp"]


def test_scout_on_a_backend_that_cannot_read_is_an_error(monkeypatch, tmp_path):
    for name in ("gemini", "grok", "ollama"):
        store = {}
        delegate.handle_delegate("t", "ask", "", "", _svc(tmp_path), _capture(store), backend=name, scout=True)
        assert store["status"] == "error" and "scout is not available" in store["resp"], name


def test_host_scout_needs_claude_or_codex(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate, "host_backend", lambda svc: ("grok", "grok"))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(tmp_path), _capture(store), backend="host", scout=True)
    assert store["status"] == "error" and "host grok maps to grok" in store["resp"]


def test_auto_scout_skips_backends_that_cannot_read(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(delegate, "_codex_available", True, raising=False)
    monkeypatch.setattr(delegate, "_handle_codex_delegate",
                        lambda task, tt, ctx, fp, svc, dcfg, fin, **kw: (calls.append("codex"), fin(
                            "c3_delegate", {"backend": "codex"}, "ok", "ok"))[1])
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(tmp_path, codex_enabled=True), _capture(store),
                             backend="auto", scout=True)
    assert calls == ["codex"]


# ── The guard hook entry point ──────────────────────────────────────────────


def _hook(project, payload):
    proc = subprocess.run([sys.executable, str(REPO / "cli" / "hook_access_guard.py"), "--project", str(project)],
                          input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_guard_hook_denies_env_and_allows_source(tmp_path):
    (tmp_path / ".env").write_text("PAYMENT_API_KEY=x\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    out = _hook(tmp_path, {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / ".env")}})
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and ".env" in decision["permissionDecisionReason"]
    assert _hook(tmp_path, {"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "app.py")}}) == ""
    # a broad search prints nothing: the permission denies handle its hits
    assert _hook(tmp_path, {"tool_name": "Grep", "tool_input": {"pattern": "KEY"}}) == ""


def test_guard_hook_fails_closed_on_a_bad_payload(tmp_path):
    proc = subprocess.run([sys.executable, str(REPO / "cli" / "hook_access_guard.py"), "--project", str(tmp_path)],
                          input="[1, 2]", capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
