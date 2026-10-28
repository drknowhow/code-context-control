"""An approved grant survives the session's cwd moving between projects.

Observed 2026-10-28 in a Yep IDE session: a native Edit of a
linked worktree's ``.mcp.json`` was held by the builtin ``**/.mcp.json``
confirm rule, approved, retried — and held again under a NEW request id,
three times. The hook has no ``--project``, so every PreToolUse sub-hook
evaluates under ``Path.cwd()``: the session's shell cwd, which the agent had
moved between ``footprint-b1``, the main checkout and ``footprint-b1-hk``.
Each request (and so each grant, minted by ``decide`` into the row's
``project_path``) landed in a different project's
``.c3/override_grants.json``, and each retry read a different one.

The session, rule, tool, op and canonical path were identical every time;
only the grant STORE differed. The properties defended here:

- the retry is allowed once wherever the cwd now is, and files no new card;
- the reach does not grow: another session, another path, another tool, a
  second use, a rule-scoped grant, or a minting project that has since
  switched the layer off all stay held;
- the peek/settle split the dispatcher uses burns the use only at settle.
"""
from __future__ import annotations

import json
import os
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

SESSION = "sess-cwd"
RULE = "**/.mcp.json"  # builtin confirm tier for agent config (v2.100.0)


class CwdDriftBase(unittest.TestCase):
    """main checkout, a worktree nested inside it, and a sibling worktree —
    the three cwds the observed session moved through."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.main = root / "yep"
        self.wt = self.main / ".claude" / "worktrees" / "footprint-b1"
        self.sib = self.main / ".claude" / "worktrees" / "footprint-b1-hk"
        for proj in (self.main, self.wt, self.sib):
            (proj / ".c3").mkdir(parents=True)
        self.target = self.wt / ".mcp.json"
        self.target.write_text("{}", encoding="utf-8")
        self.main_mcp = self.main / ".mcp.json"
        self.main_mcp.write_text("{}", encoding="utf-8")

        self._store = root / "override_requests.json"
        self._mutes = root / "override_mutes.json"
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

    def guard(self, base, tool="Edit", path=None, session_id=SESSION,
              defer=False):
        payload = {"tool_name": tool,
                   "tool_input": {"file_path": str(path or self.target)},
                   "session_id": session_id}
        return hag.run(payload, project_path=base, defer_consume=defer)

    def held(self, out) -> bool:
        return (isinstance(out, dict)
                and out.get("hookSpecificOutput", {})
                .get("permissionDecision") == "deny")

    def allowed(self, out) -> bool:
        return (isinstance(out, dict)
                and opol.TAG_GRANTED in str(out.get("additionalContext", "")))

    def file_and_approve(self, base, **decide_kw) -> dict:
        out = self.guard(base)
        self.assertTrue(self.held(out), out)
        rows = [r for r in orq.load() if r.get("status") == orq.STATUS_PENDING]
        self.assertEqual(len(rows), 1)
        decided = orq.decide(rows[0]["id"], orq.DECISION_APPROVE, **decide_kw)
        self.assertEqual(decided["status"], orq.STATUS_APPROVED)
        return decided

    def grant_in(self, proj) -> dict:
        grants, corrupt = og.load(str(proj))
        self.assertFalse(corrupt)
        self.assertEqual(len(grants), 1)
        return grants[0]

    def write_override(self, proj, override):
        (proj / ".c3" / "config.json").write_text(
            json.dumps({"override": override}), encoding="utf-8")


class TestRetryAfterCwdMoved(CwdDriftBase):
    def test_observed_sequence_retry_from_main_is_allowed_once(self):
        # Filed while the cwd was the worktree, so the grant is minted there.
        row = self.file_and_approve(self.wt)
        self.assertEqual(og.path_key(row["project_path"]), og.path_key(self.wt))

        # The retry runs with the cwd back at the main checkout.
        out = self.guard(self.main)
        self.assertTrue(self.allowed(out), out)

        # One write: the worktree's grant is spent, nothing new was filed.
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 0)
        self.assertEqual(len(orq.load()), 1)
        self.assertTrue(self.held(self.guard(self.main)))

    def test_real_hook_shape_uses_cwd_when_no_project_is_given(self):
        # The installed hook command passes no --project: run() falls back to
        # Path.cwd(). Reproduce that literally rather than through the
        # project_path argument.
        prev = os.getcwd()
        try:
            os.chdir(self.wt)
            out = hag.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(self.target)},
                           "session_id": SESSION})
            self.assertTrue(self.held(out), out)
            rid = orq.load()[0]["id"]
            orq.decide(rid, orq.DECISION_APPROVE)
            os.chdir(self.main)
            out = hag.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(self.target)},
                           "session_id": SESSION})
        finally:
            os.chdir(prev)
        self.assertTrue(self.allowed(out), out)
        self.assertEqual([r["id"] for r in orq.load()], [rid])

    def test_retry_from_a_sibling_worktree_is_allowed(self):
        # The third observed cwd: the target is OUTSIDE that project.
        self.file_and_approve(self.wt)
        self.assertTrue(self.allowed(self.guard(self.sib)))

    def test_consumption_is_audited_where_the_grant_lives(self):
        self.file_and_approve(self.wt)
        self.guard(self.main)
        consumed = [line for line in og.read_audit(str(self.wt))
                    if line.get("event") == og.EV_CONSUMED]
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0]["evaluated_in"], og.path_key(self.main))

    def test_dispatcher_peek_does_not_burn_and_settle_does(self):
        self.file_and_approve(self.wt)
        out = self.guard(self.main, defer=True)
        self.assertTrue(self.allowed(out), out)
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 1)
        self.assertTrue(out["_on_allow"]())
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 0)

    def test_same_cwd_retry_is_unchanged(self):
        self.file_and_approve(self.wt)
        self.assertTrue(self.allowed(self.guard(self.wt)))
        consumed = [line for line in og.read_audit(str(self.wt))
                    if line.get("event") == og.EV_CONSUMED]
        self.assertNotIn("evaluated_in", consumed[0])


class TestReachDoesNotGrow(CwdDriftBase):
    def test_another_session_stays_held(self):
        self.file_and_approve(self.wt)
        self.assertTrue(self.held(self.guard(self.main, session_id="other")))
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 1)

    def test_another_path_stays_held(self):
        self.file_and_approve(self.wt)
        self.assertTrue(self.held(self.guard(self.main, path=self.main_mcp)))
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 1)

    def test_a_relative_path_resolves_against_the_current_cwd(self):
        # `.mcp.json` from the main checkout is main's file, not the
        # worktree's — the key is computed under the hook's own base and
        # never re-resolved against the minting project.
        self.file_and_approve(self.wt)
        self.assertTrue(self.held(self.guard(self.main, path=".mcp.json")))
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 1)

    def test_another_tool_stays_held(self):
        self.file_and_approve(self.wt)
        self.assertTrue(self.held(self.guard(self.main, tool="Write")))
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 1)

    def test_a_layer_switched_off_where_it_was_minted_voids_it(self):
        self.file_and_approve(self.wt)
        self.write_override(self.wt, {"layers": {"access_confirm": False}})
        self.assertTrue(self.held(self.guard(self.main)))
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 1)

    def test_a_layer_switched_off_here_still_wins(self):
        self.file_and_approve(self.wt)
        self.write_override(self.main, {"layers": {"access_confirm": False}})
        out = self.guard(self.main)
        self.assertFalse(self.allowed(out), out)
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 1)

    def test_a_confirm_approval_elsewhere_does_not_answer_a_deny_here(self):
        # Same glob, same gate ("access"), but here it is a deny rule, whose
        # approval needs the rule retyped. A one-tap confirm grant from the
        # worktree must not stand in for that.
        # access_deny is escalatable in BOTH projects, so only the rule-class
        # match on the request row stands between the two.
        deny_ok = {"enabled": True, "layers": {"access_deny": True}}
        self.write_override(self.wt, deny_ok)
        self.file_and_approve(self.wt)
        (self.main / ".c3" / "config.json").write_text(json.dumps({
            "access": {"deny": [RULE]}, "override": deny_ok,
        }), encoding="utf-8")
        denial = ag.check(str(self.target), "write", str(self.main))
        self.assertEqual((denial.rule, denial.kind), (RULE, "deny"))
        self.assertTrue(self.held(self.guard(self.main)))
        self.assertEqual(self.grant_in(self.wt)["uses_remaining"], 1)

    def test_a_denied_request_grants_nothing(self):
        self.guard(self.wt)
        orq.decide(orq.load()[0]["id"], orq.DECISION_DENY)
        self.assertTrue(self.held(self.guard(self.main)))

    def test_a_rule_grant_does_not_travel(self):
        # A rule grant's reach is a glob over ITS project's paths; carried
        # into another project's evaluation it would mean something else.
        self.write_override(self.wt, {"enabled": True,
                                      "allow_rule_grants": True})
        self.file_and_approve(self.wt, mode=orq.MODE_RULE, confirm=RULE)
        self.assertEqual(self.grant_in(self.wt)["scope"], og.SCOPE_RULE)
        self.assertTrue(self.held(self.guard(self.main)))
        # ...while in its own project it still works.
        self.assertTrue(self.allowed(self.guard(self.wt)))

    def test_only_the_approved_requests_grant_is_used(self):
        # A grant planted in the minting store for the same call, but not the
        # one the approved request names, is not reachable from elsewhere.
        self.file_and_approve(self.wt)
        grants, _ = og.load(str(self.wt))
        grants[0]["id"] = "grt_planted00000"
        og._save(str(self.wt), grants)
        self.assertTrue(self.held(self.guard(self.main)))


class TestDisciplineLayer(CwdDriftBase):
    """gate_discipline (hook_pretool_enforce) has the same cwd-based base."""

    def test_discipline_grant_survives_a_cwd_move(self):
        for proj in (self.wt, self.main):
            self.write_override(proj, {"enabled": True,
                                       "layers": {"discipline": True}})
        row = orq.create(str(self.wt), session_id=SESSION, tool="Edit",
                         op="write", path=str(self.target),
                         layer=opol.LAYER_DISCIPLINE)
        orq.decide(row["id"], orq.DECISION_APPROVE)
        line = og.gate_discipline(str(self.main), tool="Edit",
                                  path=str(self.target), session_id=SESSION)
        self.assertIsNotNone(line)
        self.assertIsNone(og.gate_discipline(
            str(self.main), tool="Edit", path=str(self.target),
            session_id=SESSION))


if __name__ == "__main__":
    unittest.main()
