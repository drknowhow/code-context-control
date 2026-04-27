"""Memory Scoring Engine — composite salience scores for facts.

Replaces raw relevance_count with a multi-signal score that captures
recency, frequency, cross-session spread, co-activation strength,
source authority, confirmation history, and contradiction penalties.

Each signal is normalized to [0, 1] and combined via weighted sum.
The final salience score determines recall ranking, consolidation
priority, and pruning eligibility.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# ── Signal weights (sum to 1.0) ─────────────────────────────────────
DEFAULT_WEIGHTS = {
    "recency":        0.20,
    "frequency":      0.15,
    "cross_session":  0.20,
    "co_activation":  0.10,
    "source_auth":    0.10,
    "confirmation":   0.15,
    "contradiction":  0.10,
}

# ── Tier thresholds ─────────────────────────────────────────────────
TIER_CORE      = 0.70   # immune to pruning
TIER_ACTIVE    = 0.40   # normal retention
TIER_DORMANT   = 0.20   # candidate for archival
# below DORMANT = ephemeral, auto-pruned


class MemoryScorer:
    """Computes and caches composite salience scores for memory facts."""

    _CACHE_MAX = 512

    def __init__(self, weights: dict[str, float] | None = None):
        w = {**DEFAULT_WEIGHTS, **(weights or {})}
        total = sum(w.values())
        # Normalize so weights always sum to 1.0
        self.weights = {k: v / total for k, v in w.items()}
        # Bounded score cache keyed on fact state. Natural invalidation via
        # relevance-bucket and signal-count keys; no explicit busting needed
        # except on graph-wide mutations (see invalidate_all).
        self._score_cache: dict[tuple, dict] = {}

    # ── Public API ──────────────────────────────────────────────────

    def _cache_key(self, fact: dict) -> tuple:
        """Cache key tolerates log-scale rc drift (every 5 recalls → new entry)."""
        rc = int(fact.get("relevance_count", 0))
        return (
            fact.get("id", ""),
            rc // 5,
            len(fact.get("recall_sessions") or []),
            int(fact.get("confirmation_count", 0)),
            int(fact.get("contradiction_count", 0)),
            fact.get("source_quality", ""),
        )

    def invalidate(self, fact_id: str) -> None:
        """Drop cached scores for a single fact (called on update/delete)."""
        for key in [k for k in self._score_cache if k and k[0] == fact_id]:
            self._score_cache.pop(key, None)

    def invalidate_all(self) -> None:
        """Drop the entire cache (called on graph-wide mutations)."""
        self._score_cache.clear()

    def score(self, fact: dict, graph: Any = None) -> dict:
        """Compute all signals and return a scoring breakdown.

        Args:
            fact: A fact dict from MemoryStore.
            graph: Optional MemoryGraph for co-activation lookups.

        Returns:
            dict with per-signal scores, weighted total, and tier.
        """
        key = self._cache_key(fact)
        cached = self._score_cache.get(key)
        if cached is not None:
            return cached

        signals = {
            "recency":        self._recency(fact),
            "frequency":      self._frequency(fact),
            "cross_session":  self._cross_session(fact),
            "co_activation":  self._co_activation(fact, graph),
            "source_auth":    self._source_authority(fact),
            "confirmation":   self._confirmation(fact),
            "contradiction":  self._contradiction(fact),
        }

        salience = sum(
            self.weights[k] * v for k, v in signals.items()
        )
        salience = round(max(0.0, min(1.0, salience)), 4)

        tier = self._tier(salience)

        result = {
            "salience": salience,
            "tier": tier,
            "signals": {k: round(v, 4) for k, v in signals.items()},
        }

        if len(self._score_cache) >= self._CACHE_MAX:
            # Drop oldest ~20% (dict is insertion-ordered)
            victims = list(self._score_cache.keys())[: self._CACHE_MAX // 5]
            for v in victims:
                self._score_cache.pop(v, None)
        self._score_cache[key] = result
        return result

    def score_batch(self, facts: list[dict], graph: Any = None) -> list[dict]:
        """Score a list of facts. Returns list of (fact_id, score_dict) pairs."""
        results = []
        for f in facts:
            s = self.score(f, graph)
            results.append({"id": f.get("id", ""), **s})
        results.sort(key=lambda x: x["salience"], reverse=True)
        return results

    def tier_partition(self, facts: list[dict], graph: Any = None) -> dict[str, list[dict]]:
        """Partition facts into tier buckets."""
        buckets: dict[str, list[dict]] = {
            "core": [], "active": [], "dormant": [], "ephemeral": [],
        }
        for f in facts:
            s = self.score(f, graph)
            f_with_score = {**f, "_score": s}
            buckets[s["tier"]].append(f_with_score)
        return buckets

    # ── Signal functions (each returns 0.0 - 1.0) ──────────────────

    def _recency(self, fact: dict) -> float:
        """Exponential decay based on last access or creation time."""
        ref = fact.get("last_accessed_at") or fact.get("timestamp")
        if not ref:
            return 0.0
        try:
            dt = datetime.fromisoformat(ref)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            return 0.0
        # Half-life of 14 days: score halves every 14 days of inactivity
        half_life = 14.0
        return math.exp(-0.693 * age_days / half_life)

    def _frequency(self, fact: dict) -> float:
        """Log-scaled recall count with diminishing returns."""
        count = int(fact.get("relevance_count", 0))
        if count <= 0:
            return 0.0
        # log2(count+1) / log2(max_expected+1), capped at 1.0
        # 32 recalls = score 1.0
        return min(1.0, math.log2(count + 1) / 5.0)

    def _cross_session(self, fact: dict) -> float:
        """How many distinct sessions have recalled this fact."""
        sessions = fact.get("recall_sessions", [])
        if isinstance(sessions, list):
            n = len(set(sessions))
        else:
            n = 0
        # Also count source session
        if fact.get("source_session"):
            n = max(n, 1)
        # 5+ sessions = max score
        return min(1.0, n / 5.0)

    def _co_activation(self, fact: dict, graph: Any = None) -> float:
        """Strength of connections to other facts in the graph."""
        if graph is None:
            return 0.5  # neutral when no graph available
        fact_id = fact.get("id", "")
        if not fact_id:
            return 0.0
        edges = graph.get_edges(fact_id)
        if not edges:
            return 0.0
        # Sum of edge weights, capped
        total_weight = sum(e.get("weight", 1) for e in edges)
        # 10+ total weight = max score
        return min(1.0, total_weight / 10.0)

    def _source_authority(self, fact: dict) -> float:
        """User-provided facts score higher than auto-extracted."""
        quality = fact.get("source_quality", "user")
        authority_map = {
            "user":       1.0,
            "validated":  0.9,
            "inferred":   0.6,
            "auto":       0.4,
            "auto:search": 0.3,
            "auto:session": 0.2,
        }
        # Also check category prefix for auto-categorized facts
        cat = fact.get("category", "")
        if quality in authority_map:
            base = authority_map[quality]
        elif cat.startswith("auto:"):
            base = authority_map.get(cat, 0.4)
        else:
            base = 0.5
        return base

    def _confirmation(self, fact: dict) -> float:
        """How many times this fact has been confirmed/validated."""
        count = int(fact.get("confirmation_count", 0))
        if count <= 0:
            return 0.0
        # 3 confirmations = max score
        return min(1.0, count / 3.0)

    def _contradiction(self, fact: dict) -> float:
        """Inverse penalty — contradictions reduce this score.

        Returns 1.0 for no contradictions, 0.0 for heavily contradicted.
        This is inverted so that the weighted sum penalizes contradictions.
        """
        count = int(fact.get("contradiction_count", 0))
        if count <= 0:
            return 1.0  # no contradictions = full score
        # Each contradiction halves the score
        return max(0.0, 1.0 / (1.0 + count))

    # ── Tier classification ─────────────────────────────────────────

    @staticmethod
    def _tier(salience: float) -> str:
        if salience >= TIER_CORE:
            return "core"
        if salience >= TIER_ACTIVE:
            return "active"
        if salience >= TIER_DORMANT:
            return "dormant"
        return "ephemeral"
