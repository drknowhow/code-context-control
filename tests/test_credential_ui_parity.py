"""The browser surfaces must know about every structured credential kind.

The `login` kind shipped in the store, the MCP tool and the CLI and was
missing from BOTH web UIs for two releases. Nothing failed: the store is
type-agnostic, the server passes `type` straight through, and the UIs are
driven off a *hardcoded* `CREDS_STRUCTURED` table that simply had no entry.
The observable symptoms were silent and both bad:

  * `login` was absent from the Type dropdown, so the kind was unreachable
    from the browser at all — while the guide told the user to enter these
    through the UI precisely so the password never enters a chat.
  * an entry created via `c3 creds` rendered as a PLAIN secret: a single
    "Replace secret…" password box (which would have written the whole
    payload as one opaque string) plus agent_readable / inject toggles that
    the server refuses with a 400.

So this file asserts on the SET of kinds, not on `login` specifically —
the next kind added to the store fails here until both UIs carry it.

There is no JS runtime in this test environment, so the tables are read out
of the source with a targeted parse. That is deliberate: a test that
duplicated the field lists in Python would pass while the browser stayed
wrong, which is the exact failure being guarded.
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import credential_store as cs

UI_FILES = {
    "project": REPO_ROOT / "cli" / "ui" / "components" / "credentials.js",
    "hub": REPO_ROOT / "cli" / "hub_ui" / "components" / "hub_credentials.js",
}


def _structured_table(source: str) -> dict:
    """Parse the `const CREDS_STRUCTURED = {...};` literal out of a UI file.

    The JS object literal is JSON-shaped apart from bare keys, single quotes
    and trailing commas, so it converts cleanly to a Python literal. Comment
    lines are stripped first; none of the field names contain `//`.
    """
    match = re.search(r"const CREDS_STRUCTURED = \{(.*?)\n\};", source, re.S)
    if not match:
        raise AssertionError("no CREDS_STRUCTURED literal found")
    body = "\n".join(
        line for line in match.group(1).splitlines()
        if not line.strip().startswith("//")
    )
    body = re.sub(r"(?<![\w\"'])([A-Za-z_][A-Za-z0-9_]*)\s*:", r'"\1":', body)
    return ast.literal_eval("{" + body + "}")


def _type_options(source: str) -> set:
    """Every `<option value="X">` inside the Type <select>."""
    return set(re.findall(r"<option value=[\"']([a-z_]+)[\"']", source))


class TestCredentialUiParity(unittest.TestCase):
    def setUp(self):
        self.sources = {k: p.read_text(encoding="utf-8")
                        for k, p in UI_FILES.items()}

    def test_both_uis_carry_every_structured_kind(self):
        for name, src in self.sources.items():
            with self.subTest(ui=name):
                self.assertEqual(set(_structured_table(src)),
                                 set(cs.STRUCTURED_TYPES),
                                 f"{name} UI is out of step with "
                                 "credential_store.STRUCTURED_TYPES")

    def test_field_sets_match_the_store_schema_exactly(self):
        for name, src in self.sources.items():
            table = _structured_table(src)
            for ctype in sorted(cs.STRUCTURED_TYPES):
                required, optional = cs.schema_fields(ctype)
                with self.subTest(ui=name, ctype=ctype):
                    self.assertEqual(tuple(table[ctype]["required"]), required)
                    self.assertEqual(tuple(table[ctype]["optional"]), optional)

    def test_hidden_fields_are_real_fields_of_their_kind(self):
        """A typo'd hidden name silently renders a secret as a text input."""
        for name, src in self.sources.items():
            table = _structured_table(src)
            for ctype, spec in table.items():
                required, optional = cs.schema_fields(ctype)
                known = set(required) | set(optional)
                with self.subTest(ui=name, ctype=ctype):
                    self.assertTrue(set(spec["hidden"]) <= known,
                                    f"{spec['hidden']} not all in {sorted(known)}")

    #: Field names that must never be echoed, in any UI or on the terminal.
    #: Substring-matched against the schema so a NEW secret-bearing field on
    #: a future kind is covered without anyone remembering to add it here.
    SECRET_FIELD_MARKERS = ("password", "secret", "private_key", "passphrase",
                            "number", "cvc", "ssn")

    @classmethod
    def _secret_fields(cls):
        """{ctype: {field, …}} — every secret-bearing field in _SCHEMAS."""
        out = {}
        for ctype in sorted(cs.STRUCTURED_TYPES):
            required, optional = cs.schema_fields(ctype)
            hit = {f for f in tuple(required) + tuple(optional)
                   if any(m in f for m in cls.SECRET_FIELD_MARKERS)}
            if hit:
                out[ctype] = hit
        return out

    def test_secret_bearing_fields_render_masked(self):
        """The fields whose whole point is secrecy must be password inputs."""
        must_hide = self._secret_fields()
        self.assertIn("login", must_hide)  # the derivation actually found some
        for name, src in self.sources.items():
            table = _structured_table(src)
            for ctype, fields in must_hide.items():
                with self.subTest(ui=name, ctype=ctype):
                    self.assertTrue(fields <= set(table[ctype]["hidden"]),
                                    f"unmasked in the {name} UI: "
                                    f"{sorted(fields - set(table[ctype]['hidden']))}")

    def test_the_cli_prompts_for_secret_fields_without_echo(self):
        """The third UI, and the one nothing asserted until v2.118.0.

        `c3 creds set X --type login` prompted for the password with plain
        `input()` from v2.90.0 to v2.117.0 and echoed it to the terminal
        (fixed in v2.118.0),
        while both browser UIs masked it correctly. Derived from _SCHEMAS
        rather than hardcoded, so a future kind cannot reopen the hole.
        """
        from cli.c3 import _CREDS_HIDDEN_FIELDS, _CREDS_MULTILINE_FIELDS
        covered = set(_CREDS_HIDDEN_FIELDS) | set(_CREDS_MULTILINE_FIELDS)
        missing = {f for fields in self._secret_fields().values()
                   for f in fields} - covered
        self.assertEqual(
            missing, set(),
            f"c3 creds would echo these to the terminal: {sorted(missing)}")

    def test_every_valid_type_is_selectable_in_both_uis(self):
        for name, src in self.sources.items():
            options = _type_options(src)
            for ctype in cs.VALID_TYPES:
                with self.subTest(ui=name, ctype=ctype):
                    self.assertIn(ctype, options,
                                  f"{ctype} has no <option> in the {name} UI, "
                                  "so it cannot be created from the browser")

    def test_login_display_is_not_the_generic_join(self):
        """login's projection has a BOOLEAN in it.

        `Object.values(display).join(', ')` would print the literal word
        "true" next to the site — and the projection exists so the UI can
        show a 2FA badge. A dedicated branch is required.
        """
        for name, src in self.sources.items():
            with self.subTest(ui=name):
                self.assertRegex(
                    src, r"entry\.type === ['\"]login['\"]",
                    "no login branch in credsDisplayText")
                self.assertIn("has_totp", src)

    def test_username_is_not_rendered_from_the_projection(self):
        """The store withholds username on purpose; the UI must not re-add it.

        username + origin is half the credential, and the registry row is the
        one part of a login record non-secret surfaces may render.
        """
        self.assertNotIn("username", cs._display_projection(
            "login", {"site_id": "gh", "canonical_origin": "https://github.com",
                      "username": "alice", "password": "x"}))


if __name__ == "__main__":
    unittest.main()
