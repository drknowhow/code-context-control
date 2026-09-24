"""c3_edit success responses: the compact diff and git-worktree awareness."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.tools import _edit_report  # noqa: E402
from cli.tools.edit import handle_edit  # noqa: E402


def _make_svc(project_path: Path):
    svc = MagicMock()
    svc.project_path = str(project_path)
    svc.edit_ledger = None
    svc.activity_log = None
    svc.session_mgr = None
    return svc


def _finalize(name, args, resp, summ, **kw):
    return resp


def _edit(svc, file_path, old, new, edits=""):
    return handle_edit(file_path, old, new, "", "", False, svc, _finalize, edits)


class TestDiffInResponse(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.svc = _make_svc(self.tmp)
        self.f = self.tmp / "a.py"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_edit_shows_hunk_with_real_line_numbers(self):
        self.f.write_bytes(b"one\r\ntwo\r\nthree\r\nfour\r\nfive\r\nsix\r\n")
        resp = _edit(self.svc, str(self.f), "five", "FIVE")
        lines = resp.split("\n")
        self.assertTrue(lines[0].startswith("✓ a.py [-1+1L]"))
        self.assertIn("  @@ -3,4 +3,4 @@", lines)
        self.assertIn("  -five", lines)
        self.assertIn("  +FIVE", lines)
        self.assertNotIn("\r", resp)
        self.assertFalse(any(ln.startswith(("  ---", "  +++")) for ln in lines))

    def test_batch_edit_shows_diff(self):
        self.f.write_text("x = 1\ny = 2\n", encoding="utf-8")
        edits = json.dumps([{"old_string": "x = 1", "new_string": "x = 10"}])
        resp = _edit(self.svc, str(self.f), "", "", edits=edits)
        self.assertIn("1/1 patches applied", resp.split("\n")[0])
        self.assertIn("  +x = 10", resp)

    def test_long_diff_is_capped_with_remainder_count(self):
        self.f.write_text("".join(f"line{i}\n" for i in range(100)), encoding="utf-8")
        new = "".join(f"LINE{i}\n" for i in range(100))
        resp = _edit(self.svc, str(self.f), self.f.read_text(encoding="utf-8"), new)
        diff_lines = [ln for ln in resp.split("\n")[1:] if ln.startswith("  ")]
        self.assertEqual(len(diff_lines), _edit_report.DIFF_MAX_LINES + 1)
        self.assertRegex(diff_lines[-1], r"… \d+ more diff lines$")
        self.assertLessEqual(len(resp), _edit_report.DIFF_MAX_CHARS + 200)

    def test_show_diff_false_leaves_only_the_summary_line(self):
        (self.tmp / ".c3").mkdir()
        (self.tmp / ".c3" / "config.json").write_text(
            json.dumps({"edit": {"show_diff": False}}), encoding="utf-8")
        self.f.write_text("a\nb\n", encoding="utf-8")
        resp = _edit(self.svc, str(self.f), "b", "c")
        self.assertEqual(resp, "✓ a.py [-1+1L]")

    def test_create_mode_has_no_diff(self):
        resp = _edit(self.svc, str(self.tmp / "new.py"), "", "print(1)\n")
        self.assertEqual(resp, "✓ new.py [created, +2L]")

    def test_undecodable_bytes_render_as_replacement_char(self):
        self.f.write_bytes(b"k = '\xff'\nv = 1\n")
        resp = _edit(self.svc, str(self.f), "v = 1", "v = 2")
        resp.encode("utf-8")
        self.assertIn("�", resp)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TestWorktreeAwareness(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.main = self.base / "main"
        self.main.mkdir()
        self.wt = self.base / "wt"
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        (self.main / "shared.py").write_text("a = 1\n", encoding="utf-8")
        for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "init"],
                    ["worktree", "add", "-q", str(self.wt)]):
            subprocess.run(git + cmd, cwd=self.main, check=True,
                           capture_output=True, stdin=subprocess.DEVNULL)
        (self.wt / "only_wt.py").write_text("b = 1\n", encoding="utf-8")
        _edit_report._worktree_cache.clear()
        self.svc = _make_svc(self.main)

    def tearDown(self):
        _edit_report._worktree_cache.clear()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_relative_path_warns_and_names_worktree_copy(self):
        resp = _edit(self.svc, "shared.py", "a = 1", "a = 2")
        self.assertTrue(resp.startswith("✓ shared.py [-1+1L]"))
        self.assertIn(str((self.main / "shared.py").resolve()), resp)
        self.assertIn(str((self.wt / "shared.py").resolve()), resp)
        self.assertIn("Pass an absolute path", resp)
        self.assertEqual((self.wt / "shared.py").read_text(encoding="utf-8"), "a = 1\n")

    def test_absolute_path_has_no_worktree_warning(self):
        resp = _edit(self.svc, str(self.main / "shared.py"), "a = 1", "a = 2")
        self.assertNotIn("⚠", resp)
        self.assertNotIn(str(self.wt.resolve()), resp)

    def test_relative_path_only_in_worktree_is_refused(self):
        resp = _edit(self.svc, "only_wt.py", "b = 1", "b = 2")
        self.assertIn("[c3_edit:wrong-tree]", resp)
        self.assertIn(str((self.wt / "only_wt.py").resolve()), resp)
        self.assertEqual((self.wt / "only_wt.py").read_text(encoding="utf-8"), "b = 1\n")

    def test_relative_create_only_in_worktree_is_refused(self):
        resp = _edit(self.svc, "only_wt.py", "", "c = 1\n")
        self.assertIn("[c3_edit:wrong-tree]", resp)
        self.assertFalse((self.main / "only_wt.py").exists())

    def test_relative_create_warns_it_landed_in_main_checkout(self):
        resp = _edit(self.svc, "brand_new.py", "", "d = 1\n")
        self.assertTrue(resp.startswith("✓ brand_new.py [created"))
        self.assertIn("created in the MAIN checkout", resp)
        self.assertTrue((self.main / "brand_new.py").exists())


if __name__ == "__main__":
    unittest.main()
