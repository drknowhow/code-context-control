"""Confirm rules — the declarative "pause for a human" access mode.

Spec: docs/confirm-guard.md. The properties defended here:

- Precedence is ``deny > mask > confirm > read_only`` — mask beats confirm
  because an edit expressed against a transformed view can never be approved
  into the real file (mask-guard §3).
- User confirm rules gate the WRITE class only; reads are untouched. The
  all-ops variant is internal (builtin mode downgrades) and unreachable from
  config.
- A confirm hold auto-files an Override Request at the denial site and S8
  names it — or says exactly why filing was refused. Duplicates collapse,
  mutes stick, rate limits apply, and the vault/policy files can never
  become a request at all.
- ``access_confirm`` is the one override layer that defaults ON and does not
  require ``override.enabled`` — the human-authored rule is the opt-in. A
  corrupt override scope still disables it (fail closed), and an explicit
  ``layers.access_confirm: false`` still wins.
- An older C3 reading an ``access.confirm`` key treats the scope as corrupt
  and evaluates deny-all — loud, never silently permissive.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

import cli.hook_access_guard as hag  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import override_grants as og  # noqa: E402
from services import override_policy as opol  # noqa: E402
from services import override_requests as orq  # noqa: E402

SESSION = "sess-a"


class ConfirmBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        (self.proj / "infra").mkdir()
        self.held = self.proj / "infra" / "main.tf"
        self.held.write_text("resource {}", encoding="utf-8")
        self.write_config(access={"confirm": ["infra/**"]})

        # Hermetic: neither the developer's ~/.c3 nor their request store.
        self._store = Path(self._tmp.name) / "override_requests.json"
        self._mutes = Path(self._tmp.name) / "override_mutes.json"
        self._patches = [
            mock.patch.object(opol, "_global_base", return_value=None),
            mock.patch.object(ag, "_global_base", return_value=None),
            mock.patch.object(orq, "store_path", return_value=self._store),
            mock.patch.object(orq, "mutes_path", return_value=self._mutes),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def write_config(self, *, access=None, override=None):
        data = {}
        if access is not None:
            data["access"] = access
        if override is not None:
            data["override"] = override
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps(data), encoding="utf-8")

    def verdict(self, path=None, op="write"):
        return ag.verdict(str(path or self.held), op, str(self.proj))

    def auto_file(self, path=None, *, tool="c3_edit", op="write",
                  session_id=SESSION):
        target = str(path or self.held)
        denial = ag.check(target, op, str(self.proj))
        self.assertIsNotNone(denial)
        return orq.auto_file(str(self.proj), denial=denial, tool=tool, op=op,
                             path=target, session_id=session_id)


# ── verdict: per-op matrix + precedence ────────────────────────────────────

class TestVerdict(ConfirmBase):
    def test_write_class_pauses(self):
        for op in ("write", "create", "delete"):
            v = self.verdict(op=op)
            self.assertEqual(v.kind, "confirm", op)
            self.assertEqual(v.denial.kind, "confirm")
            self.assertEqual(v.denial.rule, "infra/**")

    def test_read_is_untouched(self):
        self.assertTrue(self.verdict(op="read").allowed)

    def test_create_under_confirm_glob_pauses(self):
        v = self.verdict(self.proj / "infra" / "new.tf", op="create")
        self.assertEqual(v.kind, "confirm")

    def test_deny_beats_confirm(self):
        self.write_config(access={"deny": ["infra/**"],
                                  "confirm": ["infra/**"]})
        self.assertEqual(self.verdict(op="write").kind, "denied")

    def test_mask_beats_confirm_on_write(self):
        # An approved edit against a transformed view would be applied to a
        # file the agent never saw — mask must win (docs/mask-guard.md §3).
        self.write_config(access={
            "confirm": ["infra/**"],
            "mask": [{"glob": "infra/**", "preset": "redact_secrets",
                      "params": {}}]})
        v = self.verdict(op="write")
        self.assertEqual(v.kind, "denied")
        self.assertEqual(v.denial.kind, "mask")

    def test_confirm_beats_read_only(self):
        self.write_config(access={"confirm": ["infra/**"],
                                  "read_only": ["infra/**"]})
        self.assertEqual(self.verdict(op="write").kind, "confirm")

    def test_enumeration_does_not_leak_and_does_not_pause(self):
        # Search-side prefilters ask for "read": a write-only confirm rule
        # must neither exclude the path nor pause the read.
        self.assertIsNone(ag.check(str(self.held), "read", str(self.proj)))

    def test_check_fails_closed_on_unmigrated_surfaces(self):
        # Any surface that only knows check() gets a denial to refuse with.
        denial = ag.check(str(self.held), "write", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.kind, "confirm")


# ── S8 refusal strings (pinned) ────────────────────────────────────────────

class TestRefusal(ConfirmBase):
    def _denial(self):
        return ag.check(str(self.held), "write", str(self.proj))

    def test_s8_with_request_id(self):
        text = ag.refusal(self._denial(), str(self.held), "write",
                          request_id="ovr_abc123")
        self.assertIn(ag.TAG_CONFIRM, text)
        self.assertIn("held for human confirmation", text)
        self.assertIn("This is a pause, not a refusal", text)
        self.assertIn("Confirmation request ovr_abc123 is pending", text)
        self.assertIn("c3_override(action='wait', request_id='ovr_abc123')",
                      text)
        self.assertIn("retry this exact call once if approved", text)

    def test_s8_with_failure_note(self):
        text = ag.refusal(self._denial(), str(self.held), "write",
                          request_note="rate limit: 3 pending")
        self.assertIn("could not be filed (rate limit: 3 pending)", text)
        self.assertIn("ask the user in chat", text)
        self.assertNotIn("is pending", text)

    def test_s8_refuse_only_surface(self):
        text = ag.refusal(self._denial(), str(self.held), "write")
        self.assertIn("No confirmation request was filed from this surface",
                      text)
        self.assertIn("c3_edit", text)

    def test_s8_never_carries_the_override_offer(self):
        # S8 self-contains the instruction; the §6 offer line would be a
        # second, contradictory invitation (docs/confirm-guard.md §4).
        self.write_config(access={"confirm": ["infra/**"]},
                          override={"enabled": True,
                                    "layers": {k: True
                                               for k in opol.LAYER_KEYS}})
        text = ag.refusal(self._denial(), str(self.held), "write",
                          surface="hook", tool="Edit")
        self.assertNotIn(opol.TAG_OFFER + " You may ask", text)


# ── policy: the access_confirm layer ───────────────────────────────────────

class TestConfirmLayer(ConfirmBase):
    def test_default_on_without_enabled(self):
        # No override section at all: confirm is escalatable, nothing else is.
        policy = opol.resolve(str(self.proj))
        self.assertFalse(policy.enabled)
        self.assertTrue(policy.escalatable(opol.LAYER_ACCESS_CONFIRM))
        for key in opol.LAYER_KEYS:
            if key != opol.LAYER_ACCESS_CONFIRM:
                self.assertFalse(policy.escalatable(key), key)

    def test_explicit_false_wins(self):
        self.write_config(access={"confirm": ["infra/**"]},
                          override={"layers": {"access_confirm": False}})
        policy = opol.resolve(str(self.proj))
        self.assertFalse(policy.escalatable(opol.LAYER_ACCESS_CONFIRM))
        row, reason = self.auto_file()
        self.assertIsNone(row)
        self.assertIn("not escalatable", reason)

    def test_corrupt_override_scope_fails_closed(self):
        self.write_config(access={"confirm": ["infra/**"]},
                          override={"allow_everything": True})
        policy = opol.resolve(str(self.proj))
        self.assertEqual(policy.corrupt_scopes, ("project",))
        self.assertFalse(policy.escalatable(opol.LAYER_ACCESS_CONFIRM))
        row, reason = self.auto_file()
        self.assertIsNone(row)

    def test_rule_class_maps_confirm_denials(self):
        denial = ag.check(str(self.held), "write", str(self.proj))
        self.assertEqual(opol.rule_class_for_denial(denial),
                         opol.LAYER_ACCESS_CONFIRM)


# ── auto-file protocol ─────────────────────────────────────────────────────

class TestAutoFile(ConfirmBase):
    def test_files_one_pending_request(self):
        row, reason = self.auto_file()
        self.assertEqual(reason, "")
        self.assertEqual(row["status"], orq.STATUS_PENDING)
        self.assertEqual(row["rule"], "infra/**")
        self.assertEqual(row["rule_class"], opol.LAYER_ACCESS_CONFIRM)
        self.assertEqual(row["layer"], opol.GATE_ACCESS)
        # Auto-filed rows carry no agent-composed text.
        self.assertEqual(row["justification"], "")

    def test_duplicate_returns_the_same_row(self):
        first, _ = self.auto_file()
        second, reason = self.auto_file()
        self.assertEqual(reason, "")
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second.get("duplicate"))

    def test_mute_suppresses_refiling(self):
        row, _ = self.auto_file()
        orq.decide(row["id"], orq.DECISION_DENY, mute=True)
        again, reason = self.auto_file()
        self.assertIsNone(again)
        self.assertIn("muted", reason)

    def test_vault_target_never_files(self):
        # A confirm glob over .c3/** outranks the builtin read_only, but the
        # policy files themselves can never become a request (forbidden
        # targets) — the hold stands with a reason instead.
        self.write_config(access={"confirm": ["**/.c3/**"]})
        target = self.proj / ".c3" / "config.json"
        denial = ag.check(str(target), "write", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.kind, "confirm")
        row, reason = orq.auto_file(str(self.proj), denial=denial,
                                    tool="c3_edit", op="write",
                                    path=str(target), session_id=SESSION)
        self.assertIsNone(row)
        self.assertIn("credential vault or the override policy", reason)

    def test_never_raises_on_a_broken_store(self):
        self._store.mkdir()  # store path is a DIRECTORY: writes will fail
        row, reason = self.auto_file()
        self.assertIsNone(row)
        self.assertTrue(reason)


# ── end-to-end: hold → approve → retry allowed once ────────────────────────

class TestRoundTrip(ConfirmBase):
    def _run_guard(self, tool, tool_input, session_id=SESSION):
        return hag.run({"tool_name": tool, "tool_input": tool_input,
                        "session_id": session_id}, project_path=self.proj)

    def test_hook_holds_files_and_names_the_request(self):
        out = self._run_guard("Edit", {"file_path": str(self.held)})
        deny = out["hookSpecificOutput"]
        self.assertEqual(deny["permissionDecision"], "deny")
        reason = deny["permissionDecisionReason"]
        self.assertIn(ag.TAG_CONFIRM, reason)
        rows = orq.list_requests(project_path=str(self.proj))
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["id"], reason)

    def test_hook_retry_reuses_the_pending_request(self):
        self._run_guard("Edit", {"file_path": str(self.held)})
        self._run_guard("Edit", {"file_path": str(self.held)})
        rows = orq.list_requests(project_path=str(self.proj))
        self.assertEqual(len(rows), 1)

    def test_approve_then_retry_consumes_one_use(self):
        out = self._run_guard("Edit", {"file_path": str(self.held)})
        row = orq.list_requests(project_path=str(self.proj))[0]
        # One-tap approve: confirm is NOT a typed-confirm layer, and no
        # `override.enabled` is required — the rule was the opt-in.
        decided = orq.decide(row["id"], orq.DECISION_APPROVE)
        self.assertEqual(decided["status"], orq.STATUS_APPROVED)

        allowed = self._run_guard("Edit", {"file_path": str(self.held)})
        self.assertIn("additionalContext", allowed)
        self.assertIn(opol.TAG_GRANTED, allowed["additionalContext"])

        # The grant was single-use: the next attempt holds again (and the
        # request store gains a fresh pending row).
        held_again = self._run_guard("Edit", {"file_path": str(self.held)})
        self.assertEqual(
            held_again["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_wrong_session_grant_does_not_apply(self):
        row, _ = self.auto_file()
        orq.decide(row["id"], orq.DECISION_APPROVE)
        out = self._run_guard("Edit", {"file_path": str(self.held)},
                              session_id="sess-b")
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_mint_via_layers_key_ignores_enabled(self):
        # decide() passes rule_class through to mint(); with no override
        # section at all the mint must still succeed for the confirm layer.
        grant = og.mint(str(self.proj), session_id=SESSION,
                        layer=opol.GATE_ACCESS, rule="infra/**",
                        tool="c3_edit", op="write", path=str(self.held),
                        layers_key=opol.LAYER_ACCESS_CONFIRM)
        self.assertTrue(grant["id"].startswith("grt_"))

    def test_mint_without_layers_key_still_needs_enabled(self):
        with self.assertRaises(ValueError):
            og.mint(str(self.proj), session_id=SESSION,
                    layer=opol.GATE_ACCESS, rule="infra/**",
                    tool="c3_edit", op="write", path=str(self.held))


# ── config writers + older-C3 fail-closed ──────────────────────────────────

class TestConfig(ConfirmBase):
    def test_set_and_remove_confirm_rule(self):
        out = ag.set_rule("db/**", "confirm", "project", str(self.proj))
        self.assertTrue(out["added"])
        rules = ag.list_rules(str(self.proj))
        self.assertIn("db/**", rules["project"]["confirm"])
        out = ag.remove_rule("db/**", "confirm", "project", str(self.proj))
        self.assertTrue(out["removed"])

    def test_list_rules_carries_confirm_everywhere(self):
        rules = ag.list_rules(str(self.proj))
        for scope in ("builtin", "global", "project"):
            self.assertIn("confirm", rules[scope], scope)
        self.assertEqual(rules["project"]["confirm"], ["infra/**"])

    def test_older_c3_fails_closed_on_a_confirm_key(self):
        # Emulate a pre-confirm C3: its _VALID_SECTION_KEYS has no 'confirm',
        # so the unknown-key rule makes the whole scope evaluate deny-all.
        older = ag._VALID_SECTION_KEYS - {ag._KIND_CONFIRM}
        with mock.patch.object(ag, "_VALID_SECTION_KEYS", older):
            denial = ag.check(str(self.held), "read", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.rule, "<corrupt-config>")

    def test_invalid_confirm_glob_is_corrupt(self):
        self.write_config(access={"confirm": [42]})
        denial = ag.check(str(self.held), "read", str(self.proj))
        self.assertIsNotNone(denial)
        self.assertEqual(denial.rule, "<corrupt-config>")


if __name__ == "__main__":
    unittest.main()
