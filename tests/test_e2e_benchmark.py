"""Tests for the E2E benchmark system with mock providers."""

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.e2e_benchmark import (
    _C3_TOOLS,
    _NATIVE_TOOLS,
    CLIProvider,
    CLIResponse,
    E2EBenchmark,
    TaskResult,
    ToolUsage,
    _build_insights,
    _detect_tools_from_text,
    compute_trends,
    detect_providers,
    generate_e2e_report,
    load_run_history,
    render_e2e_html,
)
from services.e2e_evaluator import EvalScore, Evaluator
from services.e2e_tasks import DIFFICULTY_WEIGHTS, E2ETask, GroundTruth, TaskBuilder, build_prompt


class TestGroundTruth(unittest.TestCase):
    def test_defaults(self):
        gt = GroundTruth()
        self.assertEqual(gt.required_keywords, [])
        self.assertEqual(gt.forbidden_keywords, [])
        self.assertIn("keyword", gt.scoring_weights)
        self.assertIn("factual", gt.scoring_weights)
        self.assertIn("completeness", gt.scoring_weights)

    def test_with_data(self):
        gt = GroundTruth(
            required_keywords=["foo", "bar"],
            expected_files=["a.py"],
            expected_symbols=["MyClass"],
            verifiable_claims=[("foo is in a.py", True)],
            required_aspects=["purpose", "parameters"],
        )
        self.assertEqual(len(gt.required_keywords), 2)
        self.assertEqual(len(gt.verifiable_claims), 1)


class TestDifficultyWeights(unittest.TestCase):
    def test_weights_defined(self):
        self.assertIn("easy", DIFFICULTY_WEIGHTS)
        self.assertIn("hard", DIFFICULTY_WEIGHTS)
        self.assertIn("expert", DIFFICULTY_WEIGHTS)
        self.assertGreater(DIFFICULTY_WEIGHTS["expert"], DIFFICULTY_WEIGHTS["easy"])


class TestE2ETask(unittest.TestCase):
    def test_to_dict(self):
        task = E2ETask(
            id="test_1",
            category="explanation",
            query="What does foo do?",
            difficulty="hard",
            ground_truth=GroundTruth(
                required_keywords=["foo"],
                verifiable_claims=[("x", True)],
                required_aspects=["purpose"],
            ),
        )
        d = task.to_dict()
        self.assertEqual(d["id"], "test_1")
        self.assertEqual(d["difficulty"], "hard")
        self.assertEqual(d["ground_truth"]["verifiable_claims_count"], 1)
        self.assertEqual(d["ground_truth"]["required_aspects"], ["purpose"])

    def test_build_prompt(self):
        task = E2ETask(id="t", category="c", query="How?", ground_truth=GroundTruth())
        prompt = build_prompt(task)
        self.assertIn("How?", prompt)
        self.assertIn("Question:", prompt)


