"""Background Agents — Concrete agent implementations + factory.

Base class lives in services/agent_base.py.
"""
import hashlib
import json
import math
import re
import time
from collections import Counter
from pathlib import Path

from services.agent_base import BackgroundAgent  # noqa: F401 — re-exported for consumers
from services.git_context import GitContext


class IndexStalenessAgent(BackgroundAgent):
    """Monitors file changes and triggers index rebuild when threshold is reached."""

    def __init__(self, watcher, indexer, notifications, enabled=True, interval=60,
                 warn_threshold=5, rebuild_threshold=15, **kwargs):
        super().__init__("IndexStaleness", interval, notifications, enabled, **kwargs)
        self.watcher = watcher
        self.indexer = indexer
        self.warn_threshold = warn_threshold
        self.rebuild_threshold = rebuild_threshold
        self._last_warned_count = 0

    def check(self):
        count = self.watcher._handler.change_count
        if count >= self.rebuild_threshold:
            # Capture changed file paths before rebuild resets the list
            changed_files = []
            try:
                changes = self.watcher._handler._changes
                changed_files = [c.get("path", "") for c in changes if isinstance(c, dict)]
            except Exception:
                pass

            self.watcher.rebuild_if_needed(self.indexer, threshold=self.rebuild_threshold)
            msg = f"Rebuilt after {count} file changes"
            used_ai = False

            # AI: summarize what areas changed
            if self.ai_available and changed_files:
                # Group by directory for better summaries
                dirs = Counter(str(Path(f).parent) for f in changed_files if f)
                top_dirs = ", ".join(f"{d} ({c})" for d, c in dirs.most_common(5))
                summary = self._ai_generate(
                    f"These files changed in a codebase:\n{chr(10).join(changed_files[:20])}\n"
                    f"Top directories: {top_dirs}\n\n"
                    "Summarize in ONE sentence what areas/components were affected.",
                    system="You are a concise code analyst. Reply in one sentence only.",
                    max_tokens=80,
                )
                if summary:
                    msg += f" — {summary.strip()}"
                    used_ai = True

            self.notify("info", "Index auto-rebuilt", msg, ai_enhanced=used_ai, replace_if_unacked=True)
            self._last_warned_count = 0
        elif count >= self.warn_threshold and count != self._last_warned_count:
            self._last_warned_count = count
            self.notify("warning", "Index is stale", f"{count} file changes since last rebuild",
                        replace_if_unacked=True)


class MemoryPrunerAgent(BackgroundAgent):
    """Finds duplicate and unused facts in the memory store."""

    _CONSOLIDATE_EVERY_N = 10  # every ~50 min at default 300s interval

    def __init__(self, memory, notifications, enabled=True, interval=300,
                 similarity_threshold=0.8, embed_model="nomic-embed-text", **kwargs):
        super().__init__("MemoryPruner", interval, notifications, enabled, **kwargs)
        self.memory = memory
        self.similarity_threshold = similarity_threshold
        self.embed_model = embed_model
        self._embedding_cache = {}  # fact_id -> vector
        self._last_fact_count = 0
        self._cycle_count = 0

    def check(self):
        self._cycle_count += 1
        if self._cycle_count % self._CONSOLIDATE_EVERY_N == 0:
            self._auto_consolidate()

        facts = self.memory.facts
        # Skip duplicate/unused checks if fact count hasn't changed
        if len(facts) == self._last_fact_count:
            return
        self._last_fact_count = len(facts)

        if len(facts) < 2:
            return

        # Try AI-powered duplicate detection, fall back to Jaccard
        if self.ai_available:
            duplicates = self._find_duplicates_embedding(facts)
        else:
            duplicates = self._find_duplicates_jaccard(facts)

        # Find unused facts (relevance_count == 0, only if enough facts exist)
        unused = []
        if len(facts) > 10:
            unused = [f for f in facts if f.get("relevance_count", 0) == 0]

        if duplicates:
            # Auto-delete near-identical duplicates (sim >= 0.95) — keep higher relevance_count
            auto_deleted = 0
            remaining_duplicates = []
            for a_id, b_id, sim in duplicates:
                if sim >= 0.95:
                    a_fact = next((f for f in facts if f["id"] == a_id), None)
                    b_fact = next((f for f in facts if f["id"] == b_id), None)
                    if a_fact and b_fact:
                        victim = b_fact if a_fact.get("relevance_count", 0) >= b_fact.get("relevance_count", 0) else a_fact
                        try:
                            self.memory.delete_fact(victim["id"])
                            auto_deleted += 1
                        except Exception:
                            remaining_duplicates.append((a_id, b_id, sim))
                else:
                    remaining_duplicates.append((a_id, b_id, sim))

            if auto_deleted:
                self.notify("info", "Auto-removed near-identical facts",
                            f"Deleted {auto_deleted} fact(s) with similarity ≥ 0.95")

            if remaining_duplicates:
                pairs_str = "; ".join(f"{a}≈{b} ({sim})" for a, b, sim in remaining_duplicates[:3])
                used_ai = False

                # AI: propose merged text for top duplicate pair
                if self.ai_available and remaining_duplicates:
                    a_id, b_id, _ = remaining_duplicates[0]
                    a_text = next((f["fact"] for f in facts if f["id"] == a_id), "")
                    b_text = next((f["fact"] for f in facts if f["id"] == b_id), "")
                    merged = self._ai_generate(
                        f"These two facts are duplicates. Merge them into one concise fact:\n"
                        f"1: {a_text}\n2: {b_text}\n\nMerged fact:",
                        system="You merge duplicate knowledge base entries. Output only the merged text.",
                        max_tokens=120,
                    )
                    if merged:
                        pairs_str += f"\n\nSuggested merge: {merged.strip()}"
                        used_ai = True

                self.notify("info", "Duplicate facts found",
                            f"{len(remaining_duplicates)} similar pair(s): {pairs_str}", ai_enhanced=used_ai)

        if unused:
            unused_preview = "; ".join(f["fact"][:40] for f in unused[:3])
            self.notify("info", "Unused facts detected",
                        f"{len(unused)} facts with 0 relevance — e.g. {unused_preview}")

    def _auto_consolidate(self):
        """Run lightweight cleanup: session rolling window + verbose orphan archive."""
        from datetime import datetime, timezone
        _MAX_SESSION_FACTS = 5
        _VERBOSE_CHARS = 600
        _VERBOSE_AGE_DAYS = 14
        _SESSION_AGE_DAYS = 7

        facts = self.memory.facts
        active = [f for f in facts if f.get("lifecycle", "active") == "active"]
        to_delete = set()
        now = datetime.now(timezone.utc)

        # Rolling window: keep only last N auto:session entries
        session_facts = sorted(
            [f for f in active if f.get("category") == "auto:session"],
            key=lambda f: f.get("timestamp", ""),
            reverse=True,
        )
        for f in session_facts[_MAX_SESSION_FACTS:]:
            to_delete.add(f["id"])

        # Stale auto:session with 0 recall after N days (catch any not pruned by rolling window)
        for f in active:
            if f["id"] in to_delete or f.get("category") != "auto:session":
                continue
            if f.get("relevance_count", 0) > 0:
                continue
            try:
                age = (now - datetime.fromisoformat(f.get("timestamp", ""))).days
            except (ValueError, TypeError):
                age = 0
            if age >= _SESSION_AGE_DAYS:
                to_delete.add(f["id"])

        # Verbose orphans: >600 chars, 0 recall, 14+ days
        for f in active:
            if f["id"] in to_delete:
                continue
            if len(f.get("fact", "")) <= _VERBOSE_CHARS:
                continue
            if f.get("relevance_count", 0) > 0:
                continue
            try:
                age = (now - datetime.fromisoformat(f.get("timestamp", ""))).days
            except (ValueError, TypeError):
                age = 0
            if age >= _VERBOSE_AGE_DAYS:
                to_delete.add(f["id"])

        removed = 0
        for fid in to_delete:
            try:
                self.memory.delete_fact(fid)
                removed += 1
            except Exception:
                pass

        if removed:
            self.notify("info", "Memory auto-cleanup",
                        f"Archived {removed} stale/verbose fact(s) (session window + orphan sweep)")

    def _find_duplicates_jaccard(self, facts):
        """Original Jaccard similarity duplicate detection."""
        duplicates = []
        token_sets = {}
        for f in facts:
            token_sets[f["id"]] = set(self.memory._tokenize(f["fact"]))

        seen = set()
        for i, f1 in enumerate(facts):
            for f2 in facts[i + 1:]:
                pair_key = (f1["id"], f2["id"])
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                s1, s2 = token_sets[f1["id"]], token_sets[f2["id"]]
                if not s1 or not s2:
                    continue
                jaccard = len(s1 & s2) / len(s1 | s2)
                if jaccard >= self.similarity_threshold:
                    duplicates.append((f1["id"], f2["id"], round(jaccard, 2)))
        return duplicates

    def _find_duplicates_embedding(self, facts):
        """Embedding-based cosine similarity duplicate detection."""
        # Identify facts needing new embeddings
        new_facts = [f for f in facts if f["id"] not in self._embedding_cache]
        if new_facts:
            texts = [f["fact"] for f in new_facts]
            embeddings = self.ollama.embed_batch(texts, model=self.embed_model)
            if embeddings is None:
                # Ollama failed — fall back to Jaccard for this cycle
                return self._find_duplicates_jaccard(facts)
            for f, emb in zip(new_facts, embeddings):
                self._embedding_cache[f["id"]] = emb

        # Prune cache for deleted facts
        live_ids = {f["id"] for f in facts}
        for fid in list(self._embedding_cache.keys()):
            if fid not in live_ids:
                del self._embedding_cache[fid]

        # Cosine similarity comparison
        duplicates = []
        fact_ids = [f["id"] for f in facts]
        for i in range(len(fact_ids)):
            for j in range(i + 1, len(fact_ids)):
                a, b = fact_ids[i], fact_ids[j]
                va, vb = self._embedding_cache.get(a), self._embedding_cache.get(b)
                if va is None or vb is None:
                    continue
                sim = self._cosine_similarity(va, vb)
                if sim >= self.similarity_threshold:
                    duplicates.append((a, b, round(sim, 2)))
        return duplicates

    @staticmethod
    def _cosine_similarity(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)


