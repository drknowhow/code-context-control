"""P3 hybrid fusion for c3_search (docs/search-eval.md, plan phase P3).

Pins: Reciprocal Rank Fusion; the dense backend fused into CodeIndex.search
through the retrieval contract; exact-symbol override surviving fusion;
filters and stale ids applied to dense candidates; fusion=False and the
`lexical` action; `search_fusion: off`; nomic task prefixes and the v2
collection with best-effort legacy cleanup that cannot hang.
"""

import json
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from cli.tools.search import handle_search
from services import access_guard as ag
from services import embedding_index as ei_mod
from services.embedding_index import EmbeddingIndex
from services.file_memory import FileMemoryStore
from services.indexer import CodeIndex
from services.retrieval import RetrievalBackend, rrf


def _fin(_name, _args, resp, _summ="", **_kw):
    return resp


def _no_facts(*_a, **_kw):
    return ""


class TestRRF(unittest.TestCase):
    def test_shared_ids_rise_and_order_is_deterministic(self):
        scores = rrf([["a", "b", "c"], ["c", "d"]], k=60)
        self.assertGreater(scores["c"], scores["a"], "present in both lists beats first in one")
        self.assertGreater(scores["a"], scores["b"])
        self.assertAlmostEqual(scores["a"], 1 / 61)
        self.assertAlmostEqual(scores["c"], 1 / 63 + 1 / 61)
        self.assertNotIn("e", scores)

    def test_duplicates_within_a_list_count_once(self):
        self.assertAlmostEqual(rrf([["a", "a", "b"]])["a"], 1 / 61)
        self.assertAlmostEqual(rrf([["a", "a", "b"]])["b"], 1 / 62)

    def test_k_floor(self):
        self.assertAlmostEqual(rrf([["a"]], k=0)["a"], 1 / 2)


class _FakeDense:
    """A RetrievalBackend that answers from a fixed ranking."""

    def __init__(self, ranking, ready=True):
        self.ranking = list(ranking)
        self._ready = ready
        self.calls = []

    @property
    def ready(self):
        return self._ready

    def candidates(self, query, limit=40):
        self.calls.append((query, limit))
        return [(cid, 0.9 - 0.01 * i) for i, cid in enumerate(self.ranking[:limit])]