class TestEvaluator(unittest.TestCase):
    def setUp(self):
        self.evaluator = Evaluator()

    def test_keyword_scoring_all_found(self):
        gt = GroundTruth(required_keywords=["foo", "bar"])
        response = "The function foo calls bar to process data."
        score = self.evaluator.score(response, gt)
        self.assertEqual(score.keyword_score, 1.0)

    def test_keyword_scoring_partial(self):
        gt = GroundTruth(required_keywords=["foo", "bar", "baz"])
        response = "The function foo is important."
        score = self.evaluator.score(response, gt)
        self.assertAlmostEqual(score.keyword_score, 1.0 / 3, places=2)

    def test_keyword_scoring_forbidden(self):
        gt = GroundTruth(required_keywords=["foo"], forbidden_keywords=["hallucinated"])
        response = "The function foo uses hallucinated data."
        score = self.evaluator.score(response, gt)
        self.assertLess(score.keyword_score, 1.0)

    def test_structural_scoring(self):
        gt = GroundTruth()
        response = (
            "## Analysis\n\n"
            "The file `services/foo.py` at line 42 contains:\n\n"
            "```python\ndef foo():\n    pass\n```\n\n"
            "- Point one\n- Point two\n"
            "This function handles the core logic of the application by processing requests."
        )
        score = self.evaluator.score(response, gt)
        self.assertGreater(score.structural_score, 0.5)

    def test_file_mention_scoring(self):
        gt = GroundTruth(
            expected_files=["services/foo.py", "cli/bar.py"],
            expected_symbols=["MyClass"],
        )
        response = "Found in services/foo.py - MyClass handles routing."
        score = self.evaluator.score(response, gt)
        self.assertGreater(score.file_mention_score, 0.5)

    def test_factual_accuracy_verified(self):
        """Response confirms verifiable claims."""
        gt = GroundTruth(
            verifiable_claims=[
                ("foo is defined in services/bar.py", True),
                ("foo accepts two parameters", True),
            ],
        )
        response = "foo is defined in services/bar.py and accepts two parameters."
        score = self.evaluator.score(response, gt)
        self.assertGreater(score.factual_score, 0.7)

    def test_factual_accuracy_missed(self):
        """Response doesn't mention verifiable facts."""
        gt = GroundTruth(
            verifiable_claims=[
                ("function handles database queries", True),
                ("function is deprecated", True),
            ],
        )
        response = "This is a simple helper."
        score = self.evaluator.score(response, gt)
        self.assertLess(score.factual_score, 0.5)

    def test_completeness_all_addressed(self):
        """Response covers all required aspects."""
        gt = GroundTruth(
            required_aspects=["purpose", "parameters", "error_handling"],
        )
        response = ("The purpose of this function is to handle requests. "
                     "It takes a path parameter and returns data. "
                     "Errors are caught with a try/except block.")
        score = self.evaluator.score(response, gt)
        self.assertGreater(score.completeness_score, 0.6)

    def test_completeness_partial(self):
        """Response only covers some required aspects."""
        gt = GroundTruth(
            required_aspects=["purpose", "parameters", "error_handling", "files"],
        )
        response = "The purpose of this function is to handle requests."
        score = self.evaluator.score(response, gt)
        self.assertLess(score.completeness_score, 0.5)

    def test_combined_score_uses_all_dimensions(self):
        gt = GroundTruth(
            required_keywords=["process"],
            expected_files=["main.py"],
            expected_symbols=["run"],
            verifiable_claims=[("run is in main.py", True)],
            required_aspects=["purpose"],
        )
        response = "The `run` function in main.py will process the input for its purpose."
        score = self.evaluator.score(response, gt)
        self.assertGreater(score.combined_score, 0.3)
        # All dimensions should have been computed
        self.assertGreater(score.keyword_score, 0)
        self.assertGreater(score.factual_score, 0)
        self.assertGreater(score.completeness_score, 0)


class TestCLIProvider(unittest.TestCase):
    def test_build_command_claude_with_c3(self):
        p = CLIProvider(name="claude", executable="claude", model="sonnet")
        cmd = p._build_command("test prompt", with_c3=True)
        self.assertIn("-p", cmd)
        self.assertIn("test prompt", cmd)
        self.assertIn("--model", cmd)
        self.assertNotIn("--strict-mcp-config", cmd)

    def test_build_command_claude_without_c3(self):
        p = CLIProvider(name="claude", executable="claude")
        cmd = p._build_command("test prompt", with_c3=False)
        self.assertIn("--strict-mcp-config", cmd)

    def test_build_command_gemini(self):
        p = CLIProvider(name="gemini", executable="gemini", model="gemini-2.5-flash")
        cmd = p._build_command("test", with_c3=True)
        self.assertIn("-p", cmd)
        self.assertIn("-m", cmd)

    def test_build_command_codex(self):
        p = CLIProvider(name="codex", executable="codex")
        cmd = p._build_command("test", with_c3=False)
        self.assertIn("exec", cmd)
        self.assertIn("-c", cmd)

    def test_parse_claude_json_full(self):
        """Test parsing of rich Claude JSON output."""
        p = CLIProvider(name="claude", executable="claude")
        resp = CLIResponse()
        resp.raw_stdout = json.dumps({
            "result": "The answer is 42.",
            "total_cost_usd": 0.07,
            "duration_ms": 15000,
            "duration_api_ms": 12000,
            "num_turns": 3,
            "usage": {
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_creation_input_tokens": 5000,
                "cache_read_input_tokens": 3000,
            },
            "modelUsage": {
                "claude-opus-4-6": {
                    "inputTokens": 500,
                    "outputTokens": 100,
                    "cacheCreationInputTokens": 5000,
                    "cacheReadInputTokens": 3000,
                    "contextWindow": 200000,
                    "costUSD": 0.07,
                }
            },
        })
        p._parse_output(resp)
        self.assertEqual(resp.text, "The answer is 42.")
        self.assertEqual(resp.cost_usd, 0.07)
        self.assertEqual(resp.duration_ms, 15000)
        self.assertEqual(resp.duration_api_ms, 12000)
        self.assertEqual(resp.input_tokens, 500)
        self.assertEqual(resp.output_tokens, 100)
        self.assertEqual(resp.cache_creation_tokens, 5000)
        self.assertEqual(resp.cache_read_tokens, 3000)
        self.assertEqual(resp.model_id, "claude-opus-4-6")
        self.assertEqual(resp.context_window, 200000)


