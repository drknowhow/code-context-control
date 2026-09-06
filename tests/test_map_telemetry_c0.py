"""C0 of the c3_compress remediation: every map is measured.

Measured 2026-09-06 over this repo's .c3/tool_telemetry.jsonl: c3_compress
rows said WHICH file (target, since 2.111.0) but never which backend built
the map, which parser extracted the symbols, whether the record was already
fresh, or whether the map led to a targeted read. These tests pin:

- file_memory records carry `parser` (tree_sitter | regex | generic) and a
  pre-2.120.0 record without it is re-extracted on the next update;
- handle_compress / handle_read hand a flat `detail` to the telemetry row for
  every map they serve (file_memory maps, compressor modes, batches, the two
  c3_read map fallbacks) and for source reads;
- aggregate_tool_telemetry folds those into `map_by_backend` and measures
  map → read adjacency in `map_read_chain` from `target` alone, so the chain
  works on rows written before this release too.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools.compress import handle_compress  # noqa: E402
from cli.tools.read import handle_read  # noqa: E402
from services.compressor import CodeCompressor  # noqa: E402
from services.file_memory import FileMemoryStore  # noqa: E402
from services.session_manager import SessionManager  # noqa: E402
from services.telemetry import (  # noqa: E402
    MAP_CHAIN_WINDOW,
    aggregate_tool_telemetry,
    append_telemetry_record,
)

PY_SRC = '''"""module doc"""
import os
import sys


CONST = 1


class Thing:
    def run(self, a: int, b: str = "x") -> bool:
        return True


def helper(x):
    return x
''' + "".join(
    f'''

def worker_{i}(value: int, scale: float = 1.0, *, label: str = "w{i}") -> dict:
    """Worker {i}: multiply value by scale and label the result."""
    total = value * scale
    if total > {i * 10}:
        total = total - {i}
    return {{"label": label, "total": total, "index": {i}}}
''' for i in range(12))


def _project(tmp: str) -> SimpleNamespace:
    root = Path(tmp)
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text(PY_SRC, encoding="utf-8")
    (root / "notes.r").write_text("f <- function(a) a\n", encoding="utf-8")
    sm = SessionManager(tmp)
    sm.start_session("t")
    svc = SimpleNamespace(
        project_path=tmp,
        file_memory=FileMemoryStore(tmp),
        compressor=CodeCompressor(str(root / ".c3" / "cache"), project_root=tmp),
        session_mgr=sm,
        hybrid_config={},
        activity_log=None,
        edit_ledger=None,
    )
    return svc


def _finalize_via(sm):
    def finalize(name, args, resp, summ, **kw):
        sm.log_tool_call(name, args, summ)
        sm.track_response(name, resp, kw.get("response_tokens", 0))
        return resp
    return finalize


def _rows(tmp: str) -> list:
    path = Path(tmp) / ".c3" / "tool_telemetry.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _no_facts(*_a, **_k):
    return ""


class TestParserAttribution(unittest.TestCase):
    def test_record_names_its_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _project(tmp)
            self.assertEqual(svc.file_memory.update("pkg/mod.py")["parser"], "tree_sitter")
            self.assertEqual(svc.file_memory.update("notes.r")["parser"], "regex")
            Path(tmp, "x.java").write_text("class A {}\n", encoding="utf-8")
            self.assertEqual(svc.file_memory.update("x.java")["parser"], "generic")

    def test_pre_2_120_record_is_re_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _project(tmp)
            rec = svc.file_memory.update("pkg/mod.py")
            rec.pop("parser")
            svc.file_memory._save("pkg/mod.py", rec)
            self.assertNotIn("parser", svc.file_memory.get("pkg/mod.py"))
            self.assertEqual(svc.file_memory.update("pkg/mod.py")["parser"], "tree_sitter")


class TestCompressDetail(unittest.TestCase):
    def test_map_mode_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _project(tmp)
            fin = _finalize_via(svc.session_mgr)
            handle_compress("pkg/mod.py", "map", svc, fin, _no_facts)
            handle_compress("pkg/mod.py", "map", svc, fin, _no_facts)
            rows = [r for r in _rows(tmp) if r["tool"] == "c3_compress"]
            self.assertEqual(len(rows), 2)
            first, second = (r["detail"] for r in rows)
            self.assertEqual(first["backend"], "file_memory")
            self.assertEqual(first["requested_mode"], "map")
            self.assertEqual(first["parser"], "tree_sitter")
            self.assertFalse(first["cache_hit"])
            self.assertGreater(first["sections"], 0)
            self.assertTrue(second["cache_hit"])
            self.assertEqual(rows[0]["target"], "pkg/mod.py")
            self.assertGreater(rows[0]["raw_tokens"], rows[0]["optimized_tokens"])

    def test_retired_mode_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _project(tmp)
            fin = _finalize_via(svc.session_mgr)
            out = handle_compress("pkg/mod.py", "smart", svc, fin, _no_facts)
            self.assertTrue(out.startswith("[compress:deprecated] mode 'smart'"))
            self.assertIn("C Thing [L", out)
            rows = [r["detail"] for r in _rows(tmp) if r["tool"] == "c3_compress"]
            self.assertEqual(rows[0]["backend"], "file_memory")
            self.assertEqual(rows[0]["requested_mode"], "smart")
            self.assertEqual(rows[0]["deprecated_mode"], "smart")
            self.assertEqual(rows[0]["parser"], "tree_sitter")

    def test_batch_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _project(tmp)
            fin = _finalize_via(svc.session_mgr)
            handle_compress("pkg/mod.py,notes.r,missing.py", "map", svc, fin, _no_facts)
            rows = [r for r in _rows(tmp) if r["tool"] == "c3_compress"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["detail"],
                             {"requested_mode": "map", "backend": "batch", "files": 3, "ok": 2})


class TestReadDetail(unittest.TestCase):
    def test_source_read_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _project(tmp)
            fin = _finalize_via(svc.session_mgr)
            out = handle_read("pkg/mod.py", ["helper"], None, True, svc, fin)
            self.assertIn("def helper(x):", out)
            row = [r for r in _rows(tmp) if r["tool"] == "c3_read"][-1]
            d = row["detail"]
            self.assertEqual(d["backend"], "source")
            self.assertEqual(d["symbols"], 1)
            self.assertEqual(d["ranges"], 1)
            self.assertFalse(d["by_lines"])
            self.assertEqual(d["lines_served"], 2)
            self.assertEqual(d["file_lines"], PY_SRC.count("\n"))
            self.assertLess(row["optimized_tokens"], row["raw_tokens"])

    def test_map_only_fallback_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _project(tmp)
            fin = _finalize_via(svc.session_mgr)
            handle_read("pkg/mod.py", None, None, True, svc, fin)
            d = [r for r in _rows(tmp) if r["tool"] == "c3_read"][-1]["detail"]
            self.assertEqual(d["backend"], "file_memory")
            self.assertEqual(d["fallback"], "map_only")
            self.assertEqual(d["parser"], "tree_sitter")

    def test_symbols_not_found_fallback_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _project(tmp)
            fin = _finalize_via(svc.session_mgr)
            handle_read("pkg/mod.py", ["nope_zz"], None, True, svc, fin)
            d = [r for r in _rows(tmp) if r["tool"] == "c3_read"][-1]["detail"]
            self.assertEqual(d["fallback"], "symbols_not_found")
            self.assertEqual(d["symbols"], 1)


class TestAggregate(unittest.TestCase):
    def _write(self, tmp, tool, target, detail=None, raw=None, opt=None, sid="s1"):
        append_telemetry_record(tmp, {
            "session_id": sid, "tool": tool, "target": target,
            "response_tokens": opt or 5, "raw_tokens": raw, "optimized_tokens": opt,
            **({"detail": detail} if detail else {}),
        })

    def test_map_by_backend_folds_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "c3_compress", "a.py", raw=1000, opt=100,
                        detail={"requested_mode": "map", "backend": "file_memory",
                                "parser": "tree_sitter", "cache_hit": True, "sections": 4})
            self._write(tmp, "c3_compress", "b.py", raw=1000, opt=300,
                        detail={"requested_mode": "map", "backend": "file_memory",
                                "parser": "regex", "cache_hit": False, "sections": 2})
            self._write(tmp, "c3_compress", "c.py", raw=500, opt=50,
                        detail={"requested_mode": "smart", "backend": "compressor",
                                "actual_mode": "outline", "cache_hit": False})
            self._write(tmp, "c3_read", "a.py", raw=1000, opt=120,
                        detail={"backend": "file_memory", "parser": "tree_sitter",
                                "cache_hit": True, "fallback": "map_only", "sections": 4})
            self._write(tmp, "c3_read", "a.py", raw=1000, opt=40,
                        detail={"backend": "source", "symbols": 1, "ranges": 1})
            agg = aggregate_tool_telemetry(tmp, days=0)["map_by_backend"]
            ts = agg["file_memory/tree_sitter"]
            self.assertEqual(ts["calls"], 2)
            self.assertEqual(ts["cache_hits"], 2)
            self.assertEqual(ts["map_fallbacks"], 1)
            self.assertEqual(ts["measured_calls"], 2)
            self.assertAlmostEqual(ts["ratio_p50"], 0.12, places=2)
            self.assertEqual(agg["file_memory/regex"]["calls"], 1)
            self.assertEqual(agg["compressor/outline"]["calls"], 1)
            self.assertEqual(agg["source"]["symbol_reads"], 1)

    def test_map_read_chain_from_targets_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            # map a.py -> read a.py (followed); map b.py -> nothing; read c.py cold
            self._write(tmp, "c3_compress", "a.py")
            self._write(tmp, "c3_read", "a.py")
            self._write(tmp, "c3_compress", "b.py")
            self._write(tmp, "c3_read", "c.py")
            # another session: a map far outside the window does not count
            self._write(tmp, "c3_compress", "d.py", sid="s2")
            for i in range(MAP_CHAIN_WINDOW):
                self._write(tmp, "c3_read", f"z{i}.py", sid="s2")
            self._write(tmp, "c3_read", "d.py", sid="s2")
            chain = aggregate_tool_telemetry(tmp, days=0)["map_read_chain"]
            self.assertEqual(chain["window"], MAP_CHAIN_WINDOW)
            self.assertEqual(chain["maps"], 3)
            self.assertEqual(chain["maps_followed_by_read"], 1)
            self.assertEqual(chain["reads"], 2 + MAP_CHAIN_WINDOW + 1)
            self.assertEqual(chain["reads_preceded_by_map"], 1)
            self.assertAlmostEqual(chain["map_follow_rate"], 1 / 3, places=3)

    def test_read_map_fallback_counts_as_a_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "c3_read", "a.py", detail={"backend": "file_memory",
                                                          "fallback": "map_only"})
            self._write(tmp, "c3_read", "a.py", detail={"backend": "source", "symbols": 1})
            chain = aggregate_tool_telemetry(tmp, days=0)["map_read_chain"]
            self.assertEqual(chain["maps"], 1)
            self.assertEqual(chain["maps_followed_by_read"], 1)
            self.assertEqual(chain["reads"], 1)
            self.assertEqual(chain["reads_preceded_by_map"], 1)


if __name__ == "__main__":
    unittest.main()
