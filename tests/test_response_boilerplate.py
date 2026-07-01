"""Wave-2 Stream 3 (P6): response-boilerplate diet tests.

- Per-call "raw->optimized tok" ratio headers are OFF by default and restored
  by the ``hybrid.show_token_ratios`` config flag (SHOW_SAVINGS_SUMMARY
  convention).
- Every tool that lost its header was first migrated to
  finalize_with_tokens(), so the (raw, optimized) pair still reaches
  SessionManager.record_tool_tokens() and the per-tool telemetry JSONL
  structurally (source="structured") — accounting parity with the old
  regex-scraped summaries.
- c3_memory recall no longer prints per-fact salience unless asked
  (include_scores=True).
- c3_status budget breakdown lists only tools actually used, plus ONE
  aggregate savings line.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.tools._helpers import show_token_ratios
from cli.tools.compress import handle_compress
from cli.tools.filter import handle_filter
from cli.tools.memory import handle_memory
from cli.tools.search import handle_search
from cli.tools.status import handle_status
from services.session_manager import SessionManager
from services.telemetry import read_telemetry_records


def _noop_facts(svc, topic, top_k=3, width=100):
    return ""


def _make_sm(tmp: str) -> SessionManager:
    sm = SessionManager(tmp)
    sm.start_session("boilerplate diet test")
    return sm


def _finalize_via(sm: SessionManager):
    """Mimic mcp_server._finalize_response's accounting seam:
    log_tool_call → track_response (which emits the telemetry record)."""

    def finalize(name, args, resp, summ, **kw):
        sm.log_tool_call(name, args, summ)
        sm.track_response(name, resp, response_tokens=kw.get("response_tokens", 0))
        return resp

    return finalize


def _plain_finalize(name, args, resp, summ, **kw):
    return resp


# ---------------------------------------------------------------------------
# show_token_ratios flag helper
# ---------------------------------------------------------------------------

class TestShowTokenRatiosFlag(unittest.TestCase):
    def test_default_off(self):
        svc = mock.Mock()
        svc.hybrid_config = {}
        self.assertFalse(show_token_ratios(svc))

    def test_enabled_via_config(self):
        svc = mock.Mock()
        svc.hybrid_config = {"show_token_ratios": True}
        self.assertTrue(show_token_ratios(svc))

    def test_missing_config_is_safe(self):
        svc = mock.Mock(spec=[])  # no hybrid_config attribute
        self.assertFalse(show_token_ratios(svc))
        svc2 = mock.Mock()
        svc2.hybrid_config = None
        self.assertFalse(show_token_ratios(svc2))


# ---------------------------------------------------------------------------
# c3_filter — header diet + structured accounting
# ---------------------------------------------------------------------------

class TestFilterHeaders(unittest.TestCase):
    def _svc(self, tmp, ratios=False):
        svc = mock.Mock()
        svc.project_path = tmp
        svc.hybrid_config = {"show_token_ratios": True} if ratios else {}
        svc.session_mgr = _make_sm(tmp)
        svc.output_filter.filter.return_value = {
            "filtered": "line one\nline two",
            "raw_tokens": 500,
            "pass_used": 1,
            "llm_used": False,
        }
        return svc

    def test_text_mode_header_is_method_tag_only_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            out = handle_filter("", "noisy " * 100, "", 50, "fast", False,
                                svc, _finalize_via(svc.session_mgr))
            first_line = out.splitlines()[0]
            self.assertEqual(first_line, "[filter:pass1]")
            self.assertNotIn("tok", first_line)
            self.assertNotIn("%saved", out)

    def test_text_mode_flag_restores_ratio_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp, ratios=True)
            out = handle_filter("", "noisy " * 100, "", 50, "fast", False,
                                svc, _finalize_via(svc.session_mgr))
            first_line = out.splitlines()[0]
            self.assertIn("[filter:pass1]", first_line)
            self.assertIn("500→", first_line)
            self.assertIn("%saved", first_line)

    def test_text_mode_telemetry_parity(self):
        """Header gone, but the (raw, optimized) pair still reaches telemetry
        structurally — no regex fallback needed."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            handle_filter("", "noisy " * 100, "", 50, "fast", False,
                          svc, _finalize_via(svc.session_mgr))
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["tool"], "c3_filter")
            self.assertEqual(rec["source"], "structured")
            self.assertEqual(rec["raw_tokens"], 500)
            self.assertIsNotNone(rec["optimized_tokens"])
            usage = svc.session_mgr.current_session["token_usage"]
            self.assertEqual(usage["measured_ops"], 1)
            self.assertGreater(usage["estimated_saved_vs_full_read"], 0)

    def test_file_mode_header_off_by_default_on_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text("INFO starting\nERROR something bad\nINFO done\n",
                           encoding="utf-8")
            svc = self._svc(tmp)
            out = handle_filter("run.log", "", "", 50, "smart", False,
                                svc, _finalize_via(svc.session_mgr))
            self.assertTrue(out.startswith("[extract:.log]\n"))
            self.assertNotIn("->", out.splitlines()[0])
            self.assertNotIn("% saved", out)

            svc2 = self._svc(tmp, ratios=True)
            out2 = handle_filter("run.log", "", "", 50, "smart", False,
                                 svc2, _finalize_via(svc2.session_mgr))
            first = out2.splitlines()[0]
            self.assertIn("->", first)
            self.assertIn("% saved", first)

    def test_file_mode_telemetry_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text("INFO a\nINFO b\nERROR c\n", encoding="utf-8")
            svc = self._svc(tmp)
            handle_filter("run.log", "", "", 50, "smart", False,
                          svc, _finalize_via(svc.session_mgr))
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["tool"], "c3_filter")
            self.assertEqual(rec["source"], "structured")
            self.assertIsNotNone(rec["raw_tokens"])
            self.assertIsNotNone(rec["optimized_tokens"])
            # Summary no longer carries a "raw->opttok" pair for the regex
            # fallback — accounting is structural.
            summary = svc.session_mgr.current_session["tool_calls"][-1]["result_summary"]
            self.assertNotIn("->", summary)


