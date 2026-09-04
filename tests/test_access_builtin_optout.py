"""Tests for the two-key builtin opt-out (docs/access-guard.md).

Builtins are on by default. A human may switch a Tier-1 builtin off, but only
with BOTH keys: a `access.disable_builtin` entry in the GLOBAL config AND a
keyring attestation. Either alone leaves the builtin ENFORCED.

The point of the second key is the prompt-injection case: an agent that manages
to write config.json — precisely the move that would grant itself write access
to ~/.claude/settings.json — still cannot produce the attestation. So the
config-without-attestation test below is the one that matters most.
"""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import access_guard  # noqa: E402


class _FakeKeyring:
    """Stand-in for the keyring module; nothing here touches the real vault."""

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

    def tearDown(self):
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

    def write_project(self, section):
        cfg = self.project / ".c3" / "config.json"
        cfg.write_text(json.dumps({"access": section}), encoding="utf-8")

    def denies_write(self, path) -> bool:
        return access_guard.check(str(path), "write", str(self.project)) is not None


class TestDefaults(_Base):
    def test_nothing_disabled_by_default(self):
        self.assertEqual(access_guard.disabled_builtins(), frozenset())

    def test_git_write_denied_out_of_the_box(self):
        self.assertTrue(self.denies_write(self.project / ".git" / "config"))

    def test_settings_write_denied_out_of_the_box(self):
        self.assertTrue(self.denies_write(self.home / ".claude" / "settings.json"))


class TestTwoKey(_Base):
    def test_config_without_attestation_stays_enforced(self):
        """THE test. A config an agent could have written is not enough."""
        self.write_global({"disable_builtin": ["**/.git/**"]})
        self.assertEqual(access_guard.disabled_builtins(), frozenset())
        self.assertTrue(self.denies_write(self.project / ".git" / "config"))

    def test_attestation_without_config_stays_enforced(self):
        self.keyring.set_password(
            access_guard._ACCESS_KEYRING_SERVICE,
            access_guard._builtin_attest_account("**/.git/**"), "1")
        self.assertEqual(access_guard.disabled_builtins(), frozenset())
        self.assertTrue(self.denies_write(self.project / ".git" / "config"))

    def test_both_keys_disables(self):
        access_guard.set_builtin_disabled("**/.git/**", True)
        self.assertIn("**/.git/**", access_guard.disabled_builtins())
        self.assertFalse(self.denies_write(self.project / ".git" / "config"))

    def test_disabling_one_leaves_the_others_on(self):
        access_guard.set_builtin_disabled("**/.git/**", True)
        self.assertTrue(
            self.denies_write(self.home / ".claude" / "settings.json"),
            "disabling the git builtin must not touch the settings builtin")

    def test_round_trip_re_enables(self):
        access_guard.set_builtin_disabled("**/.git/**", True)
        self.assertFalse(self.denies_write(self.project / ".git" / "config"))
        access_guard.set_builtin_disabled("**/.git/**", False)
        self.assertTrue(self.denies_write(self.project / ".git" / "config"))
        self.assertEqual(access_guard.disabled_builtins(), frozenset())


class TestFailsClosed(_Base):
    def test_broken_keyring_refuses_to_write_a_lying_config(self):
        """If the attestation cannot be stored, config must stay untouched --
        otherwise the UI would report 'disabled' while evaluation enforces."""
        self.keyring.working = False
        with self.assertRaises(ValueError) as ctx:
            access_guard.set_builtin_disabled("**/.git/**", True)
        self.assertIn("keyring", str(ctx.exception).lower())
        cfg = self.home / ".c3" / "config.json"
        body = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else {}
        self.assertEqual((body.get("access") or {}).get("disable_builtin", []), [])
        self.assertTrue(self.denies_write(self.project / ".git" / "config"))

    def test_keyring_going_away_later_re_enforces(self):
        """Attested, then the keyring becomes unreadable: fail closed, not open."""
        access_guard.set_builtin_disabled("**/.git/**", True)
        self.assertFalse(self.denies_write(self.project / ".git" / "config"))
        self.keyring.working = False
        self.assertEqual(access_guard.disabled_builtins(), frozenset())
        self.assertTrue(self.denies_write(self.project / ".git" / "config"))


class TestTierZero(_Base):
    def test_vault_globs_refuse_even_with_both_keys(self):
        for glob in access_guard.BUILTIN_ABSOLUTE_DENY:
            with self.assertRaises(ValueError) as ctx:
                access_guard.set_builtin_disabled(glob, True)
            self.assertIn("vault", str(ctx.exception).lower())

    def test_vault_glob_forced_into_config_is_ignored(self):
        """Hand-edited config + forged attestation still must not disable it."""
        self.write_global({"disable_builtin": ["**/.c3/secrets.enc"]})
        self.keyring.set_password(
            access_guard._ACCESS_KEYRING_SERVICE,
            access_guard._builtin_attest_account("**/.c3/secrets.enc"), "1")
        self.assertEqual(access_guard.disabled_builtins(), frozenset())
        self.assertTrue(
            access_guard.check(str(self.project / ".c3" / "secrets.enc"),
                               "read", str(self.project)) is not None)

    def test_unknown_glob_rejected(self):
        with self.assertRaises(ValueError):
            access_guard.set_builtin_disabled("**/anything-else", True)


class TestScopeRules(_Base):
    def test_project_scope_disable_needs_a_project_attestation(self):
        """v2.118.0: a project scope may now carry the legacy opt-out too, and
        it is still inert on its own. The config half never moved a builtin;
        the keyring half does, and it is realm-bound to this project."""
        self.write_project({"disable_builtin": ["**/.git/**"]})
        _rules, corrupt = access_guard.load_rules(str(self.project))
        self.assertEqual(corrupt, [])
        self.assertEqual(access_guard.disabled_builtins(str(self.project)),
                         frozenset())
        self.assertIsNotNone(access_guard.check(
            str(self.project / ".git" / "config"), "write", str(self.project)))

    def test_project_scope_disable_with_its_attestation_takes_effect(self):
        access_guard.set_builtin_disabled("**/.git/**", True, scope="project",
                                          project_path=str(self.project))
        self.assertIn("**/.git/**",
                      access_guard.disabled_builtins(str(self.project)))
        self.assertIsNone(access_guard.check(
            str(self.project / ".git" / "config"), "write", str(self.project)))

    def test_global_disable_is_not_corrupt(self):
        self.write_global({"disable_builtin": ["**/.git/**"]})
        _rules, corrupt = access_guard.load_rules(str(self.project))
        self.assertEqual(corrupt, [])

    def test_unknown_keys_are_still_corrupt(self):
        """disable_builtin must not have widened the 'unknown key' guard that
        exists so `allow` can never silently no-op."""
        self.write_global({"allow": ["**/*"]})
        _rules, corrupt = access_guard.load_rules(str(self.project))
        self.assertIn("global", corrupt)


class TestReporting(_Base):
    def test_list_rules_reports_enforced_not_configured(self):
        access_guard.set_builtin_disabled("**/.git/**", True)
        b = access_guard.list_rules(str(self.project))["builtin"]
        self.assertIn("**/.git/**", b["disabled"])
        self.assertNotIn("**/.git/**", b["read_only"],
                         "read_only must list what is ENFORCED, not configured")
        self.assertIn("**/.claude/settings*.json", b["read_only"])
        for glob in access_guard.BUILTIN_ABSOLUTE_DENY:
            self.assertIn(glob, b["absolute"])
            self.assertIn(glob, b["deny"])


if __name__ == "__main__":
    unittest.main()