class ClaudeMdDriftAgent(BackgroundAgent):
    """Checks CLAUDE.md for staleness when files have changed."""

    def __init__(self, watcher, claude_md, notifications, enabled=True, interval=120, **kwargs):
        super().__init__("ClaudeMdDrift", interval, notifications, enabled, **kwargs)
        self.watcher = watcher
        self.claude_md = claude_md
        self._last_issues_hash = None

    def check(self):
        # Short-circuit if no file changes and we already checked
        if self.watcher._handler.change_count == 0 and self._last_issues_hash is not None:
            return

        try:
            result = self.claude_md.check_staleness()
        except Exception:
            return

        if result.get("status") != "stale":
            self._last_issues_hash = ""
            return

        # Hash issues list to avoid re-notifying for same state
        issues = result.get("issues", [])
        issues_str = json.dumps(issues, sort_keys=True)
        issues_hash = hashlib.md5(issues_str.encode()).hexdigest()

        if issues_hash == self._last_issues_hash:
            return
        self._last_issues_hash = issues_hash

        # AI: produce actionable summary instead of raw issue list
        if self.ai_available and issues:
            raw_issues = "; ".join(i.get("message", "")[:80] for i in issues)
            summary = self._ai_generate(
                f"CLAUDE.md has these staleness issues:\n{raw_issues}\n\n"
                "Write 1-2 actionable sentences about what to update.",
                system="You are a concise project documentation advisor. Be specific and actionable.",
                max_tokens=100,
            )
            if summary:
                self.notify("warning", "CLAUDE.md is stale", summary.strip(),
                            ai_enhanced=True, replace_if_unacked=True)
                return

        # Fallback: raw issue concatenation
        summary = "; ".join(i.get("message", "")[:60] for i in issues[:3])
        self.notify("warning", "CLAUDE.md is stale", summary, replace_if_unacked=True)


