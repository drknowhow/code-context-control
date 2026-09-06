"""C4 of the c3_compress remediation: large files are bounded.

Measured before this phase: a 680 KB minified bundle took 176 s to map and
rendered 199k tokens; a 250 KB generated module rendered 117k tokens. The
cost was copying the whole line into every symbol's signature and then
rendering every symbol. These tests pin the bounds in docs/file-map.md
§ Large files: 400-char signature slices, minified detection with the
binding soup dropped, the oversized skip, the parse deadline's lexical
fallback, and the 6,000-token map budget (`lines=<int>` on a file stays a line).
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools.read import handle_read  # noqa: E402
from core import count_tokens  # noqa: E402
from services import file_memory as fm_mod  # noqa: E402
from services import parser as parser_mod  # noqa: E402
from services.file_memory import FileMemoryStore  # noqa: E402


def _minified(named: int = 6, soup: int = 600) -> str:
    body = "var a=1;" + "".join(f"var v{i}=function(x){{return x+{i}}};" for i in range(soup))
    body += "".join(f"function planted{i}(a,b){{return a+b+{i}}};" for i in range(named))
    return body + "\n"


def _svc(tmp):
    return SimpleNamespace(project_path=tmp, file_memory=FileMemoryStore(tmp),
                           edit_ledger=None, session_mgr=None, hybrid_config={})


class TestBoundedSignatures(unittest.TestCase):
    def test_signature_is_sliced_not_the_whole_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "b.min.js").write_text(_minified(), encoding="utf-8")
            store = FileMemoryStore(tmp)
            t0 = time.monotonic()
            rec = store.update("b.min.js")
            self.assertLess(time.monotonic() - t0, 5.0)
            longest = max(len(s.get("signature") or "") for s in rec["sections"])
            self.assertLessEqual(longest, parser_mod.SIGNATURE_MAX_CHARS + 1)
            raw = json.dumps(rec)
            self.assertLess(len(raw), 400_000)  # was 11 MB for a 20 KB line
            planted = [s for s in rec["sections"] if s["name"].startswith("planted")]
            self.assertEqual(len(planted), 6)
            self.assertTrue(planted[0]["signature"].startswith("function planted0(a,b)"))


class TestMinifiedAndOversized(unittest.TestCase):
    def test_minified_map_keeps_declarations_drops_soup(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "b.min.js").write_text(_minified(), encoding="utf-8")
            store = FileMemoryStore(tmp)
            text = store.get_or_build_map("b.min.js")
            self.assertEqual(store.get("b.min.js")["shape"]["minified"], True)
            self.assertIn("[map:minified]", text)
            self.assertIn("601 var bindings omitted", text)
            self.assertIn("F planted0(a,b) [L1-L1]", text)
            self.assertNotIn("V v1 ", text)
            self.assertLess(count_tokens(text), 600)

    def test_oversized_is_not_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "huge.py").write_text("x = 1\n" * (FileMemoryStore.MAX_PARSE_BYTES // 6 + 10),
                                          encoding="utf-8")
            store = FileMemoryStore(tmp)
            with patch.object(fm_mod, "extract_sections_ast",
                              side_effect=AssertionError("must not parse")):
                text = store.get_or_build_map("huge.py")
            self.assertEqual(store.get("huge.py")["parser"], "skipped")
            self.assertIn("[map:not mapped]", text)
            self.assertIn("read with lines=[a,b]", text)
            self.assertLess(len(text.splitlines()), 4)

    def test_generated_is_labelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "gen.py").write_text("# AUTO-GENERATED — do not edit\n\ndef f(a):\n    return a\n",
                                         encoding="utf-8")
            text = FileMemoryStore(tmp).get_or_build_map("gen.py")
            self.assertIn("[map:generated]", text)
            self.assertIn("F f(a) [L3-L4]", text)


class TestDeadline(unittest.TestCase):
    def test_overrun_falls_back_to_the_line_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "slow.py").write_text("import os\n\ndef alpha(a):\n    return a\n\n"
                                          "class Beta:\n    def run(self):\n        return 1\n",
                                          encoding="utf-8")
            store = FileMemoryStore(tmp)

            def _slow(content, ext):
                time.sleep(FileMemoryStore.PARSE_DEADLINE_S + 0.5)
                return []

            with patch.object(FileMemoryStore, "PARSE_DEADLINE_S", 0.2), \
                    patch.object(fm_mod, "extract_sections_ast", side_effect=_slow):
                text = store.get_or_build_map("slow.py")
            rec = store.get("slow.py")
            self.assertEqual(rec["parser"], "lexical")
            self.assertIn("[map:lexical-fallback]", text)
            self.assertIn("F alpha(a) [L3-L4]", text)
            self.assertIn("C Beta [L6-L8]", text)
            self.assertIn("  M Beta.run(self) [L7-L8]", text)


class TestBudget(unittest.TestCase):
    def _big(self, tmp, n=400):
        Path(tmp, "big.py").write_text(
            "".join(f"def f_{i}(a, b={i}):\n    return a + b\n\n" for i in range(n)), encoding="utf-8")

    def test_map_is_budgeted_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._big(tmp)
            svc = _svc(tmp)
            with patch.object(FileMemoryStore, "MAP_TOKEN_BUDGET", 300):
                out = handle_read("big.py", None, None, True, svc, None)
            self.assertLessEqual(count_tokens(out), 300 + 80)
            self.assertIn("more symbols (map budget 300 tokens", out)
            self.assertIn("F f_0(a, b=0) [L1-L2]", out)

    def test_lines_int_still_reads_a_line(self):
        # lines=<int> on a FILE is a line number, never a budget (that
        # meaning belongs to directory maps, which have no line numbers).
        with tempfile.TemporaryDirectory() as tmp:
            self._big(tmp, 5)
            out = handle_read("big.py", None, 1, True, _svc(tmp),
                              lambda n, a, r, summ, **kw: r)
            self.assertIn("def f_0(a, b=0):", out)
            self.assertNotIn("more symbols", out)

    def test_full_map_is_still_cached_unbudgeted(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._big(tmp, 50)
            store = FileMemoryStore(tmp)
            full = store.get_or_build_map("big.py")
            short = store.get_or_build_map("big.py", max_tokens=100)
            self.assertNotIn("more symbols", full)
            self.assertIn("more symbols", short)
            self.assertEqual(store.get_or_build_map("big.py"), full)


if __name__ == "__main__":
    unittest.main()
