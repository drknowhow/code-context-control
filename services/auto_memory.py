"""Auto-memory: rule-based learning from tool calls and session activity.

Runs in the background after every tool call. Extracts high-signal facts
from tool results, deduplicates against existing memory, and consolidates
stale/duplicate facts on session end.  No LLM calls — pure rule-based.

Wired into mcp_server._finalize_response() and lifespan shutdown.
"""

import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

_PRIVATE_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)


def _strip_private(text: str) -> str:
    """Remove <private>...</private> blocks before storing or queuing."""
    return _PRIVATE_RE.sub("", text).strip()


class AutoMemory:
    """Automatically extracts and consolidates facts from C3 tool activity."""

    def __init__(self, memory_store, session_mgr, config: dict | None = None):
        self.memory = memory_store
        self.session_mgr = session_mgr
        self._config = config or {}
        self._queue: list = []
        self._lock = threading.Lock()
        self._worker_running = False
        # Dedup cache: avoid re-extracting the same fact within a session.
        self._recent: set = set()
        self._max_recent = 200

    # ── Public API ──────────────────────────────────────────────────

    def on_tool_complete(
        self, tool_name: str, args: dict, summary: str, result_text: str
    ):
        """Queue extraction after a tool call (non-blocking)."""
        if not self._config.get("enabled", True):
            return
        # Only process tools that have extraction rules.
        if tool_name not in _EXTRACTORS:
            return
        with self._lock:
            self._queue.append((tool_name, args, summary, _strip_private(result_text)[:8000]))
        self._ensure_worker()

    def on_session_end(self):
        """Called synchronously on session save / snapshot / shutdown."""
        if not self._config.get("enabled", True):
            return
        self._drain_queue()
        self._generate_session_summary()

    def consolidate(self) -> dict:
        """Merge duplicate facts and archive stale auto-facts.  Returns stats."""
        facts = getattr(self.memory, "facts", None) or []
        if not facts:
            return {"merged": 0, "archived": 0, "total": 0}

        merged = 0
        archived = 0
        to_delete: set = set()

        active = [f for f in facts if f.get("lifecycle", "active") == "active"]

        # ── Merge duplicates (Jaccard > 0.55) ──
        for i, a in enumerate(active):
            if a["id"] in to_delete:
                continue
            for b in active[i + 1 :]:
                if b["id"] in to_delete:
                    continue
                sim = _jaccard(a["fact"], b["fact"])
                if sim > 0.55:
                    keeper, victim = (
                        (a, b)
                        if a.get("relevance_count", 0) >= b.get("relevance_count", 0)
                        else (b, a)
                    )
                    if sim < 0.85:
                        merged_text = _merge_texts(keeper["fact"], victim["fact"])
                        try:
                            self.memory.update_fact(
                                keeper["id"],
                                merged_text,
                                keeper.get("category", "general"),
                            )
                        except Exception:
                            pass
                    to_delete.add(victim["id"])
                    merged += 1

        # ── Archive stale auto-facts (unused for ≥ 7 days) ──
        now = datetime.now(timezone.utc)
        for f in active:
            if f["id"] in to_delete:
                continue
            cat = f.get("category", "")
            if not cat.startswith("auto:"):
                continue
            if f.get("relevance_count", 0) > 0:
                continue
            try:
                age = (now - datetime.fromisoformat(f.get("timestamp", ""))).days
            except (ValueError, TypeError):
                age = 0
            if age >= 7:
                to_delete.add(f["id"])
                archived += 1

        # ── Rolling window: keep only last _MAX_SESSION_FACTS auto:session entries ──
        _MAX_SESSION_FACTS = 5
        session_facts = sorted(
            [f for f in active if f.get("category") == "auto:session" and f["id"] not in to_delete],
            key=lambda f: f.get("timestamp", ""),
            reverse=True,
        )
        for f in session_facts[_MAX_SESSION_FACTS:]:
            to_delete.add(f["id"])
            archived += 1

        # ── Archive verbose orphans: >600 chars, 0 recall, 14+ days old ──
        for f in active:
            if f["id"] in to_delete:
                continue
            if len(f.get("fact", "")) <= 600:
                continue
            if f.get("relevance_count", 0) > 0:
                continue
            try:
                age = (now - datetime.fromisoformat(f.get("timestamp", ""))).days
            except (ValueError, TypeError):
                age = 0
            if age >= 14:
                to_delete.add(f["id"])
                archived += 1

        for fid in to_delete:
            try:
                self.memory.delete_fact(fid)
            except Exception:
                pass

        return {
            "merged": merged,
            "archived": archived,
            "total": len(facts) - len(to_delete),
        }

    # ── Background worker ───────────────────────────────────────────

    def _ensure_worker(self):
        if self._worker_running:
            return
        self._worker_running = True
        t = threading.Thread(target=self._worker, daemon=True, name="c3-auto-memory")
        t.start()

    def _worker(self):
        try:
            self._drain_queue()
        finally:
            self._worker_running = False

    def _drain_queue(self):
        while True:
            with self._lock:
                if not self._queue:
                    return
                item = self._queue.pop(0)
            try:
                self._process(*item)
            except Exception:
                pass

    # ── Extraction ──────────────────────────────────────────────────

    def _process(
        self, tool_name: str, args: dict, summary: str, result_text: str
    ):
        extractor = _EXTRACTORS.get(tool_name)
        if not extractor:
            return
        for item in extractor(args, summary, result_text):
            # Extractors yield (fact, category, source_paths). The 2-tuple form
            # is still accepted, but it lands as unknown provenance — which
            # mask activation purges wholesale. Prefer the 3-tuple.
            fact_text, category = item[0], item[1]
            source_paths = item[2] if len(item) > 2 else None
            self._save_or_merge(fact_text, category, source_paths)

    def _save_or_merge(self, fact_text: str, category: str, source_paths=None):
        """Save a new fact or merge with the most similar existing one."""
        fact_text = _strip_private(fact_text)
        if len(fact_text) < 25:
            return

        # Session-level dedup.
        key = fact_text[:120].lower()
        if key in self._recent:
            return
        self._recent.add(key)
        if len(self._recent) > self._max_recent:
            self._recent.clear()

        session_id = ""
        if self.session_mgr and self.session_mgr.current_session:
            session_id = self.session_mgr.current_session.get("id", "")

        # Check existing facts for a merge candidate.
        try:
            existing = self.memory.recall(fact_text, top_k=3)
        except Exception:
            existing = []

        for r in existing:
            sim = _jaccard(r.get("fact", ""), fact_text)
            if sim > 0.55:
                if sim < 0.85:
                    merged = _merge_texts(r["fact"], fact_text)
                    try:
                        self.memory.update_fact(
                            r["id"], merged, category or r.get("category", "general")
                        )
                    except Exception:
                        pass
                return  # Already covered by existing fact.

        try:
            self.memory.remember(fact_text, category, session_id,
                                 source_paths=source_paths)
        except Exception:
            pass

    # ── Session summary ─────────────────────────────────────────────

    def _generate_session_summary(self):
        """Build a compact session summary from decisions + file changes."""
        if not self.session_mgr or not self.session_mgr.current_session:
            return

        session = self.session_mgr.current_session
        decisions = session.get("decisions", [])
        files = session.get("files_touched", [])

        if not decisions and not files:
            return

        parts: list[str] = []

        if files:
            names = list(
                dict.fromkeys(f.get("file", "") for f in files if f.get("file"))
            )[:10]
            types = sorted(set(f.get("type", "") for f in files if f.get("type")))
            parts.append(f"Files ({', '.join(types)}): {', '.join(names)}")

        if decisions:
            for d in decisions[-3:]:
                text = d.get("decision", "")
                if text:
                    parts.append(f"Decision: {text}")

        if not parts:
            return

        sid = session.get("id", "unknown")[:8]
        summary = f"Session summary ({sid}): " + " | ".join(parts)
        # The summary names every file it touched, so its provenance is the
        # full touched set — a mask on ANY of them must purge this fact.
        self._save_or_merge(summary, "auto:session",
                            [f.get("file", "") for f in files if f.get("file")])


