"""UserPromptSubmit hook: inject relevant project memory into each prompt.

STRICTLY READ-ONLY. Loads .c3/facts/facts.json directly and ranks with the
pure-stdlib TextIndex. Never instantiates MemoryStore — its recall() writes
telemetry back to facts.json, and this hook runs in a separate process from
the live MCP server (double-writer clobber). Never imports the runtime/ML
stack — the whole hook must stay well under a second.

Output is capped to memory_llm.inject_max_tokens (~400 tokens default).
Any failure returns None: memory injection is never worth breaking a prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

_MIN_PROMPT_CHARS = 15
_LINE_CAP = 220
_WALK_UP_LEVELS = 4


def _find_project(payload: dict, project_path=None) -> Path | None:
    candidates = []
    if project_path:
        candidates.append(Path(project_path))
    if payload.get("cwd"):
        candidates.append(Path(str(payload["cwd"])))
    for base in candidates:
        node = base
        for _ in range(_WALK_UP_LEVELS):
            if (node / ".c3").is_dir():
                return node
            if node.parent == node:
                break
            node = node.parent
    return None


def run(payload: dict, project_path=None) -> dict | None:
    try:
        prompt = str(payload.get("prompt") or "").strip()
        if len(prompt) < _MIN_PROMPT_CHARS:
            return None
        project = _find_project(payload, project_path)
        if project is None:
            return None
        try:
            cfg = json.loads((project / ".c3" / "config.json").read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
        mem_cfg = cfg.get("memory_llm", {}) if isinstance(cfg, dict) else {}
        if not mem_cfg.get("prompt_inject_enabled", True):
            return None
        try:
            facts = json.loads(
                (project / ".c3" / "facts" / "facts.json").read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(facts, list) or not facts:
            return None

        from services.text_index import TextIndex  # pure stdlib (math/re/collections)
        index = TextIndex()
        by_id: dict = {}
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if fact.get("lifecycle", "active") != "active":
                continue
            fid, text = fact.get("id"), fact.get("fact", "")
            if not fid or not text:
                continue
            by_id[fid] = fact
            index.add_or_update(fid, f"{text} {fact.get('category', '')}")
        if not by_id:
            return None

        hits = index.search(prompt, top_k=max(1, int(mem_cfg.get("inject_top_k", 4))))
        max_chars = int(mem_cfg.get("inject_max_tokens", 400)) * 4
        lines = ["[c3:memory] Possibly relevant project facts:"]
        used = len(lines[0])
        for fid, score in hits:
            if score <= 0:
                continue
            fact = by_id[fid]
            body = " ".join(str(fact.get("fact", "")).split())[:_LINE_CAP]
            line = f"- ({fact.get('category', 'general')}) {body}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        if len(lines) < 2:
            return None
        return {"additionalContext": "\n".join(lines)}
    except Exception:
        return None
