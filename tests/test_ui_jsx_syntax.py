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

import os
import re
import subprocess
import tempfile
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


# ── The real thing, when a JS toolchain happens to be around ─────────────────
# The regex above only knows the one shape that shipped a black screen. This
# class transpiles the ACTUAL concatenated bundles the way the browser does, so
# it catches every syntax error rather than one family of them. It earned its
# place immediately: it caught an unterminated string literal in
# hub_credentials.js that the regex test passed clean.
#
# The project deliberately has no build step and no node_modules, so this skips
# unless a toolchain is present. To run it for real:
#     npm install @babel/standalone       (anywhere)
#     NODE_PATH=<there>/node_modules python -m pytest tests/test_ui_jsx_syntax.py
_PARSE_JS = """
const Babel = require('@babel/standalone');
const fs = require('fs');
let combined = '';
for (const f of process.argv.slice(2)) {
  combined += '\\n// === ' + f + ' ===\\n' + fs.readFileSync(f, 'utf8');
}
try {
  Babel.transform(combined, { presets: ['react'], filename: 'bundle.js' });
  console.log('OK');
} catch (e) { console.log('FAIL ' + e.message); process.exit(1); }
"""

_JS_STR = re.compile(r"""['"]([^'"]+\.js)['"]""")


def _bundle_files(server_rel, const_name):
    """The bundle's file list, read from the server that concatenates it.

    Derived from the real list rather than restated here, so a file added to
    the bundle is covered without anyone remembering to add it twice.
    """
    src = (REPO_ROOT / server_rel).read_text(encoding="utf-8")
    match = re.search(const_name + r"\s*=\s*\[(.*?)\]", src, re.S)
    if not match:
        return []
    return [REPO_ROOT / "cli" / rel for rel in _JS_STR.findall(match.group(1))]


def _toolchain_ready():
    try:
        probe = subprocess.run(
            ["node", "-e", "require.resolve('@babel/standalone'); console.log('y')"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


@unittest.skipUnless(_toolchain_ready(),
                     "node + @babel/standalone unavailable (see module comment)")
class TestBundlesActuallyTranspile(unittest.TestCase):
    def _check(self, server_rel, const_name):
        files = [f for f in _bundle_files(server_rel, const_name) if f.is_file()]
        self.assertTrue(files, "no bundle file list found in " + server_rel)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(_PARSE_JS)
            script = fh.name
        try:
            proc = subprocess.run(["node", script] + [str(f) for f in files],
                                  capture_output=True, text=True, timeout=300)
        finally:
            os.unlink(script)
        self.assertEqual(
            proc.returncode, 0,
            server_rel + " bundle does not transpile — the page renders black "
            "while the route still returns 200:\n"
            + proc.stdout.strip() + proc.stderr.strip())

    def test_hub_bundle_transpiles(self):
        self._check("cli/hub_server.py", "_HUB_JS_FILES")

    def test_project_bundle_transpiles(self):
        self._check("cli/server.py", "_UI_JS_FILES")


if __name__ == "__main__":
    unittest.main()
