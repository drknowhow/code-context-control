"""c3_edit matching: exact text applies, a lookalike-only match is refused."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.tools.edit import (  # noqa: E402
    _LOOKALIKE_TRANS,
    _apply_replacement,
    _norm,
    handle_edit,
)


def _make_svc(project_path: Path):
    svc = MagicMock()
    svc.project_path = str(project_path)
    svc.edit_ledger = None          # skip ledger side-effects
    svc.activity_log = None
    svc.session_mgr = None
    return svc


def _finalize(name, args, resp, summ, **kw):
    # Return resp unchanged so tests can assert on its contents.
    return resp


class TestLookalikeTable(unittest.TestCase):
    def test_table_is_one_to_one(self):
        for src, dst in _LOOKALIKE_TRANS.items():
            self.assertIsInstance(dst, str)
            self.assertEqual(len(dst), 1,
                             f"lookalike for U+{src:04X} must be a single char")

    def test_norm_preserves_length(self):
        samples = [
            "he said “hi” and ‘bye’",
            "em—dash and en–dash",
            "nbsp inside",
            "plain ascii text",
        ]
        for s in samples:
            self.assertEqual(len(_norm(s)), len(s))


class TestApplyReplacement(unittest.TestCase):
    def test_direct_match_no_fallback(self):
        out, count, fb = _apply_replacement("foo bar baz", "bar", "BAR", False)
        self.assertEqual(out, "foo BAR baz")
        self.assertEqual(count, 1)
        self.assertFalse(fb)

    def test_lookalike_only_match_is_not_applied(self):
        cases = [
            ('x = “hello”', 'x = "hello"'),
            ("it’s fine", "it's fine"),
            ("use --force—really", "force-really"),
            ("hello world", "hello world"),
        ]
        for content, old in cases:
            with self.subTest(content=content):
                self.assertEqual(_apply_replacement(content, old, "X", False),
                                 (None, 1, True))

    def test_exact_typographic_old_string_applies(self):
        out, count, lookalike = _apply_replacement(
            "say “hi” — now", "“hi” — now", "“bye” — now", False)
        self.assertEqual(out, "say “bye” — now")
        self.assertFalse(lookalike)

    def test_undecodable_byte_matches_its_replacement_char(self):
        content = b"caf\xe9 = 1".decode("utf-8", errors="surrogateescape")
        out, count, lookalike = _apply_replacement(
            content, "caf� = 1", "cafe = 1", False)
        self.assertEqual(out, "cafe = 1")
        self.assertFalse(lookalike)

    def test_not_found_returns_none(self):
        out, count, fb = _apply_replacement("foo bar", "qux", "QUX", False)
        self.assertIsNone(out)
        self.assertEqual(count, 0)
        self.assertFalse(fb)

    def test_ambiguous_direct(self):
        out, count, fb = _apply_replacement("ab ab ab", "ab", "AB", False)
        self.assertIsNone(out)
        self.assertEqual(count, 3)
        self.assertFalse(fb)

    def test_replace_all_does_not_apply_lookalike_matches(self):
        self.assertEqual(
            _apply_replacement("“hi” “hi”", '"hi"', "X", True),
            (None, 2, True))

    def test_no_lookalikes_no_false_positive(self):
        # Both sides pure ASCII, no match — must not silently succeed.
        out, count, fb = _apply_replacement("plain text", "absent", "X", False)
        self.assertIsNone(out)
        self.assertEqual(count, 0)
        self.assertFalse(fb)


class TestHandleEditIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = _make_svc(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_lookalike_edit_is_refused_with_the_file_text(self):
        self._write("a.py", "x = 1\nmsg = “hello” — ok\n")
        resp = handle_edit(
            "a.py", 'msg = "hello" - ok', 'msg = "HELLO" - ok',
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
        )
        self.assertTrue(resp.startswith("[c3_edit:lookalike]"))
        self.assertIn("⟦L2-L2⟧\nmsg = “hello” — ok\n⟦end⟧", resp)
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"),
                         "x = 1\nmsg = “hello” — ok\n")

    def test_still_reports_not_found_when_no_match(self):
        self._write("a.py", "msg = “hello”\n")
        resp = handle_edit(
            "a.py", 'msg = "nope"', 'msg = "X"',
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
        )
        self.assertIn("not found", resp)

    def test_batch_lookalike_patch_is_not_applied(self):
        self._write("b.py", "a = ‘one’\nb = 2\n")
        resp = handle_edit(
            "b.py", "", "",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"a = \'one\'","new_string":"a = \'ONE\'"}]',
        )
        self.assertIn("patch[0]: LOOKALIKE ONLY", resp)
        self.assertIn("⟦L1-L1⟧\na = ‘one’\n⟦end⟧", resp)
        self.assertEqual((self.root / "b.py").read_text(encoding="utf-8"),
                         "a = ‘one’\nb = 2\n")


class TestNewlinePreservation(unittest.TestCase):
    """Regression: on Windows, read_text()+write_text() round-trips line
    endings through os.linesep, rewriting an entire LF-only file to CRLF
    after a one-line edit. The fix detects + preserves the original EOL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = _make_svc(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_bytes(self, rel: str, data: bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_single_edit_preserves_lf(self):
        self._write_bytes("lf.txt", b"a\nb\nc\n")
        handle_edit(
            "lf.txt", "b", "B",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
        )
        self.assertEqual((self.root / "lf.txt").read_bytes(), b"a\nB\nc\n")

    def test_single_edit_preserves_crlf(self):
        self._write_bytes("crlf.txt", b"a\r\nb\r\nc\r\n")
        handle_edit(
            "crlf.txt", "b", "B",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
        )
        self.assertEqual((self.root / "crlf.txt").read_bytes(), b"a\r\nB\r\nc\r\n")

    def test_batch_edit_preserves_lf(self):
        self._write_bytes("lf2.txt", b"a\nb\nc\n")
        handle_edit(
            "lf2.txt", "", "",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"a","new_string":"A"},'
                  '{"old_string":"c","new_string":"C"}]',
        )
        self.assertEqual((self.root / "lf2.txt").read_bytes(), b"A\nb\nC\n")

    def test_batch_edit_preserves_crlf(self):
        self._write_bytes("crlf2.txt", b"a\r\nb\r\nc\r\n")
        handle_edit(
            "crlf2.txt", "", "",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"a","new_string":"A"}]',
        )
        self.assertEqual((self.root / "crlf2.txt").read_bytes(), b"A\r\nb\r\nc\r\n")


