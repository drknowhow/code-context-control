"""Launch-command overrides: the hub can start an agent through a wrapper.

`claude-code` means "the Claude Code CLI", not literally the binary named
`claude` — a box that launches it as `yep` must be able to say so once, per
project or hub-wide, and have every launch surface honour it.
"""
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class TestResolveIdeCmd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cli import hub_server
        cls.mod = hub_server

    def _resolve(self, ide, project_cmd="", hub_cmds=None, custom_cmd=""):
        pm = mock.Mock()
        pm.get_ide_cmd.return_value = project_cmd
        cfg = dict(self.mod._HUB_CONFIG_DEFAULTS)
        cfg["ide_cmds"] = hub_cmds or {}
        with mock.patch.object(self.mod, "_pm", return_value=pm), \
             mock.patch.object(self.mod, "_read_hub_config", return_value=cfg):
            return self.mod._resolve_ide_cmd(ide, "C:/proj", custom_cmd)

    def test_default_is_the_stock_command(self):
        self.assertEqual(self._resolve("claude-code"), ("claude", False, ""))

    def test_hub_override_replaces_the_binary(self):
        self.assertEqual(
            self._resolve("claude-code", hub_cmds={"claude-code": "yep"}),
            ("yep", False, ""))

    def test_project_override_beats_hub_override(self):
        cmd, _, _ = self._resolve("claude-code", project_cmd="yep --resume",
                                  hub_cmds={"claude-code": "yep"})
        self.assertEqual(cmd, "yep --resume")

    def test_explicit_custom_cmd_beats_everything(self):
        cmd, _, _ = self._resolve("claude-code", project_cmd="yep",
                                  hub_cmds={"claude-code": "nope"},
                                  custom_cmd="claude --continue")
        self.assertEqual(cmd, "claude --continue")

    def test_override_keeps_the_launch_style(self):
        # A wrapper for a GUI editor is still launched with the path argument.
        self.assertEqual(self._resolve("vscode", project_cmd="code-insiders"),
                         ("code-insiders", True, ""))
        self.assertEqual(self._resolve("codex", project_cmd="my-codex")[1], False)

    def test_unknown_ide_is_an_error(self):
        _, _, err = self._resolve("emacs-but-no")
        self.assertIn("Unknown IDE", err)

    def test_custom_without_a_command_is_an_error(self):
        _, _, err = self._resolve("custom")
        self.assertIn("custom_cmd is required", err)

    def test_custom_uses_the_stored_project_command(self):
        self.assertEqual(self._resolve("custom", project_cmd="nvim .")[0], "nvim .")


class TestIdeCmdsConfig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cli import hub_server
        cls.mod = hub_server
        cls.client = hub_server.app.test_client()

    def test_ide_cmds_must_be_an_object(self):
        resp = self.client.post("/api/hub/config", json={"ide_cmds": "yep"})
        self.assertEqual(resp.status_code, 400)

    def test_blank_command_clears_the_override(self):
        written = {}
        with mock.patch.object(self.mod, "_write_hub_config", written.update):
            resp = self.client.post("/api/hub/config",
                                    json={"ide_cmds": {"claude-code": "yep",
                                                       "codex": "   "}})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(written.get("ide_cmds"), {"claude-code": "yep"})


class TestProjectIdeCmdField(unittest.TestCase):
    """The per-project override must survive a write/read round trip."""

    def test_update_and_read_back(self):
        import tempfile

        from services import project_manager as pmod

        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            with mock.patch.object(pmod, "_PROJECTS_FILE", Path(tmp) / "projects.json"):
                pm = pmod.ProjectManager()
                pm.add_project(str(proj), name="proj")
                self.assertEqual(pm.get_ide_cmd(str(proj)), "")
                self.assertTrue(pm.update_project(str(proj), ide_cmd="yep"))
                self.assertEqual(pm.get_ide_cmd(str(proj)), "yep")
                pm.update_project(str(proj), ide_cmd="")
                self.assertEqual(pm.get_ide_cmd(str(proj)), "")

    def test_unknown_fields_are_still_rejected(self):
        import tempfile

        from services import project_manager as pmod

        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "proj"
            proj.mkdir()
            with mock.patch.object(pmod, "_PROJECTS_FILE", Path(tmp) / "projects.json"):
                pm = pmod.ProjectManager()
                pm.add_project(str(proj), name="proj")
                pm.update_project(str(proj), evil="rm -rf /")
                self.assertNotIn("evil", pm._read_projects()[0])


if __name__ == "__main__":
    unittest.main()
