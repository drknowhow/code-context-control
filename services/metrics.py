"""MetricsCollector — Aggregates metrics from all hybrid tier services.

Provides a single unified view of:
- Tier 1 (Output Filter): calls, raw/filtered tokens, savings
- Tier 2 (Router): routing decisions per class, latency, failures
- Tier 3 (SLTM): collection sizes, backend status
"""


class MetricsCollector:
    """Aggregates metrics from output_filter, router, vector_store, and activity_log."""

    def __init__(self, output_filter=None, router=None, vector_store=None, activity_log=None):
        self.output_filter = output_filter
        self.router = router
        self.vector_store = vector_store
        self.activity_log = activity_log

    def collect(self) -> dict:
        """Gather metrics from all tier services."""
        result = {
            "tier1_filter": None,
            "tier2_router": None,
            "tier3_sltm": None,
            "precision": None,
        }

        if self.output_filter:
            try:
                result["tier1_filter"] = self.output_filter.get_metrics()
            except Exception:
                result["tier1_filter"] = {"error": "failed to collect"}

        if self.router:
            try:
                result["tier2_router"] = self.router.get_metrics()
            except Exception:
                result["tier2_router"] = {"error": "failed to collect"}

        if self.vector_store:
            try:
                result["tier3_sltm"] = self.vector_store.get_stats()
            except Exception:
                result["tier3_sltm"] = {"error": "failed to collect"}

        if self.activity_log:
            try:
                stats = self.activity_log.get_stats()
                # Determine precision: % of tool calls that are c3_*
                # We need to look at actual tool names if possible, but for now
                # we use the 'by_type' counts if they distinguish.
                # (Refinement: ActivityLog should ideally track tool_name in tool_call events)
                # For now, we'll provide a placeholder or return the raw counts.
                result["precision"] = stats.get("by_type", {})
            except Exception:
                pass

        return result

    def summary(self) -> str:
        """One-line summary of all tiers."""
        metrics = self.collect()
        parts = []

        t1 = metrics.get("tier1_filter")
        if t1 and "calls" in t1:
            parts.append(f"filter:{t1['calls']}calls,{t1.get('total_savings_pct', 0)}%saved")

        t2 = metrics.get("tier2_router")
        if t2 and "total_routes" in t2:
            parts.append(f"router:{t2['total_routes']}routes,{t2.get('avg_latency_ms', 0)}ms")

        t3 = metrics.get("tier3_sltm")
        if t3 and "total_records" in t3:
            vec = "on" if t3.get("vector_enabled") else "off"
            parts.append(f"sltm:{t3['total_records']}rec,vec={vec}")

        # Precision metric: ratio of precision tools
        p = metrics.get("precision", {})
        total_calls = p.get("tool_call", 0)
        if total_calls > 0:
            # This is just a count of tool_call events, not tool names yet.
            # To be truly useful, we'd need to parse the log for 'c3_' prefixes.
            pass

        return " | ".join(parts) if parts else "no hybrid services active"