# ---------------------------------------------------------------------------
# c3_search — structured accounting + transcript header diet
# ---------------------------------------------------------------------------

class TestSearchAccountingAndHeaders(unittest.TestCase):
    def _code_svc(self, tmp):
        svc = mock.Mock()
        svc.project_path = tmp
        svc.hybrid_config = {}
        svc.session_mgr = _make_sm(tmp)
        svc.indexer.search.return_value = [
            {"file": "a.py", "lines": "1-10", "name": "foo", "type": "function",
             "content": "def foo():\n    return 1", "tokens": 10,
             "score": 1.0, "file_tokens": 400},
        ]
        return svc

    def test_code_search_telemetry_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._code_svc(tmp)
            out = handle_search("foo", "code", 3, 1200, svc,
                                _finalize_via(svc.session_mgr), _noop_facts)
            self.assertIn("a.py", out)
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["tool"], "c3_search")
            self.assertEqual(rec["source"], "structured")
            self.assertEqual(rec["raw_tokens"], 400)
            self.assertEqual(rec["optimized_tokens"], 10)
            usage = svc.session_mgr.current_session["token_usage"]
            self.assertEqual(usage["estimated_saved_vs_full_read"], 390)
            self.assertEqual(usage["measured_ops"], 1)
            # Summary is a plain result count, not a scrapeable token pair
            summary = svc.session_mgr.current_session["tool_calls"][-1]["result_summary"]
            self.assertEqual(summary, "1r")

    def _transcript_svc(self, tmp, ratios=False):
        svc = mock.Mock()
        svc.project_path = tmp
        svc.hybrid_config = {"show_token_ratios": True} if ratios else {}
        svc.session_mgr = _make_sm(tmp)
        svc.convo_store.sync.return_value = {"available_sources": {"claude": True}}
        svc.convo_store.search.return_value = [{
            "tokens": 5, "ts": 1700000000, "source": "claude", "role": "user",
            "session_id": "abcdef12-3456-7890-aaaa-bbbbccccdddd",
            "score": 0.42, "text": "hello world",
        }]
        return svc

    def test_transcript_headers_minimal_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._transcript_svc(tmp)
            out = handle_search("hello", "transcript", 3, 1200, svc,
                                _finalize_via(svc.session_mgr), _noop_facts)
            first = out.splitlines()[0]
            self.assertEqual(first, "[transcript:hello] 1r")
            self.assertNotIn("tok", first)
            self.assertNotIn("score:", out)
            self.assertNotIn("abcdef12-3456-7890-aaaa-bbbbccccdddd", out)
            self.assertIn("--- claude:abcdef12", out)
            self.assertIn("hello world", out)

    def test_transcript_headers_full_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._transcript_svc(tmp, ratios=True)
            out = handle_search("hello", "transcript", 3, 1200, svc,
                                _finalize_via(svc.session_mgr), _noop_facts)
            first = out.splitlines()[0]
            self.assertIn(",5tok", first)
            self.assertIn("score:0.42", out)
            self.assertIn("abcdef12-3456-7890-aaaa-bbbbccccdddd", out)


