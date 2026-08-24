"""Tests for the token-tracking surface added in v2.95.0.

The measurement layer already wrote a rich per-tool log; nothing read it back,
so the only number a user could see was a running counter. These tests cover
the three gaps that closed:

1. file attribution — a record now says WHICH file the call was about
   (SessionManager._derive_target), so "which file cost me tokens" is
   answerable rather than merely plausible.
2. the Stop hook — it read ``usage``/``cost_usd`` from an event that sends
   neither, and wrote 498 consecutive all-zero rows without failing. It now
   sums the transcript it is handed.
3. the read surfaces — aggregate_session_stats plus the /api/tokens routes
   and the cross-project overview.

The load-bearing assertion throughout: a log full of zeros must never be
reportable as "you used nothing". Absence of measurement and measurement of
absence are different claims and the code has to keep them apart.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.hook_session_stats import read_transcript_usage
from cli.hook_session_stats import run as stats_run
from services.session_manager import SessionManager
from services.telemetry import (
    aggregate_session_stats,
    aggregate_tool_telemetry,
    append_telemetry_record,
)


def _transcript(path: Path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _assistant(inp=0, out=0, cc=0, cr=0, model="claude-opus-5"):
    return {"type": "assistant", "message": {"model": model, "usage": {
        "input_tokens": inp, "output_tokens": out,
        "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}}}


class TestTargetAttribution(unittest.TestCase):
    """A tool call records what it was ABOUT, not just which tool ran."""

    def _sm(self, tmp):
        (Path(tmp) / ".c3").mkdir(exist_ok=True)
        sm = SessionManager(tmp)
        sm.start_session("t")
        return sm

    def test_absolute_path_becomes_project_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = self._sm(tmp)
            target = sm._derive_target({"file_path": str(Path(tmp) / "cli" / "server.py")})
            self.assertEqual(target, "cli/server.py")

    def test_relative_path_is_kept_and_slash_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = self._sm(tmp)
            self.assertEqual(sm._derive_target({"path": "services/x.py"}), "services/x.py")

    def test_a_path_outside_the_project_degrades_to_its_name(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            sm = self._sm(tmp)
            outside = str(Path(other) / "elsewhere.py")
            self.assertEqual(sm._derive_target({"file_path": outside}), "elsewhere.py")

    def test_a_call_with_no_file_has_no_target(self):
        """A query is not a location — inventing one poisons the file view."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = self._sm(tmp)
            self.assertEqual(sm._derive_target({"query": "def foo", "action": "code"}), "")
            self.assertEqual(sm._derive_target({}), "")

    def test_target_reaches_the_jsonl_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = self._sm(tmp)
            sm.log_tool_call("c3_read", {"file_path": "a/b.py", "action": "read"})
            sm.track_response("c3_read", "x" * 40)
            sm.log_tool_call("c3_search", {"query": "zzz"})
            sm.track_response("c3_search", "y" * 40)
            rows = [json.loads(l) for l in
                    open(Path(tmp) / ".c3" / "tool_telemetry.jsonl", encoding="utf-8")]
            self.assertEqual(rows[0]["target"], "a/b.py")
            self.assertEqual(rows[1]["target"], "")


class TestAggregationDimensions(unittest.TestCase):
    """by_day / by_session / by_target — the three questions by_tool can't answer."""

    def _seed(self, tmp):
        for rec in (
            {"ts": "2026-08-01T10:00:00+00:00", "session_id": "s1", "tool": "c3_read",
             "response_tokens": 100, "target": "a.py"},
            {"ts": "2026-08-01T11:00:00+00:00", "session_id": "s1", "tool": "c3_read",
             "response_tokens": 50, "target": "a.py"},
            {"ts": "2026-08-02T10:00:00+00:00", "session_id": "s2", "tool": "c3_edit",
             "response_tokens": 20, "target": "b.py"},
        ):
            append_telemetry_record(tmp, rec)

    def test_days_sessions_and_targets_roll_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed(tmp)
            agg = aggregate_tool_telemetry(tmp, days=0)
            self.assertEqual([d["name"] for d in agg["by_day"]],
                             ["2026-08-01", "2026-08-02"])   # chronological
            self.assertEqual(agg["by_day"][0]["response_tokens"], 150)

            top_session = agg["by_session"][0]
            self.assertEqual(top_session["name"], "s1")
            self.assertEqual(top_session["response_tokens"], 150)
            self.assertEqual(top_session["calls"], 2)

            self.assertEqual(agg["by_target"][0]["name"], "a.py")
            self.assertEqual(agg["by_target"][0]["response_tokens"], 150)
            self.assertEqual(agg["targets_tracked"], 2)

    def test_records_without_a_target_do_not_become_a_blank_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            append_telemetry_record(tmp, {"ts": "2026-08-01T10:00:00+00:00",
                                          "tool": "c3_search", "response_tokens": 9})
            agg = aggregate_tool_telemetry(tmp, days=0)
            self.assertEqual(agg["by_target"], [])
            self.assertEqual(agg["total_calls"], 1)  # still counted everywhere else


