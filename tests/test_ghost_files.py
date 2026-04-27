"""Ghost-file detector regression tests.

Catches every observed ghost pattern that shell metacharacter misinterpretation
has produced in this project's root: bare type names, version strings,
function-call fragments, partial type annotations, and metacharacter leaks
like '3.0.0`' or 'parseApiResponse(await'.

Previously '3.0.0`' slipped through because Path.suffix('3.0.0`') returns
'.0`', which is a "suffix" to Python but not to a human. The detector now
checks real_suffix = starts-with-letter.
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "cli"))

from hook_ghost_files import _is_ghost_file, scan_ghost_files  # noqa: E402


class TestGhostFileDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make(self, name: str) -> Path:
        p = self.root / name
        p.touch()
        return p

    def _assert_ghost(self, name: str):
        p = self._make(name)
        self.assertTrue(
            _is_ghost_file(p),
            f"Expected {name!r} to be flagged as a ghost but it wasn't"
        )

    def _assert_safe(self, name: str):
        p = self._make(name)
        self.assertFalse(
            _is_ghost_file(p),
            f"Expected {name!r} to be kept but it was flagged as ghost"
        )

    # Observed-in-the-wild cases — all created by bash misinterpreting code fragments.
    def test_bare_integer(self):
        self._assert_ghost("0")
        self._assert_ghost("10")
        self._assert_ghost("80")

    def test_bare_python_type_name(self):
        self._assert_ghost("str")
        self._assert_ghost("dict")

    def test_identifier_like_token(self):
        self._assert_ghost("top_k")

    def test_partial_function_call(self):
        self._assert_ghost("parseApiResponse(await")

    def test_partial_type_annotation(self):
        self._assert_ghost("tuple[float")

    def test_version_number_bare(self):
        # "pip install foo>=3.0.0" with unquoted > can leave "3.0.0" behind
        self._assert_ghost("3.0.0")

    def test_version_number_with_backtick_leak(self):
        # "3.0.0`" — classic escape: Path.suffix returns ".0`", looks like an extension.
        self._assert_ghost("3.0.0`")

    def test_pip_redirect_equals(self):
        self._assert_ghost("=3.0.0")

    def test_heredoc_marker_empty(self):
        # Empty `EOF` / `END` / `'@` were already covered by the size==0
        # extensionless fallback, but pin the behavior so it can't regress.
        self._assert_ghost("EOF")
        self._assert_ghost("END")
        self._assert_ghost("'@")

    def test_heredoc_marker_with_content(self):
        # The actual gap: a HEREDOC end-marker leak can capture body bytes
        # (e.g., `cat <<EOF >EOF\nbody\nEOF` under cmd.exe / Git Bash).
        # Non-empty `EOF` was NOT flagged before the size-agnostic check.
        p = self.root / "EOF"
        p.write_text("leaked body line\n", encoding="utf-8")
        self.assertTrue(_is_ghost_file(p))

    def test_safe_eof_with_extension(self):
        # A real doc named `EOF.md` is not a leak.
        self._assert_safe("EOF.md")

    # Non-ghosts — must NOT be flagged.
    def test_safe_python_file(self):
        self._assert_safe("main.py")

    def test_safe_readme(self):
        self._assert_safe("README.md")

    def test_safe_license_no_extension(self):
        self._assert_safe("LICENSE")

    def test_safe_dockerfile(self):
        self._assert_safe("Dockerfile")

    def test_scan_reports_reasons(self):
        self._make("0")
        self._make("str")
        self._make("3.0.0`")
        self._make("parseApiResponse(await")
        self._make("tuple[float")
        (self.root / "EOF").write_text("body\n", encoding="utf-8")
        scan = scan_ghost_files(self.root)
        by_name = {g["name"]: g for g in scan}
        self.assertEqual(len(scan), 6)
        self.assertIn("0-byte", by_name["0"]["reason"])
        self.assertEqual(by_name["str"]["reason"], "Python type name")
        self.assertEqual(by_name["3.0.0`"]["reason"], "shell metacharacter leak")
        self.assertEqual(by_name["parseApiResponse(await"]["reason"],
                         "partial function-call syntax")
        self.assertEqual(by_name["tuple[float"]["reason"], "partial type annotation")
        self.assertEqual(by_name["EOF"]["reason"], "heredoc end-marker leak")


if __name__ == "__main__":
    unittest.main()
