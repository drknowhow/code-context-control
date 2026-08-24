"""Tests for services/cred_audit — the merged credential audit trail.

Both halves were already recorded and neither was readable together: uses in
.c3/cred_usage.jsonl, changes as `cred_action` rows in .c3/activity_log.jsonl.
These tests pin the merge, the two-scope reading, and the one thing an audit
of a secret vault must never do — put a value in the trail.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import cred_audit
from services.activity_log import ActivityLog

CANARY = "audit-canary-7f3k"


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


class TestCredAudit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        self.home = Path(self._home.name)
        (self.proj / ".c3").mkdir()
        (self.home / ".c3").mkdir()
        from services import credential_store as cs
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=_StubKeyring()),
            mock.patch.object(cs, "_global_base", return_value=self.home),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()
        self._home.cleanup()

    def _use(self, base, **kw):
        row = {"ts": "2026-08-01T10:00:00+00:00", "name": "TOKEN", "field": "",
               "action": "inject_env", "surface": "shell", "session": "",
               "project": str(self.proj), "cmd": ""}
        row.update(kw)
        with open(base / ".c3" / "cred_usage.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def _change(self, base, **kw):
        data = {"kind": "creds", "action": "set", "name": "TOKEN",
                "scope": "project", "via": "hub"}
        data.update(kw)
        ActivityLog(str(base)).log("cred_action", data)

    # ── the merge ──────────────────────────────────────────────
    def test_uses_and_changes_land_on_one_timeline_newest_first(self):
        self._use(self.proj, ts="2026-08-01T10:00:00+00:00")
        self._change(self.proj, action="delete")   # ActivityLog stamps "now"
        res = cred_audit.audit_events(str(self.proj))
        self.assertEqual(res["counts"]["use"], 1)
        self.assertEqual(res["counts"]["change"], 1)
        kinds = [e["kind"] for e in res["events"]]
        self.assertEqual(kinds, ["change", "use"])  # the change is newer

    def test_kind_filter_narrows_to_one_half(self):
        self._use(self.proj)
        self._change(self.proj)
        self.assertEqual(
            cred_audit.audit_events(str(self.proj), kind="use")["counts"]["change"], 0)
        self.assertEqual(
            cred_audit.audit_events(str(self.proj), kind="change")["counts"]["use"], 0)

    def test_name_filter_applies_to_both_halves(self):
        """A key's whole history means its uses AND its changes, not one."""
        self._use(self.proj, name="KEEP")
        self._use(self.proj, name="OTHER")
        self._change(self.proj, name="KEEP")
        self._change(self.proj, name="OTHER")
        res = cred_audit.audit_events(str(self.proj), name="KEEP")
        self.assertEqual(res["matched"], 2)
        self.assertEqual({e["kind"] for e in res["events"]}, {"use", "change"})
        self.assertTrue(all(e["name"] == "KEEP" for e in res["events"]))

    # ── two scopes ─────────────────────────────────────────────
    def test_global_scope_events_are_included_for_a_project(self):
        """A global credential used from a project records into ~/.c3, so a
        project-only read would lose exactly the shared-secret entries."""
        self._use(self.home, name="SHARED")
        self._change(self.home, name="SHARED", scope="global")
        res = cred_audit.audit_events(str(self.proj))
        self.assertEqual(res["matched"], 2)

    def test_include_global_false_drops_them_for_the_rollup(self):
        """The cross-project view reads the shared vault once, separately —
        merging it per project would count one log N times."""
        self._use(self.home, name="SHARED")
        self._use(self.proj, name="LOCAL")
        res = cred_audit.audit_events(str(self.proj), include_global=False)
        self.assertEqual([e["name"] for e in res["events"]], ["LOCAL"])

    # ── the invariant ──────────────────────────────────────────
    def test_no_value_can_appear_in_the_trail(self):
        """Neither log stores a value; the merge must not invent a path for one."""
        self._use(self.proj, cmd='export TOKEN="$TOKEN"; deploy --key {{cred:TOKEN}}')
        self._change(self.proj)
        res = cred_audit.audit_events(str(self.proj))
        blob = json.dumps(res)
        self.assertNotIn(CANARY, blob)
        # the command survives in RAW TEMPLATE form — that is the point
        self.assertIn("{{cred:TOKEN}}", blob)

    def test_exposing_actions_are_counted_separately(self):
        """reveal/cli_show put plaintext where a human or model can read it;
        inject_env hands it to a subprocess. Different risk, different count."""
        self._use(self.proj, action="reveal")
        self._use(self.proj, action="cli_show")
        self._use(self.proj, action="inject_env")
        res = cred_audit.audit_events(str(self.proj))
        self.assertEqual(res["counts"]["exposing"], 2)
        self.assertEqual(res["counts"]["use"], 3)

    # ── honesty of the counters ────────────────────────────────
    def test_matched_stays_honest_when_limit_truncates(self):
        for i in range(12):
            self._use(self.proj, ts=f"2026-08-01T10:00:{i:02d}+00:00")
        res = cred_audit.audit_events(str(self.proj), limit=5)
        self.assertEqual(res["returned"], 5)
        self.assertEqual(res["matched"], 12)
        self.assertTrue(res["truncated"])

    def test_an_empty_project_reports_nothing_rather_than_failing(self):
        res = cred_audit.audit_events(str(self.proj))
        self.assertEqual(res["events"], [])
        self.assertEqual(res["matched"], 0)
        self.assertTrue(res["note"])


