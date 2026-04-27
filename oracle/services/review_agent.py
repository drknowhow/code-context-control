"""Background daemon that periodically reviews all C3 projects."""

import json
import logging
import threading
from datetime import datetime, timezone

from oracle.config import ORACLE_DIR
from oracle.services.cross_memory import CrossMemory
from oracle.services.health_checker import HealthChecker
from oracle.services.insight_engine import InsightEngine
from oracle.services.memory_reader import MemoryReader
from oracle.services.memory_writer import MemoryWriter
from oracle.services.project_scanner import ProjectScanner

_STATE_FILE = ORACLE_DIR / "review_state.json"
_REPORTS_DIR = ORACLE_DIR / "project_reports"

log = logging.getLogger("oracle.review")


def _load_state() -> dict:
    try:
        if _STATE_FILE.is_file():
            with open(_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"projects": {}}


def _save_state(state: dict):
    ORACLE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _save_report(project_path: str, report: dict):
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    import hashlib
    key = hashlib.sha256(project_path.encode()).hexdigest()[:16]
    with open(_REPORTS_DIR / f"{key}.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


def _load_report(project_path: str) -> dict | None:
    import hashlib
    key = hashlib.sha256(project_path.encode()).hexdigest()[:16]
    rfile = _REPORTS_DIR / f"{key}.json"
    if not rfile.is_file():
        return None
    try:
        with open(rfile, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


class ReviewAgent:
    """Background daemon thread that reviews projects periodically."""

    def __init__(
        self,
        scanner: ProjectScanner,
        reader: MemoryReader,
        health_checker: HealthChecker,
        insight_engine: InsightEngine,
        cross_memory: CrossMemory,
        writer: MemoryWriter,
        interval: int = 1800,
        federated_graph=None,
    ):
        self.scanner = scanner
        self.reader = reader
        self.health_checker = health_checker
        self.insight_engine = insight_engine
        self.cross_memory = cross_memory
        self.writer = writer
        self.interval = interval
        self.federated_graph = federated_graph
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = _load_state()
        self._last_run: str | None = None
        self._running = False

    @property
    def status(self) -> dict:
        return {
            "running": self._running,
            "last_run": self._last_run,
            "interval_seconds": self.interval,
            "projects_tracked": len(self._state.get("projects", {})),
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="oracle-review")
        self._thread.start()
        self._running = True
        log.info("Review agent started (interval=%ds)", self.interval)

    def stop(self):
        self._stop.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        log.info("Review agent stopped")

    def run_now(self):
        """Trigger one immediate review cycle in a background thread."""
        threading.Thread(target=self._review_cycle, daemon=True, name="oracle-review-now").start()

    def _loop(self):
        self._stop.wait(10)  # initial delay
        while not self._stop.is_set():
            try:
                self._review_cycle()
            except Exception as e:
                log.error("Review cycle failed: %s", e)
            self._stop.wait(self.interval)

    def _review_cycle(self):
        log.info("Starting review cycle")
        projects = self.scanner.discover()
        changed = []

        for proj in projects:
            path = proj["path"]
            old_mtime = (self._state.get("projects", {}).get(path, {}).get("facts_mtime"))
            current_mtime = proj.get("facts_mtime")

            if current_mtime and current_mtime != old_mtime:
                changed.append(proj)

            # Always cache health report
            try:
                report = self.health_checker.check(path)
                _save_report(path, report)
            except Exception as e:
                log.warning("Health check failed for %s: %s", path, e)

        # Update state
        for proj in projects:
            self._state.setdefault("projects", {})[proj["path"]] = {
                "last_reviewed": datetime.now(timezone.utc).isoformat(),
                "facts_mtime": proj.get("facts_mtime"),
                "fact_count": proj.get("fact_count", 0),
            }
        _save_state(self._state)

        # Refresh federated graph cache (no auto cross-insights — on-demand only)
        if len(changed) >= 2 and self.federated_graph is not None:
            try:
                all_paths = [p["path"] for p in projects if p.get("has_facts")]
                self.federated_graph.invalidate()
                self.federated_graph.build(all_paths, force=True)
                log.info("Federated graph refreshed (%d projects, %d changed)",
                         len(all_paths), len(changed))
            except Exception as e:
                log.warning("Federated graph refresh failed: %s", e)

        # Auto-suggest consolidation for projects with many facts
        for proj in changed:
            if proj.get("fact_count", 0) > 30:
                try:
                    suggestions = self.insight_engine.suggest_consolidation(proj["path"])
                    for s in suggestions:
                        if s.get("action") == "merge":
                            self.writer.suggest(proj["path"], "merge_facts", {
                                "survivor_id": s.get("survivor_id"),
                                "merge_ids": s.get("fact_ids", []),
                                "merged_text": s.get("merged_text", ""),
                            })
                        elif s.get("action") == "archive":
                            self.writer.suggest(proj["path"], "archive_facts", {
                                "fact_ids": s.get("fact_ids", []),
                            })
                except Exception as e:
                    log.warning("Consolidation suggestion failed for %s: %s", proj["path"], e)

        self._last_run = datetime.now(timezone.utc).isoformat()
        log.info("Review cycle complete: %d projects, %d changed", len(projects), len(changed))

    def get_report(self, project_path: str) -> dict | None:
        return _load_report(project_path)

    def review_single(self, project_path: str) -> dict:
        """Run a manual review for one project. Saves report + updates state."""
        report = self.health_checker.check(project_path)
        _save_report(project_path, report)
        self._state.setdefault("projects", {})[project_path] = {
            "last_reviewed": datetime.now(timezone.utc).isoformat(),
            "facts_mtime": report.get("fact_stats", {}).get("total"),
            "fact_count": report.get("fact_stats", {}).get("total", 0),
        }
        _save_state(self._state)
        return report

    def get_last_reviewed(self, project_path: str) -> str | None:
        """Return ISO timestamp of last review for a project."""
        return (self._state.get("projects", {})
                .get(project_path, {})
                .get("last_reviewed"))
