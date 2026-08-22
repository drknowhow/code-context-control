"""``c3 init --clear`` / Wipe and ``_uninstall_mcp_all`` take out what C3 wrote — and only that.

Field report, 2026-08-22 (ISSUE-5 / ISSUE-6): ``--clear`` unlinked CLAUDE.md
and AGENTS.md outright, destroying another tool's ``<!-- YEP:BEGIN -->`` block
that shared the file; and the settings cleanup printed "Removed C3
hooks/settings" while 66 ``mcp__c3`` / ``hook_dispatch`` entries were still
in the file, because it only looked at three PostToolUse matchers from the
pre-v2.42 layout.
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.claude_md import (  # noqa: E402
    C3_BLOCK_BEGIN,
    C3_BLOCK_END,
    C3_LEGACY_FIRST_LINE,
    strip_c3_block,
    wrap_c3_block,
)

C3_BODY = C3_LEGACY_FIRST_LINE + "\nUse c3_* tools.\n\n# Project Context\nstuff"
YEP_BLOCK = "<!-- YEP:BEGIN -->\n# Yep\nYep's own rules live here.\n<!-- YEP:END -->"
DISPATCH = '"C:/py/python.exe" "C:/site-packages/code_context_control/cli/hook_dispatch.py"'


def _installed_settings() -> dict:
    """The shape ``c3 install-mcp`` writes, plus things a user added."""
    def grp(matcher, cmd):
        return {"matcher": matcher, "hooks": [{"type": "command", "command": cmd}]}

    return {
        "permissions": {
            "allow": ["mcp__c3__c3_read", "mcp__c3__c3_search", "mcp__c3__c3_edit",
                      "Bash(git:*)", "Read"],
            "deny": ["mcp__c3__c3_shell"],
        },
        "enabledMcpjsonServers": ["c3", "other-server"],
        "hooks": {
            "PreToolUse": [
                grp("Read", DISPATCH + " pretool"),
                grp("Edit", DISPATCH + " pretool"),
                grp("Write", DISPATCH + " pretool"),
                grp("Bash", DISPATCH + " pretool"),
                grp("Bash", "python my_own_guard.py"),
            ],
            "PostToolUse": [
                grp("Bash", DISPATCH + " posttool"),
                grp("mcp__c3__c3_edit", DISPATCH + " posttool"),
                grp("Write", DISPATCH + " posttool"),
            ],
            "Stop": [
                grp("", DISPATCH + " stop"),
                grp("", "python user_stop_hook.py"),
            ],
            "UserPromptSubmit": [grp("", DISPATCH + " prompt")],
        },
        "somethingElse": {"keep": True},
    }


class TestStripC3Block(unittest.TestCase):
    def test_markers_with_other_content_keeps_the_rest(self):
        doc = "# Mine\n\n" + wrap_c3_block(C3_BODY) + "\n\n" + YEP_BLOCK + "\n"
        out = strip_c3_block(doc)
        self.assertNotIn(C3_BLOCK_BEGIN, out)
        self.assertNotIn(C3_BLOCK_END, out)
        self.assertNotIn("Use c3_* tools", out)
        self.assertIn("# Mine", out)
        self.assertIn(YEP_BLOCK, out)
        self.assertTrue(out.endswith("\n"))

    def test_block_alone_returns_empty(self):
        self.assertEqual(strip_c3_block(wrap_c3_block(C3_BODY) + "\n"), "")

    def test_legacy_doc_keeps_user_notes(self):
        doc = C3_BODY + "\n\n# User Notes\nkeep me\n"
        self.assertEqual(strip_c3_block(doc), "# User Notes\nkeep me\n")

    def test_legacy_doc_without_notes_is_empty(self):
        self.assertEqual(strip_c3_block(C3_BODY + "\n"), "")

    def test_user_authored_file_is_not_ours(self):
        self.assertIsNone(strip_c3_block("# My project\n\nMy rules.\n"))

    def test_out_of_order_markers_are_left_alone(self):
        doc = C3_BLOCK_END + "\nmess\n" + C3_BLOCK_BEGIN + "\n"
        self.assertIsNone(strip_c3_block(doc))


class TestRemoveInstructionDocs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        from cli.c3 import _remove_c3_instruction_docs
        self.remove = _remove_c3_instruction_docs

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.remove(str(self.tmp))
        return buf.getvalue()

    def test_shared_file_keeps_the_other_tools_block(self):
        (self.tmp / "CLAUDE.md").write_text(
            YEP_BLOCK + "\n\n" + wrap_c3_block(C3_BODY) + "\n", encoding="utf-8")
        out = self._run()
        text = (self.tmp / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn(YEP_BLOCK, text)
        self.assertNotIn(C3_BLOCK_BEGIN, text)
        self.assertIn("Removed C3 block from CLAUDE.md", out)
        self.assertNotIn("Deleted CLAUDE.md", out)

    def test_c3_only_file_is_deleted(self):
        (self.tmp / "AGENTS.md").write_text(wrap_c3_block(C3_BODY) + "\n", encoding="utf-8")
        out = self._run()
        self.assertFalse((self.tmp / "AGENTS.md").exists())
        self.assertIn("Deleted AGENTS.md", out)

    def test_user_authored_file_is_kept(self):
        (self.tmp / "GEMINI.md").write_text("# Not C3's\nhands off\n", encoding="utf-8")
        out = self._run()
        self.assertEqual((self.tmp / "GEMINI.md").read_text(encoding="utf-8"), "# Not C3's\nhands off\n")
        self.assertIn("Kept GEMINI.md", out)

    def test_missing_files_are_fine(self):
        self.assertEqual(self._run().strip(), "")


class TestStripC3FromSettings(unittest.TestCase):
    def setUp(self):
        from cli.c3 import _c3_references_in_settings, _strip_c3_from_settings
        self.refs = _c3_references_in_settings
        self.strip = _strip_c3_from_settings

    def test_references_see_every_event_and_the_permissions(self):
        refs = self.refs(_installed_settings())
        for event in ("PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit"):
            self.assertIn(f"hooks.{event}", refs)
        self.assertIn("permissions.allow:mcp__c3__c3_edit", refs)
        self.assertIn("permissions.deny:mcp__c3__c3_shell", refs)
        self.assertIn("enabledMcpjsonServers:c3", refs)

    def test_removes_every_c3_entry_and_keeps_the_rest(self):
        s = self.strip(_installed_settings())
        self.assertEqual(self.refs(s), [])
        self.assertNotIn("mcp__c3", json.dumps(s))
        self.assertNotIn("hook_dispatch", json.dumps(s))
        # User content survives untouched.
        self.assertEqual(s["permissions"]["allow"], ["Bash(git:*)", "Read"])
        self.assertNotIn("deny", s["permissions"])
        self.assertEqual(s["enabledMcpjsonServers"], ["other-server"])
        self.assertEqual(s["hooks"]["PreToolUse"],
                         [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python my_own_guard.py"}]}])
        self.assertEqual(s["hooks"]["Stop"],
                         [{"matcher": "", "hooks": [{"type": "command", "command": "python user_stop_hook.py"}]}])
        self.assertNotIn("PostToolUse", s["hooks"])
        self.assertNotIn("UserPromptSubmit", s["hooks"])
        self.assertEqual(s["somethingElse"], {"keep": True})

    def test_file_c3_created_alone_becomes_empty(self):
        s = _installed_settings()
        s["permissions"]["allow"] = ["mcp__c3__c3_read"]
        del s["somethingElse"]
        s["enabledMcpjsonServers"] = ["c3"]
        for event in s["hooks"]:
            s["hooks"][event] = [g for g in s["hooks"][event] if "hook_dispatch" in g["hooks"][0]["command"]]
        self.assertEqual(self.strip(s), {})

    def test_server_wide_and_wildcard_rules_count_as_c3(self):
        s = {"permissions": {"allow": ["mcp__c3", "mcp__c3__*", "mcp__other__tool"]}}
        self.assertEqual(self.strip(s), {"permissions": {"allow": ["mcp__other__tool"]}})


class TestUninstallSettingsPostcondition(unittest.TestCase):
    """End to end: after ``_uninstall_mcp_all`` the file on disk has no C3 in it,
    and the printed summary says so only because it re-read the file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proj"
        (self.proj / ".claude").mkdir(parents=True)
        (self.proj / ".c3").mkdir()
        self.settings = self.proj / ".claude" / "settings.local.json"
        self.settings.write_text(json.dumps(_installed_settings(), indent=2), encoding="utf-8")
        (self.proj / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"c3": {"command": "c3-mcp"}, "other": {"command": "x"}}}),
            encoding="utf-8")
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_settings_file_is_clean_and_the_report_is_true(self):
        from cli.c3 import _uninstall_mcp_all
        buf = io.StringIO()
        with mock.patch("pathlib.Path.home", return_value=self.fake_home), redirect_stdout(buf):
            _uninstall_mcp_all(str(self.proj), include_global=False)
        out = buf.getvalue()
        raw = self.settings.read_text(encoding="utf-8")
        self.assertEqual(raw.count("mcp__c3"), 0, raw)
        self.assertEqual(raw.count("hook_dispatch"), 0, raw)
        self.assertIn("Removed C3 hooks/settings from", out)
        self.assertNotIn("[!] C3 entries remain", out)
        on_disk = json.loads(raw)
        self.assertIn("python my_own_guard.py", json.dumps(on_disk))
        self.assertEqual(on_disk["permissions"]["allow"], ["Bash(git:*)", "Read"])
        mcp = json.loads((self.proj / ".mcp.json").read_text(encoding="utf-8"))
        self.assertNotIn("c3", mcp["mcpServers"])
        self.assertIn("other", mcp["mcpServers"])

    def test_nothing_to_do_is_reported_as_such(self):
        from cli.c3 import _uninstall_mcp_all
        self.settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}}), encoding="utf-8")
        buf = io.StringIO()
        with mock.patch("pathlib.Path.home", return_value=self.fake_home), redirect_stdout(buf):
            _uninstall_mcp_all(str(self.proj), include_global=False)
        self.assertIn("No C3 entries in", buf.getvalue())
        self.assertEqual(json.loads(self.settings.read_text(encoding="utf-8")),
                         {"permissions": {"allow": ["Read"]}})


if __name__ == "__main__":
    unittest.main()
