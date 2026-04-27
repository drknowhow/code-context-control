"""Sliding window state tracker for the MCP proxy.

Maintains rolling conversation context: recent tool calls, files, decisions,
and detected goal. Generates a compact context line (~50 tokens) that gets
injected into tool responses so Claude retains state awareness across turns.
"""
import re
from collections import deque


class ProxyState:
    """Rolling conversation state tracker."""

    def __init__(self, window_size: int = 10):
        self.tool_calls: deque = deque(maxlen=window_size)
        self.recent_files: deque = deque(maxlen=5)
        self.recent_decisions: deque = deque(maxlen=3)
        self.current_goal: str = ""

    # ── Recording ──────────────────────────────────────────

    def record_tool_call(self, tool_name: str, args: dict,
                         response_text: str = "") -> None:
        """Record a tool call with a 1-line summary."""
        summary = self._summarize_call(tool_name, args, response_text)
        self.tool_calls.append({"name": tool_name, "summary": summary})

        # Extract file paths from args
        self._extract_files(args)

        # Detect decisions from session_log
        if tool_name == "c3_session_log" and args.get("event_type") == "decision":
            decision = args.get("data", "")[:80]
            if decision:
                self.recent_decisions.append(decision)

        # Update goal from tool args
        self._detect_goal(tool_name, args)

    def record_user_text(self, text: str) -> None:
        """Extract goal hints from user/tool text."""
        # Look for intent patterns
        patterns = [
            (r"(?:fix|debug|resolve)\s+(.{5,40})", "fix"),
            (r"(?:add|implement|create)\s+(.{5,40})", "add"),
            (r"(?:refactor|clean|simplify)\s+(.{5,40})", "refactor"),
            (r"(?:find|search|look\s+for)\s+(.{5,40})", "find"),
            (r"(?:understand|explain|how\s+does)\s+(.{5,40})", "understand"),
        ]
        for pattern, verb in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                self.current_goal = f"{verb} {m.group(1).strip()}"
                break

    # ── Output ─────────────────────────────────────────────

    def get_context_line(self) -> str:
        """Generate compact context summary for injection."""
        parts = []

        # Last tool calls
        if self.tool_calls:
            recent = list(self.tool_calls)[-3:]
            call_strs = [c["summary"] for c in recent]
            parts.append(f"Last: {', '.join(call_strs)}")

        # Current goal
        if self.current_goal:
            parts.append(f"Goal: {self.current_goal}")

        # Recent files
        if self.recent_files:
            files = [f.split("/")[-1] for f in self.recent_files]
            parts.append(f"Files: {', '.join(files)}")

        if not parts:
            return ""
        return f"\n[Context: {'. '.join(parts)}]"

    def get_recent_tool_names(self) -> list[str]:
        """Return names of recent tool calls for classifier input."""
        return [c["name"] for c in self.tool_calls]

    def get_recent_text(self) -> str:
        """Return recent context text for classifier keyword matching."""
        parts = []
        if self.current_goal:
            parts.append(self.current_goal)
        for c in self.tool_calls:
            parts.append(c["summary"])
        parts.extend(self.recent_decisions)
        return " ".join(parts)

    # ── Internal ───────────────────────────────────────────

    def _summarize_call(self, tool_name: str, args: dict,
                        response_text: str) -> str:
        """Create a compact 1-line summary of a tool call."""
        short_name = tool_name.replace("c3_", "")

        # Summarize based on tool type
        if tool_name == "c3_search":
            query = args.get("query", "")[:30]
            # Count results from response
            count = response_text.count("## ") if response_text else "?"
            return f"{short_name}({query!r}, {count} results)"

        elif tool_name == "c3_compress":
            fp = args.get("file_path", "").split("/")[-1]
            return f"{short_name}({fp})"

        elif tool_name in ("c3_remember", "c3_recall", "c3_memory_query"):
            text = args.get("fact", args.get("query", ""))[:30]
            return f"{short_name}({text!r})"

        elif tool_name == "c3_session_log":
            etype = args.get("event_type", "")
            data = args.get("data", "")[:20]
            return f"{short_name}({etype}: {data})"

        elif tool_name in ("c3_extract", "c3_filter"):
            fp = args.get("file_path", "").split("/")[-1]
            return f"{short_name}({fp})"

        else:
            # Generic: show first string arg
            for v in args.values():
                if isinstance(v, str) and len(v) > 2:
                    return f"{short_name}({v[:25]})"
            return f"{short_name}()"

    def _extract_files(self, args: dict) -> None:
        """Extract file paths from tool args."""
        for key in ("file_path", "path"):
            val = args.get(key)
            if val and isinstance(val, str):
                # Normalize and deduplicate
                if val not in self.recent_files:
                    self.recent_files.append(val)

    def _detect_goal(self, tool_name: str, args: dict) -> None:
        """Infer user goal from tool usage patterns."""
        if tool_name == "c3_search":
            query = args.get("query", "")
            if query and not self.current_goal:
                self.current_goal = f"investigate {query[:40]}"
        elif tool_name == "c3_session_log":
            if args.get("event_type") == "decision":
                data = args.get("data", "")[:40]
                if data:
                    self.current_goal = data
