"""Tests for the scheduled activity digest in ReviewAgent (Wave 2).

Off by default (zero reporter calls), due/not-due interval logic, exception
safety (a digest failure must not kill the review cycle), persistence to
activity_digests/<date>.json + latest.json, retention pruning, optional JSONL
notify sink, and the /api/activity/digest/latest route.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import oracle.services.review_agent as ra  # noqa: E402
from oracle.services.review_agent import ReviewAgent  # noqa: E402


class _CountingReporter:
    def __init__(self):
        self.calls: list[dict] = []

    def report(self, date="", narrate=False, **kw):
        self.calls.append({"date": date, "narrate": narrate})
        return {"totals": {"sessions": 3}, "truncated": False, "window": {"label": date}}


class _StubScanner:
    def discover(self, force=False):
        return []


def _agent(reporter, cfg, oracle_dir):
    agent = ReviewAgent(
        scanner=_StubScanner(), reader=None, health_checker=mock.Mock(),
        insight_engine=mock.Mock(), cross_memory=mock.Mock(), writer=mock.Mock(),
        interval=1800, federated_graph=None, activity_reporter=reporter,
    )
    agent._state = {"projects": {}}
    return agent


class _DigestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.oracle_dir = Path(self.tmp.name)
        self.digests = self.oracle_dir / "activity_digests"
        self.cfg = {
            "digest_enabled": True,
            "digest_interval_seconds": 86400,
            "digest_narrate": False,
            "digest_notify_file": "",
            "digest_retention_days": 14,
        }
        for patcher in (
            mock.patch.object(ra, "ORACLE_DIR", self.oracle_dir),
            mock.patch.object(ra, "_STATE_FILE", self.oracle_dir / "review_state.json"),
            mock.patch.object(ra, "_REPORTS_DIR", self.oracle_dir / "project_reports"),
            mock.patch.object(ra, "_DIGESTS_DIR", self.digests),
            mock.patch.object(ra, "load_config", lambda: dict(self.cfg)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.reporter = _CountingReporter()
        self.agent = _agent(self.reporter, self.cfg, self.oracle_dir)


class TestDigestScheduling(_DigestBase):
    def test_disabled_by_default_makes_zero_calls(self):
        self.cfg["digest_enabled"] = False
        self.agent._maybe_run_digest()
        self.assertEqual(self.reporter.calls, [])
        self.assertFalse(self.digests.exists())

    def test_enabled_and_due_writes_date_and_latest(self):
        self.agent._maybe_run_digest()
        self.assertEqual(len(self.reporter.calls), 1)
        today = datetime.now(timezone.utc).date().isoformat()
        self.assertTrue((self.digests / f"{today}.json").is_file())
        latest = json.loads((self.digests / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["digest"]["totals"]["sessions"], 3)
        self.assertTrue(self.agent._state["last_digest_at"])

    def test_not_due_skips(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        self.agent._state["last_digest_at"] = recent
        self.agent._maybe_run_digest()
        self.assertEqual(self.reporter.calls, [])

    def test_due_after_interval_runs(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.agent._state["last_digest_at"] = stale
        self.agent._maybe_run_digest()
        self.assertEqual(len(self.reporter.calls), 1)

    def test_no_reporter_is_noop(self):
        agent = _agent(None, self.cfg, self.oracle_dir)
        agent.activity_reporter = None
        agent._maybe_run_digest()  # must not raise
        self.assertFalse(self.digests.exists())

    def test_reporter_failure_does_not_break_review_cycle(self):
        self.reporter.report = mock.Mock(side_effect=RuntimeError("boom"))
        # _review_cycle wraps the digest — the cycle must complete.
        self.agent._review_cycle()
        self.assertTrue(self.agent._last_run)

    def test_narrate_flag_passed_through(self):
        self.cfg["digest_narrate"] = True
        self.agent._maybe_run_digest()
        self.assertIs(self.reporter.calls[0]["narrate"], True)

    def test_status_surfaces_digest_fields(self):
        self.agent._state["last_digest_at"] = "2026-07-02T00:00:00+00:00"
        status = self.agent.status
        self.assertTrue(status["digest_enabled"])
        self.assertEqual(status["last_digest_at"], "2026-07-02T00:00:00+00:00")


class TestDigestRetentionAndNotify(_DigestBase):
    def test_retention_prunes_old_but_keeps_latest(self):
        self.digests.mkdir(parents=True)
        old = self.digests / "2020-01-01.json"
        old.write_text("{}", encoding="utf-8")
        stamp = time.time() - 30 * 86400
        os.utime(old, (stamp, stamp))
        keep = self.digests / "latest.json"
        keep.write_text("{}", encoding="utf-8")
        os.utime(keep, (stamp, stamp))  # even an old latest.json survives
        self.agent._prune_digests(14)
        self.assertFalse(old.exists())
        self.assertTrue(keep.exists())

    def test_notify_appends_jsonl_line(self):
        sink = self.oracle_dir / "notify.jsonl"
        self.cfg["digest_notify_file"] = str(sink)
        self.agent._maybe_run_digest()
        lines = sink.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["totals"]["sessions"], 3)
        self.assertIn("ts", entry)


class TestLatestDigestRoute(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.oracle_dir = Path(self.tmp.name)
        import oracle.oracle_server as srv
        self.srv = srv
        patcher = mock.patch.object(srv, "ORACLE_DIR", self.oracle_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()

    def test_missing_file_returns_null_digest(self):
        body = self.client.get("/api/activity/digest/latest").get_json()
        self.assertIsNone(body["digest"])

    def test_serves_latest_file(self):
        d = self.oracle_dir / "activity_digests"
        d.mkdir(parents=True)
        (d / "latest.json").write_text(
            json.dumps({"generated_at": "2026-07-02T10:00:00+00:00",
                        "digest": {"totals": {"sessions": 5}}}),
            encoding="utf-8",
        )
        body = self.client.get("/api/activity/digest/latest").get_json()
        self.assertEqual(body["digest"]["totals"]["sessions"], 5)


if __name__ == "__main__":
    unittest.main()
