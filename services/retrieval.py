"""Retrieval backend contract and rank fusion (plan phase P3).

A backend produces a ranked candidate list of chunk ids for a query; the
index fuses lists from several backends and assembles the response. Keeping
the contract this small is what lets the lexical engine (FTS5 / TF-IDF), the
dense engine (``EmbeddingIndex``) and any experimental adapter be measured on
the same relevance suite.

Fusion is Reciprocal Rank Fusion: ``score(c) = Σ 1 / (k + rank_i(c))`` over
the lists that contain ``c``. Scores from different engines are never
compared directly — BM25 and cosine similarity live on unrelated scales and
normalising them per query is the kind of tuning debt that flips results
between runs. ``k = 60`` is the literature default and a starting hypothesis
(``search_rrf_k`` in ``.c3/config.json``), not doctrine.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

DEFAULT_RRF_K = 60


@runtime_checkable
class RetrievalBackend(Protocol):
    """What ``CodeIndex`` needs from a candidate source.

    ``EmbeddingIndex`` satisfies this directly; a lexical backend is the
    index's own store. Anything with these two members can be plugged into
    ``CodeIndex.dense`` and evaluated with ``c3 search-eval``.
    """

    @property
    def ready(self) -> bool: ...

    def candidates(self, query: str, limit: int = 40) -> list[tuple[str, float]]:
        """``[(chunk_id, score)]`` best first. Scores are backend-local."""
        ...


def rrf(rankings: Iterable[Iterable[str]], k: int = DEFAULT_RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion over ordered id lists (best first).

    An id present in more lists, or earlier in one, scores higher. Ids seen
    twice in one list count once, at their first position.
    """
    k = max(1, int(k))
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        rank = 0
        for cid in ranking:
            if cid in seen:
                continue
            seen.add(cid)
            rank += 1
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
    return scores
