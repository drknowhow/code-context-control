"""Grok Build as a C3 host: profile, install/uninstall, trust, guard, doctor.

Isolated installations only: HOME, USERPROFILE and GROK_HOME point into
tmp_path, and every assertion that C3 leaves Grok's own user config alone
compares bytes before and after.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
import tomlkit

from core.ide import PROFILES, detect_ide, get_profile, normalize_ide_name
from services import grok_integration
from services.grok_integration import HOOK_EVENTS, hook_command, hook_state, install_hooks, remove_hooks, trust_state


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    user_config = home / ".grok" / "config.toml"
    user_config.write_text('[compat.claude]\nhooks = false\nmcps = false\n', encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GROK_HOME", str(home / ".grok"))
    monkeypatch.delenv("GROK_FOLDER_TRUST", raising=False)
    return home


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_profile_shape_and_aliases():
    profile = PROFILES["grok"]
    assert profile.display_name == "Grok Build"
    assert (profile.config_path, profile.config_key, profile.config_format) == (".grok/config.toml", "mcp_servers", "toml")
    assert profile.instructions_file == "AGENTS.md"
    assert profile.supports_hooks and not profile.config_path_global
    assert profile.settings_path == ".grok/hooks/c3.json"
    assert normalize_ide_name("grok-build") == "grok"
    assert get_profile("GROK").name == "grok"


def test_detects_grok_markers(tmp_path):
    (tmp_path / ".grok").mkdir()
    assert detect_ide(str(tmp_path)) == "grok"
    (tmp_path / ".claude").mkdir()
    assert detect_ide(str(tmp_path)) == "grok"  # an explicit .grok beats the Claude fallback
    (tmp_path / ".grok" / "config.toml").write_text("", encoding="utf-8")
    assert detect_ide(str(tmp_path)) == "grok"


def test_hooks_file_is_owned_and_complete(tmp_path):
    state = install_hooks(tmp_path, sys.executable, Path("cli/hook_dispatch.py").resolve())
    data = json.loads((tmp_path / ".grok/hooks/c3.json").read_text(encoding="utf-8"))
    assert set(data) == {"hooks"}
    assert set(data["hooks"]) == set(HOOK_EVENTS)
    assert "UserPromptSubmit" not in data["hooks"]  # Grok discards allowing prompt-hook context
    for event, groups in data["hooks"].items():
        assert len(groups) == 1 and groups[0]["matcher"] == "*"
        handler = groups[0]["hooks"][0]
        assert set(handler) == {"type", "command", "timeout"}
        assert handler["type"] == "command"
    assert state["installed"] is True
    assert hook_state(tmp_path)["installed"] is True
    # Re-install replaces rather than appends.
    install_hooks(tmp_path, sys.executable, Path("cli/hook_dispatch.py").resolve())
    again = json.loads((tmp_path / ".grok/hooks/c3.json").read_text(encoding="utf-8"))
    assert all(len(groups) == 1 for groups in again["hooks"].values())
    assert remove_hooks(tmp_path) is True
    assert not (tmp_path / ".grok/hooks").exists()
    assert remove_hooks(tmp_path) is False


def test_hook_command_forms(tmp_path):
    project = tmp_path / "space & %X% ' literal"
    windows = hook_command("C:/py/python.exe", Path("C:/c3/cli/hook_dispatch.py"), "pretool", project, windows=True)
    assert windows.startswith("powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ")
    assert not any(ch in windows for ch in "\"'$%&")
    script = base64.b64decode(windows.rsplit(" ", 1)[1]).decode("utf-16-le")
    assert "'pretool' '--host' 'grok' '--project'" in script
    assert "' literal" not in script or "'' literal" in script  # single quotes doubled
    posix = hook_command("/usr/bin/python3", PurePosixPath("/c3/cli/hook_dispatch.py"), "stop",
                         PurePosixPath("/p q"), windows=False)
    assert posix == "/usr/bin/python3 /c3/cli/hook_dispatch.py stop --host grok --project '/p q'"
    assert grok_integration._is_c3_command(windows) and grok_integration._is_c3_command(posix)


@pytest.mark.skipif(os.name != "nt", reason="Windows hook command execution")
def test_windows_hook_command_preserves_paths_stdin_and_exit(tmp_path):
    project = tmp_path / "space & %C3_TEST_EXPANSION% ' literal"
    project.mkdir()
    script = project / "hook_dispatch.py"
    script.write_text("import json, sys\nprint(json.dumps({'args': sys.argv[1:], 'input': sys.stdin.read()}))\nsys.exit(2)\n")
    command = hook_command(sys.executable, script, "pretool", project, windows=True)
    result = subprocess.run('cmd.exe /d /s /c "' + command + '"', input='{"sessionId":"synthetic"}',
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 2, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == ["pretool", "--host", "grok", "--project", str(project)]
    assert data["input"] == '{"sessionId":"synthetic"}'


def test_trust_state_reads_the_store_without_writing(tmp_path, isolated_home):
    project = tmp_path / "repo" / "sub"
    project.mkdir(parents=True)
    store = isolated_home / ".grok" / "trusted_folders.toml"
    assert trust_state(project)["trusted"] is False
    store.write_text(f"[folders.'{tmp_path / 'repo'}']\ntrusted = true\ndecided_at = 1\n", encoding="utf-8")
    before = _digest(store)
    assert trust_state(project)["trusted"] is True
    assert trust_state(tmp_path / "elsewhere")["trusted"] is False
    store.write_text(f"[folders.'{tmp_path / 'repo'}']\ntrusted = false\n", encoding="utf-8")
    assert trust_state(project)["trusted"] is False
    store.write_text(f"[folders.'{tmp_path / 'repo'}']\ntrusted = true\ndecided_at = 1\n", encoding="utf-8")
    assert _digest(store) == before


def test_install_and_uninstall_leave_grok_user_config_alone(tmp_path, monkeypatch, isolated_home):
    from cli.c3 import _uninstall_mcp_all, cmd_install_mcp
    project = tmp_path / "project with spaces"
    project.mkdir()
    monkeypatch.setattr("shutil.which", lambda name: None)
    user_config = isolated_home / ".grok" / "config.toml"
    before = _digest(user_config)
    (project / ".grok").mkdir()
    (project / ".grok" / "config.toml").write_text('# team servers\n[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")

    for _ in range(2):
        cmd_install_mcp(SimpleNamespace(project_path=str(project), ide="grok", mcp_mode="direct"))

    config = tomlkit.parse((project / ".grok/config.toml").read_text(encoding="utf-8"))
    c3 = config["mcp_servers"]["c3"]
    assert c3["args"][-2:] == ["--host", "grok"]
    assert c3["startup_timeout_sec"] == 60 and c3["enabled"] is True
    assert config["mcp_servers"]["other"]["command"] == "other"
    assert json.loads((project / ".grok/hooks/c3.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert agents.count("## Grok Build") == 1 and "c3__c3_search" in agents
    assert json.loads((project / ".c3/config.json").read_text(encoding="utf-8"))["installed_ides"] == ["grok"]
    assert not (isolated_home / ".grok" / "trusted_folders.toml").exists()
    assert _digest(user_config) == before

    _uninstall_mcp_all(str(project), include_global=False)
    remaining = tomlkit.parse((project / ".grok/config.toml").read_text(encoding="utf-8"))
    assert "c3" not in remaining["mcp_servers"] and "other" in remaining["mcp_servers"]
    assert not (project / ".grok/hooks/c3.json").exists()
    assert _digest(user_config) == before


def test_uninstall_deletes_config_that_only_held_c3(tmp_path, monkeypatch, isolated_home):
    from cli.c3 import _uninstall_mcp_all, cmd_install_mcp
    project = tmp_path / "p"
    project.mkdir()
    monkeypatch.setattr("shutil.which", lambda name: None)
    cmd_install_mcp(SimpleNamespace(project_path=str(project), ide="grok", mcp_mode="direct"))
    _uninstall_mcp_all(str(project), include_global=False)
    assert not (project / ".grok").exists()


def test_agents_md_block_carries_both_hosts():
    from cli.c3 import _AGENTS_MD_CONTENT
    from services.agents_workflow import AGENTS_MD_WORKFLOW
    assert _AGENTS_MD_CONTENT == AGENTS_MD_WORKFLOW
    assert AGENTS_MD_WORKFLOW.count("# C3 — agent workflow") == 1
    assert "## Codex" in AGENTS_MD_WORKFLOW and "## Grok Build" in AGENTS_MD_WORKFLOW
    assert "grok --trust" in AGENTS_MD_WORKFLOW
    assert len(AGENTS_MD_WORKFLOW.encode()) < 6144


def test_claude_md_manager_uses_shared_block_for_grok():
    from services.agents_workflow import AGENTS_MD_WORKFLOW
    from services.claude_md import ClaudeMdManager
    profile = PROFILES["grok"]
    manager = ClaudeMdManager(".", None, None, None, instructions_file=profile.instructions_file,
                              supports_hooks=profile.supports_hooks)
    assert manager._build_c3_workflow() == AGENTS_MD_WORKFLOW


def test_parser_accepts_grok():
    from cli.c3 import __version__, _parse_cli_ide_arg
    from cli.commands.parser import build_parser
    parser = build_parser(__version__, _parse_cli_ide_arg)
    assert parser.parse_args(["install-mcp", "--ide", "grok"]).ide == "grok"
    assert parser.parse_args(["install-mcp", "--ide", "grok-build"]).ide == "grok"
    assert parser.parse_args(["doctor", "--ide", "grok"]).ide == "grok"


def test_doctor_reports_without_exposing_env(tmp_path, monkeypatch, isolated_home):
    monkeypatch.setattr("services.grok_integration.probe_cli", lambda: {"available": False})
    path = tmp_path / ".grok/config.toml"
    path.parent.mkdir()
    path.write_text('[mcp_servers.c3]\ncommand = "c3-mcp"\nargs = ["--host", "grok"]\n'
                    '[mcp_servers.c3.env]\nPRIVATE_TEST = "never-report-me"\n', encoding="utf-8")
    result = grok_integration.diagnose(tmp_path)
    assert result["mcp"]["configured"] is True and result["mcp"]["explicit_host"] is True
    assert result["trust"]["trusted"] is False
    assert "grok --trust" in result["activation"]
    assert "never-report-me" not in json.dumps(result)


def test_probe_warns_on_major_version_drift(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "grok")
    monkeypatch.setattr("cli.tools.delegate._probe_cli_version", lambda exe, timeout=10: ("grok 2.0.1 (abc)", "", 0))
    assert "warning" in grok_integration.probe_cli()
    monkeypatch.setattr("cli.tools.delegate._probe_cli_version", lambda exe, timeout=10: ("grok 1.4.0 (abc)", "", 0))
    assert "warning" not in grok_integration.probe_cli()


@pytest.mark.parametrize("rel, kind", [
    (".grok/hooks/c3.json", "read_only"),
    (".grok/hooks/team.json", "read_only"),
    (".grok/config.toml", "confirm"),
    (".grok/skills/x/SKILL.md", "confirm"),
    (".grok/rules/style.md", "confirm"),
])
def test_grok_agent_config_is_guarded(tmp_path, rel, kind):
    from services import access_guard
    from services.artifact_defs import classify_path
    (tmp_path / ".c3").mkdir()
    denial = access_guard.check(str(tmp_path / rel), "write", str(tmp_path))
    assert denial is not None and denial.kind == kind
    assert access_guard.check(str(tmp_path / rel), "read", str(tmp_path)) is None
    if rel in (".grok/hooks/c3.json", ".grok/config.toml"):
        assert classify_path(rel).provider == "grok"
