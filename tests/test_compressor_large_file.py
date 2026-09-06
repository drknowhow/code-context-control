"""Tests for the compressor large-file fast path (P8a, Stream E) and the
per-extension AST parse-failure memo.

Fast path: files exceeding LARGE_FILE_LINE_THRESHOLD lines OR
LARGE_FILE_BYTE_THRESHOLD bytes skip the full regex/AST pipeline and return
a cheap bounded-scan header with explicit guidance text, while keeping the
normal result contract (compressed/mode/filepath + measure_savings keys)
intact so callers and raw->compressed token accounting are unaffected.

Memo: when tree-sitter parsing RAISES for an extension, that extension is
memoized in AST_PARSE_FAILURES so subsequent files with the same extension
skip straight to the regex/generic fallback without re-attempting the parse.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.compressor as compressor_mod  # noqa: E402
from services.compressor import (  # noqa: E402
    AST_PARSE_FAILURES,
    LARGE_FILE_BYTE_THRESHOLD,
    LARGE_FILE_GUIDANCE,
    LARGE_FILE_LINE_THRESHOLD,
    LARGE_FILE_PREVIEW_LINES,
    CodeCompressor,
)

# Keys every successful compress_file result must expose (existing contract:
# cli/tools/compress.py builds its summary as
# f"{res['original_tokens']}->{res['compressed_tokens']}tok").
CONTRACT_KEYS = {
    "compressed", "mode", "filepath",
    "original_tokens", "compressed_tokens", "saved_tokens", "savings_pct",
}


def _make_python_source(n_lines: int, tag: str, n_imports: int = 3) -> str:
    """Synthetic Python source: imports + a def at the head, filler body.

    Body lines are short and lowercase so the byte threshold is NOT crossed
    for ~10k lines and so they do not match the CONSTANT-assignment pattern —
    only the head imports/def should appear in a fast-path preview.
    """
    head = [f"import mod_{tag}_{j}" for j in range(n_imports)]
    head += [
        "",
        f"def head_func_{tag}(a, b):",
        "    return a + b",
        "",
    ]
    body = [f"v{i} = {i}" for i in range(n_lines - len(head))]
    return "\n".join(head + body) + "\n"


class TestLargeFileFastPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache_dir = self.root / "cache"
        self.comp = CodeCompressor(cache_dir=str(self.cache_dir),
                                   project_root=str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, content: str) -> Path:
        p = self.root / name
        p.write_text(content, encoding="utf-8")
        return p

    # ── trigger conditions ──────────────────────────────────────────────

    def test_triggers_above_line_threshold(self):
        content = _make_python_source(LARGE_FILE_LINE_THRESHOLD + 500, "big")
        # Guard: this fixture must exercise the LINE trigger, not the byte one
        self.assertLess(len(content.encode("utf-8")), LARGE_FILE_BYTE_THRESHOLD)
        fpath = self._write("big.py", content)

        res = self.comp.compress_file(str(fpath), "structure")

        self.assertNotIn("error", res)
        self.assertEqual(res.get("fast_path"), "large_file")
        self.assertEqual(res["line_count"], LARGE_FILE_LINE_THRESHOLD + 500)
        self.assertIn(LARGE_FILE_GUIDANCE, res["compressed"])
        self.assertIn("[large-file fast path]", res["compressed"])
        # Head imports/signature survive via the bounded scan
        self.assertIn("import mod_big_0", res["compressed"])
        self.assertIn("def head_func_big", res["compressed"])

    def test_triggers_at_exact_line_threshold(self):
        content = _make_python_source(LARGE_FILE_LINE_THRESHOLD, "edge")
        fpath = self._write("edge.py", content)
        res = self.comp.compress_file(str(fpath), "structure")
        self.assertEqual(res.get("fast_path"), "large_file")

    def test_triggers_on_byte_threshold_alone(self):
        # Few lines, but each is huge: bytes trip the fast path, lines do not.
        n_lines = 60
        line_len = (LARGE_FILE_BYTE_THRESHOLD // n_lines) + 100
        lines = ["import json_heavy_fixture"]
        lines += ["y = '" + "a" * line_len + "'" for _ in range(n_lines - 1)]
        content = "\n".join(lines) + "\n"
        self.assertLess(n_lines, LARGE_FILE_LINE_THRESHOLD)
        fpath = self._write("wide.py", content)

        res = self.comp.compress_file(str(fpath), "structure")

        self.assertEqual(res.get("fast_path"), "large_file")
        self.assertIn(LARGE_FILE_GUIDANCE, res["compressed"])

    def test_off_below_threshold(self):
        content = _make_python_source(200, "small")
        fpath = self._write("small.py", content)

        res = self.comp.compress_file(str(fpath), "structure")

        self.assertNotIn("error", res)
        self.assertNotIn("fast_path", res)
        self.assertNotIn(LARGE_FILE_GUIDANCE, res["compressed"])
        self.assertNotIn("[large-file fast path]", res["compressed"])
        # Normal pipeline still extracts real structure
        self.assertIn("head_func_small", res["compressed"])
        self.assertTrue(CONTRACT_KEYS.issubset(res.keys()))
        self.assertEqual(res["mode"], "structure")

    # ── result contract ─────────────────────────────────────────────────

    def test_result_contract_and_token_summary(self):
        content = _make_python_source(LARGE_FILE_LINE_THRESHOLD + 100, "ctr")
        fpath = self._write("contract.py", content)

        res = self.comp.compress_file(str(fpath), "structure")

        self.assertTrue(CONTRACT_KEYS.issubset(res.keys()),
                        f"missing keys: {CONTRACT_KEYS - set(res.keys())}")
        self.assertEqual(res["mode"], "structure")
        self.assertEqual(res["filepath"], str(fpath.resolve()))
        self.assertIsInstance(res["original_tokens"], int)
        self.assertIsInstance(res["compressed_tokens"], int)
        self.assertGreater(res["original_tokens"], res["compressed_tokens"])
        # The raw->compressed summary used by cli/tools/compress.py must build
        summary = f"{res['original_tokens']}->{res['compressed_tokens']}tok"
        self.assertRegex(summary, r"^\d+->\d+tok$")
        # JSON-serializable (disk cache format)
        json.dumps(res)

    def test_preview_capped_at_max_lines(self):
        # 60 imports at the head; the preview must stop at the cap.
        content = _make_python_source(
            LARGE_FILE_LINE_THRESHOLD + 100, "cap", n_imports=60)
        fpath = self._write("cap.py", content)

        res = self.comp.compress_file(str(fpath), "structure")

        preview_lines = [ln for ln in res["compressed"].splitlines()
                         if ln.startswith("import mod_cap_")]
        self.assertEqual(len(preview_lines), LARGE_FILE_PREVIEW_LINES)

    def test_unknown_extension_uses_generic_preview(self):
        lines = ["include stdlib", "function do_thing(x) {"]
        lines += [f"row {i}" for i in range(LARGE_FILE_LINE_THRESHOLD + 50)]
        content = "\n".join(lines) + "\n"
        fpath = self._write("blob.xyz", content)

        res = self.comp.compress_file(str(fpath), "structure")

        self.assertEqual(res.get("fast_path"), "large_file")
        self.assertIn("xyz", res["compressed"])  # language fallback = extension
        self.assertIn("include stdlib", res["compressed"])
        self.assertIn("function do_thing", res["compressed"])

    # ── cache interplay ─────────────────────────────────────────────────

    def test_fast_path_result_is_cached(self):
        content = _make_python_source(LARGE_FILE_LINE_THRESHOLD + 100, "csh")
        fpath = self._write("cached_out.py", content)

        res1 = self.comp.compress_file(str(fpath), "structure")
        h = hashlib.md5(content.encode()).hexdigest()
        cache_file = self.cache_dir / f"{h}_structure.py.json"
        self.assertTrue(cache_file.exists())

        res2 = self.comp.compress_file(str(fpath), "structure")
        self.assertEqual(res1["compressed"], res2["compressed"])
        self.assertEqual(res2["filepath"], str(fpath.resolve()))

    def test_preexisting_cache_entry_wins_over_fast_path(self):
        # Existing disk-cache entries (same content-hash key format) must be
        # returned untouched — the fast path never overrides them.
        content = _make_python_source(LARGE_FILE_LINE_THRESHOLD + 100, "pre")
        fpath = self._write("preexisting.py", content)
        h = hashlib.md5(content.encode()).hexdigest()
        entry = {"compressed": "CACHED SENTINEL", "mode": "structure",
                 "original_tokens": 5, "compressed_tokens": 2,
                 "saved_tokens": 3, "savings_pct": 60.0}
        (self.cache_dir / f"{h}_structure.py.json").write_text(
            json.dumps(entry), encoding="utf-8")

        res = self.comp.compress_file(str(fpath), "structure")

        self.assertEqual(res["compressed"], "CACHED SENTINEL")
        self.assertNotIn("fast_path", res)

    # ── mode exemptions ─────────────────────────────────────────────────

    def test_diff_mode_is_retired(self):
        # diff was retired in 2.122.0: it cached full copies of every file
        # keyed by basename. The answer is an error naming the replacement,
        # never a fast-path header.
        content = _make_python_source(LARGE_FILE_LINE_THRESHOLD + 100, "dif")
        fpath = self._write("diffable.py", content)

        res = self.comp.compress_file(str(fpath), "diff")

        self.assertTrue(res.get("retired"))
        self.assertIn("retired in 2.122.0", res["error"])
        self.assertNotIn("fast_path", res)

    def test_map_mode_is_the_canonical_map_even_for_large_files(self):
        content = _make_python_source(LARGE_FILE_LINE_THRESHOLD + 100, "mp")
        fpath = self._write("mapbig.py", content)
        res = self.comp.compress_file(str(fpath), "map")
        self.assertEqual(res["mode"], "map")
        self.assertNotIn("fast_path", res)
        self.assertTrue(res["compressed"].startswith("# "))
        self.assertIn("[L", res["compressed"])

    def test_smart_mode_takes_fast_path(self):
        content = _make_python_source(LARGE_FILE_LINE_THRESHOLD + 100, "smt")
        fpath = self._write("smartbig.py", content)
        res = self.comp.compress_file(str(fpath), "smart")
        self.assertEqual(res.get("fast_path"), "large_file")
        self.assertEqual(res["mode"], "smart")


class TestAstParseFailureMemo(unittest.TestCase):
    def setUp(self):
        self._saved_failures = dict(AST_PARSE_FAILURES)
        AST_PARSE_FAILURES.clear()
        self._orig_has_ts = compressor_mod.HAS_TREE_SITTER
        self._orig_extract = compressor_mod.extract_sections_ast
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.comp = CodeCompressor(cache_dir=str(self.root / "cache"),
                                   project_root=str(self.root))

    def tearDown(self):
        compressor_mod.HAS_TREE_SITTER = self._orig_has_ts
        compressor_mod.extract_sections_ast = self._orig_extract
        AST_PARSE_FAILURES.clear()
        AST_PARSE_FAILURES.update(self._saved_failures)
        self.tmp.cleanup()

    def _install_failing_parser(self):
        calls = []

        def failing_parser(content, ext):
            calls.append(ext)
            raise RuntimeError("synthetic parser explosion")

        compressor_mod.HAS_TREE_SITTER = True
        compressor_mod.extract_sections_ast = failing_parser
        return calls

    def test_failure_memoized_and_short_circuits(self):
        calls = self._install_failing_parser()

        out1 = self.comp._extract_structure(
            "def alpha():\n    pass\n", ".py", "structure")
        self.assertEqual(len(calls), 1)
        self.assertIn(".py", AST_PARSE_FAILURES)
        self.assertIn("RuntimeError", AST_PARSE_FAILURES[".py"])
        self.assertIn("def alpha", out1)  # regex fallback still worked

        out2 = self.comp._extract_structure(
            "def beta():\n    pass\n", ".py", "structure")
        self.assertEqual(len(calls), 1)  # parser NOT re-attempted for .py
        self.assertIn("def beta", out2)

    def test_memo_is_per_extension(self):
        calls = self._install_failing_parser()

        self.comp._extract_structure("def a():\n    pass\n", ".py", "structure")
        self.assertEqual(len(calls), 1)
        # A different extension still gets its own (single) attempt
        self.comp._extract_structure("function b() {}\n", ".js", "structure")
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(AST_PARSE_FAILURES), {".py", ".js"})

    def test_none_result_is_not_memoized(self):
        # Returning None (unsupported language) is NOT a parse failure —
        # the parser must be attempted again for the next file.
        calls = []

        def none_parser(content, ext):
            calls.append(ext)
            return None

        compressor_mod.HAS_TREE_SITTER = True
        compressor_mod.extract_sections_ast = none_parser

        self.comp._extract_structure("def a():\n    pass\n", ".py", "structure")
        self.comp._extract_structure("def b():\n    pass\n", ".py", "structure")
        self.assertEqual(len(calls), 2)
        self.assertEqual(AST_PARSE_FAILURES, {})

    def test_end_to_end_compress_file_uses_memo(self):
        calls = self._install_failing_parser()

        f1 = self.root / "one.py"
        f1.write_text("def one_func():\n    return 1\n", encoding="utf-8")
        f2 = self.root / "two.py"
        f2.write_text("def two_func():\n    return 2\n", encoding="utf-8")

        res1 = self.comp.compress_file(str(f1), "structure")
        res2 = self.comp.compress_file(str(f2), "structure")

        self.assertNotIn("error", res1)
        self.assertNotIn("error", res2)
        self.assertIn("one_func", res1["compressed"])
        self.assertIn("two_func", res2["compressed"])
        # Parser raised once for .py, then the memo short-circuited
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