class SessionInsightAgent(BackgroundAgent):
    """Periodic analysis of session activity to surface coaching tips."""

    def __init__(self, session_mgr, memory, notifications, enabled=True, interval=600,
                 min_tool_calls=10, **kwargs):
        super().__init__("SessionInsight", interval, notifications, enabled, **kwargs)
        self.session_mgr = session_mgr
        self.memory = memory
        self.min_tool_calls = min_tool_calls
        self._last_insight_hash = None
        self._last_tool_count = 0
        self._last_signal_hash = None

    def check(self):
        session = self.session_mgr.current_session
        if not session:
            return
        tool_calls = session.get("tool_calls", [])
        if len(tool_calls) < self.min_tool_calls:
            return
        # Only re-analyze when tool call count has grown meaningfully
        if len(tool_calls) - self._last_tool_count < 5:
            return
        self._last_tool_count = len(tool_calls)
        signal_summary = self._build_signal_summary(session, tool_calls)
        signal_hash = hashlib.md5(signal_summary.encode("utf-8")).hexdigest()
        if signal_hash == self._last_signal_hash:
            return
        self._last_signal_hash = signal_hash

        # Try AI insight, fall back to heuristic
        if self.ai_available:
            insight = self._ai_insight(signal_summary)
            if insight:
                self._emit_insight(insight, ai_enhanced=True)
                return

        # Heuristic mode — emit all applicable insights (not just first)
        insights = self._heuristic_insights(session, tool_calls)
        if insights:
            self._emit_insight("; ".join(insights))

    def _heuristic_insights(self, session, tool_calls):
        """Rule-based coaching tips."""
        insights = []
        tool_names = [tc.get("tool", "") for tc in tool_calls]
        tool_counts = Counter(tool_names)
        budget = session.get("context_budget", {})
        top_consumers = budget.get("top_consumers", [])
        top_tool = top_consumers[0]["tool"] if top_consumers else ""
        top_tokens = top_consumers[0]["tokens"] if top_consumers else 0

        # Detect repeated search queries (>=3 same query)
        search_queries = [tc.get("args", {}).get("query", "") for tc in tool_calls
                          if tc.get("tool") in ("c3_search", "c3_recall")]
        query_counts = Counter(q for q in search_queries if q)
        repeated = [q for q, c in query_counts.items() if c >= 3]
        if repeated:
            insights.append(f"Query '{repeated[0]}' used {query_counts[repeated[0]]}x — consider adding to CLAUDE.md")

        # Many tool calls with 0 c3_remember calls
        if len(tool_calls) > 20 and tool_counts.get("c3_remember", 0) == 0:
            insights.append("Many tool calls but no facts saved — use c3_remember to preserve key discoveries")

        # No decisions logged
        decisions = session.get("decisions", [])
        if len(tool_calls) > 15 and len(decisions) == 0:
            insights.append("No decisions logged this session — use c3_session_log to preserve reasoning")

        # Real token hotspot from session budget should override raw call-count intuition.
        if top_tool in ("Read", "read", "view_file") and top_tokens >= 800:
            insights.append(
                f"File reads are the top token consumer ({top_tokens} tok) — switch to c3_compress(mode='map') before more broad reads"
            )
        elif top_tool == "c3_search" and top_tokens >= 800:
            insights.append(
                f"c3_search is the top token consumer ({top_tokens} tok) — tighten top_k/max_tokens or stabilize findings with c3_remember/c3_session_log"
            )
        elif top_tool in ("Bash", "run_command") and top_tokens >= 600:
            insights.append(
                f"Terminal output is the top token consumer ({top_tokens} tok) — route noisy output through c3_filter before analysis"
            )

        # Detect c3_read thrashing on same file without prior c3_compress
        c3_read_files = [tc.get("args", {}).get("file_path", "").split(",")[0]
                         for tc in tool_calls if tc.get("tool") == "c3_read"]
        c3_read_file_counts = Counter(f for f in c3_read_files if f)
        compress_files = {tc.get("args", {}).get("file_path", "")
                          for tc in tool_calls if tc.get("tool") == "c3_compress"}
        for file_path, count in c3_read_file_counts.items():
            if count >= 3 and file_path not in compress_files:
                fname = Path(file_path).name if file_path else "unknown"
                insights.append(
                    f"{count}x c3_read on '{fname}' without c3_compress — "
                    "use c3_compress(mode='map') first to see all symbols and target sections"
                )
                break  # one tip is enough

        # Many reads with 0 compressions
        reads = tool_counts.get("Read", 0) + tool_counts.get("read", 0)
        compressions = tool_counts.get("c3_compress", 0)
        if reads > 5 and compressions == 0:
            insights.append(f"{reads} file reads but no compressions — use c3_compress to save tokens")

        # Heavy c3_search usage without c3_filter
        searches = tool_counts.get("c3_search", 0)
        filters = tool_counts.get("c3_filter", 0) + tool_counts.get("c3_extract", 0)
        if searches > 8 and filters == 0:
            insights.append(f"{searches} searches but no filtering — c3_filter is better for large files")

        # Many compress/review operations but no delegation
        compressions = tool_counts.get("c3_compress", 0)
        delegate_calls = tool_counts.get("c3_delegate", 0)
        heavy_ops = compressions + tool_counts.get("c3_summarize", 0)
        if heavy_ops >= 5 and delegate_calls == 0:
            insights.append(
                f"{heavy_ops} compress/summarize calls but no c3_delegate — "
                "use c3_delegate(task_type='summarize'/'review'/'test') to save Claude tokens"
            )

        # Many file reads with zero delegation — stronger file-read hint
        total_reads = tool_counts.get("Read", 0) + tool_counts.get("read", 0)
        if total_reads > 8 and delegate_calls == 0 and len(tool_calls) > 15:
            insights.append(
                f"{total_reads} file reads and 0 c3_delegate calls — "
                "for large files you only need to understand (not edit), use "
                "c3_delegate(task_type='explain', file_path='...') to offload to local LLM"
            )

        # Bash/run_command calls suggest possible errors worth delegating
        bash_calls = tool_counts.get("Bash", 0) + tool_counts.get("run_command", 0)
        if bash_calls > 3 and delegate_calls == 0 and len(tool_calls) > 10:
            insights.append(
                f"{bash_calls} terminal commands with no c3_delegate — "
                "if any produced errors, use c3_delegate(task_type='diagnose', task='<error>') "
                "to root-cause locally and save Claude tokens"
            )

        # --- Stuck detection → Codex escalation ---
        # Detect repeated edit/validate/bash cycles on the same file (fix attempts)
        recent_n = tool_calls[-20:] if len(tool_calls) >= 20 else tool_calls
        edit_targets = []
        error_count = 0
        for tc in recent_n:
            t = tc.get("tool", "")
            if t in ("c3_edit", "Edit", "edit_file"):
                fp = tc.get("args", {}).get("file_path", "")
                if fp:
                    edit_targets.append(fp)
            if t in ("Bash", "run_command", "c3_validate"):
                summary = tc.get("summary", "").lower()
                if any(w in summary for w in ("error", "fail", "exception", "traceback")):
                    error_count += 1

        edit_file_counts = Counter(edit_targets)
        repeated_edits = {f: c for f, c in edit_file_counts.items() if c >= 3}
        if repeated_edits and error_count >= 2:
            stuck_file = max(repeated_edits, key=repeated_edits.get)
            fname = Path(stuck_file).name
            insights.append(
                f"Possible stuck loop: {repeated_edits[stuck_file]}x edits on '{fname}' "
                f"with {error_count} errors in last 20 calls — "
                "try c3_delegate(task_type='diagnose', backend='codex', "
                f"task='debug repeated failures in {fname}') for a fresh perspective"
            )
        elif error_count >= 3 and not repeated_edits:
            insights.append(
                f"{error_count} errors in recent calls — "
                "consider c3_delegate(task_type='diagnose', backend='codex') "
                "to escalate investigation to Codex for a second opinion"
            )

        return insights

    def _build_signal_summary(self, session, tool_calls) -> str:
        tool_names = [tc.get("tool", "") for tc in tool_calls[-20:]]
        tool_counts = Counter(tool_names)
        decisions = session.get("decisions", [])
        fact_count = len(self.memory.facts)
        budget = session.get("context_budget", {})
        top_consumers = budget.get("top_consumers", [])
        consumers = ", ".join(f"{c['tool']}:{c['tokens']}" for c in top_consumers[:3]) if top_consumers else "none"
        summary = (
            f"Recent tool calls ({len(tool_calls[-20:])} sampled of {len(tool_calls)} total): "
            + ", ".join(f"{t}:{c}" for t, c in tool_counts.most_common(8))
            + f"\nCompression level: {budget.get('compression_level', 0)}"
            + f"\nResponse tokens: {budget.get('response_tokens', 0)}"
            + f"\nTop consumers: {consumers}"
            + f"\nDecisions logged: {len(decisions)}"
            + f"\nFacts in memory: {fact_count}"
        )
        if decisions:
            summary += "\nRecent decisions: " + "; ".join(d.get("data", "")[:50] for d in decisions[-3:])
        return summary

    def _ai_insight(self, signal_summary: str):
        """AI-powered session coaching."""
        tip = self._ai_generate(
            f"Analyze this Claude Code session and give ONE specific coaching tip:\n\n{signal_summary}",
            system="You are a productivity coach for AI coding assistants. "
                   "Give one actionable tip to improve workflow efficiency. Be specific.",
            max_tokens=90,
        )
        return tip.strip() if tip else None

    def _emit_insight(self, insight, ai_enhanced=False):
        """Emit insight notification, deduplicating via hash."""
        h = hashlib.md5(insight.encode()).hexdigest()[:8]
        if h == self._last_insight_hash:
            return
        self._last_insight_hash = h
        self.notify("info", "Session insight", insight, ai_enhanced=ai_enhanced)