# ── Extraction functions (pure, no side effects) ───────────────────


def _extract_validate(
    args: dict, summary: str, result: str
) -> List[Tuple[str, str]]:
    """Extract validation failure patterns."""
    learnings: list = []
    fp = args.get("file_path", "")
    if "FAIL" in result or "syntax_error" in summary:
        error_lines = [l.strip() for l in result.splitlines() if l.strip().startswith("- L")][:3]
        if error_lines:
            learnings.append((
                f"[validate] {fp} has syntax errors: {'; '.join(error_lines)}",
                "auto:validate",
                [fp],
            ))
    return learnings


def _extract_search(
    args: dict, summary: str, result: str
) -> List[Tuple[str, str]]:
    """Extract key file discoveries."""
    learnings: list = []
    query = args.get("query", "")
    action = args.get("action", "code")
    if action == "files" and query and len(result) > 50:
        files = re.findall(
            r"(?:^|\s)([\w/\\.-]+\.(?:py|js|ts|tsx|jsx|r|rs|go|java|rb|php|lua|pl))\b",
            result,
            re.IGNORECASE,
        )
        if files:
            unique = list(dict.fromkeys(files))[:8]
            learnings.append((
                f"[search] Key files for '{query}': {', '.join(unique)}",
                "auto:structure",
                unique,
            ))
    return learnings


