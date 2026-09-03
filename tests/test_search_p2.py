"""P2 lexical engine for c3_search (docs/search-eval.md, plan phase P2).

Pins: the alias tokenizer (digits kept, no stemming), doc/lang classification,
path/lang/kind filters, SQLite FTS5 candidate retrieval with its TF-IDF
fallback, configured synonyms replacing the hardcoded map, intent priors, and
the filters reaching every c3_search action.
"""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from cli.tools.search import handle_search
from services import access_guard as ag
from services import lexical_index as lx
from services.file_memory import FileMemoryStore
from services.indexer import CodeIndex


def _fin(_name, _args, resp, _summ="", **_kw):
    return resp


def _no_facts(*_a, **_kw):
    return ""


class TestTokenizer(unittest.TestCase):
    def test_identifiers_verbatim_and_split_digits_kept(self):
        toks = lx.tokenize_code("def parseIso8601(value): sha256_digest OAuth2Client v2 S256 utf8_decode l")
        for expected in ("parseiso8601", "parse", "iso8601", "sha256_digest", "sha256", "digest",
                         "oauth2client", "v2", "s256", "utf8_decode", "utf8", "decode"):
            self.assertIn(expected, toks, expected)
        self.assertNotIn("l", toks, "single letters are noise")
        self.assertNotIn("sha", toks, "digits are part of the token, never stripped")

    def test_no_stemming(self):
        toks = set(lx.tokenize_code("embedding embeddings embed"))
        self.assertEqual(toks, {"embedding", "embeddings", "embed"})

    def test_dedupe_for_queries_keeps_first_occurrence(self):
        self.assertEqual(lx.tokenize_code("sha256_digest sha256_digest", dedupe=True),
                         ["sha256_digest", "sha256", "digest"])

    def test_split_identifier(self):
        self.assertEqual(lx.split_identifier("HTMLParser"), ["html", "parser"])
        self.assertEqual(lx.split_identifier("migrate_v2"), ["migrate", "v2"])


class TestClassification(unittest.TestCase):
    def test_doc_kind(self):
        cases = {
            "tests/test_oauth2.py": "test", "internal/ratelimit/limiter_test.go": "test",
            "web/src/InvoiceTable.test.tsx": "test", "src/pkg/auth.py": "source",
            "docs/deployment.md": "doc", "README.md": "doc", "configs/docker-compose.yml": "config",
            ".github/workflows/ci.yml": "config", "web/src/hooks/useAuth.ts": "source",
        }
        for rel, kind in cases.items():
            self.assertEqual(lx.doc_kind(rel), kind, rel)

    def test_lang_of(self):
        self.assertEqual(lx.lang_of("a/b.py"), "python")
        self.assertEqual(lx.lang_of("a/b.tsx"), "typescript")
        self.assertEqual(lx.lang_of("x.go"), "go")
        self.assertEqual(lx.lang_of("Makefile"), "")

    def test_filters(self):
        f = lx.Filters(path="src/**", lang="py,go", kind="test,function")
        self.assertTrue(f.path_ok("src/a/b.py"))
        self.assertFalse(f.path_ok("web/x.ts"))
        self.assertTrue(f.lang_ok("x.py") and f.lang_ok("x.go"))
        self.assertFalse(f.lang_ok("x.ts"))
        self.assertTrue(f.kind_ok("tests/test_a.py", "block"))
        self.assertTrue(f.kind_ok("src/a.py", "function"))
        self.assertFalse(f.kind_ok("src/a.py", "class"))
        self.assertFalse(lx.Filters())
        self.assertEqual(lx.Filters(path="a", lang="python").key(), (("a",), ("python",), ()))

    def test_intent_prior(self):
        self.assertEqual(lx.intent_prior(["test", "exchange", "code"], "test"), 0.15)
        self.assertEqual(lx.intent_prior(["how", "do", "i", "configure"], "doc"), 0.15)
        self.assertEqual(lx.intent_prior(["apply", "vat"], "doc"), 0.0)
        self.assertEqual(lx.intent_prior(["apply", "vat"], "test"), 0.0)


