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
        self.assertTrue(any("hook_auto_snapshot.py" in c for c in stop_cmds))


if __name__ == "__main__":
    unittest.main()
