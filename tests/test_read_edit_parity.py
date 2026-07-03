"""Parity tests between c3_read output and c3_edit matching.

Regression suite for the read→edit drift bug: old_strings copied from
c3_read output failed to match in c3_edit (encoding asymmetry, splitlines()
unicode line-boundary splitting, separator pollution), pushing agents back
to native Read/Edit. These tests pin the unified pipeline:

- read: EOL-only normalization, form feeds/U+2028 stay inline,
  copy-safe ⟦…⟧ range markers with explicit gap notes, map-hint footer.
- edit: surrogateescape round-trip for non-UTF-8 bytes, U+FFFD match
  folding, closest-region payload on not-found.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.tools.edit import _closest_region, handle_edit  # noqa: E402
from cli.tools.read import handle_read  # noqa: E402


def _make_svc(project_path: Path):
    svc = MagicMock()
    svc.project_path = str(project_path)
    svc.edit_ledger = None
    svc.activity_log = None
    svc.session_mgr = None
    return svc


def _finalize(name, args, resp, summ, **kw):
    return resp


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = _make_svc(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_text(self, rel: str, text: str) -> Path:
        p = self.root / rel
        p.write_text(text, encoding="utf-8", newline="")
        return p

    def _write_bytes(self, rel: str, data: bytes) -> Path:
        p = self.root / rel
        p.write_bytes(data)
        return p

    def _read(self, rel: str, **kw) -> str:
        return handle_read(rel, svc=self.svc, finalize=_finalize, **kw)

    def _edit(self, rel: str, old: str, new: str, **kw) -> str:
        return handle_edit(rel, old, new, summary="", tags="",
                           replace_all=kw.pop("replace_all", False),
                           svc=self.svc, finalize=_finalize, **kw)


class TestReadEolParity(_Base):
    def test_form_feed_stays_inline(self):
        # splitlines() used to break on \x0c, showing a phantom line break
        # that made copied old_strings unmatchable.
        self._write_text("a.py", "alpha\x0cbeta\ngamma\n")
        resp = self._read("a.py", lines=[1, 2])
        self.assertIn("alpha\x0cbeta", resp)
        self.assertIn("gamma", resp)

    def test_u2028_stays_inline(self):
        self._write_text("b.py", "one\u2028two\nthree\n")
        resp = self._read("b.py", lines=[1, 2])
        self.assertIn("one\u2028two", resp)

    def test_crlf_normalized_to_lf(self):
        self._write_bytes("c.py", b"line1\r\nline2\r\n")
        resp = self._read("c.py", lines=[1, 2])
        self.assertNotIn("\r", resp)
        self.assertIn("line1\nline2", resp)

    def test_read_snippet_roundtrips_through_edit(self):
        # The core parity guarantee: any snippet of c3_read output (single
        # range) must match verbatim as a c3_edit old_string.
        self._write_bytes("d.py", b"def f():\r\n    return 1\r\n")
        resp = self._read("d.py", lines=[1, 2])
        self.assertIn("def f():\n    return 1", resp)
        edit_resp = self._edit("d.py", "def f():\n    return 1",
                               "def f():\n    return 2")
        self.assertIn("✓", edit_resp)  # ✓
        self.assertEqual((self.root / "d.py").read_bytes(),
                         b"def f():\r\n    return 2\r\n")


class TestReadRangeMarkers(_Base):
    def setUp(self):
        super().setUp()
        self._write_text("m.py",
                         "".join(f"line{i}\n" for i in range(1, 13)))

    def test_multi_range_has_gap_note(self):
        resp = self._read("m.py", lines=[[1, 2], [8, 9]])
        self.assertIn("⟦L1-L2⟧", resp)          # ⟦L1-L2⟧
        self.assertIn("omitted", resp)
        self.assertIn("L3-L7", resp)                       # the gap range
        self.assertNotIn("--- L", resp)                    # old separator gone

    def test_single_range_has_no_marker(self):
        resp = self._read("m.py", lines=[1, 3])
        self.assertNotIn("⟦", resp)


class TestReadMapHint(_Base):
    def test_map_response_says_how_to_get_source(self):
        self._write_text("n.py", "x = 1\n")
        self.svc.file_memory.get_or_build_map.return_value = "FILE MAP"
        resp = self._read("n.py")
        self.assertIn("FILE MAP", resp)
        self.assertIn("map only", resp)
        self.assertIn("lines=[start,end]", resp)


class TestEditByteSafety(_Base):
    LATIN1 = b"# caf\xe9 latin-1 comment\nvalue = 1\n"

    def test_edit_succeeds_despite_invalid_utf8(self):
        # Strict decode used to raise UnicodeDecodeError before matching.
        self._write_bytes("e.py", self.LATIN1)
        resp = self._edit("e.py", "value = 1", "value = 2")
        self.assertIn("✓", resp)

    def test_untouched_invalid_bytes_roundtrip(self):
        self._write_bytes("e.py", self.LATIN1)
        self._edit("e.py", "value = 1", "value = 2")
        data = (self.root / "e.py").read_bytes()
        self.assertIn(b"caf\xe9", data)          # byte preserved, not U+FFFD'd
        self.assertIn(b"value = 2", data)

    def test_fffd_from_read_output_matches_raw_byte(self):
        # c3_read renders \xe9 as U+FFFD; that old_string must still match.
        self._write_bytes("e.py", self.LATIN1)
        resp = self._edit("e.py", "# caf� latin-1 comment", "# cafe comment")
        self.assertIn("✓", resp)
        self.assertNotIn(b"\xe9", (self.root / "e.py").read_bytes())


class TestEditClosestRegion(_Base):
    SRC = ("def foo(a, b):\n"
           "    return compute(a, b)\n"
           "\n"
           "def bar():\n"
           "    return 2\n")

    def test_not_found_includes_closest_region(self):
        self._write_text("f.py", self.SRC)
        resp = self._edit("f.py", "    return compute(a, c)", "    return 0")
        self.assertIn("not found", resp)
        self.assertIn("closest match", resp)
        self.assertIn("return compute(a, b)", resp)   # actual file text
        self.assertIn("no need to re-read", resp)

    def test_wildly_wrong_old_string_gets_no_region(self):
        self._write_text("f.py", self.SRC)
        resp = self._edit("f.py", "zzz@@qq##www!!", "x")
        self.assertIn("not found", resp)
        self.assertNotIn("closest match", resp)

    def test_batch_miss_carries_locator_and_region(self):
        self._write_text("f.py", self.SRC)
        resp = handle_edit(
            "f.py", "", "", summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"    return 2","new_string":"    return 3"},'
                  '{"old_string":"    return compute(a, c)","new_string":"    return 0"}]',
        )
        self.assertIn("1/2 patches applied", resp)
        self.assertIn("NOT FOUND", resp)
        self.assertIn("closest:", resp)
        self.assertIn("return compute(a, b)", resp)

    def test_batch_summary_words_not_miscounted(self):
        # Regression: patch summaries containing 'NOT FOUND'/'skipped' were
        # substring-matched by the outcome classifier and reported as failures.
        self._write_text("f.py", self.SRC)
        resp = handle_edit(
            "f.py", "", "", summary="", tags="", replace_all=False,
            svc=self.svc, finalize=_finalize,
            edits='[{"old_string":"    return 2","new_string":"    return 3",'
                  '"summary":"this mentions NOT FOUND and skipped"}]',
        )
        self.assertIn("1/1 patches applied", resp)

    def test_closest_region_line_numbers(self):
        near = _closest_region(self.SRC, "    return compute(a, c)")
        self.assertIsNotNone(near)
        lo, hi, region, ratio = near
        self.assertLessEqual(lo, 2)
        self.assertGreaterEqual(hi, 2)
        self.assertIn("return compute(a, b)", region)
        self.assertGreater(ratio, 0.8)


if __name__ == "__main__":
    unittest.main()
