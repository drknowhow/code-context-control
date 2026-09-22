"""Past sessions, one row each: find, orient, resume, mark stale (2.143.0).

WHAT WAS MISSING. Claude Code keeps every transcript and C3 keeps session
records, snapshots and tasks, but nothing joined them, so there was no way to
see "which session was I doing X in, is it worth going back to, how do I get
back" — and nothing could say a session was a dead end.

WHAT THESE PIN.
1. A transcript is oriented from its head and tail only: system reminders
   and meta rows never become the "first prompt"; the newest title wins; a
   title buried behind megabytes of tool output is still found.
2. The cache re-reads a transcript only when it changed.
3. Stale is a flag set by an agent or a person — hints never set it — and it
   folds last-wins from an append-only log.
4. One host session can span several C3 sessions; decisions, snapshots and
   tasks from all of them land on the one row.
5. Resume is built from a validated UUID whose transcript exists, is refused
   while the session is live, and never for another project's session.
6. Nothing here creates ``.c3`` in a project C3 does not manage.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import session_catalog as sc  # noqa: E402
from tests.session_fixtures import U1, U2, U3, U3B, SessionFixture, dumps  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = SessionFixture(Path(self._tmp.name))
        self._env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.fx.claude)})
        self._env.start()
        # No real git calls from a temp dir: every branch is "present".
        self._git = mock.patch.object(sc, "_local_branches", return_value=None)
        self._git.start()
        self.proj = self.fx.project("proj")

    def tearDown(self):
        self._git.stop()
        self._env.stop()
        self._tmp.cleanup()

    def rows(self, stale="all", **kw):
        return sc.list_sessions(self.proj, stale=stale, **kw)["sessions"]

    def row(self, sid, stale="all"):
        return next(r for r in self.rows(stale) if r["id"] == sid)


class TestTranscriptScan(_Base):
    def test_head_and_tail_fields(self):
        path = self.fx.transcript(
            self.proj, U1, prompt="<system-reminder>ignore me</system-reminder>Fix the login bug",
            title="Login bug", last_prompt="and the tests", bridge="cse_ABC123",
            branch="feat/login")
        meta = sc.scan_transcript(path)
        self.assertEqual(meta["first_prompt"], "Fix the login bug")
        self.assertEqual(meta["title"], "Login bug")          # newest, not the draft
        self.assertEqual(meta["title_source"], "ai")
        self.assertEqual(meta["last_prompt"], "and the tests")
        self.assertEqual(meta["bridge"], "cse_ABC123")
        self.assertEqual(meta["branch"], "feat/login")
        self.assertEqual(meta["started"], "2026-09-01T10:00:00.000Z")
        self.assertEqual(meta["last_ts"], "2026-09-01T11:00:00.000Z")

    def test_slash_command_prompt_reads_as_the_command(self):
        path = self.fx.transcript(
            self.proj, U1,
            prompt="<command-name>/plan</command-name><command-message>plan</command-message>"
                   "<command-args>ship the sessions view</command-args>")
        self.assertEqual(sc.scan_transcript(path)["first_prompt"], "/plan ship the sessions view")

    def test_shell_escape_reads_as_the_command_and_its_output_is_dropped(self):
        path = self.fx.transcript(self.proj, U1, prompt="<bash-input>git status</bash-input>")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(dumps({"type": "user", "message": {"role": "user", "content":
                "<bash-stdout>On branch main</bash-stdout><bash-stderr></bash-stderr>"}}) + "\n")
        self.assertEqual(sc.scan_transcript(path)["first_prompt"], "! git status")
        texts = [p["text"] for p in sc.transcript_preview(path)]
        self.assertFalse(any("On branch main" in t or "bash-" in t for t in texts))

    def test_title_behind_a_large_tail_is_still_found(self):
        path = self.fx.transcript(self.proj, U1, title="Deep title", title_after_filler=True)
        self.assertGreater(path.stat().st_size, sc.TAIL_STEPS[0])
        meta = sc.scan_transcript(path)
        self.assertEqual(meta["title"], "Deep title")

    def test_quoted_title_row_inside_a_tool_result_is_not_a_title(self):
        path = self.fx.transcript(self.proj, U1, title=None, prompt="Real prompt")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(dumps({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result",
                 "content": dumps({"type": "ai-title", "aiTitle": "Not mine"})}]}}) + "\n")
        meta = sc.scan_transcript(path)
        self.assertEqual(meta["title"], "Real prompt")
        self.assertEqual(meta["title_source"], "first_prompt")

    def test_remote_url_only_for_the_known_bridge_shape(self):
        self.assertEqual(sc.remote_url("cse_0195mU83"), "https://claude.ai/code/session_0195mU83")
        self.assertIsNone(sc.remote_url("session_0195"))
        self.assertIsNone(sc.remote_url("cse_bad/../x"))
        self.assertIsNone(sc.remote_url(""))


class TestListing(_Base):
    def test_rows_newest_first_and_only_uuid_transcripts(self):
        self.fx.transcript(self.proj, U1, ended="2026-09-01T11:00:00.000Z",
                           mtime=time.time() - 3 * 86400)
        self.fx.transcript(self.proj, U2, ended="2026-09-02T11:00:00.000Z",
                           mtime=time.time() - 2 * 86400)
        (self.fx.tdir(self.proj) / "agent-sidechain.jsonl").write_text("{}\n", encoding="utf-8")
        ids = [r["id"] for r in self.rows()]
        self.assertEqual(ids, [U2, U1])

    def test_transcript_from_another_directory_is_excluded(self):
        other = self.fx.project("other")
        self.fx.transcript(self.proj, U1, cwd=other)    # sits in our folder, ran elsewhere
        self.fx.transcript(self.proj, U2)
        self.assertEqual([r["id"] for r in self.rows()], [U2])

    def test_cache_rereads_only_a_changed_transcript(self):
        path = self.fx.transcript(self.proj, U1, title="First")
        self.assertEqual(self.row(U1)["title"], "First")
        with mock.patch.object(sc, "scan_transcript", wraps=sc.scan_transcript) as spy:
            self.rows()
            self.assertEqual(spy.call_count, 0)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(dumps({"type": "ai-title", "aiTitle": "Second", "sessionId": U1}) + "\n")
            os.utime(path, (time.time() + 5, time.time() + 5))
            self.assertEqual(self.row(U1)["title"], "Second")
            self.assertEqual(spy.call_count, 1)

    def test_query_matches_title_prompt_and_id(self):
        self.fx.transcript(self.proj, U1, title="Login bug", prompt="Fix the login bug")
        self.fx.transcript(self.proj, U2, title="Docs pass", prompt="Rewrite the README")
        self.assertEqual([r["id"] for r in self.rows(q="readme")], [U2])
        self.assertEqual([r["id"] for r in self.rows(q="login bug")], [U1])
        self.assertEqual([r["id"] for r in self.rows(q=U2[:8])], [U2])

    def test_listing_never_creates_c3_in_an_unmanaged_project(self):
        bare = self.fx.project("bare", init=False)
        self.fx.transcript(bare, U1)
        self.assertEqual(len(sc.list_sessions(bare, stale="all")["sessions"]), 1)
        self.assertFalse((bare / ".c3").exists())

    def test_list_many_merges_projects_and_skips_unmanaged(self):
        other = self.fx.project("other")
        bare = self.fx.project("bare", init=False)
        self.fx.transcript(self.proj, U1, mtime=time.time() - 100)
        self.fx.transcript(other, U2, mtime=time.time() - 10)
        self.fx.transcript(bare, U3)
        res = sc.list_many([{"name": "proj", "path": str(self.proj)},
                            {"name": "other", "path": str(other)},
                            {"name": "bare", "path": str(bare)}], stale="all")
        self.assertEqual([r["id"] for r in res["sessions"]], [U2, U1])
        self.assertEqual(res["sessions"][0]["project"]["name"], "other")
        self.assertEqual(res["errors"], [])


class TestJoins(_Base):
    def test_one_host_session_many_c3_sessions(self):
        self.fx.transcript(self.proj, U1)
        self.fx.record(self.proj, "20260901_100000_aaaaaaaaaaaa", U1, decisions=["use JSONL"])
        self.fx.record(self.proj, "20260901_103000_bbbbbbbbbbbb", U1, decisions=["ship it", "x"])
        row = self.row(U1)
        self.assertEqual(sorted(row["links"]["c3_sessions"]),
                         ["20260901_100000_aaaaaaaaaaaa", "20260901_103000_bbbbbbbbbbbb"])
        self.assertEqual(row["links"]["decisions"], 3)
        detail = sc.get_session(self.proj, U1)
        self.assertIn("ship it", detail["decisions"])

    def test_snapshot_is_the_note_until_an_explicit_note_exists(self):
        self.fx.transcript(self.proj, U1)
        self.fx.record(self.proj, "20260901_100000_aaaaaaaaaaaa", U1)
        self.fx.snapshot(self.proj, "20260901_100000_aaaaaaaaaaaa", "Wire the hub tab",
                         "Desk segment next")
        note = self.row(U1)["note"]
        self.assertEqual((note["source"], note["summary"], note["next_steps"]),
                         ("snapshot", "Wire the hub tab", "Desk segment next"))
        sc.mark(self.proj, U1, "note", summary="Hub tab done", next_steps="Desk", by="agent")
        note = self.row(U1)["note"]
        self.assertEqual((note["source"], note["summary"]), ("mark", "Hub tab done"))

    def test_machine_written_snapshot_is_not_a_note(self):
        self.fx.transcript(self.proj, U1, last_prompt="and the tests")
        self.fx.record(self.proj, "20260901_100000_aaaaaaaaaaaa", U1)
        for i, label in enumerate(("auto-snapshot on stop", "auto_budget_snapshot",
                                   "MCP server session", "auto-checkpoint before /clear")):
            self.fx.snapshot(self.proj, "20260901_100000_aaaaaaaaaaaa", label, "Budget at 80%",
                             created=f"2026-09-01T10:3{i}:00+00:00")
            (self.proj / ".c3" / "snapshots" / "snap_20260901_100000.json").rename(
                self.proj / ".c3" / "snapshots" / f"snap_auto_{i}.json")
        row = self.row(U1)
        self.assertIsNone(row["note"])
        self.assertEqual(row["links"]["snapshots"], 4)

    def test_tasks_join_by_origin_session_and_by_session_link(self):
        self.fx.transcript(self.proj, U1)
        self.fx.transcript(self.proj, U2)
        self.fx.record(self.proj, "20260901_100000_aaaaaaaaaaaa", U1)
        self.fx.tasks(self.proj, [
            {"id": "t1", "title": "A", "status": "done", "lifecycle": "active",
             "origin_session": "20260901_100000_aaaaaaaaaaaa", "links": []},
            {"id": "t2", "title": "B", "status": "backlog", "lifecycle": "active",
             "origin_session": "", "links": [{"type": "session", "ref": U2}]},
            {"id": "t3", "title": "gone", "status": "backlog", "lifecycle": "archived",
             "origin_session": "20260901_100000_aaaaaaaaaaaa", "links": []},
        ])
        self.assertEqual(self.row(U1)["links"]["tasks"], 1)
        self.assertEqual(self.row(U2)["links"]["tasks"], 1)
        self.assertEqual([t["id"] for t in sc.get_session(self.proj, U2)["tasks"]], ["t2"])

    def test_other_host_record_is_listed_but_not_resumable(self):
        self.fx.record(self.proj, "20260901_100000_cccccccccccc", "thread-1", system="codex",
                       description="Codex refactor")
        row = self.row("thread-1")
        self.assertEqual(row["provider"], "codex")
        self.assertEqual(row["title"], "Codex refactor")
        self.assertFalse(row["resume"]["can_launch"])
        self.assertIn("Claude Code", row["resume"]["why_not"])

    def test_worktree_session_transcript_is_found_under_its_own_folder(self):
        wt = self.proj / ".claude" / "worktrees" / "feat"
        wt.mkdir(parents=True)
        self.fx.transcript(wt, U1, cwd=wt)
        self.fx.record(self.proj, "20260901_100000_aaaaaaaaaaaa", U1)
        row = self.row(U1)
        self.assertTrue(row["resume"]["can_launch"])
        spec = sc.resume_spec(self.proj, U1)
        self.assertTrue(Path(spec["cwd"]).samefile(wt))

    def test_detail_preview_strips_reminders_and_collapses_tools(self):
        path = self.fx.transcript(self.proj, U1,
                                  prompt="<system-reminder>x</system-reminder>Hello")
        with open(path, "a", encoding="utf-8") as fh:
            for name in ("mcp__c3__c3_shell", "mcp__c3__c3_shell", "Read"):
                fh.write(dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": name, "input": {}}]}}) + "\n")
        preview = sc.get_session(self.proj, U1)["preview"]
        texts = [p["text"] for p in preview]
        self.assertIn("Hello", texts)
        self.assertTrue(any("⚙ Bash" in t for t in texts))
        self.assertIn("⚙ c3_shell ×2 · Read", texts[-1])
        self.assertFalse(any("system-reminder" in t for t in texts))


class TestMarks(_Base):
    def setUp(self):
        super().setUp()
        self.fx.transcript(self.proj, U1)
        self.fx.transcript(self.proj, U2)

    def test_stale_hides_unstale_restores(self):
        res = sc.mark(self.proj, U1, "stale", reason="superseded by U2", successor=U2,
                      by="agent", by_session=U2)
        self.assertNotIn("error", res)
        self.assertEqual([r["id"] for r in self.rows("hide")], [U2])
        stale = self.rows("only")
        self.assertEqual([r["id"] for r in stale], [U1])
        self.assertEqual(stale[0]["stale"]["reason"], "superseded by U2")
        self.assertEqual(stale[0]["links"]["successor"], U2)
        sc.mark(self.proj, U1, "unstale", by="user")
        self.assertIsNone(self.row(U1, "hide")["stale"])
        lines = (self.proj / ".c3" / "session_marks.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)                    # append-only audit trail

    def test_mark_errors_carry_http_shaped_status(self):
        self.assertEqual(sc.mark(self.proj, U1, "delete")["status"], 400)
        self.assertEqual(sc.mark(self.proj, "44444444-0000-4000-8000-000000000000",
                                 "stale")["status"], 404)
        self.assertEqual(sc.mark(self.proj, U1, "stale", successor=U1)["status"], 400)
        self.assertEqual(sc.mark(self.proj, U1, "note")["status"], 400)
        bare = self.fx.project("bare", init=False)
        self.fx.transcript(bare, U3)
        self.assertEqual(sc.mark(bare, U3, "stale")["status"], 409)
        self.assertFalse((bare / ".c3").exists())

    def test_mark_writes_an_activity_row(self):
        sc.mark(self.proj, U1, "stale", reason="done", by="user")
        log = (self.proj / ".c3" / "activity_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"session_mark"', log)
        self.assertIn(U1, log)

    def test_prefix_resolution(self):
        self.fx.transcript(self.proj, U3)
        self.fx.transcript(self.proj, U3B)
        self.assertEqual(sc.resolve_id(self.proj, U1[:8]), (U1, ""))
        self.assertIn("too short", sc.resolve_id(self.proj, U1[:4])[1])
        self.assertIn("matches 2", sc.resolve_id(self.proj, U3[:8])[1])
        self.assertEqual(sc.resolve_id(self.proj, U3[:10])[0], U3)


class TestHints(_Base):
    def test_hints_never_set_stale(self):
        old = time.time() - 30 * 86400
        self.fx.transcript(self.proj, U1, ended="2026-08-01T11:00:00.000Z", mtime=old)
        row = self.row(U1, "hide")
        self.assertIsNone(row["stale"])
        self.assertTrue(any(h.startswith("idle ") for h in row["hints"]))
        self.assertIn(U1, [r["id"] for r in self.rows("likely")])

    def test_idle_days_is_configurable(self):
        (self.proj / ".c3" / "config.json").write_text('{"sessions": {"idle_days": 60}}',
                                                       encoding="utf-8")
        self.fx.transcript(self.proj, U1, ended="2026-08-01T11:00:00.000Z",
                           mtime=time.time() - 30 * 86400)
        self.assertFalse(any(h.startswith("idle ") for h in self.row(U1)["hints"]))

    def test_clear_chain_links_predecessor_and_successor(self):
        self.fx.transcript(self.proj, U1)
        self.fx.transcript(self.proj, U2)
        self.fx.activity(self.proj, [
            {"timestamp": "2026-09-01T11:00:00+00:00", "type": "session_end",
             "host_session_id": U1, "reason": "clear"},
            {"timestamp": "2026-09-01T11:00:03+00:00", "type": "session_open",
             "host_session_id": U2, "start_source": "clear"},
        ])
        self.assertEqual(self.row(U1)["links"]["successor"], U2)
        self.assertEqual(self.row(U2)["links"]["predecessor"], U1)
        self.assertIn("ended by /clear", self.row(U1)["hints"])

    def test_branch_gone(self):
        self.fx.transcript(self.proj, U1, branch="feat/old")
        with mock.patch.object(sc, "_local_branches", return_value={"main"}):
            self.assertIn("branch gone", self.row(U1)["hints"])

    def test_live_session_gets_no_hints(self):
        self.fx.transcript(self.proj, U1, mtime=time.time() - 30 * 86400)
        self.fx.heartbeat(self.proj, "20260901_100000_aaaaaaaaaaaa", U1)
        row = self.row(U1)
        self.assertTrue(row["live"])
        self.assertEqual(row["hints"], [])


class TestResume(_Base):
    def test_spec_is_a_fixed_argv_in_the_session_cwd(self):
        self.fx.transcript(self.proj, U1, bridge="cse_XYZ")
        spec = sc.resume_spec(self.proj, U1[:8])
        self.assertEqual(spec["argv"], ["claude", "--resume", U1])
        self.assertEqual(spec["command"], f"claude --resume {U1}")
        self.assertTrue(Path(spec["cwd"]).samefile(self.proj))
        self.assertEqual(spec["row"]["resume"]["remote_url"],
                         "https://claude.ai/code/session_XYZ")

    def test_live_session_is_refused_with_409(self):
        self.fx.transcript(self.proj, U1)
        self.fx.heartbeat(self.proj, "20260901_100000_aaaaaaaaaaaa", U1)
        spec = sc.resume_spec(self.proj, U1)
        self.assertEqual(spec["status"], 409)
        self.assertNotIn("argv", spec)

    def test_unknown_traversal_and_foreign_ids_are_404(self):
        other = self.fx.project("other")
        self.fx.transcript(other, U2)
        self.fx.transcript(self.proj, U1)
        for ref in ("../../etc/passwd", U2, "not-a-uuid-at-all", ""):
            spec = sc.resume_spec(self.proj, ref)
            self.assertEqual(spec.get("status"), 404, ref)
            self.assertNotIn("argv", spec)


class TestRootIndex(_Base):
    def test_a_worktree_transcript_created_after_a_listing_shows_on_the_next(self):
        wt = self.proj / ".claude" / "worktrees" / "feat"
        wt.mkdir(parents=True)
        self.fx.tdir(wt)
        self.fx.record(self.proj, "20260901_100000_aaaaaaaaaaaa", U1)
        self.assertFalse(self.row(U1)["resume"]["can_launch"])
        self.fx.transcript(wt, U1, cwd=wt)
        self.assertTrue(self.row(U1)["resume"]["can_launch"])

    def test_a_transcript_in_another_folder_is_read_once_across_listings(self):
        wt = self.proj / ".claude" / "worktrees" / "feat"
        wt.mkdir(parents=True)
        self.fx.transcript(wt, U1, cwd=wt)
        self.fx.record(self.proj, "20260901_100000_aaaaaaaaaaaa", U1)
        self.assertTrue(self.row(U1)["resume"]["can_launch"])
        with mock.patch.object(sc, "scan_transcript", wraps=sc.scan_transcript) as scan:
            self.assertTrue(self.row(U1)["resume"]["can_launch"])
        scan.assert_not_called()

    def test_one_index_lists_the_folders_once(self):
        self.fx.transcript(self.proj, U1)
        index = sc._RootIndex(sc.claude_projects_root())
        self.assertIsNotNone(index.owner(U1))
        with mock.patch.object(sc.os, "scandir") as scandir:
            self.assertIsNone(index.owner(U2))
            self.assertTrue(index.dirs())
        scandir.assert_not_called()


if __name__ == "__main__":
    unittest.main()
