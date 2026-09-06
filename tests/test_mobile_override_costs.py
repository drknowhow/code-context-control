"""GET /api/mobile/overrides/costs — the "this rule is costing you" route.

A read over the same store the inbox serves, so it inherits the inbox's
posture: Bearer on every method, dark when `override` is off, and a
cross-project answer that never names a project the scanner does not
register (the store is one file for the whole machine). The desktop client
codes against the `{days, rules}` shape and the `suggestion` strings here.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("C3_ORACLE_API_KEY", "mobile-override-key")

import oracle.oracle_server as srv  # noqa: E402
from oracle.services import mobile_api  # noqa: E402
from services import override_costs as oc  # noqa: E402
from services import override_requests as orq  # noqa: E402


class _StubScanner:
    def __init__(self, projects):
        self.projects = projects

    def discover(self, force=False):
        return [dict(p) for p in self.projects]


def _entry(path):
    return {"path": str(path), "name": Path(path).name, "tags": [],
            "active": False, "has_c3": True, "fact_count": 0}


class CostsRouteBase(unittest.TestCase):
    """Temp projects + a temp request store written directly (no policy
    needed: the route reads rows, it never files or decides one)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        self.other = Path(self._tmp.name) / "other"
        self.ghost = Path(self._tmp.name) / "ghost"   # never registered
        for p in (self.proj, self.other, self.ghost):
            (p / ".c3").mkdir(parents=True)
        self.store = Path(self._tmp.name) / "override_requests.json"
        self._patch = mock.patch.object(orq, "store_path",
                                        return_value=self.store)
        self._patch.start()

        self._prior_cfg = srv._cfg
        srv._cfg = {
            "mobile_api_enabled": True,
            "api_rate_limit_per_min": 0,
            "mobile_security_rate_limit_per_min": 0,
            "api_audit_enabled": False,
            "mobile_override_enabled": True,
            "mobile_override_write": True,
        }
        mobile_api.init_services(scanner=_StubScanner(
            [_entry(self.proj), _entry(self.other)]))
        srv.app.config["TESTING"] = True
        self.client = srv.app.test_client()
        self.auth = {"Authorization": "Bearer " + os.environ["C3_ORACLE_API_KEY"]}
        self.n = 0

    def tearDown(self):
        self._patch.stop()
        srv._cfg = self._prior_cfg
        self._tmp.cleanup()

    def get(self, path):
        return self.client.get(path, headers=self.auth)

    def seed(self, rows):
        self.store.write_text(json.dumps(rows), encoding="utf-8")

    def row(self, project, rule="**/.claude/skills/**", status="approved",
            age=timedelta(hours=1)):
        self.n += 1
        created = datetime.now(timezone.utc) - age
        proj = str(project).replace("\\", "/")
        return {
            "id": f"ovr_{self.n:06x}", "project_path": proj,
            "session_id": "s1", "created_at": created.isoformat(),
            "expires_at": (created + timedelta(minutes=10)).isoformat(),
            "status": status, "layer": "access", "rule": rule,
            "rule_class": "access_confirm", "scope": "project",
            "tool": "Edit", "op": "write", "path": f"{proj}/x{self.n}.md",
            "path_key": "", "refusal": "", "justification": "",
            "resolved_at": None, "decided_by": None, "decision_note": None,
        }


class TestCapabilityAndAuth(CostsRouteBase):
    def test_capability_is_advertised_and_follows_the_override_switch(self):
        info = self.get("/api/mobile/info").get_json()
        self.assertIn("override_costs", info["capabilities"])
        srv._cfg["mobile_override_enabled"] = False
        info = self.get("/api/mobile/info").get_json()
        self.assertNotIn("override_costs", info["capabilities"])
        self.assertNotIn("override", info["capabilities"])

    def test_api_version_is_not_bumped(self):
        # Additive: an older client must not be told the API moved.
        self.assertEqual(mobile_api.API_VERSION, 5)

    def test_switch_off_is_404(self):
        srv._cfg["mobile_override_enabled"] = False
        self.assertEqual(self.get("/api/mobile/overrides/costs").status_code, 404)

    def test_no_bearer_is_401(self):
        self.assertEqual(
            self.client.get("/api/mobile/overrides/costs").status_code, 401)

    def test_wrong_bearer_is_401(self):
        r = self.client.get("/api/mobile/overrides/costs",
                            headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)