class ClaudeMdUpdaterAgent(BackgroundAgent):
    """Automatically maintains CLAUDE.md using local AI, memory, and session data.

    Periodically checks for staleness, gathers promotion candidates from memory,
    analyzes recent sessions for recurring patterns, and drafts targeted updates.
    When AI is available, produces refined section patches; otherwise applies
    safe heuristic updates (session refresh, fact promotion, compaction).

    Updates are written to disk and surfaced via notifications. The agent never
    deletes user-written content — it only appends, refreshes auto-generated
    sections, and compacts when the file exceeds the truncation limit.
    """

    def __init__(self, claude_md, memory, session_mgr, watcher, notifications,
                 enabled=True, interval=900, auto_apply=True, min_facts_for_promote=2,
                 **kwargs):
        super().__init__("ClaudeMdUpdater", interval, notifications, enabled, **kwargs)
        self.claude_md = claude_md
        self.memory = memory
        self.session_mgr = session_mgr
        self.watcher = watcher
        self.auto_apply = auto_apply
        self.min_facts_for_promote = min_facts_for_promote
        self._last_content_hash = ""
        self._last_update_time = 0.0
        self._updates_applied = 0
        self._last_action_hash = ""

    @property
    def truncation_limit(self) -> int:
        """Read line limit from the ClaudeMdManager (IDE-aware)."""
        return getattr(self.claude_md, 'line_limit', 200) or 200

    def check(self):
        # Gather signals
        staleness = self._check_staleness()
        promotions = self._check_promotions()
        needs_compact = self._check_line_count()

        # Nothing to do
        if not staleness and not promotions and not needs_compact:
            return

        # Build an update plan
        actions = []
        if staleness:
            actions.append(("staleness", staleness))
        if promotions:
            actions.append(("promote", promotions))
        if needs_compact:
            actions.append(("compact", needs_compact))

        action_hash = self._action_hash(actions)
        if action_hash == self._last_action_hash:
            return
        self._last_action_hash = action_hash

        if self.ai_available:
            self._ai_update(actions)
        else:
            self._heuristic_update(actions)

    def _check_staleness(self) -> dict | None:
        """Return staleness result if CLAUDE.md is stale, else None."""
        # Only check if files have changed or we haven't checked before
        if self.watcher._handler.change_count == 0 and self._last_content_hash:
            return None
        try:
            result = self.claude_md.check_staleness()
            if result.get("status") == "stale":
                return result
        except Exception:
            pass
        return None

    def _check_promotions(self) -> dict | None:
        """Return promotion candidates if any qualify."""
        try:
            result = self.claude_md.get_promotion_candidates(
                min_relevance=self.min_facts_for_promote
            )
            total = result.get("total_candidates", 0)
            if total > 0:
                return result
        except Exception:
            pass
        return None

    def _check_line_count(self) -> dict | None:
        """Return compact info if CLAUDE.md exceeds truncation limit."""
        try:
            current = self.claude_md._read_current()
            if current and len(current.split("\n")) > self.truncation_limit:
                return {"lines": len(current.split("\n")), "limit": self.truncation_limit}
        except Exception:
            pass
        return None

    def _heuristic_update(self, actions: list):
        """Apply safe heuristic updates without AI."""
        applied = []

        for action_type, data in actions:
            if action_type == "staleness":
                # Regenerate the auto-generated sections
                try:
                    result = self.claude_md.generate(include_sessions=True)
                    if result.get("content") and self.auto_apply:
                        self._write_claude_md(result["content"])
                        applied.append("Regenerated CLAUDE.md (stale)")
                    elif result.get("content"):
                        applied.append(f"CLAUDE.md is stale ({len(data.get('issues', []))} issues) — regeneration available")
                except Exception:
                    pass

            elif action_type == "promote":
                # Append high-relevance facts to CLAUDE.md
                candidates = data.get("candidates", {})
                total = data.get("total_candidates", 0)
                if total > 0 and not self.auto_apply:
                    applied.append(f"{total} facts ready to promote into CLAUDE.md")
                elif total > 0 and self.auto_apply:
                    promoted = self._apply_promotions(candidates)
                    if promoted:
                        applied.append(f"Promoted {promoted} facts into CLAUDE.md")

            elif action_type == "compact":
                lines = data["lines"]
                limit = data["limit"]
                if self.auto_apply:
                    try:
                        result = self.claude_md.compact(target_lines=limit)
                        if result.get("content"):
                            self._write_claude_md(result["content"])
                            saved = result.get("original_lines", 0) - result.get("compacted_lines", 0)
                            applied.append(f"Compacted CLAUDE.md ({saved} lines saved)")
                    except Exception:
                        pass
                else:
                    applied.append(f"CLAUDE.md is {lines} lines (limit {limit}) — compaction available")

        if applied:
            self._updates_applied += len(applied)
            self.notify("info", "CLAUDE.md updated", "; ".join(applied))

    def _ai_update(self, actions: list):
        """AI-enhanced update — uses local LLM to produce targeted patches."""
        # Build context for AI
        current_md = ""
        try:
            current_md = self.claude_md._read_current() or ""
        except Exception:
            pass

        # Gather signals into a compact summary
        signals = []
        for action_type, data in actions:
            if action_type == "staleness":
                issues = data.get("issues", [])
                signals.append(
                    "STALENESS: " + "; ".join(i.get("message", "")[:60] for i in issues[:5])
                )
            elif action_type == "promote":
                candidates = data.get("candidates", {})
                for section, items in candidates.items():
                    for item in items[:3]:
                        signals.append(f"PROMOTE [{section}]: {item['fact'][:80]}")
            elif action_type == "compact":
                signals.append(f"OVER LIMIT: {data['lines']} lines (limit {data['limit']})")

        # Add recent session context
        session = self.session_mgr.current_session
        if session:
            decisions = session.get("decisions", [])
            if decisions:
                signals.append(
                    "RECENT DECISIONS: " + "; ".join(d.get("data", "")[:50] for d in decisions[-3:])
                )

        # Add high-relevance memory facts
        top_facts = sorted(self.memory.facts, key=lambda f: f.get("relevance_count", 0), reverse=True)[:5]
        if top_facts:
            signals.append(
                "TOP FACTS: " + "; ".join(f["fact"][:60] for f in top_facts)
            )

        signals_text = "\n".join(signals)

        # Ask AI for a targeted update plan
        ai_plan = self._ai_generate(
            f"Current CLAUDE.md has {len(current_md.split(chr(10)))} lines.\n\n"
            f"These signals indicate needed updates:\n{signals_text}\n\n"
            "List the specific updates to make as a numbered list. Be concise.\n"
            "Focus on: refreshing stale sections, adding high-value facts, removing duplicates.\n"
            "Do NOT suggest removing user-written content.",
            system="You are a project documentation maintainer. Output a concise numbered update plan.",
            max_tokens=200,
        )

        if ai_plan and self.auto_apply:
            # Apply heuristic updates (AI plan guides notification, but actual
            # changes use the safe ClaudeMdManager methods)
            self._heuristic_update(actions)
            # Enhance the notification with the AI plan
            self.notify("info", "CLAUDE.md update plan",
                        ai_plan.strip(), ai_enhanced=True)
        elif ai_plan:
            self.notify("info", "CLAUDE.md update plan (dry run)",
                        ai_plan.strip(), ai_enhanced=True)
        else:
            # AI failed, fall back
            self._heuristic_update(actions)

    def _action_hash(self, actions: list) -> str:
        signature = []
        for action_type, data in actions:
            if action_type == "staleness":
                issues = [i.get("message", "") for i in data.get("issues", [])[:5]]
                signature.append((action_type, issues))
            elif action_type == "promote":
                signature.append((action_type, data.get("total_candidates", 0)))
            elif action_type == "compact":
                signature.append((action_type, data.get("lines", 0), data.get("limit", 0)))
        return hashlib.md5(json.dumps(signature, sort_keys=True).encode("utf-8")).hexdigest()

    def _apply_promotions(self, candidates: dict) -> int:
        """Append promotion candidates to the appropriate CLAUDE.md sections."""
        try:
            current = self.claude_md._read_current()
            if not current:
                return 0
        except Exception:
            return 0

        additions = 0
        lines = current.split("\n")

        for section_name, items in candidates.items():
            if not items:
                continue

            # Find the section header
            section_idx = None
            for i, line in enumerate(lines):
                if section_name in line and line.strip().startswith("#"):
                    section_idx = i
                    break

            if section_idx is not None:
                # Find end of section (next header or EOF)
                insert_idx = section_idx + 1
                while insert_idx < len(lines) and not lines[insert_idx].strip().startswith("#"):
                    insert_idx += 1

                # Insert before next header
                new_lines = [item["snippet"] for item in items[:3]]
                for nl in reversed(new_lines):
                    lines.insert(insert_idx, nl)
                additions += len(new_lines)

        if additions > 0:
            self._write_claude_md("\n".join(lines))
        return additions

    def _write_claude_md(self, content: str):
        """Write content to the instructions file and update hash."""
        try:
            md_path = self.claude_md.project_path / self.claude_md.instructions_file
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(content, encoding="utf-8")
            self._last_content_hash = hashlib.md5(content.encode()).hexdigest()
            self._last_update_time = time.time()
        except Exception:
            pass

    def get_status(self) -> dict:
        """Extended status including updater-specific metrics."""
        status = super().get_status()
        status.update({
            "auto_apply": self.auto_apply,
            "updates_applied": self._updates_applied,
            "last_update": self._last_update_time,
        })
        return status