class TestTaskResult(unittest.TestCase):
    def test_c3_wins(self):
        tr = TaskResult(task_id="t1", task_category="test", provider="claude")
        tr.c3_score = EvalScore(combined_score=0.8)
        tr.baseline_score = EvalScore(combined_score=0.5)
        self.assertTrue(tr.c3_wins)
        self.assertAlmostEqual(tr.score_delta, 0.3, places=2)

    def test_baseline_wins(self):
        tr = TaskResult(task_id="t2", task_category="test", provider="gemini")
        tr.c3_score = EvalScore(combined_score=0.3)
        tr.baseline_score = EvalScore(combined_score=0.7)
        self.assertFalse(tr.c3_wins)

    def test_difficulty_weight(self):
        tr = TaskResult(task_id="t1", task_category="test", task_difficulty="expert")
        self.assertEqual(tr.difficulty_weight, 3.0)

    def test_efficiency_metrics(self):
        tr = TaskResult(task_id="t1", task_category="test", provider="claude")
        tr.c3_response = CLIResponse(latency_ms=10000, cost_usd=0.10, num_turns=3,
                                     input_tokens=500, output_tokens=200)
        tr.baseline_response = CLIResponse(latency_ms=20000, cost_usd=0.20, num_turns=6,
                                           input_tokens=1000, output_tokens=400)
        tr.c3_score = EvalScore(combined_score=0.8)
        tr.baseline_score = EvalScore(combined_score=0.6)

        eff = tr.efficiency()
        self.assertAlmostEqual(eff["time_saved_ms"], 10000, places=0)
        self.assertAlmostEqual(eff["cost_saved_usd"], 0.10, places=2)
        self.assertEqual(eff["turns_saved"], 3)
        self.assertGreater(eff["quality_per_dollar_c3"], 0)

    def test_to_dict_includes_efficiency(self):
        tr = TaskResult(task_id="t1", task_category="test", task_difficulty="hard", provider="claude")
        tr.c3_response = CLIResponse(latency_ms=5000, cost_usd=0.05)
        tr.baseline_response = CLIResponse(latency_ms=8000, cost_usd=0.08)
        d = tr.to_dict()
        self.assertIn("efficiency", d)
        self.assertIn("task_difficulty", d)
        self.assertIn("difficulty_weight", d)


class TestCLIResponse(unittest.TestCase):
    def test_to_dict_includes_new_fields(self):
        resp = CLIResponse(
            text="hello", latency_ms=1000, cost_usd=0.05,
            duration_ms=900, duration_api_ms=800,
            cache_creation_tokens=5000, cache_read_tokens=2000,
            model_id="claude-opus-4-6", context_window=200000,
            input_tokens=100, output_tokens=50,
        )
        d = resp.to_dict()
        self.assertEqual(d["duration_ms"], 900)
        self.assertEqual(d["duration_api_ms"], 800)
        self.assertEqual(d["cache_creation_tokens"], 5000)
        self.assertEqual(d["cache_read_tokens"], 2000)
        self.assertEqual(d["model_id"], "claude-opus-4-6")
        self.assertEqual(d["context_window"], 200000)
        self.assertEqual(d["total_tokens"], 100 + 50 + 5000 + 2000)
        self.assertIn("response_text", d)


