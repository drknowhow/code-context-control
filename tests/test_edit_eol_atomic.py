"""c3_edit writes atomically and leaves the bytes of untouched lines alone."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli.tools import edit as edit_mod  # noqa: E402
from cli.tools.edit import _apply_replacement, handle_edit  # noqa: E402
from cli.tools.edit_verify import verify  # noqa: E402
from services import atomic_json as aj  # noqa: E402


def _svc(root: Path):
    svc = MagicMock()
    svc.project_path = str(root)
    svc.edit_ledger = None
    svc.activity_log = None
    svc.session_mgr = None
    return svc


def _finalize(name, args, resp, summ, **kw):
    return resp


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.svc = _svc(self.root)
        self.path = self.root / "f.txt"

    def tearDown(self):
        if self.path.exists():
            os.chmod(self.path, stat.S_IREAD | stat.S_IWRITE)
        self._tmp.cleanup()

    def edit(self, old="", new="", **kw):
        return handle_edit(str(self.path), old, new, "", "",
                           kw.pop("replace_all", False), self.svc, _finalize,
                           **kw)


class TestLineEndingsAreByteExact(_Base):
    def check(self, before: bytes, old: str, new: str, after: bytes, **kw):
        self.path.write_bytes(before)
        out = self.edit(old, new, **kw)
        self.assertIn("✓", out)
        self.assertEqual(self.path.read_bytes(), after)

    def test_lf_file(self):
        self.check(b"a\nb\nc\n", "b", "B", b"a\nB\nc\n")

    def test_crlf_file(self):
        self.check(b"a\r\nb\r\nc\r\n", "b", "B", b"a\r\nB\r\nc\r\n")

    def test_mixed_file_keeps_minority_eols(self):
        self.check(b"a\r\nb\nc\r\nd\n", "a", "A", b"A\r\nb\nc\r\nd\n")

    def test_cr_only_file_stays_cr_only(self):
        self.check(b"a\rb\r", "a", "A", b"A\rb\r")

    def test_crlf_old_string_matches_crlf_file(self):
        self.check(b"a\r\nb\r\n", "a\r\nb", "x\r\ny", b"x\r\ny\r\n")

    def test_lf_old_string_matches_crlf_file_and_new_takes_crlf(self):
        self.check(b"a\r\nb\r\nc\r\n", "a\nb", "x\ny\nz", b"x\r\ny\r\nz\r\nc\r\n")

    def test_new_text_takes_eol_of_the_region_it_replaces(self):
        self.check(b"a\r\nb\r\nc\nd\ne\r\n", "c\nd", "C\nD\nE",
                   b"a\r\nb\r\nC\nD\nE\ne\r\n")

    def test_multiline_edit_in_cr_only_file(self):
        self.check(b"a\rb\rc\r", "a\nb", "x\ny", b"x\ry\rc\r")

    def test_replace_all_across_mixed_eols(self):
        self.check(b"k\r\nv\nk\nv\r\n", "k\nv", "K\nV",
                   b"K\r\nV\nK\nV\r\n", replace_all=True)

    def test_lookalike_fallback_with_crlf(self):
        self.check("x = “hi”\r\ny\r\nz\n".encode("utf-8"), 'x = "hi"\ny', "q\nr",
                   b"q\r\nr\r\nz\n")

    def test_batch_on_mixed_file(self):
        self.path.write_bytes(b"one\r\ntwo\nthree\r\n")
        out = self.edit(edits=json.dumps([
            {"old_string": "one", "new_string": "1"},
            {"old_string": "two\nthree", "new_string": "2\n3"},
        ]))
        self.assertIn("2/2 patches applied", out)
        self.assertEqual(self.path.read_bytes(), b"1\r\n2\n3\r\n")

    def test_apply_replacement_ambiguity_counts_every_eol_style(self):
        out, count, _ = _apply_replacement("a\r\nb\na\nb\n", "a\nb", "X", False)
        self.assertIsNone(out)
        self.assertEqual(count, 2)

    def test_not_found_region_has_right_lines_and_no_cr(self):
        self.path.write_bytes(b"l1\r\nl2\r\ndef foo(a, b):\r\n    return a\r\nl5\r\n")
        out = self.edit("def foo(a, c):\n    return a", "x")
        self.assertIn("closest match: L1-L6", out)
        self.assertIn("\nl2\ndef foo(a, b):\n    return a\nl5\n", out)
        self.assertNotIn("\r", out)


class TestAtomicWrite(_Base):
    def test_failed_replace_leaves_original_and_no_temp(self):
        self.path.write_bytes(b"keep\r\nme\n")

        def boom(src, dst):
            raise PermissionError(13, "denied")

        with patch.object(aj.os, "replace", boom), patch.object(aj.time, "sleep"):
            out = self.edit("keep", "lost")
        self.assertIn("Write error", out)
        self.assertEqual(self.path.read_bytes(), b"keep\r\nme\n")
        self.assertEqual([p.name for p in self.root.glob("f.txt*")], ["f.txt"])

    def test_create_goes_through_atomic_publish(self):
        with patch.object(edit_mod, "write_bytes_atomic",
                          wraps=aj.write_bytes_atomic) as spy:
            out = self.edit("", "new\r\nfile\n")
        self.assertIn("created", out)
        spy.assert_called_once()
        self.assertEqual(self.path.read_bytes(), b"new\r\nfile\n")

    def test_read_only_target_is_refused_not_replaced(self):
        self.path.write_bytes(b"locked\n")
        os.chmod(self.path, stat.S_IREAD)
        out = self.edit("locked", "open")
        self.assertIn("Write error", out)
        self.assertIn("read-only", out)
        self.assertEqual(self.path.read_bytes(), b"locked\n")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_permission_bits_survive_replace(self):
        self.path.write_bytes(b"#!/bin/sh\n")
        os.chmod(self.path, 0o751)
        self.edit("sh", "bash")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o751)

    def test_write_bytes_atomic_writes_exact_bytes(self):
        data = b"a\r\nb\rc\n\xff"
        aj.write_bytes_atomic(self.path, data)
        self.assertEqual(self.path.read_bytes(), data)

    def test_write_text_atomic_translates_like_write_text(self):
        ref = self.root / "ref.txt"
        ref.write_text("x\ny\r\n", encoding="utf-8")
        aj.write_text_atomic(self.path, "x\ny\r\n")
        self.assertEqual(self.path.read_bytes(), ref.read_bytes())


class TestNoOpAndEmpty(_Base):
    def test_empty_old_string_on_existing_file_is_a_clear_error(self):
        self.path.write_bytes(b"hello\n")
        out = self.edit("", "x")
        self.assertIn("old_string is empty", out)
        self.assertNotIn("matches", out)
        self.assertEqual(self.path.read_bytes(), b"hello\n")

    def test_identical_old_and_new_neither_writes_nor_logs(self):
        self.path.write_bytes(b"same\r\n")
        with patch.object(edit_mod, "write_bytes_atomic") as write, \
                patch.object(edit_mod, "_log_to_ledger") as log:
            out = self.edit("same", "same")
        self.assertIn("unchanged", out)
        self.assertNotIn("✓", out)
        write.assert_not_called()
        log.assert_not_called()

    def test_all_noop_batch_neither_writes_nor_logs(self):
        self.path.write_bytes(b"a\nb\n")
        with patch.object(edit_mod, "write_bytes_atomic") as write, \
                patch.object(edit_mod, "_log_to_ledger") as log:
            out = self.edit(edits=[{"old_string": "a", "new_string": "a"}])
        self.assertIn("unchanged", out)
        self.assertIn("no change", out)
        write.assert_not_called()
        log.assert_not_called()

    def test_batch_with_one_noop_still_writes_the_rest(self):
        self.path.write_bytes(b"a\nb\n")
        out = self.edit(edits=[{"old_string": "a", "new_string": "a"},
                               {"old_string": "b", "new_string": "B"}])
        self.assertIn("1/2 patches applied", out)
        self.assertIn("patch[0]: no change", out)
        self.assertEqual(self.path.read_bytes(), b"a\nB\n")


class TestEditsParam(_Base):
    def test_edits_as_a_list(self):
        self.path.write_bytes(b"x\ny\n")
        out = self.edit(edits=[{"old_string": "x", "new_string": "X"}])
        self.assertIn("1/1 patches applied", out)
        self.assertEqual(self.path.read_bytes(), b"X\ny\n")

    def test_c3_edit_schema_accepts_an_array(self):
        import asyncio

        from cli import mcp_server
        tool = asyncio.run(mcp_server.mcp.get_tool("c3_edit"))
        schema = json.dumps(tool.parameters["properties"]["edits"])
        self.assertIn('"array"', schema)
        self.assertIn('"string"', schema)


class TestVerifyAfterCrlfEdit(_Base):
    def test_crlf_new_string_is_found_in_crlf_file(self):
        self.path.write_bytes(b"a\r\nb\r\n")
        self.edit("a", "x\r\ny")
        body, _ = verify(str(self.path), "a", "x\r\ny", "", self.svc)
        self.assertNotIn("NOT_APPLIED", body)


if __name__ == "__main__":
    unittest.main()
