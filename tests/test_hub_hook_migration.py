"""The hub's hook migration (v2.128.0) — how an installed project catches up.

Adding a hook to `c3 install-mcp` does nothing for a project that is already
installed. The v2.126.0 SessionStart / SessionEnd hooks landed in the
installer only, so every existing project — 60 of them on the machine this was
found on, C3's own repo among them — kept writing no `session_open` /
`session_end` rows at all, and liveness had no end-of-session signal to read.

`_migrate_project_hooks` already rewrote every registered project's settings
at hub startup, but for exactly one hardcoded entry. These pin the
data-driven version: additive, idempotent, never creates a settings file, and
never drops a user's own hooks.
"""
from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli import hub_server  # noqa: E402

CLAUDE_SETTINGS = ".claude/settings.local.json"
GEMINI_SETTINGS = ".gemini/settings.json"


class TestHookMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)

    def _write(self, rel: str, settings: dict):
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        return path

    def _read(self, rel: str) -> dict:
        return json.loads((self.project / rel).read_text(encoding="utf-8"))

    def _commands(self, settings: dict, event: str) -> list:
        return [hk.get("command", "")
                for entry in settings.get("hooks", {}).get(event, [])
                for hk in entry.get("hooks", [])]

    def _migrate(self) -> int:
        return hub_server.migrate_hooks_for_project(str(self.project))

    # ── the gap this closes ───────────────────────────────────────────────

    def test_lifecycle_hooks_are_added_to_an_installed_project(self):
        self._write(CLAUDE_SETTINGS, {"hooks": {"PostToolUse": []}})
        self.assertEqual(self._migrate(), 1)
        settings = self._read(CLAUDE_SETTINGS)
        for event, arg in (("SessionStart", "start"), ("SessionEnd", "end")):
            commands = self._commands(settings, event)
            self.assertEqual(len(commands), 1, event)
            self.assertIn("hook_dispatch.py", commands[0])
            self.assertTrue(commands[0].rstrip().endswith(arg), commands[0])

    def test_c3read_hook_still_lands(self):
        self._write(CLAUDE_SETTINGS, {"hooks": {}})
        self._migrate()
        matchers = [e.get("matcher") for e
                    in self._read(CLAUDE_SETTINGS)["hooks"]["PostToolUse"]]
        self.assertIn("mcp__c3__c3_read", matchers)

    def test_settings_with_no_hooks_key_at_all(self):
        self._write(CLAUDE_SETTINGS, {"permissions": {"allow": []}})
        self.assertEqual(self._migrate(), 1)
        settings = self._read(CLAUDE_SETTINGS)
        self.assertEqual(settings["permissions"], {"allow": []})
        self.assertIn("SessionStart", settings["hooks"])

    # ── idempotence and user content ──────────────────────────────────────

    def test_second_run_changes_nothing(self):
        self._write(CLAUDE_SETTINGS, {"hooks": {}})
        self.assertEqual(self._migrate(), 1)
        before = self._read(CLAUDE_SETTINGS)
        self.assertEqual(self._migrate(), 0)
        self.assertEqual(self._read(CLAUDE_SETTINGS), before)

    def test_user_hooks_for_the_same_event_survive(self):
        user_entry = {"matcher": "", "hooks": [
            {"type": "command", "command": "powershell notify.ps1 start"}]}
        self._write(CLAUDE_SETTINGS, {"hooks": {"SessionStart": [user_entry]}})
        self._migrate()
        commands = self._commands(self._read(CLAUDE_SETTINGS), "SessionStart")
        self.assertEqual(len(commands), 2)
        self.assertIn("powershell notify.ps1 start", commands)

    def test_an_existing_c3_entry_is_not_duplicated(self):
        existing = {"matcher": "", "hooks": [
            {"type": "command", "command": "python /elsewhere/cli/hook_dispatch.py start"}]}
        self._write(CLAUDE_SETTINGS, {"hooks": {"SessionStart": [existing]}})
        self.assertEqual(
            len(self._commands(self._read(CLAUDE_SETTINGS), "SessionStart")), 1)
        self._migrate()
        self.assertEqual(
            len(self._commands(self._read(CLAUDE_SETTINGS), "SessionStart")), 1)

    # ── the command string has to actually run ────────────────────────────

    def test_commands_are_quoted_for_this_platform(self):
        """shlex.quote is WRONG on Windows: single quotes are not quoting to
        cmd.exe, so an entry for any path with a space (every path under
        "1. Projects") never ran. The migration used it until 2.128.0."""
        self._write(CLAUDE_SETTINGS, {"hooks": {}})
        self._migrate()
        settings = self._read(CLAUDE_SETTINGS)
        commands = (self._commands(settings, "SessionStart")
                    + self._commands(settings, "SessionEnd")
                    + self._commands(settings, "PostToolUse"))
        self.assertTrue(commands)
        for command in commands:
            tokens = shlex.split(command, posix=(sys.platform != "win32"))
            script = Path(tokens[1].strip('"'))
            self.assertTrue(script.is_file(), command)
            if sys.platform == "win32":
                self.assertNotIn("'", command, command)
                self.assertNotIn("\\", command, command)

    def test_dispatcher_command_matches_the_installer_exactly(self):
        from cli._hook_utils import hook_command_arg
        cli_dir = Path(hub_server.__file__).resolve().parent
        expected = (f"{hook_command_arg(sys.executable)} "
                    f"{hook_command_arg(str(cli_dir / 'hook_dispatch.py'))} start")
        self._write(CLAUDE_SETTINGS, {"hooks": {}})
        self._migrate()
        self.assertIn(expected,
                      self._commands(self._read(CLAUDE_SETTINGS), "SessionStart"))

    # ── what it refuses to do ─────────────────────────────────────────────

    def test_missing_settings_file_is_not_created(self):
        self.assertEqual(self._migrate(), 0)
        self.assertFalse((self.project / CLAUDE_SETTINGS).exists())

    def test_unparseable_settings_are_left_untouched(self):
        path = self.project / CLAUDE_SETTINGS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self._migrate(), 0)
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_gemini_gets_its_tool_hook_and_no_lifecycle_events(self):
        self._write(GEMINI_SETTINGS, {"hooks": {}})
        self.assertEqual(self._migrate(), 1)
        hooks = self._read(GEMINI_SETTINGS)["hooks"]
        self.assertIn("AfterTool", hooks)
        self.assertNotIn("SessionStart", hooks)
        self.assertNotIn("SessionEnd", hooks)

    def test_both_ides_in_one_project_are_one_pass_each(self):
        self._write(CLAUDE_SETTINGS, {"hooks": {}})
        self._write(GEMINI_SETTINGS, {"hooks": {}})
        self.assertEqual(self._migrate(), 2)


class TestMigrateAllProjects(unittest.TestCase):
    def test_every_registered_project_is_visited(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = []
            for name in ("a", "b"):
                path = Path(tmp) / name
                (path / ".claude").mkdir(parents=True)
                (path / ".claude" / "settings.local.json").write_text(
                    "{}", encoding="utf-8")
                projects.append({"path": str(path)})
            pm = mock.Mock()
            pm.list_projects.return_value = projects + [{"path": ""}]
            with mock.patch.object(hub_server, "_pm", return_value=pm):
                hub_server._migrate_project_hooks()
            for project in projects:
                settings = json.loads(
                    (Path(project["path"]) / ".claude" / "settings.local.json")
                    .read_text(encoding="utf-8"))
                self.assertIn("SessionStart", settings["hooks"])

    def test_a_broken_project_list_is_survivable(self):
        pm = mock.Mock()
        pm.list_projects.side_effect = OSError("registry gone")
        with mock.patch.object(hub_server, "_pm", return_value=pm):
            hub_server._migrate_project_hooks()  # must not raise


if __name__ == "__main__":
    unittest.main()