class FileMemoryAgent(BackgroundAgent):
    """Maintains persistent structural maps of source files.

    Watches for file changes, re-extracts section maps (classes, functions, line ranges),
    and optionally generates AI summaries. Processes queued files from the Read hook.
    """

    def __init__(self, file_memory, watcher, notifications,
                 enabled=True, interval=120, max_files_per_cycle=5, **kwargs):
        super().__init__("FileMemory", interval, notifications, enabled, **kwargs)
        self.file_memory = file_memory
        self.watcher = watcher
        self.max_files_per_cycle = max_files_per_cycle
        self._last_change_count = 0

    def check(self):
        files_to_process = []

        # 1. Drain the async queue (from Read hook)
        queued = self.file_memory.drain_queue()
        files_to_process.extend(queued)

        # 2. Check watcher for changed files
        change_count = self.watcher._handler.change_count
        if change_count != self._last_change_count:
            self._last_change_count = change_count
            # Check tracked files for staleness
            for rel_path in self.file_memory.list_tracked():
                if self.file_memory.needs_update(rel_path):
                    files_to_process.append(rel_path)

        # Deduplicate
        seen = set()
        unique = []
        for p in files_to_process:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        files_to_process = unique[:self.max_files_per_cycle]

        if not files_to_process:
            return

        updated_count = 0
        completed = []
        failed = []
        for rel_path in files_to_process:
            ai_summary = None

            # Generate AI summary if available
            if self.ai_available:
                record = self.file_memory.get(rel_path)
                section_names = ""
                if record:
                    names = [s.get("name", "") for s in record.get("sections", [])
                             if s.get("type") not in ("import", "decorator")]
                    section_names = ", ".join(names[:10])

                if section_names:
                    ai_summary = self._ai_generate(
                        f"File: {rel_path}\n"
                        f"Symbols: {section_names}\n\n"
                        f"Describe the purpose of this file and its main symbols in 1-2 concise sentences. "
                        f"Focus on what they do, not how they are implemented.",
                        system="You are a senior architect summarizing code for an AI assistant. "
                               "Be technical, concise, and highlight the main responsibilities of the symbols.",
                        max_tokens=100,
                    )

            result = self.file_memory.update(rel_path, ai_summary=ai_summary)
            if result:
                updated_count += 1
                completed.append(rel_path)
            else:
                failed.append(rel_path)

        if completed:
            self.file_memory.complete_updates(completed)
        if failed:
            self.file_memory.complete_updates(failed, failed=True)

        if updated_count > 0:
            self.notify("info", "File maps updated",
                        f"Updated {updated_count} file map(s): {', '.join(files_to_process[:3])}")

    def get_status(self) -> dict:
        status = super().get_status()
        status["tracked_files"] = len(self.file_memory.list_tracked())
        return status


