"""Tests for c3_read argument coercion (cli/tools/read.py).

MCP clients sometimes serialize list/number arguments as strings. These tests
lock in that:
  * comma-separated `symbols` strings split into multiple targets, and
  * `lines` survives string serialization (so a range read returns source,
    not the file map).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.tools.read import _coerce_lines, _coerce_list  # noqa: E402


class TestCoerceSymbols(unittest.TestCase):
    def test_comma_string_splits(self):
        self.assertEqual(_coerce_list("a,b,c"), ["a", "b", "c"])

    def test_comma_string_strips_whitespace(self):
        self.assertEqual(_coerce_list("a, b , c"), ["a", "b", "c"])

    def test_single_symbol(self):
        self.assertEqual(_coerce_list("foo"), ["foo"])

    def test_json_string_list(self):
        self.assertEqual(_coerce_list('["a", "b"]'), ["a", "b"])

    def test_real_list_passthrough(self):
        self.assertEqual(_coerce_list(["a", "b"]), ["a", "b"])

    def test_regex_anchor_preserved(self):
        self.assertEqual(_coerce_list("^foo$"), ["^foo$"])

    def test_none(self):
        self.assertIsNone(_coerce_list(None))


class TestCoerceLines(unittest.TestCase):
    def test_json_string_pair(self):
        self.assertEqual(_coerce_lines("[22, 193]"), [22, 193])

    def test_json_string_list_of_ranges(self):
        self.assertEqual(_coerce_lines("[[1, 5], [10, 20]]"), [[1, 5], [10, 20]])

    def test_int_string(self):
        self.assertEqual(_coerce_lines("22"), 22)

    def test_dash_range(self):
        self.assertEqual(_coerce_lines("22-40"), [22, 40])

    def test_real_list_passthrough(self):
        self.assertEqual(_coerce_lines([22, 193]), [22, 193])

    def test_real_int_passthrough(self):
        self.assertEqual(_coerce_lines(22), 22)

    def test_none(self):
        self.assertIsNone(_coerce_lines(None))

    def test_garbage_returns_none(self):
        self.assertIsNone(_coerce_lines("not a range"))


if __name__ == "__main__":
    unittest.main()
