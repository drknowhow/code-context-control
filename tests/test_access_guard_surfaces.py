"""Access Guard T3 surface tests — REST /api/access, write API, CLI, UI bundle.

Flask test client + temp PROJECT_PATH + patched global base — fully offline.
Mutations are human-only surfaces (UI/REST/CLI); every add/remove must land
in the activity log and edit ledger by identifier. The check probe must
return the exact S1/S2 refusal strings from the frozen spec (§4).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cli.server as srv
from services import access_guard as ag


class AccessBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        self._old_project_path = srv.PROJECT_PATH
        srv.PROJECT_PATH = self.proj
        self._gb = mock.patch.object(
            ag, "_global_base", return_value=Path(self._home.name))
        self._gb.start()
        self.client = srv.app.test_client()

    def tearDown(self):
        self._gb.stop()
        srv.PROJECT_PATH = self._old_project_path
        self._tmp.cleanup()
        self._home.cleanup()

    def _add(self, glob, kind="deny", scope="project"):
        return self.client.post(
            "/api/access", json={"glob": glob, "kind": kind, "scope": scope})

    def _check(self, path, op="read"):
        return self.client.get(
            "/api/access/check", query_string={"path": str(path), "op": op})


class TestAccessRoutes(AccessBase):
    def test_list_includes_builtins_and_coverage(self):
        resp = self.client.get("/api/access")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("**/.env*", data["scopes"]["builtin"]["deny"])
        self.assertIn("**/.git/**", data["scopes"]["builtin"]["read_only"])
        self.assertIn("Enforced:", data["coverage"])
        self.assertIn("NOT enforced:", data["coverage"])
        self.assertEqual(data["corrupt"], [])
        for scope in ("global", "project"):
            self.assertEqual(data["scopes"][scope]["deny"], [])
            self.assertEqual(data["scopes"][scope]["read_only"], [])

    def test_add_check_remove_round_trip(self):
        resp = self._add("secrets/**")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["rule"]["added"])
        # Persisted in the project config
        cfg = json.loads(
            (self.proj / ".c3" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["access"]["deny"], ["secrets/**"])
        # Visible in the list
        data = self.client.get("/api/access").get_json()
        self.assertIn("secrets/**", data["scopes"]["project"]["deny"])
        # Denied probe returns verdict + matched rule + scope + exact S1 text
        target = str(self.proj / "secrets" / "x.txt")
        data = self._check(target).get_json()
        self.assertEqual(data["verdict"], "denied")
        self.assertEqual(data["rule"], "secrets/**")
        self.assertEqual(data["scope"], "project")
        denial = ag.check(target, "read", str(self.proj))
        self.assertEqual(data["refusal"], ag.refusal(denial, target, "read"))
        self.assertIn("[c3-access:denied]", data["refusal"])
        self.assertIn("policy decision, not a transient error", data["refusal"])
        self.assertIn("do not retry or route around it", data["refusal"])
        self.assertIn("'secrets/**' (project scope)", data["refusal"])
        # Remove, then the probe allows
        resp = self.client.delete("/api/access", query_string={
            "glob": "secrets/**", "kind": "deny", "scope": "project"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["removed"])
        data = self._check(target).get_json()
        self.assertEqual(data["verdict"], "allowed")
        self.assertEqual(data["refusal"], "")

    def test_read_only_rule_write_probe_returns_s2(self):
        self._add("docs/**", kind="read_only")
        target = str(self.proj / "docs" / "a.md")
        self.assertEqual(self._check(target, "read").get_json()["verdict"],
                         "allowed")
        data = self._check(target, "write").get_json()
        self.assertEqual(data["verdict"], "read_only")
        self.assertEqual(data["rule"], "docs/**")
        self.assertIn("[c3-access:read_only]", data["refusal"])
        self.assertIn("reads are evaluated separately", data["refusal"])

    def test_add_rejects_bad_glob_unknown_kind_unknown_scope(self):
        self.assertEqual(self._add("   ").status_code, 400)
        self.assertEqual(self._add("x/**", kind="allow").status_code, 400)
        self.assertEqual(self._add("x/**", kind="").status_code, 400)
        self.assertEqual(
            self._add("x/**", scope="planet").status_code, 400)
        # Nothing persisted by the rejected calls
        cfg_path = self.proj / ".c3" / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(cfg.get("access", {}).get("deny", []), [])

    def test_check_validates_params(self):
        self.assertEqual(
            self.client.get("/api/access/check").status_code, 400)
        resp = self._check(str(self.proj / "a.txt"), op="chmod")
        self.assertEqual(resp.status_code, 400)

    def test_mutations_audited_by_identifier(self):
        self._add("secrets/**")
        self.client.delete("/api/access", query_string={
            "glob": "secrets/**", "kind": "deny", "scope": "project"})
        log = self.proj / ".c3" / "activity_log.jsonl"
        self.assertTrue(log.exists())
        text = log.read_text(encoding="utf-8")
        self.assertIn("access_action", text)
        self.assertIn("secrets/**", text)
        ledger = self.proj / ".c3" / "edit_ledger.jsonl"
        self.assertTrue(ledger.exists())
        ltext = ledger.read_text(encoding="utf-8")
        self.assertIn("access://secrets/**", ltext)
        self.assertIn("access_add", ltext)
        self.assertIn("access_remove", ltext)

    def test_corrupt_scope_reported_and_mutation_refused(self):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"access": {"deny": [], "allow": ["**"]}}),
            encoding="utf-8")
        data = self.client.get("/api/access").get_json()
        self.assertIn("project", data["corrupt"])
        self.assertTrue(data["scopes"]["project"]["corrupt"])
        # Mutations never silently rewrite a corrupt section
        self.assertEqual(self._add("x/**").status_code, 400)
        resp = self.client.delete("/api/access", query_string={
            "glob": "x/**", "kind": "deny", "scope": "project"})
        self.assertEqual(resp.status_code, 400)
        # The scope fails closed for the probe
        data = self._check(str(self.proj / "anything.txt")).get_json()
        self.assertEqual(data["verdict"], "denied")
        self.assertEqual(data["rule"], "<corrupt-config>")


class TestWriteApi(AccessBase):
    def test_set_rule_normalizes_and_dedupes(self):
        r1 = ag.set_rule("secrets\\inner\\**", "deny", "project", str(self.proj))
        self.assertEqual(r1["glob"], "secrets/inner/**")
        self.assertTrue(r1["added"])
        r2 = ag.set_rule("SECRETS/INNER/**", "deny", "project", str(self.proj))
        self.assertFalse(r2["added"])
        cfg = json.loads(
            (self.proj / ".c3" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["access"]["deny"], ["secrets/inner/**"])

    def test_set_rule_global_scope_writes_patched_home(self):
        ag.set_rule("*.p12", "deny", "global", str(self.proj))
        gcfg = Path(self._home.name) / ".c3" / "config.json"
        self.assertTrue(gcfg.exists())
        data = ag.list_rules(str(self.proj))
        self.assertIn("*.p12", data["global"]["deny"])
        # Evaluator picks it up through the same patched base
        d = ag.check(str(self.proj / "cert.p12"), "read", str(self.proj))
        self.assertIsNotNone(d)
        self.assertEqual(d.scope, "global")

    def test_unknown_kind_and_scope_raise(self):
        with self.assertRaises(ValueError):
            ag.set_rule("x/**", "allow", "project", str(self.proj))
        with self.assertRaises(ValueError):
            ag.set_rule("x/**", "deny", "galaxy", str(self.proj))
        with self.assertRaises(ValueError):
            ag.remove_rule("x/**", "allow", "project", str(self.proj))

    def test_invalid_glob_raises(self):
        with self.assertRaises(ValueError):
            ag.set_rule("   ", "deny", "project", str(self.proj))
        with self.assertRaises(ValueError):
            ag.set_rule("", "read_only", "project", str(self.proj))

    def test_remove_missing_rule_reports_removed_false(self):
        r = ag.remove_rule("ghost/**", "deny", "project", str(self.proj))
        self.assertFalse(r["removed"])
        self.assertFalse((self.proj / ".c3" / "config.json").exists())

    def test_write_preserves_other_config_sections(self):
        cfg_path = self.proj / ".c3" / "config.json"
        cfg_path.write_text(
            json.dumps({"memory_llm": {"model": "m"}, "access": {"deny": []}}),
            encoding="utf-8")
        ag.set_rule("secrets/**", "deny", "project", str(self.proj))
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["memory_llm"], {"model": "m"})
        self.assertEqual(cfg["access"]["deny"], ["secrets/**"])


class TestCliSurface(AccessBase):
    def test_parser_wires_access_subcommands(self):
        from cli.commands.parser import build_parser
        parser = build_parser("0.0-test", lambda v: v)
        args = parser.parse_args(
            ["access", "add", "x/**", "--kind", "deny", "--path", str(self.proj)])
        self.assertEqual(args.command, "access")
        self.assertEqual(args.access_cmd, "add")
        self.assertEqual(args.kind, "deny")
        self.assertFalse(args.use_global)
        args = parser.parse_args(
            ["access", "remove", "x/**", "--kind", "read_only", "--global"])
        self.assertTrue(args.use_global)
        args = parser.parse_args(["access", "check", "foo.txt", "--op", "write"])
        self.assertEqual(args.target, "foo.txt")
        self.assertEqual(args.op, "write")
        args = parser.parse_args(["access", "list"])
        self.assertEqual(args.access_cmd, "list")

    def _run(self, **kw):
        from cli.c3 import cmd_access
        buf = StringIO()
        with redirect_stdout(buf):
            cmd_access(SimpleNamespace(**kw))
        return buf.getvalue()

    def test_cmd_access_add_list_check_remove(self):
        pp = str(self.proj)
        out = self._run(access_cmd="add", glob="secrets/**", kind="deny",
                        use_global=False, project_path=pp)
        self.assertIn("[OK] Added deny rule 'secrets/**'", out)
        cfg = json.loads(
            (self.proj / ".c3" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["access"]["deny"], ["secrets/**"])
        # Duplicate add is a no-op
        out = self._run(access_cmd="add", glob="secrets/**", kind="deny",
                        use_global=False, project_path=pp)
        self.assertIn("already present", out)
        # List shows scope groups + rule + coverage matrix
        out = self._run(access_cmd="list", project_path=pp)
        self.assertIn("[builtin]", out)
        self.assertIn("secrets/**", out)
        self.assertIn("NOT enforced:", out)
        # Check prints the S1 refusal for a denied path
        out = self._run(access_cmd="check",
                        target=str(self.proj / "secrets" / "a.txt"),
                        op="read", project_path=pp)
        self.assertIn("[c3-access:denied]", out)
        self.assertIn("'secrets/**' (project scope)", out)
        # Check allows an untouched path
        out = self._run(access_cmd="check",
                        target=str(self.proj / "src" / "main.py"),
                        op="read", project_path=pp)
        self.assertIn("[OK] read allowed", out)
        # Remove
        out = self._run(access_cmd="remove", glob="secrets/**", kind="deny",
                        use_global=False, project_path=pp)
        self.assertIn("[OK] Removed deny rule", out)
        # CLI mutations are audited too (identifiers only, house pattern)
        text = (self.proj / ".c3" / "activity_log.jsonl").read_text(
            encoding="utf-8")
        self.assertIn("access_action", text)
        self.assertIn("secrets/**", text)
        ltext = (self.proj / ".c3" / "edit_ledger.jsonl").read_text(
            encoding="utf-8")
        self.assertIn("access://secrets/**", ltext)

    def test_cmd_access_bad_glob_prints_error(self):
        out = self._run(access_cmd="add", glob="   ", kind="deny",
                        use_global=False, project_path=str(self.proj))
        self.assertIn("[error]", out)


class TestStatusView(AccessBase):
    def _status(self):
        from cli.tools.status import handle_status
        svc = SimpleNamespace(project_path=str(self.proj))
        return handle_status("access", False, svc,
                             lambda name, args, resp, summ, **kw: resp)

    def test_access_view_counts_and_coverage(self):
        ag.set_rule("secrets/**", "deny", "project", str(self.proj))
        ag.set_rule("docs/**", "read_only", "project", str(self.proj))
        out = self._status()
        self.assertIn("[project] 1 deny, 1 read_only", out)
        self.assertIn("[builtin]", out)
        self.assertIn("[global] 0 deny, 0 read_only", out)
        self.assertIn("NOT enforced:", out)

    def test_access_view_corrupt_warning(self):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"access": {"allow": ["**"]}}), encoding="utf-8")
        out = self._status()
        self.assertIn("fails closed", out)


class TestUiBundle(unittest.TestCase):
    def test_access_bundle_entry_present_ordered_and_on_disk(self):
        files = srv._UI_JS_FILES
        self.assertIn("ui/components/access.js", files)
        self.assertLess(files.index("ui/components/credentials.js"),
                        files.index("ui/components/access.js"))
        self.assertLess(files.index("ui/components/access.js"),
                        files.index("ui/app.js"))
        self.assertTrue((REPO_ROOT / "cli" / "ui" / "components" / "access.js").exists())

    def test_app_registers_access_tab_and_panel(self):
        src = (REPO_ROOT / "cli" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id: "access"', src)
        self.assertIn('label: "Access Guard"', src)
        self.assertIn("<AccessPanel />", src)
        comp = (REPO_ROOT / "cli" / "ui" / "components" / "access.js"
                ).read_text(encoding="utf-8")
        self.assertIn("const AccessPanel", comp)
        self.assertIn("/api/access/check", comp)
        self.assertIn("NOT enforced:", comp)  # §5 coverage matrix footer


if __name__ == "__main__":
    unittest.main()