class _Project(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self._home = mock.patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.write("src/auth.py", "def exchange_code(code):\n    return code\n")
        self.write("src/session.py", "class SessionStore:\n    def expire(self):\n        return 0\n")
        self.write("src/retry.py", "def retry_with_backoff(fn):\n    return fn()\n")
        self.write("tests/test_auth.py", "from src.auth import exchange_code\n\ndef test_exchange_code():\n    assert exchange_code\n")
        self.write("docs/auth.md", "# Auth\n\nTokens rotate on refresh.\n")

    def tearDown(self):
        self._home.stop()
        self._tmp.cleanup()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def config(self, cfg):
        (self.root / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")

    def build(self):
        idx = CodeIndex(str(self.root))
        idx.build_index()
        return idx

    @staticmethod
    def files(hits):
        return [h["file"].replace("\\", "/") for h in hits]

    @staticmethod
    def cid(idx, suffix):
        return next(c for c in idx.chunks if c.replace("\\", "/").endswith(suffix))


class TestFusion(_Project):
    def test_backend_contract(self):
        self.assertIsInstance(_FakeDense([]), RetrievalBackend)

    def test_dense_candidates_are_fused_by_rank(self):
        idx = self.build()
        session = self.cid(idx, "src/session.py::SessionStore.expire")
        retry = self.cid(idx, "src/retry.py::retry_with_backoff")
        # Lexically "rotate tokens on refresh" hits docs/auth.md only; the
        # dense backend says session.expire and retry are relevant too.
        idx.dense = _FakeDense([session, retry])
        self.assertEqual(idx.fusion, "rrf")
        hits = idx.search("rotate tokens on refresh", top_k=5, max_tokens=2000)
        files = self.files(hits)
        self.assertIn("src/session.py", files)
        self.assertIn("docs/auth.md", files)
        self.assertTrue(idx.dense.calls and idx.dense.calls[0][0] == "rotate tokens on refresh")

    def test_shared_candidate_outranks_single_list_leaders(self):
        idx = self.build()
        auth = self.cid(idx, "src/auth.py::exchange_code")
        test = self.cid(idx, "tests/test_auth.py::test_exchange_code")
        # Lexical puts the test first (mentions the token more); dense puts
        # the definition first and the test nowhere -> definition wins.
        idx.dense = _FakeDense([auth])
        hits = idx.search("exchange code", top_k=3, max_tokens=2000)
        self.assertEqual(self.files(hits)[0], "src/auth.py")
        self.assertIn(test, {h["chunk_id"] for h in hits})

    def test_exact_symbol_override_survives_fusion(self):
        idx = self.build()
        session = self.cid(idx, "src/session.py::SessionStore.expire")
        idx.dense = _FakeDense([session] * 1)
        hits = idx.search("exchange_code", top_k=3, max_tokens=2000)
        self.assertEqual(self.files(hits)[0], "src/auth.py")
        self.assertTrue(hits[0].get("exact_symbol"))

    def test_stale_and_filtered_dense_ids_are_dropped(self):
        idx = self.build()
        test = self.cid(idx, "tests/test_auth.py::test_exchange_code")
        idx.dense = _FakeDense(["gone::stale", test])
        hits = idx.search("exchange code", top_k=5, max_tokens=2000, kind="source")
        self.assertNotIn("tests/test_auth.py", self.files(hits))
        self.assertNotIn("gone::stale", {h["chunk_id"] for h in hits})

    def test_fusion_false_and_config_off_and_not_ready(self):
        idx = self.build()
        session = self.cid(idx, "src/session.py::SessionStore.expire")
        idx.dense = _FakeDense([session])
        plain = idx.search("rotate tokens on refresh", top_k=5, max_tokens=2000, fusion=False)
        self.assertNotIn("src/session.py", self.files(plain))
        idx.dense = _FakeDense([session], ready=False)
        self.assertEqual(idx.fusion, "off")
        self.assertNotIn("src/session.py", self.files(idx.search("rotate tokens on refresh", top_k=5, max_tokens=2000)))
        self.config({"search_fusion": "off"})
        off = CodeIndex(str(self.root))
        off._load_index()
        off.dense = _FakeDense([session])
        self.assertEqual(off.fusion, "off")
        self.assertNotIn("src/session.py", self.files(off.search("rotate tokens on refresh", top_k=5, max_tokens=2000)))

    def test_backend_failure_leaves_lexical_ranking(self):
        idx = self.build()

        class Broken(_FakeDense):
            def candidates(self, query, limit=40):
                raise RuntimeError("ollama down")

        idx.dense = Broken([])
        hits = idx.search("exchange code", top_k=3, max_tokens=2000)
        self.assertTrue(hits)

    def test_lexical_action_never_fuses(self):
        idx = self.build()
        session = self.cid(idx, "src/session.py::SessionStore.expire")
        idx.dense = _FakeDense([session])
        svc = types.SimpleNamespace(project_path=str(self.root), indexer=idx,
                                    file_memory=FileMemoryStore(str(self.root)), embedding_index=None,
                                    hybrid_config={}, compressor=None, convo_store=None)
        fused = handle_search("rotate tokens on refresh", "code", 5, 2000, svc, _fin, _no_facts)
        lexical = handle_search("rotate tokens on refresh", "lexical", 5, 2000, svc, _fin, _no_facts)
        self.assertIn("session.py", fused)
        self.assertNotIn("session.py", lexical)


class _FakeOllama:
    def __init__(self):
        self.embedded = []
        self.queries = []

    def is_available(self, timeout=None):
        return True

    def has_model(self, model):
        return True

    def embed(self, text, model="nomic-embed-text"):
        self.queries.append(text)
        return [1.0, 0.0]

    def embed_batch(self, texts, model="nomic-embed-text"):
        self.embedded.extend(texts)
        return [[1.0, 0.0] for _ in texts]


class _FakeCollection:
    def __init__(self):
        self.rows = {}

    def count(self):
        return len(self.rows)

    def upsert(self, ids, embeddings, documents, metadatas):
        for i, cid in enumerate(ids):
            self.rows[cid] = (documents[i], metadatas[i])

    def get(self, where=None, include=None, **kw):
        doc_id = (where or {}).get("doc_id")
        return {"ids": [c for c, (_d, m) in self.rows.items() if m.get("doc_id") == doc_id]}

    def delete(self, ids=None, **kw):
        for cid in ids or []:
            self.rows.pop(cid, None)

    def query(self, query_embeddings, n_results, include):
        ids = list(self.rows)[:n_results]
        return {"ids": [ids], "distances": [[0.1 * (i + 1) for i in range(len(ids))]],
                "documents": [[self.rows[c][0] for c in ids]], "metadatas": [[self.rows[c][1] for c in ids]]}


def _embedding_index(tmp, model="nomic-embed-text"):
    ollama = _FakeOllama()
    idx = EmbeddingIndex(str(tmp), ollama, embed_model=model)
    collection = _FakeCollection()

    def fake_init():
        idx._collection = collection
        idx._available = True
        idx._ollama_up = idx._model_ok = idx._ollama_ok = True

    idx._init_backends = fake_init
    return idx, ollama, collection


class TestTaskPrefixesAndCandidates(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / ".c3").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _code_index(self):
        return types.SimpleNamespace(chunks={
            "a.py::fa": {"doc_id": "a.py", "name": "fa", "type": "function",
                         "content": "def fa():\n    return 'twenty characters plus'"},
        })

    def test_nomic_gets_document_and_query_prefixes(self):
        idx, ollama, collection = _embedding_index(self.tmp)
        idx.build(self._code_index())
        self.assertTrue(ollama.embedded and ollama.embedded[0].startswith("search_document: File: a.py"))
        hits = idx.search("fa", top_k=1)
        self.assertEqual(ollama.queries[-1], "search_query: fa")
        self.assertEqual(hits[0]["content"].splitlines()[0], "def fa():", "prefix line stripped from content")
        cands = idx.candidates("fa", limit=5)
        self.assertEqual(cands[0][0], "a.py::fa")
        self.assertAlmostEqual(cands[0][1], 0.9)
        self.assertTrue((self.tmp / ".c3" / "embeddings" / "file_hashes_v2.json").exists())

    def test_admission_floor_drops_neighbours_that_are_not_answers(self):
        """A dense index always has nearest neighbours; below the floor they
        are not candidates, so an unanswerable query stays unanswered."""
        idx, _, collection = _embedding_index(self.tmp)
        idx.build(self._code_index())
        self.assertEqual(idx.min_score, ei_mod.DEFAULT_MIN_SCORE_NOMIC)
        collection.rows["b.py::gb"] = ("File: b.py\ndef gb(): pass", {"doc_id": "b.py", "name": "gb",
                                                                      "type": "function", "line_start": 1, "line_end": 1})
        # Fake query distances are 0.1, 0.2 -> similarities 0.9, 0.8: both admitted.
        self.assertEqual(len(idx.candidates("x", limit=5)), 2)
        idx._min_score_override = 0.85
        self.assertEqual([c for c, _ in idx.candidates("x", limit=5)], ["a.py::fa"])
        self.assertEqual([h["file"] for h in idx.search("x", top_k=5)], ["a.py"])
        idx._min_score_override = 0.95
        self.assertEqual(idx.candidates("x", limit=5), [])
        self.assertEqual(idx.search("x", top_k=5), [])

    def test_floor_defaults_by_model_and_config_override(self):
        nomic, _, _ = _embedding_index(self.tmp)
        self.assertEqual(nomic.min_score, 0.62)
        other, _, _ = _embedding_index(self.tmp, model="mxbai-embed-large")
        self.assertEqual(other.min_score, 0.55)
        custom = EmbeddingIndex(str(self.tmp), _FakeOllama(), min_score=0.7)
        self.assertEqual(custom.min_score, 0.7)

    def test_other_models_get_no_prefix(self):
        idx, ollama, _ = _embedding_index(self.tmp, model="mxbai-embed-large")
        idx.build(self._code_index())
        self.assertTrue(ollama.embedded[0].startswith("File: a.py"))
        idx.search("fa", top_k=1)
        self.assertEqual(ollama.queries[-1], "fa")

    def test_collection_name_is_v2(self):
        self.assertEqual(ei_mod.COLLECTION_NAME, "code_embeddings_v2")
        self.assertNotEqual(ei_mod.COLLECTION_NAME, ei_mod.LEGACY_COLLECTION_NAME)

    def test_legacy_drop_is_bounded_even_when_the_client_hangs(self):
        idx, _, _ = _embedding_index(self.tmp)
        (self.tmp / ".c3" / "embeddings" / "file_hashes.json").write_text("{}", encoding="utf-8")
        entered = threading.Event()

        class HangingClient:
            def list_collections(self):
                return [ei_mod.LEGACY_COLLECTION_NAME, ei_mod.COLLECTION_NAME]

            def delete_collection(self, name):
                entered.set()
                threading.Event().wait()  # never returns

        idx._chroma_client = HangingClient()
        done = threading.Event()

        def run():
            idx._drop_legacy_collection()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(10), "_drop_legacy_collection must return despite a hung delete")
        self.assertTrue(entered.is_set())
        self.assertFalse((self.tmp / ".c3" / "embeddings" / "file_hashes.json").exists(),
                         "the v1 hash file goes even when the collection drop hangs")

    def test_legacy_drop_noop_without_legacy_collection(self):
        idx, _, _ = _embedding_index(self.tmp)
        calls = []

        class Client:
            def list_collections(self):
                return [types.SimpleNamespace(name=ei_mod.COLLECTION_NAME)]

            def delete_collection(self, name):
                calls.append(name)

        idx._chroma_client = Client()
        idx._drop_legacy_collection()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