class TestBatchIsAllOrNothing(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = _make_svc(self.root)
        # Real-ish ledger spy so we can assert it is NOT called.
        self.svc.edit_ledger = MagicMock()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_bytes(self, rel: str, data: bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_no_op_batch_does_not_rewrite_or_log(self):
        self._write_bytes("x.txt", b"a\nb\nc\n")
        before_mtime = (self.root / "x.txt").stat().st_mtime_ns
        resp = handle_edit(
            "x.txt", "", "",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"nope","new_string":"X"}]',
        )
        self.assertIn("1 of 1 patches could not be placed", resp)
        # File untouched (bytes + mtime unchanged).
        self.assertEqual((self.root / "x.txt").read_bytes(), b"a\nb\nc\n")
        self.assertEqual((self.root / "x.txt").stat().st_mtime_ns, before_mtime)
        # No ledger entry recorded for a no-op batch.
        self.svc.edit_ledger.log_edit.assert_not_called()

    def test_one_unplaceable_patch_blocks_the_batch(self):
        self._write_bytes("y.txt", b"a\nb\nc\n")
        for bad in ('{"old_string":"nope","new_string":"X"}',
                    '{"old_string":"\\n","new_string":"X"}',
                    '{"old_string":"","new_string":"X"}'):
            with self.subTest(bad=bad):
                resp = handle_edit(
                    "y.txt", "", "",
                    summary="", tags="", replace_all=False,
                    svc=self.svc, finalize=_finalize,
                    edits='[{"old_string":"a","new_string":"A"},' + bad + ']',
                )
                self.assertIn("1 of 2 patches could not be placed", resp)
                self.assertEqual((self.root / "y.txt").read_bytes(), b"a\nb\nc\n")
        self.svc.edit_ledger.log_edit.assert_not_called()

    def test_patch_that_changes_nothing_does_not_block_the_batch(self):
        self._write_bytes("w.txt", b"a\nb\nc\n")
        resp = handle_edit(
            "w.txt", "", "",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"a","new_string":"A"},'
                  '{"old_string":"b","new_string":"b"}]',
        )
        self.assertIn("1/2 patches applied", resp)
        self.assertEqual((self.root / "w.txt").read_bytes(), b"A\nb\nc\n")

    def test_non_dict_element_rejected(self):
        self._write_bytes("z.txt", b"a\nb\nc\n")
        resp = handle_edit(
            "z.txt", "", "",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='["not-a-dict"]',
        )
        self.assertIn("non-object element", resp)
        # File untouched.
        self.assertEqual((self.root / "z.txt").read_bytes(), b"a\nb\nc\n")


if __name__ == "__main__":
    unittest.main()
