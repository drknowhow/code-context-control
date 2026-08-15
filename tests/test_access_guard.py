"""Access Guard v1 evaluator tests — docs/access-guard.md §7 (T1 slice).

Covers: schema fail-closed, scope union/tightening, builtins, glob
semantics, canonicalization (platform-gated), deny-CREATE, refusal-string
pinning, S4 footer, AccessDenied. Enforcement-surface wiring tests land
with T2; this file tests the evaluator itself.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import access_guard as ag  # noqa: E402

WIN = os.name == "nt"


class GuardBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write_access(self, section, base=None):
        cfg = (base or self.proj) / ".c3" / "config.json"
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text(json.dumps({"access": section}), encoding="utf-8")

    def _check(self, rel, op="read"):
        return ag.check(str(self.proj / rel), op, str(self.proj))


class TestSchema(GuardBase):
    def test_no_config_allows_normal_files(self):
        self.assertIsNone(self._check("src/main.py"))

    def test_unknown_key_fails_closed(self):
        self._write_access({"deny": [], "allow": ["**"]})
        d = self._check("src/main.py")
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, "<corrupt-config>")

    def test_corrupt_json_fails_closed(self):
        (self.proj / ".c3" / "config.json").write_text("{nope", encoding="utf-8")
        d = self._check("src/main.py")
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, "<corrupt-config>")

    def test_non_list_globs_fail_closed(self):
        self._write_access({"deny": "secrets/**"})
        self.assertIsNotNone(self._check("src/main.py"))

    def test_empty_section_is_valid(self):
        self._write_access({"deny": [], "read_only": []})
        self.assertIsNone(self._check("src/main.py"))


class TestRules(GuardBase):
    def test_deny_blocks_read_and_write(self):
        self._write_access({"deny": ["secrets/**"]})
        for op in ("read", "write", "create", "delete"):
            d = self._check("secrets/key.txt", op)
            self.assertIsNotNone(d, op)
            self.assertEqual(d.kind, "deny")
            self.assertEqual(d.scope, "project")

    def test_deny_covers_directory_itself(self):
        self._write_access({"deny": ["secrets/**"]})
        self.assertIsNotNone(self._check("secrets"))

    def test_read_only_blocks_writes_allows_reads(self):
        self._write_access({"read_only": ["docs/**"]})
        self.assertIsNone(self._check("docs/a.md", "read"))
        d = self._check("docs/a.md", "write")
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "read_only")

    def test_deny_beats_read_only(self):
        self._write_access({"deny": ["docs/x.md"], "read_only": ["docs/**"]})
        d = self._check("docs/x.md", "read")
        self.assertEqual(d.kind, "deny")

    def test_basename_pattern_matches_any_depth(self):
        self._write_access({"deny": ["*.pem"]})
        self.assertIsNotNone(self._check("a/b/c/server.pem"))
        self.assertIsNone(self._check("a/b/pem.txt"))

    def test_case_insensitive(self):
        self._write_access({"deny": ["Secrets/**"]})
        self.assertIsNotNone(self._check("sEcReTs/k.txt"))

    def test_single_star_does_not_cross_separator(self):
        self._write_access({"deny": ["logs/*.log"]})
        self.assertIsNotNone(self._check("logs/a.log"))
        self.assertIsNone(self._check("logs/sub/a.log"))

    def test_scope_union_tightens(self):
        home = Path(tempfile.mkdtemp())
        try:
            self._write_access({"read_only": ["src/**"]}, base=home)
            self._write_access({"deny": ["src/hot.py"]})
            import unittest.mock as mock
            with mock.patch.object(ag, "_global_base", return_value=home):
                d = ag.check(str(self.proj / "src/hot.py"), "read",
                             str(self.proj))
                self.assertEqual(d.kind, "deny")
                d2 = ag.check(str(self.proj / "src/other.py"), "write",
                              str(self.proj))
                self.assertEqual(d2.kind, "read_only")
                self.assertEqual(d2.scope, "global")
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)


class TestBuiltins(GuardBase):
    def test_env_files_denied_read_and_write(self):
        for op in ("read", "write"):
            d = self._check(".env", op)
            self.assertIsNotNone(d, op)
            self.assertEqual(d.scope, "builtin")
        self.assertIsNotNone(self._check("sub/dir/.env.local"))

    def test_vault_sidecars_denied(self):
        self.assertIsNotNone(self._check(".c3/secrets.enc"))
        self.assertIsNotNone(self._check(".c3/cred_state.json"))

    def test_c3_dir_write_denied_read_allowed(self):
        self.assertIsNone(self._check(".c3/config.json", "read"))
        d = self._check(".c3/config.json", "write")
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "read_only")
        self.assertEqual(d.scope, "builtin")

    def test_git_write_denied_read_allowed(self):
        self.assertIsNone(self._check(".git/HEAD", "read"))
        self.assertIsNotNone(self._check(".git/hooks/pre-commit", "write"))

    def test_claude_settings_write_denied(self):
        self.assertIsNotNone(self._check(".claude/settings.local.json", "write"))
        self.assertIsNone(self._check(".claude/settings.local.json", "read"))

    def test_builtins_not_overridable(self):
        # A user config cannot loosen builtins (no allow key exists at all);
        # writing MORE rules never unblocks .env.
        self._write_access({"read_only": ["**"]})
        d = self._check(".env", "read")
        self.assertEqual(d.scope, "builtin")


class TestCanonicalize(GuardBase):
    def test_relative_resolves_under_root(self):
        canon, rel, denial = ag.canonicalize("a/b.txt", str(self.proj))
        self.assertIsNone(denial)
        self.assertTrue(canon.endswith("a/b.txt"))
        self.assertEqual(rel, "a/b.txt")

    def test_dotdot_escape_leaves_rel_empty(self):
        canon, rel, denial = ag.canonicalize("../outside.txt", str(self.proj))
        self.assertIsNone(denial)
        self.assertEqual(rel, "")

    def test_nonexistent_target_still_canonicalizes(self):
        canon, rel, denial = ag.canonicalize("no/such/dir/f.txt", str(self.proj))
        self.assertIsNone(denial)
        self.assertEqual(rel, "no/such/dir/f.txt")

    def test_unc_denied(self):
        for spelling in (r"\\server\share\x.txt", "//server/share/x.txt",
                         "\\\\?\\UNC\\server\\share\\x.txt"):
            _, _, denial = ag.canonicalize(spelling, str(self.proj))
            self.assertIsNotNone(denial, spelling)
            self.assertEqual(denial.rule, "<unc>")

    @unittest.skipUnless(WIN, "Windows device-prefix semantics")
    def test_device_prefix_stripped(self):
        target = self.proj / "x.txt"
        canon, rel, denial = ag.canonicalize("\\\\?\\" + str(target),
                                             str(self.proj))
        self.assertIsNone(denial)
        self.assertEqual(rel, "x.txt")

    @unittest.skipUnless(WIN, "NTFS trailing-dot semantics")
    def test_trailing_dot_lands_on_real_name(self):
        canon, rel, denial = ag.canonicalize("secrets/key.txt.",
                                             str(self.proj))
        self.assertIsNone(denial)
        self.assertEqual(rel, "secrets/key.txt")

    @unittest.skipUnless(WIN, "NTFS ADS semantics")
    def test_ads_denied(self):
        _, _, denial = ag.canonicalize("file.txt:stream", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.rule, "<ads>")

    @unittest.skipUnless(WIN, "8.3 short names")
    def test_short_name_on_missing_target_denied(self):
        _, _, denial = ag.canonicalize("MISSIN~1/f.txt", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.rule, "<8.3-alias>")

    @unittest.skipUnless(WIN, "NTFS deny-CREATE spellings")
    def test_deny_create_via_trailing_dot_spelling(self):
        self._write_access({"deny": ["secrets/**"]})
        d = self._check("secrets/new-key.pem.", "create")
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "deny")


class TestRefusals(GuardBase):
    def setUp(self):
        super().setUp()
        self._write_access({"deny": ["secrets/**"],
                            "read_only": ["docs/**"]})

    def test_s1_mcp_deny(self):
        d = self._check("secrets/k.txt")
        msg = ag.refusal(d, "secrets/k.txt", "read")
        self.assertTrue(msg.startswith("[c3-access:denied]"))
        self.assertIn("'secrets/**'", msg)
        self.assertIn("project scope", msg)
        self.assertIn("not a transient error", msg)
        self.assertIn("do not retry", msg)
        self.assertIn("c3 access list", msg)

    def test_s2_read_only(self):
        d = self._check("docs/a.md", "write")
        msg = ag.refusal(d, "docs/a.md", "write")
        self.assertTrue(msg.startswith("[c3-access:read_only]"))
        self.assertIn("reads are evaluated separately", msg)
        self.assertIn("Do not retry the write", msg)

    def test_s3_hook(self):
        d = self._check("secrets/k.txt")
        msg = ag.refusal(d, "secrets/k.txt", "write", surface="hook",
                         tool="Edit")
        self.assertIn("native Edit write denied", msg)
        self.assertIn("another tool or the shell", msg)

    def test_s5_proxy(self):
        d = self._check("secrets/k.txt")
        msg = ag.refusal(d, "secrets/k.txt", "read", surface="proxy",
                         project="OtherProj")
        self.assertIn("through project 'OtherProj'", msg)
        self.assertIn("proxied access", msg)

    def test_s4_footer_present_when_rules_active(self):
        footer = ag.search_footer(str(self.proj))
        self.assertTrue(footer.startswith("[c3-access:limited]"))
        self.assertIn("Absence is not evidence", footer)

    def test_s4_footer_absent_without_user_rules(self):
        clean = tempfile.TemporaryDirectory()
        try:
            import unittest.mock as mock
            with mock.patch.object(ag, "_global_base", return_value=None):
                self.assertEqual(ag.search_footer(clean.name), "")
        finally:
            clean.cleanup()

    def test_path_interpolation_capped(self):
        d = self._check("secrets/k.txt")
        long_path = "x" * 1000
        msg = ag.refusal(d, long_path, "read")
        self.assertLess(len(msg), 600)
        self.assertIn(" … ", msg)


class TestEnforce(GuardBase):
    def test_enforce_raises_with_denial_and_message(self):
        self._write_access({"deny": ["secrets/**"]})
        with self.assertRaises(ag.AccessDenied) as ctx:
            ag.enforce(str(self.proj / "secrets/k.txt"), "read",
                       str(self.proj))
        self.assertEqual(ctx.exception.denial.kind, "deny")
        self.assertIn("[c3-access:denied]", ctx.exception.message)

    def test_enforce_passes_when_allowed(self):
        ag.enforce(str(self.proj / "src/ok.py"), "read", str(self.proj))


class TestSeedDefaults(GuardBase):
    """seed_default_global_rules — spec §1 removable defaults, sticky removal."""

    def setUp(self):
        super().setUp()
        import unittest.mock as mock
        self._home = Path(tempfile.mkdtemp())
        self._gb = mock.patch.object(ag, "_global_base",
                                     return_value=self._home)
        self._gb.start()
        self.global_cfg = self._home / ".c3" / "config.json"

    def tearDown(self):
        self._gb.stop()
        import shutil
        shutil.rmtree(self._home, ignore_errors=True)
        super().tearDown()

    def test_seeds_on_first_run(self):
        res = ag.seed_default_global_rules()
        self.assertTrue(res["seeded"])
        data = json.loads(self.global_cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["access"]["deny"],
                         list(ag.DEFAULT_GLOBAL_RULES))

    def test_seeded_rules_are_enforced(self):
        ag.seed_default_global_rules()
        d = ag.check(str(self.proj / "server.pem"), "read", str(self.proj))
        self.assertIsNotNone(d)
        self.assertEqual(d.scope, "global")

    def test_second_run_is_noop(self):
        ag.seed_default_global_rules()
        self.assertFalse(ag.seed_default_global_rules()["seeded"])

    def test_removal_stays_sticky(self):
        ag.seed_default_global_rules()
        for glob in ag.DEFAULT_GLOBAL_RULES:
            ag.remove_rule(glob, "deny", "global")
        self.assertFalse(ag.seed_default_global_rules()["seeded"])
        data = json.loads(self.global_cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["access"]["deny"], [])

    def test_existing_access_section_never_touched(self):
        self._write_access({"deny": ["mine/**"]}, base=self._home)
        self.assertFalse(ag.seed_default_global_rules()["seeded"])
        data = json.loads(self.global_cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["access"]["deny"], ["mine/**"])

    def test_config_without_access_section_preserves_other_keys(self):
        self.global_cfg.parent.mkdir(parents=True, exist_ok=True)
        self.global_cfg.write_text(json.dumps({"other": {"k": 1}}),
                                   encoding="utf-8")
        res = ag.seed_default_global_rules()
        self.assertTrue(res["seeded"])
        data = json.loads(self.global_cfg.read_text(encoding="utf-8"))
        self.assertEqual(data["other"], {"k": 1})
        self.assertEqual(data["access"]["deny"],
                         list(ag.DEFAULT_GLOBAL_RULES))

    def test_corrupt_config_never_rewritten(self):
        self.global_cfg.parent.mkdir(parents=True, exist_ok=True)
        self.global_cfg.write_text("{nope", encoding="utf-8")
        self.assertFalse(ag.seed_default_global_rules()["seeded"])
        self.assertEqual(self.global_cfg.read_text(encoding="utf-8"), "{nope")

    def test_no_home_is_a_noop(self):
        import unittest.mock as mock
        with mock.patch.object(ag, "_global_base", return_value=None):
            self.assertFalse(ag.seed_default_global_rules()["seeded"])


if __name__ == "__main__":
    unittest.main()
