"""Shell file writes reach the edit ledger after the fact (ISSUE-1).

hook_edit_ledger gets a Bash branch: files the command probably wrote
(cli._shell_writes) that exist, are editable, and changed within the last
two minutes become change_type "shell" rows carrying the command. The
dispatcher routes Bash PostToolUse to it after the ghost-file sweep.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

from cli import hook_edit_ledger  # noqa: E402
from cli.hook_dispatch import _routes  # noqa: E402


class _Project(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        self.ledger = self.tmp / ".c3" / "edit_ledger.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bash(self, cmd, session="s1"):
        return hook_edit_ledger.run({"tool_name": "Bash", "session_id": session,
                                     "tool_input": {"command": cmd}, "tool_response": ""}, self.tmp)

    def _rows(self):
        if not self.ledger.exists():
            return []
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestShellRows(_Project):
    def test_fresh_written_file_is_logged_as_shell(self):
        (self.tmp / "gen.py").write_text("print(1)\n", encoding="utf-8")
        out = self._bash("python -c \"from pathlib import Path; Path('gen.py').write_text('print(1)')\"")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["file"], row["change_type"]), ("gen.py", "shell"))
        self.assertIn("shell", row["tags"])
        self.assertIn("python -c", row["summary"])
        self.assertEqual(row["session_id"], "s1")
        self.assertIn("gen.py", out["_text"])
        self.assertIn("no pre-edit snapshot", out["_text"])

    def test_heredoc_and_redirect(self):
        (self.tmp / "docs").mkdir()
        (self.tmp / "docs" / "n.md").write_text("# n\n", encoding="utf-8")
        self._bash("cat > docs/n.md <<'EOF'\n# n\nEOF")
        self.assertEqual([r["file"] for r in self._rows()], ["docs/n.md"])

    def test_named_but_not_written_is_not_logged(self):
        stale = self.tmp / "old.py"
        stale.write_text("x", encoding="utf-8")
        hour_ago = time.time() - 3600
        os.utime(stale, (hour_ago, hour_ago))
        self.assertIsNone(self._bash("touch old.py"))
        self.assertIsNone(self._bash("echo > missing.py"))   # never created
        self.assertIsNone(self._bash("rm -f gone.py"))       # nothing to see
        self.assertEqual(self._rows(), [])

    def test_read_only_command_is_silent(self):
        (self.tmp / "a.py").write_text("x", encoding="utf-8")
        self.assertIsNone(self._bash("grep -n x a.py && ls -la"))
        self.assertEqual(self._rows(), [])

    def test_non_editable_extension_skipped(self):
        (self.tmp / "blob.bin").write_bytes(b"\x00")
        self.assertIsNone(self._bash("cp x blob.bin"))

    def test_not_a_c3_project(self):
        shutil.rmtree(self.tmp / ".c3")
        (self.tmp / "a.py").write_text("x", encoding="utf-8")
        self.assertIsNone(self._bash("echo > a.py"))

    def test_versions_advance(self):
        (self.tmp / "v.py").write_text("x", encoding="utf-8")
        self._bash("echo > v.py")
        self._bash("echo >> v.py")
        self.assertEqual([r["version"] for r in self._rows()], ["v1", "v2"])


class TestDispatcherRoute(unittest.TestCase):
    def test_bash_posttool_reaches_ledger_after_ghost_sweep(self):
        mods = list(_routes("posttool", "Bash", "Bash"))
        self.assertIn("hook_edit_ledger", mods)
        self.assertLess(mods.index("hook_ghost_files"), mods.index("hook_edit_ledger"))

    def test_edit_tools_unchanged(self):
        mods = list(_routes("posttool", "Edit", "Edit"))
        self.assertIn("hook_edit_ledger", mods)
        self.assertIn("hook_artifact", mods)


if __name__ == "__main__":
    unittest.main()
