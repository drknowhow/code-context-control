"""Hub override-approval routes — the desktop half of Override Requests (P5).

Flask test client + temp project + patched stores — fully offline. The
load-bearing assertions:

- the typed-glob challenge for access_deny/access_builtin approvals is
  enforced server-side (the UI computes it too, but only decide() enforces);
- a lapsed/decided request answers 409, never a silently minted grant;
- deny+mute writes the mute row so the same session cannot re-ask;
- the audit trail carries identifiers only — the agent-supplied
  justification (untrusted text) never lands in the activity log or the
  edit ledger.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

import cli.hub_server as hub_server  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import override_grants as og  # noqa: E402
from services import override_policy as opol  # noqa: E402
from services import override_requests as orq  # noqa: E402

SESSION = "sess-hub"
CANARY = "justification-canary-9f2k should never reach an audit surface"


class HubOverrideBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        (self.proj / "infra").mkdir()
        (self.proj / "secrets").mkdir()
        self.held = self.proj / "infra" / "main.tf"
        self.held.write_text("resource {}", encoding="utf-8")
        self.blocked = self.proj / "secrets" / "key.txt"
        self.blocked.write_text("k", encoding="utf-8")
        self.write_config()

        self._store = Path(self._tmp.name) / "override_requests.json"
        self._mutes = Path(self._tmp.name) / "override_mutes.json"
        self._patches = [
            mock.patch.object(opol, "_global_base", return_value=None),
            mock.patch.object(ag, "_global_base", return_value=None),
            mock.patch.object(orq, "store_path", return_value=self._store),
            mock.patch.object(orq, "mutes_path", return_value=self._mutes),
        ]
        for p in self._patches:
            p.start()
        self.client = hub_server.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def write_config(self, *, override=None):
        data = {"access": {"confirm": ["infra/**"],
                           "deny": ["secrets/**"]}}
        if override is not None:
            data["override"] = override
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps(data), encoding="utf-8")

    def confirm_request(self):
        """A pending access_confirm request (no override section needed)."""
        denial = ag.check(str(self.held), "write", str(self.proj))
        return orq.create(str(self.proj), session_id=SESSION, tool="c3_edit",
                          op="write", path=str(self.held), denial=denial,
                          justification=CANARY)

    def deny_request(self):
        """A pending access_deny request (needs the override section on)."""
        self.write_config(override={"enabled": True,
                                    "layers": {k: True
                                               for k in opol.LAYER_KEYS}})
        denial = ag.check(str(self.blocked), "read", str(self.proj))
        return orq.create(str(self.proj), session_id=SESSION, tool="Read",
                          op="read", path=str(self.blocked), denial=denial,
                          justification=CANARY)

    def decide(self, request_id, body):
        return self.client.post(f"/api/hub/overrides/{request_id}", json=body)


class TestList(HubOverrideBase):
    def test_lists_pending_with_decision_context(self):
        row = self.confirm_request()
        resp = self.client.get("/api/hub/overrides?status=pending")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["count"], 1)
        entry = data["requests"][0]
        self.assertEqual(entry["id"], row["id"])
        self.assertEqual(entry["rule_class"], opol.LAYER_ACCESS_CONFIRM)
        self.assertFalse(entry["needs_typed_confirm"])
        self.assertEqual(entry["confirm_with"], "")
        self.assertTrue(entry["escalatable"])
        self.assertEqual(entry["project_name"], "proj")
        # Grant-matching identity stays server-side.
        self.assertNotIn("path_key", entry)

    def test_deny_class_row_demands_typed_confirm(self):
        self.deny_request()
        entry = self.client.get(
            "/api/hub/overrides?status=pending").get_json()["requests"][0]
        self.assertTrue(entry["needs_typed_confirm"])
        self.assertEqual(entry["confirm_with"], "secrets/**")


class TestDecide(HubOverrideBase):
    def test_approve_confirm_class_is_one_tap(self):
        row = self.confirm_request()
        resp = self.decide(row["id"], {"decision": "approve"})
        self.assertEqual(resp.status_code, 200)
        out = resp.get_json()["request"]
        self.assertEqual(out["status"], orq.STATUS_APPROVED)
        self.assertEqual(out["decided_by"], "desktop")
        self.assertTrue(out["grant_id"].startswith("grt_"))
        # The grant is real and consumable by the asking session.
        grant = og.consume(str(self.proj), session_id=SESSION,
                           layer=opol.GATE_ACCESS, rule="infra/**",
                           tool="c3_edit", op="write", path=str(self.held))
        self.assertIsNotNone(grant)

    def test_approve_deny_class_needs_the_rule_retyped(self):
        row = self.deny_request()
        resp = self.decide(row["id"], {"decision": "approve"})
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertTrue(body["needs_confirmation"])
        self.assertEqual(body["confirm_with"], "secrets/**")
        # Still pending — the refusal decided nothing.
        self.assertEqual(orq.get(row["id"])["status"], orq.STATUS_PENDING)

        resp = self.decide(row["id"], {"decision": "approve",
                                       "confirm": "secrets/**"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["request"]["status"],
                         orq.STATUS_APPROVED)

    def test_decided_request_answers_409(self):
        row = self.confirm_request()
        self.decide(row["id"], {"decision": "deny"})
        resp = self.decide(row["id"], {"decision": "approve"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["request"]["status"],
                         orq.STATUS_DENIED)

    def test_deny_mute_suppresses_the_session(self):
        row = self.confirm_request()
        resp = self.decide(row["id"], {"decision": "deny", "mute": True})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(orq.is_muted(
            str(self.proj), session_id=SESSION, layer=opol.GATE_ACCESS,
            rule="infra/**", tool="c3_edit", op="write",
            path_key=og.path_key(str(self.held), str(self.proj))))

    def test_unknown_id_is_404(self):
        self.assertEqual(
            self.decide("ovr_nope", {"decision": "deny"}).status_code, 404)

    def test_bad_decision_is_400(self):
        row = self.confirm_request()
        self.assertEqual(
            self.decide(row["id"], {"decision": "maybe"}).status_code, 400)


class TestAudit(HubOverrideBase):
    def _audit_text(self):
        parts = []
        activity = self.proj / ".c3" / "activity_log.jsonl"
        if activity.is_file():
            parts.append(activity.read_text(encoding="utf-8"))
        for ledger in (self.proj / ".c3").rglob("*.jsonl"):
            parts.append(ledger.read_text(encoding="utf-8"))
        return "\n".join(parts)

    def test_audit_carries_ids_never_the_justification(self):
        row = self.confirm_request()
        self.decide(row["id"], {"decision": "approve"})
        text = self._audit_text()
        self.assertIn(row["id"], text)
        self.assertNotIn(CANARY, text)


class TestAccessView(HubOverrideBase):
    def test_rules_and_policy_for_one_project(self):
        resp = self.client.get(
            f"/api/hub/access?path={self.proj}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["rules"]["project"]["confirm"], ["infra/**"])
        self.assertEqual(data["rules"]["project"]["deny"], ["secrets/**"])
        self.assertIn(opol.LAYER_ACCESS_CONFIRM, data["policy"]["layers"])
        # The wake argv never crosses the wire (spec §3.1 note).
        self.assertNotIn("wake", data["policy"])
        self.assertIn("wake_configured", data["policy"])

    def test_path_is_required_and_validated(self):
        self.assertEqual(
            self.client.get("/api/hub/access").status_code, 400)
        self.assertEqual(
            self.client.get("/api/hub/access?path=Q:/nope/never").status_code,
            404)


if __name__ == "__main__":
    unittest.main()