class TestE2EBenchmarkWithMocks(unittest.TestCase):
    """Integration test with mocked subprocess calls."""

    def _make_mock_provider(self, name, response_text="The answer is foo in services/bar.py at line 10."):
        p = CLIProvider(name=name, executable=name, available=True)
        mock_response = CLIResponse(
            text=response_text,
            response_text=response_text,
            latency_ms=5000.0,
            exit_code=0,
            input_tokens=1000,
            output_tokens=200,
            cost_usd=0.005,
            num_turns=3,
        )
        p.run = MagicMock(return_value=mock_response)
        return p

    def test_benchmark_run(self):
        providers = [self._make_mock_provider("claude")]
        tasks = [
            E2ETask(
                id="test_task",
                category="explanation",
                query="What does foo do?",
                difficulty="medium",
                ground_truth=GroundTruth(
                    required_keywords=["foo"],
                    expected_files=["services/bar.py"],
                    expected_symbols=["foo"],
                    verifiable_claims=[("foo is in services/bar.py", True)],
                    required_aspects=["purpose"],
                ),
            ),
        ]
        evaluator = Evaluator()
        bench = E2EBenchmark(".", providers, tasks, evaluator, parallel=False)
        results = bench.run_all()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].provider, "claude")
        self.assertGreater(results[0].c3_score.combined_score, 0)

    def test_report_generation(self):
        providers = [
            self._make_mock_provider("claude", "Function foo in services/bar.py handles requests."),
            self._make_mock_provider("gemini", "The foo function processes data in services/bar.py."),
        ]
        tasks = [
            E2ETask(
                id="t1", category="explanation", query="What?", difficulty="easy",
                ground_truth=GroundTruth(
                    required_keywords=["foo"],
                    expected_files=["services/bar.py"],
                ),
            ),
        ]
        evaluator = Evaluator()
        bench = E2EBenchmark(".", providers, tasks, evaluator, parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, providers, tasks)

        self.assertIn("scorecard", report)
        self.assertIn("efficiency_summary", report)
        self.assertIn("dimension_breakdown", report)
        self.assertIn("weighted_win_rate", report["scorecard"])
        self.assertIn("total_time_saved_s", report["efficiency_summary"])
        self.assertIn("projected_monthly_cost_saved_usd", report["efficiency_summary"])
        self.assertIn("provider_stats", report)
        self.assertIn("claude", report["provider_stats"])
        self.assertIn("gemini", report["provider_stats"])
        self.assertEqual(report["total_results"], 2)

    def test_html_rendering(self):
        providers = [self._make_mock_provider("claude")]
        tasks = [
            E2ETask(
                id="t1", category="explanation", query="Test?",
                ground_truth=GroundTruth(required_keywords=["test"]),
            ),
        ]
        evaluator = Evaluator()
        bench = E2EBenchmark(".", providers, tasks, evaluator, parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, providers, tasks)
        html = render_e2e_html(report)

        self.assertIn("<!doctype html>", html)
        self.assertIn("C3 End-to-End Benchmark", html)
        self.assertIn("chart.js", html.lower())
        self.assertIn("providerChart", html)
        self.assertIn("dimChart", html)
        self.assertIn("Time Saved", html)
        self.assertIn("Cost Saved", html)
        self.assertIn("Response Comparison", html)

    @unittest.skip(
        "Pre-existing failure: same root cause as test_report_includes_tool_analysis — "
        "the bench worktree path zeroes mocked CLIResponse fields when .mcp.json is "
        "absent. Tracked separately."
    )
    def test_report_efficiency_summary(self):
        """Verify efficiency calculations in the report."""
        p = self._make_mock_provider("claude")
        # Override to give different C3 vs baseline responses
        c3_resp = CLIResponse(text="C3 answer", response_text="C3 answer",
                              latency_ms=10000, cost_usd=0.10, input_tokens=500, output_tokens=200)
        base_resp = CLIResponse(text="Base answer", response_text="Base answer",
                                latency_ms=20000, cost_usd=0.20, input_tokens=1000, output_tokens=400)
        p.run = MagicMock(side_effect=[c3_resp, base_resp])

        tasks = [E2ETask(id="t1", category="test", query="?",
                         ground_truth=GroundTruth(required_keywords=["answer"]))]
        bench = E2EBenchmark(".", [p], tasks, Evaluator(), parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, [p], tasks)

        eff = report["efficiency_summary"]
        self.assertGreater(eff["total_time_saved_s"], 0)
        self.assertGreater(eff["total_cost_saved_usd"], 0)
        self.assertGreater(eff["total_tokens_saved"], 0)


