"""Tests for services/cred_telemetry.py — the credential usage history.

Stubbed keyring + temp scopes, same fixture family as the store tests.
The load-bearing assertions: events land in the OWNING scope's file,
decoded values never appear in the log, and corrupt lines never break
reads.
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

from services import cred_telemetry as ct
from services import credential_store as cs


class _StubKeyring:
    def __init__(self):
        self.store = {}

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


class TestCredTelemetry(unittest.TestCase):
    def setUp(self):
        self._stub = _StubKeyring()
        self._tmp_proj = tempfile.TemporaryDirectory()
        self._tmp_home = tempfile.TemporaryDirectory()
        self.project = self._tmp_proj.name
        self.home = Path(self._tmp_home.name)
        (Path(self.project) / ".c3").mkdir()
        (self.home / ".c3").mkdir()
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=self._stub),
            mock.patch.object(cs, "_crypto_module", return_value=_StubFernet),
            mock.patch.object(cs, "_global_base", return_value=self.home),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()
        cs.set_credential("PROJ_TOK", "proj-value-1", scope="project",
                          project_path=self.project)
        cs.set_credential("GLOB_TOK", "glob-value-1", scope="global",
                          project_path=self.project)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        cs._ACTIVE_SECRETS.clear()
        self._tmp_proj.cleanup()
        self._tmp_home.cleanup()

    def _proj_log(self):
        return Path(self.project) / ".c3" / "cred_usage.jsonl"

    def _home_log(self):
        return self.home / ".c3" / "cred_usage.jsonl"

    # ── placement ──
    def test_events_land_in_owning_scope(self):
        ct.record_use(["PROJ_TOK", "GLOB_TOK"], project_path=self.project,
                      action=ct.ACTION_INJECT, surface="shell",
                      cmd_preview="echo hi")
        proj_events = [json.loads(x) for x in
                       self._proj_log().read_text(encoding="utf-8").splitlines()]
        home_events = [json.loads(x) for x in
                       self._home_log().read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["name"] for e in proj_events], ["PROJ_TOK"])
        self.assertEqual([e["name"] for e in home_events], ["GLOB_TOK"])

    def test_unknown_name_is_a_noop(self):
        ct.record_use(["GHOST"], project_path=self.project)
        self.assertFalse(self._proj_log().exists())
        self.assertFalse(self._home_log().exists())

    def test_dotted_ref_splits_name_and_field(self):
        ct.record_use(["PROJ_TOK.number"], project_path=self.project,
                      action=ct.ACTION_TEMPLATE)
        ev = json.loads(self._proj_log().read_text(encoding="utf-8"))
        self.assertEqual(ev["name"], "PROJ_TOK")
        self.assertEqual(ev["field"], "number")
        self.assertEqual(ev["action"], "template")

    # ── hygiene ──
    def test_values_and_caps(self):
        ct.record_use(["PROJ_TOK"], project_path=self.project,
                      cmd_preview="deploy {{cred:PROJ_TOK}} " + "x" * 500,
                      exit_code=0)
        text = self._proj_log().read_text(encoding="utf-8")
        self.assertNotIn("proj-value-1", text)
        ev = json.loads(text)
        self.assertLessEqual(len(ev["cmd"]), 120)
        self.assertIn("{{cred:PROJ_TOK}}", ev["cmd"])
        self.assertEqual(ev["exit"], 0)

    def test_read_merges_scopes_and_tolerates_corrupt_lines(self):
        ct.record_use(["PROJ_TOK"], project_path=self.project)
        ct.record_use(["GLOB_TOK"], project_path=self.project)
        with open(self._proj_log(), "a", encoding="utf-8") as fh:
            fh.write("{not json\n\n[3]\n")
        events = ct.read_events(self.project)
        self.assertEqual(sorted(e["name"] for e in events),
                         ["GLOB_TOK", "PROJ_TOK"])

    def test_rotation(self):
        self._proj_log().write_text("x" * (ct._MAX_BYTES + 1),
                                    encoding="utf-8")
        ct.record_use(["PROJ_TOK"], project_path=self.project)
        self.assertTrue(self._proj_log().with_name(
            "cred_usage.jsonl.1").exists())
        events = ct.read_events(self.project)
        self.assertEqual([e["name"] for e in events], ["PROJ_TOK"])

    # ── readers ──
    def test_search_filters_and_honest_truncation(self):
        for i in range(5):
            ct.record_use(["PROJ_TOK"], project_path=self.project,
                          surface="shell", cmd_preview=f"run {i}")
        ct.record_use(["PROJ_TOK"], project_path=self.project,
                      action=ct.ACTION_REVEAL, surface="tool")
        res = ct.search_events(self.project, surface="shell", limit=2)
        self.assertEqual(len(res["events"]), 2)
        self.assertEqual(res["matched"], 5)
        self.assertTrue(res["truncated"])
        res2 = ct.search_events(self.project, action="reveal")
        self.assertEqual(res2["matched"], 1)

    def test_aggregate_counts_and_name_filter(self):
        ct.record_use(["PROJ_TOK", "PROJ_TOK.number"],
                      project_path=self.project, surface="shell")
        ct.record_use(["PROJ_TOK"], project_path=self.project,
                      action=ct.ACTION_REVEAL, surface="tool")
        agg = ct.aggregate(self.project)
        self.assertEqual(agg["total"], 3)
        self.assertEqual(agg["by_surface"], {"shell": 2, "tool": 1})
        row = agg["rows"][0]
        self.assertEqual(row["name"], "PROJ_TOK")
        self.assertEqual(row["hits"], 3)
        self.assertEqual(row["fields"], ["number"])
        only = ct.aggregate(self.project, name="PROJ_TOK")
        self.assertEqual(only["total"], 3)

    def test_clear_is_scope_explicit(self):
        ct.record_use(["PROJ_TOK"], project_path=self.project)
        ct.record_use(["GLOB_TOK"], project_path=self.project)
        removed = ct.clear(self.project)  # default: project only
        self.assertEqual(removed, 1)
        self.assertFalse(self._proj_log().exists())
        self.assertTrue(self._home_log().exists())


if __name__ == "__main__":
    unittest.main()
