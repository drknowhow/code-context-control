"""Lightweight incremental text index used by local memory stores."""

from __future__ import annotations

import math
import re
from collections import Counter


class TextIndex:
    """Incremental TF-IDF index over small local document collections."""

    def __init__(self):
        self._docs: dict[str, str] = {}
        self._tf: dict[str, Counter] = {}
        self._df: Counter = Counter()

    @staticmethod
    def tokenize(text: str) -> list[str]:
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
        text = text.replace("_", " ").replace("-", " ")
        return re.findall(r"[a-zA-Z0-9]{2,}", text.lower())

    def __len__(self) -> int:
        return len(self._docs)

    def clear(self):
        self._docs.clear()
        self._tf.clear()
        self._df.clear()

    def ids(self) -> list[str]:
        return list(self._docs.keys())

    def get_text(self, doc_id: str) -> str:
        return self._docs.get(doc_id, "")

    def rebuild(self, docs: dict[str, str]):
        self.clear()
        for doc_id, text in docs.items():
            self.add_or_update(doc_id, text)

    def add_or_update(self, doc_id: str, text: str):
        text = text or ""
        old_tf = self._tf.get(doc_id)
        if old_tf:
            for token in old_tf:
                self._df[token] -= 1
                if self._df[token] <= 0:
                    del self._df[token]

        tokens = self.tokenize(text)
        tf = Counter(tokens)
        self._docs[doc_id] = text
        self._tf[doc_id] = tf
        for token in tf:
            self._df[token] += 1

    def remove(self, doc_id: str):
        old_tf = self._tf.pop(doc_id, None)
        self._docs.pop(doc_id, None)
        if not old_tf:
            return
        for token in old_tf:
            self._df[token] -= 1
            if self._df[token] <= 0:
                del self._df[token]

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        query_tokens = self.tokenize(query)
        if not query_tokens or not self._docs:
            return []

        q_terms = Counter(query_tokens)
        total_docs = len(self._docs)
        scores: dict[str, float] = {}

        for doc_id, term_counts in self._tf.items():
            total_terms = sum(term_counts.values()) or 1
            score = 0.0
            for term, q_count in q_terms.items():
                if term not in term_counts:
                    continue
                tf = term_counts[term] / total_terms
                idf = math.log((total_docs + 1) / (self._df.get(term, 0) + 1)) + 1.0
                score += tf * idf * q_count
            if score > 0:
                scores[doc_id] = score

        return sorted(scores.items(), key=lambda item: item[1], reverse=True)[:top_k]
