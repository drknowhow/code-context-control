"""Tests for cli/tools/credentials.py — stubbed keyring/ledger/svc.

Covers the security contract: reveal denied unless the user set
agent_readable, the agent cannot raise agent_readable on an existing entry,
values never reach finalize args or ledger entries, and the vault is not
dispatchable through c3_project.
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli.tools.credentials import handle_credentials
from services import credential_store as cs


class _StubKeyring:
    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service, account, password):
        self.store[(service, account)] = password

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        if (service, account) not in self.store:
            raise KeyError("not found")
        del self.store[(service, account)]


class _StubFernet:
    def __init__(self, key):
        self._key = key

    @staticmethod
    def generate_key():
        return base64.urlsafe_b64encode(b"0" * 32)

    def encrypt(self, data):
        return base64.urlsafe_b64encode(self._key + b"|" + data)

    def decrypt(self, token):
        raw = base64.urlsafe_b64decode(token)
        key, _, data = raw.partition(b"|")
        if key != self._key:
            raise ValueError("bad key")
        return data


class _StubLedger:
    def __init__(self):
        self.edits: list[dict] = []

    def log_edit(self, **kw):
        self.edits.append(kw)


class _StubActivity:
    def __init__(self):
        self.events: list[tuple] = []

    def log(self, kind, data):
        self.events.append((kind, data))


class _Svc:
    def __init__(self, project_path):
        self.project_path = project_path
        self.edit_ledger = _StubLedger()
        self.activity_log = _StubActivity()


class TestCredentialsTool(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._tmp_proj = tempfile.TemporaryDirectory()
        self._tmp_home = tempfile.TemporaryDirectory()
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=self._stub),
            mock.patch.object(cs, "_crypto_module", return_value=_StubFernet),
            mock.patch.object(cs, "_global_base", return_value=Path(self._tmp_home.name)),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()
        self.svc = _Svc(self._tmp_proj.name)
        self.finalized: list[dict] = []

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        cs._ACTIVE_SECRETS.clear()
        self._tmp_proj.cleanup()
        self._tmp_home.cleanup()

    def _finalize(self, tool, args, resp, summary, **kw):
        self.finalized.append({"tool": tool, "args": args, "resp": resp,
                               "summary": summary})
        return resp

    def _call(self, action, **kw):
        return handle_credentials(action, self.svc, self._finalize, **kw)

    # ── basic actions ─────────────────────────────────────

    def test_list_empty(self):
        resp = self._call("list")
        self.assertIn("no credentials registered", resp)

    def test_set_then_list_and_describe(self):
        resp = self._call("set", name="API_KEY", value="canary-abc123",
                          description="test key")
        self.assertIn("[creds:set] API_KEY", resp)
        listing = self._call("list")
        self.assertIn("API_KEY", listing)
        self.assertIn("env_creds=", listing)
        described = self._call("describe", name="API_KEY")
        self.assertIn("injection-only", described)
        for out in (resp, listing, described):
            self.assertNotIn("canary-abc123", out)

    def test_check(self):
        self._call("set", name="OK", value="v")
        self.assertIn("resolvable=true", self._call("check", name="OK"))
        self.assertIn("[creds:unknown]", self._call("check", name="GHOST"))

    def test_unknown_action_and_missing_name(self):
        self.assertIn("[creds:unknown-action]", self._call("frobnicate"))
        self.assertIn("name is required", self._call("describe"))

    # ── reveal gate ───────────────────────────────────────

    def test_reveal_denied_by_default(self):
        self._call("set", name="LOCKED", value="hidden-v")
        resp = self._call("reveal", name="LOCKED")
        self.assertIn("[creds:not-readable]", resp)
        self.assertNotIn("hidden-v", resp)

    def test_reveal_allowed_when_flag_set_at_creation(self):
        self._call("set", name="OPEN", value="visible-v", agent_readable=True)
        resp = self._call("reveal", name="OPEN")
        self.assertIn("[creds:reveal]", resp)
        self.assertIn("visible-v", resp)
        self.assertIn("visible-v", cs._ACTIVE_SECRETS.values())
        kinds = [e["change_type"] for e in self.svc.edit_ledger.edits]
        self.assertIn("cred_reveal", kinds)

    def test_set_cannot_raise_agent_readable_on_existing_entry(self):
        self._call("set", name="PROD", value="v1")
        resp = self._call("set", name="PROD", value="v2", agent_readable=True)
        self.assertIn("[creds:not-allowed]", resp)
        entry = cs.get_entry("PROD", project_path=self.svc.project_path)
        self.assertFalse(entry["agent_readable"])
        self.assertEqual(cs.get_value("PROD", project_path=self.svc.project_path), "v1")

    # ── delete ────────────────────────────────────────────

    def test_delete_resolves_owning_scope(self):
        self._call("set", name="D", value="v")
        resp = self._call("delete", name="D")
        self.assertIn("[creds:deleted] D (scope=project)", resp)
        self.assertIn("[creds:unknown]", self._call("delete", name="D"))
        kinds = [e["change_type"] for e in self.svc.edit_ledger.edits]
        self.assertIn("cred_delete", kinds)

    # ── leak canaries ─────────────────────────────────────

    def test_value_never_in_finalize_args_or_ledger(self):
        canary = "canary-value-zq9"
        self._call("set", name="C", value=canary, agent_readable=True)
        self._call("list")
        self._call("describe", name="C")
        self._call("check", name="C")
        self._call("delete", name="C")
        for record in self.finalized:
            self.assertNotIn(canary, json.dumps(record["args"]))
            self.assertNotIn(canary, record["summary"])
        self.assertNotIn(canary, json.dumps(self.svc.edit_ledger.edits))
        self.assertNotIn(canary, json.dumps(self.svc.activity_log.events))

    # ── structured kinds ──────────────────────────────────

    PAN = "4242424242424242"
    CARD = json.dumps({"cardholder": "D T", "number": PAN,
                       "expiry": "12/27", "cvc": "123"})

    def test_structured_set_describe_and_check(self):
        resp = self._call("set", name="VISA", value=self.CARD, ctype="card")
        self.assertIn("[creds:set]", resp)
        listing = self._call("list")
        self.assertIn("visa 4242", listing)
        self.assertNotIn(self.PAN, listing)
        desc = self._call("describe", name="VISA")
        self.assertIn("fields: cardholder, cvc, expiry, number", desc)
        self.assertIn("VISA.cardholder", desc)
        self.assertIn("inject-only", desc)
        self.assertNotIn(self.PAN, desc)
        check = self._call("check", name="VISA")
        self.assertIn("resolvable=true", check)

    def test_structured_reveal_permanently_refused(self):
        self._call("set", name="VISA", value=self.CARD, ctype="card")
        resp = self._call("reveal", name="VISA")
        self.assertTrue(resp.startswith("[creds:structured]"))
        self.assertNotIn(self.PAN, resp)
        # not ledger-logged as a reveal
        self.assertFalse([e for e in self.svc.edit_ledger.edits
                          if e.get("change_type") == "cred_reveal"])

    def test_structured_reveal_refused_even_with_hostile_flag(self):
        self._call("set", name="VISA", value=self.CARD, ctype="card")
        cfg_path = Path(self.svc.project_path) / ".c3" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["credentials"]["entries"]["VISA"]["agent_readable"] = True
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        resp = self._call("reveal", name="VISA")
        self.assertTrue(resp.startswith("[creds:structured]"))
        self.assertNotIn(self.PAN, resp)

    def test_structured_flags_refused_at_set(self):
        resp = self._call("set", name="VISA", value=self.CARD, ctype="card",
                          inject=True)
        self.assertIn("[creds:error]", resp)
        self.assertIn("inject-only", resp)

    def test_structured_payload_never_in_finalize_or_ledger(self):
        self._call("set", name="VISA", value=self.CARD, ctype="card")
        self._call("list")
        self._call("describe", name="VISA")
        self._call("check", name="VISA")
        self._call("reveal", name="VISA")
        # invalid payload too: error path must not echo submitted content
        bad = json.dumps({"cardholder": "x", "number": "4242424242424241",
                          "expiry": "12/27"})
        self._call("set", name="V2", value=bad, ctype="card")
        blob = json.dumps([r["args"] for r in self.finalized]) + \
            json.dumps([r["resp"] for r in self.finalized
                        if "[creds:error]" in r["resp"]]) + \
            json.dumps(self.svc.edit_ledger.edits) + \
            json.dumps(self.svc.activity_log.events)
        self.assertNotIn(self.PAN, blob)
        self.assertNotIn("4242424242424241", blob)

    def test_reveal_records_usage_event(self):
        canary = "reveal-me-zq1"
        self._call("set", name="R", value=canary, agent_readable=True)
        self._call("reveal", name="R")
        log = Path(self.svc.project_path) / ".c3" / "cred_usage.jsonl"
        text = log.read_text(encoding="utf-8")
        self.assertNotIn(canary, text)
        ev = json.loads(text.splitlines()[-1])
        self.assertEqual((ev["name"], ev["action"], ev["surface"]),
                         ("R", "reveal", "tool"))

    # ── usage action ──────────────────────────────────────

    def test_usage_action_reports_and_scopes_foreign_projects(self):
        from services import cred_telemetry as ct
        self._call("set", name="G", value="glob-v", scope="global")
        # a use from THIS project, and one from another project (global
        # creds share ~/.c3, so both land in the same log)
        ct.record_use(["G"], project_path=self.svc.project_path,
                      surface="shell", cmd_preview="local {{cred:G}}")
        with tempfile.TemporaryDirectory() as other:
            ct.record_use(["G"], project_path=other, surface="shell",
                          cmd_preview="foreign-secret-workflow {{cred:G}}")
        resp = self._call("usage", name="G")
        self.assertIn("[creds:usage] G — 2 use(s)", resp)
        self.assertIn("local {{cred:G}}", resp)
        # H15: another project's cmd previews never reach this surface
        self.assertNotIn("foreign-secret-workflow", resp)
        self.assertIn("other projects: 1 use(s)", resp)
        # empty case + overview form
        self.assertIn("no recorded uses", self._call("usage", name="NOPE"))
        self.assertIn("use(s) across", self._call("usage"))

    # ── federation exclusion ──────────────────────────────

    def test_c3_project_has_no_credentials_verb(self):
        from cli.tools import project as project_tool
        ops = (project_tool._WRITE_OPS | project_tool._READ_OPS
               | project_tool._DISCOVERY_OPS | project_tool._SUB_OPS
               | project_tool._MEMORY_WRITE)
        self.assertFalse({"credentials", "creds", "credential", "reveal"} & ops)


if __name__ == "__main__":
    unittest.main()
