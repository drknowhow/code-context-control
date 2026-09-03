"""The co-occurrence pass is quadratic per chunk and must stay bounded.

Measured origin: a 139-file project whose vendor directory holds a minified
CSS bundle produced 4091 chunks averaging 798 unique tokens. The unbounded
pass needed ~1.9e9 Counter updates and pinned one core for over fifteen
minutes on an index that is otherwise built in seconds.
"""
import tempfile
import unittest
from pathlib import Path

from services import indexer as indexer_mod
from services.indexer import CodeIndex


def _word(i: int) -> str:
    """Distinct lowercase word - the tokenizer keeps [a-zA-Z]{2,} only."""
    a, b = divmod(i, 26)
    c, a = divmod(a, 26)
    return "zz" + chr(97 + c) + chr(97 + a) + chr(97 + b)


def _minified_like(n_tokens: int) -> str:
    """A single line of n distinct tokens - what a minified bundle looks like."""
    return ".".join(_word(i) for i in range(n_tokens)) + "\n"


class TestCooccurrenceBounds(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _build(self):
        # Co-occurrence is off by default since 2.105.0; these tests are about
        # its bounds, so they opt in.
        idx = CodeIndex(str(self.root), str(self.root / ".c3" / "index"), cooccurrence=True)
        return idx, idx.build_index()

    def test_token_dense_chunks_are_skipped_and_reported(self):
        """A chunk past the cap contributes nothing - and says so."""
        big = indexer_mod._COOC_MAX_CHUNK_TOKENS + 50
        (self.root / "vendor.js").write_text(_minified_like(big), encoding="utf-8")
        (self.root / "small.py").write_text(
            "def alpha_helper():\n    return beta_helper\n", encoding="utf-8")

        idx, result = self._build()
        stats = result["cooccurrence"]

        self.assertGreaterEqual(stats["chunks_skipped"], 1)
        self.assertFalse(stats["budget_exhausted"])
        # The skipped chunk's tokens are absent from the synonym map...
        self.assertNotIn(_word(0), idx._cooccurrence)
        # ...while the ordinary file was still processed.
        self.assertLessEqual(
            stats["pair_updates"],
            indexer_mod._COOC_MAX_PAIR_UPDATES,
        )

    def test_pair_updates_never_exceed_the_budget(self):
        """Many at-the-cap chunks stop the pass instead of running away."""
        # Each file sits just under the per-chunk cap, so only the whole-pass
        # budget can stop this.
        per_chunk = indexer_mod._COOC_MAX_CHUNK_TOKENS
        pairs_each = per_chunk * (per_chunk - 1)
        n_files = (indexer_mod._COOC_MAX_PAIR_UPDATES // pairs_each) + 3
        for i in range(n_files):
            (self.root / f"f{i}.js").write_text(
                _minified_like(per_chunk), encoding="utf-8")

        _, result = self._build()
        stats = result["cooccurrence"]

        self.assertLessEqual(
            stats["pair_updates"], indexer_mod._COOC_MAX_PAIR_UPDATES)
        self.assertTrue(stats["budget_exhausted"])

    def test_ordinary_project_is_unaffected(self):
        """The bound must not change the synonym map for normal code."""
        (self.root / "a.py").write_text(
            "def alpha_helper():\n"
            "    return beta_helper() + gamma_helper()\n", encoding="utf-8")
        (self.root / "b.py").write_text(
            "def beta_helper():\n"
            "    return alpha_helper() + gamma_helper()\n", encoding="utf-8")

        idx, result = self._build()
        stats = result["cooccurrence"]

        self.assertEqual(stats["chunks_skipped"], 0)
        self.assertFalse(stats["budget_exhausted"])
        self.assertGreater(stats["pair_updates"], 0)


if __name__ == "__main__":
    unittest.main()