class TestShape(CostsRouteBase):
    def test_empty_store_is_an_empty_list(self):
        body = self.get("/api/mobile/overrides/costs").get_json()
        self.assertEqual(body["days"], 7)
        self.assertEqual(body["rules"], [])
        self.assertEqual(body["count"], 0)

    def test_rows_carry_the_contract_fields_and_suggestion(self):
        self.seed([self.row(self.proj) for _ in range(3)]
                  + [self.row(self.proj, rule="secrets/**", status="denied"),
                     self.row(self.proj, rule="secrets/**", status="denied")])
        body = self.get("/api/mobile/overrides/costs").get_json()
        self.assertEqual(body["days"], 7)
        self.assertEqual(body["count"], 2)
        first, second = body["rules"]
        # jsonify sorts keys; the contract is the SET of names (order is the
        # service's business, tests/test_override_costs.py pins it).
        self.assertEqual(set(first.keys()), set(oc.FIELDS))
        self.assertEqual(set(oc.FIELDS), {
            "project_path", "rule", "rule_class", "layer", "count",
            "approved", "denied", "expired", "pending", "last_at",
            "suggestion"})
        self.assertEqual(first["rule"], "**/.claude/skills/**")
        self.assertEqual((first["count"], first["approved"]), (3, 3))
        self.assertEqual(first["suggestion"], "convert to allow")
        self.assertEqual(second["rule"], "secrets/**")
        self.assertEqual(second["denied"], 2)
        self.assertEqual(second["suggestion"], "tighten or deny")

    def test_path_key_and_justification_never_cross_the_wire(self):
        self.seed([self.row(self.proj)])
        text = json.dumps(self.get("/api/mobile/overrides/costs").get_json())
        self.assertNotIn("path_key", text)
        self.assertNotIn("justification", text)

    def test_costs_is_not_swallowed_by_the_id_route(self):
        # A request literally named "costs" would 404 as unknown; the static
        # rule must win so the answer is the aggregation.
        body = self.get("/api/mobile/overrides/costs").get_json()
        self.assertIn("rules", body)
        self.assertNotIn("error", body)


class TestProjectScope(CostsRouteBase):
    def test_project_filter(self):
        self.seed([self.row(self.proj), self.row(self.other, rule="b/**")])
        body = self.get(f"/api/mobile/overrides/costs?project={self.proj}").get_json()
        self.assertEqual([r["rule"] for r in body["rules"]], ["**/.claude/skills/**"])
        self.assertEqual(body["project"], str(self.proj.resolve()))

    def test_unknown_project_is_404(self):
        self.assertEqual(
            self.get(f"/api/mobile/overrides/costs?project={self.ghost}").status_code,
            404)
        nowhere = Path(self._tmp.name) / "nowhere"
        self.assertEqual(
            self.get(f"/api/mobile/overrides/costs?project={nowhere}").status_code,
            404)

    def test_omitting_project_returns_every_registered_project(self):
        self.seed([self.row(self.proj), self.row(self.other, rule="b/**")])
        body = self.get("/api/mobile/overrides/costs").get_json()
        self.assertEqual(sorted(r["rule"] for r in body["rules"]),
                         ["**/.claude/skills/**", "b/**"])

    def test_unregistered_project_rows_never_leak(self):
        # The ghost project exists on disk and has rows in the shared store;
        # the scanner does not serve it, so the gateway must not name it.
        self.seed([self.row(self.ghost, rule="ghost/**") for _ in range(4)]
                  + [self.row(self.proj)])
        body = self.get("/api/mobile/overrides/costs").get_json()
        text = json.dumps(body)
        self.assertNotIn("ghost", text)
        self.assertEqual([r["rule"] for r in body["rules"]], ["**/.claude/skills/**"])

    def test_rows_for_a_dropped_project_are_filtered_too(self):
        self.seed([self.row(self.other, rule="b/**")])
        mobile_api.init_services(scanner=_StubScanner([_entry(self.proj)]))
        body = self.get("/api/mobile/overrides/costs").get_json()
        self.assertEqual(body["rules"], [])


class TestDaysClamp(CostsRouteBase):
    def test_default_is_seven(self):
        self.assertEqual(self.get("/api/mobile/overrides/costs").get_json()["days"], 7)

    def test_within_range_is_honoured(self):
        self.assertEqual(
            self.get("/api/mobile/overrides/costs?days=14").get_json()["days"], 14)

    def test_above_thirty_clamps_to_thirty(self):
        self.assertEqual(
            self.get("/api/mobile/overrides/costs?days=365").get_json()["days"], 30)

    def test_below_one_clamps_to_one(self):
        self.assertEqual(
            self.get("/api/mobile/overrides/costs?days=0").get_json()["days"], 1)
        self.assertEqual(
            self.get("/api/mobile/overrides/costs?days=-3").get_json()["days"], 1)

    def test_garbage_is_the_default(self):
        self.assertEqual(
            self.get("/api/mobile/overrides/costs?days=soon").get_json()["days"], 7)

    def test_the_window_actually_moves(self):
        self.seed([self.row(self.proj, age=timedelta(days=10))])
        self.assertEqual(
            self.get("/api/mobile/overrides/costs").get_json()["rules"], [])
        self.assertEqual(
            self.get("/api/mobile/overrides/costs?days=30").get_json()["count"], 1)


class TestStoreTrouble(CostsRouteBase):
    def test_corrupt_store_is_an_empty_list_not_a_500(self):
        self.store.write_text("{nope", encoding="utf-8")
        r = self.get("/api/mobile/overrides/costs")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["rules"], [])


if __name__ == "__main__":
    unittest.main()