class BranchWatchAgent(BackgroundAgent):
    """Detects branch / HEAD changes and queues a scoped, targeted re-index.

    Change detection keys on the HEAD sha, so it fires on checkout, switch,
    pull, and merge — but NOT on ``git fetch`` (which only moves remote refs;
    the working tree is untouched). On a move it queues exactly the files that
    differ between the old and new HEAD (from ``git diff``), restricted to files
    C3 already tracks and that genuinely need re-indexing, then notifies
    (warning on a branch switch, info on a same-branch HEAD move).

    Every cycle it also queues tracked files that are dirty on disk, catching
    edits made outside C3 (rebase, ``git restore``, another editor) that the
    lazy mtime-on-access path would only notice later. The actual re-extraction
    is done by FileMemoryAgent, which drains the same queue.
    """

    def __init__(self, file_memory, notifications, project_path,
                 enabled=True, interval=30, max_queue=200, **kwargs):
        super().__init__("BranchWatch", interval, notifications, enabled, **kwargs)
        self.file_memory = file_memory
        self.project_path = project_path
        self.max_queue = max_queue
        self._git = GitContext(project_path)
        self._state_path = Path(project_path) / ".c3" / "branch_state.json"
        self._loaded = False
        self._last = {"branch": None, "head_sha": ""}

    def _load_state(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                self._last = {"branch": data.get("branch"),
                              "head_sha": data.get("head_sha", "")}
        except Exception:
            pass

    def _save_state(self, branch, head_sha):
        self._last = {"branch": branch, "head_sha": head_sha}
        try:
            self._state_path.write_text(
                json.dumps({"branch": branch, "head_sha": head_sha}),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _queue_changed(self, paths) -> int:
        """Queue tracked files that still need re-indexing; return count queued.

        Paths are compared slash-agnostically against the file-memory store and
        filtered through ``needs_update`` so unchanged files (e.g. after a local
        commit where the working tree already matches) are not re-processed.
        """
        if not paths:
            return 0
        tracked = {t.replace("\\", "/"): t for t in self.file_memory.list_tracked()}
        queued = 0
        for p in paths:
            canonical = tracked.get(p.replace("\\", "/"))
            if canonical is None:
                continue
            try:
                if not self.file_memory.needs_update(canonical):
                    continue
            except Exception:
                pass
            self.file_memory.queue_for_update(canonical)
            queued += 1
            if queued >= self.max_queue:
                break
        return queued

    def check(self):
        st = self._git.state(force=True)
        if not st.get("available"):
            return False  # no git here — idle backoff
        self._load_state()
        cur_branch = st.get("branch")
        cur_head = st.get("head_sha", "")
        if not cur_head:
            return False
        prev_branch = self._last.get("branch")
        prev_head = self._last.get("head_sha", "")

        # Catch out-of-band working-tree edits every cycle.
        dirty_queued = self._queue_changed(self._git.dirty_files())

        # First run for this project — record the baseline without notifying.
        if not prev_head:
            self._save_state(cur_branch, cur_head)
            return False if dirty_queued == 0 else None

        if cur_head == prev_head and cur_branch == prev_branch:
            return False if dirty_queued == 0 else None

        # HEAD or branch moved — scope the re-index to what actually differs.
        changed = self._git.changed_files(prev_head, cur_head)
        if not changed:
            # Old commit unreachable / diff failed — fall back to dirty set.
            changed = self._git.dirty_files()
        queued = self._queue_changed(changed)

        switched = (cur_branch != prev_branch)
        self._save_state(cur_branch, cur_head)

        new_label = cur_branch or "(detached)"
        if switched:
            old_label = prev_branch or (prev_head[:8] if prev_head else "?")
            self.notify(
                "warning", "Branch changed",
                f"{old_label} → {new_label}; queued {queued} tracked file(s) for re-index",
                replace_if_unacked=True,
            )
        else:
            self.notify(
                "info", "Index refresh",
                f"HEAD {prev_head[:8]}→{cur_head[:8]} on {new_label}; "
                f"queued {queued} file(s) for re-index",
                replace_if_unacked=True,
            )
        return None

    def get_status(self) -> dict:
        status = super().get_status()
        status["branch"] = self._git.label()
        return status


class AutonomyPlannerAgent(BackgroundAgent):
    """Builds a prioritized autonomous action plan from recent tool telemetry."""

    def __init__(self, session_mgr, watcher, notifications, enabled=True, interval=240,
                 lookback_tool_calls=30, cooldown_seconds=600, min_signal_score=2, max_actions=3, **kwargs):
        super().__init__("AutonomyPlanner", interval, notifications, enabled, **kwargs)
        self.session_mgr = session_mgr
        self.watcher = watcher
        self.lookback_tool_calls = max(10, int(lookback_tool_calls))
        self.cooldown_seconds = max(60, int(cooldown_seconds))
        self.min_signal_score = max(1, int(min_signal_score))
        self.max_actions = max(1, int(max_actions))
        self._last_tool_count = 0
        self._last_plan_hash = None
        self._last_plan_time = 0.0

    def check(self):
        session = self.session_mgr.current_session
        if not session:
            return
        tool_calls = session.get("tool_calls", [])
        if len(tool_calls) < 5:
            return
        if len(tool_calls) - self._last_tool_count < 3:
            return
        self._last_tool_count = len(tool_calls)

        recent = tool_calls[-self.lookback_tool_calls:]
        actions = self._build_actions(session, recent)
        if not actions:
            return

        actions.sort(key=lambda a: a["score"], reverse=True)
        selected = actions[:self.max_actions]
        if selected[0]["score"] < self.min_signal_score:
            return

        message = self._format_plan(selected, len(recent))
        now = time.time()
        plan_hash = hashlib.md5(message.encode("utf-8")).hexdigest()[:12]
        if self._last_plan_hash == plan_hash and (now - self._last_plan_time) < self.cooldown_seconds:
            return

        used_ai = False
        if self.ai_available:
            ai_plan = self._ai_refine_plan(selected, len(recent))
            if ai_plan:
                message = ai_plan
                used_ai = True
                plan_hash = hashlib.md5(message.encode("utf-8")).hexdigest()[:12]
                if self._last_plan_hash == plan_hash and (now - self._last_plan_time) < self.cooldown_seconds:
                    return

        severity = "warning" if selected[0]["score"] >= 4 else "info"
        self.notify(severity, "Autonomy plan", message, ai_enhanced=used_ai)
        self._last_plan_hash = plan_hash
        self._last_plan_time = now

    def _build_actions(self, session, tool_calls: list[dict]) -> list[dict]:
        actions = {}

        def add_action(key: str, score: int, text: str):
            existing = actions.get(key)
            if existing and existing["score"] >= score:
                return
            actions[key] = {"score": score, "text": text}

        names = [tc.get("tool", "") for tc in tool_calls]
        counts = Counter(names)
        delegate_calls = counts.get("c3_delegate", 0)
        budget = session.get("context_budget", {})
        top_consumers = budget.get("top_consumers", [])
        top_tool = top_consumers[0]["tool"] if top_consumers else ""
        top_tokens = top_consumers[0]["tokens"] if top_consumers else 0

        # Context pressure should surface first.
        level = self.session_mgr.get_compression_level() if hasattr(self.session_mgr, "get_compression_level") else 0
        if level >= 2:
            add_action(
                "context_critical",
                5,
                "Context is at compression level 2. Run `c3_session(action='snapshot', data='checkpoint')`, then start a fresh session.",
            )
        elif level == 1:
            add_action(
                "context_tight",
                3,
                "Context is elevated (level 1). Prefer `c3_compress`/`c3_search` and keep responses concise to avoid escalation.",
            )

        # Real token hotspots should produce more precise next steps.
        if top_tool in ("Read", "read", "view_file") and top_tokens >= 800:
            add_action(
                "read_hotspot",
                4,
                f"File reads are currently the top token consumer ({top_tokens} tok). Use `c3_compress(mode='map')` or `c3_compress(mode='smart')` before more broad reads.",
            )
        elif top_tool == "c3_search" and top_tokens >= 800:
            add_action(
                "search_hotspot",
                3,
                f"`c3_search` is the top token consumer ({top_tokens} tok). Reduce `top_k`/`max_tokens` and persist stable findings with `c3_remember(...)`.",
            )
        elif top_tool in ("Bash", "run_command") and top_tokens >= 600:
            add_action(
                "terminal_hotspot",
                3,
                f"Terminal output is the top token consumer ({top_tokens} tok). Run noisy output through `c3_filter(text=...)` before more analysis.",
            )

        # Detect large file reads without file maps.
        read_tools = {"Read", "read", "view_file"}
        read_calls = [tc for tc in tool_calls if tc.get("tool", "") in read_tools]
        large_reads = [tc for tc in read_calls if self._extract_read_lines(tc.get("result_summary", "")) >= 200]
        file_map_calls = counts.get("c3_file_map", 0) + counts.get("c3_compress", 0)
        if large_reads and file_map_calls == 0:
            path_hint = self._extract_path_hint(large_reads[-1])
            target = f" for `{path_hint}`" if path_hint else ""
            add_action(
                "file_map",
                4 if len(large_reads) >= 2 else 3,
                f"Large reads detected{target}. Use `c3_compress(file_path='...', mode='map')` before additional reads to target sections.",
            )

        # Detect terminal failures that should be delegated to local diagnosis.
        failed_commands = 0
        for tc in tool_calls:
            if tc.get("tool", "") not in ("Bash", "run_command"):
                continue
            summary = (tc.get("result_summary", "") or "").lower()
            if any(tok in summary for tok in ("error", "err", "fail", "exception", "traceback", "exit code")):
                failed_commands += 1
        if failed_commands > 0 and delegate_calls == 0:
            add_action(
                "diagnose",
                4,
                "Terminal failures detected. Use `c3_delegate(task_type='diagnose', task='<error output>')` for local root-cause analysis.",
            )

        # Detect heavy analysis done in Claude without local delegation.
        heavy_ops = counts.get("c3_compress", 0) + counts.get("c3_summarize", 0)
        if heavy_ops >= 4 and delegate_calls == 0:
            add_action(
                "delegate_heavy",
                3,
                "High summarize/compress volume. Offload with `c3_delegate(task_type='summarize'|'review'|'test')` where possible.",
            )

        # Detect repeated search loops that should be stabilized into memory/decisions.
        queries = [tc.get("args", {}).get("query", "").strip() for tc in tool_calls if tc.get("tool") == "c3_search"]
        query_counts = Counter(q for q in queries if q)
        repeated = [q for q, c in query_counts.items() if c >= 3]
        if repeated:
            top_query = repeated[0]
            if len(top_query) > 48:
                top_query = top_query[:45] + "..."
            add_action(
                "stabilize_loop",
                2,
                f"Repeated search loop on '{top_query}'. Record the result with `c3_session_log(...)` and persist reusable facts with `c3_remember(...)`.",
            )

        # Detect c3_read thrashing on same file without a structural map
        c3_read_calls = [tc for tc in tool_calls if tc.get("tool") == "c3_read"]
        read_file_counts = Counter(
            tc.get("args", {}).get("file_path", "").split(",")[0]
            for tc in c3_read_calls
            if tc.get("args", {}).get("file_path", "")
        )
        compress_files = {tc.get("args", {}).get("file_path", "")
                          for tc in tool_calls if tc.get("tool") == "c3_compress"}
        thrashing = [(f, c) for f, c in read_file_counts.items() if c >= 3 and f not in compress_files]
        if thrashing:
            worst = max(thrashing, key=lambda x: x[1])
            fname = Path(worst[0]).name if worst[0] else "file"
            add_action(
                "read_thrash",
                4,
                f"`c3_read` called {worst[1]}x on '{fname}' without a structural map. "
                f"Run `c3_compress(file_path='{worst[0]}', mode='map')` first to locate all symbols, "
                "then target exact sections — or delegate with `c3_delegate(task_type='investigate')`.",
            )

        # Detect high tool-call volume with no compress/plan — loop risk
        if len(tool_calls) > 30 and file_map_calls == 0:
            add_action(
                "loop_risk",
                3,
                f"High tool call volume ({len(tool_calls)} calls) with no structural maps. "
                "Stop, run `c3_compress(mode='map')` on key files, then use `c3_session(action='plan')` to reset approach.",
            )

        # Detect stale index pressure for search-heavy loops.
        change_count = getattr(getattr(self.watcher, "_handler", None), "change_count", 0)
        if change_count >= 10:
            add_action(
                "index_stale",
                2,
                "Index likely stale from file churn. Run `c3_status(view='optimize')` or rebuild index before more broad searches.",
            )
        elif change_count >= 5 and counts.get("c3_search", 0) >= 2:
            add_action(
                "index_stale",
                2,
                "Recent file changes plus active search detected. Consider `c3_status(view='optimize')` to refresh retrieval quality.",
            )

        return list(actions.values())

    def _extract_read_lines(self, summary: str) -> int:
        m = re.search(r"(\d+)\s*(?:L|lines?)", summary or "", flags=re.IGNORECASE)
        if not m:
            return 0
        try:
            return int(m.group(1))
        except Exception:
            return 0

    def _extract_path_hint(self, tool_call: dict) -> str:
        args = tool_call.get("args", {}) or {}
        raw = args.get("file_path") or args.get("AbsolutePath") or args.get("path") or ""
        if not raw:
            return ""
        return Path(str(raw)).name

    def _format_plan(self, actions: list[dict], sample_size: int) -> str:
        lines = [f"Autonomy plan ({sample_size} recent calls):"]
        for i, action in enumerate(actions, start=1):
            lines.append(f"{i}. {action['text']}")
        return "\n".join(lines)

    def _ai_refine_plan(self, actions: list[dict], sample_size: int) -> str | None:
        actions_text = "\n".join(f"- ({a['score']}) {a['text']}" for a in actions)
        refined = self._ai_generate(
            f"Rewrite this autonomous next-step plan to be concise and prioritized.\n"
            f"Keep command snippets exactly as written.\n"
            f"Use up to {len(actions)} numbered items.\n\n"
            f"Scope: {sample_size} recent tool calls\n"
            f"Draft actions:\n{actions_text}",
            system="You are an operations planner for a local AI coding workflow. Be precise, direct, and compact.",
            max_tokens=220,
        )
        if not refined:
            return None
        text = refined.strip()
        return text if len(text) >= 20 else None


class DelegateCoachAgent(BackgroundAgent):
    """Watches activity log for missed local AI delegation opportunities and emits actionable coaching."""

    def __init__(self, session_mgr, notifications, enabled=True, interval=180, lookback_lines=200, **kwargs):
        super().__init__("DelegateCoach", interval, notifications, enabled, **kwargs)
        self.session_mgr = session_mgr
        self.lookback_lines = lookback_lines
        self._last_checked_tool_count = 0

    def check(self):
        session = self.session_mgr.current_session
        if not session:
            return

        tool_calls = session.get("tool_calls", [])
        if len(tool_calls) <= self._last_checked_tool_count:
            return

        new_calls = tool_calls[self._last_checked_tool_count:]
        self._last_checked_tool_count = len(tool_calls)

        # Look for heavy operations that should have been delegated
        for tc in new_calls:
            tool = tc.get("tool", "")
            args = tc.get("args", {})
            summary = tc.get("result_summary", "")

            # Detected a large file read without delegation
            if tool in ("Read", "read", "view_file"):
                try:
                    # Parse lines from summary if possible (e.g. "850L" or "850 lines")
                    lines = 0
                    if "L" in summary:
                        lines = int(summary.split("L")[0].split()[-1])
                    if lines > self.lookback_lines:
                        path_str = args.get("file_path", args.get("AbsolutePath", ""))
                        if path_str:
                            file_name = Path(path_str).name
                            self.notify(
                                "info", "Delegate opportunity",
                                f"You read {lines} lines from {file_name}. Next time, use `c3_delegate(task_type='explain', file_path='...')` to save Claude tokens."
                            )
                            return  # one tip per cycle is enough
                except Exception:
                    pass

            # Detected an error output from Bash/Run Command
            if tool in ("Bash", "run_command"):
                # We can't see the full output here, but we can check if it failed
                if "err" in summary.lower() or "fail" in summary.lower() or "exit code" in summary.lower():
                    self.notify(
                        "info", "Delegate opportunity",
                        "Command failed. Use `c3_delegate(task_type='diagnose', task='<error output>')` to have local AI root-cause the issue."
                    )
                    return

            # Heavy compression usage
            if tool == "c3_compress" and len(new_calls) > 3:
                # Count recent compressions
                recent_comps = sum(1 for c in new_calls if c.get("tool") == "c3_compress")
                if recent_comps >= 3:
                    self.notify(
                        "info", "Delegate opportunity",
                        "Multiple files compressed. If you need a summary of them, use `c3_delegate(task_type='summarize')` instead of doing it yourself."
                    )
                    return

        # Detect c3_read thrashing — many symbol reads on same file without a structural map
        c3_reads = [tc for tc in new_calls if tc.get("tool") == "c3_read"]
        if len(c3_reads) >= 3:
            read_file_counts = Counter(
                tc.get("args", {}).get("file_path", "").split(",")[0]
                for tc in c3_reads
                if tc.get("args", {}).get("file_path", "")
            )
            compress_files = {tc.get("args", {}).get("file_path", "")
                              for tc in new_calls if tc.get("tool") == "c3_compress"}
            for file_path, count in read_file_counts.items():
                if count >= 3 and file_path not in compress_files:
                    fname = Path(file_path).name if file_path else "file"
                    self.notify(
                        "warning", "Read loop detected",
                        f"`c3_read` called {count}x on '{fname}' — stop and use "
                        f"`c3_compress(file_path='{file_path}', mode='map')` to see all symbols at once, "
                        "or delegate with `c3_delegate(task_type='investigate')`."
                    )
                    return


class KeyFileVersionAgent(BackgroundAgent):
    """Tracks key file versions and warns when agent-facing files drift."""

    def __init__(self, version_tracker, notifications, ide_name: str = "claude-code",
                 enabled=True, interval=180, max_changes_per_notice: int = 4, agent_target: str = "current", **kwargs):
        super().__init__("KeyFileVersion", interval, notifications, enabled, **kwargs)
        self.version_tracker = version_tracker
        self.ide_name = ide_name
        self.max_changes_per_notice = max_changes_per_notice
        self.agent_target = agent_target
        self._primed = False

    def check(self):
        if not self.version_tracker:
            return
        result = self.version_tracker.scan(agent=self.agent_target)
        changed = result.get("changed", [])
        if not self._primed:
            self._primed = True
            return
        if not changed:
            return

        sample = changed[:self.max_changes_per_notice]
        files = ", ".join(item["file"] for item in sample)
        if len(changed) > len(sample):
            files += f" (+{len(changed) - len(sample)} more)"
        dirty = sum(1 for item in changed if (item.get("git", {}) or {}).get("dirty"))
        severity = "warning" if dirty else "info"
        target = self.ide_name if self.agent_target in ("", "current", None) else self.agent_target
        self.notify(
            severity,
            "Key file versions changed",
            f"{files}. Tailored target: {target}. Git dirty: {dirty}.",
        )


class EditLedgerEnricherAgent(BackgroundAgent):
    """Asynchronously enriches edit ledger entries with git info and syntax validation.

    Runs on a short interval after the hook logs entries with git_pending=True.
    Appends patch entries to the ledger (append-only; readers merge on the fly).
    Also validates recently edited files and notifies on syntax errors.
    Optionally runs a Codex verification pass on enriched diffs.
    """

    def __init__(self, edit_ledger, validation_cache, notifications,
                 delegate_config=None, project_path=None,
                 enabled=True, interval=30, **kwargs):
        super().__init__("EditLedgerEnricher", interval, notifications, enabled, **kwargs)
        self.edit_ledger = edit_ledger
        self.validation_cache = validation_cache
        self.delegate_config = delegate_config or {}
        self.project_path = project_path
        self._verified_ids: set = set()  # track already-verified entries

    def check(self):
        # Git enrichment — processes entries marked git_pending=True
        enriched_count = self.edit_ledger.enrich_pending(batch=10)

        # Syntax validation — validates recently edited files
        validated = self.edit_ledger.validate_pending(
            batch=5, validation_cache=self.validation_cache
        )

        failures = [v for v in validated if not v.get("valid", True)]
        if failures:
            files = ", ".join(v["file"] for v in failures[:3])
            extra = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
            self.notify(
                "warning",
                "Syntax errors in edited files",
                f"{len(failures)} file(s) have syntax errors: {files}{extra}",
                replace_if_unacked=True,
            )

        # Codex verification — optional, runs on enriched entries with diffs
        if enriched_count and self.delegate_config.get("codex_enabled") and \
                self.delegate_config.get("codex_verify_edits", False):
            self._codex_verify_recent()

        if not enriched_count and not validated:
            return False

    def _codex_verify_recent(self):
        """Run a Codex read-only review on recently enriched diffs."""
        try:
            from cli.tools.delegate import _codex_available, _run_codex, check_codex
            if _codex_available is None:
                check_codex()
            from cli.tools.delegate import _codex_available as avail
            if not avail:
                return

            # Gather recent enriched entries with diffs
            recent = self.edit_ledger.get_history(limit=10)
            diffs = []
            for entry in recent:
                eid = entry.get("id", "")
                if eid in self._verified_ids:
                    continue
                diff_summary = entry.get("diff_summary", "")
                if diff_summary and len(diff_summary) > 20:
                    diffs.append((eid, entry.get("file", "unknown"), diff_summary))

            if not diffs:
                return

            # Batch up to 3 diffs into one Codex call
            batch = diffs[:3]
            combined = "\n".join(
                f"--- {f} ---\n{d}" for _, f, d in batch
            )
            task = "Review these recent edits for regressions, bugs, or issues. Be concise — only flag real problems."
            model = self.delegate_config.get("codex_default_model", "gpt-5.3-codex-spark")
            timeout = int(self.delegate_config.get("codex_timeout", 120))

            output, ok = _run_codex(
                task=task, context=combined,
                model=model, sandbox="read-only",
                reasoning="medium", timeout=timeout,
                cwd=str(self.project_path) if self.project_path else None,
            )

            for eid, _, _ in batch:
                self._verified_ids.add(eid)
            # Cap memory
            if len(self._verified_ids) > 200:
                self._verified_ids = set(list(self._verified_ids)[-100:])

            if ok and output and not output.startswith("[codex:"):
                # Only notify if Codex found actual issues (not just "looks good")
                lower = output.lower()
                benign = ("no issues", "looks good", "no problems", "no regressions", "lgtm", "all good")
                if not any(b in lower for b in benign):
                    files = ", ".join(f for _, f, _ in batch)
                    self.notify(
                        "info",
                        "Codex edit verification",
                        f"Codex flagged potential issues in: {files}\n\n{output[:500]}",
                        replace_if_unacked=True,
                    )
        except Exception:
            pass  # non-critical — never break the enrichment loop


def create_agents(services, notifications, config=None, ollama=None) -> list:
    """Factory to instantiate all background agents with service references.

    config: optional dict from .c3/config.json "agents" key, e.g.:
        {"IndexStaleness": {"enabled": true, "interval": 90}, "MemoryPruner": {"enabled": false}}
    ollama: optional OllamaClient instance for AI-enhanced agent behavior.
    """
    config = config or {}

    def _cfg(name, defaults):
        overrides = config.get(name, {})
        merged = {**defaults, **overrides}
        # Inject ollama into all agents
        merged["ollama"] = ollama
        return merged

    agents = [
        IndexStalenessAgent(
            watcher=services.watcher,
            indexer=services.indexer,
            notifications=notifications,
            **_cfg("IndexStaleness", {
                "enabled": True, "interval": 60, "use_ai": False,
                "ai_model": "gemma3n:latest", "warn_threshold": 5, "rebuild_threshold": 15,
            }),
        ),
        MemoryPrunerAgent(
            memory=services.memory,
            notifications=notifications,
            **_cfg("MemoryPruner", {
                "enabled": False, "interval": 300, "use_ai": True,
                "ai_model": "gemma3n:latest", "embed_model": "nomic-embed-text",
                "similarity_threshold": 0.8,
            }),
        ),
        ClaudeMdDriftAgent(
            watcher=services.watcher,
            claude_md=services.claude_md,
            notifications=notifications,
            **_cfg("ClaudeMdDrift", {
                "enabled": False, "interval": 120, "use_ai": False, "ai_model": "gemma3n:latest",
            }),
        ),
        SessionInsightAgent(
            session_mgr=services.session_mgr,
            memory=services.memory,
            notifications=notifications,
            **_cfg("SessionInsight", {
                "enabled": False, "interval": 600, "use_ai": True,
                "ai_model": "gemma3n:latest", "min_tool_calls": 10,
            }),
        ),
        AutonomyPlannerAgent(
            session_mgr=services.session_mgr,
            watcher=services.watcher,
            notifications=notifications,
            **_cfg("AutonomyPlanner", {
                "enabled": False, "interval": 240, "use_ai": True,
                "ai_model": "gemma3n:latest", "lookback_tool_calls": 30,
                "cooldown_seconds": 600, "min_signal_score": 2, "max_actions": 3,
            }),
        ),
        ClaudeMdUpdaterAgent(
            claude_md=services.claude_md,
            memory=services.memory,
            session_mgr=services.session_mgr,
            watcher=services.watcher,
            notifications=notifications,
            **_cfg("ClaudeMdUpdater", {
                "enabled": False, "interval": 900, "use_ai": True,
                "ai_model": "gemma3n:latest", "auto_apply": True,
                "min_facts_for_promote": 2,
            }),
        ),
        DelegateCoachAgent(
            session_mgr=services.session_mgr,
            notifications=notifications,
            **_cfg("DelegateCoach", {
                "enabled": False, "interval": 180, "use_ai": False,
            }),
        ),
        KeyFileVersionAgent(
            version_tracker=getattr(services, "version_tracker", None),
            notifications=notifications,
            ide_name=getattr(services, "ide_name", "claude-code"),
            **_cfg("KeyFileVersion", {
                "enabled": False, "interval": 180, "use_ai": False,
                "agent_target": "current", "max_changes_per_notice": 4,
            }),
        ),
    ]

    # FileMemoryAgent — only if file_memory is available on services
    if hasattr(services, 'file_memory') and services.file_memory:
        agents.append(
            FileMemoryAgent(
                file_memory=services.file_memory,
                watcher=services.watcher,
                notifications=notifications,
                **_cfg("FileMemory", {
                    "enabled": True, "interval": 120, "use_ai": False,
                    "ai_model": "gemma3n:latest", "max_files_per_cycle": 5,
                }),
            )
        )

    # BranchWatchAgent — needs file_memory's re-index queue.
    if hasattr(services, 'file_memory') and services.file_memory:
        agents.append(
            BranchWatchAgent(
                file_memory=services.file_memory,
                notifications=notifications,
                project_path=getattr(services, 'project_path', None),
                **_cfg("BranchWatch", {
                    "enabled": True, "interval": 30, "max_queue": 200,
                }),
            )
        )

    # EditLedgerEnricherAgent — only if edit_ledger is available
    if getattr(services, 'edit_ledger', None):
        agents.append(
            EditLedgerEnricherAgent(
                edit_ledger=services.edit_ledger,
                validation_cache=getattr(services, 'validation_cache', None),
                delegate_config=getattr(services, 'delegate_config', None),
                project_path=getattr(services, 'project_path', None),
                notifications=notifications,
                **_cfg("EditLedgerEnricher", {
                    "enabled": True, "interval": 10, "use_ai": False,
                }),
            )
        )

    return agents
