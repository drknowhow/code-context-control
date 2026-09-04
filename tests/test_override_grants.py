"""Override Requests P1 — policy resolution, the grant primitive, hook gates.

Frozen spec: docs/override-requests.md §3.1 (config), §3.4/§3.5 (store +
audit), §4 (matching), §5 (gate placement), §12 (failure modes).

The load-bearing tests here are the negative ones. §4 lists nine conditions a
grant must satisfy; each is negated individually below, because a grant that
matches "close enough" is a standing capability, not an approval. Likewise
every fail-closed path (corrupt file, corrupt config, policy switched off
while grants are live) asserts *denied*, never "skipped the check".
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

import cli.hook_access_guard as hag  # noqa: E402
import cli.hook_pretool_enforce as hpe  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import override_grants as og  # noqa: E402
from services import override_policy as opol  # noqa: E402

SESSION = "sess-a"
ALL_LAYERS_ON = {k: True for k in opol.LAYER_KEYS}


class OverrideBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        (self.proj / "secrets").mkdir()
        self.blocked = self.proj / "secrets" / "key.txt"
        self.blocked.write_text("k", encoding="utf-8")
        self.write_config(access={"deny": ["secrets/**"], "read_only": ["docs/**"]},
                          override={"enabled": True, "layers": dict(ALL_LAYERS_ON)})
        # Hermetic: never let the developer's real ~/.c3 change a verdict.
        self._patch = mock.patch.object(opol, "_global_base", return_value=None)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def write_config(self, *, access=None, override=None, enforcement=None,
                     base=None):
        cfg = (base or self.proj) / ".c3" / "config.json"
        cfg.parent.mkdir(exist_ok=True)
        data = {}
        if access is not None:
            data["access"] = access
        if override is not None:
            data["override"] = override
        if enforcement is not None:
            data["enforcement"] = enforcement
        cfg.write_text(json.dumps(data), encoding="utf-8")

    def denial_for(self, path=None, op="read"):
        return ag.check(str(path or self.blocked), op, str(self.proj))

    def mint(self, **kw):
        args = dict(session_id=SESSION, layer=opol.GATE_ACCESS,
                    rule="secrets/**", tool="Read", op="read",
                    path=str(self.blocked))
        args.update(kw)
        return og.mint(str(self.proj), **args)

    def consume(self, **kw):
        args = dict(session_id=SESSION, layer=opol.GATE_ACCESS,
                    rule="secrets/**", tool="Read", op="read",
                    path=str(self.blocked))
        args.update(kw)
        return og.consume(str(self.proj), **args)

    def audit_events(self):
        return [e.get("event") for e in og.read_audit(str(self.proj), 0)]


# ── §3.1 policy schema + merge ─────────────────────────────────────────────

class TestPolicy(OverrideBase):
    def test_absent_section_is_off(self):
        # ...for every layer except access_confirm, whose opt-in is the
        # confirm rule itself, not the override section
        # (docs/confirm-guard.md §5).
        self.write_config(access={"deny": ["secrets/**"]})
        p = opol.resolve(str(self.proj))
        self.assertFalse(p.enabled)
        self.assertFalse(any(p.escalatable(k) for k in opol.LAYER_KEYS
                             if k != opol.LAYER_ACCESS_CONFIRM))
        self.assertTrue(p.escalatable(opol.LAYER_ACCESS_CONFIRM))

    def test_defaults_are_all_false(self):
        # Same carve-out: access_confirm defaults True by design.
        self.write_config(override={})
        p = opol.resolve(str(self.proj))
        self.assertFalse(p.enabled)
        for key in opol.LAYER_KEYS:
            if key == opol.LAYER_ACCESS_CONFIRM:
                self.assertTrue(p.layers[key], key)
            else:
                self.assertFalse(p.layers[key], key)

    def test_unknown_key_fails_closed(self):
        self.write_config(override={"enabled": True, "allow_everything": True})
        p = opol.resolve(str(self.proj))
        self.assertFalse(p.enabled)
        self.assertEqual(p.corrupt_scopes, ("project",))
        self.assertTrue(p.warnings)

    def test_unknown_layer_key_fails_closed(self):
        self.write_config(override={"enabled": True, "layers": {"vault": True}})
        self.assertFalse(opol.resolve(str(self.proj)).enabled)

    def test_wrong_types_fail_closed(self):
        for section in ({"enabled": "yes"},
                        {"max_ttl_s": 0},
                        {"max_ttl_s": True},
                        {"layers": {"discipline": "yes"}},
                        {"notify_severity": "loud"},
                        {"channel": "carrier-pigeon"}):
            with self.subTest(section=section):
                self.write_config(override=section)
                self.assertTrue(opol.resolve(str(self.proj)).corrupt_scopes)

    def test_unparseable_config_fails_closed(self):
        (self.proj / ".c3" / "config.json").write_text("{nope", encoding="utf-8")
        self.assertFalse(opol.resolve(str(self.proj)).enabled)

    def test_ttl_clamped_to_hard_ceiling(self):
        self.write_config(override={"enabled": True, "max_ttl_s": 86400})
        p = opol.resolve(str(self.proj))
        self.assertEqual(p.max_ttl_s, opol.HARD_MAX_TTL_S)
        self.assertEqual(p.clamp_ttl(999999), opol.HARD_MAX_TTL_S)

    def test_project_cannot_widen_global(self):
        home = Path(self._tmp.name) / "home"
        (home / ".c3").mkdir(parents=True)
        self.write_config(base=home, override={
            "enabled": False, "layers": {"access_deny": False}, "max_ttl_s": 60})
        self.write_config(override={
            "enabled": True, "layers": {"access_deny": True}, "max_ttl_s": 900})
        with mock.patch.object(opol, "_global_base", return_value=home):
            p = opol.resolve(str(self.proj))
        self.assertFalse(p.enabled)                       # AND, not override
        self.assertFalse(p.layers["access_deny"])
        self.assertEqual(p.max_ttl_s, 60)                 # min, not project's

    def test_both_scopes_on_enables(self):
        home = Path(self._tmp.name) / "home2"
        (home / ".c3").mkdir(parents=True)
        self.write_config(base=home, override={
            "enabled": True, "layers": {"access_deny": True}})
        self.write_config(override={
            "enabled": True, "layers": {"access_deny": True}})
        with mock.patch.object(opol, "_global_base", return_value=home):
            p = opol.resolve(str(self.proj))
        self.assertTrue(p.escalatable(opol.LAYER_ACCESS_DENY))


# ── §2 the "never" rows ────────────────────────────────────────────────────

class TestNeverEscalatable(OverrideBase):
    def test_tier0_vault_denial_has_no_layer(self):
        vault = self.proj / ".c3" / "secrets.enc"
        vault.write_text("x", encoding="utf-8")
        denial = self.denial_for(vault)
        self.assertIsNotNone(denial)
        self.assertIsNone(opol.rule_class_for_denial(denial))

    def test_synthetic_spelling_rules_have_no_layer(self):
        for rule in ("<unc>", "<ads>", "<corrupt-config>", "<8.3-alias>"):
            with self.subTest(rule=rule):
                self.assertIsNone(opol.rule_class_for_denial(
                    ag.Denial(rule, "deny", "builtin", "r")))

    def test_forbidden_targets_never_match_a_grant(self):
        for name in sorted(opol.FORBIDDEN_TARGET_NAMES):
            with self.subTest(name=name):
                self.assertTrue(opol.forbidden_target(f"c:/p/.c3/{name}"))
        self.assertFalse(opol.forbidden_target("c:/p/.c3/other.json"))
        self.assertFalse(opol.forbidden_target("c:/p/src/config.json"))

    def test_mint_refuses_a_vault_target(self):
        with self.assertRaises(ValueError):
            self.mint(path=str(self.proj / ".c3" / "secrets.enc"))

    def test_gate_never_consults_grants_for_tier0(self):
        vault = self.proj / ".c3" / "cred_state.json"
        vault.write_text("{}", encoding="utf-8")
        denial = self.denial_for(vault)
        self.assertIsNone(og.gate_access(
            str(self.proj), denial, tool="Read", op="read",
            path=str(vault), session_id=SESSION))

    def test_layer_mapping(self):
        self.assertEqual(
            opol.rule_class_for_denial(ag.Denial("**/.env*", "deny", "builtin", "")),
            opol.LAYER_ACCESS_BUILTIN)
        self.assertEqual(
            opol.rule_class_for_denial(ag.Denial("secrets/**", "deny", "project", "")),
            opol.LAYER_ACCESS_DENY)
        self.assertEqual(
            opol.rule_class_for_denial(ag.Denial("docs/**", "read_only", "project", "")),
            opol.LAYER_ACCESS_READONLY)
        self.assertEqual(
            opol.rule_class_for_denial(ag.Denial("data/**", "mask", "project", "")),
            opol.LAYER_MASK)


# ── §4 the matching truth table ────────────────────────────────────────────

class TestMatching(OverrideBase):
    def test_exact_match_consumes(self):
        grant = self.mint()
        hit = self.consume()
        self.assertIsNotNone(hit)
        self.assertEqual(hit["id"], grant["id"])
        self.assertEqual(hit["uses_remaining"], 0)

    def test_each_condition_negated_denies(self):
        cases = {
            "session": dict(session_id="other-session"),
            "layer": dict(layer=opol.GATE_DISCIPLINE),
            "rule": dict(rule="secrets/other/**"),
            "tool": dict(tool="Grep"),
            "op": dict(op="write"),
            "path": dict(path=str(self.proj / "secrets" / "other.txt")),
        }
        for name, override in cases.items():
            with self.subTest(condition=name):
                self.tearDown()
                self.setUp()  # a fresh store per case
                self.mint()
                self.assertIsNone(self.consume(**override))

    def test_expired_grant_denies(self):
        grant = self.mint()
        stored = json.loads(og.grants_path(self.proj).read_text(encoding="utf-8"))
        stored["grants"][0]["expires_at"] = og.iso(og.now() - timedelta(seconds=1))
        og.grants_path(self.proj).write_text(json.dumps(stored), encoding="utf-8")
        self.assertIsNone(self.consume())
        self.assertNotIn(grant["id"], [g["id"] for g in og.active(str(self.proj))])

    def test_unparseable_expiry_is_expired_not_eternal(self):
        self.mint()
        stored = json.loads(og.grants_path(self.proj).read_text(encoding="utf-8"))
        stored["grants"][0]["expires_at"] = "never"
        og.grants_path(self.proj).write_text(json.dumps(stored), encoding="utf-8")
        self.assertIsNone(self.consume())

    def test_single_use_burns(self):
        self.mint()
        self.assertIsNotNone(self.consume())
        self.assertIsNone(self.consume())

    def test_path_spelling_is_canonicalized_not_string_matched(self):
        self.mint()
        weird = self.proj / "secrets" / ".." / "secrets" / "key.txt"
        self.assertIsNotNone(self.consume(path=str(weird)))

    def test_session_isolation(self):
        self.mint()
        self.assertIsNone(self.consume(session_id="sess-b"))
        self.assertIsNotNone(self.consume())

    def test_mint_requires_a_session(self):
        with self.assertRaises(ValueError):
            self.mint(session_id="")

    def test_near_miss_is_recorded(self):
        self.mint()
        self.consume(path=str(self.proj / "secrets" / "other.txt"))
        self.assertIn(og.EV_NEAR_MISS, self.audit_events())

    def test_find_does_not_consume(self):
        self.mint()
        hit = og.find(str(self.proj), session_id=SESSION,
                      layer=opol.GATE_ACCESS, rule="secrets/**", tool="Read",
                      op="read", path=str(self.blocked))
        self.assertIsNotNone(hit)
        self.assertIsNotNone(self.consume())  # the use was still there


# ── §4.1 rule-scoped grants ────────────────────────────────────────────────
#
# The negative tests are again the load-bearing ones. A rule grant relaxes
# exactly two of the nine conditions; every OTHER condition, and every limit
# that replaces the ones it drops, is asserted individually here. The one
# that matters most is `test_forbidden_target_never_rides_a_rule_grant`: the
# widened path condition is the only route by which a grant could ever reach
# the vault or the policy files, and it must not.

class TestRuleScope(OverrideBase):
    def setUp(self):
        super().setUp()
        self.write_config(
            access={"deny": ["secrets/**"], "read_only": ["docs/**"]},
            override={"enabled": True, "layers": dict(ALL_LAYERS_ON),
                      "allow_rule_grants": True})
        self.other = self.proj / "secrets" / "other.txt"
        self.other.write_text("o", encoding="utf-8")

    def rule_mint(self, **kw):
        kw.setdefault("scope", og.SCOPE_RULE)
        return self.mint(**kw)

    def test_covers_a_different_path_under_the_same_rule(self):
        self.rule_mint()
        hit = self.consume(path=str(self.other))
        self.assertIsNotNone(hit)
        self.assertEqual(hit["scope"], og.SCOPE_RULE)

    def test_uses_are_unlimited_and_never_burn_out(self):
        self.rule_mint()
        for _ in range(60):  # past HARD_MAX_USES, which does not apply here
            self.assertIsNotNone(self.consume(path=str(self.other)))
        self.assertIsNone(og._uses_left(og.active(str(self.proj))[0]))

    def test_a_tool_in_the_same_class_rides_it(self):
        self.rule_mint(tool="Read")
        self.assertIsNotNone(self.consume(tool="Grep"))

    def test_a_tool_in_another_class_does_not(self):
        self.rule_mint(tool="Read")
        self.assertIsNone(self.consume(tool="Edit"))

    def test_an_unclassed_tool_is_never_widened(self):
        self.rule_mint(tool="SomeFutureTool")
        self.assertIsNone(self.consume(tool="Read"))
        self.assertIsNotNone(self.consume(tool="SomeFutureTool"))

    def test_a_path_outside_the_rule_does_not(self):
        outside = self.proj / "docs"
        outside.mkdir()
        target = outside / "note.md"
        target.write_text("n", encoding="utf-8")
        self.rule_mint()
        self.assertIsNone(self.consume(path=str(target)))

    def test_session_layer_op_still_pin_it(self):
        for name, override in {
            "session": dict(session_id="other-session"),
            "layer": dict(layer=opol.GATE_DISCIPLINE),
            "rule": dict(rule="secrets/other/**"),
            "op": dict(op="write"),
        }.items():
            with self.subTest(condition=name):
                self.tearDown()
                self.setUp()
                self.rule_mint()
                self.assertIsNone(self.consume(path=str(self.other), **override))

    def test_forbidden_target_never_rides_a_rule_grant(self):
        """The whole reason the widened path condition is safe.

        A rule grant over `**/.c3/**` would otherwise cover
        `override_grants.json` — the agent minting its own grants (spec §11
        threat 3). find/consume refuse a forbidden target on EVERY call, not
        just at mint, which is what makes the relaxation survivable.
        """
        c3_glob = "**/.c3/**"
        self.write_config(
            access={"deny": [c3_glob]},
            override={"enabled": True, "layers": dict(ALL_LAYERS_ON),
                      "allow_rule_grants": True})
        innocent = self.proj / ".c3" / "notes.txt"
        innocent.write_text("n", encoding="utf-8")
        og.mint(str(self.proj), session_id=SESSION, layer=opol.GATE_ACCESS,
                rule=c3_glob, tool="Read", op="read", path=str(innocent),
                scope=og.SCOPE_RULE)
        for name in ("override_grants.json", "config.json", "secrets.enc",
                     "cred_state.json", "overrides.jsonl"):
            with self.subTest(target=name):
                self.assertIsNone(og.consume(
                    str(self.proj), session_id=SESSION,
                    layer=opol.GATE_ACCESS, rule=c3_glob, tool="Read",
                    op="read", path=str(self.proj / ".c3" / name)))
        # …and the innocent sibling still works, so the refusal above is the
        # forbidden-target rule and not a broken grant.
        self.assertIsNotNone(og.consume(
            str(self.proj), session_id=SESSION, layer=opol.GATE_ACCESS,
            rule=c3_glob, tool="Read", op="read", path=str(innocent)))

    def test_idle_window_expires_an_unused_grant(self):
        self.rule_mint()
        stored = json.loads(og.grants_path(self.proj).read_text(encoding="utf-8"))
        stored["grants"][0]["last_used_at"] = og.iso(
            og.now() - timedelta(seconds=stored["grants"][0]["idle_s"] + 1))
        og.grants_path(self.proj).write_text(json.dumps(stored), encoding="utf-8")
        self.assertIsNone(self.consume(path=str(self.other)))
        self.assertEqual(og.active(str(self.proj)), [])

    def test_using_it_resets_the_idle_window(self):
        self.rule_mint()
        before = og.active(str(self.proj))[0].get("last_used_at")
        self.assertIsNone(before)
        self.consume(path=str(self.other))
        self.assertIsNotNone(og.active(str(self.proj))[0]["last_used_at"])

    def test_unreadable_idle_window_is_expired_not_eternal(self):
        self.rule_mint()
        stored = json.loads(og.grants_path(self.proj).read_text(encoding="utf-8"))
        stored["grants"][0]["idle_s"] = "forever"
        og.grants_path(self.proj).write_text(json.dumps(stored), encoding="utf-8")
        self.assertIsNone(self.consume(path=str(self.other)))

    def test_ttl_uses_the_rule_ceiling_not_the_call_one(self):
        grant = self.rule_mint(ttl_s=opol.HARD_MAX_RULE_TTL_S * 4)
        life = (og.parse_ts(grant["expires_at"]) - og.now()).total_seconds()
        self.assertGreater(life, opol.HARD_MAX_TTL_S)
        self.assertLessEqual(life, opol.HARD_MAX_RULE_TTL_S)

    def test_a_call_grant_still_gets_the_15_minute_ceiling(self):
        grant = self.mint(ttl_s=opol.HARD_MAX_RULE_TTL_S)
        life = (og.parse_ts(grant["expires_at"]) - og.now()).total_seconds()
        self.assertLessEqual(life, opol.HARD_MAX_TTL_S)

    def test_refused_when_the_policy_switch_is_off(self):
        self.write_config(
            access={"deny": ["secrets/**"]},
            override={"enabled": True, "layers": dict(ALL_LAYERS_ON),
                      "allow_rule_grants": False})
        with self.assertRaises(ValueError):
            self.rule_mint()

    def test_refused_for_a_synthetic_rule(self):
        with self.assertRaises(ValueError):
            self.rule_mint(layer=opol.GATE_DISCIPLINE,
                           rule=opol.RULE_DISCIPLINE, tool="Write", op="write")

    def test_refused_when_the_rule_does_not_cover_the_refusal(self):
        with self.assertRaises(ValueError):
            self.rule_mint(rule="docs/**")

    def test_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError):
            self.mint(scope="everything")

    def test_near_miss_does_not_fire_for_the_paths_it_covers(self):
        self.rule_mint()
        self.consume(path=str(self.other))
        self.assertNotIn(og.EV_NEAR_MISS, self.audit_events())

    def test_context_line_says_what_it_actually_covers(self):
        self.rule_mint()
        hit = self.consume(path=str(self.other))
        line = og.granted_context(hit, "secrets/**")
        self.assertIn("every path secrets/** matches", line)
        self.assertNotIn("uses left", line)


class TestConcurrency(OverrideBase):
    def test_one_use_survives_a_race(self):
        """Two hook subprocesses racing the last use: exactly one wins."""
        self.mint()
        results, barrier = [], threading.Barrier(2)

        def worker():
            barrier.wait()
            results.append(self.consume())

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r), 1)

    def test_audit_records_exactly_one_consumption(self):
        self.mint()
        self.consume()
        self.consume()
        self.assertEqual(self.audit_events().count(og.EV_CONSUMED), 1)


# ── §12 failure modes ──────────────────────────────────────────────────────

class TestFailClosed(OverrideBase):
    def test_corrupt_grants_file_means_zero_grants(self):
        self.mint()
        og.grants_path(self.proj).write_text("{nope", encoding="utf-8")
        self.assertEqual(og.load(str(self.proj)), ([], True))
        self.assertEqual(og.active(str(self.proj)), [])
        self.assertIsNone(self.consume())

    def test_non_dict_entries_are_corrupt(self):
        og.grants_path(self.proj).parent.mkdir(exist_ok=True)
        og.grants_path(self.proj).write_text(
            json.dumps({"grants": ["not-a-grant"]}), encoding="utf-8")
        self.assertEqual(og.load(str(self.proj)), ([], True))

    def test_policy_off_voids_live_grants(self):
        self.mint()
        self.write_config(access={"deny": ["secrets/**"]},
                          override={"enabled": False,
                                    "layers": dict(ALL_LAYERS_ON)})
        denial = self.denial_for()
        self.assertIsNone(og.gate_access(str(self.proj), denial, tool="Read",
                                         op="read", path=str(self.blocked),
                                         session_id=SESSION))

    def test_layer_off_voids_live_grants(self):
        self.mint()
        layers = dict(ALL_LAYERS_ON)
        layers[opol.LAYER_ACCESS_DENY] = False
        self.write_config(access={"deny": ["secrets/**"]},
                          override={"enabled": True, "layers": layers})
        denial = self.denial_for()
        self.assertIsNone(og.gate_access(str(self.proj), denial, tool="Read",
                                         op="read", path=str(self.blocked),
                                         session_id=SESSION))

    def test_mint_refuses_when_disabled(self):
        self.write_config(access={"deny": ["secrets/**"]},
                          override={"enabled": False})
        with self.assertRaises(ValueError):
            self.mint()

    def test_sweep_expires_unused_grants(self):
        self.mint(ttl_s=1)
        stored = json.loads(og.grants_path(self.proj).read_text(encoding="utf-8"))
        stored["grants"][0]["expires_at"] = og.iso(og.now() - timedelta(seconds=1))
        og.grants_path(self.proj).write_text(json.dumps(stored), encoding="utf-8")
        self.assertEqual(og.sweep_expired(str(self.proj)), 1)
        self.assertIn(og.EV_EXPIRED, self.audit_events())


# ── §5 gate placement — the hooks actually honour a grant ──────────────────

class TestHookGates(OverrideBase):
    def _run_guard(self, tool, tool_input, session_id=SESSION):
        return hag.run({"tool_name": tool, "tool_input": tool_input,
                        "session_id": session_id}, project_path=self.proj)

    def test_denied_without_a_grant(self):
        out = self._run_guard("Read", {"file_path": str(self.blocked)})
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_allowed_with_a_grant(self):
        self.mint()
        out = self._run_guard("Read", {"file_path": str(self.blocked)})
        self.assertIn("additionalContext", out)
        self.assertIn(opol.TAG_GRANTED, out["additionalContext"])
        self.assertIn("still in force", out["additionalContext"])

    def test_grant_is_spent_after_one_call(self):
        self.mint()
        self._run_guard("Read", {"file_path": str(self.blocked)})
        out = self._run_guard("Read", {"file_path": str(self.blocked)})
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_grant_does_not_cross_sessions_at_the_hook(self):
        self.mint()
        out = self._run_guard("Read", {"file_path": str(self.blocked)},
                              session_id="sess-b")
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_discipline_grant_unblocks_a_native_write(self):
        # Pin the mode: enforcement resolves project -> global -> default, and
        # a developer's global `advisory` would make this test vacuous.
        self.write_config(access={"deny": ["secrets/**"]},
                          override={"enabled": True, "layers": dict(ALL_LAYERS_ON)},
                          enforcement={"mode": "strict"})
        target = self.proj / "src" / "main.py"
        target.parent.mkdir(exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(target)},
                   "session_id": SESSION}

        blocked = hpe.run(dict(payload), project_path=self.proj)
        self.assertEqual(
            blocked["hookSpecificOutput"]["permissionDecision"], "deny")

        og.mint(str(self.proj), session_id=SESSION,
                layer=opol.GATE_DISCIPLINE, rule=opol.RULE_DISCIPLINE,
                tool="Edit", op="write", path=str(target))
        allowed = hpe.run(dict(payload), project_path=self.proj)
        self.assertIn(opol.TAG_GRANTED, allowed.get("additionalContext", ""))

    def test_vault_write_stays_denied_even_with_a_grant_attempt(self):
        vault = self.proj / ".c3" / "cred_state.json"
        vault.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            og.mint(str(self.proj), session_id=SESSION,
                    layer=opol.GATE_DISCIPLINE, rule=opol.RULE_DISCIPLINE,
                    tool="Edit", op="write", path=str(vault))
        out = hpe.run({"tool_name": "Edit",
                       "tool_input": {"file_path": str(vault)},
                       "session_id": SESSION}, project_path=self.proj)
        self.assertIn("vault-protected",
                      out["hookSpecificOutput"]["permissionDecisionReason"])


class TestOfferLine(OverrideBase):
    def test_offer_names_the_request_action_not_an_approve_action(self):
        line = opol.offer_line(opol.LAYER_ACCESS_DENY, "x.env", "Read", "read")
        self.assertIn("action='request'", line)
        self.assertNotIn("approve", line)


if __name__ == "__main__":
    unittest.main()
