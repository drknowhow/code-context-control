"""Tests for the api_version 4 ops surface (``/api/mobile/*``).

Sibling of ``tests/test_mobile_api.py`` and reuses its conventions: a Flask
test client over ``oracle.oracle_server``, a stub scanner registering real
tmp-dir projects with genuine ``.c3`` files, and the Bearer supplied via the
``C3_ORACLE_API_KEY`` env override.

The routes covered here differ from the feed/PM ones in where their data
lives. ``/edits`` and ``/locks`` read per-project ``.c3`` files, so those use
the real stores. ``/status``, ``/insights``, ``/suggestions`` and ``/review``
read the Oracle's process-wide singletons (``_bridge``, ``_cross_memory``,
``_writer``, ``_agent``) which a test process never starts — those are stubbed
onto the server module and restored in teardown, so this suite can run in the
same process as any other.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ["C3_ORACLE_API_KEY"] = "mobile-extras-key"

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import mobile_api  # noqa: E402

E1 = "2026-08-01T10:00:00"
E2 = "2026-08-02T10:00:00"
E3 = "2026-08-03T10:00:00"


class _StubScanner:
    def __init__(self, projects):
        self.projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self.projects]


class _StubBridge:
    model = "stub-model:1b"

    def is_available(self, timeout=3):
        return True

    def list_models(self):
        return ["stub-model:1b", "other:7b"]

    def has_model(self):
        return True


class _StubCrossMemory:
    """Mirrors ``CrossMemory``'s field names, not the mobile contract's —
    the mapping between the two is what ``_insight_row`` is under test for."""

    def __init__(self, insights):
        self.insights = insights

    def reload(self):
        pass

    def get_all_insights(self):
        return [i for i in self.insights if not i.get("dismissed")]

    def get_for_project(self, project_path):
        return [i for i in self.get_all_insights()
                if project_path in i.get("source_projects", [])]

    def stats(self):
        return {"total_insights": len(self.get_all_insights()),
                "by_type": {}, "total_links": 0}

    def dismiss(self, insight_id):
        for i in self.insights:
            if i["id"] == insight_id:
                i["dismissed"] = True
                return {"dismissed": True, "id": insight_id}
        return {"error": "Insight not found"}


class _StubWriter:
    def __init__(self, suggestions):
        self.suggestions = suggestions
        self.approved: list[str] = []

    def list_pending(self, project_path=None):
        rows = [s for s in self.suggestions if s.get("status") == "pending"]
        if project_path:
            rows = [s for s in rows if s.get("project_path") == project_path]
        return rows

    def approve_suggestion(self, suggestion_id):
        for s in self.suggestions:
            if s["id"] == suggestion_id and s["status"] == "pending":
                s["status"] = "approved"
                self.approved.append(suggestion_id)
                return {"approved": True, "id": suggestion_id,
                        "result": {"added": 1}}
        return {"error": "Suggestion not found or already resolved"}

    def dismiss_suggestion(self, suggestion_id):
        for s in self.suggestions:
            if s["id"] == suggestion_id and s["status"] == "pending":
                s["status"] = "dismissed"
                return {"dismissed": True, "id": suggestion_id}
        return {"error": "Suggestion not found or already resolved"}


class _StubAgent:
    status = {"running": True, "last_run": E3, "interval_seconds": 1800,
              "projects_tracked": 2, "digest_enabled": True,
              "last_digest_at": None}


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _edit(eid, ts, file, change_type, summary, version, branch="main"):
    return {"id": eid, "timestamp": ts, "session_id": "s1", "file": file,
            "change_type": change_type, "summary": summary,
            "lines_changed": [1, 2], "version": version,
            "git": {"commit": "abc123", "author": "dev", "subject": "wip",
                    "dirty": False, "branch": branch, "head_sha": "abc123"},
            "diff_summary": "+2 -0", "tags": ["auto"]}


class _OpsBase(unittest.TestCase):
    """Fixture projects + stubbed Oracle singletons, restored on teardown."""

    @classmethod
    def setUpClass(cls):
        # .resolve() is load-bearing, not tidiness: the API returns the path
        # the registry resolved, and mkdtemp hands back the UNRESOLVED spelling
        # — 8.3 short names on a Windows CI runner (RUNNER~1), /var for
        # /private/var on macOS. Comparing against the raw temp dir passes on
        # Linux and fails on both others.
        cls.tmp = Path(tempfile.mkdtemp(prefix="c3-mobile-extras-")).resolve()
        cls.alpha = cls.tmp / "alpha"
        cls.beta = cls.tmp / "beta"
        cls.outsider = cls.tmp / "outsider"   # never registered
        for p in (cls.alpha, cls.beta, cls.outsider):
            p.mkdir()
        for p in (cls.alpha, cls.beta):
            (p / ".c3").mkdir()

        _write_jsonl(cls.alpha / ".c3" / "edit_ledger.jsonl", [
            _edit("edit_1", E1, "a.py", "edit", "first", "v1"),
            _edit("edit_2", E2, "a.py", "edit", "second", "v2"),
            _edit("edit_3", E3, "b.py", "create", "new file", "v1",
                  branch="feature"),
        ])
        # Two sessions: one has hook-captured token stats, one does not, so
        # the join in /status is exercised on both halves.
        (cls.alpha / ".c3" / "sessions").mkdir(parents=True, exist_ok=True)
        for sid, started, calls in (("sess-a", E1, 3), ("sess-b", E2, 1)):
            with open(cls.alpha / ".c3" / "sessions" / f"session_{sid}.json",
                      "w", encoding="utf-8") as f:
                json.dump({"id": sid, "started": started, "ended": started,
                           "description": f"desc {sid}", "decisions": [],
                           "files_touched": [], "context_notes": [],
                           "tool_calls": [{"tool": "c3_read"}] * calls}, f)
        _write_jsonl(cls.alpha / ".c3" / "session_stats.jsonl", [
            {"ts": E1, "session_id": "sess-a", "stop_reason": "end_turn",
             "cost_usd": 0.25, "input_tokens": 100, "output_tokens": 40},
        ])
        # Distinct agent+title per row: the store collapses duplicates of a
        # pair on read, which would otherwise merge these three into one.
        _write_jsonl(cls.alpha / ".c3" / "notifications.jsonl", [
            {"id": "n1", "agent": "a1", "severity": "critical",
             "title": "t1", "message": "m1", "message_hash": "n1",
             "timestamp": E1, "last_seen": E1, "count": 1,
             "acknowledged": False},
            {"id": "n2", "agent": "a2", "severity": "warning",
             "title": "t2", "message": "m2", "message_hash": "n2",
             "timestamp": E2, "last_seen": E2, "count": 1,
             "acknowledged": False},
            {"id": "n3", "agent": "a3", "severity": "info",
             "title": "t3", "message": "m3", "message_hash": "n3",
             "timestamp": E3, "last_seen": E3, "count": 1,
             "acknowledged": True},
        ])

        cls._prior_key = os.environ.get("C3_ORACLE_API_KEY")
        os.environ["C3_ORACLE_API_KEY"] = "mobile-extras-key"
        srv.app.config["TESTING"] = True
        cls.client = srv.app.test_client()
        cls.auth = {"Authorization": "Bearer mobile-extras-key"}

    @classmethod
    def tearDownClass(cls):
        if cls._prior_key is None:
            os.environ.pop("C3_ORACLE_API_KEY", None)
        else:
            os.environ["C3_ORACLE_API_KEY"] = cls._prior_key
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._prior_cfg = srv._cfg
        srv._cfg = {
            "mobile_api_enabled": True,
            "api_rate_limit_per_min": 0,               # shared bucket off
            "mobile_security_rate_limit_per_min": 0,   # security bucket off
            "api_audit_enabled": False,
            "mobile_insights_write": True,
            "mobile_suggestions_write": True,
            # Nothing listens here; /status must degrade hub_available to
            # False rather than fail.
            "hub_url": "http://127.0.0.1:9",
        }
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None

        def entry(path, name):
            return {"path": str(path), "name": name, "tags": [],
                    "active": False, "has_c3": True, "fact_count": 0}

        mobile_api.init_services(
            scanner=_StubScanner([entry(self.alpha, "alpha"),
                                  entry(self.beta, "beta")]))

        self.insights = [
            {"id": "ins_1", "type": "pattern",
             "text": "Both projects retry on 429\nwith the same backoff.",
             "source_projects": [str(self.alpha), str(self.beta)],
             "source_fact_ids": {}, "confidence": 0.8, "created_at": E2,
             "last_reviewed": E2, "dismissed": False, "tags": []},
            {"id": "ins_2", "type": "risk", "text": "Beta pins an old lib.",
             "source_projects": [str(self.beta)], "source_fact_ids": {},
             "confidence": 0.5, "created_at": E1, "last_reviewed": E1,
             "dismissed": False, "tags": []},
        ]
        self.suggestions = [
            {"id": "sug_1", "project_path": str(self.alpha),
             "type": "archive_facts", "data": {"fact_ids": ["f1", "f2"]},
             "status": "pending", "created_at": E2, "resolved_at": None},
            {"id": "sug_2", "project_path": str(self.beta),
             "type": "add_fact", "data": {"fact": "Beta uses pnpm"},
             "status": "pending", "created_at": E1, "resolved_at": None},
        ]
        self.cross = _StubCrossMemory(self.insights)
        self.writer = _StubWriter(self.suggestions)

        self._prior = {n: getattr(srv, n, None)
                       for n in ("_bridge", "_cross_memory", "_writer",
                                 "_agent")}
        srv._bridge = _StubBridge()
        srv._cross_memory = self.cross
        srv._writer = self.writer
        srv._agent = _StubAgent()

    def tearDown(self):
        for name, value in self._prior.items():
            setattr(srv, name, value)
        srv._cfg = self._prior_cfg

    # helpers ------------------------------------------------------------

    def get(self, path, **kw):
        return self.client.get(path, headers=self.auth, **kw)

    def post(self, path, payload=None):
        return self.client.post(path, headers=self.auth, json=payload or {})

    def ok(self, path, **kw):
        r = self.get(path, **kw)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()


class TestInfoContract(_OpsBase):

    def test_info_reports_version_4_and_new_capabilities(self):
        body = self.ok("/api/mobile/info")
        self.assertEqual(body["api_version"], 4)
        for cap in ("edits", "locks", "status", "insights", "insights_write",
                    "suggestions", "suggestions_write", "review"):
            self.assertIn(cap, body["capabilities"])

    def test_info_drops_gated_write_caps_when_switched_off(self):
        srv._cfg["mobile_insights_write"] = False
        srv._cfg["mobile_suggestions_write"] = False
        body = self.ok("/api/mobile/info")
        self.assertNotIn("insights_write", body["capabilities"])
        self.assertNotIn("suggestions_write", body["capabilities"])
        # The READS stay: they are not in _CAPABILITY_SWITCHES at all.
        self.assertIn("insights", body["capabilities"])
        self.assertIn("suggestions", body["capabilities"])


class TestAuth(_OpsBase):
    """Every new route is bearer-gated, GETs included."""

    ROUTES = (
        ("GET", "/api/mobile/edits"),
        ("GET", "/api/mobile/edits/versions"),
        ("GET", "/api/mobile/locks"),
        ("GET", "/api/mobile/status"),
        ("GET", "/api/mobile/insights"),
        ("POST", "/api/mobile/insights/dismiss"),
        ("GET", "/api/mobile/suggestions"),
        ("POST", "/api/mobile/suggestions/decide"),
        ("GET", "/api/mobile/review"),
    )

    def test_401_without_bearer(self):
        for method, path in self.ROUTES:
            with self.subTest(path=path):
                r = (self.client.get(path) if method == "GET"
                     else self.client.post(path, json={}))
                self.assertEqual(r.status_code, 401)

    def test_401_with_wrong_bearer(self):
        for method, path in self.ROUTES:
            with self.subTest(path=path):
                bad = {"Authorization": "Bearer nope"}
                r = (self.client.get(path, headers=bad) if method == "GET"
                     else self.client.post(path, headers=bad, json={}))
                self.assertEqual(r.status_code, 401)


class TestEdits(_OpsBase):

    def test_edits_shape_and_order(self):
        body = self.ok("/api/mobile/edits",
                       query_string={"project": str(self.alpha)})
        self.assertEqual(body["path"], str(self.alpha))
        self.assertFalse(body["truncated"])
        self.assertEqual([e["id"] for e in body["entries"]],
                         ["edit_3", "edit_2", "edit_1"])
        row = body["entries"][0]
        self.assertEqual(
            set(row),
            {"id", "timestamp", "session_id", "file", "change_type", "summary",
             "lines_changed", "version", "git", "diff_summary", "tags"})
        self.assertEqual(
            set(row["git"]),
            {"branch", "commit", "subject", "author", "dirty", "head_sha"})
        self.assertEqual(row["git"]["branch"], "feature")
        self.assertEqual(body["stats"]["total"], 3)
        self.assertEqual(body["stats"]["files"], 2)
        self.assertEqual(body["stats"]["by_type"], {"edit": 2, "create": 1})

    def test_missing_fields_are_null_not_absent(self):
        """A bare pre-enrichment line still answers the full shape."""
        bare = self.beta / ".c3" / "edit_ledger.jsonl"
        _write_jsonl(bare, [{"id": "edit_bare", "timestamp": E1,
                             "file": "z.py"}])
        try:
            body = self.ok("/api/mobile/edits",
                           query_string={"project": str(self.beta)})
            row = body["entries"][0]
            self.assertIsNone(row["summary"])
            self.assertIsNone(row["session_id"])
            self.assertIsNone(row["lines_changed"])
            self.assertEqual(row["tags"], [])
            self.assertIsNone(row["git"]["branch"])
            self.assertFalse(row["git"]["dirty"])
        finally:
            bare.unlink()

    def test_edits_file_filter_accepts_relative_and_absolute(self):
        for arg in ("a.py", str(self.alpha / "a.py")):
            with self.subTest(file=arg):
                body = self.ok("/api/mobile/edits",
                               query_string={"project": str(self.alpha),
                                             "file": arg})
                self.assertEqual([e["id"] for e in body["entries"]],
                                 ["edit_2", "edit_1"])

    def test_edits_since_and_branch_filters(self):
        body = self.ok("/api/mobile/edits",
                       query_string={"project": str(self.alpha), "since": E2})
        self.assertEqual([e["id"] for e in body["entries"]],
                         ["edit_3", "edit_2"])
        body = self.ok("/api/mobile/edits",
                       query_string={"project": str(self.alpha),
                                     "branch": "feature"})
        self.assertEqual([e["id"] for e in body["entries"]], ["edit_3"])

    def test_edits_limit_and_truncation(self):
        body = self.ok("/api/mobile/edits",
                       query_string={"project": str(self.alpha), "limit": 2})
        self.assertEqual(len(body["entries"]), 2)
        self.assertTrue(body["truncated"])

    def test_edits_requires_registered_project(self):
        self.assertEqual(
            self.get("/api/mobile/edits").status_code, 400)
        self.assertEqual(
            self.get("/api/mobile/edits",
                     query_string={"project": str(self.outsider)}).status_code,
            404)

    def test_versions_newest_first(self):
        body = self.ok("/api/mobile/edits/versions",
                       query_string={"project": str(self.alpha),
                                     "file": "a.py"})
        self.assertEqual(body["file"], "a.py")
        self.assertEqual([v["version"] for v in body["versions"]],
                         ["v2", "v1"])
        self.assertEqual(
            set(body["versions"][0]),
            {"version", "id", "timestamp", "summary", "change_type",
             "lines_changed", "session_id"})

    def test_versions_requires_file(self):
        r = self.get("/api/mobile/edits/versions",
                     query_string={"project": str(self.alpha)})
        self.assertEqual(r.status_code, 400)


class TestLocks(_OpsBase):

    def _seed_lock(self, ttl=60.0):
        from services import agent_locks
        store = agent_locks.LockStore(str(self.alpha), ttl_s=ttl)
        return store.acquire([str(self.alpha / "a.py")], agent_id="agent-1",
                             session_id="sess-a", intent="refactor")

    def test_locks_shape(self):
        self._seed_lock()
        body = self.ok("/api/mobile/locks",
                       query_string={"project": str(self.alpha)})
        self.assertEqual(body["path"], str(self.alpha))
        self.assertEqual(body["count"], 1)
        row = body["locks"][0]
        self.assertEqual(
            set(row),
            {"key", "file", "agent_id", "session_id", "fencing_token",
             "intent", "acquired_at", "expires_at", "ttl_s", "expired"})
        self.assertEqual(row["agent_id"], "agent-1")
        self.assertEqual(row["session_id"], "sess-a")
        self.assertEqual(row["intent"], "refactor")
        self.assertEqual(row["file"], "a.py")
        self.assertEqual(row["key"], "a.py")
        self.assertFalse(row["expired"])
        # Epoch floats on disk, ISO on the wire.
        self.assertTrue(row["acquired_at"].endswith("+00:00"))
        self.assertAlmostEqual(row["ttl_s"], 60.0, delta=2.0)

    def test_locks_empty_when_none_held(self):
        body = self.ok("/api/mobile/locks",
                       query_string={"project": str(self.beta)})
        self.assertEqual(body, {"path": str(self.beta), "count": 0,
                                "locks": []})

    def test_locks_are_read_only(self):
        """No mutating verb exists — and reading never sweeps the store."""
        self._seed_lock()
        state = self.alpha / ".c3" / "locks.json"
        before = json.loads(state.read_text(encoding="utf-8"))
        self.ok("/api/mobile/locks", query_string={"project": str(self.alpha)})
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), before)
        for verb in (self.client.post, self.client.delete):
            r = verb("/api/mobile/locks", headers=self.auth, json={})
            self.assertEqual(r.status_code, 405)

    def test_expired_leases_are_not_listed(self):
        self._seed_lock(ttl=0.01)
        time.sleep(0.05)
        body = self.ok("/api/mobile/locks",
                       query_string={"project": str(self.alpha)})
        self.assertEqual(body["count"], 0)

    def test_locks_requires_registered_project(self):
        self.assertEqual(self.get("/api/mobile/locks").status_code, 400)
        self.assertEqual(
            self.get("/api/mobile/locks",
                     query_string={"project": str(self.outsider)}).status_code,
            404)

    def tearDown(self):
        for name in ("locks.json", "locks.lock"):
            (self.alpha / ".c3" / name).unlink(missing_ok=True)
        super().tearDown()


class TestStatus(_OpsBase):

    def test_server_wide_status(self):
        body = self.ok("/api/mobile/status")
        self.assertIsNone(body["project"])
        self.assertIsNone(body["sessions"])
        self.assertIsNone(body["notifications"])
        server = body["server"]
        self.assertEqual(server["api_version"], 4)
        self.assertIsInstance(server["c3_version"], str)
        self.assertFalse(server["hub_available"])
        self.assertEqual(server["ollama"], {
            "available": True, "model": "stub-model:1b",
            "has_model": True, "models_count": 2})

    def test_project_status_sessions_and_notifications(self):
        body = self.ok("/api/mobile/status",
                       query_string={"project": str(self.alpha)})
        self.assertEqual(body["project"],
                         {"path": str(self.alpha), "name": "alpha"})
        rows = {s["id"]: s for s in body["sessions"]["recent"]}
        self.assertEqual(set(rows), {"sess-a", "sess-b"})
        self.assertEqual(rows["sess-a"]["tool_calls"], 3)
        self.assertEqual(rows["sess-a"]["input_tokens"], 100)
        self.assertEqual(rows["sess-a"]["cost_usd"], 0.25)
        # No captured stat line for sess-b: zeros, not a missing key.
        self.assertEqual(rows["sess-b"]["input_tokens"], 0)
        self.assertEqual(rows["sess-b"]["cost_usd"], 0.0)
        self.assertEqual(rows["sess-b"]["description"], "desc sess-b")
        self.assertEqual(body["sessions"]["totals"], {
            "sessions": 2, "tool_calls": 4, "input_tokens": 100,
            "output_tokens": 40, "cost_usd": 0.25})
        self.assertEqual(body["notifications"],
                         {"unacked": 2,
                          "by_severity": {"critical": 1, "warning": 1}})

    def test_one_broken_source_degrades_only_its_key(self):
        """A failing Ollama bridge must not cost the client the whole card."""
        class _Boom:
            model = "x"

            def is_available(self, timeout=3):
                raise RuntimeError("no network")

        srv._bridge = _Boom()
        body = self.ok("/api/mobile/status",
                       query_string={"project": str(self.alpha)})
        self.assertIsNone(body["server"]["ollama"])
        self.assertIsNotNone(body["sessions"])
        self.assertIsNotNone(body["notifications"])

    def test_status_rejects_unregistered_project(self):
        r = self.get("/api/mobile/status",
                     query_string={"project": str(self.outsider)})
        self.assertEqual(r.status_code, 404)


class TestInsights(_OpsBase):

    def test_insights_shape_and_field_mapping(self):
        body = self.ok("/api/mobile/insights")
        self.assertEqual([i["id"] for i in body["insights"]],
                         ["ins_1", "ins_2"])
        row = body["insights"][0]
        self.assertEqual(
            set(row),
            {"id", "type", "title", "content", "confidence", "projects",
             "created_at", "dismissed"})
        # The store's `text` becomes `content`; `title` is its first line.
        self.assertEqual(row["title"], "Both projects retry on 429")
        self.assertTrue(row["content"].startswith("Both projects retry"))
        self.assertEqual(row["projects"],
                         [str(self.alpha), str(self.beta)])
        self.assertFalse(row["dismissed"])
        self.assertEqual(body["stats"]["total_insights"], 2)

    def test_insights_project_filter_and_limit(self):
        body = self.ok("/api/mobile/insights",
                       query_string={"project": str(self.beta)})
        self.assertEqual({i["id"] for i in body["insights"]},
                         {"ins_1", "ins_2"})
        body = self.ok("/api/mobile/insights", query_string={"limit": 1})
        self.assertEqual(len(body["insights"]), 1)

    def test_dismiss_round_trip(self):
        r = self.post("/api/mobile/insights/dismiss", {"id": "ins_2"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"dismissed": True})
        body = self.ok("/api/mobile/insights")
        self.assertEqual([i["id"] for i in body["insights"]], ["ins_1"])

    def test_dismiss_unknown_id_404s(self):
        r = self.post("/api/mobile/insights/dismiss", {"id": "ins_nope"})
        self.assertEqual(r.status_code, 404)

    def test_dismiss_requires_id(self):
        self.assertEqual(
            self.post("/api/mobile/insights/dismiss", {}).status_code, 400)

    def test_dismiss_404s_when_write_capability_off(self):
        srv._cfg["mobile_insights_write"] = False
        r = self.post("/api/mobile/insights/dismiss", {"id": "ins_2"})
        self.assertEqual(r.status_code, 404)
        # The read half is unaffected.
        self.assertEqual(self.get("/api/mobile/insights").status_code, 200)

    def test_no_generation_route_is_exposed(self):
        """Generation runs an LLM over every fact — deliberately absent."""
        rules = {str(r) for r in srv.app.url_map.iter_rules()}
        for path in ("/api/mobile/insights/generate",
                     "/api/mobile/insights/cross"):
            self.assertNotIn(path, rules)


class TestSuggestions(_OpsBase):

    def test_suggestions_shape_and_derived_summary(self):
        body = self.ok("/api/mobile/suggestions")
        self.assertEqual([s["id"] for s in body["suggestions"]],
                         ["sug_1", "sug_2"])
        row = body["suggestions"][0]
        self.assertEqual(
            set(row),
            {"id", "type", "project_path", "project_name", "summary", "data",
             "created_at"})
        self.assertEqual(row["project_name"], "alpha")
        self.assertEqual(row["summary"], "Archive 2 fact(s) in alpha")
        # The raw payload survives alongside the human string.
        self.assertEqual(row["data"], {"fact_ids": ["f1", "f2"]})
        self.assertEqual(body["suggestions"][1]["summary"],
                         "Add a fact to beta: Beta uses pnpm")

    def test_suggestions_project_filter(self):
        body = self.ok("/api/mobile/suggestions",
                       query_string={"project": str(self.beta)})
        self.assertEqual([s["id"] for s in body["suggestions"]], ["sug_2"])

    def test_dismiss_needs_no_confirmation(self):
        r = self.post("/api/mobile/suggestions/decide",
                      {"id": "sug_2", "decision": "dismiss"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["decision"], "dismiss")
        self.assertEqual(body["id"], "sug_2")
        self.assertTrue(body["result"]["dismissed"])
        self.assertEqual(self.writer.approved, [])

    def test_approve_without_confirmation_is_refused(self):
        r = self.post("/api/mobile/suggestions/decide",
                      {"id": "sug_1", "decision": "approve"})
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIs(body.get("needs_confirmation"), True)
        # The challenge IS the suggestion id — the same shape the client
        # already parses for mask activation and rule loosening.
        self.assertEqual(body["confirm_with"], "sug_1")
        self.assertEqual(self.writer.approved, [])

    def test_approve_with_wrong_confirmation_is_refused(self):
        r = self.post("/api/mobile/suggestions/decide",
                      {"id": "sug_1", "decision": "approve",
                       "confirm": "yes"})
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.get_json()["needs_confirmation"])
        self.assertEqual(self.writer.approved, [])

    def test_approve_with_typed_confirmation_succeeds(self):
        r = self.post("/api/mobile/suggestions/decide",
                      {"id": "sug_1", "decision": "approve",
                       "confirm": "sug_1"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["id"], "sug_1")
        self.assertEqual(body["decision"], "approve")
        self.assertTrue(body["result"]["approved"])
        self.assertEqual(self.writer.approved, ["sug_1"])

    def test_unknown_id_and_bad_decision(self):
        r = self.post("/api/mobile/suggestions/decide",
                      {"id": "sug_nope", "decision": "approve",
                       "confirm": "sug_nope"})
        self.assertEqual(r.status_code, 404)
        r = self.post("/api/mobile/suggestions/decide",
                      {"id": "sug_1", "decision": "maybe"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            self.post("/api/mobile/suggestions/decide",
                      {"decision": "dismiss"}).status_code, 400)

    def test_decide_404s_when_write_capability_off(self):
        srv._cfg["mobile_suggestions_write"] = False
        r = self.post("/api/mobile/suggestions/decide",
                      {"id": "sug_2", "decision": "dismiss"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.get("/api/mobile/suggestions").status_code, 200)

    def test_decide_consumes_the_security_budget(self):
        """Same tighter bucket the other mutating routes use.

        The limiter has a burst floor (``max(5, per_minute // 4)``), so a
        budget of 1 still allows an opening flurry — the assertion is that the
        route CONSUMES tokens and eventually 429s with the security budget
        named, not that it refuses on the second call.
        """
        srv._cfg["mobile_security_rate_limit_per_min"] = 1
        mobile_api._sec_limiter = None
        mobile_api._sec_limiter_key = None
        statuses = [
            self.post("/api/mobile/suggestions/decide",
                      {"id": "sug_nope", "decision": "dismiss"})
            for _ in range(8)
        ]
        codes = [r.status_code for r in statuses]
        self.assertIn(429, codes)
        limited = next(r for r in statuses if r.status_code == 429)
        self.assertEqual(limited.get_json()["budget"], "security")
        # The gate runs BEFORE the store is touched, so nothing was written.
        self.assertEqual(self.writer.approved, [])


class TestReview(_OpsBase):

    def test_review_status_shape(self):
        body = self.ok("/api/mobile/review")
        self.assertEqual(body, {"running": True, "last_run": E3,
                                "interval_seconds": 1800,
                                "digest_enabled": True})

    def test_review_degrades_when_agent_absent(self):
        srv._agent = None
        self.assertEqual(self.ok("/api/mobile/review"), {
            "running": False, "last_run": None, "interval_seconds": None,
            "digest_enabled": False})

    def test_no_control_routes_are_exposed(self):
        """run-now triggers LLM analysis — deliberately absent."""
        rules = {str(r) for r in srv.app.url_map.iter_rules()}
        for path in ("/api/mobile/review/start", "/api/mobile/review/stop",
                     "/api/mobile/review/run"):
            self.assertNotIn(path, rules)
        r = self.post("/api/mobile/review", {})
        self.assertEqual(r.status_code, 405)


if __name__ == "__main__":
    unittest.main()
