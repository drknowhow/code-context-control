"""SessionPreloader — First-prompt auto-retrieval for Local RAG Pipeline.

On the first c3_memory(action='recall') in a session, this module automatically
retrieves relevant doc chunks, code context, and session history, then merges
them into a pre-context block injected before the normal recall results.

This eliminates repeated discovery work across sessions for the same topics.
"""

import logging
import re
from typing import Optional

from core import count_tokens

log = logging.getLogger(__name__)

# Budget cap for pre-context injection
_DEFAULT_MAX_PRECONTEXT_TOKENS = 400

# Minimum score threshold for doc chunks to be included
_MIN_DOC_SCORE = 0.05


class SessionPreloader:
    """Auto-retrieves relevant project context on first recall of a session."""

    def __init__(self, doc_index, embedding_index=None, session_mgr=None,
                 memory_store=None, config: Optional[dict] = None):
        self.doc_index = doc_index
        self.embedding_index = embedding_index
        self.session_mgr = session_mgr
        self.memory_store = memory_store
        self._config = config or {}
        self._preloaded_sessions: set = set()  # session IDs that already got preloaded

    @property
    def max_tokens(self) -> int:
        return self._config.get("max_precontext_tokens", _DEFAULT_MAX_PRECONTEXT_TOKENS)

    @property
    def enabled(self) -> bool:
        return self._config.get("enabled", True)

    def should_preload(self, session_id: str) -> bool:
        """Check if this session hasn't been preloaded yet."""
        if not self.enabled:
            return False
        if not self.doc_index:
            return False
        if not self.doc_index.chunks:
            return False
        return session_id not in self._preloaded_sessions

    def preload(self, query: str, session_id: str, top_k: int = 5) -> str:
        """Generate pre-context for the first recall in a session.

        Returns a formatted string to prepend to the recall results,
        or empty string if nothing relevant found.
        """
        if not self.should_preload(session_id):
            return ""

        self._preloaded_sessions.add(session_id)

        # Extract expanded signals from the query
        signals = self._extract_signals(query)
        # Skip preloading for simple/short queries — not worth 700+ token injection
        if len(signals) < 3:
            return ""

        signal_query = " ".join(signals)

        # Retrieve from doc index
        doc_results = self.doc_index.search(signal_query, top_k=top_k * 2)

        # Also try embedding-based search if available
        if self.embedding_index and self.embedding_index.ready:
            try:
                embed_results = self.embedding_index.search(query, top_k=3)
                # Convert to comparable format (embed results have different shape)
                for er in embed_results:
                    if er.get("content") and er.get("score", 0) > 0.3:
                        doc_results.append({
                            "id": er.get("chunk_id", er.get("id", "")),
                            "doc_id": er.get("doc_id", ""),
                            "content": er["content"],
                            "tokens": count_tokens(er["content"]),
                            "kind": "code",
                            "source_type": "code_semantic",
                            "priority": 1.0,
                            "score": er["score"] * 0.8,  # slightly discount code vs docs
                            "heading_path": [er.get("doc_id", "")],
                        })
            except Exception:
                pass

        # Get recent session context as weak signal
        session_context = self._get_session_signals()

        # Rank, deduplicate, and budget-cap
        precontext = self._build_precontext(doc_results, session_context)

        if not precontext:
            return ""

        return precontext

    def _extract_signals(self, query: str) -> list[str]:
        """Extract retrieval signals from the user's query."""
        signals = []

        # Direct tokens from query
        tokens = re.findall(r"\w+", query.lower())
        # Filter out very short/common words
        stopwords = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                      "for", "to", "of", "in", "on", "at", "by", "with", "from",
                      "and", "or", "not", "this", "that", "it", "as", "do", "does",
                      "has", "have", "had", "can", "could", "will", "would", "should",
                      "may", "might", "about", "what", "how", "when", "where", "which",
                      "who", "all", "any", "some", "no", "my", "your", "our", "their"}
        signals.extend(t for t in tokens if t not in stopwords and len(t) > 1)

        # Extract file paths mentioned in query
        file_patterns = re.findall(r"[\w/\\]+\.[\w]+", query)
        for fp in file_patterns:
            # Add stem words from file path
            parts = re.split(r"[/\\_.\-]", fp)
            signals.extend(p.lower() for p in parts if len(p) > 1)

        return list(dict.fromkeys(signals))  # deduplicate preserving order

    def _get_session_signals(self) -> str:
        """Get compressed context from recent sessions as weak signals."""
        if not self.session_mgr:
            return ""

        try:
            return self.session_mgr.get_session_context(n_sessions=2)
        except Exception:
            return ""

    def _build_precontext(self, doc_results: list, session_context: str) -> str:
        """Build the final pre-context string within token budget."""
        if not doc_results and not session_context:
            return ""

        budget = self.max_tokens
        parts = []
        used_tokens = 0
        seen_docs = set()

        # Header
        header = "[session:pre-context] Auto-retrieved project context"
        used_tokens += count_tokens(header) + 2  # +2 for newlines

        # Sort by score descending
        doc_results.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Filter low-score results
        doc_results = [r for r in doc_results if r.get("score", 0) >= _MIN_DOC_SCORE]

        # Group by source file for cleaner output
        for result in doc_results:
            doc_id = result.get("doc_id", "")
            content = result.get("content", "").strip()
            if not content:
                continue

            # Deduplicate by doc_id + heading
            dedup_key = f"{doc_id}::{result.get('id', '')}"
            if dedup_key in seen_docs:
                continue
            seen_docs.add(dedup_key)

            # Format chunk
            source_label = self._source_label(result)
            chunk_text = f"\n## {source_label}\n{content}"
            chunk_tokens = count_tokens(chunk_text)

            if used_tokens + chunk_tokens > budget:
                # Try to fit a truncated version
                remaining = budget - used_tokens - 20  # margin
                if remaining > 50:
                    lines = content.split("\n")
                    truncated = []
                    t = 0
                    for line in lines:
                        lt = count_tokens(line)
                        if t + lt > remaining:
                            break
                        truncated.append(line)
                        t += lt
                    if truncated:
                        chunk_text = f"\n## {source_label}\n" + "\n".join(truncated) + "\n..."
                        chunk_tokens = count_tokens(chunk_text)
                        parts.append(chunk_text)
                        used_tokens += chunk_tokens
                break  # budget exhausted
            else:
                parts.append(chunk_text)
                used_tokens += chunk_tokens

        # Add session context if budget remains
        if session_context and used_tokens < budget - 100:
            remaining = budget - used_tokens - 10
            sc_tokens = count_tokens(session_context)
            if sc_tokens > remaining:
                # Truncate session context
                lines = session_context.split("\n")
                truncated = []
                t = 0
                for line in lines:
                    lt = count_tokens(line)
                    if t + lt > remaining:
                        break
                    truncated.append(line)
                    t += lt
                session_context = "\n".join(truncated)

            if session_context.strip():
                parts.append(f"\n## Recent Session Context\n{session_context.strip()}")

        if not parts:
            return ""

        chunk_count = len([p for p in parts if p.startswith("\n##")])
        total_tokens = sum(count_tokens(p) for p in parts)
        header = f"[session:pre-context] Auto-retrieved project context ({chunk_count} chunks, {total_tokens} tokens)"

        return header + "\n" + "\n".join(parts) + "\n\n---\n"

    def _source_label(self, result: dict) -> str:
        """Generate a human-readable source label for a chunk."""
        doc_id = result.get("doc_id", "unknown")
        source_type = result.get("source_type", "")
        heading_path = result.get("heading_path", [])

        if source_type == "markdown":
            if len(heading_path) > 1:
                return f"{heading_path[-1]} (from {doc_id})"
            return f"From {doc_id}"
        elif source_type == "docstring":
            name = result.get("id", "").split("::")[-1] if "::" in result.get("id", "") else doc_id
            return f"Docstring: {name} (from {doc_id})"
        elif source_type == "config":
            return f"Config: {doc_id}"
        elif source_type == "code_semantic":
            return f"Related code: {doc_id}"
        else:
            return f"From {doc_id}"
