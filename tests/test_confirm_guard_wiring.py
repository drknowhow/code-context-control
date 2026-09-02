"""The confirm flow the instruction docs mandate, end to end (v2.102.0).

CLAUDE.md item 11.6 tells the agent: C3 filed the request, wait on the id S8
names, retry the same call once if approved. A code review of the doc found
the flow failed on its most common path and that the tier the doc described
did not cover what it claimed. Each class here pins one of those.

Companions: tests/test_confirm_guard.py (the evaluator + S8 strings),
tests/test_builtin_modes.py (the tier's modes), tests/test_hook_dispatch.py
(sub-hook composition).
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
import cli.hook_dispatch as hd  # noqa: E402
from cli.tools import _grants  # noqa: E402
from cli.tools.override import handle_override  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services import artifact_defs  # noqa: E402
from services import override_grants as og  # noqa: E402
from services import override_policy as opol  # noqa: E402
from services import override_requests as orq  # noqa: E402

#: The id Claude Code puts in every hook payload AND exports to the MCP
#: server's environment. One value, two surfaces — that is the whole point.
HOST_SESSION = "f2c315be-28be-4751-930d-06756d3bd365"


def _finalize(name, args, resp, summ, **kw):
    return resp


class _Svc:
    """An MCP runtime whose session manager reports both ids, as the real
    one does since v2.102.0 (services.session_manager.start_session)."""

    def __init__(self, project_path, *, c3_id="20260902_120000",
                 host_id=HOST_SESSION):
        self.project_path = str(project_path)
        current = {"id": c3_id}
        if host_id is not None:
            current["host_session_id"] = host_id
        self.session_mgr = mock.Mock(current_session=current)


class ConfirmWiringBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name) / "proj"
        (self.proj / ".c3").mkdir(parents=True)
        (self.proj / "CLAUDE.md").write_text("# live\n", encoding="utf-8")
        self.write_config()

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
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        _hook_utils.drain_state_warnings()
        self._tmp.cleanup()

    def write_config(self, *, access=None, mode="advisory"):
        """Pin the tool-discipline mode (test_hook_dispatch.py does the same):
        an ambient `c3 enforce ...` must not decide what these assert."""
        data = {"enforcement": {"mode": mode}}
        if access is not None:
            data["access"] = access
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps(data), encoding="utf-8")

    def hook(self, tool="Edit", rel="CLAUDE.md", session_id=HOST_SESSION):
        """One PreToolUse pass through the DISPATCHER, as production runs."""
        return hd.dispatch("pretool",
                           {"tool_name": tool, "session_id": session_id,
                            "tool_input": {"file_path": str(self.proj / rel)}},
                           project_path=self.proj)

    def reason(self, out):
        return (out or {}).get("hookSpecificOutput", {}).get(
            "permissionDecisionReason", "")


# ── 1. The hook files it; the MCP tool must be able to wait on it ──────────

class TestSessionIdentityBridge(ConfirmWiringBase):
    """The flow 11.6 mandates, on the path it actually takes.

    The hook files under Claude Code's session id (its payload's
    ``session_id``); ``c3_override`` resolved C3's own ``YYYYmmdd_HHMMSS``
    id. Every hook-filed hold therefore answered "that request belongs to
    another session", so the wait step the doc calls mandatory could not
    run for the most common hold in the product.
    """

    def _filed_request(self):
        out = self.hook()
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")
        rows = orq.list_requests(project_path=str(self.proj))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_agent_can_wait_on_a_hook_filed_request(self):
        row = self._filed_request()
        out = handle_override("status", "", "", "write", "", row["id"], "", 1,
                              _Svc(self.proj), _finalize)
        self.assertNotIn("another session", out)
        self.assertIn("Still pending", out)

    def test_approve_then_wait_returns_the_retry_instruction(self):
        row = self._filed_request()
        orq.decide(row["id"], orq.DECISION_APPROVE)
        out = handle_override("wait", "", "", "write", "", row["id"], "", 1,
                              _Svc(self.proj), _finalize)
        self.assertIn("Retry the SAME call once", out)

    def test_the_agent_can_withdraw_a_hook_filed_request(self):
        row = self._filed_request()
        out = handle_override("withdraw", "", "", "write", "", row["id"], "",
                              1, _Svc(self.proj), _finalize)
        self.assertIn("Withdrew", out)

    def test_a_hook_filed_hold_lists_for_the_mcp_session(self):
        self._filed_request()
        out = handle_override("list", "", "", "write", "", "", "", 1,
                              _Svc(self.proj), _finalize)
        self.assertIn("1 request(s)", out)

    def test_a_c3_edit_retry_does_not_file_a_second_card(self):
        # Dedup keys on session_id among other fields, so two ids meant two
        # cards for one blocked write — "duplicates collapse" was false.
        self._filed_request()
        svc = _Svc(self.proj)
        denial = ag.check(str(self.proj / "CLAUDE.md"), "write", str(self.proj))
        rid, note = _grants.confirm_request(svc, denial, tool="Edit",
                                            op="write",
                                            path=str(self.proj / "CLAUDE.md"))
        self.assertEqual(note, "")
        self.assertEqual(len(orq.list_requests(project_path=str(self.proj))), 1)

    def test_a_genuinely_foreign_request_is_still_refused(self):
        # The fix is one identity, not no identity.
        row = self._filed_request()
        out = handle_override("status", "", "", "write", "", row["id"], "", 1,
                              _Svc(self.proj, host_id="other-session"),
                              _finalize)
        self.assertIn("another session", out)

    def test_the_message_names_both_ids(self):
        row = self._filed_request()
        out = handle_override("status", "", "", "write", "", row["id"], "", 1,
                              _Svc(self.proj, host_id="other-session"),
                              _finalize)
        self.assertIn(HOST_SESSION, out)      # who filed it
        self.assertIn("other-session", out)   # who is asking

    def test_host_id_wins_over_c3_id(self):
        self.assertEqual(_grants.session_id(_Svc(self.proj)), HOST_SESSION)

    def test_c3_id_is_the_fallback_when_there_is_no_host(self):
        # Codex/Antigravity export no session id; C3's own must still serve.
        svc = _Svc(self.proj, c3_id="20260902_120000", host_id=None)
        self.assertEqual(_grants.session_id(svc), "20260902_120000")

    def test_the_identity_never_resolves_empty(self):
        blank = type("S", (), {"session_mgr": None})()
        self.assertTrue(_grants.session_id(blank).startswith("pid-"))

    def test_start_session_records_the_host_id(self):
        from services.session_manager import SessionManager
        with mock.patch.dict(os.environ,
                             {SessionManager.HOST_SESSION_ENV: HOST_SESSION}):
            sm = SessionManager(str(self.proj))
            sm.start_session("t")
        self.assertEqual(sm.current_session["host_session_id"], HOST_SESSION)

    def test_start_session_survives_a_host_that_exports_nothing(self):
        from services.session_manager import SessionManager
        env = {k: v for k, v in os.environ.items()
               if k != SessionManager.HOST_SESSION_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            sm = SessionManager(str(self.proj))
            sm.start_session("t")
        self.assertEqual(sm.current_session["host_session_id"], "")


# ── 2. The tier must cover what the doc says it covers ────────────────────

class TestTierCoversEveryTrackedArtifact(ConfirmWiringBase):
    """Item 12 promises a hold on "the files that shape the agent itself" —
    the c3_artifacts set. The 2.100.0 tier listed the Claude Code files
    only, so an agent could add an MCP server through another IDE's config
    or rewrite the Copilot instructions with no hold at all."""

    def check(self, rel, op="write"):
        return ag.check(str(self.proj / rel), op, str(self.proj))

    def test_every_other_ide_config_surface_now_pauses(self):
        for rel in (".github/copilot-instructions.md", ".cursorrules",
                    ".vscode/mcp.json", ".cursor/mcp.json",
                    ".codex/config.toml", ".gemini/settings.json",
                    ".claude/plugins/p/plugin.json"):
            denial = self.check(rel)
            self.assertIsNotNone(denial, rel)
            self.assertEqual(denial.kind, "confirm", rel)

    def test_reads_stay_open_across_the_widened_tier(self):
        for rel in (".github/copilot-instructions.md", ".cursor/mcp.json",
                    ".claude/plugins/p/plugin.json"):
            self.assertIsNone(self.check(rel, "read"), rel)

    def test_the_tier_matches_the_artifact_table(self):
        """The invariant, not a list: every single-file artifact class that
        shapes the agent (instructions, mcp) must be covered. This is what
        keeps the two from drifting apart the next time a profile lands."""
        uncovered = []
        for rel, ref in artifact_defs._FILE_TABLE.items():
            classes = {ref.cls} | set(ref.roles)
            if not classes & {"instructions", "mcp"}:
                continue  # 'settings' is the hook-REGISTRATION write-deny
            denial = self.check(rel)
            if denial is None or denial.kind != "confirm":
                uncovered.append(rel)
        self.assertEqual(uncovered, [])

    def test_settings_registration_still_hard_denies(self):
        # The deliberate split: hook BODIES pause, REGISTRATION does not.
        denial = self.check(".claude/settings.local.json")
        self.assertEqual(denial.kind, "read_only")

    def test_every_tier_glob_is_mode_governable(self):
        for glob in ag.BUILTIN_CONFIRM_WRITE:
            self.assertIn(glob, ag.DISABLEABLE_BUILTINS, glob)
            self.assertIn(ag._norm_builtin(glob), ag._BUILTIN_VARIANTS, glob)


# ── 3. Shell writes are write-class ───────────────────────────────────────

class TestShellWriteScan(ConfirmWiringBase):
    """`>>`, `sed -i` and `cp` were evaluated as READS, and confirm is
    write-class — so the doc's "writes pause by default" was false for the
    one route an agent reaches for when a write is blocked."""

    def bash(self, cmd, session_id=HOST_SESSION):
        return hd.dispatch("pretool",
                           {"tool_name": "Bash", "session_id": session_id,
                            "tool_input": {"command": cmd}},
                           project_path=self.proj)

    def test_a_redirect_into_a_held_file_now_holds(self):
        out = self.bash(f'echo hi >> "{self.proj / "CLAUDE.md"}"')
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(ag.TAG_CONFIRM, self.reason(out))

    def test_the_shell_hold_files_a_request_like_any_other(self):
        self.bash(f'echo hi >> "{self.proj / "CLAUDE.md"}"')
        rows = orq.list_requests(project_path=str(self.proj))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["op"], "write")

    def test_sed_in_place_on_a_held_file_holds(self):
        out = self.bash(f'sed -i "s/a/b/" "{self.proj / "CLAUDE.md"}"')
        self.assertIn(ag.TAG_CONFIRM, self.reason(out))

    def test_a_heredoc_into_mcp_json_holds(self):
        out = self.bash(f'cat > "{self.proj / ".mcp.json"}" <<EOF\n{{}}\nEOF')
        self.assertIn(ag.TAG_CONFIRM, self.reason(out))

    def test_a_user_read_only_rule_refuses_a_shell_write(self):
        self.write_config(access={"read_only": ["docs/**"]})
        (self.proj / "docs").mkdir()
        out = self.bash(f'echo x > "{self.proj / "docs" / "a.md"}"')
        self.assertIn(ag.TAG_READ_ONLY, self.reason(out))

    def test_reading_a_held_file_is_still_free(self):
        self.assertIsNone(self.bash(f'cat "{self.proj / "CLAUDE.md"}"'))

    def test_an_ordinary_write_is_not_blocked(self):
        # It still collects the pre-existing "prefer c3_edit" nudge from
        # hook_pretool_enforce; what must not appear is a permission deny.
        out = self.bash(f'echo x > "{self.proj / "notes.txt"}"')
        self.assertNotIn("hookSpecificOutput", out or {})

    def test_a_command_naming_no_path_is_untouched(self):
        self.assertIsNone(self.bash("git status"))


# ── 4. A grant is spent only when the call actually proceeds ──────────────

class TestGrantConsumptionIsSettled(ConfirmWiringBase):
    """hook_access_guard ran first and consumed the grant; a strict-mode
    discipline deny from hook_pretool_enforce then won the merge. Grant
    spent, nothing written, and the user asked twice for one edit."""

    def _approved_grant(self):
        out = self.hook()
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")
        row = orq.list_requests(project_path=str(self.proj))[0]
        orq.decide(row["id"], orq.DECISION_APPROVE)
        return row

    def _live_grants(self):
        return og.active(str(self.proj), session_id=HOST_SESSION)

    def test_a_strict_mode_discipline_deny_does_not_spend_the_grant(self):
        """The reported repro, with no stubs: the access hold is approved,
        and the very next PreToolUse pass is denied by tool discipline
        (strict mode, no prior c3_* call). The user approved one edit; they
        must not have to approve it twice."""
        self._approved_grant()
        self.write_config(mode="strict")
        out = self.hook()
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[c3:enforce]",
                      out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(self._live_grants()), 1,
                         "a denied call must leave the grant live")
        # And the grant still works once discipline is satisfied.
        self.write_config(mode="advisory")
        allowed = self.hook()
        self.assertIn(opol.TAG_GRANTED, allowed["additionalContext"])

    def test_a_later_deny_does_not_spend_the_grant(self):
        self._approved_grant()
        deny = {"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "discipline"}}
        saved = dict(hd._RUN_CACHE)
        try:
            hd._RUN_CACHE["hook_pretool_enforce"] = (
                lambda p, pp=None: deny, "")
            out = self.hook()
            self.assertEqual(
                out["hookSpecificOutput"]["permissionDecision"], "deny")
        finally:
            hd._RUN_CACHE.clear()
            hd._RUN_CACHE.update(saved)
        self.assertEqual(len(self._live_grants()), 1,
                         "a denied call must leave the grant live")

    def test_the_retry_after_that_deny_still_works(self):
        # The point of not spending it: one approval, one write.
        self._approved_grant()
        out = self.hook()
        self.assertIn("additionalContext", out)
        self.assertIn(opol.TAG_GRANTED, out["additionalContext"])
        self.assertEqual(self._live_grants(), [])

    def test_an_allowed_call_still_consumes_exactly_one_use(self):
        self._approved_grant()
        self.hook()
        self.assertEqual(self._live_grants(), [])
        again = self.hook()
        self.assertEqual(
            again["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_no_on_allow_key_leaks_into_the_hook_output(self):
        # `_on_allow` is an internal callable; a host parsing hook JSON must
        # never see it.
        self._approved_grant()
        out = self.hook()
        self.assertNotIn("_on_allow", out)
        self.assertNotIn("_on_allow", json.dumps(out))

    def test_sub_hooks_after_a_deny_do_not_run(self):
        seen = []
        saved = dict(hd._RUN_CACHE)
        try:
            hd._RUN_CACHE["hook_pretool_enforce"] = (
                lambda p, pp=None: seen.append("ran"), "")
            self.hook()  # access guard denies first
        finally:
            hd._RUN_CACHE.clear()
            hd._RUN_CACHE.update(saved)
        self.assertEqual(seen, [])

    def test_peek_does_not_consume(self):
        self._approved_grant()
        denial = ag.check(str(self.proj / "CLAUDE.md"), "write", str(self.proj))
        line = og.gate_access(str(self.proj), denial, tool="Edit", op="write",
                              path=str(self.proj / "CLAUDE.md"),
                              session_id=HOST_SESSION, peek=True)
        self.assertTrue(line)
        self.assertEqual(len(self._live_grants()), 1)


# ── 5. The agent's artifact restore is a write like any other ─────────────

class TestArtifactRestoreHolds(ConfirmWiringBase):
    """Item 12 advertises `restore` in the same sentence that says these
    writes pause, but restore was exempt from the confirm tier and
    agent-callable — an unheld write path to CLAUDE.md, .mcp.json and every
    hook body. A human's click stays the approval; an agent's does not."""

    def _store_with_history(self, rel="CLAUDE.md"):
        from services.artifact_store import ArtifactStore
        store = ArtifactStore(str(self.proj))
        (self.proj / rel).write_text("v1\n", encoding="utf-8")
        store.scan()
        (self.proj / rel).write_text("v2\n", encoding="utf-8")
        store.scan()
        return store

    def _run(self, store, **kw):
        from cli.tools.artifacts import handle_artifacts
        svc = _Svc(self.proj)
        svc.artifact_store = store
        svc.hybrid_config = {}
        svc.edit_ledger = None
        return handle_artifacts("restore", svc, _finalize,
                                artifact="instructions:CLAUDE.md", **kw)

    def test_a_human_restore_is_still_exempt(self):
        store = self._store_with_history()
        self.assertTrue(store.restore("instructions:CLAUDE.md", 1)["restored"])
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"),
                         "v1\n")

    def test_an_agent_restore_holds_and_files(self):
        store = self._store_with_history()
        out = self._run(store, version=1)
        self.assertIn(ag.TAG_CONFIRM, out)
        self.assertEqual(
            len(orq.list_requests(project_path=str(self.proj))), 1)
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"),
                         "v2\n", "held restore must not write")

    def test_an_approved_agent_restore_goes_through(self):
        store = self._store_with_history()
        self._run(store, version=1)
        row = orq.list_requests(project_path=str(self.proj))[0]
        orq.decide(row["id"], orq.DECISION_APPROVE)
        out = self._run(store, version=1)
        self.assertIn("[artifacts:restored]", out)
        self.assertIn(opol.TAG_GRANTED, out)
        self.assertEqual((self.proj / "CLAUDE.md").read_text(encoding="utf-8"),
                         "v1\n")

    def test_a_user_rule_refuses_even_a_human_restore(self):
        store = self._store_with_history()
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"access": {"read_only": ["CLAUDE.md"]}}),
            encoding="utf-8")
        with self.assertRaises(ag.AccessDenied):
            store.restore("instructions:CLAUDE.md", 1)

    def test_the_confirm_mode_argument_is_validated(self):
        store = self._store_with_history()
        with self.assertRaises(ValueError):
            store.restore("instructions:CLAUDE.md", 1, confirm="yes-please")


if __name__ == "__main__":
    unittest.main()
