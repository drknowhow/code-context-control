"""Allow/deny matrix for cli/hook_pretool_enforce.run().

Drives the enforcement logic in-process (no subprocess) via the importable
run(payload, project_path) entry point added for the v2.42 dispatcher:
  - no state → read-class advisory, write-class deny
  - fresh signal in consolidated state → allow (write tools only for
    write-granting c3 tools)
  - stale TTL → advisory
  - sticky per-file unlock → allow + drift-guard nudge
  - corrupted state file → advisory fallback + critical warning + quarantine
  - legacy last_c3_call.json / unlocked_files.json fallback still honored
  - session mismatch → state treated as stale (advisory, no stale unlocks)
"""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

# hook_pretool_enforce imports plain `_hook_utils`; alias so both spellings
# resolve to the same module instance (mirrors cli/hook_dispatch.py).
sys.modules.setdefault("_hook_utils", _hook_utils)

from cli.hook_pretool_enforce import run  # noqa: E402


def _now_iso(offset_secs: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_secs)).isoformat()


class EnforceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        _hook_utils.drain_state_warnings()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, tool_name, tool_input, session_id=""):
        payload = {"tool_name": tool_name, "tool_input": tool_input}
        if session_id:
            payload["session_id"] = session_id
        return run(payload, project_path=self.tmp)

    def _write_state(self, tool="", read_unlocked=False, ts=None,
                     unlocked=None, session_id=""):
        state = {"session_id": session_id, "last_c3_call": None,
                 "unlocked_files": unlocked or {}}
        if tool:
            state["last_c3_call"] = {
                "ts": ts if ts is not None else _now_iso(),
                "tool": tool,
                "read_unlocked": read_unlocked,
            }
        (self.tmp / ".c3" / "enforcement_state.json").write_text(
            json.dumps(state), encoding="utf-8")

    # ── assertion helpers ────────────────────────────────────────────────────
    def assert_advisory(self, out):
        self.assertIsNotNone(out)
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("[c3:hint]", out.get("additionalContext", ""))

    def assert_denied(self, out):
        self.assertIsNotNone(out)
        hso = out.get("hookSpecificOutput") or {}
        self.assertEqual(hso.get("permissionDecision"), "deny")
        self.assertIn("ledger", hso.get("permissionDecisionReason", "").lower())

    def assert_allowed_silently(self, out):
        self.assertIsNone(out)


class TestNoState(EnforceBase):
    def test_read_is_advisory(self):
        self.assert_advisory(self._run("Read", {"file_path": "foo.py"}))

    def test_grep_is_advisory(self):
        self.assert_advisory(self._run("Grep", {"pattern": "foo"}))

    def test_glob_is_advisory(self):
        self.assert_advisory(self._run("Glob", {"pattern": "**/*.py"}))

    def test_edit_is_denied(self):
        self.assert_denied(self._run("Edit", {"file_path": "foo.py",
                                              "old_string": "a", "new_string": "b"}))

    def test_write_is_denied(self):
        self.assert_denied(self._run("Write", {"file_path": "foo.py", "content": "x"}))

    def test_unknown_tool_passes_through(self):
        self.assert_allowed_silently(self._run("SomeRandomTool", {}))


class TestSignal(EnforceBase):
    def test_fresh_read_signal_allows_read(self):
        self._write_state(tool="c3_search", read_unlocked=True)
        self.assert_allowed_silently(self._run("Read", {"file_path": "foo.py"}))

    def test_fresh_signal_records_sticky_unlock(self):
        self._write_state(tool="c3_search", read_unlocked=True)
        self._run("Read", {"file_path": str(self.tmp / "foo.py")})
        state = _hook_utils.load_enforcement_state(self.tmp)
        normalized = _hook_utils.canonical_key(self.tmp / "foo.py")
        self.assertIn("read", state["unlocked_files"].get(normalized, []))

    def test_read_signal_does_not_unlock_edit(self):
        self._write_state(tool="c3_search", read_unlocked=True)
        self.assert_denied(self._run("Edit", {"file_path": "foo.py",
                                              "old_string": "a", "new_string": "b"}))

    def test_status_signal_does_not_unlock_write(self):
        self._write_state(tool="c3_status", read_unlocked=False)
        self.assert_denied(self._run("Write", {"file_path": "foo.py", "content": "x"}))

    def test_edit_signal_unlocks_edit(self):
        self._write_state(tool="c3_edit", read_unlocked=False)
        self.assert_allowed_silently(self._run("Edit", {"file_path": "foo.py",
                                                        "old_string": "a", "new_string": "b"}))

    def test_agent_signal_unlocks_multiedit(self):
        self._write_state(tool="c3_agent", read_unlocked=False)
        self.assert_allowed_silently(self._run("MultiEdit", {"file_path": "foo.py", "edits": []}))

    def test_stale_ttl_signal_is_advisory(self):
        # 700s old signal > 600s TTL → treated as absent
        self._write_state(tool="c3_search", read_unlocked=True, ts=_now_iso(-700))
        self.assert_advisory(self._run("Read", {"file_path": "foo.py"}))

    def test_stale_ttl_signal_keeps_edit_denied(self):
        self._write_state(tool="c3_edit", read_unlocked=False, ts=_now_iso(-700))
        self.assert_denied(self._run("Edit", {"file_path": "foo.py",
                                              "old_string": "a", "new_string": "b"}))

    def test_non_read_unlocking_signal_grep_without_target(self):
        # Fix 5 parity: c3_memory signal does not unlock bare Grep/Glob
        self._write_state(tool="c3_memory", read_unlocked=False)
        self.assert_advisory(self._run("Grep", {}))