# ---------------------------------------------------------------------------
# c3_compress — structured accounting + batch header diet
# ---------------------------------------------------------------------------

class TestCompressAccountingAndHeaders(unittest.TestCase):
    def _svc(self, tmp, ratios=False):
        svc = mock.Mock()
        svc.project_path = tmp
        svc.hybrid_config = {"show_token_ratios": True} if ratios else {}
        svc.session_mgr = _make_sm(tmp)
        svc.compressor.compress_file.return_value = {
            "compressed": "SUMMARY OF FILE",
            "original_tokens": 300,
            "compressed_tokens": 30,
        }
        svc.file_memory.drain_queue.return_value = []
        svc.file_memory.get_or_build_map.return_value = "def foo()\nclass Bar"
        return svc

    def test_smart_mode_telemetry_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            out = handle_compress("x.py", "smart", svc,
                                  _finalize_via(svc.session_mgr), _noop_facts)
            self.assertIn("SUMMARY OF FILE", out)
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["tool"], "c3_compress")
            self.assertEqual(rec["source"], "structured")
            self.assertEqual(rec["raw_tokens"], 300)
            self.assertEqual(rec["optimized_tokens"], 30)
            usage = svc.session_mgr.current_session["token_usage"]
            self.assertEqual(usage["estimated_saved_vs_full_read"], 270)
            summary = svc.session_mgr.current_session["tool_calls"][-1]["result_summary"]
            self.assertNotIn("->", summary)

    def test_map_mode_telemetry_parity(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "m.py").write_text("x = 1\n" * 50, encoding="utf-8")
            svc = self._svc(tmp)
            out = handle_compress("m.py", "map", svc,
                                  _finalize_via(svc.session_mgr), _noop_facts)
            self.assertIn("def foo()", out)
            recs = read_telemetry_records(tmp)
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual(rec["tool"], "c3_compress")
            self.assertEqual(rec["source"], "structured")
            self.assertGreater(rec["raw_tokens"], 0)
            self.assertGreater(rec["optimized_tokens"], 0)
            summary = svc.session_mgr.current_session["tool_calls"][-1]["result_summary"]
            self.assertEqual(summary, "map")

    def test_batch_per_file_headers_off_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.py", "b.py"):
                (Path(tmp) / name).write_text("y = 2\n", encoding="utf-8")
            svc = self._svc(tmp)
            out = handle_compress("a.py,b.py", "smart", svc,
                                  _finalize_via(svc.session_mgr), _noop_facts)
            self.assertIn("[compress:batch] 2/2 files (smart)", out)
            self.assertIn("## a.py\nSUMMARY OF FILE", out)
            self.assertIn("## b.py\nSUMMARY OF FILE", out)
            self.assertNotIn("->", out)
            # Aggregate accounting: batch totals flow structurally
            rec = read_telemetry_records(tmp)[0]
            self.assertEqual(rec["source"], "structured")
            self.assertEqual(rec["raw_tokens"], 600)
            self.assertEqual(rec["optimized_tokens"], 60)

    def test_batch_per_file_headers_restored_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.py", "b.py"):
                (Path(tmp) / name).write_text("y = 2\n", encoding="utf-8")
            svc = self._svc(tmp, ratios=True)
            out = handle_compress("a.py,b.py", "smart", svc,
                                  _finalize_via(svc.session_mgr), _noop_facts)
            self.assertIn("## a.py (300->30tok)", out)
            self.assertIn("## b.py (300->30tok)", out)


# ---------------------------------------------------------------------------
# c3_memory — recall salience display is opt-in
# ---------------------------------------------------------------------------

