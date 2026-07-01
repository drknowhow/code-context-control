"""Tests for the honest measurement layer (Stream A):

- structured per-tool token accounting (SessionManager.record_tool_tokens
  + cli.tools._helpers.finalize_with_tokens)
- per-tool telemetry JSONL writing (.c3/tool_telemetry.jsonl), including
  failure-safety
- the aggregation query (services.telemetry.aggregate_tool_telemetry)
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.tools._helpers import finalize_with_tokens
from services.session_manager import SessionManager
from services.telemetry import (
    aggregate_tool_telemetry,
    append_telemetry_record,
    read_telemetry_records,
    telemetry_path,
)


def _make_session_manager(tmpdir: str) -> SessionManager:
    sm = SessionManager(tmpdir)
    sm.start_session("test session")
    return sm


class TestAppendTelemetryRecord(unittest.TestCase):
    def test_appends_jsonl_record_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = append_telemetry_record(tmp, {"tool": "c3_read", "response_tokens": 42})
            self.assertTrue(ok)
            path = telemetry_path(tmp)
            self.assertTrue(path.exists())
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            # Full schema present, defaults filled
            for key in ("ts", "session_id", "tool", "action", "response_tokens",
                        "raw_tokens", "optimized_tokens", "duration_ms"):
                self.assertIn(key, rec)
            self.assertEqual(rec["tool"], "c3_read")
            self.assertEqual(rec["response_tokens"], 42)
            self.assertIsNone(rec["raw_tokens"])
            self.assertTrue(rec["ts"])  # auto-filled timestamp

    def test_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            append_telemetry_record(tmp, {"tool": "a"})
            append_telemetry_record(tmp, {"tool": "b"})
            lines = telemetry_path(tmp).read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)

    def test_failure_safe_returns_false_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Make ".c3" a FILE so mkdir(parents=True) fails
            (Path(tmp) / ".c3").write_text("not a directory", encoding="utf-8")
            ok = append_telemetry_record(tmp, {"tool": "c3_read"})
            self.assertFalse(ok)

    def test_failure_safe_on_unserializable_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            # default=str in json.dumps means even odd values serialize;
            # this must not raise either way.
            ok = append_telemetry_record(tmp, {"tool": "x", "duration_ms": object()})
            self.assertIn(ok, (True, False))


class TestReadTelemetryRecords(unittest.TestCase):
    def test_skips_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = telemetry_path(tmp)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "tool": "good"})
                + "\n{not json}\n\n" + json.dumps({"tool": "also_good"}) + "\n",
                encoding="utf-8",
            )
            recs = read_telemetry_records(tmp)
            self.assertEqual([r["tool"] for r in recs], ["good", "also_good"])

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(read_telemetry_records(tmp), [])


class TestAggregateToolTelemetry(unittest.TestCase):
    def _write(self, tmp, *records):
        for r in records:
            self.assertTrue(append_telemetry_record(tmp, r))

    def test_per_tool_aggregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc).isoformat()
            self._write(
                tmp,
                {"ts": now, "tool": "c3_read", "response_tokens": 100,
                 "raw_tokens": 1000, "optimized_tokens": 100, "duration_ms": 10},
                {"ts": now, "tool": "c3_read", "response_tokens": 50,
                 "raw_tokens": 500, "optimized_tokens": 50, "duration_ms": 30},
                {"ts": now, "tool": "c3_filter", "response_tokens": 30},
            )
            agg = aggregate_tool_telemetry(tmp, days=7)
            self.assertEqual(agg["total_calls"], 3)
            self.assertEqual(agg["total_response_tokens"], 180)
            self.assertEqual(agg["estimated_saved_vs_full_read"], 1350)
            read = agg["by_tool"]["c3_read"]
            self.assertEqual(read["calls"], 2)
            self.assertEqual(read["response_tokens"], 150)
            self.assertEqual(read["raw_tokens"], 1500)
            self.assertEqual(read["optimized_tokens"], 150)
            self.assertEqual(read["measured_calls"], 2)
            self.assertEqual(read["estimated_saved_vs_full_read"], 1350)
            self.assertEqual(read["avg_duration_ms"], 20.0)
            filt = agg["by_tool"]["c3_filter"]
            self.assertEqual(filt["calls"], 1)
            self.assertEqual(filt["measured_calls"], 0)
            self.assertEqual(filt["estimated_saved_vs_full_read"], 0)
            self.assertIsNone(filt["avg_duration_ms"])
            # Savings are labeled honestly
            self.assertIn("baseline", agg["baseline_note"].lower())

    def test_window_filtering(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(timezone.utc)
            old = (now - timedelta(days=30)).isoformat()
            self._write(
                tmp,
                {"ts": now.isoformat(), "tool": "c3_read", "response_tokens": 10},
                {"ts": old, "tool": "c3_read", "response_tokens": 999},
            )
            agg = aggregate_tool_telemetry(tmp, days=7)
            self.assertEqual(agg["total_calls"], 1)
            self.assertEqual(agg["total_response_tokens"], 10)
            # days <= 0 means all records
            agg_all = aggregate_tool_telemetry(tmp, days=0)
            self.assertEqual(agg_all["total_calls"], 2)
            self.assertIsNone(agg_all["since"])

    def test_empty_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            agg = aggregate_tool_telemetry(tmp, days=7)
            self.assertEqual(agg["total_calls"], 0)
            self.assertEqual(agg["by_tool"], {})


class TestStructuredAccounting(unittest.TestCase):
    def test_record_tool_tokens_accumulates_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.record_tool_tokens("c3_read", raw_tokens=1000, optimized_tokens=100)
            usage = sm.current_session["token_usage"]
            self.assertEqual(usage["estimated_saved_vs_full_read"], 900)
            self.assertEqual(usage["estimated_used"], 100)
            self.assertEqual(usage["measured_ops"], 1)

    def test_structured_path_prevents_regex_double_count(self):
        """When a tool reports structured tokens, the legacy summary regex
        must NOT accumulate the same pair a second time."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.record_tool_tokens("c3_read", raw_tokens=1000, optimized_tokens=100)
            # Same call also carries the legacy summary encoding
            sm.log_tool_call("c3_read", {"file": "x.py"}, "1000->100tok")
            usage = sm.current_session["token_usage"]
            self.assertEqual(usage["estimated_saved_vs_full_read"], 900)  # not 1800
            self.assertEqual(usage["measured_ops"], 1)

    def test_legacy_summary_fallback_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.log_tool_call("c3_compress", {"mode": "map"}, "2000->500tok")
            usage = sm.current_session["token_usage"]
            self.assertEqual(usage["estimated_saved_vs_full_read"], 1500)
            self.assertEqual(usage["estimated_used"], 500)
            self.assertEqual(usage["measured_ops"], 1)

    def test_invalid_values_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.record_tool_tokens("c3_read", raw_tokens=-5, optimized_tokens="junk")
            usage = sm.current_session["token_usage"]
            self.assertEqual(usage["estimated_saved_vs_full_read"], 0)
            self.assertEqual(usage["measured_ops"], 0)


