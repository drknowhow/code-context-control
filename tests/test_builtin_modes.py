"""Per-builtin modes (docs/confirm-guard.md §7): deny | confirm | allow.

The generalisation of the two-key opt-out. The properties defended here:

- Two keys or nothing: a config entry an agent could have written is inert
  without the keyring attestation, an attestation with the WRONG mode string
  is inert, and a keyring that dies later re-enforces the default.
- A mode never widens the op class the builtin governs — `confirm` on the
  full-deny `**/.env*` pauses reads too (else it would silently become
  allow-read), `deny` on a write-deny builtin tightens to a full deny.
- Ambiguity (one glob in both `builtin_mode` and legacy `disable_builtin`)
  is a loud corrupt scope, never a precedence puzzle; a project-scope
  `builtin_mode` is corrupt too (project scopes only tighten).
- Tier-0 vault globs take no mode at any price, and even a confirm-mode
  `.c3/**` write to the policy files can never become a request.
- The legacy opt-out keeps working, and `set_builtin_mode` lazily retires
  the legacy spelling so the two keys can never disagree.
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

import cli.hook_access_guard as hag  # noqa: E402
from services import access_guard  # noqa: E402
from services import override_policy as opol  # noqa: E402
from services import override_requests as orq  # noqa: E402

SESSION = "sess-bm"


class _FakeKeyring:
    def __init__(self, working=True):
        self.store = {}
        self.working = working

    def set_password(self, service, account, value):
        if not self.working:
            raise RuntimeError("no keyring backend")
        self.store[(service, account)] = value

    def get_password(self, service, account):
        if not self.working:
            raise RuntimeError("no keyring backend")
        return self.store.get((service, account))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".c3").mkdir(parents=True)
        self.project = Path(self.tmp.name) / "proj"
        (self.project / ".c3").mkdir(parents=True)

        self._orig_global_base = access_guard._global_base
        access_guard._global_base = lambda: self.home

        self.keyring = _FakeKeyring()
        self._orig_keyring = sys.modules.get("keyring")
        sys.modules["keyring"] = self._as_module(self.keyring)

        # Override-request store, hermetic (for the confirm round-trips).
        self._store = Path(self.tmp.name) / "override_requests.json"
        self._patches = [
            mock.patch.object(opol, "_global_base", return_value=None),
            mock.patch.object(orq, "store_path", return_value=self._store),
            mock.patch.object(orq, "mutes_path",
                              return_value=Path(self.tmp.name) / "mutes.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        access_guard._global_base = self._orig_global_base
        if self._orig_keyring is None:
            sys.modules.pop("keyring", None)
        else:
            sys.modules["keyring"] = self._orig_keyring
        self.tmp.cleanup()

    @staticmethod
    def _as_module(fake):
        mod = types.ModuleType("keyring")
        mod.set_password = fake.set_password
        mod.get_password = fake.get_password
        return mod

    def write_global(self, section):
        cfg = self.home / ".c3" / "config.json"
        cfg.write_text(json.dumps({"access": section}), encoding="utf-8")

    def check(self, path, op):
        return access_guard.check(str(path), op, str(self.project))


class TestTwoKeyModes(_Base):
    def test_config_without_attestation_keeps_the_default(self):
        """THE test, mode edition: a config an agent could have written is
        not enough to change a builtin's behaviour."""
        self.write_global({"builtin_mode": {"**/.git/**": "allow"}})
        self.assertEqual(access_guard.builtin_modes(), {})
        self.assertIsNotNone(self.check(self.project / ".git" / "config",
                                        "write"))

    def test_attestation_with_the_wrong_value_is_inert(self):
        self.write_global({"builtin_mode": {"**/.git/**": "allow"}})
        self.keyring.set_password(
            access_guard._ACCESS_KEYRING_SERVICE,
            access_guard._mode_attest_account("**/.git/**"), "confirm")
        self.assertEqual(access_guard.builtin_modes(), {})
        self.assertIsNotNone(self.check(self.project / ".git" / "config",
                                        "write"))

    def test_both_keys_take_effect(self):
        access_guard.set_builtin_mode("**/.git/**", "allow")
        self.assertEqual(access_guard.builtin_modes(),
                         {"**/.git/**": "allow"})
        self.assertIsNone(self.check(self.project / ".git" / "config",
                                     "write"))

    def test_keyring_dying_later_re_enforces(self):
        access_guard.set_builtin_mode("**/.git/**", "allow")
        self.keyring.working = False
        self.assertEqual(access_guard.builtin_modes(), {})
        self.assertIsNotNone(self.check(self.project / ".git" / "config",
                                        "write"))

    def test_broken_keyring_refuses_to_write_a_lying_config(self):
        self.keyring.working = False
        with self.assertRaises(ValueError) as ctx:
            access_guard.set_builtin_mode("**/.env*", "confirm")
        self.assertIn("keyring", str(ctx.exception).lower())
        cfg = self.home / ".c3" / "config.json"
        body = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else {}
        self.assertEqual((body.get("access") or {}).get("builtin_mode", {}), {})