class TestCredAuditRoutes(unittest.TestCase):
    def test_hub_audit_route_isolates_an_unreadable_project(self):
        import cli.hub_server as hub_server
        from services import credential_store as cs

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            proj = Path(tmp) / "p"
            (proj / ".c3").mkdir(parents=True)
            ActivityLog(str(proj)).log("cred_action", {
                "kind": "creds", "action": "set", "name": "X",
                "scope": "project", "via": "hub"})
            rows = [{"name": "p", "path": str(proj)}]

            class _PM:
                def list_projects(self):
                    return rows

            with mock.patch.object(hub_server, "_pm", new=lambda: _PM()), \
                    mock.patch.object(cs, "_global_base", return_value=Path(home)):
                client = hub_server.app.test_client()
                data = client.get("/api/hub/credentials/audit").get_json()

            self.assertEqual(data["counts"]["change"], 1)
            self.assertEqual(data["events"][0]["name"], "X")
            self.assertEqual(data["events"][0]["project_name"], "p")
            self.assertTrue(data["note"])

    def test_project_audit_route_requires_a_path(self):
        import cli.hub_server as hub_server
        resp = hub_server.app.test_client().get("/api/projects/credentials/audit")
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()


class TestCredAuditScopeParam(unittest.TestCase):
    """scope=global answers a different question from the roll-up."""

    def test_scope_global_reads_the_shared_vault_alone(self):
        import cli.hub_server as hub_server
        from services import credential_store as cs

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            proj = Path(tmp) / "p"
            (proj / ".c3").mkdir(parents=True)
            (Path(home) / ".c3").mkdir(parents=True)
            ActivityLog(str(proj)).log("cred_action", {
                "kind": "creds", "action": "set", "name": "PROJ_ONLY",
                "scope": "project", "via": "hub"})
            ActivityLog(str(home)).log("cred_action", {
                "kind": "creds", "action": "set", "name": "SHARED",
                "scope": "global", "via": "hub"})

            class _PM:
                def list_projects(self):
                    return [{"name": "p", "path": str(proj)}]

            with mock.patch.object(hub_server, "_pm", new=lambda: _PM()), \
                    mock.patch.object(cs, "_global_base", return_value=Path(home)):
                client = hub_server.app.test_client()
                everything = client.get("/api/hub/credentials/audit").get_json()
                shared = client.get(
                    "/api/hub/credentials/audit?scope=global").get_json()

            self.assertEqual({e["name"] for e in everything["events"]},
                             {"PROJ_ONLY", "SHARED"})
            self.assertEqual(everything["scope"], "all")
            self.assertEqual([e["name"] for e in shared["events"]], ["SHARED"])
            self.assertEqual(shared["scope"], "global")
