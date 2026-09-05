"""Native host boundary regressions; synthetic data and isolated installations."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import tomlkit

from core.codex_patch import patch_targets
from core.host import resolve_host
from core.mcp_toml import merge_toml_section
from services.codex_integration import CODEX_WORKFLOW, install_hooks
from services.codex_transcript import read_rollout


@pytest.mark.parametrize("order", [("claude", "codex"), ("codex", "claude")])
def test_install_coexists_and_preserves_settings(tmp_path, monkeypatch, order):
    from cli.c3 import cmd_install_mcp
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project with spaces"
    project.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr("shutil.which", lambda name: None)
    for host in (*order, *order):
        cmd_install_mcp(SimpleNamespace(project_path=str(project), ide=host, mcp_mode="direct"))
    assert "c3" in json.loads((project / ".mcp.json").read_text())["mcpServers"]
    codex = tomlkit.parse((project / ".codex/config.toml").read_text())["mcp_servers"]["c3"]
    assert "--host" in codex["args"]
    assert codex["startup_timeout_sec"] == 30
    assert not (home / ".codex/config.toml").exists()
    hooks = json.loads((project / ".codex/hooks.json").read_text())["hooks"]
    assert all(len(groups) == 1 for groups in hooks.values())
    assert json.loads((project / ".claude/settings.local.json").read_text())["hooks"]["PreToolUse"]
    assert json.loads((project / ".c3/config.json").read_text())["installed_ides"] == ["claude-code", "codex"]


def test_merge_keeps_user_toml_and_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('''# user settings
[mcp_servers.c3]
command = "old" # launcher
args = ["old"]
enabled = false
startup_timeout_sec = 90
disabled_tools = ["c3_shell"]
[mcp_servers.c3.env]
SAFE_TEST_VALUE = 'C:\\literal\\value'
[other]
anything = 42
''')
    merge_toml_section(path, "mcp_servers.c3", {"command": 'C:\\a "quoted"\\b.exe', "args": ["--host", "codex"]},
                       {"enabled": True, "startup_timeout_sec": 30})
    text = path.read_text()
    data = tomlkit.parse(text)
    server = data["mcp_servers"]["c3"]
    assert server["enabled"] is False
    assert server["startup_timeout_sec"] == 90
    assert server["disabled_tools"] == ["c3_shell"]
    assert server["env"]["SAFE_TEST_VALUE"] == 'C:\\literal\\value'
    assert server["command"] == 'C:\\a "quoted"\\b.exe'
    assert "# user settings" in text and "# launcher" in text
    assert data["other"]["anything"] == 42


def test_invalid_toml_does_not_get_overwritten(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[broken\n")
    with pytest.raises(Exception):
        merge_toml_section(path, "mcp_servers.c3", {"command": "new"})
    assert path.read_text() == "[broken\n"


def test_hooks_preserve_other_handlers_and_do_not_claim_trust(tmp_path):
    path = tmp_path / ".codex/hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "user-hook"}]}]}}))
    for _ in range(2):
        state = install_hooks(tmp_path, sys.executable, Path("cli/hook_dispatch.py").resolve())
    assert state["active"] == "unknown"
    handlers = json.loads(path.read_text())["hooks"]["PreToolUse"]
    assert len(handlers) == 2
    assert handlers[0]["hooks"][0]["command"] == "user-hook"
    assert "-EncodedCommand" in handlers[1]["hooks"][0]["commandWindows"]
    assert "bypass" not in path.read_text()
    assert len(CODEX_WORKFLOW.encode()) < 4096


def test_host_identity_is_explicit_and_provider_scoped():
    env = {"CODEX_THREAD_ID": "codex-parent", "CLAUDE_CODE_SESSION_ID": "claude-child"}
    assert resolve_host(environ=env).provider == "claude-code"
    assert resolve_host(explicit_host="claude-code", environ=env).host_session_id == "claude-child"
    assert resolve_host(explicit_host="codex", environ=env).host_session_id == "codex-parent"
    assert resolve_host(explicit_host="codex", environ={}).host_session_id == ""


PATCH = """*** Begin Patch
*** Add File: new.py
+print('hello')
*** Update File: old.py
*** Move to: moved.py
@@
-old
+new
*** Delete File: gone.py
*** End Patch"""


def test_patch_tracks_source_and_destination(tmp_path):
    assert {(Path(t.path).name, t.change_type) for t in patch_targets(PATCH, tmp_path)} == {
        ("new.py", "created"), ("old.py", "deleted"), ("moved.py", "created"), ("gone.py", "deleted")}


@pytest.mark.parametrize("command", [None, "echo hi", "*** Begin Patch\n*** End Patch",
    "*** Begin Patch\n*** Move to: x\n*** End Patch",
    "*** Begin Patch\n*** Add File: x\n*** Unknown: y\n*** End Patch"])
def test_bad_patch_denies(tmp_path, command):
    from cli.hook_dispatch import dispatch
    result = dispatch("pretool", {"turn_id": "t", "tool_name": "apply_patch",
        "tool_input": {"command": command}}, tmp_path)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_patch_grant_not_spent_if_any_target_denied(tmp_path, monkeypatch):
    import cli.hook_dispatch as dispatcher
    consumed, checked = [], []
    def guard(payload, project, defer_consume=False):
        path = Path(payload["tool_input"]["file_path"]).name
        checked.append(path)
        if path == "moved.py":
            return {"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "held"}}
        return {"_on_allow": lambda: consumed.append(path) or "granted"}
    monkeypatch.setattr(dispatcher, "_RUN_CACHE", {
        "hook_access_guard": (guard, ""), "hook_pretool_enforce": (lambda p, pp: None, "")})
    result = dispatcher.dispatch("pretool", {"turn_id": "t", "tool_name": "apply_patch",
        "tool_input": {"command": PATCH}}, tmp_path)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert checked == ["new.py", "old.py", "moved.py"]
    assert consumed == []


def test_patch_guard_import_failure_denies(tmp_path, monkeypatch):
    import cli.hook_dispatch as dispatcher
    monkeypatch.setattr(dispatcher, "_RUN_CACHE", {"hook_access_guard": (None, "broken")})
    out = dispatcher.dispatch("pretool", {"turn_id": "t", "tool_name": "apply_patch",
        "tool_input": {"command": PATCH}}, tmp_path)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_patch_posts_every_target_to_ledger(tmp_path):
    from cli.hook_dispatch import dispatch
    (tmp_path / ".c3").mkdir()
    with patch("cli.hook_edit_ledger.load_hybrid_config", return_value={"edit_ledger": {"tracking_level": "minimal"}}):
        dispatch("posttool", {"turn_id": "t", "session_id": "codex-thread", "tool_name": "apply_patch",
            "tool_input": {"command": PATCH}, "tool_response": "Success. Updated files"}, tmp_path)
    entries = [json.loads(line) for line in (tmp_path / ".c3/edit_ledger.jsonl").read_text().splitlines()]
    assert {e["file"]: e["change_type"] for e in entries} == {
        "new.py": "created", "old.py": "deleted", "moved.py": "created", "gone.py": "deleted"}
    assert {e["session_id"] for e in entries} == {"codex-thread"}


def rollout(path, project, thread="thread-a"):
    records = [
        {"type": "session_meta", "payload": {"id": thread, "cwd": str(project)}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "private reasoning"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "answer"}},
        *[{"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": n, "output_tokens": n // 2}}}} for n in (100, 200, 200)],
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n{partial", encoding="utf-8")


def test_rollout_deduplicates_wire_views_and_cumulative_usage(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rollout(path, tmp_path)
    data = read_rollout(path, tmp_path, "thread-a")
    assert [t["text"] for t in data["turns"]] == ["hello", "answer"]
    assert data["usage"] == {"input_tokens": 200, "output_tokens": 100}
    assert data["usage_available"] is True
    assert read_rollout(path, tmp_path / "other") is None
    assert read_rollout(path, tmp_path, "other-thread") is None


def test_codex_sync_is_incremental_and_project_scoped(tmp_path, monkeypatch):
    from services.conversation_store import ConversationStore
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    project = tmp_path / "project"
    project.mkdir()
    rollout(home / "sessions/2026/rollout-a.jsonl", project)
    rollout(home / "sessions/2026/rollout-b.jsonl", tmp_path / "other", "thread-b")
    store = ConversationStore(str(project))
    assert store.sync("codex")["synced"] == 1
    assert store.sync("codex")["synced"] == 0
    assert store.sync("codex", force=True)["synced"] == 1
    assert len(store.list_sessions()) == 1
    assert store.list_sessions()[0]["source"] == "codex"


def test_codex_usage_unavailable_is_not_zero(tmp_path):
    from cli.hook_session_stats import run
    (tmp_path / ".c3").mkdir()
    run({"turn_id": "t", "session_id": "s"}, tmp_path)
    data = json.loads((tmp_path / ".c3/session_stats.jsonl").read_text())
    assert data["usage_available"] is False
    assert data["input_tokens"] is None and data["usage"] is None


def test_delegate_args_validate_without_model_call():
    import shutil

    from cli.tools.delegate import _codex_cmd
    from services.win_subprocess import harden_win_argv
    if not shutil.which("codex"):
        pytest.skip("Codex CLI unavailable")
    cmd = _codex_cmd("-", "", "read-only", "high")
    assert "--full-auto" not in cmd
    result = subprocess.run(harden_win_argv(cmd[:-1] + ["--help"]), capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_delegate_json_result_and_bound_resume(tmp_path, monkeypatch):
    from cli.tools import delegate
    thread = str(uuid.uuid4())
    events = [{"type": "thread.started", "thread_id": thread},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "answer"}},
              {"type": "turn.completed"}]
    assert delegate._codex_result("\n".join(map(json.dumps, events))) == ("answer", thread, True)
    assert delegate._codex_result('{"type":"turn.failed","error":"bad"}')[2] is False
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert delegate._run_codex_resume("next", cwd=str(tmp_path), origin_id="caller")[1] is False
    path, project, origin = delegate._delegate_binding(tmp_path, "caller")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"project": project, "origin": origin, "thread_id": thread}))
    calls = []
    monkeypatch.setattr(delegate, "_execute_codex", lambda cmd, *a, **kw: calls.append(cmd) or ("ok", True))
    assert delegate._run_codex_resume("next", cwd=str(tmp_path), origin_id="caller") == ("ok", True)
    assert thread in calls[0] and "--last" not in calls[0]
    assert delegate._run_codex_resume("next", cwd=str(tmp_path), origin_id="other")[1] is False


def test_child_environment_does_not_impersonate_parent(monkeypatch):
    from cli.tools.delegate import _child_host_env
    monkeypatch.setenv("CODEX_THREAD_ID", "parent")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "other-parent")
    env = _child_host_env("claude-code")
    assert "CODEX_THREAD_ID" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["C3_HOST"] == "claude-code"


def test_failed_patch_is_not_logged(tmp_path):
    from cli.hook_dispatch import dispatch
    (tmp_path / ".c3").mkdir()
    dispatch("posttool", {"turn_id": "t", "tool_name": "apply_patch",
        "tool_input": {"command": PATCH}, "tool_response": "Failed to find expected lines"}, tmp_path)
    assert not (tmp_path / ".c3/edit_ledger.jsonl").exists()


def test_bad_pretool_payload_fails_closed(tmp_path):
    result = subprocess.run([sys.executable, "cli/hook_dispatch.py", "pretool", "--host", "codex",
        "--project", str(tmp_path)], input="[]", capture_output=True, text=True, timeout=10)
    assert result.returncode == 2


def test_handoff_is_thread_bound_and_preserves_newer_checkpoint(tmp_path):
    from cli.hook_codex_lifecycle import checkpoint, run
    from services.codex_integration import hook_state
    (tmp_path / ".c3/sessions").mkdir(parents=True)
    install_hooks(tmp_path, sys.executable, Path("cli/hook_dispatch.py").resolve())
    saved = tmp_path / ".c3/sessions/session_old.json"
    data = {"host_session_id": "thread", "source_ide": "codex", "description": "old"}
    saved.write_text(json.dumps(data))
    os.utime(saved, (1, 1))
    checkpoint(tmp_path, {**data, "description": "new"})
    run({"session_id": "thread", "hook_event_name": "PreCompact"}, tmp_path)
    assert "new" in run({"session_id": "thread", "hook_event_name": "SessionStart"}, tmp_path)["additionalContext"]
    assert run({"session_id": "other", "hook_event_name": "SessionStart"}, tmp_path) is None
    assert hook_state(tmp_path, "thread")["active"] == "observed_this_session"
    hooks = tmp_path / ".codex/hooks.json"
    hooks.write_text(hooks.read_text() + "\n")
    assert hook_state(tmp_path, "thread")["active"] == "unknown"


def test_codex_cached_input_is_not_counted_twice(tmp_path):
    from cli.hook_session_stats import run
    from services.telemetry import aggregate_session_stats
    (tmp_path / ".c3").mkdir()
    path = tmp_path / "rollout.jsonl"
    rollout(path, tmp_path)
    with path.open("a") as stream:
        stream.write('\n' + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 200, "cached_input_tokens": 80, "output_tokens": 100}}}}))
    for _ in range(2):
        run({"turn_id": "t", "session_id": "thread-a", "transcript_path": str(path)}, tmp_path)
    run({"turn_id": "t", "session_id": "unknown"}, tmp_path)
    report = aggregate_session_stats(tmp_path)
    assert report["total_tokens"] == 300
    assert report["totals"]["input_tokens"] == 120
    assert report["unavailable_sessions"] == 1
    assert report["all_zero_rows"] == 0


def test_workflow_refresh_preserves_user_sections(tmp_path):
    from cli.c3 import _ensure_codex_agents_workflow
    path = tmp_path / "AGENTS.md"
    path.write_text("User instructions before.\n")
    _ensure_codex_agents_workflow(path)
    with path.open("a") as stream:
        stream.write("\nUser instructions after.\n")
    _ensure_codex_agents_workflow(path)
    text = path.read_text(encoding="utf-8")
    assert text.count("<!-- C3:BEGIN") == 1
    assert text.count("# C3 — Codex workflow") == 1
    assert "User instructions before." in text and "User instructions after." in text


def test_doctor_never_exposes_environment_values(tmp_path, monkeypatch):
    from services.codex_integration import diagnose
    monkeypatch.setattr("services.codex_integration.probe_cli", lambda: {"available": False})
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('[mcp_servers.c3]\ncommand="c3-mcp"\n[mcp_servers.c3.env]\nPRIVATE_TEST="never-report-me"\n')
    result = diagnose(tmp_path)
    assert result["mcp"]["configured"] is True
    assert "never-report-me" not in json.dumps(result)


@pytest.mark.parametrize("name", [".codex/hooks.json", "AGENTS.override.md"])
def test_codex_agent_config_is_guarded_and_audited(tmp_path, name):
    from cli.hook_dispatch import dispatch
    from services import access_guard
    from services.artifact_defs import classify_path
    (tmp_path / ".c3").mkdir()
    denial = access_guard.check(tmp_path / name, "write", str(tmp_path))
    assert denial is not None and denial.kind == "confirm"
    assert classify_path(name).provider == "codex"
    command = f"*** Begin Patch\n*** Add File: {name}\n+content\n*** End Patch"
    dispatch("posttool", {"turn_id": "t", "session_id": "thread", "tool_name": "apply_patch",
        "tool_input": {"command": command}, "tool_response": "Success. Updated files"}, tmp_path)
    records = [json.loads(line) for line in (tmp_path / ".c3/agent_artifacts/pending.jsonl").read_text().splitlines()]
    assert records[-1]["session_id"] == "thread"


def test_session_and_grant_identity_use_supplied_host_context(tmp_path):
    from cli.tools._grants import session_id
    from core.host import HostContext
    from services.session_manager import SessionManager
    manager = SessionManager(str(tmp_path))
    manager.host_context = HostContext("codex", "thread-bound")
    first = manager.start_session()["session_id"]
    second = manager.start_session()["session_id"]
    assert first != second
    assert session_id(SimpleNamespace(session_mgr=manager)) == "thread-bound"


def test_explicit_global_fallback_only_updates_codex_home(tmp_path, monkeypatch):
    from cli.c3 import _ensure_global_session_fallbacks
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    codex = tmp_path / "custom-codex"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    ag = home / ".gemini/antigravity/mcp_config.json"
    ag.parent.mkdir(parents=True)
    ag.write_text('{"user": true}')
    _ensure_global_session_fallbacks("server.py", primary_profile="codex")
    data = tomlkit.parse((codex / "config.toml").read_text())
    assert data["mcp_servers"]["c3"]["args"] == ["server.py", "--host", "codex"]
    assert ag.read_text() == '{"user": true}'
    assert not (home / ".codex").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows hook command execution")
def test_windows_hook_command_preserves_paths_stdin_and_exit(tmp_path):
    project = tmp_path / "space & %C3_TEST_EXPANSION% ' literal"
    project.mkdir()
    script = project / "hook_dispatch.py"
    script.write_text("import json, sys\nprint(json.dumps({'args': sys.argv[1:], 'input': sys.stdin.read()}))\nsys.exit(2)\n")
    install_hooks(project, sys.executable, script)
    handler = json.loads((project / ".codex/hooks.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    # cmd.exe -> powershell.exe -> python: a cold PowerShell start on a busy
    # CI runner alone can pass 10 s, so the bound covers the runner, not the hook.
    result = subprocess.run('cmd.exe /d /s /c "' + handler["commandWindows"] + '"',
        input='{"session_id":"synthetic"}', capture_output=True, text=True, timeout=120)
    assert result.returncode == 2, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == ["pretool", "--host", "codex", "--project", str(project)]
    assert data["input"] == '{"session_id":"synthetic"}'
