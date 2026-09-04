"""P5 UX for c3_search (docs/search-eval.md, plan phase P5).

Pins: per-action top_k caps (files/exact 50, the rest 10); zero-result hints
that name the other action and never echo the query; `exact` ordered
definitions-first (symbol table, then a declaring line, then source before
config, docs and tests); backend tags on code/semantic headers
(``[lexical]``, ``[lexical+dense]``, ``[symbol+lexical]``, ``, reranked``,
``[dense]``); the eval harness parsing tagged headers.
"""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from cli.tools import search as search_mod
from cli.tools.search import handle_search
from services import access_guard as ag
from services.bench import search_eval as se
from services.file_memory import FileMemoryStore
from services.indexer import CodeIndex


def _fin(_name, _args, resp, _summ="", **_kw):
    return resp


def _no_facts(*_a, **_kw):
    return ""


class _FakeDense:
    def __init__(self, ranking, ready=True):
        self.ranking = list(ranking)
        self._ready = ready

    @property
    def ready(self):
        return self._ready

    def candidates(self, query, limit=40):
        return [(cid, 0.9 - 0.01 * i) for i, cid in enumerate(self.ranking[:limit])]


class _FakeReranker:
    name = "fake"
    ready = True

    def __init__(self, preferred):
        self.preferred = preferred

    def rerank(self, query, docs):
        ids = [cid for cid, _ in docs]
        ordered = [c for c in self.preferred if c in ids] + [c for c in ids if c not in self.preferred]
        return [(cid, 1.0 - 0.01 * i) for i, cid in enumerate(ordered)]


