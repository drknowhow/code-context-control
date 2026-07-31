"""Tests for the user-tunable tool-discipline layer (v2.66.0).

Covers services/enforcement_policy, the mode-aware hook_pretool_enforce, and
the durability fixes in cli/_hook_utils. The invariant this file exists to
protect: relaxing tool discipline must NOT relax any security boundary — the
credential-vault guard and Access Guard path policy stay enforced at every
mode, including `off`.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "cli")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cli import _hook_utils  # noqa: E402
from cli import hook_pretool_enforce as hpe  # noqa: E402
from services import access_telemetry as at  # noqa: E402
from services import enforcement_policy as ep  # noqa: E402


def _project(section=None, extra=None):
    """A temp project dir with an optional `enforcement` config section."""
    tmp = TemporaryDirectory()
    root = Path(tmp.name)
    (root / ".c3").mkdir()
    cfg = dict(extra or {})
    if section is not None:
        cfg["enforcement"] = section
    (root / ".c3" / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return tmp, root


def _edit(path="src/app.py", tool="Edit", session="s1"):
    return {"tool_name": tool, "tool_input": {"file_path": path},
            "session_id": session}


def _decision(out):
    if not out:
        return "allow"
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "allow")


class TestModeResolution(unittest.TestCase):
    def test_missing_section_defaults_to_strict(self):
        """An install that predates this feature must not change behavior."""
        tmp, root = _project(None)
        with tmp:
            policy = ep.resolve(str(root))
            self.assertEqual(policy.mode, ep.MODE_STRICT)
            self.assertEqual(policy.scope, "default")

    def test_each_mode_round_trips(self):
        for mode in ep.MODES:
            tmp, root = _project({"mode": mode})
            with tmp:
                self.assertEqual(ep.resolve(str(root)).mode, mode)

    def test_tier_derivation_table(self):
        self.assertEqual(ep.derive_from_tier("standard"), ep.MODE_ADVISORY)
        self.assertEqual(ep.derive_from_tier("permissive"), ep.MODE_OFF)
        self.assertEqual(ep.derive_from_tier("c3-strict"), ep.MODE_STRICT)
        self.assertEqual(ep.derive_from_tier("read-only"), ep.MODE_STRICT)

    def test_unknown_tier_is_strict_not_permissive(self):
        """A typo in a tier name must fail toward MORE enforcement."""
        for bogus in ("", None, "nonsense", "PERMISSIVE-ish"):
            self.assertEqual(ep.derive_from_tier(bogus), ep.MODE_STRICT)

    def test_project_scope_beats_global(self):
        tmp, root = _project({"mode": "off"})
        with tmp:
            home = root / "fakehome"
            (home / ".c3").mkdir(parents=True)
            (home / ".c3" / "config.json").write_text(
                json.dumps({"enforcement": {"mode": "strict"}}), encoding="utf-8")
            old = os.environ.get("C3_HOME")
            os.environ["C3_HOME"] = str(home)
            try:
                self.assertEqual(ep.resolve(str(root)).mode, "off")
            finally:
                if old is None:
                    os.environ.pop("C3_HOME", None)
                else:
                    os.environ["C3_HOME"] = old

    def test_global_applies_when_project_has_no_section(self):
        tmp, root = _project(None)
        with tmp:
            home = root / "fakehome"
            (home / ".c3").mkdir(parents=True)
            (home / ".c3" / "config.json").write_text(
                json.dumps({"enforcement": {"mode": "advisory"}}), encoding="utf-8")
            old = os.environ.get("C3_HOME")
            os.environ["C3_HOME"] = str(home)
            try:
                policy = ep.resolve(str(root))
                self.assertEqual(policy.mode, "advisory")
                self.assertEqual(policy.scope, "global")
            finally:
                if old is None:
                    os.environ.pop("C3_HOME", None)
                else:
                    os.environ["C3_HOME"] = old


class TestFailClosed(unittest.TestCase):
    """Every malformed input must land on strict, never on off."""

    def test_bad_mode_string(self):
        tmp, root = _project({"mode": "yolo"})
        with tmp:
            policy = ep.resolve(str(root))
            self.assertEqual(policy.mode, ep.MODE_STRICT)
            self.assertTrue(policy.warnings)

    def test_section_wrong_type(self):
        tmp, root = _project(["not", "a", "dict"])
        with tmp:
            policy = ep.resolve(str(root))
            self.assertEqual(policy.mode, ep.MODE_STRICT)
            self.assertTrue(policy.warnings)

    def test_unparseable_config(self):
        tmp, root = _project(None)
        with tmp:
            (root / ".c3" / "config.json").write_text("{ not json",
                                                      encoding="utf-8")
            policy = ep.resolve(str(root))
            self.assertEqual(policy.mode, ep.MODE_STRICT)
            self.assertTrue(policy.warnings)

    def test_ttl_is_clamped_not_trusted(self):
        tmp, root = _project({"mode": "advisory", "signal_ttl_s": 10**9})
        with tmp:
            policy = ep.resolve(str(root))
            self.assertLessEqual(policy.signal_ttl_s, 86_400)
            self.assertTrue(policy.warnings)

    def test_ungovernable_blocked_tool_falls_back_to_defaults(self):
        """`blocked_tools: ["Bash"]` would silently no-op — reject it loudly."""
        tmp, root = _project({"mode": "strict", "blocked_tools": ["Bash"]})
        with tmp:
            policy = ep.resolve(str(root))
            self.assertEqual(policy.blocked_tools, frozenset(
                {"Edit", "Write", "MultiEdit"}))
            self.assertTrue(policy.warnings)

    def test_missing_policy_module_keeps_strict(self):
        """If the policy module cannot be imported the hook must NOT relax."""
        tmp, root = _project({"mode": "off"})
        with tmp:
            original = hpe.enforcement_policy
            hpe.enforcement_policy = None
            try:
                self.assertEqual(_decision(hpe.run(_edit(), root)), "deny")
            finally:
                hpe.enforcement_policy = original


class TestHookHonorsMode(unittest.TestCase):
    def test_strict_blocks_native_write(self):
        tmp, root = _project({"mode": "strict"})
        with tmp:
            self.assertEqual(_decision(hpe.run(_edit(), root)), "deny")

    def test_advisory_allows_with_hint(self):
        tmp, root = _project({"mode": "advisory"})
        with tmp:
            out = hpe.run(_edit(), root)
            self.assertEqual(_decision(out), "allow")
            self.assertIn("[c3:hint]", out.get("additionalContext", ""))

    def test_off_allows_silently(self):
        tmp, root = _project({"mode": "off"})
        with tmp:
            self.assertIsNone(hpe.run(_edit(), root))

    def test_no_section_matches_pre_v266_behavior(self):
        tmp, root = _project(None)
        with tmp:
            self.assertEqual(_decision(hpe.run(_edit(), root)), "deny")

    def test_read_class_stays_advisory_in_every_mode(self):
        for mode in ep.MODES:
            tmp, root = _project({"mode": mode})
            with tmp:
                out = hpe.run(
                    {"tool_name": "Read",
                     "tool_input": {"file_path": "a.py"}, "session_id": "s"},
                    root)
                self.assertEqual(_decision(out), "allow", f"mode={mode}")

    def test_strict_refusal_names_the_escape_hatch(self):
        """A blocked agent should be able to tell the user how to unblock it."""
        tmp, root = _project({"mode": "strict"})
        with tmp:
            reason = hpe.run(_edit(), root)["hookSpecificOutput"][
                "permissionDecisionReason"]
            self.assertIn("c3 enforce advisory", reason)

    def test_blocked_tools_override_narrows_scope(self):
        tmp, root = _project({"mode": "strict", "blocked_tools": ["Write"]})
        with tmp:
            self.assertEqual(_decision(hpe.run(_edit(tool="Edit"), root)),
                             "allow")
            self.assertEqual(_decision(hpe.run(_edit(tool="Write"), root)),
                             "deny")


class TestSecurityBoundariesSurviveEveryMode(unittest.TestCase):
    """The whole point of splitting Layer C from Layer B."""

    def test_vault_write_denied_in_all_modes(self):
        for mode in ep.MODES:
            tmp, root = _project({"mode": mode})
            with tmp:
                for vault_file in ("secrets.enc", "cred_state.json",
                                   "config.json"):
                    payload = _edit(str(root / ".c3" / vault_file),
                                    tool="Write")
                    out = hpe.run(payload, root)
                    self.assertEqual(_decision(out), "deny",
                                     f"mode={mode} file={vault_file}")
                    self.assertIn("vault-protected",
                                  out["hookSpecificOutput"][
                                      "permissionDecisionReason"])

    def test_blocked_tools_empty_cannot_open_the_vault(self):
        """A config override must not become a route to the credential store."""
        tmp, root = _project({"mode": "off", "blocked_tools": []})
        with tmp:
            out = hpe.run(_edit(str(root / ".c3" / "secrets.enc"),
                                tool="Write"), root)
            self.assertEqual(_decision(out), "deny")

    def test_access_guard_is_untouched_by_discipline_mode(self):
        """hook_access_guard denies on path policy regardless of Layer C."""
        from cli import hook_access_guard as hag
        for mode in ep.MODES:
            tmp, root = _project({"mode": mode},
                                 extra={"access": {"deny": ["secrets/**"]}})
            with tmp:
                (root / "secrets").mkdir()
                target = root / "secrets" / "k.txt"
                target.write_text("x", encoding="utf-8")
                out = hag.run({"tool_name": "Read",
                               "tool_input": {"file_path": str(target)},
                               "session_id": "s"}, root)
                self.assertEqual(_decision(out), "deny", f"mode={mode}")


class TestProvenance(unittest.TestCase):
    def test_tier_does_not_clobber_explicit_user_choice(self):
        tmp, root = _project(None)
        with tmp:
            ep.set_mode("off", str(root), set_by=ep.SET_BY_USER)
            result = ep.set_mode("strict", str(root), set_by=ep.SET_BY_TIER)
            self.assertTrue(result["deferred"])
            self.assertEqual(ep.resolve(str(root)).mode, "off")

    def test_tier_may_overwrite_a_tier_set_mode(self):
        tmp, root = _project(None)
        with tmp:
            ep.set_mode("advisory", str(root), set_by=ep.SET_BY_TIER)
            ep.set_mode("strict", str(root), set_by=ep.SET_BY_TIER)
            self.assertEqual(ep.resolve(str(root)).mode, "strict")

    def test_user_always_wins(self):
        tmp, root = _project(None)
        with tmp:
            ep.set_mode("strict", str(root), set_by=ep.SET_BY_TIER)
            ep.set_mode("off", str(root), set_by=ep.SET_BY_USER)
            self.assertEqual(ep.resolve(str(root)).mode, "off")

    def test_set_mode_rejects_unknown_mode(self):
        tmp, root = _project(None)
        with tmp:
            with self.assertRaises(ValueError):
                ep.set_mode("sorta-strict", str(root))

    def test_set_mode_preserves_unrelated_config_keys(self):
        tmp, root = _project(None, extra={"permission_tier": "standard",
                                          "index_max_files": 42})
        with tmp:
            ep.set_mode("advisory", str(root))
            data = json.loads((root / ".c3" / "config.json").read_text(
                encoding="utf-8"))
            self.assertEqual(data["permission_tier"], "standard")
            self.assertEqual(data["index_max_files"], 42)
            self.assertEqual(data["enforcement"]["mode"], "advisory")


class TestAtomicWriteDurability(unittest.TestCase):
    """Regression cover for the tmp-file leak + truncated-state bug."""

    def test_no_temp_file_survives_a_successful_write(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".c3").mkdir()
            _hook_utils.save_enforcement_state(
                {"session_id": "s", "last_c3_call": None,
                 "unlocked_files": {}}, root)
            leftovers = list((root / ".c3").glob("*.tmp*"))
            self.assertEqual(leftovers, [])

    def test_temp_file_removed_when_replace_fails(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".c3").mkdir()
            target = root / ".c3" / "state.json"

            real_replace = os.replace

            def boom(src, dst):
                raise PermissionError("simulated sharing violation")

            os.replace = boom
            try:
                with self.assertRaises(PermissionError):
                    _hook_utils._atomic_write_json(target, {"a": 1})
            finally:
                os.replace = real_replace

            self.assertEqual(list((root / ".c3").glob("*.tmp*")), [],
                             "failed replace must not orphan a temp file")

    def test_written_state_is_valid_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".c3").mkdir()
            state = {"session_id": "abc", "last_c3_call": None,
                     "unlocked_files": {"x": ["read"]}}
            _hook_utils.save_enforcement_state(state, root)
            raw = (root / ".c3" / "enforcement_state.json").read_text(
                encoding="utf-8")
            self.assertEqual(json.loads(raw)["session_id"], "abc")

    def test_sweep_removes_dead_pid_temps_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            c3 = root / ".c3"
            c3.mkdir()
            # A temp file owned by a PID that cannot be running.
            dead = c3 / "enforcement_state.json.tmp999999"
            dead.write_text("{}", encoding="utf-8")
            # One owned by THIS process — a concurrent write; must survive.
            live = c3 / f"enforcement_state.json.tmp{os.getpid()}"
            live.write_text("{}", encoding="utf-8")
            # Not a pid-suffixed temp at all.
            unrelated = c3 / "notes.tmpbackup"
            unrelated.write_text("x", encoding="utf-8")

            removed = _hook_utils.sweep_stale_temps(root)

            self.assertEqual(removed, 1)
            self.assertFalse(dead.exists())
            self.assertTrue(live.exists())
            self.assertTrue(unrelated.exists())

    def test_sweep_is_safe_without_a_c3_dir(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(_hook_utils.sweep_stale_temps(Path(tmp)), 0)


class TestDenialTelemetry(unittest.TestCase):
    def test_discipline_block_is_recorded(self):
        tmp, root = _project({"mode": "strict"})
        with tmp:
            hpe.run(_edit("a.py"), root)
            hpe.run(_edit("b.py"), root)
            agg = at.aggregate(str(root))
            self.assertEqual(agg["total"], 2)
            self.assertEqual(agg["by_layer"]["discipline"], 2)

    def test_coalescing_counts_hits_per_rule_and_tool(self):
        tmp, root = _project({"mode": "strict"})
        with tmp:
            for i in range(5):
                hpe.run(_edit(f"f{i}.py", tool="Edit"), root)
            hpe.run(_edit("g.py", tool="Write"), root)
            rows = at.aggregate(str(root))["rows"]
            by_tool = {r["tool"]: r["hits"] for r in rows}
            self.assertEqual(by_tool["Edit"], 5)
            self.assertEqual(by_tool["Write"], 1)

    def test_session_filter(self):
        tmp, root = _project({"mode": "strict"})
        with tmp:
            hpe.run(_edit("a.py", session="s1"), root)
            hpe.run(_edit("b.py", session="s2"), root)
            hpe.run(_edit("c.py", session="s2"), root)
            self.assertEqual(at.aggregate(str(root), session_id="s2")["total"], 2)

    def test_advisory_mode_records_nothing(self):
        """No block, no denial event — the log measures friction, not traffic."""
        tmp, root = _project({"mode": "advisory"})
        with tmp:
            hpe.run(_edit(), root)
            self.assertEqual(at.aggregate(str(root))["total"], 0)

    def test_suggest_names_the_right_lever(self):
        self.assertIn("c3 enforce", at.suggest(
            {"layer": "discipline", "rule": "native-write-blocked"}))
        self.assertIn("builtin disable", at.suggest(
            {"layer": "access", "rule": "**/.env*", "scope": "builtin"}))
        self.assertIn("access remove", at.suggest(
            {"layer": "access", "rule": "secrets/**", "scope": "project"}))
        self.assertIn("--global", at.suggest(
            {"layer": "access", "rule": "*.pem", "scope": "global"}))
        self.assertIn("spelling", at.suggest(
            {"layer": "access", "rule": "<8.3-alias>", "scope": "builtin"}))

    def test_corrupt_lines_are_skipped_not_fatal(self):
        tmp, root = _project({"mode": "strict"})
        with tmp:
            hpe.run(_edit("a.py"), root)
            log = root / at.DENIAL_LOG
            with open(log, "a", encoding="utf-8") as fh:
                fh.write("{ this is not json\n\n")
            self.assertEqual(at.aggregate(str(root))["total"], 1)

    def test_clear_empties_the_log(self):
        tmp, root = _project({"mode": "strict"})
        with tmp:
            hpe.run(_edit("a.py"), root)
            at.clear(str(root))
            self.assertEqual(at.aggregate(str(root))["total"], 0)

    def test_record_without_c3_dir_is_a_noop(self):
        with TemporaryDirectory() as tmp:
            at.record(layer="access", rule="x", tool="Read",
                      project_path=tmp)  # no .c3 dir
            self.assertEqual(at.aggregate(tmp)["total"], 0)


if __name__ == "__main__":
    unittest.main()
