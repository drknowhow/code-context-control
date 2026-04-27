"""Memory Consolidator — 4-phase pipeline for memory maintenance.

Inspired by biological memory consolidation ("sleep cycles"):
  Phase 1 (Triage):    Score all facts, detect new co-recall edges
  Phase 2 (Merge):     Cluster similar facts, merge duplicates
  Phase 3 (Reinforce): Pre-warm graph neighbourhood for working files
  Phase 4 (Prune):     Archive low-salience facts, decay stale edges

Also provides cross-session relevance:
  - Session fingerprints (files touched + facts recalled + decisions)
  - Session similarity matching for context priming
  - Fact lifespan analysis (foundational vs contextual)
  - Trend detection (hot zones under active development)
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.memory_scorer import MemoryScorer


class SessionFingerprint:
    """Compact representation of a session for similarity matching."""

    def __init__(
        self,
        session_id: str,
        files: list[str],
        facts_recalled: list[str],
        decisions: list[str],
        timestamp: str = "",
    ):
        self.session_id = session_id
        self.files = set(files)
        self.facts_recalled = set(facts_recalled)
        self.decisions = decisions
        self.timestamp = timestamp or datetime.now(timezone.utc).isoformat()

    def similarity(self, other: "SessionFingerprint") -> float:
        """Jaccard similarity across files + facts."""
        file_sim = _jaccard_sets(self.files, other.files)
        fact_sim = _jaccard_sets(self.facts_recalled, other.facts_recalled)
        # Weight files more — they're more stable signals
        return 0.6 * file_sim + 0.4 * fact_sim

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "files": sorted(self.files),
            "facts_recalled": sorted(self.facts_recalled),
            "decisions": self.decisions,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionFingerprint":
        return cls(
            session_id=data.get("session_id", ""),
            files=data.get("files", []),
            facts_recalled=data.get("facts_recalled", []),
            decisions=data.get("decisions", []),
            timestamp=data.get("timestamp", ""),
        )


class MemoryConsolidator:
    """Orchestrates the 4-phase memory consolidation pipeline."""

    def __init__(
        self,
        memory_store: Any,
        graph: Any,
        scorer: MemoryScorer | None = None,
        project_path: str = "",
        data_dir: str = ".c3/facts",
    ):
        self.memory = memory_store
        self.graph = graph
        self.scorer = scorer or MemoryScorer()
        self.project_path = Path(project_path) if project_path else Path(".")
        self.data_dir = self.project_path / data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.fingerprints_file = self.data_dir / "session_fingerprints.json"
        self.fingerprints: list[SessionFingerprint] = self._load_fingerprints()

    # ── Full pipeline ───────────────────────────────────────────────

    def run(self, current_session: dict | None = None) -> dict:
        """Execute all 4 phases. Returns combined stats."""
        stats: dict[str, Any] = {"phases": {}}

        # Phase 1: Triage
        triage = self.phase_triage(current_session)
        stats["phases"]["triage"] = triage

        # Phase 2: Merge
        merge = self.phase_merge()
        stats["phases"]["merge"] = merge

        # Phase 3: Reinforce (only if we have a current session)
        if current_session:
            reinforce = self.phase_reinforce(current_session)
            stats["phases"]["reinforce"] = reinforce

        # Phase 4: Prune
        prune = self.phase_prune()
        stats["phases"]["prune"] = prune

        stats["total_facts"] = len([
            f for f in self.memory.facts
            if f.get("lifecycle") == "active"
        ])
        return stats

    # ── Phase 1: Triage ─────────────────────────────────────────────

    def phase_triage(self, session: dict | None = None) -> dict:
        """Score all facts and record session fingerprint."""
        facts = [
            f for f in self.memory.facts
            if f.get("lifecycle") == "active"
        ]

        # Score everything
        scores = self.scorer.score_batch(facts, self.graph)
        tier_counts = Counter(s["tier"] for s in scores)

        # Record co-recall edges from this session's recalled facts
        co_recall_edges = 0
        if session:
            recalled_ids = []
            for f in facts:
                sessions = f.get("recall_sessions", [])
                sid = session.get("id", "")
                if sid and sid in sessions:
                    recalled_ids.append(f["id"])
            if len(recalled_ids) >= 2 and self.graph:
                co_recall_edges = self.graph.record_co_recall(recalled_ids)

            # Save session fingerprint
            files = [
                fc.get("file", "") for fc in session.get("files_touched", [])
                if fc.get("file")
            ]
            decisions = [
                d.get("decision", "") for d in session.get("decisions", [])
                if d.get("decision")
            ]
            fp = SessionFingerprint(
                session_id=session.get("id", ""),
                files=files,
                facts_recalled=recalled_ids,
                decisions=decisions,
            )
            self.fingerprints.append(fp)
            # Keep last 100 fingerprints
            self.fingerprints = self.fingerprints[-100:]
            self._save_fingerprints()

        return {
            "scored": len(scores),
            "tiers": dict(tier_counts),
            "co_recall_edges": co_recall_edges,
        }

    # ── Phase 2: Merge ──────────────────────────────────────────────

    def phase_merge(self) -> dict:
        """Merge duplicate facts using graph clusters and text similarity."""
        facts = [
            f for f in self.memory.facts
            if f.get("lifecycle") == "active"
        ]
        if len(facts) < 2:
            return {"merged": 0}

        merged = 0
        to_delete: set[str] = set()

        # Use graph clusters first — facts in the same cluster are related
        clusters = self.graph.detect_clusters(min_cluster_size=2) if self.graph else []

        for cluster in clusters:
            cluster_facts = [
                f for f in facts
                if f["id"] in set(cluster) and f["id"] not in to_delete
            ]
            if len(cluster_facts) < 2:
                continue

            # Within each cluster, check for text similarity
            for i, a in enumerate(cluster_facts):
                if a["id"] in to_delete:
                    continue
                for b in cluster_facts[i + 1:]:
                    if b["id"] in to_delete:
                        continue
                    sim = _jaccard_text(a["fact"], b["fact"])
                    if sim > 0.55:
                        keeper, victim = self._pick_keeper(a, b)
                        if sim < 0.85:
                            keeper["fact"] = _merge_texts(
                                keeper["fact"], victim["fact"]
                            )
                            try:
                                self.memory.update_fact(
                                    keeper["id"], keeper["fact"],
                                    keeper.get("category", "general"),
                                )
                            except Exception:
                                pass
                        # Transfer graph edges from victim to keeper
                        if self.graph:
                            self.graph.record_refinement(victim["id"], keeper["id"])
                        to_delete.add(victim["id"])
                        merged += 1

        # Also do a global pass for non-clustered duplicates
        unclustered = [
            f for f in facts
            if f["id"] not in to_delete
            and not any(f["id"] in c for c in clusters)
        ]
        for i, a in enumerate(unclustered):
            if a["id"] in to_delete:
                continue
            for b in unclustered[i + 1:]:
                if b["id"] in to_delete:
                    continue
                sim = _jaccard_text(a["fact"], b["fact"])
                if sim > 0.55:
                    keeper, victim = self._pick_keeper(a, b)
                    if sim < 0.85:
                        keeper["fact"] = _merge_texts(
                            keeper["fact"], victim["fact"]
                        )
                        try:
                            self.memory.update_fact(
                                keeper["id"], keeper["fact"],
                                keeper.get("category", "general"),
                            )
                        except Exception:
                            pass
                    if self.graph:
                        self.graph.record_refinement(victim["id"], keeper["id"])
                    to_delete.add(victim["id"])
                    merged += 1
            if merged >= 50:  # safety cap per run
                break

        for fid in to_delete:
            try:
                self.memory.delete_fact(fid)
            except Exception:
                pass
            if self.graph:
                self.graph.remove_node(fid)

        return {"merged": merged, "deleted": len(to_delete)}

    # ── Phase 3: Reinforce ──────────────────────────────────────────

    def phase_reinforce(self, session: dict) -> dict:
        """Pre-warm memory for the current working context.

        Uses session fingerprint similarity and graph spreading activation
        to identify facts likely to be relevant.
        """
        # Find similar past sessions
        current_files = [
            fc.get("file", "") for fc in session.get("files_touched", [])
            if fc.get("file")
        ]
        current_fp = SessionFingerprint(
            session_id=session.get("id", ""),
            files=current_files,
            facts_recalled=[],
            decisions=[],
        )

        similar_sessions = self.find_similar_sessions(current_fp, top_k=3)

        # Collect fact IDs from similar sessions
        primed_fact_ids: set[str] = set()
        for sfp, sim in similar_sessions:
            primed_fact_ids.update(sfp.facts_recalled)

        # Also use graph spreading activation from file-touching facts
        if self.graph:
            file_facts: list[str] = []
            for f in current_files[:10]:
                file_facts.extend(self.graph.get_facts_touching(f))

            if file_facts:
                activated = self.graph.spreading_activation(
                    seed_ids=file_facts, max_depth=2, max_results=20
                )
                for a in activated:
                    primed_fact_ids.add(a["id"])

        return {
            "similar_sessions": len(similar_sessions),
            "primed_facts": len(primed_fact_ids),
            "primed_ids": sorted(primed_fact_ids)[:20],
        }

    # ── Phase 4: Prune ──────────────────────────────────────────────

    def phase_prune(self) -> dict:
        """Archive low-salience facts and decay stale graph edges."""
        facts = [
            f for f in self.memory.facts
            if f.get("lifecycle") == "active"
        ]

        archived = 0
        to_delete: set[str] = set()

        for f in facts:
            score = self.scorer.score(f, self.graph)
            tier = score["tier"]

            if tier == "ephemeral":
                # Auto-archive ephemeral facts older than 3 days
                age = self._fact_age_days(f)
                if age >= 3:
                    to_delete.add(f["id"])
                    archived += 1

            elif tier == "dormant":
                # Archive dormant facts older than 14 days
                age = self._fact_age_days(f)
                if age >= 14:
                    to_delete.add(f["id"])
                    archived += 1

        # Rolling window: keep only last 5 auto:session entries
        session_facts = sorted(
            [f for f in facts
             if f.get("category") == "auto:session"
             and f["id"] not in to_delete],
            key=lambda f: f.get("timestamp", ""),
            reverse=True,
        )
        for f in session_facts[5:]:
            to_delete.add(f["id"])
            archived += 1

        for fid in to_delete:
            try:
                self.memory.delete_fact(fid)
            except Exception:
                pass
            if self.graph:
                self.graph.remove_node(fid)

        # Decay graph edges
        edges_decayed = 0
        if self.graph:
            edges_decayed = self.graph.decay_edges(half_life_days=30.0)

        return {
            "archived": archived,
            "edges_decayed": edges_decayed,
            "remaining": len(facts) - archived,
        }

    # ── Cross-session analysis ──────────────────────────────────────

    def find_similar_sessions(
        self,
        target: SessionFingerprint,
        top_k: int = 5,
    ) -> list[tuple[SessionFingerprint, float]]:
        """Find past sessions most similar to the target."""
        results: list[tuple[SessionFingerprint, float]] = []
        for fp in self.fingerprints:
            if fp.session_id == target.session_id:
                continue
            sim = target.similarity(fp)
            if sim > 0.1:
                results.append((fp, round(sim, 4)))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_k]

    def fact_lifespan_analysis(self) -> dict:
        """Classify facts as foundational vs contextual.

        Foundational: recalled across many different sessions
        Contextual:   recalled only within a narrow set of sessions
        """
        facts = [
            f for f in self.memory.facts
            if f.get("lifecycle") == "active"
        ]

        foundational: list[dict] = []
        contextual: list[dict] = []

        for f in facts:
            sessions = set(f.get("recall_sessions", []))
            session_count = len(sessions)
            score = self.scorer.score(f, self.graph)

            entry = {
                "id": f["id"],
                "fact": f["fact"][:80],
                "session_spread": session_count,
                "salience": score["salience"],
                "tier": score["tier"],
            }

            if session_count >= 3:
                foundational.append(entry)
            elif session_count <= 1 and self._fact_age_days(f) > 7:
                contextual.append(entry)

        foundational.sort(key=lambda x: x["session_spread"], reverse=True)
        contextual.sort(key=lambda x: x["salience"])

        return {
            "foundational": foundational[:20],
            "contextual": contextual[:20],
            "total_facts": len(facts),
        }

    def detect_trends(self) -> dict:
        """Detect hot zones — files/areas under active development.

        Looks at recent session fingerprints to find frequently-touched files
        and frequently-recalled facts.
        """
        recent = self.fingerprints[-20:]  # last 20 sessions
        if not recent:
            return {"hot_files": [], "hot_facts": [], "sessions_analyzed": 0}

        file_counts: Counter = Counter()
        fact_counts: Counter = Counter()

        for fp in recent:
            for f in fp.files:
                file_counts[f] += 1
            for fid in fp.facts_recalled:
                fact_counts[fid] += 1

        hot_files = [
            {"file": f, "sessions": c}
            for f, c in file_counts.most_common(10)
            if c >= 2
        ]
        hot_facts = [
            {"fact_id": fid, "sessions": c}
            for fid, c in fact_counts.most_common(10)
            if c >= 2
        ]

        # Enrich hot_facts with fact text
        facts_by_id = {f["id"]: f for f in self.memory.facts}
        for hf in hot_facts:
            fact = facts_by_id.get(hf["fact_id"])
            if fact:
                hf["fact"] = fact["fact"][:80]

        return {
            "hot_files": hot_files,
            "hot_facts": hot_facts,
            "sessions_analyzed": len(recent),
        }

    # ── Helpers ─────────────────────────────────────────────────────

    def _pick_keeper(self, a: dict, b: dict) -> tuple[dict, dict]:
        """Pick which fact to keep based on salience."""
        sa = self.scorer.score(a, self.graph)["salience"]
        sb = self.scorer.score(b, self.graph)["salience"]
        return (a, b) if sa >= sb else (b, a)

    @staticmethod
    def _fact_age_days(fact: dict) -> float:
        ref = fact.get("last_accessed_at") or fact.get("timestamp")
        if not ref:
            return 0.0
        try:
            dt = datetime.fromisoformat(ref)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        except (ValueError, TypeError):
            return 0.0

    def _load_fingerprints(self) -> list[SessionFingerprint]:
        if not self.fingerprints_file.exists():
            return []
        try:
            with open(self.fingerprints_file, encoding="utf-8") as f:
                data = json.load(f)
            return [SessionFingerprint.from_dict(d) for d in data]
        except Exception:
            return []

    def _save_fingerprints(self):
        data = [fp.to_dict() for fp in self.fingerprints]
        with open(self.fingerprints_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


# ── Module-level helpers ────────────────────────────────────────────

def _jaccard_sets(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def _jaccard_text(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    return _jaccard_sets(ta, tb)


def _merge_texts(existing: str, new: str) -> str:
    """Merge two fact texts, preferring the more complete one."""
    if len(new) > len(existing) * 1.3:
        return new
    if len(existing) > len(new) * 1.3:
        return existing
    # Similar length — combine unique sentences
    existing_sentences = set(s.strip() for s in existing.split(".") if s.strip())
    new_sentences = set(s.strip() for s in new.split(".") if s.strip())
    combined = existing_sentences | new_sentences
    return ". ".join(sorted(combined)) + "." if combined else existing
