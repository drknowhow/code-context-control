"""cli._shell_writes — which files does a shell command probably write?

Field report 2026-08-22, ISSUE-1's buried finding: Bash was the one tool
discipline never looked at. The extractor feeds both the PreToolUse nudge
and the after-the-fact ledger row, so it must be conservative: a missed
target is a missing hint, a false target is a misleading one.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli._shell_writes import shell_write_targets  # noqa: E402

# A base WITHOUT shell-syntax characters: the repo path itself contains
# "(C3)", and parentheses are exactly what the extractor refuses to treat
# as part of a path (a false target costs a misleading hint).
BASE = str(Path(tempfile.mkdtemp()).resolve())


def _rel(paths):
    return [os.path.relpath(p, BASE).replace("\\", "/") for p in paths]


class TestTargets(unittest.TestCase):
    def t(self, cmd):
        return _rel(shell_write_targets(cmd, BASE))

    def test_redirects(self):
        self.assertEqual(self.t("echo hi > a.txt"), ["a.txt"])
        self.assertEqual(self.t("echo hi >> log/b.log"), ["log/b.log"])
        self.assertEqual(self.t("cat > dir/b.py <<'EOF'\nprint(1)\nEOF"), ["dir/b.py"])
        self.assertEqual(self.t("python - <<EOF > out.log\nx\nEOF"), ["out.log"])
        self.assertEqual(self.t('echo > "my file.txt"'), ["my file.txt"])

    def test_tee_cp_mv_touch_rm_sed(self):
        self.assertEqual(self.t("ls | tee -a log.txt"), ["log.txt"])
        self.assertEqual(self.t("cp a.py b.py"), ["b.py"])
        self.assertEqual(self.t("mv src/a.py src/b.py"), ["src/b.py"])
        self.assertEqual(self.t("touch new.py old.py"), ["new.py", "old.py"])
        self.assertEqual(self.t("rm -f gone.py"), ["gone.py"])
        self.assertEqual(self.t("sed -i 's/x/y/' f.py"), ["f.py"])
        self.assertEqual(self.t("sed -n '1,5p' f.py"), [])  # not in-place: a read

    def test_python_inline_writes(self):
        self.assertEqual(self.t("python -c \"open('gen.py','w').write('x')\""), ["gen.py"])
        self.assertEqual(self.t("python3 -c \"open('a.txt', 'a').write('x')\""), ["a.txt"])
        self.assertEqual(self.t("python -c \"open('r.txt').read()\""), [])  # read mode
        self.assertEqual(self.t("python -c \"from pathlib import Path; Path('w.md').write_text('x')\""), ["w.md"])

    def test_not_writes(self):
        self.assertEqual(self.t("ls -la"), [])
        self.assertEqual(self.t("pip install foo>=3.0.0"), [])
        self.assertEqual(self.t("cmd 2>&1 >/dev/null"), [])
        self.assertEqual(self.t("grep -rn x . | head"), [])
        self.assertEqual(self.t("git log --oneline"), [])
        self.assertEqual(self.t("echo $HOME > $OUT"), [])  # syntax token skipped

    def test_cd_moves_the_ground(self):
        self.assertEqual(self.t("cd sub && echo > c.txt"), ["sub/c.txt"])
        self.assertEqual(self.t("cd sub; touch d.py; cd .. && touch e.py"), ["sub/d.py", "e.py"])

    def test_absolute_and_dedup_and_cap(self):
        absolute = str(Path(BASE) / "abs.py")
        self.assertEqual(shell_write_targets(f'echo > "{absolute}" && echo >> "{absolute}"', BASE), [absolute])
        many = " && ".join(f"touch f{i}.py" for i in range(40))
        self.assertEqual(len(shell_write_targets(many, BASE)), 20)

    def test_never_raises(self):
        self.assertEqual(shell_write_targets("", BASE), [])
        self.assertEqual(shell_write_targets(None, BASE), [])
        self.assertIsInstance(shell_write_targets(">>> >> > ;;; &&", BASE), list)


if __name__ == "__main__":
    unittest.main()
