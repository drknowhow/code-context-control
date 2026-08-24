"""The `.env` parser, in isolation.

`import_env` used to walk lines one at a time: `splitlines()` then `strip()`
then `partition("=")`. That is fine until a value spans lines, which is exactly
what a `.env` holding a PEM key or a JSON service-account blob does. The old
parser stored the first line only — quote included, so
`GOOGLE_KEY="-----BEGIN PRIVATE KEY-----` — and every continuation line was
dropped on the `"=" not in line` test without ever reaching `skipped`. The
caller was told the import succeeded and the user had a truncated key.

So these tests are mostly about values the old parser got silently WRONG,
rather than about lines it obviously rejected. `parse_env` is pure — no
keyring, no config, no store — which is what lets them run flat like this.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = str(Path(__file__).resolve().parents[1])
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.credential_store import parse_env  # noqa: E402

PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n"
    "C7VJTUt9Us8cKjMzEfYyjiWA4R4/M2bS1GB4t7NXp98C3SC6dV\n"
    "-----END PRIVATE KEY-----"
)


def rows_by_name(text: str) -> dict:
    return {r["name"]: r for r in parse_env(text)}


class TestMultiLineValues(unittest.TestCase):
    """The regression this module exists for."""

    def test_pem_round_trips_byte_for_byte(self):
        row = rows_by_name('GOOGLE_KEY="%s"\n' % PEM)["GOOGLE_KEY"]
        self.assertTrue(row["ok"], row["reason"])
        self.assertEqual(row["value"], PEM)

    def test_json_blob_round_trips(self):
        blob = json.dumps({"type": "service_account", "id": "1"}, indent=2)
        row = rows_by_name("SA='%s'\n" % blob)["SA"]
        self.assertEqual(row["value"], blob)

    def test_keys_after_a_multiline_value_still_parse(self):
        """The old parser resynced wrongly and swallowed whatever followed."""
        rows = rows_by_name('KEY="%s"\nAFTER=fine\n' % PEM)
        self.assertEqual(rows["KEY"]["value"], PEM)
        self.assertEqual(rows["AFTER"]["value"], "fine")

    def test_interior_indentation_is_preserved(self):
        row = rows_by_name('K="a\n    indented\nb"\n')["K"]
        self.assertEqual(row["value"], "a\n    indented\nb")

    def test_unterminated_quote_is_reported_not_guessed(self):
        row = rows_by_name('K="never closes\nstill going\n')["K"]
        self.assertFalse(row["ok"])
        self.assertEqual(row["reason"], "unterminated-quote")


class TestQuoting(unittest.TestCase):
    def test_single_quotes_are_literal(self):
        # POSIX: no escape processing inside single quotes.
        row = rows_by_name("K='raw \\n stays'\n")["K"]
        self.assertEqual(row["value"], "raw \\n stays")

    def test_double_quotes_unescape(self):
        row = rows_by_name('K="esc \\n becomes"\n')["K"]
        self.assertEqual(row["value"], "esc \n becomes")

    def test_double_quote_escapes_tab_return_backslash_quote(self):
        row = rows_by_name('K="a\\tb\\rc\\\\d\\"e"\n')["K"]
        self.assertEqual(row["value"], 'a\tb\rc\\d"e')

    def test_escaped_quote_does_not_end_the_value(self):
        row = rows_by_name('K="say \\"hi\\" now"\n')["K"]
        self.assertEqual(row["value"], 'say "hi" now')

    def test_interior_whitespace_survives_inside_quotes(self):
        """A secret may legitimately start or end with a space."""
        self.assertEqual(rows_by_name('K="  keep me  "\n')["K"]["value"],
                         "  keep me  ")

    def test_unquoted_values_are_stripped(self):
        self.assertEqual(rows_by_name("K=   trimmed   \n")["K"]["value"],
                         "trimmed")


class TestComments(unittest.TestCase):
    def test_inline_comment_is_dropped_from_unquoted_value(self):
        # Old behaviour stored "bar # trailing note" as the secret.
        self.assertEqual(rows_by_name("K=bar # trailing note\n")["K"]["value"],
                         "bar")

    def test_hash_inside_a_token_survives(self):
        self.assertEqual(rows_by_name("K=pa#ss\n")["K"]["value"], "pa#ss")

    def test_hash_inside_quotes_survives(self):
        self.assertEqual(rows_by_name('K="has # inside"\n')["K"]["value"],
                         "has # inside")

    def test_full_line_comments_and_blanks_produce_no_rows(self):
        self.assertEqual(parse_env("# one\n\n   \n# two\n"), [])


class TestLineShapes(unittest.TestCase):
    def test_utf8_bom_does_not_poison_the_first_key(self):
        # A BOM made the first name fail _NAME_RE, so the first secret in the
        # file was silently the one that went missing.
        row = rows_by_name("﻿FIRST=v\n")["FIRST"]
        self.assertTrue(row["ok"], row["reason"])

    def test_crlf(self):
        self.assertEqual(rows_by_name("A=1\r\nB=2\r\n")["B"]["value"], "2")

    def test_export_prefix(self):
        self.assertEqual(rows_by_name("export K=v\n")["K"]["value"], "v")

    def test_bad_name_is_reported_by_name(self):
        row = rows_by_name("1BAD=nope\n")["1BAD"]
        self.assertEqual(row["reason"], "bad-name")

    def test_empty_value_is_reported(self):
        self.assertEqual(rows_by_name("K=\n")["K"]["reason"], "empty")

    def test_last_definition_of_a_name_wins(self):
        rows = parse_env("D=one\nD=two\n")
        self.assertEqual([r["reason"] for r in rows], ["duplicate", ""])
        self.assertEqual(rows[-1]["value"], "two")

    def test_line_without_assignment_is_reported_without_echoing_it(self):
        """A stray line in a .env is likelier to be key material than prose."""
        secret = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcw"
        rows = parse_env("%s\n" % secret)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "no-assignment")
        self.assertNotIn(secret, json.dumps(rows))
        self.assertEqual(rows[0]["name"], "line 1")

    def test_line_numbers_point_at_the_definition(self):
        rows = parse_env("# c\n\nA=1\nB=2\n")
        self.assertEqual([(r["name"], r["line"]) for r in rows],
                         [("A", 3), ("B", 4)])

    def test_empty_input(self):
        self.assertEqual(parse_env(""), [])
        self.assertEqual(parse_env(None), [])


if __name__ == "__main__":
    unittest.main()