class TestTranscriptUsage(unittest.TestCase):
    """The Stop hook's actual job: read the transcript it is handed."""

    def test_sums_usage_across_assistant_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp) / "t.jsonl"
            _transcript(tpath, [
                {"type": "user", "message": {"content": "hi"}},
                _assistant(inp=10, out=100, cc=5, cr=1000),
                _assistant(inp=2, out=50, cc=0, cr=2000),
            ])
            u = read_transcript_usage(str(tpath))
            self.assertEqual(u["input_tokens"], 12)
            self.assertEqual(u["output_tokens"], 150)
            self.assertEqual(u["cache_creation_tokens"], 5)
            self.assertEqual(u["cache_read_tokens"], 3000)
            self.assertEqual(u["messages"], 2)
            self.assertEqual(u["model"], "claude-opus-5")

    def test_a_truncated_final_line_does_not_lose_the_rest(self):
        """Transcripts are appended live; the last line can be half-written."""
        with tempfile.TemporaryDirectory() as tmp:
            tpath = Path(tmp) / "t.jsonl"
            with open(tpath, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(_assistant(out=42)) + "\n")
                fh.write('{"type": "assist')
            self.assertEqual(read_transcript_usage(str(tpath))["output_tokens"], 42)

    def test_missing_transcript_is_zero_with_no_messages(self):
        u = read_transcript_usage("/no/such/file.jsonl")
        self.assertEqual(u["messages"], 0)
        self.assertEqual(u["output_tokens"], 0)

    def test_run_reads_the_transcript_when_the_event_omits_usage(self):
        """The exact shape Claude Code actually sends: no usage, no cost."""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            (proj / ".c3").mkdir()
            tpath = proj / "t.jsonl"
            _transcript(tpath, [_assistant(inp=1, out=200, cc=3, cr=7)])
            stats_run({"session_id": "abc", "transcript_path": str(tpath),
                       "hook_event_name": "Stop"}, project_path=proj)
            row = json.loads((proj / ".c3" / "session_stats.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["output_tokens"], 200)
            self.assertEqual(row["cache_read_tokens"], 7)
            self.assertEqual(row["source"], "transcript")
            self.assertEqual(row["assistant_messages"], 1)

    def test_payload_usage_wins_when_a_future_version_sends_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            (proj / ".c3").mkdir()
            stats_run({"session_id": "abc", "transcript_path": "/nope",
                       "usage": {"input_tokens": 5, "output_tokens": 6}},
                      project_path=proj)
            row = json.loads((proj / ".c3" / "session_stats.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["output_tokens"], 6)
            self.assertEqual(row["source"], "payload")


class TestSessionStatsAggregate(unittest.TestCase):
    def _write(self, proj, rows):
        (proj / ".c3").mkdir(exist_ok=True)
        with open(proj / ".c3" / "session_stats.jsonl", "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_rows_are_cumulative_so_only_the_newest_per_session_counts(self):
        """Stop fires per turn and re-sums the transcript. Adding rows would
        report a session that ran ten turns as ten sessions' worth of cost."""
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            self._write(proj, [
                {"ts": "2026-08-01T10:00:00+00:00", "session_id": "s1", "output_tokens": 100},
                {"ts": "2026-08-01T11:00:00+00:00", "session_id": "s1", "output_tokens": 250},
                {"ts": "2026-08-01T12:00:00+00:00", "session_id": "s2", "output_tokens": 30},
            ])
            agg = aggregate_session_stats(proj)
            self.assertEqual(agg["session_count"], 2)
            self.assertEqual(agg["total_tokens"], 280)   # 250 + 30, not 380
            self.assertEqual(agg["sessions"][0]["session_id"], "s2")  # newest first

    def test_an_all_zero_log_is_reported_as_such_not_as_no_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            self._write(proj, [
                {"ts": "2026-08-01T10:00:00+00:00", "session_id": "s1", "output_tokens": 0},
                {"ts": "2026-08-01T11:00:00+00:00", "session_id": "s2", "output_tokens": 0},
            ])
            agg = aggregate_session_stats(proj)
            self.assertEqual(agg["rows_seen"], 2)
            self.assertEqual(agg["all_zero_rows"], 2)
            self.assertEqual(agg["total_tokens"], 0)

    def test_a_missing_log_is_empty_rather_than_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            agg = aggregate_session_stats(Path(tmp))
            self.assertEqual(agg["session_count"], 0)
            self.assertEqual(agg["rows_seen"], 0)


class TestTokenRoutes(unittest.TestCase):
    """The read surfaces. Counts only — never transcript content."""

    def _seed(self, proj):
        (proj / ".c3").mkdir(exist_ok=True)
        append_telemetry_record(str(proj), {
            "ts": "2026-08-01T10:00:00+00:00", "session_id": "s1",
            "tool": "c3_read", "response_tokens": 100, "target": "a.py"})

    def test_hub_overview_isolates_a_project_it_cannot_read(self):
        from unittest import mock

        import cli.hub_server as hub_server

        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good"
            good.mkdir()
            self._seed(good)
            rows = [{"name": "good", "path": str(good)},
                    {"name": "bare", "path": str(Path(tmp) / "bare")}]

            class _PM:
                def list_projects(self):
                    return rows

            with mock.patch.object(hub_server, "_pm", new=lambda: _PM()):
                client = hub_server.app.test_client()
                data = client.get("/api/hub/tokens/overview?days=0").get_json()

            by_name = {r["name"]: r for r in data["projects"]}
            self.assertEqual(by_name["good"]["tool_tokens"], 100)
            # "we could not look" must not render as a confident zero
            self.assertFalse(by_name["bare"]["initialized"])
            self.assertEqual(data["totals"]["tool_tokens"], 100)

    def test_days_param_is_clamped_not_trusted(self):
        from unittest import mock

        import cli.hub_server as hub_server

        with tempfile.TemporaryDirectory() as tmp:
            class _PM:
                def list_projects(self):
                    return []

            with mock.patch.object(hub_server, "_pm", new=lambda: _PM()):
                client = hub_server.app.test_client()
                for raw, expect in (("-5", 0), ("999999", 3650), ("junk", 30)):
                    data = client.get(f"/api/hub/tokens/overview?days={raw}").get_json()
                    self.assertEqual(data["days"], expect, raw)


if __name__ == "__main__":
    unittest.main()