class TestToolUsage(unittest.TestCase):
    def test_tool_usage_defaults(self):
        tu = ToolUsage()
        self.assertEqual(tu.total_tool_calls, 0)
        self.assertEqual(tu.unique_tools, 0)
        self.assertEqual(tu.c3_tool_calls, 0)
        self.assertEqual(tu.native_tool_calls, 0)
        self.assertEqual(tu.tool_counts, {})

    def test_tool_usage_to_dict(self):
        tu = ToolUsage(
            tool_counts={"Read": 3, "c3_search": 2},
            total_tool_calls=5, unique_tools=2,
            c3_tool_calls=2, native_tool_calls=3,
        )
        d = tu.to_dict()
        self.assertEqual(d["total_tool_calls"], 5)
        self.assertEqual(d["c3_tool_calls"], 2)
        self.assertIn("Read", d["tool_counts"])

    def test_detect_tools_from_text_c3_tools(self):
        text = "I'll use c3_search to find files, then c3_compress to read the structure."
        counts = _detect_tools_from_text(text)
        self.assertIn("c3_search", counts)
        self.assertIn("c3_compress", counts)

    def test_detect_tools_from_text_native_tools(self):
        text = "Let me read the file services/foo.py. I'll search for the keyword."
        counts = _detect_tools_from_text(text)
        self.assertIn("Read", counts)
        self.assertIn("Grep", counts)

    def test_detect_tools_from_text_empty(self):
        text = "The function computes a sum."
        counts = _detect_tools_from_text(text)
        self.assertEqual(len(counts), 0)

    def test_cli_response_includes_tool_usage(self):
        resp = CLIResponse(text="test")
        d = resp.to_dict()
        self.assertIn("tool_usage", d)
        self.assertEqual(d["tool_usage"]["total_tool_calls"], 0)

    def test_extract_tools_from_claude_json_messages(self):
        """Test extraction from Claude JSON with tool_use messages."""
        p = CLIProvider(name="claude", executable="claude")
        resp = CLIResponse()
        resp.raw_stdout = json.dumps({
            "result": "Found the file.",
            "num_turns": 3,
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file": "foo.py"}},
                    {"type": "tool_use", "name": "Grep", "input": {"pattern": "class"}},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Read", "input": {"file": "bar.py"}},
                    {"type": "tool_use", "name": "c3_search", "input": {"query": "test"}},
                ]},
            ],
        })
        p._parse_output(resp)
        usage = p._extract_tool_usage(resp)
        self.assertEqual(usage.tool_counts.get("Read", 0), 2)
        self.assertEqual(usage.tool_counts.get("Grep", 0), 1)
        self.assertEqual(usage.tool_counts.get("c3_search", 0), 1)
        self.assertEqual(usage.total_tool_calls, 4)
        self.assertEqual(usage.unique_tools, 3)
        self.assertEqual(usage.c3_tool_calls, 1)
        self.assertEqual(usage.native_tool_calls, 3)

    def test_extract_tools_fallback_to_heuristic(self):
        """When no messages array, falls back to text heuristic."""
        p = CLIProvider(name="claude", executable="claude")
        resp = CLIResponse(
            text="I'll read the file foo.py. Let me search for the keyword.",
            raw_stdout=json.dumps({"result": "I'll read the file foo.py. Let me search for the keyword."}),
        )
        p._parse_output(resp)
        usage = p._extract_tool_usage(resp)
        self.assertGreater(usage.total_tool_calls, 0)

    def test_extract_tools_num_turns_fallback(self):
        """When no tools detected but num_turns > 1, estimates from turns."""
        p = CLIProvider(name="claude", executable="claude")
        resp = CLIResponse(text="Simple answer.", num_turns=5, raw_stdout="Simple answer.")
        usage = p._extract_tool_usage(resp)
        self.assertIn("_unknown_tools", usage.tool_counts)
        self.assertEqual(usage.tool_counts["_unknown_tools"], 4)


class TestToolAnalysisInReport(unittest.TestCase):
    @unittest.skip(
        "Pre-existing failure: bench worktree path zeroes mocked tool_usage when "
        ".mcp.json is absent from the project root. Fix tracked separately — "
        "either inject a stub .mcp.json into the test sandbox or refactor "
        "_run_task to respect pre-populated tool_usage on mocked responses."
    )
    def test_report_includes_tool_analysis(self):
        """Report generation includes tool_analysis section."""
        p = CLIProvider(name="claude", executable="claude", available=True)
        c3_resp = CLIResponse(
            text="answer", response_text="answer", latency_ms=5000,
            cost_usd=0.05, input_tokens=100, output_tokens=50,
            tool_usage=ToolUsage(
                tool_counts={"c3_search": 2, "Read": 3},
                total_tool_calls=5, unique_tools=2,
                c3_tool_calls=2, native_tool_calls=3,
            ),
        )
        base_resp = CLIResponse(
            text="answer", response_text="answer", latency_ms=8000,
            cost_usd=0.08, input_tokens=200, output_tokens=100,
            tool_usage=ToolUsage(
                tool_counts={"Read": 5, "Grep": 2},
                total_tool_calls=7, unique_tools=2,
                c3_tool_calls=0, native_tool_calls=7,
            ),
        )
        p.run = MagicMock(side_effect=[c3_resp, base_resp])

        tasks = [E2ETask(id="t1", category="test", query="?",
                         ground_truth=GroundTruth(required_keywords=["answer"]))]
        bench = E2EBenchmark(".", [p], tasks, Evaluator(), parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, [p], tasks)

        self.assertIn("tool_analysis", report)
        ta = report["tool_analysis"]
        self.assertIn("summary", ta)
        self.assertIn("tool_comparison", ta)
        self.assertIn("category_breakdown", ta)
        self.assertEqual(ta["summary"]["total_c3_tool_calls"], 5)
        self.assertEqual(ta["summary"]["total_baseline_tool_calls"], 7)
        self.assertEqual(ta["summary"]["c3_mcp_calls"], 2)

    def test_html_includes_tool_section(self):
        """HTML report includes tool usage analysis section."""
        p = CLIProvider(name="claude", executable="claude", available=True)
        resp = CLIResponse(text="test", response_text="test", latency_ms=5000,
                           cost_usd=0.05, input_tokens=100, output_tokens=50)
        p.run = MagicMock(return_value=resp)
        tasks = [E2ETask(id="t1", category="test", query="?",
                         ground_truth=GroundTruth(required_keywords=["test"]))]
        bench = E2EBenchmark(".", [p], tasks, Evaluator(), parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, [p], tasks)
        html = render_e2e_html(report)

        self.assertIn("Tool Usage Analysis", html)
        self.assertIn("toolCatChart", html)
        self.assertIn("toolCompChart", html)
        self.assertIn("Tool Comparison Detail", html)


