"""D4 of the delegate remediation: the surfaces that make downshifting the obvious move.

Measured 2026-09-14: 963 of 991 native Agent calls left `model` unset (92% of
subagent turns on the parent's Opus/Fable-class model), and the instruction
block described c3_delegate as "offload to other models" with Ollama first.
These tests pin:

- install-mcp writes c3-scout (haiku, read-only tools) and c3-worker (sonnet)
  into .claude/agents, rewrites only files carrying C3's marker, and skips
  with --no-agents or on other IDEs;
- the managed CLAUDE.md block and the global template teach backend=host,
  tier, scout and the two subagents;
- c3_status's budget view reports the last 7 days of delegation;
- no nudge names a task type c3_delegate does not have.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from services import delegate_agents
from services.telemetry import append_telemetry_record


def _frontmatter(text: str) -> dict:
    block = text.split("---", 2)[1]
    return dict(line.split(": ", 1) for line in block.strip().splitlines())


def test_agent_definitions_downshift_and_scope_tools():
    scout = _frontmatter(delegate_agents.AGENTS["c3-scout"])
    worker = _frontmatter(delegate_agents.AGENTS["c3-worker"])
    assert scout["name"] == "c3-scout" and scout["model"] == "haiku"
    assert worker["name"] == "c3-worker" and worker["model"] == "sonnet"
    scout_tools = {t.strip() for t in scout["tools"].split(",")}
    assert {"Read", "Grep", "Glob", "mcp__c3__c3_read", "mcp__c3__c3_search"} <= scout_tools
    assert not scout_tools & {"Edit", "Write", "Bash", "MultiEdit", "NotebookEdit", "mcp__c3__c3_edit",
                              "mcp__c3__c3_shell"}
    assert "tools" not in worker  # inherits the session's tools and hooks
    for text in delegate_agents.AGENTS.values():
        assert delegate_agents.MARKER in text


def test_install_writes_keeps_updates_and_never_touches_user_files(tmp_path):
    (tmp_path / ".c3").mkdir()
    first = dict((Path(p).name, a) for p, a in delegate_agents.install_delegate_agents(tmp_path))
    assert first == {"c3-scout.md": "wrote", "c3-worker.md": "wrote"}
    again = dict((Path(p).name, a) for p, a in delegate_agents.install_delegate_agents(tmp_path))
    assert again == {"c3-scout.md": "kept", "c3-worker.md": "kept"}

    scout = tmp_path / ".claude" / "agents" / "c3-scout.md"
    scout.write_text(scout.read_text(encoding="utf-8") + "\nold C3 text\n", encoding="utf-8")
    worker = tmp_path / ".claude" / "agents" / "c3-worker.md"
    worker.write_text("---\nname: c3-worker\nmodel: opus\n---\nmine\n", encoding="utf-8")
    third = dict((Path(p).name, a) for p, a in delegate_agents.install_delegate_agents(tmp_path))
    assert third == {"c3-scout.md": "updated", "c3-worker.md": "skipped"}
    assert scout.read_text(encoding="utf-8").strip() == delegate_agents.AGENTS["c3-scout"].strip()
    assert worker.read_text(encoding="utf-8") == "---\nname: c3-worker\nmodel: opus\n---\nmine\n"


def test_install_mcp_flag_and_wiring():
    from cli.c3 import __version__, _parse_cli_ide_arg
    from cli.commands.parser import build_parser

    parser = build_parser(__version__, _parse_cli_ide_arg)
    assert parser.parse_args(["install-mcp", "--no-agents"]).no_agents is True
    assert parser.parse_args(["install-mcp"]).no_agents is False
    src = Path(__file__).resolve().parents[1].joinpath("cli", "c3.py").read_text(encoding="utf-8")
    assert 'profile.name == "claude-code" and not getattr(args, "no_agents", False)' in src
    assert "install_delegate_agents(target)" in src


def test_instruction_surfaces_teach_host_tier_scout_and_subagents():
    from cli.c3 import _GLOBAL_CLAUDE_MD_CONTENT
    from services.claude_md import C3_COMPACT_WORKFLOW

    for text in (C3_COMPACT_WORKFLOW, _GLOBAL_CLAUDE_MD_CONTENT):
        line = next(ln for ln in text.splitlines() if "c3_delegate(" in ln)
        for needle in ("host", "tier", "scout", "write_paths", "c3-scout", "c3-worker"):
            assert needle in line, (needle, line)
        assert "backend='ollama|" not in line


def test_status_budget_view_reports_delegation(tmp_path):
    from cli.tools.status import _budget_view

    for detail in ({"backend": "claude", "tier": "small", "outcome": "ok", "cost_usd": 0.004, "wall_ms": 3000},
                   {"backend": "claude", "tier": "small", "outcome": "error", "wall_ms": 100},
                   {"backend": "codex", "tier": "small", "outcome": "ok", "wall_ms": 9000},
                   {"backend": "probe", "task_type": "available", "outcome": "5/5 up", "probe": True}):
        from datetime import datetime, timezone
        append_telemetry_record(tmp_path, {"ts": datetime.now(timezone.utc).isoformat(), "tool": "c3_delegate",
                                           "response_tokens": 5, "detail": detail})
    sm = SimpleNamespace(
        get_budget_snapshot=lambda: {"response_tokens": 10, "threshold": 100, "call_count": 1,
                                     "avg_tokens_per_call": 10},
        current_session={})
    svc = SimpleNamespace(session_mgr=sm, project_path=str(tmp_path),
                          file_memory=SimpleNamespace(list_tracked=lambda: []),
                          indexer=SimpleNamespace(get_stats=lambda: {"files_indexed": 0}))
    out = _budget_view(svc, False, lambda tool, args, resp, summary, **kw: resp)
    assert "[delegate:7d] 3 calls, 2 answered, $0.0040 reported (claude:small:2 | codex:small:1)" in out

    from datetime import datetime, timezone
    append_telemetry_record(tmp_path, {"ts": datetime.now(timezone.utc).isoformat(), "tool": "c3_delegate",
                                       "response_tokens": 5,
                                       "detail": {"backend": "claude", "tier": "medium", "mode": "write",
                                                  "outcome": "ok", "files_changed": 3, "wall_ms": 20000}})
    out = _budget_view(svc, False, lambda tool, args, resp, summary, **kw: resp)
    assert "[delegate:7d] 4 calls, 3 answered, $0.0040 reported, 3 file(s) written (" in out


def test_denied_file_path_is_a_blocked_response_counted_in_telemetry(monkeypatch, tmp_path):
    from cli.tools import delegate
    from services import access_guard

    denial = access_guard.Denial(rule="**/.env*", kind="deny", scope="builtin", reason="r")
    monkeypatch.setattr(delegate.access_guard, "verdict",
                        lambda p, op, root: access_guard.Verdict("denied", denial=denial))
    monkeypatch.setattr(delegate, "_run_claude", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    recorded, store = [], {}
    svc = SimpleNamespace(project_path=str(tmp_path), delegate_config={"enabled": True},
                          session_mgr=SimpleNamespace(record_tool_tokens=lambda tool, **kw: recorded.append(kw)),
                          compressor=None, notifications=None, _agent_progress_cb=None)

    def finalize(tool, meta, resp, status, **kw):
        store.update(resp=resp, status=status)
        return resp

    out = delegate.handle_delegate("what is in it?", "ask", "", ".env", svc, finalize, backend="claude")
    assert store["status"] == "blocked" and out.startswith("[c3-access:denied]")
    assert recorded[-1]["detail"]["outcome"] == "blocked"


def test_no_nudge_names_a_task_type_delegate_does_not_have():
    from cli.tools.delegate import DELEGATE_TASKS

    valid = set(DELEGATE_TASKS) | {"auto", "ping", "available", "codex_check", "gemini_check", "grok_check",
                                   "codex_resume"}
    root = Path(__file__).resolve().parents[1]
    for rel in ("services/agents.py", "cli/hook_filter.py", "services/claude_md.py"):
        text = root.joinpath(rel).read_text(encoding="utf-8")
        for task_type in re.findall(r"c3_delegate\(task_type='([a-z_]+)'", text):
            assert task_type in valid, (rel, task_type)
