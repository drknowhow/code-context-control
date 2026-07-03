"""LLM-powered analysis engine for Oracle."""

import json
import re
from pathlib import Path

from oracle.services.cross_memory import CrossMemory
from oracle.services.memory_reader import MemoryReader
from services.ollama_bridge import OllamaBridge

_SYSTEM_PROMPT = """You are Oracle, an AI memory analyst for software projects.
You analyze project memory facts and identify patterns, risks, and opportunities.
Always respond with valid JSON. No markdown fences, no extra text."""


class InsightEngine:
    """LLM-powered cross-project analysis."""

    def __init__(self, bridge: OllamaBridge, reader: MemoryReader, cross_memory: CrossMemory):
        self.bridge = bridge
        self.reader = reader
        self.cross_memory = cross_memory

    def analyze_project(self, project_path: str, max_facts: int = 100) -> dict:
        """LLM reviews a project's top facts. Returns analysis dict."""
        facts = self.reader.read_facts(project_path)
        if not facts:
            return {"project": project_path, "analysis": "No facts found.", "suggestions": []}

        # Sort by relevance (most accessed first), take top N
        facts.sort(key=lambda f: int(f.get("relevance_count", 0)), reverse=True)
        top_facts = facts[:max_facts]

        fact_summary = "\n".join(
            f"- [{f.get('category', 'general')}] {f.get('fact', '')[:200]}"
            for f in top_facts
        )

        prompt = f"""Analyze these memory facts from project "{Path(project_path).name}":

{fact_summary}

Return JSON with:
{{
  "health_narrative": "1-2 sentence summary of memory health",
  "key_themes": ["theme1", "theme2"],
  "suggestions": [
    {{"type": "merge|archive|investigate", "description": "what to do", "fact_ids": []}}
  ]
}}"""

        response = self.bridge.generate(prompt, system=_SYSTEM_PROMPT, max_tokens=1024)
        if not response:
            return {"project": project_path, "analysis": "LLM unavailable", "suggestions": []}

        parsed = self._parse_json(response)
        return {
            "project": project_path,
            "analysis": parsed.get("health_narrative", response[:500]),
            "key_themes": parsed.get("key_themes", []),
            "suggestions": parsed.get("suggestions", []),
        }

    def find_cross_project_links(self, project_paths: list[str], max_facts_per: int = 50) -> list[dict]:
        """Compare facts across projects, generate cross-project insights."""
        if len(project_paths) < 2:
            return []

        project_summaries = []
        for path in project_paths:
            facts = self.reader.read_facts(path)
            facts.sort(key=lambda f: int(f.get("relevance_count", 0)), reverse=True)
            top = facts[:max_facts_per]
            summary = "\n".join(
                f"  - [{f.get('category', 'general')}] {f.get('fact', '')[:150]}"
                for f in top
            )
            project_summaries.append(f"### {Path(path).name}\n{summary}")

        prompt = f"""Compare memory facts from these {len(project_paths)} projects:

{"".join(project_summaries)}

Identify cross-project patterns, shared conventions, dependencies, risks, and reuse opportunities.

Return JSON array:
[
  {{
    "type": "pattern|dependency|convention|risk|opportunity|drift",
    "text": "description of the insight",
    "projects": ["project_name_1", "project_name_2"],
    "confidence": 0.0-1.0,
    "tags": ["tag1"]
  }}
]"""

        response = self.bridge.generate(prompt, system=_SYSTEM_PROMPT, max_tokens=2048, num_ctx=16384)
        if not response:
            return []

        insights_raw = self._parse_json_array(response)
        # Map project names back to paths
        name_to_path = {Path(p).name: p for p in project_paths}

        new_insights = []
        for raw in insights_raw:
            source_projects = [
                name_to_path.get(n, n) for n in raw.get("projects", [])
            ]
            if len(source_projects) < 2:
                continue

            insight = self.cross_memory.add_insight(
                text=raw.get("text", ""),
                insight_type=raw.get("type", "pattern"),
                source_projects=source_projects,
                confidence=float(raw.get("confidence", 0.7)),
                tags=raw.get("tags", []),
            )
            new_insights.append(insight)

        return new_insights

    def generate_cross_project_insights(
        self,
        project_paths: list[str],
        federated_graph: dict | None = None,
        max_pairs: int = 40,
    ) -> list[dict]:
        """Generate cross-project insights from the federated graph's cross_similar edges.

        Feeds the LLM with concrete pairs of similar facts across projects so it can
        produce typed insights (shared_convention, duplicated_fact, divergent_decision,
        shared_bug_pattern). Falls back to find_cross_project_links if no graph given.
        """
        if not federated_graph:
            return self.find_cross_project_links(project_paths)

        nodes_by_id = {n["id"]: n for n in federated_graph.get("nodes", [])}
        cross_edges = [
            e for e in federated_graph.get("edges", [])
            if e.get("scope") == "cross_similar"
        ]
        if not cross_edges:
            return []

        cross_edges.sort(key=lambda e: e.get("weight", 0), reverse=True)
        pairs = []
        for e in cross_edges[:max_pairs]:
            a = nodes_by_id.get(e["src"])
            b = nodes_by_id.get(e["dst"])
            if not a or not b:
                continue
            pairs.append({
                "a": {"project": a["project"], "category": a.get("category"),
                      "text": a.get("text", ""), "local_id": a.get("local_id"),
                      "project_path": a.get("project_path")},
                "b": {"project": b["project"], "category": b.get("category"),
                      "text": b.get("text", ""), "local_id": b.get("local_id"),
                      "project_path": b.get("project_path")},
                "similarity": e.get("weight"),
            })
        if not pairs:
            return []

        pair_lines = []
        for i, p in enumerate(pairs):
            pair_lines.append(
                f"{i+1}. [{p['similarity']:.2f}] "
                f"{p['a']['project']} ({p['a']['category']}): {p['a']['text'][:200]}\n"
                f"   vs {p['b']['project']} ({p['b']['category']}): {p['b']['text'][:200]}"
            )
        prompt = (
            f"These {len(pairs)} pairs of facts from different C3 projects scored "
            f"as similar. Classify each into one of: shared_convention, duplicated_fact, "
            f"divergent_decision, shared_bug_pattern, opportunity, drift. Merge pairs "
            f"into cross-project insights when they reinforce the same point.\n\n"
            + "\n".join(pair_lines)
            + "\n\nReturn JSON array of insights:\n"
            '[{"type": "<type>", "text": "<insight>", '
            '"projects": ["<proj_slug>", ...], '
            '"pair_indices": [<int>, ...], '
            '"confidence": 0.0-1.0, "tags": ["..."]}]'
        )

        response = self.bridge.generate(prompt, system=_SYSTEM_PROMPT, max_tokens=2048, num_ctx=16384)
        if not response:
            return []
        raw_list = self._parse_json_array(response)

        slug_to_path = {n["project"]: n["project_path"] for n in federated_graph.get("nodes", [])}
        new_insights: list[dict] = []
        for raw in raw_list:
            proj_slugs = raw.get("projects", [])
            source_projects = [slug_to_path.get(s, s) for s in proj_slugs]
            source_projects = [p for p in source_projects if p in project_paths]
            if len(source_projects) < 2:
                continue
            source_fact_ids: dict[str, list[str]] = {}
            for idx in raw.get("pair_indices", []):
                try:
                    p = pairs[int(idx) - 1]
                except (ValueError, IndexError):
                    continue
                for side in ("a", "b"):
                    path = p[side]["project_path"]
                    fid = p[side]["local_id"]
                    if path and fid:
                        source_fact_ids.setdefault(path, []).append(fid)
            insight = self.cross_memory.add_insight(
                text=raw.get("text", ""),
                insight_type=raw.get("type", "pattern"),
                source_projects=source_projects,
                source_fact_ids=source_fact_ids or None,
                confidence=float(raw.get("confidence", 0.7)),
                tags=raw.get("tags", []),
            )
            new_insights.append(insight)
        return new_insights

    def suggest_consolidation(self, project_path: str) -> list[dict]:
        """Analyze a project's facts and suggest merge/archive actions."""
        facts = self.reader.read_facts(project_path)
        if len(facts) < 5:
            return []

        # Focus on potentially stale/duplicate facts
        fact_texts = "\n".join(
            f"[{f.get('id', '?')}] ({f.get('category', 'general')}) {f.get('fact', '')[:200]}"
            for f in facts
        )

        prompt = f"""Review these {len(facts)} memory facts and suggest consolidation:

{fact_texts}

Find duplicates to merge and stale facts to archive.
Return JSON array:
[
  {{
    "action": "merge",
    "fact_ids": ["id1", "id2"],
    "survivor_id": "id1",
    "merged_text": "combined fact text",
    "reason": "why merge"
  }},
  {{
    "action": "archive",
    "fact_ids": ["id3"],
    "reason": "why archive"
  }}
]"""

        response = self.bridge.generate(prompt, system=_SYSTEM_PROMPT, max_tokens=2048, num_ctx=16384)
        if not response:
            return []

        return self._parse_json_array(response)

    def _parse_json(self, text: str) -> dict:
        """Extract JSON object from LLM response."""
        # Try direct parse
        try:
            return json.loads(text)
        except Exception:
            pass
        # Try extracting from markdown fences
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass
        # Try finding first { ... }
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except Exception:
                pass
        return {}

    def _parse_json_array(self, text: str) -> list[dict]:
        """Extract JSON array from LLM response."""
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except Exception:
            pass
        match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(1))
                if isinstance(result, list):
                    return result
            except Exception:
                pass
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                if isinstance(result, list):
                    return result
            except Exception:
                pass
        return []