def _extract_compress(
    args: dict, summary: str, result: str
) -> List[Tuple[str, str]]:
    """Extract top-level symbols from structural maps."""
    learnings: list = []
    fp = args.get("file_path", "")
    mode = args.get("mode", "")
    if fp and mode in ("map", "dense_map") and len(result) > 100:
        symbols = re.findall(
            r"^(?:def |class |function |export |pub fn |func )\s*(\w+)",
            result,
            re.MULTILINE,
        )
        if symbols:
            unique = list(dict.fromkeys(symbols))[:15]
            learnings.append((
                f"[structure] {fp} exports: {', '.join(unique)}",
                "auto:structure",
                [fp],
            ))
    return learnings


def _extract_edit(
    args: dict, summary: str, result: str
) -> List[Tuple[str, str]]:
    """Extract what was edited and why (from c3_edit summary arg)."""
    learnings: list = []
    fp = args.get("file_path", "")
    edit_summary = (args.get("summary") or "").strip()
    if fp and edit_summary and len(edit_summary) >= 20:
        learnings.append((
            f"[edit] {fp}: {edit_summary}",
            "auto:edit",
            [fp],
        ))
    return learnings


def _extract_agent(
    args: dict, summary: str, result: str
) -> List[Tuple[str, str]]:
    """Extract workflow outcomes from c3_agent calls.

    Deliberately yields 2-tuples => unknown provenance. A workflow can read
    anything, so we cannot honestly name its source files; mask activation
    therefore purges these wholesale rather than pretending they are clean.
    """
    learnings: list = []
    workflow = args.get("workflow", "")
    scope = args.get("scope", "")
    if workflow and len(result) > 80:
        # First meaningful non-header line from result
        lines = [
            line.strip()
            for line in result.splitlines()
            if line.strip() and not line.strip().startswith("[")
        ]
        finding = lines[0][:120] if lines else ""
        if finding:
            scope_str = f" (scope: {scope})" if scope else ""
            learnings.append((
                f"[agent:{workflow}]{scope_str}: {finding}",
                "auto:agent",
            ))
    return learnings


_EXTRACTORS: Dict[str, Any] = {
    "c3_validate": _extract_validate,
    "c3_search": _extract_search,
    "c3_compress": _extract_compress,
    "c3_edit": _extract_edit,
    "c3_agent": _extract_agent,
}


# ── Utility functions ──────────────────────────────────────────────


def _jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _merge_texts(existing: str, new: str) -> str:
    """Merge two fact texts, preferring the more complete one."""
    if len(new) < len(existing) * 0.5:
        return existing
    if len(existing) < len(new) * 0.5:
        return new
    # Check how much genuinely new content there is.
    existing_words = set(existing.lower().split())
    new_unique = [w for w in new.lower().split() if w not in existing_words]
    if len(new_unique) < 3:
        return existing
    if len(existing) + len(new) > 500:
        return new  # Newer is more current; avoid bloat.
    return f"{existing} [updated] {new}"
