"""D6 of the delegate remediation: delegation that happens without the agent deciding to.

1. A native ``Agent`` call with no ``model`` runs on the parent's model
   (Claude Code 2.1.270, measured: Explore and general-purpose both did). A
   PreToolUse command hook that returns ``updatedInput`` with ``model`` set
   changes it (measured: ``opus`` appeared in ``modelUsage`` only with the
   hook). ``hook_agent_model`` fills one tier below the parent, floor Sonnet.
2. ``[c3:delegate-hint]`` lines on c3_* responses when the session's own work
   looks delegable, with a cooldown and follow-through telemetry.

No subprocess except the dispatcher's own entry point.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import hook_agent_model, hook_dispatch
from services import agent_downshift as ad
from services import delegate_hints as dh
from services.telemetry import read_telemetry_records

REPO = Path(__file__).resolve().parents[1]
DEFAULTS = {"agent_downshift": "one_down", "agent_downshift_floor": "sonnet",
            "agent_downshift_skip": ["fork", "Plan"], "agent_models": {}}


# ── Policy ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("model, expected", [
    ("claude-haiku-4-5-20251001", 0), ("sonnet", 1), ("claude-opus-5[1m]", 2), ("claude-fable-5-1", 3),
    ("", None), ("<synthetic>", None), (None, None),
])
def test_rank(model, expected):
    assert ad.rank(model) == expected


@pytest.mark.parametrize("parent, stype, expected", [
    ("claude-fable-5-1", "general-purpose", ("opus", "one_down")),
    ("claude-opus-5", "Explore", ("sonnet", "one_down")),
    ("claude-sonnet-5", "Explore", ("", "at_floor")),
    ("claude-haiku-4-5", "general-purpose", ("", "at_floor")),
    ("claude-opus-5", "Plan", ("", "skipped_type")),
    ("claude-opus-5", "fork", ("", "skipped_type")),
    ("", "Explore", ("", "parent_unknown")),
])
def test_resolve_defaults(parent, stype, expected):
    assert ad.resolve(stype, parent, DEFAULTS) == expected


def test_resolve_config():
    assert ad.resolve("Explore", "claude-opus-5", {**DEFAULTS, "agent_downshift": "off"}) == ("", "off")
    fixed = {**DEFAULTS, "agent_downshift": "sonnet"}
    assert ad.resolve("x", "claude-fable-5-1", fixed) == ("sonnet", "sonnet")
    assert ad.resolve("x", "claude-sonnet-5", fixed) == ("", "at_floor")
    floor = {**DEFAULTS, "agent_downshift_floor": "haiku"}
    assert ad.resolve("x", "claude-sonnet-5", floor) == ("haiku", "one_down")
    per_type = {**DEFAULTS, "agent_models": {"Explore": "haiku", "Plan": "sonnet", "gp": "inherit"}}
    assert ad.resolve("Explore", "claude-opus-5", per_type) == ("haiku", "override")
    assert ad.resolve("Explore", "", per_type) == ("haiku", "override")  # explicit choice, parent unknown
    assert ad.resolve("Explore", "claude-haiku-4-5", per_type) == ("", "not_a_downshift")
    assert ad.resolve("Plan", "claude-opus-5", per_type) == ("", "skipped_type")  # skip list first
    assert ad.resolve("gp", "claude-opus-5", per_type) == ("", "override_inherit")
    assert ad.resolve("x", "claude-opus-5", {**DEFAULTS, "agent_downshift_skip": []})[0] == "sonnet"


# ── Parent model ────────────────────────────────────────────────────────────


def _transcript(tmp_path, *models):
    path = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "hi"}}]
    rows += [{"type": "assistant", "message": {"model": m, "content": []}} for m in models]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_transcript_model_takes_the_latest_real_model(tmp_path):
    assert ad.transcript_model(_transcript(tmp_path, "claude-sonnet-5", "claude-opus-5", "<synthetic>")) \
        == "claude-opus-5"
    assert ad.transcript_model(_transcript(tmp_path)) == ""
    assert ad.transcript_model(tmp_path / "missing.jsonl") == ""


def test_parent_model_falls_back_to_remembered_then_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    project = tmp_path / "p"
    (project / ".c3").mkdir(parents=True)
    # transcript wins and is remembered
    t = _transcript(tmp_path, "claude-fable-5-1")
    assert ad.parent_model({"transcript_path": str(t)}, project) == ("claude-fable-5-1", "transcript")
    assert ad.parent_model({"transcript_path": ""}, project) == ("claude-fable-5-1", "remembered")
    # stale memory is ignored; settings next
    state = project / ".c3" / ad.STATE_FILE
    state.write_text(json.dumps({"last_parent_model": "claude-fable-5-1",
                                 "seen_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
    assert ad.parent_model({}, project) == ("", "unknown")
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.local.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    assert ad.parent_model({}, project) == ("opus", "settings")


def test_definition_sets_model(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    agents = tmp_path / "p" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "c3-scout.md").write_text("---\nname: c3-scout\nmodel: haiku\n---\nbody", encoding="utf-8")
    (agents / "helper.md").write_text("---\nname: helper\nmodel: inherit\n---\n", encoding="utf-8")
    (agents / "other-file.md").write_text("---\nname: renamed\nmodel: sonnet\n---\n", encoding="utf-8")
    user_agents = tmp_path / "home" / ".claude" / "agents"
    user_agents.mkdir(parents=True)
    (user_agents / "mine.md").write_text("---\nname: mine\nmodel: opus\n---\n", encoding="utf-8")
    project = tmp_path / "p"
    assert ad.definition_sets_model("c3-scout", project)
    assert not ad.definition_sets_model("helper", project)
    assert ad.definition_sets_model("renamed", project)
    assert ad.definition_sets_model("mine", project)
    assert not ad.definition_sets_model("Explore", project)


# ── The hook ────────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("C3_AGENT_DOWNSHIFT", raising=False)
    p = tmp_path / "proj"
    (p / ".c3").mkdir(parents=True)
    return p


def _payload(project, transcript, **tool_input):
    return {"tool_name": "Agent", "cwd": str(project), "transcript_path": str(transcript),
            "session_id": "s1", "tool_input": {"description": "d", "prompt": "p", **tool_input}}


def test_hook_fills_one_tier_down_and_records_it(project, tmp_path):
    t = _transcript(tmp_path, "claude-opus-5")
    out = hook_agent_model.run(_payload(project, t, subagent_type="Explore"), project)
    assert out == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {
        "description": "d", "prompt": "p", "subagent_type": "Explore", "model": "sonnet"}}}
    (row,) = [r for r in read_telemetry_records(project) if r["tool"] == "agent_downshift"]
    assert row["detail"] == {"subagent_type": "Explore", "parent_model": "claude-opus-5",
                             "parent_source": "transcript", "model": "sonnet", "reason": "one_down",
                             "applied": True}


def test_hook_leaves_explicit_models_defined_agents_plugins_and_opt_outs(project, tmp_path, monkeypatch):
    t = _transcript(tmp_path, "claude-fable-5-1")
    assert hook_agent_model.run(_payload(project, t, model="fable"), project) is None
    assert hook_agent_model.run(_payload(project, t, subagent_type="codex:codex-rescue"), project) is None
    agents = project / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "c3-worker.md").write_text("---\nname: c3-worker\nmodel: sonnet\n---\n", encoding="utf-8")
    assert hook_agent_model.run(_payload(project, t, subagent_type="c3-worker"), project) is None
    (project / ".c3" / "config.json").write_text(json.dumps({"delegate": {"agent_downshift": "off"}}),
                                                 encoding="utf-8")
    assert hook_agent_model.run(_payload(project, t), project) is None
    (project / ".c3" / "config.json").unlink()
    monkeypatch.setenv("C3_AGENT_DOWNSHIFT", "0")
    assert hook_agent_model.run(_payload(project, t), project) is None
    monkeypatch.delenv("C3_AGENT_DOWNSHIFT")
    assert hook_agent_model.run({**_payload(project, t), "tool_name": "Read"}, project) is None
    out = hook_agent_model.run(_payload(project, t), project)
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "opus"


def test_hook_records_skips_and_never_raises(project, tmp_path, monkeypatch):
    t = _transcript(tmp_path)  # no model yet, nothing remembered, no settings
    assert hook_agent_model.run(_payload(project, t), project) is None
    (row,) = [r for r in read_telemetry_records(project) if r["tool"] == "agent_downshift_skip"]
    assert row["detail"]["reason"] == "parent_unknown" and row["detail"]["applied"] is False
    monkeypatch.setattr(ad, "resolve", lambda *a: 1 / 0)
    assert hook_agent_model.run(_payload(project, _transcript(tmp_path, "claude-opus-5")), project) is None


# ── Dispatcher ──────────────────────────────────────────────────────────────


def test_routes_agent_on_claude_only():
    assert "hook_agent_model" in list(hook_dispatch._routes("pretool", "Agent", "Agent"))
    assert "hook_agent_model" in list(hook_dispatch._routes("pretool", "Task", "Task"))
    assert "hook_agent_model" not in list(hook_dispatch._routes("pretool", "Read", "Read"))
    assert "hook_agent_model" not in list(hook_dispatch._routes("pretool", "Agent", "Agent",
                                                                 hook_dispatch.HOST_CODEX))


def test_merge_carries_updated_input_but_a_deny_wins():
    upd = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {"model": "sonnet"}}}
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "no"}}
    assert hook_dispatch.merge_outputs([upd], [], event="pretool") == upd
    assert hook_dispatch.merge_outputs([deny, upd], [], event="pretool")["hookSpecificOutput"] \
        == deny["hookSpecificOutput"]
    assert hook_dispatch.merge_outputs([upd], [], event="posttool") is None


def test_dispatcher_entry_point_end_to_end(project, tmp_path):
    t = _transcript(tmp_path, "claude-fable-5-1")
    payload = _payload(project, t, subagent_type="general-purpose")
    env = {**__import__("os").environ, "HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")}
    env.pop("C3_AGENT_DOWNSHIFT", None)
    proc = subprocess.run([sys.executable, str(REPO / "cli" / "hook_dispatch.py"), "pretool"],
                          input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8",
                          cwd=project, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["updatedInput"]["model"] == "opus"
    assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] == "general-purpose"


# ── Installer and hub migration ─────────────────────────────────────────────


def test_hub_migration_adds_the_agent_matcher_once(tmp_path):
    from cli.c3 import AGENT_MATCHER
    from cli.hub_server import c3_hook_migrations, migrate_hooks_for_project

    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": []}]}}),
                        encoding="utf-8")
    migrations = [m for m in c3_hook_migrations() if m["event"] == "PreToolUse"]
    assert migrations and migrations[0]["entry"]["matcher"] == AGENT_MATCHER
    assert migrate_hooks_for_project(str(tmp_path), migrations) == 1
    assert migrate_hooks_for_project(str(tmp_path), migrations) == 0
    pre = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    assert [e["matcher"] for e in pre] == ["Read", AGENT_MATCHER]
    command = pre[1]["hooks"][0]["command"]  # quoting differs by OS (hook_command_arg)
    assert "hook_dispatch.py" in command and command.endswith(" pretool")


def test_installer_registers_the_agent_matcher():
    src = (REPO / "cli" / "c3.py").read_text(encoding="utf-8")
    assert "_pre_matcher_names.append(AGENT_MATCHER)" in src


# ── Hints ───────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_write_hint_fires_once_per_cooldown():
    clock = Clock()
    tr = dh.HintTracker(clock)
    hints = []
    for i in range(6):
        clock.t += 60
        hints.append(tr.note_edit(f"f{i % 3}.py", 900, cooldown_s=45 * 60))
    fired = [h for h in hints if h]
    assert len(fired) == 1 and hints.index(fired[0]) == 4  # 5th edit: 5 edits, 3 files, 4500 chars
    assert "write_paths=" in fired[0] and fired[0].startswith(dh.TAG)
    clock.t += 30 * 60
    assert not tr.note_edit("f9.py", 5000, cooldown_s=45 * 60)  # still cooling down
    clock.t += 20 * 60
    for i in range(5):
        clock.t += 30
        last = tr.note_edit(f"g{i}.py", 1000, cooldown_s=45 * 60)
    assert last


def test_write_hint_thresholds_and_big_edit():
    clock = Clock()
    tr = dh.HintTracker(clock)
    for _ in range(8):  # many edits, one file
        clock.t += 10
        assert not tr.note_edit("one.py", 2000, cooldown_s=60)
    tr2 = dh.HintTracker(clock)
    assert "one edit of ~1750 tokens" in tr2.note_edit("new.py", 7000, cooldown_s=60)


def test_no_write_hint_while_already_delegating_writes():
    clock = Clock()
    tr = dh.HintTracker(clock)
    tr.note_delegate(write=True, scout=False)
    for i in range(6):
        clock.t += 10
        assert not tr.note_edit(f"f{i}.py", 2000, cooldown_s=60)


def test_explore_hint_needs_files_and_no_edit():
    clock = Clock()
    tr = dh.HintTracker(clock)
    out = [tr.note_read(f, cooldown_s=60) for f in ("a.py", "b.py", "", "c.py", "", "d.py")]
    assert out[-1] and "scout=True" in out[-1] and not any(out[:-1])
    tr2 = dh.HintTracker(clock)
    tr2.note_edit("x.py", 10, cooldown_s=60)
    assert not any(tr2.note_read(f"{i}.py", cooldown_s=60) for i in range(8))


def test_follow_counts_once_per_hint():
    clock = Clock()
    tr = dh.HintTracker(clock)
    for i in range(5):
        tr.note_edit(f"f{i}.py", 1000, cooldown_s=60)
    clock.t += 120
    assert tr.note_delegate(write=True, scout=False) == ("write", 120.0)
    assert tr.note_delegate(write=True, scout=False) is None
    assert tr.note_delegate(write=False, scout=True) is None  # no explore hint was shown
    clock.t += dh.FOLLOW_WINDOW_S + 1
    assert tr.note_delegate(write=True, scout=False) is None


def test_edit_chars():
    assert dh.edit_chars("abc", "") == 3
    assert dh.edit_chars("", json.dumps([{"new_string": "xy"}, {"new_string": "z"}, "junk"])) == 3
    assert dh.edit_chars("a", "not json") == 1


def _hint_svc(tmp_path, **cfg):
    return SimpleNamespace(project_path=str(tmp_path), delegate_config={"enabled": True, **cfg})


def test_service_helpers_record_telemetry_and_respect_config(tmp_path):
    svc = _hint_svc(tmp_path)
    svc._delegate_hints = dh.HintTracker(Clock())
    for i in range(5):
        hint = dh.after_edit(svc, f"f{i}.py", 1000)
    assert hint
    dh.after_delegate(svc, write=True, scout=False)
    tools = [r["tool"] for r in read_telemetry_records(tmp_path)]
    assert tools == ["delegate_hint", "delegate_hint_followed"]
    off = _hint_svc(tmp_path / "off", hints=False)
    assert not any(dh.after_edit(off, f"f{i}.py", 5000) for i in range(6))


def test_mcp_with_hint_appends_only_to_successes():
    from cli.mcp_server import _with_hint
    assert _with_hint("✓ a.py [+1L]", lambda: "[c3:delegate-hint] x") == "✓ a.py [+1L]\n[c3:delegate-hint] x"
    assert _with_hint("✓ a.py", lambda: "") == "✓ a.py"
    assert _with_hint("[c3_edit:error] nope", lambda: "hint") == "[c3_edit:error] nope"
    assert _with_hint("✓ ok", lambda: 1 / 0) == "✓ ok"


def test_status_line(tmp_path):
    from cli.tools.status import _budget_view
    from services.telemetry import append_telemetry_record

    for tool in ("agent_downshift", "agent_downshift", "agent_downshift_skip", "delegate_hint",
                 "delegate_hint_followed"):
        append_telemetry_record(tmp_path, {"tool": tool, "detail": {}})
    sm = SimpleNamespace(get_budget_snapshot=lambda: {"response_tokens": 1, "threshold": 100, "call_count": 1,
                                                      "avg_tokens_per_call": 1}, current_session={})
    svc = SimpleNamespace(session_mgr=sm, project_path=str(tmp_path),
                          file_memory=SimpleNamespace(list_tracked=lambda: []),
                          indexer=SimpleNamespace(get_stats=lambda: {"files_indexed": 0}))
    out = _budget_view(svc, False, lambda tool, args, resp, summary, **kw: resp)
    assert ("[delegate-auto:7d] 2 of 3 model-less subagent call(s) moved to a lower model, "
            "1 delegation hint(s) shown, 1 followed") in out