class TestTelemetryEmission(unittest.TestCase):
    def test_track_response_emits_structured_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.record_tool_tokens("c3_read", raw_tokens=1000, optimized_tokens=100,
                                  duration_ms=12.5)
            sm.log_tool_call("c3_read", {"file": "x.py", "action": "symbols"},
                             "1000->100tok")
            sm.track_response("c3_read", "response text", response_tokens=100)

            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["tool"], "c3_read")
            self.assertEqual(rec["action"], "symbols")
            self.assertEqual(rec["session_id"], sm.current_session["id"])
            self.assertEqual(rec["response_tokens"], 100)
            self.assertEqual(rec["raw_tokens"], 1000)
            self.assertEqual(rec["optimized_tokens"], 100)
            self.assertEqual(rec["duration_ms"], 12.5)
            self.assertEqual(rec["source"], "structured")
            self.assertTrue(rec["ts"])

    def test_track_response_emits_summary_fallback_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.log_tool_call("c3_compress", {"mode": "map"}, "2000->500tok")
            sm.track_response("c3_compress", "map text", response_tokens=500)
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["raw_tokens"], 2000)
            self.assertEqual(rec["optimized_tokens"], 500)
            self.assertEqual(rec["source"], "summary")
            self.assertEqual(rec["action"], "map")
            self.assertIsNone(rec["duration_ms"])

    def test_record_without_token_info_still_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.log_tool_call("c3_status", {"action": "health"}, "ok")
            sm.track_response("c3_status", "healthy", response_tokens=5)
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["tool"], "c3_status")
            self.assertIsNone(recs[0]["raw_tokens"])
            self.assertIsNone(recs[0]["source"])

    def test_pending_cleared_between_calls(self):
        """Structured data from call N must not leak into call N+1."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.record_tool_tokens("c3_read", raw_tokens=1000, optimized_tokens=100)
            sm.log_tool_call("c3_read", {"file": "x.py"}, "1000->100tok")
            sm.track_response("c3_read", "resp", response_tokens=100)
            # Second call: plain tool, no structured data
            sm.log_tool_call("c3_status", {"action": "health"}, "ok")
            sm.track_response("c3_status", "healthy", response_tokens=5)
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 2)
            self.assertIsNone(recs[1]["raw_tokens"])
            self.assertIsNone(recs[1]["source"])

    def test_stale_pending_from_other_tool_discarded(self):
        """A pending record from an aborted call must not be attributed to
        a different tool."""
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            sm.record_tool_tokens("c3_read", raw_tokens=1000, optimized_tokens=100)
            # The c3_read finalize never happens; a different tool runs next
            sm.log_tool_call("c3_status", {"action": "health"}, "ok")
            sm.track_response("c3_status", "healthy", response_tokens=5)
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["tool"], "c3_status")
            self.assertIsNone(recs[0]["raw_tokens"])

    def test_telemetry_write_failure_never_breaks_tool_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            with mock.patch("services.session_manager.append_telemetry_record",
                            side_effect=RuntimeError("disk on fire")):
                sm.log_tool_call("c3_read", {"file": "x.py"}, "1000->100tok")
                # Must not raise
                sm.track_response("c3_read", "resp", response_tokens=100)
            # Budget accounting still happened
            self.assertEqual(
                sm.current_session["context_budget"]["by_tool"]["c3_read"], 100)


class TestFinalizeWithTokens(unittest.TestCase):
    def _finalize(self, name, args, resp, summ, **kw):
        self.finalize_calls.append((name, args, resp, summ, kw))
        return resp

    def setUp(self):
        self.finalize_calls = []

    def test_records_and_delegates(self):
        with tempfile.TemporaryDirectory() as tmp:
            sm = _make_session_manager(tmp)
            svc = mock.Mock()
            svc.session_mgr = sm
            out = finalize_with_tokens(
                self._finalize, svc, "c3_read", {"file": "x.py"},
                "resp body", "1000->100tok",
                raw_tokens=1000, optimized_tokens=100, response_tokens=100)
            self.assertEqual(out, "resp body")
            self.assertEqual(len(self.finalize_calls), 1)
            name, args, resp, summ, kw = self.finalize_calls[0]
            self.assertEqual(name, "c3_read")
            self.assertEqual(kw, {"response_tokens": 100})
            usage = sm.current_session["token_usage"]
            self.assertEqual(usage["estimated_saved_vs_full_read"], 900)

    def test_no_session_mgr_is_safe(self):
        svc = mock.Mock(spec=[])  # no session_mgr attribute
        out = finalize_with_tokens(self._finalize, svc, "c3_read", {}, "resp", "s",
                                   raw_tokens=10, optimized_tokens=5)
        self.assertEqual(out, "resp")

    def test_recording_error_is_safe(self):
        svc = mock.Mock()
        svc.session_mgr.record_tool_tokens.side_effect = RuntimeError("boom")
        out = finalize_with_tokens(self._finalize, svc, "c3_read", {}, "resp", "s",
                                   raw_tokens=10, optimized_tokens=5)
        self.assertEqual(out, "resp")

    def test_no_token_values_skips_recording(self):
        svc = mock.Mock()
        out = finalize_with_tokens(self._finalize, svc, "c3_read", {}, "resp", "s")
        self.assertEqual(out, "resp")
        svc.session_mgr.record_tool_tokens.assert_not_called()


if __name__ == "__main__":
    unittest.main()
