"""AgentCI — reading the run history back (Phase 9 / PRD 7).

Every local run already writes a durable record. This module answers the three
questions that record can support honestly:

* **Which jobs are flaky?** A job that has both passed and failed on the SAME
  fingerprint — identical definition, identical inputs — changed its mind
  without the world changing. That is the only flake signal available locally
  that is not a guess, and it is a strong one.
* **Which jobs fail most, and how slow are they?** Straight counting.
* **Is a red run actually new information?** A failure that has happened
  before, in the same job, is a different situation from a first-ever failure.

What this deliberately does NOT do, because the data cannot support it:
predict which tests a change will break, infer coverage, or rank "risk". Those
need a dependency graph and coverage data that C3 does not have, and a
confident-sounding number derived from neither is worse than silence — an
agent would act on it.

Sample size is always reported. Three runs is not a trend, and a flake rate
computed from two observations should be visibly untrustworthy rather than
quietly wrong.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from services.ci_runner import (
    CACHED,
    CI_DIR,
    FAILED,
    PASSED,
    TIMEOUT,
    list_runs,
)

# Below this many observations a rate is noise, and is labelled as such.
MIN_CONFIDENT_RUNS = 5


@dataclass
class JobStats:
    key: str
    runs: int = 0
    passed: int = 0
    failed: int = 0
    cached: int = 0
    total_ms: int = 0
    fingerprints: dict = field(default_factory=lambda: defaultdict(set))
    last_status: str = ""
    last_at: str = ""

    @property
    def executed(self) -> int:
        """Runs that actually executed — cached reuse is not an observation."""
        return self.passed + self.failed

    @property
    def fail_rate(self) -> float:
        return (self.failed / self.executed) if self.executed else 0.0

    @property
    def avg_ms(self) -> int:
        return round(self.total_ms / self.passed) if self.passed else 0

    @property
    def flaky_fingerprints(self) -> list:
        """Fingerprints seen both passing AND failing — same inputs, two answers."""
        return sorted(fp for fp, outcomes in self.fingerprints.items()
                      if fp and len(outcomes) > 1)

    @property
    def confident(self) -> bool:
        return self.executed >= MIN_CONFIDENT_RUNS

    def to_dict(self) -> dict:
        return {
            "key": self.key, "runs": self.runs, "executed": self.executed,
            "passed": self.passed, "failed": self.failed, "cached": self.cached,
            "fail_rate": round(self.fail_rate, 3), "avg_ms": self.avg_ms,
            "flaky": bool(self.flaky_fingerprints),
            "flaky_fingerprints": self.flaky_fingerprints[:5],
            "confident": self.confident,
            "last_status": self.last_status, "last_at": self.last_at,
        }


def _iter_run_records(project, limit: int) -> list:
    """Full run.json records, newest first."""
    base = Path(project) / CI_DIR / "runs"
    records: list = []
    for row in list_runs(project, limit=limit):
        path = base / str(row.get("run_id", "")) / "run.json"
        if not path.is_file():
            continue
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return records


def analyse(project, limit: int = 100) -> dict:
    """Per-job history across the last *limit* runs."""
    records = _iter_run_records(project, limit)
    stats: dict = {}

    for record in records:
        for job in record.get("jobs", []):
            status = job.get("status", "")
            if status not in (PASSED, FAILED, TIMEOUT, CACHED):
                continue                      # never ran; not an observation
            key = job.get("key", "?")
            entry = stats.setdefault(key, JobStats(key=key))
            entry.runs += 1
            if not entry.last_status:         # records are newest-first
                entry.last_status = status
                entry.last_at = record.get("started_at", "")
            if status == CACHED:
                entry.cached += 1
                continue
            fingerprint = job.get("fingerprint") or ""
            if status == PASSED:
                entry.passed += 1
                entry.total_ms += int(job.get("duration_ms") or 0)
                entry.fingerprints[fingerprint].add(PASSED)
            else:
                entry.failed += 1
                entry.fingerprints[fingerprint].add(FAILED)

    ordered = sorted(stats.values(),
                     key=lambda s: (-s.fail_rate, -s.executed, s.key))
    flaky = [s for s in ordered if s.flaky_fingerprints]
    slowest = sorted((s for s in ordered if s.avg_ms),
                     key=lambda s: -s.avg_ms)[:5]

    return {
        "runs_analysed": len(records),
        "jobs": [s.to_dict() for s in ordered],
        "flaky": [s.to_dict() for s in flaky],
        "slowest": [s.to_dict() for s in slowest],
        "note": _headline(records, flaky, ordered),
    }


def _headline(records: list, flaky: list, ordered: list) -> str:
    if not records:
        return ("No local runs recorded yet — history-based analysis needs "
                "runs to read.")
    parts = [f"{len(records)} run(s) analysed"]
    if flaky:
        parts.append(
            f"{len(flaky)} job(s) both passed AND failed on identical inputs — "
            "that is a flake, not a code change")
    unconfident = [s for s in ordered if s.executed and not s.confident]
    if unconfident and not flaky:
        parts.append(
            f"{len(unconfident)} job(s) have fewer than {MIN_CONFIDENT_RUNS} "
            "executions, so their rates are noise rather than trend")
    return "; ".join(parts)


def failure_is_new(project, job_key: str, limit: int = 100) -> dict:
    """Has this job failed before? A repeat failure is a different situation.

    `unknown` when there is no history to answer with — which is not the same
    as "no, it is new", and the caller must be able to tell them apart.
    """
    data = analyse(project, limit)
    for job in data["jobs"]:
        if job["key"] == job_key:
            if job["executed"] == 0:
                return {"known": False, "reason": "no executed runs on record"}
            return {
                "known": True,
                "failed_before": job["failed"] > 0,
                "failed": job["failed"], "executed": job["executed"],
                "flaky": job["flaky"],
                "reason": (
                    f"failed {job['failed']} of {job['executed']} execution(s)"
                    + (" — and has passed on identical inputs, so treat this "
                       "as a flake before treating it as a regression"
                       if job["flaky"] else "")),
            }
    return {"known": False, "reason": f"no history for {job_key}"}