class TestStickyUnlock(EnforceBase):
    def _unlock(self, name, cats):
        normalized = str((self.tmp / name).resolve())
        self._write_state(unlocked={normalized: cats})
        return str(self.tmp / name)

    def test_read_unlock_allows_read_with_drift_guard(self):
        fp = self._unlock("foo.py", ["read"])
        out = self._run("Read", {"file_path": fp})
        self.assertIsNotNone(out)
        self.assertIn("[c3:drift-guard]", out.get("additionalContext", ""))
        self.assertNotIn("hookSpecificOutput", out)

    def test_read_unlock_does_not_allow_edit(self):
        fp = self._unlock("foo.py", ["read"])
        self.assert_denied(self._run("Edit", {"file_path": fp,
                                              "old_string": "a", "new_string": "b"}))

    def test_edit_unlock_allows_edit(self):
        fp = self._unlock("foo.py", ["edit"])
        out = self._run("Edit", {"file_path": fp, "old_string": "a", "new_string": "b"})
        self.assertIsNotNone(out)
        self.assertIn("[c3:drift-guard]", out.get("additionalContext", ""))

    def test_unlock_is_per_file(self):
        self._unlock("foo.py", ["read"])
        self.assert_advisory(self._run("Read", {"file_path": str(self.tmp / "bar.py")}))


class TestCorruptedState(EnforceBase):
    def test_corrupted_state_falls_back_to_advisory(self):
        state_path = self.tmp / ".c3" / "enforcement_state.json"
        state_path.write_text("{not valid json !!!", encoding="utf-8")
        out = self._run("Read", {"file_path": "foo.py"})
        self.assert_advisory(out)

    def test_corrupted_state_is_quarantined_and_warned(self):
        state_path = self.tmp / ".c3" / "enforcement_state.json"
        state_path.write_text("{not valid json !!!", encoding="utf-8")
        self._run("Read", {"file_path": "foo.py"})
        self.assertFalse(state_path.exists(), "corrupt state file must be quarantined")
        self.assertTrue((self.tmp / ".c3" / "enforcement_state.json.corrupt").exists())
        warnings = _hook_utils.drain_state_warnings()
        self.assertTrue(any("[c3:hook-error]" in w for w in warnings), warnings)

    def test_corrupted_state_keeps_write_denied(self):
        # Fail-open for reads must never fail-open for writes.
        (self.tmp / ".c3" / "enforcement_state.json").write_text("garbage", encoding="utf-8")
        self.assert_denied(self._run("Write", {"file_path": "foo.py", "content": "x"}))


class TestLegacyFallback(EnforceBase):
    def test_legacy_signal_file_honored(self):
        (self.tmp / ".c3" / "last_c3_call.json").write_text(json.dumps({
            "timestamp": _now_iso(), "tool": "c3_search", "read_unlocked": True,
        }), encoding="utf-8")
        self.assert_allowed_silently(self._run("Read", {"file_path": "foo.py"}))

    def test_legacy_unlock_file_honored(self):
        normalized = str((self.tmp / "foo.py").resolve())
        (self.tmp / ".c3" / "unlocked_files.json").write_text(
            json.dumps({normalized: ["read"]}), encoding="utf-8")
        out = self._run("Read", {"file_path": str(self.tmp / "foo.py")})
        self.assertIsNotNone(out)
        self.assertIn("[c3:drift-guard]", out.get("additionalContext", ""))

    def test_new_state_wins_over_legacy(self):
        # Legacy has a fresh signal but the new file exists (empty) → new wins.
        (self.tmp / ".c3" / "last_c3_call.json").write_text(json.dumps({
            "timestamp": _now_iso(), "tool": "c3_search", "read_unlocked": True,
        }), encoding="utf-8")
        self._write_state()  # empty consolidated state
        self.assert_advisory(self._run("Read", {"file_path": "foo.py"}))


