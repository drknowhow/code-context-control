"""Tool classifier for the MCP proxy — dynamically filters visible tools.

Categorizes the ~26 C3 tools into 6 groups and selects which groups are
relevant based on recent tool usage, keyword patterns, and optional SLM input.
"""
import re
from typing import Optional

from services.ollama_client import OllamaClient


# ── Tool Categories ────────────────────────────────────────

CATEGORIES = {
    "core": {
        "tools": [
            "c3_search", "c3_compress", "c3_validate", "c3_filter",
            "c3_session", "c3_memory", "c3_read", "c3_impact", "c3_shell",
        ],
        "keywords": None,  # Always included
        "priority": 0,
    },
    "analysis": {
        "tools": [
            "c3_delegate",
        ],
        "keywords": re.compile(
            r"hybrid|ollama|filter|route|summarize|llm|tier|raw\s*output|delegate",
            re.IGNORECASE,
        ),
        "priority": 1,
    },
    "meta": {
        "tools": [
            "c3_status",
        ],
        "keywords": re.compile(
            r"token|stats|optimi[sz]e|index|rebuild|notif|budget|context\s*status",
            re.IGNORECASE,
        ),
        "priority": 2,
    },
}

# Reverse lookup: tool name -> category
_TOOL_TO_CATEGORY = {}
for _cat, _info in CATEGORIES.items():
    for _tool in _info["tools"]:
        _TOOL_TO_CATEGORY[_tool] = _cat


class ToolClassifier:
    """Selects which tool categories are active based on context."""

    def __init__(self, always_visible: list[str] = None,
                 max_tools: int = 12,
                 use_slm: bool = True,
                 slm_model: str = "gemma3n:latest",
                 ollama: Optional[OllamaClient] = None):
        self.always_visible = always_visible or ["core"]
        self.max_tools = max_tools
        self.use_slm = use_slm
        self.slm_model = slm_model
        self.ollama = ollama
        self.classification_reasons: dict[str, str] = {}

    def classify(self, recent_tool_names: list[str],
                 recent_text: str) -> list[str]:
        """Return list of active category names."""
        # "all" shortcut — every category is always visible
        if "all" in self.always_visible:
            all_cats = sorted(CATEGORIES, key=lambda c: CATEGORIES[c].get("priority", 99))
            self.classification_reasons = {c: "always" for c in all_cats}
            return all_cats

        active = set(self.always_visible)
        reasons: dict[str, str] = {}

        # Always-visible categories
        for cat in self.always_visible:
            reasons[cat] = "always"

        # Include categories of recently-used tools
        for name in recent_tool_names[-5:]:
            cat = _TOOL_TO_CATEGORY.get(name)
            if cat and cat not in active:
                reasons[cat] = "recent"
                active.add(cat)
            elif cat and cat not in reasons:
                reasons[cat] = "recent"

        # Keyword scan
        for cat_name, cat_info in CATEGORIES.items():
            if cat_name in active:
                continue
            pattern = cat_info["keywords"]
            if pattern and pattern.search(recent_text):
                active.add(cat_name)
                reasons[cat_name] = "keyword"

        # SLM refinement if heuristic is narrow
        if (len(active) <= 2 and self.use_slm
                and self.ollama and recent_text.strip()):
            slm_cats = self._slm_classify(recent_text, active)
            if slm_cats:
                for cat in slm_cats:
                    reasons[cat] = "slm"
                active.update(slm_cats)

        self.classification_reasons = reasons
        return sorted(active, key=lambda c: CATEGORIES.get(c, {}).get("priority", 99))

    def filter_tools(self, all_tools: list[dict],
                     active_categories: list[str]) -> list[dict]:
        """Filter a tools/list response to only include active categories."""
        # Build set of allowed tool names
        allowed = set()
        for cat in active_categories:
            cat_info = CATEGORIES.get(cat)
            if cat_info:
                allowed.update(cat_info["tools"])

        filtered = [t for t in all_tools if t.get("name") in allowed]

        # Cap at max_tools by priority
        if len(filtered) > self.max_tools:
            # Sort by category priority, keep first max_tools
            def tool_priority(t):
                cat = _TOOL_TO_CATEGORY.get(t.get("name"), "")
                return CATEGORIES.get(cat, {}).get("priority", 99)
            filtered.sort(key=tool_priority)
            filtered = filtered[:self.max_tools]

        return filtered

    def get_active_tool_count(self, active_categories: list[str]) -> int:
        """Count how many tools would be visible for given categories."""
        count = 0
        for cat in active_categories:
            cat_info = CATEGORIES.get(cat)
            if cat_info:
                count += len(cat_info["tools"])
        return min(count, self.max_tools)

    # ── SLM Refinement ─────────────────────────────────────

    def _slm_classify(self, text: str, current: set[str]) -> list[str]:
        """Ask SLM which additional categories might be relevant."""
        available = [c for c in CATEGORIES if c not in current]
        if not available:
            return []

        prompt = (
            f"Given this context: {text[:200]}\n\n"
            f"Which of these tool categories are relevant? "
            f"Categories: {', '.join(available)}\n"
            f"- core: search, compress, read, filter, validate, session, memory\n"
            f"- analysis: delegate tasks to local LLM\n"
            f"- meta: status, budget, notifications, health\n\n"
            f"Reply with ONLY the category names, comma-separated. "
            f"If none are relevant, reply NONE."
        )

        try:
            result = self.ollama.generate(
                prompt=prompt,
                model=self.slm_model,
                temperature=0.0,
                max_tokens=50,
            )
            if not result or "NONE" in result.upper():
                return []
            # Parse comma-separated category names
            cats = [c.strip().lower() for c in result.split(",")]
            return [c for c in cats if c in CATEGORIES]
        except Exception:
            return []
