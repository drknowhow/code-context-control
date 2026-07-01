"""install-mcp writes the c3-mcp entry point (not a source path) when available.

This is what makes upgrades a no-op: with the entry point baked in, `pip install -U`
relocates nothing in any project's .mcp.json. Falls back to the source script when
C3 runs from a checkout with no installed console script.

HOME / USERPROFILE are redirected to a temp dir so the real ~/.claude is never touched.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class TestInstallMcpEntryPoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "proj"
        self.project.mkdir()
        # Redirect home so any global config writes land in the sandbox.
        self._saved_env = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        os.environ["HOME"] = str(self.root)
        os.environ["USERPROFILE"] = str(self.root)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _run_install(self):
        from cli.c3 import cmd_install_mcp
        cmd_install_mcp(SimpleNamespace(
            project_path=str(self.project), ide="claude", mcp_mode="direct",
        ))
        return json.loads((self.project / ".mcp.json").read_text(encoding="utf-8"))

    def test_uses_entry_point_when_available(self):
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_exe = fake_bin / ("c3-mcp.exe" if sys.platform == "win32" else "c3-mcp")
        fake_exe.write_text("", encoding="utf-8")

        with mock.patch("shutil.which", return_value=str(fake_exe)):
            config = self._run_install()

        entry = config["mcpServers"]["c3"]
        self.assertEqual(entry["command"], Path(fake_exe).resolve().as_posix())
        self.assertEqual(entry["args"], ["--project", "."])
        # The source path must NOT leak into the config.
        self.assertNotIn("mcp_server.py", json.dumps(entry))

    def test_falls_back_to_source_when_no_entry_point(self):
        with mock.patch("shutil.which", return_value=None):
            config = self._run_install()

        entry = config["mcpServers"]["c3"]
        self.assertEqual(entry["command"], "python")
        self.assertTrue(entry["args"][0].endswith("mcp_server.py"))
        self.assertEqual(entry["args"][1:], ["--project", "."])

    def test_install_preserves_existing_user_config(self):
        """install-mcp must not clobber the user's .mcp.json or settings.local.json."""
        # Pre-existing user content that must survive install.
        (self.project / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"myserver": {"command": "foo", "args": []}},
            "someTopKey": 123,
        }), encoding="utf-8")
        claude_dir = self.project / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.local.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(mytool:*)"], "deny": []},
            "hooks": {
                "PostToolUse": [
                    {"matcher": "MyCustomTool",
                     "hooks": [{"type": "command", "command": "echo hi"}]}
                ],
                "Stop": [
                    {"matcher": "",
                     "hooks": [{"type": "command", "command": "echo userstop"}]}
                ],
            },
        }), encoding="utf-8")

        with mock.patch("shutil.which", return_value=None):
            self._run_install()

        # .mcp.json: other servers + top-level keys preserved, c3 added.
        mcp = json.loads((self.project / ".mcp.json").read_text(encoding="utf-8"))
        self.assertIn("myserver", mcp["mcpServers"])
        self.assertIn("c3", mcp["mcpServers"])
        self.assertEqual(mcp.get("someTopKey"), 123)

        settings = json.loads((claude_dir / "settings.local.json").read_text(encoding="utf-8"))
        # Plain install (no --permissions) leaves user permissions untouched.
        self.assertIn("Bash(mytool:*)", settings["permissions"]["allow"])
        # User PostToolUse hook (custom matcher) preserved.
        self.assertTrue(
            any(h.get("matcher") == "MyCustomTool" for h in settings["hooks"]["PostToolUse"])
        )
        # User Stop hook (empty matcher) preserved alongside C3's own stop hook.
        stop_cmds = [
            hk.get("command", "")
            for h in settings["hooks"]["Stop"]
            for hk in h.get("hooks", [])
        ]
        self.assertTrue(any("echo userstop" in c for c in stop_cmds))
        # v2.42: single dispatcher entry replaces the per-hook stop commands.
        self.assertTrue(any("hook_dispatch.py" in c and c.endswith(" stop")
                            for c in stop_cmds))

    def test_install_registers_dispatcher_per_event(self):
        """v2.42: hooks route through cli/hook_dispatch.py — one command per
        event — and re-running install-mcp migrates old per-hook entries."""
        claude_dir = self.project / ".claude"
        claude_dir.mkdir()
        # Simulate a pre-v2.42 install: old per-hook commands on C3 matchers.
        (claude_dir / "settings.local.json").write_text(json.dumps({
            "hooks": {
                "PostToolUse": [
                    {"matcher": "Bash", "hooks": [
                        {"type": "command", "command": "python cli/hook_filter.py"},
                        {"type": "command", "command": "python cli/hook_ghost_files.py"},
                    ]},
                ],
                "PreToolUse": [
                    {"matcher": "Read", "hooks": [
                        {"type": "command", "command": "python cli/hook_pretool_enforce.py"},
                    ]},
                ],
                "Stop": [
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": "python cli/hook_session_stats.py"},
                        {"type": "command", "command": "python cli/hook_auto_snapshot.py"},
                    ]},
                ],
            },
        }), encoding="utf-8")

        with mock.patch("shutil.which", return_value=None):
            self._run_install()

        settings = json.loads(
            (claude_dir / "settings.local.json").read_text(encoding="utf-8"))
        hooks = settings["hooks"]

        def _cmds(event):
            return [hk.get("command", "")
                    for h in hooks.get(event, [])
                    for hk in h.get("hooks", [])]

        # Old per-hook commands are gone after migration.
        all_cmds = _cmds("PostToolUse") + _cmds("PreToolUse") + _cmds("Stop")
        for legacy in ("hook_filter.py", "hook_ghost_files.py",
                       "hook_pretool_enforce.py", "hook_session_stats.py",
                       "hook_auto_snapshot.py"):
            self.assertFalse(any(legacy in c for c in all_cmds),
                             f"legacy {legacy} entry survived migration")

        # Every registered command is the dispatcher with the right event arg.
        self.assertTrue(all("hook_dispatch.py" in c and c.endswith(" posttool")
                            for c in _cmds("PostToolUse")))
        self.assertTrue(all("hook_dispatch.py" in c and c.endswith(" pretool")
                            for c in _cmds("PreToolUse")))
        self.assertTrue(all("hook_dispatch.py" in c and c.endswith(" stop")
                            for c in _cmds("Stop")))

        # One spawn per event: each matcher entry carries exactly one command.
        for event in ("PostToolUse", "PreToolUse"):
            for entry in hooks[event]:
                self.assertEqual(len(entry["hooks"]), 1,
                                 f"{event}/{entry.get('matcher')} must have "
                                 f"exactly one dispatcher command")

        # Matcher granularity is preserved (Bash still matched for posttool,
        # Read/Edit/Write still matched for pretool).
        post_matchers = {h.get("matcher") for h in hooks["PostToolUse"]}
        pre_matchers = {h.get("matcher") for h in hooks["PreToolUse"]}
        self.assertIn("Bash", post_matchers)
        self.assertIn("mcp__c3__c3_read", post_matchers)
        for m in ("Read", "Grep", "Glob", "Edit", "Write"):
            self.assertIn(m, pre_matchers)


if __name__ == "__main__":
    unittest.main()