class TestModeSemantics(_Base):
    def test_env_confirm_pauses_reads_too(self):
        """On a full-deny builtin, confirm must cover ALL ops — a write-only
        confirm would silently turn deny-all into allow-read."""
        access_guard.set_builtin_mode("**/.env*", "confirm")
        env = self.project / ".env"
        env.write_text("KEY=1", encoding="utf-8")
        for op in ("read", "write"):
            denial = self.check(env, op)
            self.assertIsNotNone(denial, op)
            self.assertEqual(denial.kind, "confirm", op)

    def test_settings_confirm_pauses_writes_only(self):
        access_guard.set_builtin_mode("**/.claude/settings*.json", "confirm")
        target = self.home / ".claude" / "settings.json"
        self.assertIsNone(self.check(target, "read"))
        denial = self.check(target, "write")
        self.assertIsNotNone(denial)
        self.assertEqual(denial.kind, "confirm")

    def test_write_deny_builtin_can_tighten_to_full_deny(self):
        git = self.project / ".git" / "config"
        self.assertIsNone(self.check(git, "read"))  # default: reads open
        access_guard.set_builtin_mode("**/.git/**", "deny")
        denial = self.check(git, "read")
        self.assertIsNotNone(denial)
        self.assertEqual(denial.kind, "deny")

    def test_env_confirm_round_trip_through_the_hook(self):
        access_guard.set_builtin_mode("**/.env*", "confirm")
        env = self.project / ".env"
        env.write_text("KEY=1", encoding="utf-8")
        out = hag.run({"tool_name": "Read",
                       "tool_input": {"file_path": str(env)},
                       "session_id": SESSION}, project_path=self.project)
        deny = out["hookSpecificOutput"]
        self.assertEqual(deny["permissionDecision"], "deny")
        self.assertIn(access_guard.TAG_CONFIRM,
                      deny["permissionDecisionReason"])
        rows = orq.list_requests(project_path=str(self.project))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rule_class"], opol.LAYER_ACCESS_CONFIRM)

        orq.decide(rows[0]["id"], orq.DECISION_APPROVE)
        allowed = hag.run({"tool_name": "Read",
                           "tool_input": {"file_path": str(env)},
                           "session_id": SESSION}, project_path=self.project)
        self.assertIn("additionalContext", allowed)
        self.assertIn(opol.TAG_GRANTED, allowed["additionalContext"])

    def test_confirm_mode_c3_dir_never_grants_the_policy_files(self):
        access_guard.set_builtin_mode("**/.c3/**", "confirm")
        target = self.project / ".c3" / "config.json"
        target.write_text("{}", encoding="utf-8")
        denial = self.check(target, "write")
        self.assertIsNotNone(denial)
        self.assertEqual(denial.kind, "confirm")
        row, reason = orq.auto_file(str(self.project), denial=denial,
                                    tool="c3_edit", op="write",
                                    path=str(target), session_id=SESSION)
        self.assertIsNone(row)
        self.assertIn("credential vault or the override policy", reason)

    def test_tier0_stays_denied_under_every_mode_change(self):
        access_guard.set_builtin_mode("**/.c3/**", "confirm")
        vault = self.project / ".c3" / "secrets.enc"
        denial = self.check(vault, "read")
        self.assertIsNotNone(denial)
        self.assertEqual(denial.kind, "deny")


class TestValidation(_Base):
    def test_tier0_globs_take_no_mode(self):
        for glob in access_guard.BUILTIN_ABSOLUTE_DENY:
            with self.assertRaises(ValueError) as ctx:
                access_guard.set_builtin_mode(glob, "confirm")
            self.assertIn("vault", str(ctx.exception).lower())

    def test_unknown_glob_and_mode_rejected(self):
        with self.assertRaises(ValueError):
            access_guard.set_builtin_mode("**/anything", "confirm")
        with self.assertRaises(ValueError):
            access_guard.set_builtin_mode("**/.git/**", "maybe")

    def test_ambiguous_spelling_is_corrupt(self):
        self.write_global({"disable_builtin": ["**/.git/**"],
                           "builtin_mode": {"**/.git/**": "confirm"}})
        _rules, corrupt = access_guard.load_rules(str(self.project))
        self.assertIn("global", corrupt)
        # Corrupt scope ⇒ deny-all, the loud direction.
        self.assertIsNotNone(self.check(self.project / "anything.txt", "read"))

    def test_project_scope_builtin_mode_is_corrupt(self):
        (self.project / ".c3" / "config.json").write_text(
            json.dumps({"access": {"builtin_mode": {"**/.git/**": "allow"}}}),
            encoding="utf-8")
        _rules, corrupt = access_guard.load_rules(str(self.project))
        self.assertIn("project", corrupt)


