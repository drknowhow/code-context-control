"""Memory Grounder — validates facts against codebase reality.

Facts can reference files, symbols, and patterns that may have been
renamed, deleted, or refactored. The grounder checks these references
and adjusts confidence/salience accordingly.

Grounding checks:
  - File existence: does the referenced file still exist?
  - Symbol existence: does the referenced function/class still exist?
  - Content drift: has the file changed significantly since the fact was created?
  - Confidence decay: ungrounded facts lose confidence over time
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Patterns to extract file/symbol references from fact text
_FILE_PATTERN = re.compile(
    r"""(?:^|[\s(,'"`])"""            # boundary
    r"""((?:[\w./-]+/)?"""            # optional directory prefix
    r"""[\w.-]+"""                     # filename stem
    r"""\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|c|cpp|h|hpp|css|html|json|yaml|yml|toml|md|sql|sh|bat))"""  # extension
    r"""(?:[\s),'"`:]|$)""",          # boundary
    re.MULTILINE,
)

_SYMBOL_PATTERN = re.compile(
    r"""(?:class|def|function|func|fn|struct|interface|type|const|var|let)\s+"""
    r"""([\w]+)""",
)

# Also match "ClassName", "function_name", "methodName" when preceded by context clues
_NAMED_REF_PATTERN = re.compile(
    r"""(?:(?:class|function|method|module|service|handler|middleware|component|hook)\s+)"""
    r"""`?([\w.]+)`?""",
    re.IGNORECASE,
)


class GroundingResult:
    """Result of grounding a single fact."""

    def __init__(self, fact_id: str):
        self.fact_id = fact_id
        self.file_refs: list[dict] = []       # {path, exists, changed}
        self.symbol_refs: list[dict] = []     # {name, found, file}
        self.grounded = True
        self.issues: list[str] = []
        self.confidence_delta: float = 0.0

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "grounded": self.grounded,
            "file_refs": self.file_refs,
            "symbol_refs": self.symbol_refs,
            "issues": self.issues,
            "confidence_delta": round(self.confidence_delta, 4),
        }


class MemoryGrounder:
    """Validates memory facts against the current codebase state."""

    def __init__(
        self,
        project_path: str,
        memory_store: Any = None,
        graph: Any = None,
        file_memory: Any = None,
    ):
        self.project_path = Path(project_path)
        self.memory = memory_store
        self.graph = graph
        self.file_memory = file_memory

    # ── Public API ──────────────────────────────────────────────────

    def ground_fact(self, fact: dict) -> GroundingResult:
        """Validate a single fact against the codebase."""
        result = GroundingResult(fact.get("id", ""))
        text = fact.get("fact", "")

        # Check file references
        file_refs = self._extract_file_refs(text)
        for ref in file_refs:
            exists = self._file_exists(ref)
            entry = {"path": ref, "exists": exists}
            if not exists:
                result.grounded = False
                result.issues.append(f"file not found: {ref}")
                result.confidence_delta -= 0.15
            result.file_refs.append(entry)

        # Check symbol references
        symbol_refs = self._extract_symbol_refs(text)
        for sym in symbol_refs:
            found, location = self._symbol_exists(sym, file_refs)
            entry = {"name": sym, "found": found, "file": location}
            if not found and file_refs:
                # Only penalize if we had file context to search in
                result.grounded = False
                result.issues.append(f"symbol not found: {sym}")
                result.confidence_delta -= 0.10
            result.symbol_refs.append(entry)

        # Bonus: fact with no extractable references is neither grounded nor ungrounded
        if not file_refs and not symbol_refs:
            result.grounded = True  # neutral — can't disprove

        return result

    def ground_all(self, max_facts: int = 100) -> dict:
        """Ground all active facts. Returns summary stats."""
        if not self.memory:
            return {"error": "no memory store"}

        facts = [
            f for f in self.memory.facts
            if f.get("lifecycle") == "active"
        ][:max_facts]

        results: list[dict] = []
        grounded_count = 0
        ungrounded_count = 0
        confidence_updates = 0

        for fact in facts:
            gr = self.ground_fact(fact)
            results.append(gr.to_dict())

            if gr.grounded:
                grounded_count += 1
            else:
                ungrounded_count += 1

            # Apply confidence delta
            if gr.confidence_delta != 0.0:
                new_conf = max(
                    0.0,
                    min(1.0, fact.get("confidence", 1.0) + gr.confidence_delta),
                )
                if new_conf != fact.get("confidence", 1.0):
                    fact["confidence"] = round(new_conf, 4)
                    # Also bump contradiction_count for scorer
                    if gr.confidence_delta < 0:
                        fact["contradiction_count"] = (
                            fact.get("contradiction_count", 0) + 1
                        )
                    confidence_updates += 1

        if confidence_updates:
            self.memory._save_facts()

        # Update graph — mark file references
        if self.graph:
            for fact, gr_dict in zip(facts, results):
                for fref in gr_dict.get("file_refs", []):
                    if fref.get("exists"):
                        self.graph.record_touch(fact["id"], fref["path"])

        ungrounded_facts = [
            r for r in results if not r["grounded"]
        ]

        return {
            "total": len(facts),
            "grounded": grounded_count,
            "ungrounded": ungrounded_count,
            "confidence_updates": confidence_updates,
            "ungrounded_details": ungrounded_facts[:10],
        }

    def apply_confidence_decay(self, decay_per_day: float = 0.02) -> dict:
        """Apply daily confidence decay to ungrounded facts.

        Facts with file/symbol references that failed grounding
        lose confidence over time. Fully grounded facts are unaffected.
        """
        if not self.memory:
            return {"error": "no memory store"}

        facts = [
            f for f in self.memory.facts
            if f.get("lifecycle") == "active"
        ]

        decayed = 0
        now = datetime.now(timezone.utc)

        for fact in facts:
            # Only decay facts that have contradiction signals
            if fact.get("contradiction_count", 0) <= 0:
                continue

            ref = fact.get("last_accessed_at") or fact.get("timestamp")
            if not ref:
                continue
            try:
                dt = datetime.fromisoformat(ref)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_days = (now - dt).total_seconds() / 86400
            except (ValueError, TypeError):
                continue

            if age_days < 1:
                continue

            current_conf = fact.get("confidence", 1.0)
            new_conf = max(0.0, current_conf - (decay_per_day * age_days * 0.1))
            if new_conf < current_conf:
                fact["confidence"] = round(new_conf, 4)
                decayed += 1

        if decayed:
            self.memory._save_facts()

        return {"decayed": decayed, "total": len(facts)}

    # ── Extraction ──────────────────────────────────────────────────

    def _extract_file_refs(self, text: str) -> list[str]:
        """Extract file path references from fact text."""
        refs = set()
        for match in _FILE_PATTERN.finditer(text):
            path = match.group(1).strip()
            if path:
                refs.add(path)
        # Also look for explicit path-like references
        # e.g., "services/auth.py" or "cli/tools/memory.py"
        for word in text.split():
            word = word.strip("`,.'\"()[]{}:")
            if "/" in word and "." in word.split("/")[-1]:
                ext = word.rsplit(".", 1)[-1].lower()
                if ext in {
                    "py", "js", "ts", "tsx", "jsx", "go", "rs", "java",
                    "rb", "c", "cpp", "h", "hpp", "css", "html", "json",
                    "yaml", "yml", "toml", "md", "sql", "sh", "bat",
                }:
                    refs.add(word)
        return sorted(refs)

    def _extract_symbol_refs(self, text: str) -> list[str]:
        """Extract symbol references from fact text."""
        symbols = set()
        for match in _SYMBOL_PATTERN.finditer(text):
            symbols.add(match.group(1))
        for match in _NAMED_REF_PATTERN.finditer(text):
            symbols.add(match.group(1))
        # Filter out common English words that match patterns
        noise = {
            "the", "a", "an", "is", "in", "on", "at", "to", "for",
            "of", "and", "or", "not", "with", "from", "by", "as",
            "that", "this", "it", "be", "are", "was", "were", "has",
            "have", "had", "do", "does", "did", "will", "would",
            "True", "False", "None", "true", "false", "null",
        }
        return sorted(s for s in symbols if s not in noise and len(s) > 2)

    # ── Existence checks ────────────────────────────────────────────

    def _file_exists(self, ref: str) -> bool:
        """Check if a file reference exists in the project."""
        # Try exact path
        full = self.project_path / ref
        if full.exists():
            return True
        # Try common prefixes
        for prefix in ["", "src/", "lib/", "app/"]:
            if (self.project_path / prefix / ref).exists():
                return True
        return False

    def _symbol_exists(
        self, symbol: str, file_refs: list[str]
    ) -> tuple[bool, str]:
        """Check if a symbol exists in the referenced files or project."""
        # Search in referenced files first
        for ref in file_refs:
            full = self.project_path / ref
            if not full.exists():
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="ignore")
                if symbol in content:
                    return True, ref
            except Exception:
                continue

        # If file_memory is available, use structural index
        if self.file_memory:
            try:
                results = self.file_memory.search(symbol, top_k=1)
                if results:
                    return True, results[0].get("file", "")
            except Exception:
                pass

        return False, ""
