"""P1 correctness fixes for c3_search (docs/search-eval.md, plan phase P1).

Each test names the defect it pins:
- exact searched file_memory (files an agent had read), not the index universe
- exact had no ignore-case and no per-file cap
- files was a content search with the content hidden
- semantic's zero-result header promised a fallback that never ran
- recency mtimes were never persisted; the symbol map was never consulted
- a chunk larger than the budget was skipped instead of windowed
- markdown headings were one-line chunks; co-occurrence synonyms were always on
"""

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from cli.tools import search as search_mod
from cli.tools.search import handle_search
from services import access_guard as ag
from services.file_memory import FileMemoryStore
from services.indexer import CodeIndex


def _fin(_name, _args, resp, _summ="", **_kw):
    return resp


def _no_facts(*_a, **_kw):
    return ""


class _Project(unittest.TestCase):
    """A tiny indexed project with a mask rule available on demand."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        # Isolate from the developer's real ~/.c3 access rules: with any
        # global rule active the ripgrep pre-filter correctly stays off, which
        # would make the rg tests below meaningless on such a machine.
        self._home = mock.patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.write("pkg/auth.py",
                   "class OAuth2Client:\n"
                   "    def exchange_code(self, code):\n"
                   "        return code\n"
                   "\n"
                   "def sha256_digest(data):\n"
                   "    return data\n")
        self.write("pkg/limiter.py", "RATE = 1\n" + "".join(f"needle_{i} = {i}\n" for i in range(40)))
        self.write("web/InvoiceTable.tsx", "export function InvoiceTable() { return null; }\n")
        self.write("docs/guide.md", "# Guide\n\nintro\n\n## Setup\n\nstep one\nstep two\n\n## Usage\n\nrun it\n")
        self.write("configs/docker-compose.yml", "services: {}\n")
        self.write("data/customers.csv", "id,name,national_id\n1,Ann,CANARY-SSN-1\n2,Bob,CANARY-SSN-2\n")

    def tearDown(self):
        self._home.stop()
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def config(self, cfg: dict):
        (self.root / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def svc(self, *, index=True, file_memory_files=()):
        indexer = CodeIndex(str(self.root))
        if index:
            indexer.build_index()
        fm = FileMemoryStore(str(self.root))
        for rel in file_memory_files:
            fm.update(rel)
        return types.SimpleNamespace(project_path=str(self.root), indexer=indexer, file_memory=fm,
                                     embedding_index=None, hybrid_config={}, compressor=None,
                                     convo_store=None)


class TestExactSearch(_Project):
    def test_universe_is_the_index_not_file_memory(self):
        """Nothing tracked in file_memory; every indexed file is still searchable."""
        svc = self.svc(file_memory_files=())
        self.assertEqual(svc.file_memory.list_tracked(), [])
        out = handle_search("def exchange_code", "exact", 5, 1200, svc, _fin, _no_facts)
        self.assertIn("--- pkg/auth.py ---", out)
        self.assertIn(">L2: ", out)

    def test_ignore_case(self):
        svc = self.svc()
        self.assertIn("0 results", handle_search("oauth2client", "exact", 5, 1200, svc, _fin, _no_facts))
        out = handle_search("oauth2client", "exact", 5, 1200, svc, _fin, _no_facts, ignore_case=True)
        self.assertIn("--- pkg/auth.py ---", out)

    def test_zero_results_names_the_universe_size(self):
        svc = self.svc()
        out = handle_search("no_such_token_anywhere", "exact", 5, 1200, svc, _fin, _no_facts)
        self.assertRegex(out, r"0 results in \d+ files")

    def test_per_file_match_cap(self):
        svc = self.svc()
        out = handle_search(r"needle_\d+", "exact", 5, 2400, svc, _fin, _no_facts)
        self.assertIn("--- pkg/limiter.py ---", out)
        self.assertIn(f"[+{40 - search_mod._EXACT_MAX_MATCHES_PER_FILE} more matching lines", out)
        self.assertEqual(out.count(">L"), search_mod._EXACT_MAX_MATCHES_PER_FILE)

    def test_masked_file_is_scanned_through_the_view(self):
        self.config({"access": {"mask": [{"glob": "data/**", "preset": "redact_columns",
                                          "params": {"columns": ["national_id"]}}]}})
        svc = self.svc()
        with mock.patch.object(search_mod, "_ripgrep_path", return_value="/definitely/not/rg"):
            out = handle_search("CANARY-SSN-1", "exact", 5, 1200, svc, _fin, _no_facts)
        self.assertNotIn("CANARY-SSN-1\n", out.split("] 0 results", 1)[-1])
        self.assertIn("0 results", out)
        header = handle_search("id,name,national_id", "exact", 5, 1200, svc, _fin, _no_facts)
        self.assertIn("--- data/customers.csv ---", header)
        self.assertNotIn("CANARY", header.replace("id,name,national_id", ""))

    def test_ripgrep_is_not_used_when_rules_are_active(self):
        self.config({"access": {"deny": ["configs/**"]}})
        svc = self.svc()
        with mock.patch.object(search_mod, "_ripgrep_path", return_value="/some/rg"), \
             mock.patch.object(search_mod, "_rg_candidate_files") as rg:
            handle_search("services", "exact", 5, 1200, svc, _fin, _no_facts)
        rg.assert_not_called()

    def test_ripgrep_prefilter_only_narrows_the_manifest(self):
        svc = self.svc()
        with mock.patch.object(search_mod, "_ripgrep_path", return_value="/some/rg"), \
             mock.patch.object(search_mod, "_rg_candidate_files",
                               return_value=["pkg/auth.py", "not/indexed.py"]) as rg:
            out = handle_search("exchange_code", "exact", 5, 1200, svc, _fin, _no_facts)
        rg.assert_called_once()
        self.assertIn("--- pkg/auth.py ---", out)
        self.assertNotIn("not/indexed.py", out)

    def test_ripgrep_failure_falls_back_to_full_scan(self):
        svc = self.svc()
        with mock.patch.object(search_mod, "_ripgrep_path", return_value="/some/rg"), \
             mock.patch.object(search_mod, "_rg_candidate_files", return_value=None):
            out = handle_search("exchange_code", "exact", 5, 1200, svc, _fin, _no_facts)
        self.assertIn("--- pkg/auth.py ---", out)

    @unittest.skipUnless(shutil.which("rg"), "ripgrep not on PATH")
    def test_real_ripgrep_agrees_with_python_scan(self):
        svc = self.svc()
        with_rg = handle_search("exchange_code", "exact", 5, 1200, svc, _fin, _no_facts)
        with mock.patch.object(search_mod, "_ripgrep_path", return_value=None):
            without = handle_search("exchange_code", "exact", 5, 1200, svc, _fin, _no_facts)
        self.assertEqual(with_rg, without)


class TestFilesSearch(_Project):
    def _rows(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("- ")]

    def test_exact_name_first(self):
        out = handle_search("auth.py", "files", 5, 1200, self.svc(), _fin, _no_facts)
        self.assertTrue(self._rows(out)[0].startswith("- pkg"), out)
        self.assertIn("exact name", out)

    def test_substring_and_prefix(self):
        svc = self.svc()
        self.assertIn("InvoiceTable.tsx", handle_search("Invoi", "files", 5, 1200, svc, _fin, _no_facts))
        self.assertIn("limiter.py", handle_search("limit", "files", 5, 1200, svc, _fin, _no_facts))

    def test_glob(self):
        out = handle_search("configs/*.yml", "files", 5, 1200, self.svc(), _fin, _no_facts)
        self.assertIn("docker-compose.yml", out)
        self.assertIn("matches configs/*.yml", out)

    def test_falls_back_to_content_terms(self):
        """'docker compose' names no path; the TF-IDF path terms still find it."""
        out = handle_search("docker compose", "files", 5, 1200, self.svc(), _fin, _no_facts)
        self.assertIn("docker-compose.yml", out)

    def test_masked_file_discoverable_without_values(self):
        self.config({"access": {"mask": [{"glob": "data/**", "preset": "redact_columns",
                                          "params": {"columns": ["national_id"]}}]}})
        out = handle_search("customers.csv", "files", 5, 1200, self.svc(), _fin, _no_facts)
        self.assertIn("customers.csv", out)
        self.assertNotIn("CANARY", out)


class TestSemanticFallback(_Project):
    def test_zero_semantic_results_run_code_search(self):
        svc = self.svc()
        svc.embedding_index = types.SimpleNamespace(ready=True, search=lambda *a, **k: [])
        out = handle_search("exchange_code", "semantic", 3, 1200, svc, _fin, _no_facts)
        self.assertIn("code search instead", out)
        self.assertIn("exchange_code", out.split("\n", 1)[1])
        self.assertIn("auth.py", out)


class TestCodeSearchRelativeFilter(unittest.TestCase):
    def test_exact_symbol_hit_survives_the_relative_score_filter(self):
        """CodeIndex (windowed class, score 8.8) vs _FakeCodeIndex (test class,
        58.9): the 20%-of-best filter used to drop the real definition."""
        loud = {"file": "tests/test_fake.py", "name": "_FakeCodeIndex", "type": "class",
                "lines": "1-5", "tokens": 31, "score": 58.9, "content": "class _FakeCodeIndex: ..."}
        real = {"file": "services/indexer.py", "name": "CodeIndex", "type": "class",
                "lines": "53-90", "tokens": 429, "score": 8.8, "content": "class CodeIndex: ...",
                "windowed": True, "exact_symbol": True}
        svc = types.SimpleNamespace(project_path=tempfile.gettempdir(), hybrid_config={},
                                    indexer=types.SimpleNamespace(search=lambda *a, **k: [real, loud]))
        with mock.patch.object(search_mod, "_read_denied", return_value=False):
            out = search_mod._code_search("CodeIndex", 3, 1200, svc, _fin, _no_facts)
        self.assertIn("services/indexer.py", out)
        self.assertLess(out.index("services/indexer.py"), out.index("tests/test_fake.py"))


class TestIndexerFixes(_Project):
    def test_mtimes_persist_across_reload(self):
        svc = self.svc()
        self.assertTrue(svc.indexer._file_mtimes)
        fresh = CodeIndex(str(self.root))
        fresh._load_index()
        self.assertEqual(fresh._file_mtimes, svc.indexer._file_mtimes)

    def test_symbol_fast_path_puts_definition_first(self):
        # A test file mentions the symbol many times; the definition still wins.
        self.write("tests/test_auth.py",
                   "from pkg.auth import exchange_code\n" + "exchange_code\n" * 30 +
                   "def test_exchange_code():\n    assert exchange_code\n")
        svc = self.svc()
        top = svc.indexer.search("exchange_code", top_k=3, max_tokens=1200)[0]
        self.assertEqual(top["file"].replace("\\", "/"), "pkg/auth.py")
        self.assertTrue(top["name"].endswith("exchange_code"))

    def test_oversized_chunk_is_windowed_not_skipped(self):
        body = "class Ledger:\n" + "".join(
            f"    def method_{i}(self):\n        '''doc {i}'''\n        return {i}\n" for i in range(120))
        self.write("pkg/ledger.py", body)
        svc = self.svc()
        big = [c for c in svc.indexer.chunks.values() if c.get("name") == "Ledger"][0]
        self.assertGreater(big["tokens"], 400)
        hits = svc.indexer.search("Ledger", top_k=3, max_tokens=400)
        self.assertTrue(hits, "class chunk must be returned as a window, not dropped")
        top = hits[0]
        self.assertTrue(top.get("windowed"))
        self.assertLessEqual(top["tokens"], 400)
        self.assertIn("class Ledger", top["content"])
        self.assertIn("[window L", top["content"])
        start, end = (int(x) for x in top["lines"].split("-"))
        self.assertEqual(start, big["line_start"])
        self.assertLess(end, big["line_end"])

    def test_cooccurrence_off_by_default_and_opt_in(self):
        svc = self.svc()
        self.assertEqual(svc.indexer._cooccurrence, {})
        self.assertTrue(svc.indexer._cooccurrence_stats.get("disabled"))
        on = CodeIndex(str(self.root), cooccurrence=True)
        on.build_index()
        self.assertFalse(on._cooccurrence_stats.get("disabled"))
        self.config({"search_cooccurrence_synonyms": True})
        via_cfg = CodeIndex(str(self.root))
        self.assertTrue(via_cfg._cooccurrence_wanted())

    def test_markdown_heading_spans_its_section(self):
        svc = self.svc()
        heads = {c["name"]: c for c in svc.indexer.chunks.values()
                 if c["doc_id"].replace("\\", "/") == "docs/guide.md"}
        setup = heads["h2: Setup"]
        self.assertIn("step two", setup["content"])
        self.assertNotIn("run it", setup["content"])
        self.assertIn("run it", heads["h2: Usage"]["content"])
        self.assertIn("step one", heads["h1: Guide"]["content"], "h1 spans the h2s beneath it")


if __name__ == "__main__":
    unittest.main()