class TestLegacyAndMigration(_Base):
    def test_shim_round_trip_still_works(self):
        out = access_guard.set_builtin_disabled("**/.git/**", True)
        self.assertTrue(out["disabled"] and out["changed"])
        self.assertIn("**/.git/**", access_guard.disabled_builtins())
        out = access_guard.set_builtin_disabled("**/.git/**", False)
        self.assertTrue(out["changed"])
        self.assertEqual(access_guard.disabled_builtins(), frozenset())

    def test_hand_written_legacy_optout_still_honoured(self):
        self.write_global({"disable_builtin": ["**/.git/**"]})
        self.keyring.set_password(
            access_guard._ACCESS_KEYRING_SERVICE,
            access_guard._builtin_attest_account("**/.git/**"), "1")
        self.assertIn("**/.git/**", access_guard.disabled_builtins())
        self.assertIsNone(self.check(self.project / ".git" / "config",
                                     "write"))

    def test_setting_a_mode_retires_the_legacy_spelling(self):
        self.write_global({"disable_builtin": ["**/.git/**"]})
        self.keyring.set_password(
            access_guard._ACCESS_KEYRING_SERVICE,
            access_guard._builtin_attest_account("**/.git/**"), "1")
        access_guard.set_builtin_mode("**/.git/**", "confirm")
        cfg = json.loads((self.home / ".c3" / "config.json")
                         .read_text(encoding="utf-8"))
        self.assertNotIn("**/.git/**",
                         (cfg["access"].get("disable_builtin") or []))
        self.assertEqual(cfg["access"]["builtin_mode"],
                         {"**/.git/**": "confirm"})
        # No ambiguity corrupt, and the mode is live.
        _rules, corrupt = access_guard.load_rules(str(self.project))
        self.assertEqual(corrupt, [])
        self.assertEqual(access_guard.effective_builtin_modes(),
                         {"**/.git/**": "confirm"})

    def test_default_resets_both_spellings(self):
        access_guard.set_builtin_mode("**/.git/**", "confirm")
        out = access_guard.set_builtin_mode("**/.git/**", "default")
        self.assertTrue(out["changed"])
        self.assertEqual(access_guard.effective_builtin_modes(), {})
        self.assertIsNotNone(self.check(self.project / ".git" / "config",
                                        "write"))


class TestAgentConfigTier(_Base):
    """BUILTIN_CONFIRM_WRITE — the previously-unguarded agent-config
    surfaces now pause by default (docs/confirm-guard.md §7.3)."""

    def test_previously_unguarded_surfaces_now_pause_writes(self):
        for rel in (".mcp.json", "CLAUDE.md", "AGENTS.md", "GEMINI.md",
                    ".claude/hooks/pre.py", ".claude/skills/x/SKILL.md",
                    ".claude/agents/a.md", ".claude/commands/c.md"):
            denial = self.check(self.project / rel, "write")
            self.assertIsNotNone(denial, rel)
            self.assertEqual(denial.kind, "confirm", rel)
            self.assertIsNone(self.check(self.project / rel, "read"), rel)

    def test_registration_stays_hard_while_bodies_pause(self):
        """settings*.json (hook REGISTRATION) keeps the hard write-deny; a
        hook BODY pauses. Registration decides code execution — the split is
        the point."""
        settings = self.check(self.home / ".claude" / "settings.json", "write")
        self.assertEqual(settings.kind, "read_only")
        body = self.check(self.home / ".claude" / "hooks" / "h.py", "write")
        self.assertEqual(body.kind, "confirm")

    def test_hook_files_a_request_for_an_mcp_json_write(self):
        target = self.project / ".mcp.json"
        out = hag.run({"tool_name": "Write",
                       "tool_input": {"file_path": str(target)},
                       "session_id": SESSION}, project_path=self.project)
        deny = out["hookSpecificOutput"]
        self.assertEqual(deny["permissionDecision"], "deny")
        self.assertIn(access_guard.TAG_CONFIRM,
                      deny["permissionDecisionReason"])
        rows = orq.list_requests(project_path=str(self.project))
        self.assertEqual(len(rows), 1)

    def test_tier_modes_deny_hardens_and_allow_restores(self):
        target = self.project / ".mcp.json"
        access_guard.set_builtin_mode("**/.mcp.json", "deny")
        denial = self.check(target, "write")
        self.assertEqual(denial.kind, "read_only")  # write-deny; reads open
        self.assertIsNone(self.check(target, "read"))
        access_guard.set_builtin_mode("**/.mcp.json", "allow")
        self.assertIsNone(self.check(target, "write"))
        access_guard.set_builtin_mode("**/.mcp.json", "default")
        self.assertEqual(self.check(target, "write").kind, "confirm")


class TestReporting(_Base):
    def test_list_rules_reports_modes_and_moved_kinds(self):
        access_guard.set_builtin_mode("**/.env*", "confirm")
        access_guard.set_builtin_mode("**/.git/**", "allow")
        b = access_guard.list_rules(str(self.project))["builtin"]
        self.assertIn("**/.env*", b["confirm"])
        self.assertNotIn("**/.env*", b["deny"])
        self.assertIn("**/.git/**", b["disabled"])
        self.assertNotIn("**/.git/**", b["read_only"])
        self.assertEqual(b["modes"]["**/.env*"], "confirm")
        self.assertEqual(b["modes"]["**/.git/**"], "allow")
        self.assertEqual(b["modes"]["**/.claude/settings*.json"], "default")
        for glob in access_guard.BUILTIN_ABSOLUTE_DENY:
            self.assertIn(glob, b["deny"])


if __name__ == "__main__":
    unittest.main()
