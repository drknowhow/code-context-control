"""Lazy backend init for VectorStore / EmbeddingIndex.

Regression guard for the MCP startup-latency fix: constructing these stores must
NOT trigger the heavy chromadb/ollama init — that must happen on first *use*, off
the MCP handshake path. See services/runtime.py build_runtime + the _ensure_ready
methods on each store.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.embedding_index import EmbeddingIndex
from services.vector_store import VectorStore


class _StubOllama:
    def is_available(self, timeout=None):
        return False

    def has_model(self, model=None):
        return False

    def embed(self, text, model=None):
        return None


class TestLazyStoreInit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir()

    def test_vector_store_defers_init_until_first_use(self):
        vs = VectorStore(str(self.project), config={"disable_vector_backend": True})
        self.assertFalse(vs._initialized)        # not initialized on construct
        # status reporters must NOT trigger init
        vs.get_stats()
        self.assertFalse(vs.vector_enabled)
        self.assertFalse(vs._initialized)
        # a work method triggers init
        vs.search("anything")
        self.assertTrue(vs._initialized)

    def test_embedding_index_defers_init_until_first_use(self):
        ei = EmbeddingIndex(str(self.project), _StubOllama())
        calls = []
        ei._init_backends = lambda: calls.append("init")   # avoid real chromadb
        ei._load_hashes = lambda: calls.append("hashes")
        self.assertFalse(ei._initialized)
        # status reporters must NOT trigger init
        self.assertFalse(ei.ready)
        ei.get_stats()
        self.assertFalse(ei._initialized)
        self.assertEqual(calls, [])
        # a work method triggers exactly-once init
        ei.search("anything")
        self.assertTrue(ei._initialized)
        self.assertEqual(calls, ["init", "hashes"])
        ei.search("again")                                  # idempotent
        self.assertEqual(calls, ["init", "hashes"])

    def test_warm_initializes(self):
        vs = VectorStore(str(self.project), config={"disable_vector_backend": True})
        self.assertFalse(vs._initialized)
        vs.warm()
        self.assertTrue(vs._initialized)

    def test_vector_search_does_not_block_behind_warmup(self):
        vs = VectorStore(str(self.project), config={
            "disable_vector_backend": True,
            "backend_init_wait_seconds": 0.01,
        })
        vs._init_lock.acquire()
        try:
            started = time.monotonic()
            self.assertEqual(vs.search("anything"), [])
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(vs._initialized)
        finally:
            vs._init_lock.release()

    def test_embedding_search_does_not_block_behind_build(self):
        ei = EmbeddingIndex(str(self.project), _StubOllama())
        ei._init_lock.acquire()
        try:
            started = time.monotonic()
            with patch("services.embedding_index._SEARCH_INIT_WAIT_SECONDS", 0.01):
                self.assertEqual(ei.search("anything"), [])
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertFalse(ei._initialized)
        finally:
            ei._init_lock.release()


if __name__ == "__main__":
    unittest.main()
