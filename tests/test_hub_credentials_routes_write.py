"""Tests for the hub's credential management routes (v2.59.0).

Flask test client + temp project/home + stubbed keyring — fully offline.
The load-bearing test is the endpoint sweep: no hub route may ever return a
stored value under any parameters (write-only wire contract, hub edition).
"""
from __future__ import annotations

import base64
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.hub_server as hub_server
from services import credential_store as cs

CANARY = "hub-write-canary-92kk"


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


class _StubPM:
    def __init__(self, rows):
        self.rows = rows

    def list_projects(self):
        return self.rows


class TestHubCredentialsWriteRoutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        self.home = Path(self._home.name)
        (self.proj / ".c3").mkdir()
        self._patchers = [
            mock.patch.object(cs, "_keyring_module", return_value=_StubKeyring()),
            mock.patch.object(cs, "_crypto_module", return_value=_StubFernet),
            mock.patch.object(cs, "_global_base", return_value=self.home),
        ]
        for p in self._patchers:
            p.start()
        cs._ACTIVE_SECRETS.clear()
        self.client = hub_server.app.test_client()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()
        self._home.cleanup()

    def _patched_pm(self, rows=None):
        if rows is None:
            rows = [{"name": "tmpproj", "path": str(self.proj)}]
        return mock.patch.object(hub_server, "_pm", new=lambda: _StubPM(rows))

    # ── set / update ──────────────────────────────────────────

    def test_post_set_project_scope_never_echoes(self):
        resp = self.client.post("/api/projects/credentials", json={
            "path": str(self.proj), "scope": "project", "name": "API_KEY",
            "value": CANARY, "description": "hub set",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("API_KEY", body)
        self.assertNotIn(CANARY, body)
        self.assertEqual(cs.get_value("API_KEY", project_path=str(self.proj)), CANARY)

    def test_post_metadata_only_partial_update(self):
        self.client.post("/api/projects/credentials", json={
            "path": str(self.proj), "scope": "project", "name": "PART",
            "value": CANARY, "description": "orig", "inject": True,
        })
        resp = self.client.post("/api/projects/credentials", json={
            "path": str(self.proj), "scope": "project", "name": "PART",
            "description": "changed",
        })
        self.assertEqual(resp.status_code, 200)
        entry = resp.get_json()["entry"]
        self.assertEqual(entry["description"], "changed")
        self.assertTrue(entry["inject"])  # untouched key survives
        self.assertEqual(cs.get_value("PART", project_path=str(self.proj)), CANARY)

    def test_post_metadata_update_unknown_name_400(self):
        resp = self.client.post("/api/projects/credentials", json={
            "path": str(self.proj), "scope": "project", "name": "NOPE",
            "description": "x",
        })
        self.assertEqual(resp.status_code, 400)

    def test_post_global_scope_without_path(self):
        resp = self.client.post("/api/projects/credentials", json={
            "scope": "global", "name": "GLOBAL_ONLY", "value": CANARY,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(CANARY, resp.get_data(as_text=True))
        self.assertIn("GLOBAL_ONLY", cs.list_entries(str(self.home)))
        # global entry resolves from any project too
        self.assertEqual(
            cs.get_value("GLOBAL_ONLY", project_path=str(self.proj)), CANARY)

    # ── import ────────────────────────────────────────────────

    def test_import_env(self):
        resp = self.client.post("/api/projects/credentials/import", json={
            "path": str(self.proj), "scope": "project",
            "text": f"A_KEY={CANARY}\nB_KEY=other\n",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(sorted(body["created"]), ["A_KEY", "B_KEY"])
        self.assertNotIn(CANARY, resp.get_data(as_text=True))
        # no overwrite by default
        resp2 = self.client.post("/api/projects/credentials/import", json={
            "path": str(self.proj), "scope": "project", "text": "A_KEY=new\n",
        })
        self.assertIn("A_KEY", resp2.get_json()["skipped"])
        self.assertEqual(cs.get_value("A_KEY", project_path=str(self.proj)), CANARY)

    # ── delete ────────────────────────────────────────────────

    def test_delete_scope_inference_and_explicit(self):
        cs.set_credential("DUP", "g-" + CANARY, scope="global",
                          project_path=str(self.proj))
        cs.set_credential("DUP", "p-" + CANARY, scope="project",
                          project_path=str(self.proj))
        # no scope: owning realm is the project shadow
        resp = self.client.delete(
            f"/api/projects/credentials/DUP?path={self.proj}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"removed": True, "scope": "project"})
        # global entry survives; explicit global delete without a path
        self.assertEqual(cs.get_value("DUP", project_path=str(self.proj)),
                         "g-" + CANARY)
        resp2 = self.client.delete("/api/projects/credentials/DUP?scope=global")
        self.assertEqual(resp2.get_json(), {"removed": True, "scope": "global"})
        self.assertIsNone(cs.get_value("DUP", project_path=str(self.proj)))

    # ── check ─────────────────────────────────────────────────

    def test_check_fingerprint_never_value(self):
        cs.set_credential("CHK", CANARY, project_path=str(self.proj))
        resp = self.client.post(
            "/api/projects/credentials/CHK/check", json={"path": str(self.proj)})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["resolvable"])
        self.assertTrue(re.fullmatch(r"[0-9a-f]{8}", body["fingerprint"]))
        self.assertNotIn(CANARY, resp.get_data(as_text=True))

    def test_check_unknown_404_and_global_no_path(self):
        resp = self.client.post("/api/projects/credentials/NOPE/check", json={})
        self.assertEqual(resp.status_code, 404)
        cs.set_credential("GCHK", CANARY, scope="global",
                          project_path=str(self.proj))
        resp2 = self.client.post("/api/projects/credentials/GCHK/check", json={})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()["scope"], "global")

    # ── target resolution guards ──────────────────────────────

    def test_project_scope_requires_path(self):
        resp = self.client.post("/api/projects/credentials", json={
            "scope": "project", "name": "X", "value": "v",
        })
        self.assertEqual(resp.status_code, 400)

    def test_invalid_scope_rejected(self):
        resp = self.client.post("/api/projects/credentials", json={
            "path": str(self.proj), "scope": "sneaky", "name": "X", "value": "v",
        })
        self.assertEqual(resp.status_code, 400)

    def test_hostile_path_rejected_on_every_mutation(self):
        bad = str(self.proj / "does" / "not" / "exist")
        mutations = [
            lambda: self.client.post("/api/projects/credentials", json={
                "path": bad, "scope": "project", "name": "X", "value": "v"}),
            lambda: self.client.post("/api/projects/credentials/import", json={
                "path": bad, "scope": "project", "text": "X=v"}),
            lambda: self.client.delete(
                f"/api/projects/credentials/X?path={bad}&scope=project"),
        ]
        for call in mutations:
            self.assertEqual(call().status_code, 404)

    def test_uninitialized_project_mutation_409(self):
        with tempfile.TemporaryDirectory() as bare:
            resp = self.client.post("/api/projects/credentials", json={
                "path": bare, "scope": "project", "name": "X", "value": "v",
            })
            self.assertEqual(resp.status_code, 409)
            self.assertTrue(resp.get_json().get("needs_init"))
            # reads stay permissive
            resp2 = self.client.get(f"/api/projects/credentials?path={bare}")
            self.assertEqual(resp2.status_code, 200)

    # ── the sweep: no route ever returns a value ──────────────

    def test_endpoint_sweep_no_route_ever_returns_value(self):
        cs.set_credential("SWEEP_P", CANARY, scope="project",
                          project_path=str(self.proj))
        cs.set_credential("SWEEP_G", CANARY, scope="global",
                          project_path=str(self.proj))
        with self._patched_pm():
            responses = [
                self.client.get(f"/api/projects/credentials?path={self.proj}"),
                self.client.post("/api/projects/credentials", json={
                    "path": str(self.proj), "scope": "project",
                    "name": "SWEEP_P", "description": "meta"}),
                self.client.post("/api/projects/credentials", json={
                    "path": str(self.proj), "scope": "project",
                    "name": "SWEEP_P2", "value": CANARY}),
                self.client.post("/api/projects/credentials/import", json={
                    "path": str(self.proj), "scope": "project",
                    "text": f"SWEEP_I={CANARY}"}),
                self.client.post("/api/projects/credentials/SWEEP_P/check",
                                 json={"path": str(self.proj)}),
                self.client.post("/api/projects/credentials/SWEEP_G/check",
                                 json={}),
                self.client.get("/api/hub/credentials/overview"),
                self.client.delete(
                    f"/api/projects/credentials/SWEEP_P2?path={self.proj}"),
            ]
        for resp in responses:
            self.assertNotIn(CANARY, resp.get_data(as_text=True),
                             f"value leaked from {resp.request.path}")
        # the on-disk registry never holds the value either
        cfg = (self.proj / ".c3" / "config.json").read_text(encoding="utf-8")
        self.assertNotIn(CANARY, cfg)

    # ── audits ────────────────────────────────────────────────

    def _log_lines(self, base: Path):
        f = base / ".c3" / "activity_log.jsonl"
        if not f.exists():
            return []
        return [json.loads(line) for line in
                f.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_project_mutations_audited_names_only(self):
        self.client.post("/api/projects/credentials", json={
            "path": str(self.proj), "scope": "project", "name": "AUD",
            "value": CANARY,
        })
        events = [e for e in self._log_lines(self.proj)
                  if e.get("type") == "cred_action"]
        self.assertTrue(events, "no cred_action audit event written")
        evt = events[-1]
        self.assertEqual(evt["name"], "AUD")
        self.assertEqual(evt["via"], "hub")
        self.assertEqual(evt["action"], "set")
        raw = (self.proj / ".c3" / "activity_log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(CANARY, raw)

    def test_global_mutations_audited_to_global_home(self):
        # no path → home log only
        self.client.post("/api/projects/credentials", json={
            "scope": "global", "name": "GAUD", "value": CANARY,
        })
        home_events = [e for e in self._log_lines(self.home)
                       if e.get("type") == "cred_action"]
        self.assertTrue(home_events)
        self.assertEqual(home_events[-1]["name"], "GAUD")
        # path + global scope → both logs
        self.client.post("/api/projects/credentials", json={
            "path": str(self.proj), "scope": "global", "name": "GAUD2",
            "value": CANARY,
        })
        self.assertIn("GAUD2", [e.get("name") for e in self._log_lines(self.home)])
        self.assertIn("GAUD2", [e.get("name") for e in self._log_lines(self.proj)])

    # ── overview / shadowing ──────────────────────────────────

    def test_overview_shadowing(self):
        cs.set_credential("SHARED", "g-" + CANARY, scope="global",
                          project_path=str(self.proj))
        cs.set_credential("SHARED", "p-" + CANARY, scope="project",
                          project_path=str(self.proj))
        cs.set_credential("LOCAL", "l-" + CANARY, scope="project",
                          project_path=str(self.proj))
        with self._patched_pm():
            resp = self.client.get("/api/hub/credentials/overview")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        g = {e["name"]: e for e in body["global"]["entries"]}
        self.assertEqual([s["path"] for s in g["SHARED"]["shadowed_in"]],
                         [str(self.proj)])
        rows = {p["path"]: p for p in body["projects"]}
        proj_entries = {e["name"]: e for e in rows[str(self.proj)]["entries"]}
        self.assertTrue(proj_entries["SHARED"]["shadows_global"])
        self.assertFalse(proj_entries["LOCAL"]["shadows_global"])
        # project list carries project-scoped entries only
        self.assertNotIn("SHARED", [e["name"] for e in body["projects"][0]["entries"]
                                    if e["scope"] == "global"])
        self.assertNotIn(CANARY, resp.get_data(as_text=True))

    def test_overview_uninitialized_project_flagged(self):
        with tempfile.TemporaryDirectory() as bare:
            with self._patched_pm([{"name": "bare", "path": bare}]):
                resp = self.client.get("/api/hub/credentials/overview")
            row = resp.get_json()["projects"][0]
            self.assertFalse(row["initialized"])
            self.assertEqual(row["entries"], [])

    # ── hub config accepts the new view ───────────────────────

    def test_hub_config_accepts_creds_main_view(self):
        cfg_file = self.home / "hub_config.json"
        with mock.patch.object(hub_server, "_GLOBAL_C3_DIR", self.home), \
                mock.patch.object(hub_server, "_HUB_CONFIG_FILE", cfg_file):
            resp = self.client.post("/api/hub/config", json={"main_view": "creds"})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                json.loads(cfg_file.read_text(encoding="utf-8"))["main_view"],
                "creds")
            resp2 = self.client.post("/api/hub/config", json={"main_view": "bogus"})
            self.assertEqual(resp2.status_code, 400)


if __name__ == "__main__":
    unittest.main()
