"""Tests for c3_edit unicode-lookalike fallback matching.

Regression: c3_edit previously failed when the caller used straight quotes
but the file contained curly quotes (or vice versa). The fallback path
normalizes a small, 1:1 table of lookalikes (curly quotes, unicode
dashes, NBSP) before counting matches, then splices replacements into the
original content at the matched offsets so unrelated lookalikes in the
file are preserved.
"""
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

    def test_curly_double_quote_fallback(self):
        content = 'x = “hello”'
        out, count, fb = _apply_replacement(content, 'x = "hello"', 'x = "world"', False)
        self.assertEqual(out, 'x = "world"')
        self.assertEqual(count, 1)
        self.assertTrue(fb)

    def test_curly_single_quote_fallback(self):
        content = "it’s fine"
        out, count, fb = _apply_replacement(content, "it's fine", "it was fine", False)
        self.assertEqual(out, "it was fine")
        self.assertTrue(fb)

    def test_em_dash_fallback(self):
        content = "use --force—really"
        out, count, fb = _apply_replacement(content, "force-really", "force now", False)
        self.assertEqual(out, "use --force now")
        self.assertTrue(fb)

    def test_nbsp_fallback(self):
        content = "hello world"
        out, count, fb = _apply_replacement(content, "hello world", "hello there", False)
        self.assertEqual(out, "hello there")
        self.assertTrue(fb)

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

    def test_ambiguous_via_fallback(self):
        content = "“hi” “hi”"
        out, count, fb = _apply_replacement(content, '"hi"', 'X', False)
        self.assertIsNone(out)
        self.assertEqual(count, 2)
        self.assertTrue(fb)

    def test_replace_all_via_fallback(self):
        content = "“hi” “hi”"
        out, count, fb = _apply_replacement(content, '"hi"', 'X', True)
        self.assertEqual(out, "X X")
        self.assertEqual(count, 2)
        self.assertTrue(fb)

    def test_fallback_only_touches_matched_region(self):
        # Unrelated curly quotes elsewhere in the file must not be rewritten.
        content = "keep “me” :: target “here”"
        out, count, fb = _apply_replacement(
            content, 'target "here"', 'target DONE', False)
        self.assertEqual(out, "keep “me” :: target DONE")
        self.assertTrue(fb)

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

    def test_curly_quote_single_edit_succeeds(self):
        self._write("a.py", "msg = “hello”\n")
        resp = handle_edit(
            "a.py", 'msg = "hello"', 'msg = "world"',
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
        )
        self.assertIn("unicode-normalized", resp)
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"),
                         'msg = "world"\n')

    def test_still_reports_not_found_when_no_match(self):
        self._write("a.py", "msg = “hello”\n")
        resp = handle_edit(
            "a.py", 'msg = "nope"', 'msg = "X"',
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
        )
        self.assertIn("not found", resp)

    def test_batch_mode_with_fallback(self):
        self._write("b.py", "a = ‘one’\nb = “two”\n")
        resp = handle_edit(
            "b.py", "", "",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"a = \'one\'","new_string":"a = \'ONE\'"},'
                  '{"old_string":"b = \\"two\\"","new_string":"b = \\"TWO\\""}]',
        )
        self.assertIn("2/2 patches applied", resp)
        self.assertIn("unicode-normalized", resp)
        self.assertEqual((self.root / "b.py").read_text(encoding="utf-8"),
                         "a = 'ONE'\nb = \"TWO\"\n")


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


class TestBatchNoOp(unittest.TestCase):
    """Regression: batch mode wrote the file + logged a ledger entry even
    when zero patches applied (all NOT FOUND/AMBIGUOUS). Fix: only write +
    log when at least one patch actually modified the content."""

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
        self.assertIn("0/1 patches applied", resp)
        # File untouched (bytes + mtime unchanged).
        self.assertEqual((self.root / "x.txt").read_bytes(), b"a\nb\nc\n")
        self.assertEqual((self.root / "x.txt").stat().st_mtime_ns, before_mtime)
        # No ledger entry recorded for a no-op batch.
        self.svc.edit_ledger.log_edit.assert_not_called()

    def test_partial_batch_writes_and_logs(self):
        self._write_bytes("y.txt", b"a\nb\nc\n")
        resp = handle_edit(
            "y.txt", "", "",
            summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"a","new_string":"A"},'
                  '{"old_string":"nope","new_string":"X"}]',
        )
        self.assertIn("1/2 patches applied", resp)
        self.assertEqual((self.root / "y.txt").read_bytes(), b"A\nb\nc\n")

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