class TestMemoryRecallScores(unittest.TestCase):
    def _svc(self, tmp):
        svc = mock.Mock()
        svc.project_path = tmp
        svc.hybrid_config = {}
        svc.session_mgr = _make_sm(tmp)
        svc.memory.recall.return_value = [
            {"id": "f1", "category": "arch", "fact": "Fact one"},
            {"id": "f2", "category": "arch", "fact": "Fact two"},
        ]
        svc.vector_store = None
        svc.memory_graph = None
        svc.preloader = None
        scorer = mock.Mock()
        scorer.score.return_value = {"salience": 0.87, "tier": "core"}
        svc.memory_scorer = scorer
        return svc

    def test_recall_hides_scores_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            # top_k=5 was previously the "full enrichment" path that printed
            # sal=... per fact — now scores are opt-in.
            out = handle_memory("recall", "arch", "", "", 5, svc, _plain_finalize)
            self.assertIn("Fact one", out)
            self.assertNotIn("sal=", out)
            svc.memory_scorer.score.assert_not_called()

    def test_recall_shows_scores_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            out = handle_memory("recall", "arch", "", "", 5, svc, _plain_finalize,
                                include_scores=True)
            self.assertIn("sal=0.87/core", out)

    def test_include_scores_overrides_fast_mode(self):
        """Explicit include_scores=True wins even for small (top_k<=3) recalls."""
        with tempfile.TemporaryDirectory() as tmp:
            svc = self._svc(tmp)
            out = handle_memory("recall", "arch", "", "", 2, svc, _plain_finalize,
                                include_scores=True)
            self.assertIn("sal=0.87/core", out)


# ---------------------------------------------------------------------------
# c3_status — adaptive breakdown + single aggregate savings line
# ---------------------------------------------------------------------------

class TestStatusBudgetView(unittest.TestCase):
    def _svc(self, by_tool, token_usage=None):
        svc = mock.Mock()
        svc.session_mgr.get_budget_snapshot.return_value = {
            "response_tokens": 150, "threshold": 35000, "call_count": 3,
            "avg_tokens_per_call": 50, "content_tokens": 150, "infra_tokens": 0,
            "by_tool": by_tool, "c3_calls": 3, "native_calls": 0,
            "c3_adoption_pct": 100,
        }
        svc.session_mgr.current_session = {
            "started": "2026-07-01T00:00:00",
            "token_usage": token_usage or {},
        }
        svc.file_memory.list_tracked.return_value = []
        svc.indexer.get_stats.return_value = {
            "files_indexed": 0, "total_tokens_in_codebase": 0}
        return svc

    def test_breakdown_lists_only_used_tools(self):
        svc = self._svc({"c3_read": 100, "c3_search": 50, "c3_status": 0})
        out = handle_status("budget", False, svc, _plain_finalize)
        breakdown = [l for l in out.splitlines() if l.startswith("[breakdown]")]
        self.assertEqual(len(breakdown), 1)
        self.assertIn("c3_read:100tok", breakdown[0])
        self.assertIn("c3_search:50tok", breakdown[0])
        self.assertNotIn("c3_status", breakdown[0])
        self.assertNotIn("more", breakdown[0])  # no padding/tail for 2 tools

    def test_breakdown_omitted_when_no_tool_tokens(self):
        svc = self._svc({"c3_status": 0})
        out = handle_status("budget", False, svc, _plain_finalize)
        self.assertNotIn("[breakdown]", out)

    def test_single_aggregate_savings_line(self):
        svc = self._svc(
            {"c3_read": 100},
            token_usage={"estimated_saved_vs_full_read": 12345, "measured_ops": 7})
        out = handle_status("budget", False, svc, _plain_finalize)
        savings = [l for l in out.splitlines() if l.startswith("[savings]")]
        self.assertEqual(len(savings), 1)
        self.assertIn("(7 measured ops)", savings[0])
        self.assertIn("full-read baseline", savings[0])

    def test_no_savings_line_when_nothing_measured(self):
        svc = self._svc({"c3_read": 100})
        out = handle_status("budget", False, svc, _plain_finalize)
        self.assertNotIn("[savings]", out)


if __name__ == "__main__":
    unittest.main()