class TestSessionScoping(EnforceBase):
    def test_session_mismatch_ignores_signal(self):
        self._write_state(tool="c3_search", read_unlocked=True, session_id="session-A")
        out = self._run("Read", {"file_path": "foo.py"}, session_id="session-B")
        self.assert_advisory(out)

    def test_session_mismatch_ignores_write_unlock(self):
        self._write_state(tool="c3_edit", read_unlocked=False, session_id="session-A")
        self.assert_denied(self._run(
            "Edit", {"file_path": "foo.py", "old_string": "a", "new_string": "b"},
            session_id="session-B",
        ))

    def test_same_session_signal_allows(self):
        self._write_state(tool="c3_search", read_unlocked=True, session_id="session-A")
        self.assert_allowed_silently(
            self._run("Read", {"file_path": "foo.py"}, session_id="session-A"))

    def test_unscoped_legacy_state_still_accepted(self):
        # State without a session_id (legacy writers) is accepted by any session.
        self._write_state(tool="c3_search", read_unlocked=True, session_id="")
        self.assert_allowed_silently(
            self._run("Read", {"file_path": "foo.py"}, session_id="session-B"))

class TestProjectScope(EnforceBase):
    """A call aimed outside the project root is none of this hook's business
    (field report 2026-08-22, ISSUE-2): no ledger here to protect, so no
    block and no hint — in strict mode, with no c3 state at all."""

    def setUp(self):
        super().setUp()
        self.other = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.other, ignore_errors=True)
        super().tearDown()

    def test_write_outside_root_passes_through(self):
        out = self._run("Write", {"file_path": str(self.other / "scratch.html"), "content": "x"})
        self.assertIsNone(out)

    def test_edit_outside_root_passes_through(self):
        out = self._run("Edit", {"file_path": str(self.other / "a.py"), "old_string": "a", "new_string": "b"})
        self.assertIsNone(out)

    def test_write_inside_root_is_still_blocked(self):
        out = self._run("Write", {"file_path": str(self.tmp / "a.py"), "content": "x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_relative_paths_resolve_against_the_root(self):
        inside = self._run("Edit", {"file_path": "sub/a.py", "old_string": "a", "new_string": "b"})
        self.assertEqual(inside["hookSpecificOutput"]["permissionDecision"], "deny")
        escaped = self._run("Edit", {"file_path": "../escaped.py", "old_string": "a", "new_string": "b"})
        self.assertIsNone(escaped)

    def test_read_outside_root_gets_no_hint(self):
        self.assertIsNone(self._run("Read", {"file_path": str(self.other / "notes.md")}))
        self.assertIsNone(self._run("Grep", {"pattern": "x", "path": str(self.other)}))

    def test_vault_guard_still_runs_first(self):
        out = self._run("Write", {"file_path": str(self.other / ".c3" / "secrets.enc"), "content": "x"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("vault", out["hookSpecificOutput"]["permissionDecisionReason"].lower())

class TestSubagentGrant(EnforceBase):
    """A sub-agent whose `tools:` grant has no c3 MCP tool cannot follow a
    "use c3_edit" deny (field report 2026-08-22, ISSUE-1). For that agent the
    strict block degrades to the advisory nudge; everyone else stays strict."""

    def _agent(self, name, frontmatter, where=None):
        d = (where or self.tmp) / ".claude" / "agents"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.md").write_text("---\n" + frontmatter + "\n---\n\nYou are an agent.\n", encoding="utf-8")

    def _run_as(self, agent, tool_name="Write", tool_input=None):
        payload = {"tool_name": tool_name, "agent_type": agent, "agent_id": "a1",
                   "tool_input": tool_input or {"file_path": str(self.tmp / "a.py"), "content": "x"}}
        return run(payload, project_path=self.tmp)

    def _assert_deny(self, out):
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def _assert_hint(self, out):
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("c3_edit", out["additionalContext"])
        self.assertIn("ledger still records", out["additionalContext"])

    def test_inline_grant_without_c3_degrades_to_a_hint(self):
        self._agent("writer", "name: writer\ntools: Read, Write, Edit, Bash")
        self._assert_hint(self._run_as("writer"))
        self._assert_hint(self._run_as("writer", "Edit", {"file_path": str(self.tmp / "a.py"), "old_string": "a", "new_string": "b"}))

    def test_bracket_and_list_forms(self):
        self._agent("br", "tools: [Read, Write]")
        self._assert_hint(self._run_as("br"))
        self._agent("ls", "name: ls\ntools:\n  - Read\n  - Write\nmodel: inherit")
        self._assert_hint(self._run_as("ls"))

    def test_grant_that_reaches_c3_stays_strict(self):
        self._agent("tooled", "tools: Read, Write, mcp__c3__c3_edit")
        self._assert_deny(self._run_as("tooled"))
        self._agent("server", "tools: Read, Write, mcp__c3")
        self._assert_deny(self._run_as("server"))
        self._agent("star", "tools: '*'")
        self._assert_deny(self._run_as("star"))

    def test_no_tools_line_inherits_everything_and_stays_strict(self):
        self._agent("plain", "name: plain\nmodel: inherit")
        self._assert_deny(self._run_as("plain"))

    def test_unknown_agent_stays_strict_and_the_deny_names_the_fix(self):
        out = self._run_as("ghost")
        self._assert_deny(out)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("agent 'ghost'", reason)
        self.assertIn("mcp__c3__c3_edit", reason)

    def test_main_thread_deny_has_no_agent_sentence(self):
        out = self._run("Write", {"file_path": str(self.tmp / "a.py"), "content": "x"})
        self._assert_deny(out)
        self.assertNotIn("Running as agent", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_user_level_agents_dir_is_consulted(self):
        home = Path(tempfile.mkdtemp())
        try:
            self._agent("homer", "tools: Read, Write", where=home)
            with mock.patch("pathlib.Path.home", return_value=home):
                self._assert_hint(self._run_as("homer"))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_nested_agent_file_is_found(self):
        d = self.tmp / ".claude" / "agents" / "team"
        d.mkdir(parents=True)
        (d / "nested.md").write_text("---\ntools: Write\n---\n", encoding="utf-8")
        self._assert_hint(self._run_as("nested"))

    def test_plugin_scoped_names_are_not_looked_up(self):
        self._assert_deny(self._run_as("codex:codex-rescue"))

    def test_read_class_is_unaffected(self):
        self._agent("reader", "tools: Read")
        out = self._run_as("reader", "Read", {"file_path": str(self.tmp / "a.py")})
        self.assertIn("[c3:hint]", out["additionalContext"])
        self.assertNotIn("ledger still records", out["additionalContext"])

class TestShellAdvisory(EnforceBase):
    """Bash is nudged, never denied (field report 2026-08-22, ISSUE-1's
    buried finding): a command that looks like it writes project files gets
    the advisory hint naming them — in strict mode too — unless c3_edit just
    ran. Read-only commands, writes outside the root, and `off` are silent."""

    def _bash(self, cmd, session_id=""):
        return self._run("Bash", {"command": cmd}, session_id=session_id)

    def test_write_inside_gets_hint_not_deny(self):
        out = self._bash("python -c \"open('gen.py','w').write('x')\"")
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("[c3:hint]", out["additionalContext"])
        self.assertIn("gen.py", out["additionalContext"])
        self.assertIn("never blocked", out["additionalContext"])

    def test_heredoc_redirect_names_the_file(self):
        out = self._bash("cat > docs/notes.md <<'EOF'\nhi\nEOF")
        self.assertIn("docs/notes.md", out["additionalContext"])

    def test_read_only_command_is_silent(self):
        self.assertIsNone(self._bash("grep -rn foo . | head"))
        self.assertIsNone(self._bash("git status && ls -la"))

    def test_write_outside_root_is_silent(self):
        other = Path(tempfile.mkdtemp())
        try:
            self.assertIsNone(self._bash(f'echo x > "{other / "scratch.txt"}"'))
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_fresh_c3_edit_signal_silences_the_hint(self):
        self._write_state(tool="c3_edit", session_id="s1")
        self.assertIsNone(self._bash("echo x > a.py", session_id="s1"))

    def test_read_class_signal_does_not_silence(self):
        self._write_state(tool="c3_search", read_unlocked=True, session_id="s1")
        out = self._bash("echo x > a.py", session_id="s1")
        self.assertIn("[c3:hint]", out["additionalContext"])

    def test_run_shell_command_alias(self):
        out = self._run("run_shell_command", {"command": "touch made.py"})
        self.assertIn("made.py", out["additionalContext"])

    def test_off_mode_is_silent(self):
        (self.tmp / ".c3" / "config.json").write_text(
            json.dumps({"enforcement": {"mode": "off", "set_by": "user"}}), encoding="utf-8")
        self.assertIsNone(self._bash("echo x > a.py"))

    def test_empty_command(self):
        self.assertIsNone(self._bash(""))


if __name__ == "__main__":
    unittest.main()