class _Project(unittest.TestCase):
    """Manifest order puts app/ and docs/ before src/, so without ordering an
    exact hit on the definition prints behind a config and a doc."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self._home = mock.patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.write("src/ledger/oauth2.py",
                   "class OAuth2Client:\n    def exchange_code(self, code):\n        return code\n")
        self.write("src/ledger/session.py",
                   "class SessionStore:\n    def expire(self):\n        return 0\n")
        self.write("src/ledger/invoice.py",
                   "def compute_total(self):\n    return apply_vat(self.subtotal())\n")
        self.write("docs/auth.md",
                   "# Auth\n\nOAuth2Client exchanges the code. Tokens rotate on refresh.\n")
        self.write("tests/test_oauth2.py",
                   "from src.ledger.oauth2 import OAuth2Client\n\ndef test_client():\n    assert OAuth2Client\n")
        self.write("app/config.yml", "client: OAuth2Client\n")
        for i in range(12):
            self.write(f"cfg/f{i:02d}.yml", f"k: {i}\n")

    def tearDown(self):
        self._home.stop()
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def config(self, cfg):
        (self.root / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def svc(self, embedding_index=None):
        indexer = CodeIndex(str(self.root))
        indexer.build_index()
        return types.SimpleNamespace(project_path=str(self.root), indexer=indexer,
                                     file_memory=FileMemoryStore(str(self.root)),
                                     embedding_index=embedding_index, hybrid_config={},
                                     compressor=None, convo_store=None)

    @staticmethod
    def cid(idx, suffix):
        return next(c for c in idx.chunks if c.replace("\\", "/").endswith(suffix))

    @staticmethod
    def headers(out):
        # Chunk headers carry the OS separator; compare in POSIX.
        return [line.replace("\\", "/") for line in out.splitlines() if line.startswith("--- ")]


class TestTopKCaps(_Project):
    def test_code_stays_capped_at_ten(self):
        seen = {}

        def spy(query, top_k, *a, **kw):
            seen["top_k"] = top_k
            return ""

        with mock.patch.object(search_mod, "_code_search", side_effect=spy):
            handle_search("x", "code", 40, 1200, self.svc(), _fin, _no_facts)
        self.assertEqual(seen["top_k"], search_mod._TOP_K_CAP_DEFAULT)

    def test_files_and_exact_take_up_to_fifty(self):
        seen = {}

        def spy_files(query, top_k, *a, **kw):
            seen["files"] = top_k
            return ""

        def spy_exact(query, top_k, *a, **kw):
            seen["exact"] = top_k
            return ""

        svc = self.svc()
        with mock.patch.object(search_mod, "_files_search", side_effect=spy_files), \
                mock.patch.object(search_mod, "_exact_search", side_effect=spy_exact):
            handle_search("x", "files", 40, 1200, svc, _fin, _no_facts)
            handle_search("x", "exact", 80, 1200, svc, _fin, _no_facts)
        self.assertEqual(seen, {"files": 40, "exact": 50})

    def test_files_glob_returns_more_than_ten_rows(self):
        out = handle_search("cfg/*.yml", "files", 40, 1200, self.svc(), _fin, _no_facts)
        rows = [line.replace("\\", "/") for line in out.splitlines()]
        self.assertEqual(sum(1 for line in rows if line.startswith("- cfg/")), 12)


class TestZeroResultHints(_Project):
    def _tail(self, out):
        return out.split("] 0 results", 1)[-1]

    def test_code_names_exact_and_files(self):
        out = handle_search("zzqx_nothing", "code", 3, 1200, self.svc(), _fin, _no_facts)
        self.assertIn("0 results", out)
        self.assertIn("action='exact'", out)
        self.assertIn("action='files'", out)
        self.assertNotIn("zzqx_nothing", self._tail(out))

    def test_code_pathlike_query_points_at_files(self):
        out = handle_search("zzqx/nothing.qq", "code", 3, 1200, self.svc(), _fin, _no_facts)
        self.assertIn("0 results", out)
        self.assertIn("action='files'", out)
        self.assertNotIn("nothing.qq", self._tail(out))

    def test_code_regex_query_points_at_exact(self):
        out = handle_search(r"zzqx_\d+", "code", 3, 1200, self.svc(), _fin, _no_facts)
        self.assertIn("action='exact'", out)

    def test_exact_names_ignore_case_and_code(self):
        svc = self.svc()
        out = handle_search("zzqx_nothing", "exact", 3, 1200, svc, _fin, _no_facts)
        self.assertRegex(out, r"0 results in \d+ files; try ignore_case=True")
        self.assertIn("action='code'", out)
        self.assertNotIn("zzqx_nothing", self._tail(out))
        again = handle_search("zzqx_nothing", "exact", 3, 1200, svc, _fin, _no_facts, ignore_case=True)
        self.assertNotIn("ignore_case", again)
        self.assertIn("action='code'", again)

    def test_files_names_exact(self):
        out = handle_search("zzqx_nothing", "files", 3, 1200, self.svc(), _fin, _no_facts)
        self.assertIn("0 results", out)
        self.assertIn("action='exact'", out)
        self.assertNotIn("zzqx_nothing", self._tail(out))

    def test_active_filter_is_named(self):
        out = handle_search("zzqx_nothing", "code", 3, 1200, self.svc(), _fin, _no_facts, lang="go")
        self.assertIn("filter", out)
        plain = handle_search("zzqx_nothing", "code", 3, 1200, self.svc(), _fin, _no_facts)
        self.assertNotIn("filter", plain)


class TestExactDefinitionsFirst(_Project):
    def test_symbol_definition_outranks_config_doc_and_test(self):
        out = handle_search("OAuth2Client", "exact", 10, 2400, self.svc(), _fin, _no_facts)
        heads = self.headers(out)
        self.assertEqual(heads[0], "--- src/ledger/oauth2.py --- [definition]")
        order = [h.split(" ---")[0][4:] for h in heads]
        self.assertEqual(order, ["src/ledger/oauth2.py", "app/config.yml",
                                 "docs/auth.md", "tests/test_oauth2.py"])
        self.assertEqual(sum(h.endswith("[definition]") for h in heads), 1)

    def test_declaring_line_counts_for_a_regex_query(self):
        out = handle_search(r"def exchange_code", "exact", 10, 2400, self.svc(), _fin, _no_facts)
        self.assertEqual(self.headers(out)[0], "--- src/ledger/oauth2.py --- [definition]")

    def test_method_tail_in_symbol_table(self):
        out = handle_search("expire", "exact", 10, 2400, self.svc(), _fin, _no_facts)
        self.assertEqual(self.headers(out)[0], "--- src/ledger/session.py --- [definition]")

    def test_top_k_cut_keeps_the_definition(self):
        out = handle_search("OAuth2Client", "exact", 1, 2400, self.svc(), _fin, _no_facts)
        self.assertEqual([h.split(" ---")[0][4:] for h in self.headers(out)], ["src/ledger/oauth2.py"])

    def test_def_line_regex_shapes(self):
        yes = ["def foo(", "class Foo:", "export default function foo(", "pub fn foo(",
               "func (l *Limiter) Allow(", "type Foo struct {", "impl<T> Trait for X {",
               "const RATE = 1", "#define MAX 3", "_CAP = 2400", "x: int = 1"]
        no = ["type: object", "    return foo(x)", "  x = 1", "if a == b:",
              "client: OAuth2Client", "from x import y"]
        for line in yes:
            self.assertTrue(search_mod._DEF_LINE_RE.match(line), line)
        for line in no:
            self.assertFalse(search_mod._DEF_LINE_RE.match(line), line)

    def test_harness_parses_tagged_exact_header(self):
        hits = se.parse_hits("exact", "--- src/x.py --- [definition]\n>L1: x\n--- src/y.py ---\n>L2: y")
        self.assertEqual([h.file for h in hits], ["src/x.py", "src/y.py"])


class TestBackendTags(_Project):
    def test_symbol_and_lexical(self):
        out = handle_search("compute_total", "code", 3, 1200, self.svc(), _fin, _no_facts)
        self.assertTrue(self.headers(out)[0].endswith(" (function) [symbol+lexical]"), out)

    def test_lexical_only(self):
        out = handle_search("rotate tokens on refresh", "code", 3, 1200, self.svc(), _fin, _no_facts)
        self.assertTrue(all(h.endswith(" [lexical]") for h in self.headers(out)), out)

    def test_fused_lists_are_named(self):
        svc = self.svc()
        idx = svc.indexer
        idx.dense = _FakeDense([self.cid(idx, "src/ledger/session.py::SessionStore.expire"),
                                self.cid(idx, "src/ledger/oauth2.py::OAuth2Client.exchange_code")])
        out = handle_search("exchange code", "code", 5, 2400, svc, _fin, _no_facts)
        tags = [(h.split(":L")[0][4:], h.rsplit(" [", 1)[-1].rstrip("]")) for h in self.headers(out)]
        # Dense-only: lexically nothing in session.py matches "exchange code".
        self.assertIn(("src/ledger/session.py", "dense"), tags)
        # Both lists name the method chunk; the class chunk is lexical-only.
        self.assertIn(("src/ledger/oauth2.py", "lexical+dense"), tags)
        self.assertIn(("src/ledger/oauth2.py", "lexical"), tags)

    def test_reranked_block_is_marked(self):
        self.config({"search_rerank": "auto"})
        svc = self.svc()
        idx = svc.indexer
        idx.reranker = _FakeReranker([self.cid(idx, "docs/auth.md::h1: Auth")])
        out = handle_search("how does the client exchange the code", "code", 5, 2400, svc, _fin, _no_facts)
        heads = self.headers(out)
        self.assertTrue(heads and heads[0].startswith("--- docs/auth.md:L"), out)
        self.assertTrue(heads[0].endswith(", reranked]"), out)

    def test_semantic_action_is_dense(self):
        ei = types.SimpleNamespace(ready=True, search=lambda q, top_k, max_tokens: [
            {"file": "src/ledger/session.py", "lines": "1-3", "name": "SessionStore",
             "type": "class", "tokens": 8, "content": "class SessionStore:"}])
        out = handle_search("session expiry", "semantic", 3, 1200, self.svc(embedding_index=ei),
                            _fin, _no_facts)
        self.assertEqual(self.headers(out)[0], "--- src/ledger/session.py:L1-3 SessionStore (class) [dense]")

    def test_harness_parses_tagged_chunk_header(self):
        hits = se.parse_hits("code", "--- src/x.py:L1-3 Foo (class) [symbol+lexical, reranked]\nbody\n"
                                     "--- src/y.py:L4-9 (block) [dense]\nbody")
        self.assertEqual([(h.file, h.name, h.type) for h in hits],
                         [("src/x.py", "Foo", "class"), ("src/y.py", "", "block")])


if __name__ == "__main__":
    unittest.main()
