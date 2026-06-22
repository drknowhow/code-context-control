"""Cross-project activity reporting for the Oracle.

Aggregates per-project C3 activity (sessions, tool calls, edits, git mutations,
token/cost) into a single daily digest.  Reads the same ``.c3`` JSONL artifacts
the local UI uses, directly per project — no C3Runtime build required (mirrors
how ``MemoryReader`` / ``ProjectManager`` read project data straight off disk).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from services.activity_log import ActivityLog
from services.edit_ledger import EditLedger
from services.session_manager import SessionManager

log = logging.getLogger("oracle")

# Lexicographic sentinels for an open-ended window (ISO-8601 keeps string order).
_MIN_TS = "0000-01-01T00:00:00"
_MAX_TS = "9999-12-31T23:59:59"

# Per-source scan caps. When a source returns exactly its cap, the day may have
# more rows than we scanned, so the digest surfaces ``truncated: true`` rather
# than silently undercounting a very busy day. (Paging is overkill — a flag is
# enough for a digest.)
_CAP_ACTIVITY = 10000
_CAP_SESSIONS = 100
_CAP_SESSION_STATS = 500
_CAP_EDITS = 10000

_NARRATE_SYSTEM = (
    "You are C3's activity reporter. Given a JSON digest of a developer's work "
    "across their projects for a time window, write a concise, friendly summary "
    "(3-6 sentences). Lead with the headline (busiest project and the totals), "
    "then call out notable specifics. Use ONLY the numbers provided — never "
    "invent data. If everything is zero, say it was a quiet period."
)


class ActivityReporter:
    """Builds cross-project (or single-project) activity digests.

    Construct with an Oracle ``ProjectScanner`` and, optionally, an
    ``OllamaBridge`` for prose narration (``narrate=True``).
    """

    def __init__(self, scanner, ollama_bridge=None):
        self.scanner = scanner
        self.ollama_bridge = ollama_bridge

    # ── Public API ───────────────────────────────────────────────

    def report(self, date: str = "", since: str = "", until: str = "",
               project_path: str = "", narrate: bool = False) -> dict:
        """Aggregate activity into a digest dict.

        date: UTC day ``YYYY-MM-DD`` (default today). since/until: ISO bounds
        that override ``date``. project_path: limit to one project (else all
        registered projects with a ``.c3`` dir). narrate: add an LLM prose
        summary (best-effort; never fails the structured result).
        """
        lo, hi, window = self._resolve_window(date, since, until)

        proj_reports = []
        for proj in self._target_projects(project_path):
            pr = self._report_project(proj, lo, hi)
            if (pr["tool_calls"] or pr["edits"] or pr["git_mutations"]
                    or pr["sessions"] or pr["decisions"]):
                proj_reports.append(pr)

        digest = {
            "window": window,
            "totals": self._aggregate_totals(proj_reports),
            "projects": proj_reports,
            # True if any project hit a per-source scan cap (counts may be
            # undercounted for that project). See _CAP_* constants.
            "truncated": any(pr.get("truncated") for pr in proj_reports),
            "narrative": None,
        }
        if narrate:
            digest["narrative"] = self._narrate(digest)
        return digest

    # ── Window resolution ────────────────────────────────────────

    @staticmethod
    def _resolve_window(date: str, since: str, until: str):
        """Return (lo, hi, window_dict).

        Bounds are naive (no tz suffix) on purpose: the date/time prefix
        dominates lexicographically, so the same bounds correctly window both
        naive ledger timestamps and tz-aware ``+00:00`` activity timestamps.
        """
        if since or until:
            lo = since or _MIN_TS
            hi = until or _MAX_TS
            window = {
                "since": since or None,
                "until": until or None,
                "label": f"{since or 'beginning'} → {until or 'now'}",
                "tz": "as-given",
            }
            return lo, hi, window

        day = date.strip() if date else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lo = f"{day}T00:00:00"
        hi = f"{day}T23:59:59.999999"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        window = {
            "since": lo,
            "until": hi,
            "label": f"{day}{' (today)' if day == today else ''}, UTC",
            "tz": "UTC",
        }
        return lo, hi, window

    # ── Project selection ────────────────────────────────────────

    def _target_projects(self, project_path: str) -> list[dict]:
        if project_path:
            p = Path(project_path)
            return [{"path": str(p), "name": p.name, "has_c3": (p / ".c3").is_dir()}]
        return [p for p in self.scanner.discover() if p.get("has_c3")]

    # ── Per-project aggregation ──────────────────────────────────

    def _report_project(self, proj: dict, lo: str, hi: str) -> dict:
        path = proj.get("path", "")
        base = {
            "name": proj.get("name") or Path(path).name,
            "path": path,
            "sessions": [],
            "tool_calls": 0,
            "edits": 0,
            "git_mutations": 0,
            "decisions": 0,
            "events": {},
            "tokens": {"input": 0, "output": 0},
            "cost_usd": 0.0,
            "first_activity": None,
            "last_activity": None,
            "truncated": False,
        }
        # Guard: only read projects that already have a .c3 dir — never create
        # one as a side effect (ActivityLog/SessionManager mkdir on init).
        if not path or not (Path(path) / ".c3").is_dir():
            return base

        timestamps: list[str] = []

        # Activity log → per-type event counts.
        try:
            counts: dict[str, int] = {}
            rows = list(ActivityLog(path).get_recent(limit=_CAP_ACTIVITY, since=lo, until=hi))
            if len(rows) >= _CAP_ACTIVITY:
                base["truncated"] = True
            for e in rows:
                etype = e.get("type", "unknown")
                counts[etype] = counts.get(etype, 0) + 1
                if e.get("timestamp"):
                    timestamps.append(e["timestamp"])
            base["events"] = counts
            base["tool_calls"] = counts.get("tool_call", 0)
            base["decisions"] = counts.get("decision", 0)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("activity_log read failed for %s: %s", path, exc)

        try:
            sm = SessionManager(path)
            # Sessions started within the window.
            sess_rows = list(sm.list_sessions(_CAP_SESSIONS))
            if len(sess_rows) >= _CAP_SESSIONS:
                base["truncated"] = True
            for s in sess_rows:
                started = s.get("started", "")
                if started and lo <= started <= hi:
                    base["sessions"].append({
                        "id": s.get("id"),
                        "started": started,
                        "ended": s.get("ended", ""),
                        "description": s.get("description", ""),
                        "tool_calls": s.get("tool_calls", 0),
                        "duration": s.get("duration", ""),
                    })
                    timestamps.append(started)
            # Token / cost from hook-captured session stats.
            stat_rows = list(sm.get_session_stats(_CAP_SESSION_STATS))
            if len(stat_rows) >= _CAP_SESSION_STATS:
                base["truncated"] = True
            for st in stat_rows:
                ts = st.get("ts", "")
                if ts and lo <= ts <= hi:
                    base["tokens"]["input"] += int(st.get("input_tokens", 0) or 0)
                    base["tokens"]["output"] += int(st.get("output_tokens", 0) or 0)
                    base["cost_usd"] += float(st.get("cost_usd", 0) or 0)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("session read failed for %s: %s", path, exc)

        # Edit ledger → edits vs git mutations (get_history filters >= lo only).
        try:
            edit_rows = list(EditLedger(path).get_history(since=lo, limit=_CAP_EDITS))
            if len(edit_rows) >= _CAP_EDITS:
                base["truncated"] = True
            for en in edit_rows:
                ts = en.get("timestamp", "")
                if ts and ts > hi:
                    continue
                if en.get("change_type") == "shell_git":
                    base["git_mutations"] += 1
                else:
                    base["edits"] += 1
                if ts:
                    timestamps.append(ts)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("edit ledger read failed for %s: %s", path, exc)

        base["cost_usd"] = round(base["cost_usd"], 4)
        if timestamps:
            timestamps.sort()
            base["first_activity"] = timestamps[0]
            base["last_activity"] = timestamps[-1]
        return base

    @staticmethod
    def _aggregate_totals(reports: list[dict]) -> dict:
        totals = {
            "projects_active": len(reports),
            "sessions": 0,
            "tool_calls": 0,
            "edits": 0,
            "git_mutations": 0,
            "decisions": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        }
        for r in reports:
            totals["sessions"] += len(r["sessions"])
            totals["tool_calls"] += r["tool_calls"]
            totals["edits"] += r["edits"]
            totals["git_mutations"] += r["git_mutations"]
            totals["decisions"] += r["decisions"]
            totals["input_tokens"] += r["tokens"]["input"]
            totals["output_tokens"] += r["tokens"]["output"]
            totals["cost_usd"] += r["cost_usd"]
        totals["cost_usd"] = round(totals["cost_usd"], 4)
        return totals

    # ── Narration (best-effort) ──────────────────────────────────

    def _narrate(self, digest: dict) -> str | None:
        if self.ollama_bridge is None:
            digest["narrative_error"] = "No Ollama bridge configured."
            return None
        payload = {
            "window": digest["window"]["label"],
            "totals": digest["totals"],
            "projects": [
                {
                    "name": p["name"],
                    "tool_calls": p["tool_calls"],
                    "edits": p["edits"],
                    "git_mutations": p["git_mutations"],
                    "sessions": len(p["sessions"]),
                    "cost_usd": p["cost_usd"],
                }
                for p in digest["projects"]
            ],
        }
        prompt = "Summarize this developer activity digest:\n\n" + json.dumps(payload, indent=2)
        try:
            text = self.ollama_bridge.generate(prompt, system=_NARRATE_SYSTEM)
            return (text or "").strip() or None
        except Exception as exc:
            log.warning("activity narration failed: %s", exc)
            digest["narrative_error"] = str(exc)
            return None