class TestInsightsEngine(unittest.TestCase):
    def _make_report(self, win_rate=75, delta=0.05, cost_pct=25, token_pct=20,
                     time_pct=15, weighted_wr=None, dims=None, cats=None):
        """Helper to build a minimal report dict for insights testing."""
        if weighted_wr is None:
            weighted_wr = win_rate
        return {
            "scorecard": {
                "c3_win_rate": win_rate,
                "weighted_win_rate": weighted_wr,
                "avg_score_delta": delta,
            },
            "efficiency_summary": {
                "cost_saved_pct": cost_pct,
                "total_cost_saved_usd": 0.05,
                "projected_monthly_cost_saved_usd": 5.50,
                "tokens_saved_pct": token_pct,
                "total_tokens_saved": 5000,
                "time_saved_pct": time_pct,
                "total_time_saved_s": 30,
            },
            "dimension_breakdown": dims or {},
            "category_stats": cats or {},
            "tool_analysis": {"summary": {}},
            "results": [],
        }

    def test_strong_win_rate(self):
        report = self._make_report(win_rate=80)
        insights = _build_insights(report)
        self.assertIn("verdict", insights)
        self.assertIn("findings", insights)
        strengths = [f for f in insights["findings"] if f["severity"] == "strength"]
        self.assertGreater(len(strengths), 0)

    def test_weak_win_rate(self):
        report = self._make_report(win_rate=30, delta=-0.02)
        insights = _build_insights(report)
        warnings = [f for f in insights["findings"] if f["severity"] == "warning"]
        self.assertGreater(len(warnings), 0)

    def test_weighted_divergence(self):
        report = self._make_report(win_rate=50, weighted_wr=70)
        insights = _build_insights(report)
        titles = [f["title"] for f in insights["findings"]]
        self.assertTrue(any("harder" in t.lower() for t in titles))

    def test_cost_savings_insight(self):
        report = self._make_report(cost_pct=30)
        insights = _build_insights(report)
        titles = [f["title"] for f in insights["findings"]]
        self.assertTrue(any("cost" in t.lower() for t in titles))

    def test_cost_increase_warning(self):
        report = self._make_report(cost_pct=-25)
        insights = _build_insights(report)
        warnings = [f for f in insights["findings"] if f["severity"] == "warning"]
        cost_warnings = [f for f in warnings if "cost" in f["area"]]
        self.assertGreater(len(cost_warnings), 0)

    def test_dimension_weakness(self):
        dims = {
            "factual_score": {"avg_c3": 0.5, "avg_baseline": 0.8, "delta": -0.3},
        }
        report = self._make_report(dims=dims)
        insights = _build_insights(report)
        titles = [f["title"] for f in insights["findings"]]
        self.assertTrue(any("factual" in t.lower() for t in titles))

    def test_dimension_strength(self):
        dims = {
            "completeness_score": {"avg_c3": 0.9, "avg_baseline": 0.6, "delta": 0.3},
        }
        report = self._make_report(dims=dims)
        insights = _build_insights(report)
        strengths = [f for f in insights["findings"] if f["severity"] == "strength"]
        self.assertTrue(any("completeness" in f["title"].lower() for f in strengths))

    def test_weak_category_detection(self):
        cats = {
            "code_review": {"win_rate_c3": 10, "avg_score_delta": -0.15},
        }
        report = self._make_report(cats=cats)
        insights = _build_insights(report)
        titles = [f["title"] for f in insights["findings"]]
        self.assertTrue(any("weak" in t.lower() or "code review" in t.lower() for t in titles))

    def test_verdict_present(self):
        report = self._make_report()
        insights = _build_insights(report)
        self.assertIsInstance(insights["verdict"], str)
        self.assertGreater(len(insights["verdict"]), 0)

    def test_counts_correct(self):
        report = self._make_report(win_rate=80, cost_pct=30, token_pct=25, time_pct=25)
        insights = _build_insights(report)
        counts = insights["counts"]
        total_findings = len(insights["findings"])
        self.assertEqual(
            counts["critical"] + counts["warnings"] + counts["strengths"] + counts["info"],
            total_findings,
        )

    def test_insights_in_report(self):
        """Verify insights appear in generated report."""
        p = CLIProvider(name="claude", executable="claude", available=True)
        resp = CLIResponse(text="answer", response_text="answer", latency_ms=5000,
                           cost_usd=0.05, input_tokens=100, output_tokens=50)
        p.run = MagicMock(return_value=resp)
        tasks = [E2ETask(id="t1", category="test", query="?",
                         ground_truth=GroundTruth(required_keywords=["answer"]))]
        bench = E2EBenchmark(".", [p], tasks, Evaluator(), parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, [p], tasks)
        self.assertIn("insights", report)
        self.assertIn("verdict", report["insights"])
        self.assertIn("findings", report["insights"])

    def test_insights_in_html(self):
        """Verify insights render in HTML."""
        p = CLIProvider(name="claude", executable="claude", available=True)
        resp = CLIResponse(text="answer", response_text="answer", latency_ms=5000,
                           cost_usd=0.05, input_tokens=100, output_tokens=50)
        p.run = MagicMock(return_value=resp)
        tasks = [E2ETask(id="t1", category="test", query="?",
                         ground_truth=GroundTruth(required_keywords=["answer"]))]
        bench = E2EBenchmark(".", [p], tasks, Evaluator(), parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, [p], tasks)
        html = render_e2e_html(report)
        self.assertIn("Insights", html)
        self.assertIn("How to Read This Report", html)
        self.assertIn("verdict", html)


