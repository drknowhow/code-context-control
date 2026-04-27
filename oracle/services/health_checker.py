"""Validates per-project .c3/ structure and fact integrity."""

import json
from pathlib import Path

from oracle.services.memory_reader import MemoryReader


class HealthChecker:
    """Heuristic health checks on project memory — no LLM calls."""

    def __init__(self, reader: MemoryReader):
        self.reader = reader

    def check(self, project_path: str) -> dict:
        """Run all checks and return a health report."""
        root = Path(project_path)
        c3_dir = root / ".c3"
        issues: list[dict] = []

        # ── Structure check ──
        structure_ok = True
        for required in ["facts/facts.json"]:
            if not (c3_dir / required).is_file():
                structure_ok = False
                issues.append({"severity": "error", "message": f"Missing {required}"})

        for optional in ["facts/memory_graph.json", "config.json"]:
            if not (c3_dir / optional).is_file():
                issues.append({"severity": "warning", "message": f"Missing optional {optional}"})

        # ── Validate JSON parse ──
        for json_file in ["facts/facts.json", "facts/memory_graph.json"]:
            fp = c3_dir / json_file
            if fp.is_file():
                try:
                    with open(fp, encoding="utf-8") as f:
                        json.load(f)
                except Exception as e:
                    structure_ok = False
                    issues.append({"severity": "error", "message": f"Invalid JSON in {json_file}: {e}"})

        # ── Fact integrity ──
        facts = self.reader.read_facts(project_path)
        seen_ids = set()
        required_fields = {"id", "fact", "category", "timestamp", "lifecycle"}
        for fact in facts:
            fid = fact.get("id", "")
            missing = required_fields - set(fact.keys())
            if missing:
                issues.append({"severity": "warning", "message": f"Fact {fid}: missing fields {missing}"})
            if fid in seen_ids:
                issues.append({"severity": "warning", "message": f"Duplicate fact ID: {fid}"})
            seen_ids.add(fid)

        # ── Graph integrity ──
        graph_stats = self.reader.get_graph_stats(project_path)
        if (graph_stats.get("orphaned_edges") or 0) > 0:
            issues.append({
                "severity": "warning",
                "message": f"{graph_stats['orphaned_edges']} orphaned graph edges (reference deleted facts)",
            })

        # ── Tier distribution ──
        fact_stats = self.reader.get_fact_stats(project_path)

        # ── Freshness ──
        freshness = self._compute_freshness(facts)
        if (freshness.get("days_since_last_fact") or 0) > 30:
            issues.append({"severity": "info", "message": "No new facts in over 30 days"})

        # ── Overall status ──
        error_count = sum(1 for i in issues if i["severity"] == "error")
        warn_count = sum(1 for i in issues if i["severity"] == "warning")
        if error_count > 0:
            status = "error"
        elif warn_count > 0:
            status = "warning"
        else:
            status = "ok"

        return {
            "project_path": project_path,
            "status": status,
            "structure_ok": structure_ok,
            "fact_stats": fact_stats,
            "graph_stats": graph_stats,
            "freshness": freshness,
            "issues": issues,
        }

    def _compute_freshness(self, facts: list[dict]) -> dict:
        """Compute how fresh the memory is."""
        if not facts:
            return {"last_fact_timestamp": None, "days_since_last_fact": None}

        from datetime import datetime, timezone

        timestamps = []
        for f in facts:
            ts = f.get("timestamp")
            if ts:
                try:
                    timestamps.append(datetime.fromisoformat(ts))
                except Exception:
                    pass

        if not timestamps:
            return {"last_fact_timestamp": None, "days_since_last_fact": None}

        latest = max(timestamps)
        days = (datetime.now(timezone.utc) - latest.replace(tzinfo=timezone.utc if latest.tzinfo is None else latest.tzinfo)).days

        return {
            "last_fact_timestamp": latest.isoformat(),
            "days_since_last_fact": days,
        }
