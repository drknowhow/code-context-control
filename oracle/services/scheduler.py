"""The Oracle's scheduler: one tick thread that claims due schedules and fires them.

Each tick: lapse stale approvals, fold finished jobs into their runs, claim
what is due, route each claimed run through the overlap, misfire, check and
autonomy gates, dispatch approved runs, sweep old run files. A fire is a
detached ``services.shell_jobs`` job, so the tick never waits on one.

Only one scheduler per machine may dispatch: ``start`` takes
``scheduler.lock`` and a loser stays in ``standby``, retrying every tick,
because two Oracles can share a port on Windows and both would otherwise
fire every schedule.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from oracle.config import load_config
from services import schedule_runner as runner
from services.activity_log import ActivityLog
from services.notifications import notify
from services.schedule_approvals import ApprovalStore
from services.schedule_store import EXECUTED_STATUSES, ScheduleError, ScheduleStore, _FileLock, parse_iso

log = logging.getLogger("oracle.scheduler")

INITIAL_DELAY_S = 5
SKIP_POLICY_LATENESS_S = 60
RUN_SEVERITY = {"done": "info", "check_skipped": "info", "skipped_overlap": "info",
                "denied": "info", "expired": "info", "missed": "warning"}


def defaults_from_config(cfg: dict) -> dict:
    """The ``normalize_schedule`` defaults a config dict implies."""
    return {
        "timeout_s": int(cfg.get("scheduler_default_timeout_s", 1800)),
        "max_timeout_s": int(cfg.get("scheduler_max_timeout_s", 21600)),
        "approval_ttl_s": int(cfg.get("scheduler_approval_ttl_s", 3600)),
        "misfire_grace_s": int(cfg.get("scheduler_misfire_grace_s", 900)),
        "permission_mode": str(cfg.get("scheduler_default_permission_mode", "acceptEdits")),
        "allow_bypass_permissions": bool(cfg.get("scheduler_allow_bypass_permissions", False)),
        "max_budget_usd": float(cfg.get("scheduler_default_max_budget_usd", 0) or 0),
    }


class Scheduler:
    """Tick loop over a ``ScheduleStore``; see the module docstring."""

    def __init__(self, *, store: ScheduleStore, approvals: ApprovalStore, jobstore,
                 config=load_config, interval: int = 30, lock_path=None):
        self.store = store
        self.approvals = approvals
        self.jobstore = jobstore
        self.config = config
        self.interval = max(5, int(interval))
        self._lock = _FileLock(lock_path or (store.root / "scheduler.lock"))
        self._lock_held = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick_guard = threading.Lock()
        self._running = False
        self._last_tick_at: str | None = None
        self._last_summary: dict = {}

    @property
    def status(self) -> dict:
        schedules = self.store.list(enabled=True)
        due = sorted(r["next_run_at"] for r in schedules if r.get("next_run_at"))
        return {
            "running": self._running,
            "standby": self._running and not self._lock_held,
            "lock_held": self._lock_held,
            "interval_seconds": self.interval,
            "last_tick_at": self._last_tick_at,
            "last_tick": self._last_summary,
            "enabled_schedules": len(schedules),
            "next_due": due[0] if due else None,
            "open_runs": len(self.store.open_runs()),
            "pending_approvals": len(self.approvals.list(status="pending")),
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="oracle-scheduler")
        self._thread.start()
        self._running = True
        log.info("Scheduler started (interval=%ds)", self.interval)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._release_lock()
        log.info("Scheduler stopped")

    def kick(self) -> None:
        self._wake.set()

    def run_now(self, schedule_id: str, *, bypass_approval: bool = False, by: str = "") -> dict:
        """Queue a manual run; ``bypass_approval`` skips the approval gate once."""
        run = self.store.request_run(schedule_id, trigger="manual", by=by)
        if bypass_approval:
            run = self.store.update_run(run["run_id"], status="approved")
        self.kick()
        return run

    def _loop(self) -> None:
        self._wake.wait(INITIAL_DELAY_S)
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception as exc:
                log.error("Scheduler tick failed: %s", exc)
            self._wake.wait(self.interval)

    def _acquire_lock(self) -> bool:
        if self._lock_held:
            return True
        self._lock_held = self._lock.acquire(timeout_s=0.2)
        if not self._lock_held:
            log.warning("Scheduler standby: another scheduler holds %s", self._lock.path)
        return self._lock_held

    def _release_lock(self) -> None:
        if self._lock_held:
            self._lock.release()
            self._lock_held = False

    def tick(self, now: datetime | None = None) -> dict:
        """One pass; returns counts. Safe to call from a request thread."""
        now = now or datetime.now(timezone.utc)
        cfg = self.config() or {}
        summary = {"standby": False, "expired": 0, "finished": 0, "claimed": 0, "dispatched": 0,
                   "awaiting_approval": 0, "skipped": 0, "swept": 0}
        if not bool(cfg.get("scheduler_enabled", True)):
            summary["disabled"] = True
            return summary
        if not self._acquire_lock():
            summary["standby"] = True
            return summary
        with self._tick_guard:
            self._last_tick_at = now.isoformat()
            self._step(summary, "expired", lambda: self._expire_approvals(now))
            self._step(summary, "finished", lambda: self._reconcile_jobs())
            self._step(summary, "claimed", lambda: len(self.store.claim_due(now)))
            self._step(summary, None, lambda: self._route_claimed(now, cfg, summary))
            self._step(summary, None, lambda: self._route_decided(summary))
            self._step(summary, "swept", lambda: self.store.sweep_runs(
                int(cfg.get("scheduler_run_retention_days", 30)), now))
            self._last_summary = summary
        return summary

    @staticmethod
    def _step(summary: dict, key, fn) -> None:
        try:
            result = fn()
        except Exception as exc:
            log.error("Scheduler step %s failed: %s", key or fn.__name__, exc)
            summary.setdefault("errors", []).append(f"{key or 'route'}: {exc}")
            return
        if key is not None and isinstance(result, int):
            summary[key] += result

    def _expire_approvals(self, now: datetime) -> int:
        expired = self.approvals.sweep_expired(now)
        for row in expired:
            run = self.store.get_run(row.get("run_id") or "")
            if run and run.get("status") == "awaiting_approval":
                self._finish(run, "expired")
        return len(expired)

    def _reconcile_jobs(self) -> int:
        try:
            self.jobstore.reap(session_id=runner.SESSION_ID)
        except Exception as exc:
            log.warning("job reap failed: %s", exc)
        finished = 0
        for run in self.store.runs(status=("dispatched", "running", "check_running"), limit=0):
            job = self.jobstore.find(run.get("job_id") or "") if run.get("job_id") else None
            if job is None:
                self._finish(run, "lost", error="job record missing")
                finished += 1
                continue
            if not job.terminal:
                if run.get("status") == "dispatched" and job.status == "running":
                    self.store.update_run(run["run_id"], status="running")
                continue
            schedule = self.store.get(run.get("schedule_id", "")) or {}
            if run.get("status") == "check_running":
                self._settle_check(run, job, schedule)
            else:
                parser = "command" if schedule.get("kind") == "command" else str(schedule.get("provider") or "claude")
                done = runner.finalize(self.store, self.jobstore, run, job, parser=parser)
                self._announce(done)
            finished += 1
        return finished

    def _settle_check(self, run: dict, job, schedule: dict) -> None:
        if job.status != "done" and job.exit_code != 1:
            self._finish(run, "failed", job_id=job.id, exit_code=job.exit_code,
                         error=f"check_command ended {job.status}: {job.error or ''}".strip())
            return
        if job.exit_code == 0:
            self._finish(run, "check_skipped", job_id=job.id, exit_code=0, duration_ms=job.duration_ms)
            return
        if not schedule:
            self._finish(run, "failed", error="schedule deleted while its check ran")
            return
        run = self.store.update_run(run["run_id"], status="claimed", job_id=None, exit_code=None)
        self._gate(schedule, run, self.config() or {})

    def _route_claimed(self, now: datetime, cfg: dict, summary: dict) -> None:
        for run in self.store.runs(status="claimed", limit=0):
            schedule = self.store.get(run.get("schedule_id", ""))
            if schedule is None:
                self._finish(run, "failed", error="schedule deleted before it ran")
                continue
            if run.get("trigger") == "cron" and self._misfired(schedule, run, now):
                self._finish(run, "missed", error="fired outside the misfire window")
                summary["skipped"] += 1
                continue
            if schedule.get("overlap", "skip") == "skip" and self.store.open_run_for(
                    schedule["id"], exclude=run["run_id"]):
                self._finish(run, "skipped_overlap", error="previous run still open")
                summary["skipped"] += 1
                continue
            if schedule.get("check_command"):
                runner.dispatch(self.store, self.jobstore, schedule, run, check=True)
                continue
            self._gate(schedule, run, cfg, summary)

    @staticmethod
    def _misfired(schedule: dict, run: dict, now: datetime) -> bool:
        try:
            late = (now - parse_iso(run.get("due_at") or "")).total_seconds()
        except ValueError:
            return False
        misfire = schedule.get("misfire") or {}
        if misfire.get("policy") == "skip":
            return late > SKIP_POLICY_LATENESS_S
        return late > int(misfire.get("grace_s") or 0)

    def _gate(self, schedule: dict, run: dict, cfg: dict, summary: dict | None = None) -> None:
        if schedule.get("autonomy") == "approval":
            ttl = int(schedule.get("approval_ttl_s") or cfg.get("scheduler_approval_ttl_s", 3600))
            row = self.approvals.create(schedule, run, ttl_s=ttl)
            if row.get("duplicate"):
                self._finish(run, "skipped_overlap", error=f"approval {row['id']} already pending")
                return
            self.store.update_run(run["run_id"], status="awaiting_approval", approval_id=row["id"])
            if summary is not None:
                summary["awaiting_approval"] += 1
            return
        self._dispatch(schedule, run, summary)

    def _dispatch(self, schedule: dict, run: dict, summary: dict | None) -> None:
        done = runner.dispatch(self.store, self.jobstore, schedule, run)
        if done.get("status") in EXECUTED_STATUSES:
            self._announce(done)
        elif summary is not None:
            summary["dispatched"] += 1

    def _route_decided(self, summary: dict) -> None:
        for run in self.store.runs(status=("awaiting_approval", "approved"), limit=0):
            schedule = self.store.get(run.get("schedule_id", ""))
            if schedule is None:
                self._finish(run, "failed", error="schedule deleted while awaiting approval")
                continue
            if run.get("status") == "approved":
                self._dispatch(schedule, run, summary)
                continue
            row = self.approvals.get(run.get("approval_id") or "")
            state = (row or {}).get("status")
            if state == "approved":
                self.store.update_run(run["run_id"], status="approved")
                self._dispatch(schedule, dict(run, status="approved"), summary)
            elif state == "denied":
                self._finish(run, "denied", error=f"denied by {row.get('decided_by') or 'unknown'}")
            elif state in (None, "expired"):
                self._finish(run, "expired")

    def _finish(self, run: dict, status: str, **extra) -> dict:
        try:
            done = self.store.finish_run(run["run_id"], status, **extra)
        except ScheduleError as exc:
            log.warning("finish %s as %s failed: %s", run.get("run_id"), status, exc)
            return run
        self._announce(done)
        return done

    def _announce(self, run: dict) -> None:
        status = str(run.get("status") or "")
        name = run.get("schedule_name") or run.get("schedule_id") or "schedule"
        parts = [f"{status}"]
        if run.get("exit_code") is not None:
            parts.append(f"exit {run['exit_code']}")
        if run.get("duration_ms") is not None:
            parts.append(f"{(run['duration_ms'] or 0) / 1000:.1f}s")
        if run.get("cost_usd") is not None:
            parts.append(f"${float(run['cost_usd']):.3f}")
        if run.get("error"):
            parts.append(str(run["error"])[:200])
        severity = RUN_SEVERITY.get(status, "warning")
        notify(run.get("project_path") or "", agent="scheduler", severity=severity,
               title=f"Scheduled run {status}: {name}", message=" — ".join(parts),
               kind="schedule", ref_id=run["run_id"])
        try:
            ActivityLog(run.get("project_path") or "").log("schedule_run", {
                "run_id": run["run_id"], "schedule_id": run.get("schedule_id"), "name": name,
                "status": status, "trigger": run.get("trigger"), "job_id": run.get("job_id"),
                "exit_code": run.get("exit_code"), "duration_ms": run.get("duration_ms"),
                "cost_usd": run.get("cost_usd"), "output_id": run.get("output_id"),
            })
        except (OSError, ValueError) as exc:
            log.debug("activity log skipped for %s: %s", run["run_id"], exc)
