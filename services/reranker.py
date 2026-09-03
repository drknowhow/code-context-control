"""Optional cross-encoder reranking for c3_search (plan phase P4).

A reranker scores (query, passage) pairs jointly, which a bi-encoder or BM25
cannot, at the price of one model pass per candidate. It is applied only to
natural-language queries, only to the top few fused candidates, and only when
the user opted in (`search_rerank: "auto"` in the hybrid config): it must
earn default status on the relevance suite, and on this repository's golden
suite it has not (see docs/search-eval.md for the measurement).

Contract (:class:`Reranker`): ``ready`` plus
``rerank(query, [(id, text)]) -> [(id, score)]`` best first. The shipped
adapter wraps FlashRank (ONNX, CPU, models of 4-22 MB downloaded once into
``~/.c3/models/flashrank``); anything with the same two members plugs in.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger("c3.reranker")

DEFAULT_MODEL = "ms-marco-TinyBERT-L-2-v2"   # 4 MB, ~0.8 s cold, ~20-100 ms per 16 passages
DEFAULT_TOP_N = 16
_MAX_PASSAGE_CHARS = 1500


@runtime_checkable
class Reranker(Protocol):
    @property
    def ready(self) -> bool: ...

    def rerank(self, query: str, docs: list[tuple[str, str]]) -> list[tuple[str, float]]: ...


def default_cache_dir() -> Path:
    return Path.home() / ".c3" / "models" / "flashrank"


def is_natural_language(query: str, base_tokens: list[str]) -> bool:
    """A query worth a cross-encoder pass: three or more words, at least two
    of them prose — lowercase, letters only, as typed. Judged on the raw
    words, not on the alias tokens: ``migrate_v2 sqlite_store v3`` splits
    into ``migrate``, ``sqlite``, ``store`` and would look like prose to a
    token count, but it is identifier soup.

    ``compute_total``, ``OAuth2Client``, ``sha256 digest`` are identifier
    lookups; the lexical engine and the symbol fast path already handle them
    and a reranker trained on web passages only adds latency and noise.
    """
    q = (query or "").strip()
    words = q.split()
    if len(words) < 3 or len([t for t in base_tokens if t]) < 3:
        return False
    prose = [w for w in words if w.isalpha() and w.islower()]
    return len(prose) >= 2


def passage_text(chunk: dict) -> str:
    """What the reranker sees for one chunk: path, symbol, then content (capped)."""
    head = str(chunk.get("doc_id") or "")
    name = chunk.get("name")
    if name:
        head += f": {name}"
    body = (chunk.get("content") or "")[:_MAX_PASSAGE_CHARS]
    return f"{head}\n{body}" if body else head


def flashrank_available() -> bool:
    try:
        import flashrank  # noqa: F401
        return True
    except Exception:
        return False


class FlashRankReranker:
    """FlashRank adapter. The model loads on the first call, never at import
    or construction, so attaching it costs nothing until a query uses it."""

    name = "flashrank"

    def __init__(self, model_name: str = DEFAULT_MODEL, cache_dir=None, max_length: int = 512):
        self.model_name = model_name or DEFAULT_MODEL
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.max_length = int(max_length)
        self.available = flashrank_available()
        self._ranker = None
        self._failed = False
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.available and not self._failed

    @property
    def loaded(self) -> bool:
        return self._ranker is not None

    def _load(self):
        if self._ranker is not None or self._failed:
            return
        with self._lock:
            if self._ranker is not None or self._failed:
                return
            try:
                from flashrank import Ranker
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._ranker = Ranker(model_name=self.model_name, cache_dir=str(self.cache_dir),
                                      max_length=self.max_length)
            except Exception as exc:
                self._failed = True
                log.warning("reranker %s unavailable (%s); results stay in fused order", self.model_name, exc)

    def rerank(self, query: str, docs: list[tuple[str, str]]) -> list[tuple[str, float]]:
        if not docs:
            return []
        self._load()
        if self._ranker is None:
            return []
        from flashrank import RerankRequest
        passages = [{"id": cid, "text": text} for cid, text in docs]
        out = self._ranker.rerank(RerankRequest(query=query, passages=passages))
        return [(str(o["id"]), float(o["score"])) for o in out]
