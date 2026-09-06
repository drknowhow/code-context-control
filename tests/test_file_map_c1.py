"""C1 of the c3_compress remediation: one canonical map, everywhere.

Before 2.121.0 three different "maps" existed (file_memory's emoji map,
compressor.compress_file(path, "map") which was not a mode and fell through
to a structure-only summary, and search's 600-token truncation of the
first), the emoji map spent 24% of its tokens on chrome, truncated
parameter lists at 60 characters, and opened with an Ollama summary
generated from symbol names alone. These tests pin the grammar in
docs/file-map.md and the wiring:

- render_map: one line per symbol `K Qualified.name(params) -> ret [La-Lb]`,
  no emoji/padding/summary, imports collapse above six, methods qualify,
  flat parsers (Rust) nest by containment, max_tokens shortens from the end;
- parse_signature: full multi-line params and return types per language;
- FileMemoryStore renders through it, stores posix paths, purges the old
  summaries once, and resolves `Class.method` in get_symbol_ranges;
- the search prefetch and agent maps come from the same renderer.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import count_tokens  # noqa: E402
from services.file_map import (  # noqa: E402
    SYMBOL_LINE_RE,
    parse_map,
    parse_signature,
    render_map,
)
from services.file_memory import FileMemoryStore  # noqa: E402

PY_SRC = '''"""Module doc."""
import os
import sys
import json
import re
import time
import math
import hashlib

LIMIT = 10


class Base:
    """A base."""

    def run(self, a: int) -> bool:
        """Run base."""
        return True

    @property
    def name(self) -> str:
        return "base"


class Child(Base):
    async def run(self, a: int,
                  b: str = "x",
                  *, c: float = 1.0) -> bool:
        """Run child. Second sentence."""
        return False


def helper(x):
    def inner(y):
        return y
    return inner(x)


async def fetch(url: str) -> bytes:
    return b""
'''


class TestRenderGrammar(unittest.TestCase):
    def _record(self, tmp):
        Path(tmp, "pkg").mkdir()
        Path(tmp, "pkg", "m.py").write_text(PY_SRC, encoding="utf-8")
        store = FileMemoryStore(tmp)
        return store, store.update("pkg/m.py")

    def test_python_map_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, rec = self._record(tmp)
            text = render_map(rec)
            lines = text.splitlines()
            self.assertEqual(lines[0], f"# pkg/m.py ({PY_SRC.count(chr(10))}L python)")
            self.assertIn("I 7 imports", lines)
            self.assertIn("K LIMIT [L10-L10]", lines)
            self.assertIn("C Base [L13-L22]", lines)
            self.assertIn("  M Base.run(self, a: int) -> bool [L16-L18]", lines)
            self.assertIn("  P Base.name [L21-L22]", lines)
            self.assertIn("C Child(Base) [L25-L30]", lines)
            self.assertIn('  M async Child.run(self, a: int, b: str = "x", *, c: float = 1.0) -> bool [L26-L30]',
                          lines)
            self.assertIn("F helper(x) [L33-L36]", lines)
            self.assertIn("F async fetch(url: str) -> bytes [L39-L40]", lines)
            self.assertNotIn("inner", text)
            self.assertNotIn("...", text)
            for line in lines[1:]:
                self.assertTrue(SYMBOL_LINE_RE.match(line) or line.startswith("I "), line)
            # No chrome: no emoji, no double spaces after content.
            self.assertFalse(any(ord(ch) > 0x2000 for ch in text), "emoji/chrome in map")
            self.assertNotIn("  imports", text)

    def test_docs_only_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, rec = self._record(tmp)
            self.assertNotIn("Run child", render_map(rec))
            text = render_map(rec, include_docs=True)
            self.assertIn('    "Run child."', text)
            self.assertNotIn("Second sentence", text)

    def test_max_tokens_shortens_from_the_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, rec = self._record(tmp)
            full = render_map(rec)
            short = render_map(rec, max_tokens=40)
            note = short.splitlines()[-1]
            self.assertTrue(note.startswith("… "))
            self.assertIn("more symbols", note)
            self.assertLessEqual(count_tokens(short), 40 + count_tokens(note))
            self.assertTrue(short.startswith(full.splitlines()[0]))
            self.assertLess(len(short.splitlines()), len(full.splitlines()))

    def test_parse_map_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, rec = self._record(tmp)
            symbols = parse_map(render_map(rec))
            names = {s["name"] for s in symbols}
            self.assertIn("Child.run", names)
            child = next(s for s in symbols if s["name"] == "Child.run")
            self.assertTrue(child["async"])
            self.assertEqual(child["ret"], "bool")
            self.assertEqual((child["a"], child["b"]), (26, 30))
            self.assertEqual(child["depth"], 1)

    def test_few_imports_are_listed(self):
        rec = {"path": "a.py", "lines": 3, "language": "python", "sections": [
            {"type": "import", "name": "os", "line_start": 1, "line_end": 1, "signature": "import os"},
            {"type": "import", "name": "sys", "line_start": 2, "line_end": 2, "signature": "import sys"},
        ]}
        self.assertEqual(render_map(rec), "# a.py (3L python)\nI import os [L1-L1]\nI import sys [L2-L2]")

    def test_backslash_path_renders_posix(self):
        rec = {"path": "cli\\tools\\x.py", "lines": 1, "language": "python", "sections": []}
        self.assertEqual(render_map(rec), "# cli/tools/x.py (1L python)")

    def test_flat_rust_sections_nest_by_containment(self):
        rec = {"path": "lib.rs", "lines": 20, "language": "rust", "sections": [
            {"type": "struct", "name": "Point", "line_start": 1, "line_end": 4, "signature": "pub struct Point {"},
            {"type": "impl", "name": "impl Point", "line_start": 6, "line_end": 14, "signature": "impl Point {"},
            {"type": "function", "name": "new", "line_start": 7, "line_end": 9,
             "signature": "pub fn new(x: i32, y: i32) -> Self {"},
            {"type": "function", "name": "norm", "line_start": 11, "line_end": 13,
             "signature": "pub fn norm(&self) -> f64 {"},
            {"type": "function", "name": "free", "line_start": 16, "line_end": 18,
             "signature": "fn free(a: u8) {"},
        ]}
        lines = render_map(rec).splitlines()
        self.assertIn("S Point [L1-L4]", lines)
        self.assertIn("IM Point [L6-L14]", lines)
        self.assertIn("  F Point.new(x: i32, y: i32) -> Self [L7-L9]", lines)
        self.assertIn("  F Point.norm(&self) -> f64 [L11-L13]", lines)
        self.assertIn("F free(a: u8) [L16-L18]", lines)

    def test_markdown_headings_nest_by_level(self):
        rec = {"path": "d.md", "lines": 30, "language": "markdown", "sections": [
            {"type": "heading", "name": "h1: Title", "line_start": 1, "line_end": 30, "signature": "# Title"},
            {"type": "heading", "name": "h2: Part", "line_start": 5, "line_end": 20, "signature": "## Part"},
            {"type": "heading", "name": "h3: Sub", "line_start": 9, "line_end": 12, "signature": "### Sub"},
        ]}
        self.assertEqual(render_map(rec).splitlines()[1:],
                         ["H Title [L1-L30]", "  H Part [L5-L20]", "    H Sub [L9-L12]"])

    def test_no_summary_ever(self):
        rec = {"path": "a.py", "lines": 1, "language": "python", "summary": "FAKE PROSE",
               "sections": []}
        self.assertNotIn("FAKE", render_map(rec))


class TestParseSignature(unittest.TestCase):
    def test_python(self):
        self.assertEqual(parse_signature("def f(a,\n      b: int = 2) -> str:", "python"),
                         {"params": "a, b: int = 2", "ret": "str"})
        self.assertEqual(parse_signature("async def g():", "python"), {"params": ""})
        self.assertEqual(parse_signature("class A(B, C):", "python"), {"bases": "B, C"})
        self.assertEqual(parse_signature("class A:", "python"), {})

    def test_go(self):
        p = parse_signature("func (s *Server) Start(ctx context.Context, n int) (err error) {", "go")
        self.assertEqual(p["params"], "ctx context.Context, n int")
        self.assertEqual(p["receiver"], "Server")
        self.assertEqual(p["ret"], "err error")
        self.assertEqual(parse_signature("func main() {", "go"), {"params": ""})

    def test_rust(self):
        self.assertEqual(parse_signature("pub async fn run(&mut self, n: u32) -> Result<(), E> {", "rust"),
                         {"params": "&mut self, n: u32", "ret": "Result<(), E>"})

    def test_typescript(self):
        self.assertEqual(parse_signature("export async function load(id: string): Promise<User> {", "typescript"),
                         {"params": "id: string", "ret": "Promise<User>"})
        self.assertEqual(parse_signature("const add = (a: number, b: number): number => a + b", "typescript"),
                         {"params": "a: number, b: number", "ret": "number"})
        self.assertEqual(parse_signature("private render(state: State): void {", "typescript"),
                         {"params": "state: State", "ret": "void"})
        self.assertEqual(parse_signature("const id = x => x", "javascript"), {"params": "x"})


class TestStoreWiring(unittest.TestCase):
    def test_get_or_build_map_is_canonical_and_posix(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "cli").mkdir()
            Path(tmp, "cli", "x.py").write_text("def f(a, b):\n    return a\n", encoding="utf-8")
            store = FileMemoryStore(tmp)
            text = store.get_or_build_map("cli\\x.py")
            self.assertEqual(text, "# cli/x.py (2L python)\nF f(a, b) [L1-L2]")
            self.assertEqual(store.get("cli\\x.py")["path"], "cli/x.py")
            self.assertEqual(store.get_or_build_dense_map("cli/x.py"), text)
            self.assertNotIn("summary", store.get("cli/x.py"))

    def test_qualified_symbol_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "m.py").write_text(PY_SRC, encoding="utf-8")
            store = FileMemoryStore(tmp)
            store.update("m.py")
            hits = store.get_symbol_ranges("m.py", ["Child.run"], return_matches=True)
            self.assertEqual([(h["match"], h["range"]) for h in hits], [("Child.run", (26, 30))])
            bare = store.get_symbol_ranges("m.py", ["run"], return_matches=True)
            self.assertEqual(sorted(h["match"] for h in bare), ["Base.run", "Child.run"])
            self.assertEqual(store.get_symbol_ranges("m.py", ["Nope.run"]), [])

    def test_purge_summaries_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.py").write_text("X = 1\n", encoding="utf-8")
            store = FileMemoryStore(tmp)
            rec = store.update("a.py")
            rec["summary"] = "old prose"
            rec["path"] = "a.py"
            store._save("a.py", rec)
            self.assertIn("summary", store.get("a.py"))
            (store.store_dir / store._PURGE_MARKER).unlink()  # pretend pre-2.121.0
            fresh = FileMemoryStore(tmp)  # __init__ purges, marker-gated
            self.assertNotIn("summary", fresh.get("a.py"))
            self.assertEqual(fresh.purge_summaries(), 0)


class TestCallSites(unittest.TestCase):
    def test_search_prefetch_uses_the_renderer(self):
        from cli.tools import search as search_mod
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "m.py").write_text(PY_SRC, encoding="utf-8")
            store = FileMemoryStore(tmp)
            svc = SimpleNamespace(project_path=tmp, file_memory=store)
            out = search_mod._first_file_map("m.py", svc)
            self.assertIn("M Base.run(self, a: int) -> bool [L16-L18]", out)
            self.assertNotIn("[map truncated]", out)


if __name__ == "__main__":
    unittest.main()
