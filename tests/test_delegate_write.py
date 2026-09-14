"""D5 of the delegate remediation: write_paths, a Claude worker that edits inside a write set.

The caller names the files; ``claude -p --restricted`` makes the change with
Read/Grep/Glob/Edit/Write. Measured on Claude Code 2.1.270 before building
it (a throwaway project, ``--permission-mode dontAsk``, ``Edit(./a.py)`` and
``Edit(./sub/**)`` allowed): edits to a.py, sub/c.py and a new sub/new.py
landed; b.py, a new top.py and .env were refused, and ``permission_denials``
named each one. The guard hook adds Access Guard on the canonical path, the
write set again, .git/.c3/vault, agent locks and the pre-image snapshots C3
diffs afterwards. No real ``claude`` subprocess here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import hook_access_guard as hook
from cli.tools import delegate
from services import delegate_write as dw

REPO = Path(__file__).resolve().parents[1]


def _svc(tmp, **cfg):
    from core.config import DELEGATE_DEFAULTS
    return SimpleNamespace(project_path=str(tmp),
                           delegate_config={**DELEGATE_DEFAULTS, "enabled": True, "auto_compress": False, **cfg},
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
    monkeypatch.delenv("MCP_TOOL_TIMEOUT", raising=False)
    yield


# ── The write set ───────────────────────────────────────────────────────────


def test_parse_write_paths_normalizes_and_dedupes():
    globs, err = dw.parse_write_paths(" ./app/pricing.py, tests\\test_x.py\napp/pricing.py, app/**/*.py ")
    assert err == "" and globs == ["app/pricing.py", "tests/test_x.py", "app/**/*.py"]
    assert dw.parse_write_paths(["a.py", "b.py"]) == (["a.py", "b.py"], "")


@pytest.mark.parametrize("raw, fragment", [
    ("", "empty"), (" , ", "empty"),
    ("C:/x/app.py", "absolute"), ("/etc/passwd", "absolute"), ("~/x", "absolute"),
    ("app/../../x.py", "leaves the project"),
    (".git/config", ".git/"), (".c3/config.json", ".git/"), (".C3", ".git/"),
])
def test_parse_write_paths_refuses(raw, fragment):
    globs, err = dw.parse_write_paths(raw)
    assert globs == [] and fragment in err


def test_parse_write_paths_caps_entries():
    _, err = dw.parse_write_paths(",".join(f"f{i}.py" for i in range(dw.MAX_WRITE_PATHS + 1)))
    assert "max" in err


def test_in_write_set_is_rooted_and_casefolded():
    globs = ["app/pricing.py", "tests/*.py", "docs/**", "top.py"]
    assert dw.in_write_set("app/pricing.py", globs)
    assert dw.in_write_set("APP/Pricing.py", globs)
    assert dw.in_write_set("tests/test_a.py", globs)
    assert not dw.in_write_set("tests/unit/test_a.py", globs)  # * does not cross /
    assert dw.in_write_set("docs/a/b/c.md", globs)
    assert dw.in_write_set("top.py", globs)
    assert not dw.in_write_set("sub/top.py", globs)  # a bare name is the root file only
    assert not dw.in_write_set("", globs)


# ── Snapshots, changes, report ──────────────────────────────────────────────


def test_snapshot_keeps_the_first_pre_image_and_diffs(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    f = project / "a.py"
    f.write_bytes(b"x = 1\r\ny = 2\r\n")
    state = dw.new_state_dir(project, ["a.py", "new.py"])
    try:
        dw.snapshot(state, str(f), "canon-a")
        f.write_bytes(b"x = 1\r\ny = 3\r\n")
        dw.snapshot(state, str(f), "canon-a")  # second write: first pre-image stays
        f.write_bytes(b"x = 10\r\ny = 3\r\n")
        dw.snapshot(state, str(project / "new.py"), "canon-new")
        (project / "new.py").write_text("print('hi')\n", encoding="utf-8")
        changes = {c["rel"]: c for c in dw.collect_changes(state, project)}
    finally:
        import shutil
        shutil.rmtree(state, ignore_errors=True)
    a = changes["a.py"]
    assert a["change"] == "modified" and (a["added"], a["removed"]) == (2, 2)
    assert "-y = 2" in a["diff"] and "+x = 10" in a["diff"] and "\r" not in a["diff"]
    new = changes["new.py"]
    assert new["change"] == "created" and new["added"] == 1 and "--- /dev/null" in new["diff"]


def test_unchanged_binary_and_line_ending_changes(tmp_path):
    same, blob, crlf = tmp_path / "same.py", tmp_path / "blob.bin", tmp_path / "crlf.txt"
    same.write_text("a\n", encoding="utf-8")
    blob.write_bytes(b"\x00\x01")
    crlf.write_bytes(b"a\nb\n")
    state = dw.new_state_dir(tmp_path, ["*"])
    for i, p in enumerate((same, blob, crlf)):
        dw.snapshot(state, str(p), f"k{i}")
    blob.write_bytes(b"\x00\x02")
    crlf.write_bytes(b"a\r\nb\r\n")
    changes = {c["rel"]: c for c in dw.collect_changes(state, tmp_path)}
    assert "same.py" not in changes
    assert changes["blob.bin"]["binary"] is True
    assert changes["crlf.txt"]["note"] == "line endings or final newline only"


def test_render_lists_refusals_and_truncates_the_diff():
    changes = [{"rel": "a.py", "change": "modified", "added": 1, "removed": 0, "binary": False,
                "diff": "--- a/a.py\n+++ b/a.py\n" + "+x\n" * 400}]
    text = dw.render(changes, header="[delegate:write] H", report="did it",
                     refused=["Edit b.py"], max_diff_chars=200)
    assert text.startswith("[delegate:write] H\n  M a.py (+1 -0)")
    assert "Refused (1): Edit b.py" in text and "did it" in text
    assert "[diff truncated at 200 chars" in text


def test_denial_lines_dedupe_and_relativize(tmp_path):
    d = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "b.py")}}
    assert dw.denial_lines([d, d, "junk"], tmp_path) == ["Edit b.py"]


# ── The worker hook ─────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    (p / "app").mkdir(parents=True)
    (p / "app" / "pricing.py").write_text("x = 1\n", encoding="utf-8")
    (p / "app" / "checkout.py").write_text("y = 1\n", encoding="utf-8")
    (p / ".env").write_text("KEY=canary\n", encoding="utf-8")
    (p / "CLAUDE.md").write_text("# notes\n", encoding="utf-8")
    return p


def _edit(path):
    return {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b"}}


def _reason(out):
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", out
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_worker_hook_allows_the_write_set_and_snapshots(project):
    state = dw.new_state_dir(project, ["app/pricing.py", "app/new.py"])
    assert hook.worker_run(_edit(project / "app" / "pricing.py"), project, state) is None
    assert hook.worker_run({"tool_name": "Write", "tool_input": {"file_path": str(project / "app" / "new.py"),
                                                                 "content": "z"}}, project, state) is None
    markers = [json.loads(p.read_text(encoding="utf-8")) for p in (state / "pre").glob("*.json")]
    assert sorted(m["existed"] for m in markers) == [False, True]


@pytest.mark.parametrize("target, fragment", [
    ("app/checkout.py", "outside the write set"),
    (".git/config", "under .git/ or .c3/"),
    (".c3/config.json", "under .git/ or .c3/"),
    (".env", "Access Guard deny"),
    ("CLAUDE.md", "Access Guard confirm"),
])
def test_worker_hook_refuses(project, target, fragment, monkeypatch):
    filed = []
    monkeypatch.setattr(hook, "_confirm_request", lambda *a, **k: filed.append(a) or ("r1", ""))
    state = dw.new_state_dir(project, ["**"])
    if target == "app/checkout.py":
        state = dw.new_state_dir(project, ["app/pricing.py"])
    reason = _reason(hook.worker_run(_edit(project / target), project, state))
    assert fragment in reason and "[c3-delegate:refused]" in reason
    assert filed == []  # a worker never files an override request
    assert not list((state / "pre").glob("*.json"))


def test_worker_hook_refuses_outside_the_project(project, tmp_path):
    state = dw.new_state_dir(project, ["**"])
    assert "outside the project" in _reason(hook.worker_run(_edit(tmp_path / "elsewhere.py"), project, state))


def test_worker_hook_respects_another_agents_lock(project, monkeypatch):
    from services import agent_locks
    seen = {}

    def check(path, base, session_id):
        seen["session"] = session_id
        return {"agent_id": "cod"}

    monkeypatch.setattr(agent_locks, "check", check)
    state = dw.new_state_dir(project, ["app/pricing.py"], session_id="brain-1")
    assert "locked by cod" in _reason(hook.worker_run(_edit(project / "app" / "pricing.py"), project, state))
    assert seen["session"] == "brain-1"


def test_worker_hook_reads_go_through_the_guard(project):
    state = dw.new_state_dir(project, ["app/pricing.py"])
    out = hook.worker_run({"tool_name": "Read", "tool_input": {"file_path": str(project / ".env")}}, project, state)
    assert ".env" in _reason(out)
    assert hook.worker_run({"tool_name": "Read", "tool_input": {"file_path": str(project / "app" / "checkout.py")}},
                           project, state) is None


def _hook_proc(project, state, payload):
    return subprocess.run([sys.executable, str(REPO / "cli" / "hook_access_guard.py"), "--project", str(project),
                           "--worker-state", str(state)],
                          input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_worker_hook_entry_point(project):
    state = dw.new_state_dir(project, ["app/pricing.py"])
    ok = _hook_proc(project, state, _edit(project / "app" / "pricing.py"))
    assert ok.returncode == 0 and ok.stdout.strip() == "", ok.stderr
    denied = _hook_proc(project, state, _edit(project / "app" / "checkout.py"))
    assert json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_worker_hook_fails_closed_without_a_spec(project, tmp_path):
    proc = _hook_proc(project, tmp_path / "no-such-state", _edit(project / "app" / "pricing.py"))
    decision = json.loads(proc.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny" and "worker guard failed" in decision["permissionDecisionReason"]


# ── Settings and argv ───────────────────────────────────────────────────────


def test_worker_settings(project):
    state = dw.new_state_dir(project, ["app/pricing.py", "tests/**"])
    settings = delegate.worker_settings(project, ["app/pricing.py", "tests/**"], state)
    perms = settings["permissions"]
    assert perms["allow"] == ["Edit(./app/pricing.py)", "Edit(./tests/**)"]
    assert "Read(**/.env*)" in perms["deny"] and "Edit(**/.env*)" in perms["deny"]
    assert any(d.startswith("Edit(") and "claude.md" in d.casefold() for d in perms["deny"])
    (entry,) = settings["hooks"]["PreToolUse"]
    assert set(entry["matcher"].split("|")) >= {"Read", "Grep", "Glob", "Edit", "Write"}
    command = entry["hooks"][0]["command"]
    assert "hook_dispatch" not in command
    assert Path(command.split("--worker-state", 1)[1].strip().strip('"')) == state


def test_worker_edit_denies_fail_closed_on_corrupt_config(monkeypatch, tmp_path):
    monkeypatch.setattr(delegate.access_guard, "load_all", lambda p: ([], [], ["project"]))
    with pytest.raises(ValueError):
        delegate.worker_edit_denies(tmp_path)


def test_worker_argv():
    cmd = delegate._claude_cmd("claude", "sonnet", "SYS", settings={"permissions": {}}, tools=dw.WORKER_TOOLS)
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob,Edit,Write"
    assert "--restricted" in cmd and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "Bash" not in cmd[cmd.index("--tools") + 1]


def test_parse_claude_json_keeps_permission_denials():
    denial = {"tool_name": "Edit", "tool_input": {"file_path": "b.py"}}
    _, ok, stats = delegate.parse_claude_json(json.dumps(
        {"result": "done", "is_error": False, "permission_denials": [denial]}))
    assert ok and stats["denials"] == [denial]


def test_write_tier_defaults_to_medium():
    from core.config import DELEGATE_DEFAULTS
    assert DELEGATE_DEFAULTS["claude_write_default_tier"] == "medium"
    assert delegate.resolve_claude_tier("", "", dict(DELEGATE_DEFAULTS), write=True)[:2] == ("medium", "sonnet")
    assert delegate.resolve_claude_tier("small", "", dict(DELEGATE_DEFAULTS), write=True)[:2] == ("small", "haiku")


def test_worker_timeout_stays_inside_the_mcp_ceiling(monkeypatch):
    assert delegate._worker_timeout({"claude_write_timeout": 600}) == (600, "")
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "120000")
    seconds, note = delegate._worker_timeout({"claude_write_timeout": 600})
    assert seconds == 105 and "120s" in note
    assert delegate._worker_timeout({"claude_write_timeout": 60}) == (60, "")


# ── Handler and routing ─────────────────────────────────────────────────────


def _fake_worker(calls, *, writes=(), ok=True, text="Changed pricing.py.", denials=None):
    """A stand-in for _run_claude that behaves like a worker behind the hook."""
    def run(prompt, system_prompt, model="", **kw):
        calls.append(dict(prompt=prompt, system=system_prompt, model=model, **kw))
        state = Path(kw["settings"]["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
                     .split("--worker-state", 1)[1].strip().strip('"'))
        for path, content in writes:
            dw.snapshot(state, str(path), str(path).casefold())
            Path(path).write_text(content, encoding="utf-8")
        stats = {"model": "claude-sonnet-5", "turns": 5, "cost_usd": 0.05}
        if denials:
            stats["denials"] = denials
        return text, ok, stats
    return run


def test_handler_write_mode_reports_diff_ledger_and_telemetry(monkeypatch, project):
    calls, ledger = [], []
    target = project / "app" / "pricing.py"
    monkeypatch.setattr(delegate, "_run_claude", _fake_worker(
        calls, writes=[(target, "x = 2\n")],
        denials=[{"tool_name": "Edit", "tool_input": {"file_path": str(project / "app" / "checkout.py")}}]))
    import cli.tools.edit as edit_mod
    monkeypatch.setattr(edit_mod, "_log_to_ledger", lambda rel, summary, tags, svc, detail=None: (
        ledger.append((rel, summary, tags, detail)), "")[1])
    svc = _svc(project)
    svc.edit_ledger = object()
    store = {}
    for _ in range(2):
        delegate.handle_delegate("Set x to 2 in app/pricing.py", "ask", "", "", svc, _capture(store),
                                 backend="claude", write_paths="app/pricing.py")
    assert len(calls) == 2  # never cached
    call = calls[0]
    assert call["tools"] == dw.WORKER_TOOLS and call["scout_project"] == str(project)
    assert call["model"] == "sonnet" and call["timeout"] == 600
    assert "- app/pricing.py" in call["prompt"] and "Set x to 2" in call["prompt"]
    assert "worker making one code change" in call["system"]
    assert store["status"] == "ok" and store["meta"]["mode"] == "write"
    resp = store["resp"]
    # the second run finds pricing.py already at x = 2, so it changed nothing
    assert resp.startswith("[delegate:write] claude medium (sonnet) changed nothing")
    first = ledger[0]
    assert first[0] == "app/pricing.py" and "c3_delegate" in first[2] and first[3]["delegate"]["tier"] == "medium"
    detail = delegate.delegate_telemetry_detail(store["meta"], store["status"], requested_backend="claude",
                                                task_type="ask", host="claude-code", wall_ms=10)
    assert detail["mode"] == "write" and detail["denied"] == 1 and detail["files_changed"] == 0


def test_handler_write_mode_first_run_diff(monkeypatch, project):
    target = project / "app" / "pricing.py"
    monkeypatch.setattr(delegate, "_run_claude", _fake_worker([], writes=[(target, "x = 2\n")]))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="claude",
                             write_paths="app/pricing.py")
    resp = store["resp"]
    assert "changed 1 file(s)" in resp and "  M app/pricing.py (+1 -1)" in resp
    assert "-x = 1" in resp and "+x = 2" in resp and "--- worker report ---" in resp
    meta = store["meta"]
    assert (meta["files_changed"], meta["lines_added"], meta["lines_removed"]) == (1, 1, 1)


def test_handler_write_timeout_still_reports_what_changed(monkeypatch, project):
    target = project / "app" / "pricing.py"
    monkeypatch.setenv("MCP_TOOL_TIMEOUT", "120000")
    monkeypatch.setattr(delegate, "_run_claude", _fake_worker(
        [], writes=[(target, "x = 3\n")], ok=False, text="[claude:timeout] No response after 105s"))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="claude",
                             write_paths="app/pricing.py")
    resp = store["resp"]
    assert store["status"] == "error"
    assert resp.startswith("[delegate:write-failed] [claude:timeout]")
    assert "1 file(s) were changed before it stopped" in resp and "+x = 3" in resp
    assert "MCP client's 120s limit" in resp


def test_handler_write_state_dir_is_removed(monkeypatch, project):
    seen = []

    def run(prompt, system_prompt, model="", **kw):
        cmd = kw["settings"]["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        seen.append(Path(cmd.split("--worker-state", 1)[1].strip().strip('"')))
        return "nothing to do", True, {}

    monkeypatch.setattr(delegate, "_run_claude", run)
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture({}), backend="claude",
                             write_paths="app/pricing.py")
    assert seen and not seen[0].exists()


def test_handler_write_refuses_corrupt_guard_config(monkeypatch, project):
    monkeypatch.setattr(delegate.access_guard, "load_all", lambda p: ([], [], ["global"]))
    monkeypatch.setattr(delegate, "_run_claude", lambda *a, **k: pytest.fail("must not run"))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="claude",
                             write_paths="app/*.py")  # a glob skips the literal pre-check
    assert store["status"] == "blocked" and "write mode refused" in store["resp"]
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="claude",
                             write_paths="app/pricing.py")
    assert store["status"] == "blocked" and "<corrupt-config>" in store["resp"]


def test_guard_refused_literal_paths_are_dropped_and_named(monkeypatch, project):
    calls = []
    monkeypatch.setattr(delegate, "_run_claude", _fake_worker(calls))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="claude",
                             write_paths="app/pricing.py, .env, CLAUDE.md, app/*.py")
    prompt = calls[0]["prompt"]
    assert "- app/pricing.py" in prompt and "- app/*.py" in prompt
    assert "- .env" not in prompt and "- CLAUDE.md" not in prompt
    assert calls[0]["settings"]["permissions"]["allow"] == ["Edit(./app/pricing.py)", "Edit(./app/*.py)"]
    resp = store["resp"]
    assert "Not attempted, Access Guard refuses a delegate: .env (deny rule" in resp
    assert "CLAUDE.md (confirm rule" in resp


def test_all_write_paths_guard_refused_never_spawns(monkeypatch, project):
    monkeypatch.setattr(delegate, "_run_claude", lambda *a, **k: pytest.fail("must not run"))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="claude",
                             write_paths=".env")
    assert store["status"] == "blocked" and "refuses every write path" in store["resp"]


def test_bad_write_paths_never_spawn(monkeypatch, project):
    monkeypatch.setattr(delegate, "_run_claude", lambda *a, **k: pytest.fail("must not run"))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="claude",
                             write_paths="../outside.py")
    assert store["status"] == "error" and "leaves the project" in store["resp"]


@pytest.mark.parametrize("backend", ["gemini", "codex", "grok", "ollama", "auto"])
def test_write_mode_is_claude_only(monkeypatch, project, backend):
    monkeypatch.setattr(delegate, "_run_claude", lambda *a, **k: pytest.fail("must not run"))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend=backend,
                             write_paths="app/pricing.py")
    assert store["status"] == "error" and "claude backend only" in store["resp"]


def test_host_write_needs_a_claude_host(monkeypatch, project):
    monkeypatch.setattr(delegate, "host_backend", lambda svc: ("codex", "codex"))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="host",
                             write_paths="app/pricing.py")
    assert store["status"] == "error" and "host codex maps to codex" in store["resp"]


def test_host_write_routes_to_claude_medium(monkeypatch, project):
    calls = []
    monkeypatch.setattr(delegate, "host_backend", lambda svc: ("claude-code", "claude"))
    monkeypatch.setattr(delegate, "_run_claude", _fake_worker(calls))
    store = {}
    delegate.handle_delegate("t", "ask", "", "", _svc(project), _capture(store), backend="host",
                             write_paths="app/pricing.py")
    assert calls[0]["model"] == "sonnet" and store["meta"]["tier"] == "medium"


def test_write_paths_do_not_apply_to_probes(monkeypatch, project):
    for task_type in ("ping", "available"):
        store = {}
        delegate.handle_delegate("t", task_type, "", "", _svc(project), _capture(store), backend="claude",
                                 write_paths="app/pricing.py")
        assert store["status"] == "error" and "does not apply" in store["resp"], task_type


def test_mcp_tool_exposes_write_paths():
    import inspect

    from cli import mcp_server
    fn = getattr(mcp_server.c3_delegate, "fn", mcp_server.c3_delegate)
    assert "write_paths" in inspect.signature(fn).parameters
    assert "write_paths" in (fn.__doc__ or "")
