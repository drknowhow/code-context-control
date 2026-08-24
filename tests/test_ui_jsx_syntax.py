"""Guards the UI bundles against a JSX syntax error that only Babel sees.

The hub, project and Oracle UIs are transpiled in the browser by
`babel.min.js`. A syntax error anywhere in the concatenated script kills the
whole bundle — the page renders black and the only clue is a console message,
so nothing on the Python side notices. 2.92.0 through 2.92.3 shipped exactly
that: a `{/* ... */}` comment placed directly inside `cond && ( ... )`.

That position is expression context, not JSX children context, so Babel reads
the `{` as an object literal and fails with `Unexpected token, expected ","`.
A JSX comment is only legal between JSX tags; in expression position it has to
be a plain `/* ... */`, or move above the conditional.

The existing bundle tests check the SHAPE of the build (files listed, markers
stamped, order kept) and passed the whole time the broken file was shipping.
This one checks the property that actually broke.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Roots holding browser-transpiled JSX (shared project UI, hub UI, Oracle UI).
UI_ROOTS = ("cli/ui", "cli/hub_ui", "oracle/ui")

# A line that opens an expression paren: the next token must be an expression,
# and `{/*` there is an object literal, not a comment.
_OPENS_EXPRESSION = re.compile(r"(?:&&|\|\||\?|:|=>|=|\breturn\b|\(|,)\s*\($")


def _ui_files() -> list[Path]:
    files: list[Path] = []
    for rel in UI_ROOTS:
        root = REPO_ROOT / rel
        if root.is_dir():
            files.extend(sorted(root.rglob("*.js")))
    return files


class TestJsxCommentPlacement(unittest.TestCase):
    def test_ui_roots_are_not_empty(self):
        # A silent rename would make the scan below pass by finding nothing.
        self.assertGreater(len(_ui_files()), 40, "UI file scan found almost nothing")

    def test_no_jsx_comment_in_expression_position(self):
        offenders = []
        for path in _ui_files():
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines[:-1]):
                if not _OPENS_EXPRESSION.search(line.rstrip()):
                    continue
                if lines[i + 1].lstrip().startswith("{/*"):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}:{i + 2}: {lines[i + 1].strip()[:60]}")
        self.assertEqual(
            offenders,
            [],
            "JSX comment in expression position — Babel reads `{` as an object "
            "literal and the whole bundle fails to parse:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
