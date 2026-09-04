"""P4 optional reranker for c3_search (docs/search-eval.md, plan phase P4).

Pins: the natural-language gate, the reranker contract, reordering of the
top block with a fake reranker, the exact-symbol override ahead of it,
identifier queries bypassing it, failure and empty answers leaving the
fused order, off-by-default, the harness measurement flag, and the FlashRank
adapter's availability/laziness (a real model pass only when installed).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services import access_guard as ag
from services import reranker as rr_mod
from services.indexer import CodeIndex
from services.reranker import FlashRankReranker, Reranker, is_natural_language, passage_text


class TestGate(unittest.TestCase):
    def test_natural_language_detection(self):
        from services.lexical_index import tokenize_code as tok

        def nl(q):
            return is_natural_language(q, tok(q, dedupe=True))

        self.assertTrue(nl("how do I configure the oauth2 redirect uri"))
        self.assertTrue(nl("rotate refresh tokens on reuse"))
        self.assertFalse(nl("compute_total"))
        self.assertFalse(nl("OAuth2Client"))
        self.assertFalse(nl("sha256 digest"), "two tokens is a lookup, not a question")
        self.assertFalse(nl("migrate_v2 sqlite_store v3"), "identifier soup has no plain words")

    def test_passage_text(self):
        text = passage_text({"doc_id": "src/a.py", "name": "A.b", "content": "x" * 5000})
        self.assertTrue(text.startswith("src/a.py: A.b\n"))
        self.assertLessEqual(len(text), 1500 + 20)


class _FakeReranker:
    name = "fake"

    def __init__(self, preferred, ready=True, fail=False, empty=False):
        self.preferred = preferred
        self._ready = ready
        self.fail = fail
        self.empty = empty
        self.calls = []

    @property
    def ready(self):
        return self._ready

    def rerank(self, query, docs):
        self.calls.append((query, [cid for cid, _ in docs]))
        if self.fail:
            raise RuntimeError("model missing")
        if self.empty:
            return []
        ids = [cid for cid, _ in docs]
        ordered = [c for c in self.preferred if c in ids] + [c for c in ids if c not in self.preferred]
        return [(cid, 1.0 - 0.01 * i) for i, cid in enumerate(ordered)]


class _Project(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".c3").mkdir()
        self._home = mock.patch.object(ag, "_global_base", return_value=None)
        self._home.start()
        self.write("src/invoice.py", "def compute_total(self):\n    return apply_vat(self.subtotal())\n")
        self.write("src/tax.py", "def apply_vat(amount, country):\n    return amount * rate\n")
        self.write("docs/billing.md", "# Billing\n\nInvoice totals include VAT for the customer's country.\n")
        self.write("tests/test_invoice.py", "def test_compute_total_applies_vat():\n    assert compute_total\n")

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


class TestRerankInSearch(_Project):
    def test_off_by_default_even_when_attached(self):
        idx = self.build()
        idx.reranker = _FakeReranker([])
        self.assertEqual(idx.rerank, "off")
        idx.search("how does the invoice total include vat", top_k=3, max_tokens=2000)
        self.assertEqual(idx.reranker.calls, [])

    def test_natural_language_query_is_reranked(self):
        self.config({"search_rerank": "auto"})
        idx = self.build()
        doc = self.cid(idx, "docs/billing.md::h1: Billing")
        idx.reranker = _FakeReranker([doc])
        self.assertEqual(idx.rerank, "fake")
        self.assertIsInstance(idx.reranker, Reranker)
        hits = idx.search("how does the invoice total include vat", top_k=3, max_tokens=2000)
        self.assertEqual(self.files(hits)[0], "docs/billing.md")
        self.assertEqual(len(idx.reranker.calls), 1)
        self.assertIn(doc, idx.reranker.calls[0][1])

    def test_identifier_query_bypasses_the_reranker(self):
        self.config({"search_rerank": "auto"})
        idx = self.build()
        idx.reranker = _FakeReranker([self.cid(idx, "docs/billing.md::h1: Billing")])
        hits = idx.search("compute_total", top_k=3, max_tokens=2000)
        self.assertEqual(self.files(hits)[0], "src/invoice.py")
        self.assertEqual(idx.reranker.calls, [])

    def test_exact_symbol_stays_ahead_of_the_reranked_block(self):
        self.config({"search_rerank": "auto"})
        idx = self.build()
        doc = self.cid(idx, "docs/billing.md::h1: Billing")
        idx.reranker = _FakeReranker([doc])
        # "apply_vat" is an exact symbol; the rest of the query is prose.
        hits = idx.search("apply_vat", top_k=3, max_tokens=2000)
        self.assertTrue(hits[0].get("exact_symbol"))

    def test_failure_and_empty_answers_keep_fused_order(self):
        self.config({"search_rerank": "auto"})
        idx = self.build()
        baseline = self.files(idx.search("how does the invoice total include vat", top_k=3, max_tokens=2000))
        for rr in (_FakeReranker([], fail=True), _FakeReranker([], empty=True), _FakeReranker([], ready=False)):
            idx.reranker = rr
            idx._search_cache.clear()
            self.assertEqual(self.files(idx.search("how does the invoice total include vat", top_k=3, max_tokens=2000)),
                             baseline)


class TestFlashRankAdapter(unittest.TestCase):
    def test_unavailable_without_the_package(self):
        with mock.patch.dict(sys.modules, {"flashrank": None}):
            rr = FlashRankReranker()
            self.assertFalse(rr.available)
            self.assertFalse(rr.ready)
            self.assertEqual(rr.rerank("q", [("a", "x"), ("b", "y")]), [])

    def test_construction_never_loads_the_model(self):
        rr = FlashRankReranker(cache_dir=tempfile.mkdtemp())
        self.assertFalse(rr.loaded)

    def test_load_failure_marks_not_ready_once(self):
        rr = FlashRankReranker(cache_dir=tempfile.mkdtemp())
        rr.available = True
        with mock.patch.dict(sys.modules, {"flashrank": None}):
            self.assertEqual(rr.rerank("q", [("a", "x")]), [])
        self.assertFalse(rr.ready)

    @unittest.skipUnless(rr_mod.flashrank_available(), "flashrank not installed")
    def test_real_model_prefers_the_relevant_passage(self):
        rr = FlashRankReranker()  # default cache dir: the model is downloaded once per machine
        out = rr.rerank("how are session cookies expired after inactivity", [
            ("limiter", "internal/ratelimit/limiter.go: Allow\nfunc (l *Limiter) Allow() bool { token bucket }"),
            ("session", "src/auth/session.py: SessionStore.expire\ndef expire(self, now=None):\n"
                        "    '''Drop sessions idle longer than ttl_seconds.'''"),
        ])
        self.assertEqual(out[0][0], "session")
        self.assertTrue(rr.loaded)


class TestHarnessFlag(unittest.TestCase):
    def test_rerank_on_attaches_the_adapter(self):
        from services.bench import search_eval as se
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".c3").mkdir()
            (root / "a.py").write_text("def alpha_beta():\n    return 1\n", encoding="utf-8")
            with mock.patch.object(ag, "_global_base", return_value=None):
                rt = se.build_eval_runtime(root, semantic="off", rerank="on")
            self.assertIsNotNone(rt.svc.indexer.reranker)
            expected = "flashrank" if rr_mod.flashrank_available() else "off"
            self.assertEqual(rt.stats.rerank, expected)


if __name__ == "__main__":
    unittest.main()
