"""Windows reliability guards.

Locks in invariants that have bitten this project on Windows:
- Text-mode open() calls must pass encoding='utf-8' so cp1252 doesn't corrupt JSON
- Path.read_text()/write_text() must pass encoding='utf-8' for the same reason
- No mojibake byte sequences in .py sources (re-encoding bugs decay silently)
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SKIP_DIRS = {".venv", "__pycache__", ".pytest_cache", ".c3",
              # Build artifacts: gitignored copies of sources we already scan.
              "build", "dist", ".eggs"}
_SKIP_FRAGMENTS = (".tmp.",)

# Sequence produced when UTF-8 text is written then re-read as cp1252 and
# saved back as UTF-8. "—" (em dash, 0xE2 0x80 0x94) becomes this triple.
_MOJIBAKE_EM_DASH = b"\xc3\xa2\xe2\x82\xac\xe2\x80\x9d"

# Any open(...) call. We later inspect the arguments.
_OPEN_CALL = re.compile(r"(?<!webbrowser\.)(?<!\.)\bopen\s*\(", re.MULTILINE)

# Binary-mode markers that don't require encoding=
_BINARY_MODES = ('"rb"', "'rb'", '"wb"', "'wb'", '"ab"', "'ab'",
                 '"rb+"', "'rb+'", '"wb+"', "'wb+'", '"ab+"', "'ab+'",
                 '"r+b"', "'r+b'", '"w+b"', "'w+b'", '"a+b"', "'a+b'")


def _iter_py_files():
    for p in sorted(REPO_ROOT.rglob("*.py")):
        parts = set(p.relative_to(REPO_ROOT).parts)
        if parts & _SKIP_DIRS:
            continue
        if any(frag in p.name for frag in _SKIP_FRAGMENTS):
            continue
        yield p


def _find_open_calls_missing_encoding(text: str):
    """Yield (line_no, call_text) for text-mode open() calls without encoding= ."""
    for m in _OPEN_CALL.finditer(text):
        depth = 1
        i = m.end()
        while i < len(text) and depth > 0:
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        call = text[m.start():i]
        if "encoding" in call:
            continue
        if any(mode in call for mode in _BINARY_MODES):
            continue
        # URL opens (webbrowser-style with positional string) slip the
        # webbrowser. guard only when imported as `from webbrowser import open`
        if re.search(r'\bopen\s*\(\s*["\']?(https?|file:|url)', call):
            continue
        line_no = text[: m.start()].count("\n") + 1
        yield line_no, call.replace("\n", " ")[:160]


# Path.read_text/write_text default to locale encoding exactly like open().
# `importlib.metadata` Distribution.read_text takes a FILENAME, not an encoding.
_PATH_TEXT_CALL = re.compile(r"\.(read_text|write_text)\s*\(", re.MULTILINE)
_PATH_TEXT_EXEMPT = re.compile(r"\b(dist|distribution)\.\s*$")


def _find_path_text_calls_missing_encoding(text: str):
    """Yield (line_no, call_text) for read_text/write_text without an encoding.

    ``read_text('utf-8')`` (positional) counts: encoding is the first parameter.
    """
    for m in _PATH_TEXT_CALL.finditer(text):
        if _PATH_TEXT_EXEMPT.search(text[max(0, m.start() - 24):m.start() + 1]):
            continue
        depth = 1
        i = m.end()
        while i < len(text) and depth > 0:
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        call = text[m.start():i]
        if "encoding" in call or "utf-8" in call or "utf8" in call:
            continue
        line_no = text[: m.start()].count("\n") + 1
        yield line_no, call.replace("\n", " ")[:160]


class TestWindowsReliability(unittest.TestCase):
    def test_no_text_open_without_encoding(self):
        """Every text-mode open() in shipped code must pass encoding='utf-8'.

        cp1252 default on Windows silently corrupts JSON that contains em dashes,
        smart quotes, or any non-ASCII. This is a production correctness invariant.
        This test itself is excluded (scans its own open() regex string).
        """
        self_path = Path(__file__).resolve()
        offenders = []
        for py in _iter_py_files():
            if py.resolve() == self_path:
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                self.fail(f"{py} is not valid UTF-8: {e}")
            for line_no, call in _find_open_calls_missing_encoding(text):
                offenders.append(f"{py.relative_to(REPO_ROOT).as_posix()}:{line_no}: {call}")
        if offenders:
            self.fail(
                "Text-mode open() calls missing encoding='utf-8' "
                "(will corrupt on Windows cp1252):\n  "
                + "\n  ".join(offenders)
            )

    def test_no_path_text_io_without_encoding(self):
        """Path.read_text()/write_text() must declare encoding='utf-8'.

        The indexer, doc index, protocol dictionary and .gitignore scanner all
        called ``read_text(errors='replace')`` with no encoding. On Windows
        that decodes UTF-8 source as cp1252, and ``errors='replace'`` means it
        never raises — every em dash landed in .c3/index, the doc index and the
        chroma collection as the three-char mojibake this module's
        ``_MOJIBAKE_EM_DASH`` matches. Source files were never touched; the
        corruption lived only in the indexed copies served back to the agent.
        """
        offenders = []
        for py in _iter_py_files():
            # Shipped code only. Test fixtures write their own temp files and
            # control both ends of the encoding.
            if "tests" in py.relative_to(REPO_ROOT).parts:
                continue
            try:
                text = py.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                self.fail(f"{py} is not valid UTF-8: {e}")
            for line_no, call in _find_path_text_calls_missing_encoding(text):
                offenders.append(
                    f"{py.relative_to(REPO_ROOT).as_posix()}:{line_no}: {call}")
        if offenders:
            self.fail(
                "read_text()/write_text() calls missing encoding='utf-8' "
                "(silently mojibake non-ASCII on Windows cp1252):\n  "
                + "\n  ".join(offenders)
            )

    def test_no_mojibake_bytes_in_sources(self):
        """No .py file should contain the UTF-8-re-encoded-as-cp1252 em-dash signature.

        If this test ever fails, a file was written by a writer that didn't
        declare encoding and Windows default cp1252 produced the mojibake.
        """
        offenders = []
        for py in _iter_py_files():
            try:
                raw = py.read_bytes()
            except Exception:
                continue
            if _MOJIBAKE_EM_DASH in raw:
                idx = raw.find(_MOJIBAKE_EM_DASH)
                offenders.append(
                    f"{py.relative_to(REPO_ROOT).as_posix()} @ byte {idx}"
                )
        if offenders:
            self.fail(
                "Mojibake byte sequence found — re-save as UTF-8:\n  "
                + "\n  ".join(offenders)
            )


if __name__ == "__main__":
    unittest.main()
