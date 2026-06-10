"""c3_shell robustness tests: ghost-file self-sweep, forced-UTF-8 child env,
and the git-diagnostic filter guard (cli/tools/shell.py)."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.tools import shell as shell_mod  # noqa: E402


class _Svc:
    """Minimal svc stub: project_path + no activity log."""

    def __init__(self, root):
        self.project_path = str(root)
        self.activity_log = None


def _finalize(name, args, resp, summ="", **kw):
    return resp


class TestPopenUtf8(unittest.TestCase):
    def test_forces_utf8_env(self):
        kw = shell_mod._popen_kwargs()
        self.assertEqual(kw["env"].get("PYTHONUTF8"), "1")
        self.assertEqual(kw["env"].get("PYTHONIOENCODING"), "utf-8")

    def test_unicode_output_does_not_crash(self):
        # A child printing non-cp1252 chars (→) must not crash on Windows.
        r = shell_mod._run_sync('python -c "print(chr(0x2192))"', ".", 30)
        self.assertEqual(r["exit_code"], 0)


class TestGitDiagnosticFilter(unittest.TestCase):
    def test_matches_diagnostics(self):
        for c in ["git status", "  git diff --stat", "git log --oneline",
                  "git show HEAD", "git branch -a", "git stash list"]:
            self.assertTrue(shell_mod._GIT_DIAGNOSTIC.search(c), c)

    def test_ignores_mutations_and_others(self):
        for c in ["git commit -m x", "git add .", "git reset --hard",
                  "python -c print", "gitk"]:
            self.assertFalse(shell_mod._GIT_DIAGNOSTIC.search(c), c)


class TestGhostSweep(unittest.TestCase):
    def test_new_ghost_swept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = shell_mod._list_root_files(root)
            (root / "L170").write_text("")  # 0-byte redirect-artifact ghost
            swept = shell_mod._sweep_new_ghost_files(root, before)
            self.assertIn("L170", swept)
            self.assertFalse((root / "L170").exists())

    def test_preexisting_ghost_not_swept(self):
        # Only files that appear DURING the command are removed.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "preexisting").write_text("")
            before = shell_mod._list_root_files(root)  # includes it
            swept = shell_mod._sweep_new_ghost_files(root, before)
            self.assertEqual(swept, [])
            self.assertTrue((root / "preexisting").exists())

    def test_real_file_not_swept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = shell_mod._list_root_files(root)
            (root / "real.py").write_text("x = 1\n")  # real extension + content
            swept = shell_mod._sweep_new_ghost_files(root, before)
            self.assertEqual(swept, [])
            self.assertTrue((root / "real.py").exists())

    def test_handle_shell_sweeps_created_ghost(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _Svc(tmp)
            cmd = "python -c \"import pathlib; pathlib.Path('Lghost9').touch()\""
            out = asyncio.run(shell_mod.handle_shell(
                cmd, tmp, 10, True, False, svc, _finalize))
            self.assertIn("ghost-sweep", out)
            self.assertFalse((Path(tmp) / "Lghost9").exists())


if __name__ == "__main__":
    unittest.main()