@unittest.skipUnless(lx.fts5_available(), "SQLite built without FTS5")
class TestLexicalIndex(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.idx = lx.LexicalIndex(Path(self._tmp.name))
        self.chunks = {
            "src/auth.py::sha256_digest": {"doc_id": "src/auth.py", "name": "sha256_digest", "type": "function",
                                            "content": "def sha256_digest(data):\n    return hashlib.sha256(data)"},
            "src/store.py::migrate_v2": {"doc_id": "src/store.py", "name": "Store.migrate_v2", "type": "method",
                                          "content": "def migrate_v2(self):\n    add tax_rate column"},
            "tests/test_auth.py::test_digest": {"doc_id": "tests/test_auth.py", "name": "test_digest",
                                                 "type": "function",
                                                 "content": "def test_digest():\n    assert sha256_digest(b'a')"},
            "docs/guide.md::h2": {"doc_id": "docs/guide.md", "name": "h2: Migrations", "type": "heading",
                                   "content": "## Migrations\nrun migrate before deploy"},
        }
        self.docs = {c["doc_id"]: {} for c in self.chunks.values()}

    def tearDown(self):
        self._tmp.cleanup()

    def test_rebuild_and_search(self):
        rows = self.idx.rebuild(self.chunks, self.docs)
        self.assertEqual(rows, 4)
        self.assertTrue(self.idx.ready())
        hits = self.idx.search(["sha256"])
        self.assertEqual(hits[0][0], "src/auth.py::sha256_digest", hits)
        self.assertGreater(hits[0][1], 0)
        v2 = self.idx.search(["v2"])
        self.assertEqual([h[0] for h in v2], ["src/store.py::migrate_v2"])

    def test_filters_in_sql_and_allowed_docs(self):
        self.idx.rebuild(self.chunks, self.docs)
        only_tests = self.idx.search(["sha256"], filters=lx.Filters(kind="test"))
        self.assertEqual([h[0] for h in only_tests], ["tests/test_auth.py::test_digest"])
        only_md = self.idx.search(["migrate"], filters=lx.Filters(lang="markdown"))
        self.assertEqual([h[0] for h in only_md], ["docs/guide.md::h2"])
        allowed = self.idx.search(["sha256"], allowed_docs=["src/auth.py"])
        self.assertEqual([h[0] for h in allowed], ["src/auth.py::sha256_digest"])
        self.assertEqual(self.idx.search(["sha256"], allowed_docs=[]), [])

    def test_rebuild_is_atomic_swap(self):
        self.idx.rebuild(self.chunks, self.docs)
        self.idx.rebuild({"only::one": self.chunks["src/auth.py::sha256_digest"]}, {"src/auth.py": {}})
        self.assertEqual(self.idx.row_count(), 1)
        self.assertFalse(self.idx.path.with_suffix(".sqlite.tmp").exists())


class _Project(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self._home = mock.patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.write("src/auth/tokens.py",
                   "def sha256_digest(data):\n    return data\n\n"
                   "def base64url_encode(data):\n    return data\n")
        self.write("src/auth/oauth2.py",
                   "class OAuth2Client:\n    def authorization_url(self):\n"
                   "        return 'code_challenge_method=S256'\n")
        self.write("src/storage/sqlite_store.py",
                   "class SqliteStore:\n    def migrate_v2(self):\n        '''Add the tax_rate column.'''\n"
                   "        return 2\n")
        self.write("src/api/routes.py", "def register_routes(app):\n    app.add_route('/healthz')\n")
        self.write("tests/test_oauth2.py",
                   "from src.auth.oauth2 import OAuth2Client\n\n"
                   "def test_exchange_code():\n    assert OAuth2Client\n")
        self.write("docs/deployment.md", "# Deployment\n\n## Migrations\n\nRun migrate before deploying a new version.\n")
        self.write("internal/limiter.go", "package ratelimit\n\nfunc (l *Limiter) Allow() bool {\n\treturn true\n}\n")

    def tearDown(self):
        self._home.stop()
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def config(self, cfg):
        (self.root / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def svc(self):
        indexer = CodeIndex(str(self.root))
        indexer.build_index()
        return types.SimpleNamespace(project_path=str(self.root), indexer=indexer,
                                     file_memory=FileMemoryStore(str(self.root)), embedding_index=None,
                                     hybrid_config={}, compressor=None, convo_store=None)

    @staticmethod
    def files(hits):
        return [h["file"].replace("\\", "/") for h in hits]


class TestCodeIndexLexical(_Project):
    def test_engine_is_fts5_when_available(self):
        svc = self.svc()
        expected = "fts5" if lx.fts5_available() else "tfidf"
        self.assertEqual(svc.indexer.lexical_engine, expected)
        self.assertEqual(svc.indexer.get_stats()["lexical_engine"], expected)
        fresh = CodeIndex(str(self.root))
        fresh._load_index()
        self.assertEqual(fresh.lexical_engine, expected, "readiness survives a reload")

    def test_older_index_schema_rebuilds_on_load(self):
        """An index.json written before the tokenizer change carries TF-IDF
        terms the new queries cannot hit; loading it must rebuild, not serve it."""
        svc = self.svc()
        index_file = svc.indexer.index_dir / "index.json"
        data = json.loads(index_file.read_text(encoding="utf-8"))
        data.pop("index_schema", None)
        data["chunk_tfidf"] = {cid: {"stale": 1.0} for cid in data["chunks"]}
        index_file.write_text(json.dumps(data), encoding="utf-8")
        svc.indexer._lexical.path.unlink(missing_ok=True)
        fresh = CodeIndex(str(self.root))
        self.assertTrue(fresh._load_index())
        self.assertEqual(json.loads(index_file.read_text(encoding="utf-8")).get("index_schema"), 2)
        self.assertEqual(self.files(fresh.search("S256", top_k=3, max_tokens=1200))[0], "src/auth/oauth2.py")

    def test_digit_tokens_find_their_definitions(self):
        svc = self.svc()
        for query, expect in (("v2 migration", "src/storage/sqlite_store.py"),
                              ("S256", "src/auth/oauth2.py"),
                              ("sha256 digest", "src/auth/tokens.py"),
                              ("base64url", "src/auth/tokens.py")):
            hits = svc.indexer.search(query, top_k=3, max_tokens=1200)
            self.assertTrue(hits, query)
            self.assertEqual(self.files(hits)[0], expect, query)

    def test_tfidf_fallback_finds_the_same_digit_tokens(self):
        self.config({"search_engine": "tfidf"})
        svc = self.svc()
        self.assertEqual(svc.indexer.lexical_engine, "tfidf")
        for query, expect in (("v2 migration", "src/storage/sqlite_store.py"), ("S256", "src/auth/oauth2.py")):
            hits = svc.indexer.search(query, top_k=3, max_tokens=1200)
            self.assertEqual(self.files(hits)[0], expect, query)

    def test_fts5_unavailable_means_tfidf(self):
        with mock.patch.object(lx, "fts5_available", return_value=False):
            idx = CodeIndex(str(self.root))
            idx.build_index()
            self.assertEqual(idx.lexical_engine, "tfidf")
            self.assertTrue(idx.search("register_routes", top_k=3, max_tokens=1200))

    def test_filters_narrow_candidates(self):
        svc = self.svc()
        tests_only = svc.indexer.search("OAuth2Client", top_k=5, max_tokens=1200, kind="test")
        self.assertEqual(self.files(tests_only), ["tests/test_oauth2.py"])
        go_only = svc.indexer.search("allow", top_k=5, max_tokens=1200, lang="go")
        self.assertEqual(self.files(go_only), ["internal/limiter.go"])
        docs_only = svc.indexer.search("migrate", top_k=5, max_tokens=1200, path="docs/**")
        self.assertTrue(docs_only)
        self.assertEqual(set(self.files(docs_only)), {"docs/deployment.md"})
        nothing = svc.indexer.search("migrate", top_k=5, max_tokens=1200, path="nope/**")
        self.assertEqual(nothing, [])

    def test_exact_symbol_respects_filters(self):
        svc = self.svc()
        hits = svc.indexer.search("OAuth2Client", top_k=5, max_tokens=1200, path="src/**")
        self.assertEqual(self.files(hits)[0], "src/auth/oauth2.py")
        self.assertTrue(hits[0].get("exact_symbol"))

    def test_intent_prior_prefers_tests_for_test_queries(self):
        svc = self.svc()
        hits = svc.indexer.search("test exchange code", top_k=3, max_tokens=1200)
        self.assertEqual(self.files(hits)[0], "tests/test_oauth2.py")
        hits = svc.indexer.search("how do I deploy a new version", top_k=3, max_tokens=1200)
        self.assertEqual(self.files(hits)[0], "docs/deployment.md")

    def test_synonyms_come_from_config_only(self):
        # "endpoint" used to expand to route/handler/api by a hardcoded map.
        svc = self.svc()
        self.assertEqual(svc.indexer.search("endpoint", top_k=3, max_tokens=1200), [])
        self.config({"search_synonyms": {"endpoint": ["route"]}})
        svc = self.svc()
        self.assertEqual(self.files(svc.indexer.search("endpoint", top_k=3, max_tokens=1200))[0],
                         "src/api/routes.py")


class TestFiltersThroughTheTool(_Project):
    def test_code_files_exact_semantic(self):
        svc = self.svc()
        code = handle_search("OAuth2Client", "code", 5, 1200, svc, _fin, _no_facts, kind="test")
        self.assertIn("tests/test_oauth2.py", code.replace("\\", "/"))
        self.assertNotIn("src/auth/oauth2.py", code.replace("\\", "/"))
        files = handle_search("oauth2", "files", 5, 1200, svc, _fin, _no_facts, path="src/**")
        self.assertIn("src/auth/oauth2.py", files.replace("\\", "/"))
        self.assertNotIn("tests/", files.replace("\\", "/"))
        exact = handle_search("OAuth2Client", "exact", 5, 1200, svc, _fin, _no_facts, lang="python", kind="source")
        self.assertIn("--- src/auth/oauth2.py ---", exact)
        self.assertNotIn("tests/test_oauth2.py", exact)
        svc.embedding_index = types.SimpleNamespace(ready=True, search=lambda *a, **k: [
            {"file": "tests/test_oauth2.py", "lines": "1-3", "name": "", "type": "block", "content": "x", "tokens": 1},
            {"file": "src/auth/oauth2.py", "lines": "1-3", "name": "OAuth2Client", "type": "class",
             "content": "y", "tokens": 1},
        ])
        sem = handle_search("client", "semantic", 5, 1200, svc, _fin, _no_facts, kind="source")
        self.assertIn("src/auth/oauth2.py", sem)
        self.assertNotIn("tests/test_oauth2.py", sem)


if __name__ == "__main__":
    unittest.main()
