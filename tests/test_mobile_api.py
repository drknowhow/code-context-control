"""Tests for the /api/mobile/* companion-app gateway.

Flask test client + stub scanner/checker/reporter over real tmp-dir fixture
projects with genuine ``.c3`` JSONL files, so the merged feed, ack, and PM
paths exercise the real stores. The API key is supplied via the
``C3_ORACLE_API_KEY`` env override (same convention as the Discovery tests).

Deterministic feed assertions pass ``before=2026-08-07T00:00:00`` so entries
written at runtime (PM audit lines etc.) can never leak into the expected
fixture window.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ["C3_ORACLE_API_KEY"] = "mobile-test-key"

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import mobile_api  # noqa: E402

# Fixture timestamps (mixed naive / +00:00 on purpose — both exist on disk).
T1 = "2026-08-01T10:00:00+00:00"   # alpha notification (info, acked)
T2 = "2026-08-02T10:00:00+00:00"   # alpha activity event
T3 = "2026-08-03T10:00:00+00:00"   # alpha notification (warning, unacked)
T4 = "2026-08-04T10:00:00"         # alpha edit (ledger stamps are naive)
T5 = "2026-08-05T10:00:00+00:00"   # alpha session stat
T6 = "2026-08-06T10:00:00+00:00"   # beta activity event
FIXTURE_CEILING = "2026-08-07T00:00:00"


class _StubScanner:
    def __init__(self, projects):
        self.projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self.projects]


class _StubChecker:
    def check(self, project_path):
        return {"status": "ok", "path": project_path, "issues": []}


class _StubReporter:
    def report(self, date="", since="", until="", project_path="",
               narrate=False):
        return {"window": {"date": date, "since": since, "until": until,
                           "project": project_path},
                "totals": {}, "projects": []}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _notification(nid, severity, ts, acked):
    return {"id": nid, "agent": "test-agent", "severity": severity,
            "title": f"title-{nid}", "message": f"msg-{nid}",
            "message_hash": nid, "timestamp": ts, "last_seen": ts,
            "count": 1, "acknowledged": acked}


class TestMobileAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="c3-mobile-test-"))
        cls.alpha = cls.tmp / "alpha"
        cls.beta = cls.tmp / "beta"
        cls.gamma = cls.tmp / "gamma"      # mutation target (PM + ack)
        cls.outsider = cls.tmp / "outsider"  # never registered
        for p in (cls.alpha, cls.beta, cls.gamma, cls.outsider):
            p.mkdir()
        for p in (cls.alpha, cls.beta, cls.gamma):
            (p / ".c3").mkdir()

        _write_jsonl(cls.alpha / ".c3" / "notifications.jsonl", [
            _notification("n-info", "info", T1, acked=True),
            _notification("n-warn", "warning", T3, acked=False),
        ])
        _write_jsonl(cls.alpha / ".c3" / "activity_log.jsonl", [
            {"timestamp": T2, "type": "tool_call", "kind": "c3_search"},
        ])
        _write_jsonl(cls.alpha / ".c3" / "edit_ledger.jsonl", [
            {"id": "edit_1", "timestamp": T4, "session_id": "s1",
             "file": "a.py", "change_type": "edit", "summary": "fix",
             "lines_changed": None, "version": "v1",
             "git": {"commit": "", "branch": None}, "diff_summary": "",
             "tags": []},
        ])
        _write_jsonl(cls.alpha / ".c3" / "session_stats.jsonl", [
            {"ts": T5, "session_id": "s1", "stop_reason": "end_turn",
             "cost_usd": 0.5, "input_tokens": 100, "output_tokens": 50},
        ])
        _write_jsonl(cls.beta / ".c3" / "activity_log.jsonl", [
            {"timestamp": T6, "type": "decision", "note": "ship it"},
        ])
        _write_jsonl(cls.gamma / ".c3" / "notifications.jsonl", [
            _notification("n-gamma", "critical", T3, acked=False),
        ])

        def entry(path, name):
            return {"path": str(path), "name": name, "tags": [],
                    "active": False, "has_c3": True, "fact_count": 0}

        # Other test modules (test_oracle_discovery_api) also set this env var
        # at import time; the last import wins. Re-assert ours at setup and
        # restore the prior value on teardown so both suites pass in one run.
        cls._prior_key = os.environ.get("C3_ORACLE_API_KEY")
        os.environ["C3_ORACLE_API_KEY"] = "mobile-test-key"

        srv._cfg = {
            "mobile_api_enabled": True,
            "api_rate_limit_per_min": 0,  # limiter off; not under test here
        }
        mobile_api.init_services(
            scanner=_StubScanner([entry(cls.alpha, "alpha"),
                                  entry(cls.beta, "beta"),
                                  entry(cls.gamma, "gamma")]),
            checker=_StubChecker(),
            reporter=_StubReporter(),
        )
        srv.app.config["TESTING"] = True
        cls.client = srv.app.test_client()
        cls.auth = {"Authorization": "Bearer mobile-test-key"}

    @classmethod
    def tearDownClass(cls):
        if cls._prior_key is None:
            os.environ.pop("C3_ORACLE_API_KEY", None)
        else:
            os.environ["C3_ORACLE_API_KEY"] = cls._prior_key
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ── Auth ─────────────────────────────────────────────

    def test_auth_required_on_get(self):
        self.assertEqual(self.client.get("/api/mobile/info").status_code, 401)

    def test_auth_bad_token(self):
        r = self.client.get("/api/mobile/info",
                            headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)

    def test_auth_required_on_post(self):
        r = self.client.post("/api/mobile/pm/task",
                             json={"project": str(self.gamma)})
        self.assertEqual(r.status_code, 401)

    def test_auth_disabled_flag(self):
        srv._cfg["mobile_api_enabled"] = False
        try:
            r = self.client.get("/api/mobile/info", headers=self.auth)
            self.assertEqual(r.status_code, 404)
        finally:
            srv._cfg["mobile_api_enabled"] = True

    # ── Info ─────────────────────────────────────────────

    def test_info(self):
        r = self.client.get("/api/mobile/info", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        # 2 since the security surface: capabilities is the EFFECTIVE list,
        # filtered by config, rather than the static CAPABILITIES constant.
        # 3 since `/feed?wait=` — advertised so a client can choose to hold a
        # connection open instead of discovering support by timing a request.
        self.assertEqual(body["api_version"], 3)
        self.assertIn("feed", body["capabilities"])
        self.assertIn("feed_wait", body["capabilities"])
        self.assertIn("pm", body["capabilities"])

    # ── Projects ─────────────────────────────────────────

    def test_projects_overview(self):
        r = self.client.get("/api/mobile/projects", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        rows = {p["name"]: p for p in r.get_json()["projects"]}
        self.assertEqual(set(rows), {"alpha", "beta", "gamma"})
        self.assertEqual(rows["alpha"]["open_tasks"], 0)
        self.assertEqual(rows["alpha"]["pending_notifications"], 1)
        self.assertIsNotNone(rows["alpha"]["last_activity"])

    def test_project_health(self):
        r = self.client.get("/api/mobile/projects/health",
                            query_string={"project": str(self.alpha)},
                            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")

    # ── Feed ─────────────────────────────────────────────

    def _feed(self, **params):
        params.setdefault("before", FIXTURE_CEILING)
        r = self.client.get("/api/mobile/feed", query_string=params,
                            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_feed_merges_and_orders(self):
        body = self._feed()
        ts_list = [i["ts"] for i in body["items"]]
        # T3 appears twice: alpha's warning and gamma's critical notification.
        self.assertEqual(ts_list, [T6, T5, T4, T3, T3, T2, T1])
        self.assertEqual(len({i["id"] for i in body["items"]}), 7)
        self.assertFalse(body["truncated"])
        self.assertIsNone(body["next_cursor"])

    def test_feed_type_filter(self):
        body = self._feed(types="edit")
        self.assertEqual([i["type"] for i in body["items"]], ["edit"])
        self.assertEqual(body["items"][0]["data"]["file"], "a.py")

    def test_feed_severity_filter(self):
        body = self._feed(types="notification", severity="warning",
                          project=str(self.alpha))
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["data"]["id"], "n-warn")

    def test_feed_project_filter(self):
        body = self._feed(project=str(self.beta))
        self.assertEqual([i["ts"] for i in body["items"]], [T6])
        self.assertEqual(body["items"][0]["project_name"], "beta")

    def test_feed_since_watermark(self):
        body = self._feed(since=T4)
        self.assertEqual([i["ts"] for i in body["items"]], [T6, T5])

    def test_feed_cursor_pagination(self):
        page1 = self._feed(limit=2)
        self.assertEqual([i["ts"] for i in page1["items"]], [T6, T5])
        self.assertTrue(page1["truncated"])
        self.assertEqual(page1["next_cursor"], T5)
        page2 = self._feed(limit=2, before=page1["next_cursor"])
        self.assertEqual([i["ts"] for i in page2["items"]], [T4, T3])

    # ── Feed long-poll (`wait`) ──────────────────────────
    #
    # The delivery floor is what this is for: before `wait`, the phone learned
    # about an override request either on its 15s foreground tick or on
    # Android's ~15-MINUTE WorkManager tick, and a 10-minute request TTL could
    # expire in between. These tests pin the two halves that make holding a
    # connection safe — it returns EARLY when something lands, and it returns
    # AT ALL when nothing does.

    def _raw_feed(self, **params):
        r = self.client.get("/api/mobile/feed", query_string=params,
                            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_feed_wait_needs_a_watermark(self):
        # Without `since` the first scan matches history and answers instantly,
        # so honouring `wait` would be a promise the parameter cannot keep.
        # Silently ignoring it is correct; doing so *invisibly* is not, hence
        # the absent `waited_s`.
        body = self._raw_feed(wait=5, project=str(self.beta))
        self.assertNotIn("waited_s", body)
        self.assertEqual([i["ts"] for i in body["items"]], [T6])

    def test_feed_wait_ignored_when_paginating(self):
        body = self._raw_feed(wait=5, since=T1, before=FIXTURE_CEILING,
                              project=str(self.alpha))
        self.assertNotIn("waited_s", body)

    def test_feed_wait_returns_empty_after_holding(self):
        # An empty page after a full hold is the NORMAL answer, not an error:
        # the client reconnects with the same watermark. If this ever raised or
        # blocked past its deadline, one phone would pin a server thread.
        started = time.monotonic()
        body = self._raw_feed(wait=2, since="2099-01-01T00:00:00+00:00",
                              project=str(self.beta))
        elapsed = time.monotonic() - started
        self.assertEqual(body["items"], [])
        self.assertGreaterEqual(body["waited_s"], 1.0)
        self.assertLess(elapsed, 10)

    def test_feed_wait_returns_early_when_something_lands(self):
        # The whole point, measured end to end: a notification written while
        # the request is in flight comes back on that request, not on the next
        # poll. Timestamped in 2099 so it stays outside every other test's
        # `before=FIXTURE_CEILING` window.
        landed = "2099-06-01T00:00:00+00:00"
        target = self.gamma / ".c3" / "notifications.jsonl"

        def land():
            time.sleep(0.4)
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(
                    _notification("n-woke", "critical", landed, False)) + "\n")

        writer = threading.Thread(target=land, daemon=True)
        started = time.monotonic()
        writer.start()
        try:
            body = self._raw_feed(wait=20, since="2099-01-01T00:00:00+00:00",
                                  types="notification",
                                  project=str(self.gamma))
        finally:
            writer.join(timeout=5)
        elapsed = time.monotonic() - started

        self.assertEqual([i["data"]["id"] for i in body["items"]], ["n-woke"])
        # Early, not at the deadline — otherwise the mtime watch is dead code
        # and this passes for the wrong reason.
        self.assertLess(elapsed, 15)

    def test_feed_wait_is_clamped(self):
        # A client asking for an hour gets the ceiling. That clamp is the only
        # thing bounding how long a phone can hold a server thread, so it is
        # asserted against a lowered ceiling rather than by actually sitting
        # through 30 seconds in CI — the behaviour under test is the min(), not
        # the duration.
        self.assertEqual(mobile_api._MAX_WAIT_S, 30)
        started = time.monotonic()
        with mock.patch.object(mobile_api, "_MAX_WAIT_S", 2):
            body = self._raw_feed(wait=99999,
                                  since="2099-01-01T00:00:00+00:00",
                                  project=str(self.beta))
        elapsed = time.monotonic() - started
        self.assertEqual(body["items"], [])
        self.assertLess(elapsed, 10)
        self.assertLessEqual(body["waited_s"], 4)

    def test_feed_wait_degrades_when_every_slot_is_taken(self):
        # Backpressure, not a queue: past the waiter cap the request answers
        # immediately rather than parking another thread behind the others.
        held = [mobile_api._waiters.acquire(blocking=False)
                for _ in range(mobile_api._MAX_WAITERS)]
        try:
            self.assertTrue(all(held))
            started = time.monotonic()
            body = self._raw_feed(wait=20, since="2099-01-01T00:00:00+00:00",
                                  project=str(self.beta))
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(body["items"], [])
            self.assertEqual(body["waited_s"], 0.0)
        finally:
            for ok in held:
                if ok:
                    mobile_api._waiters.release()

    def test_feed_unknown_type(self):
        r = self.client.get("/api/mobile/feed",
                            query_string={"types": "bogus"}, headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_feed_unknown_project(self):
        r = self.client.get("/api/mobile/feed",
                            query_string={"project": str(self.outsider)},
                            headers=self.auth)
        self.assertEqual(r.status_code, 404)

    # ── Notifications ack ────────────────────────────────

    def test_ack_single(self):
        from services.notifications import NotificationStore
        r = self.client.post("/api/mobile/notifications/ack",
                             json={"project": str(self.gamma),
                                   "id": "n-gamma"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["acked"], 1)
        self.assertEqual(
            NotificationStore(str(self.gamma)).get_pending_count(), 0)

    def test_ack_requires_id_or_all(self):
        r = self.client.post("/api/mobile/notifications/ack",
                             json={"project": str(self.gamma)},
                             headers=self.auth)
        self.assertEqual(r.status_code, 400)

    # ── PM board ─────────────────────────────────────────

    def test_pm_flow(self):
        # Create
        r = self.client.post("/api/mobile/pm/task",
                             json={"project": str(self.gamma),
                                   "title": "Ship the mobile app",
                                   "priority": "p1"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 201)
        task = r.get_json()["task"]
        self.assertEqual(task["status"], "backlog")

        # Board reflects it, with a rev for optimistic concurrency
        r = self.client.get("/api/mobile/pm",
                            query_string={"project": str(self.gamma)},
                            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        board = r.get_json()["board"]
        self.assertEqual(
            [t["id"] for t in board["columns"]["backlog"]], [task["id"]])
        rev = board["rev"]
        self.assertGreaterEqual(rev, 1)

        # Stale rev → 409 rev_conflict
        r = self.client.put("/api/mobile/pm/task",
                            json={"project": str(self.gamma),
                                  "id": task["id"],
                                  "move": {"status": "in_progress"},
                                  "expected_rev": 999999},
                            headers=self.auth)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "rev_conflict")

        # Correct rev → moves
        r = self.client.put("/api/mobile/pm/task",
                            json={"project": str(self.gamma),
                                  "id": task["id"],
                                  "move": {"status": "in_progress"},
                                  "expected_rev": rev},
                            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["task"]["status"], "in_progress")

        # Mutations are audited to the project's activity log
        log_text = (self.gamma / ".c3" / "activity_log.jsonl").read_text(
            encoding="utf-8")
        self.assertIn("pm_write", log_text)
        self.assertIn("oracle-mobile", log_text)

        # Event history records the mutation trail
        r = self.client.get("/api/mobile/pm/events",
                            query_string={"project": str(self.gamma)},
                            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        ops = [e["op"] for e in r.get_json()["events"]]
        self.assertIn("create", ops)
        self.assertIn("move", ops)

    def test_pm_milestone_and_note(self):
        r = self.client.post("/api/mobile/pm/milestone",
                             json={"project": str(self.gamma),
                                   "name": "V1"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 201)
        r = self.client.post("/api/mobile/pm/note",
                             json={"project": str(self.gamma),
                                   "text": "decided: Expo", "kind": "decision"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 201)

    def test_pm_unregistered_path_404_no_side_effects(self):
        r = self.client.post("/api/mobile/pm/task",
                             json={"project": str(self.outsider),
                                   "title": "nope"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 404)
        self.assertFalse((self.outsider / ".c3").exists())

    # ── Digest ───────────────────────────────────────────

    def test_digest_on_demand(self):
        r = self.client.get("/api/mobile/digest",
                            query_string={"date": "2026-08-06"},
                            headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["window"]["date"], "2026-08-06")

    def test_digest_latest(self):
        r = self.client.get("/api/mobile/digest",
                            query_string={"latest": "1"}, headers=self.auth)
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