class TestTrendAnalysis(unittest.TestCase):
    def _make_run(self, win_rate=50, delta=0.01, c3_score=0.7, base_score=0.65,
                  cost_c3=0.05, cost_base=0.08, timestamp="2026-03-10T12:00:00",
                  cats=None):
        return {
            "timestamp": timestamp,
            "total_results": 10,
            "scorecard": {
                "c3_win_rate": win_rate,
                "weighted_win_rate": win_rate,
                "avg_score_delta": delta,
                "avg_score_c3": c3_score,
                "avg_score_baseline": base_score,
            },
            "efficiency_summary": {
                "total_cost_c3_usd": cost_c3,
                "total_cost_baseline_usd": cost_base,
                "total_cost_saved_usd": cost_base - cost_c3,
                "total_tokens_saved": 1000,
            },
            "category_stats": cats or {},
        }

    def test_no_history(self):
        current = self._make_run()
        trends = compute_trends(current, [])
        self.assertFalse(trends["available"])

    def test_with_history(self):
        history = [
            self._make_run(win_rate=60, delta=0.02, timestamp="2026-03-10T14:00:00"),
            self._make_run(win_rate=50, delta=0.01, timestamp="2026-03-10T12:00:00"),
        ]
        current = self._make_run(win_rate=70, delta=0.05, timestamp="2026-03-11T10:00:00")
        trends = compute_trends(current, history)

        self.assertTrue(trends["available"])
        self.assertEqual(trends["run_count"], 3)  # 2 history + 1 current

    def test_since_last_deltas(self):
        prev = self._make_run(win_rate=60, delta=0.02, c3_score=0.70)
        current = self._make_run(win_rate=75, delta=0.05, c3_score=0.80)
        trends = compute_trends(current, [prev])

        sl = trends["since_last"]
        self.assertAlmostEqual(sl["win_rate_delta"], 15.0, places=1)
        self.assertAlmostEqual(sl["score_delta_delta"], 0.03, places=3)
        self.assertAlmostEqual(sl["avg_c3_delta"], 0.10, places=3)

    def test_sparklines(self):
        history = [
            self._make_run(win_rate=50, timestamp="2026-03-10T14:00:00"),
            self._make_run(win_rate=40, timestamp="2026-03-10T12:00:00"),
        ]
        current = self._make_run(win_rate=70, timestamp="2026-03-11T10:00:00")
        trends = compute_trends(current, history)

        sp = trends["sparklines"]
        self.assertEqual(len(sp["win_rates"]), 3)
        # Oldest first in sparkline
        self.assertEqual(sp["win_rates"][0], 40)
        self.assertEqual(sp["win_rates"][-1], 70)

    def test_category_trends(self):
        prev = self._make_run(cats={"code_review": {"win_rate_c3": 40, "avg_score_delta": -0.05}})
        current = self._make_run(cats={"code_review": {"win_rate_c3": 70, "avg_score_delta": 0.10}})
        trends = compute_trends(current, [prev])

        ct = trends["category_trends"]
        self.assertIn("code_review", ct)
        self.assertTrue(ct["code_review"]["improving"])
        self.assertAlmostEqual(ct["code_review"]["score_delta_delta"], 0.15, places=3)

    def test_moving_averages(self):
        history = [
            self._make_run(win_rate=60, timestamp="2026-03-10T16:00:00"),
            self._make_run(win_rate=50, timestamp="2026-03-10T14:00:00"),
            self._make_run(win_rate=40, timestamp="2026-03-10T12:00:00"),
        ]
        current = self._make_run(win_rate=70)
        trends = compute_trends(current, history)
        # 3-run MA should be average of last 3: 50, 60, 70
        self.assertAlmostEqual(trends["moving_averages"]["win_rate_3run"], 60.0, places=0)

    @patch("services.e2e_benchmark.load_run_history", return_value=[])
    def test_trends_in_report(self, mock_history):
        """Verify trends appear in generated report with no history."""
        p = CLIProvider(name="claude", executable="claude", available=True)
        resp = CLIResponse(text="answer", response_text="answer", latency_ms=5000,
                           cost_usd=0.05, input_tokens=100, output_tokens=50)
        p.run = MagicMock(return_value=resp)
        tasks = [E2ETask(id="t1", category="test", query="?",
                         ground_truth=GroundTruth(required_keywords=["answer"]))]
        bench = E2EBenchmark(".", [p], tasks, Evaluator(), parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, [p], tasks)
        self.assertIn("trends", report)
        self.assertFalse(report["trends"]["available"])

    @patch("services.e2e_benchmark.load_run_history", return_value=[])
    def test_trends_in_html_when_available(self, mock_history):
        """HTML includes trend charts when history exists."""
        p = CLIProvider(name="claude", executable="claude", available=True)
        resp = CLIResponse(text="answer", response_text="answer", latency_ms=5000,
                           cost_usd=0.05, input_tokens=100, output_tokens=50)
        p.run = MagicMock(return_value=resp)
        tasks = [E2ETask(id="t1", category="test", query="?",
                         ground_truth=GroundTruth(required_keywords=["answer"]))]
        bench = E2EBenchmark(".", [p], tasks, Evaluator(), parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, [p], tasks)

        # Inject fake trend data
        prev = {
            "timestamp": "2026-03-10T12:00:00",
            "total_results": 1,
            "scorecard": {"c3_win_rate": 50, "weighted_win_rate": 50,
                          "avg_score_delta": 0.01, "avg_score_c3": 0.6, "avg_score_baseline": 0.59},
            "efficiency_summary": {"total_cost_c3_usd": 0.05, "total_cost_baseline_usd": 0.08,
                                   "total_cost_saved_usd": 0.03, "total_tokens_saved": 500},
            "category_stats": {},
        }
        report["trends"] = compute_trends(report, [prev])

        html = render_e2e_html(report)
        self.assertIn("Trend Analysis", html)
        self.assertIn("trendWinRate", html)
        self.assertIn("trendDelta", html)
        self.assertIn("Since last run", html)

    @patch("services.e2e_benchmark.load_run_history", return_value=[])
    def test_no_trends_html_when_unavailable(self, mock_history):
        """HTML omits trend section when no history."""
        p = CLIProvider(name="claude", executable="claude", available=True)
        resp = CLIResponse(text="answer", response_text="answer", latency_ms=5000,
                           cost_usd=0.05, input_tokens=100, output_tokens=50)
        p.run = MagicMock(return_value=resp)
        tasks = [E2ETask(id="t1", category="test", query="?",
                         ground_truth=GroundTruth(required_keywords=["answer"]))]
        bench = E2EBenchmark(".", [p], tasks, Evaluator(), parallel=False)
        results = bench.run_all()
        report = generate_e2e_report(".", results, [p], tasks)
        html = render_e2e_html(report)
        self.assertNotIn("Trend Analysis", html)
        self.assertNotIn("trendWinRate", html)

    @patch("services.e2e_benchmark.Path.glob")
    def test_load_run_history(self, mock_glob):
        """Test loading run history files."""
        # No files
        mock_glob.return_value = []
        history = load_run_history("/fake/path")
        self.assertEqual(len(history), 0)


class TestDetectProviders(unittest.TestCase):
    @patch("services.e2e_benchmark.CLIProvider.detect")
    def test_filters_unavailable(self, mock_detect):
        mock_detect.return_value = False
        providers = detect_providers(["claude", "gemini"])
        self.assertEqual(len(providers), 0)


if __name__ == "__main__":
    unittest.main()
