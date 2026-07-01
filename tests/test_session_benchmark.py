"""Tests for the real-world session benchmark system."""

import json
import os
import sys
import unittest
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.session_benchmark import (
    ScenarioResult,
    SessionBenchmark,
    StepResult,
    generate_report,
    render_html,
)


class TestStepResult(unittest.TestCase):
    def test_basic_creation(self):
        step = StepResult("search", "c3_search", tokens=500, latency_ms=12.5, quality=95.0)
        self.assertEqual(step.name, "search")
        self.assertEqual(step.tokens, 500)
        self.assertEqual(step.quality, 95.0)


class TestScenarioResult(unittest.TestCase):
    def _make_scenario(self):
        s = ScenarioResult(name="test", description="test scenario")
        s.steps_c3 = [
            StepResult("a", "c3_search", tokens=100, latency_ms=5.0, quality=90.0),
            StepResult("b", "c3_compress", tokens=200, latency_ms=10.0, quality=100.0),
        ]
        s.steps_baseline = [
            StepResult("a", "native", tokens=500, latency_ms=1.0, quality=100.0),
            StepResult("b", "native", tokens=800, latency_ms=2.0, quality=80.0),
        ]
        return s

    def test_total_tokens(self):
        s = self._make_scenario()
        self.assertEqual(s.total_tokens_c3, 300)
        self.assertEqual(s.total_tokens_baseline, 1300)

    def test_savings(self):
        s = self._make_scenario()
        self.assertAlmostEqual(s.token_savings_pct, 76.9, places=1)

    def test_budget_multiplier(self):
        s = self._make_scenario()
        self.assertAlmostEqual(s.budget_multiplier, 4.33, places=2)

    def test_quality(self):
        s = self._make_scenario()
        self.assertAlmostEqual(s.avg_quality_c3, 95.0, places=1)
        self.assertAlmostEqual(s.avg_quality_baseline, 90.0, places=1)

    def test_to_dict(self):
        s = self._make_scenario()
        d = s.to_dict()
        self.assertEqual(d["name"], "test")
        self.assertEqual(d["total_tokens_c3"], 300)
        self.assertEqual(len(d["steps_c3"]), 2)

    def test_empty_scenario(self):
        s = ScenarioResult(name="empty", description="no steps")
        self.assertEqual(s.total_tokens_c3, 0)
        self.assertEqual(s.token_savings_pct, 0.0)
        self.assertEqual(s.budget_multiplier, 0.0)


class TestGenerateReport(unittest.TestCase):
    def test_basic_report_structure(self):
        s = ScenarioResult(name="test", description="desc")
        s.steps_c3 = [StepResult("a", "c3_search", tokens=100, latency_ms=5.0)]
        s.steps_baseline = [StepResult("a", "native", tokens=1000, latency_ms=1.0)]

        report = generate_report("/test/path", [s], 10, 50)

        self.assertIn("timestamp", report)
        self.assertIn("scorecard", report)
        self.assertIn("session_longevity", report)
        self.assertIn("timeline", report)
        self.assertIn("scenarios", report)
        self.assertIn("tool_contributions", report)

        self.assertEqual(report["files_considered"], 50)
        self.assertEqual(report["scorecard"]["total_tokens_c3"], 100)
        self.assertEqual(report["scorecard"]["total_tokens_baseline"], 1000)
        self.assertGreater(report["scorecard"]["token_savings_pct"], 80)

    def test_session_longevity(self):
        s = ScenarioResult(name="test", description="desc")
        s.steps_c3 = [StepResult("a", "c3_search", tokens=1000)]
        s.steps_baseline = [StepResult("a", "native", tokens=10000)]

        report = generate_report("/test", [s], 10, 50)
        lon = report["session_longevity"]

        self.assertEqual(lon["context_limit"], 200_000)
        self.assertGreater(lon["estimated_turns_c3"], lon["estimated_turns_baseline"])
        self.assertGreater(lon["turn_multiplier"], 1.0)

    def test_timeline_generation(self):
        s = ScenarioResult(name="test", description="desc")
        s.steps_c3 = [StepResult("a", "tool", tokens=5000)]
        s.steps_baseline = [StepResult("a", "native", tokens=50000)]

        report = generate_report("/test", [s], 10, 50)
        timeline = report["timeline"]

        self.assertGreater(len(timeline), 1)
        self.assertEqual(timeline[0]["turn"], 1)
        # Cumulative should increase
        self.assertLess(timeline[0]["cumulative_c3"], timeline[1]["cumulative_c3"])


class TestRenderHtml(unittest.TestCase):
    def test_html_output(self):
        s = ScenarioResult(name="test_scenario", description="A test")
        s.steps_c3 = [StepResult("search", "c3_search", tokens=500, latency_ms=10.0, quality=100.0)]
        s.steps_baseline = [StepResult("grep", "native", tokens=5000, latency_ms=1.0, quality=100.0)]

        report = generate_report("/test", [s], 10, 50)
        html_output = render_html(report)

        self.assertIn("<!doctype html>", html_output)
        self.assertIn("C3 Session Benchmark", html_output)
        self.assertIn("chart.js", html_output.lower())
        self.assertIn("Test Scenario", html_output)
        self.assertIn("timelineChart", html_output)
        self.assertIn("scenarioChart", html_output)
        self.assertIn("heatmapChart", html_output)


class TestSessionBenchmarkIntegration(unittest.TestCase):
    """Integration tests that run against the actual project.

    bench.run_all() is expensive (~1s). It is executed once in setUpClass and
    shared across all tests in this class — results/report are class-level attrs.
    """

    @classmethod
    def setUpClass(cls):
        cls.project_path = str(Path(__file__).resolve().parent.parent)
        # Only run if we have a .c3 directory (project is initialized)
        if not (Path(cls.project_path) / ".c3").exists():
            raise unittest.SkipTest("Project not initialized with C3")

        # Run benchmark once; all tests share these results
        cls.bench = SessionBenchmark(cls.project_path, sample_size=5, min_tokens=100)
        cls.results = cls.bench.run_all()
        cls.report = generate_report(
            cls.project_path, cls.results, 5, len(cls.bench.files)
        )

    def test_benchmark_runs(self):
        """Full integration test: run all scenarios on this project."""
        self.assertGreater(len(self.bench.files), 0, "Should find eligible files")
        self.assertGreater(len(self.bench.sample), 0, "Should have a sample")
        self.assertEqual(len(self.results), 6, "Should produce 6 scenario results")

        for r in self.results:
            self.assertIsInstance(r, ScenarioResult)
            self.assertTrue(r.name, "Scenario should have a name")
            # Scenarios should have steps (unless error)
            if "Error" not in r.description:
                self.assertGreater(len(r.steps_c3), 0, f"{r.name}: should have C3 steps")
                self.assertGreater(len(r.steps_baseline), 0, f"{r.name}: should have baseline steps")

    def test_full_report_generation(self):
        """Full pipeline: run scenarios → generate report → render HTML."""
        self.assertIn("scorecard", self.report)
        self.assertGreater(self.report["scorecard"]["token_savings_pct"], 0, "Should show some savings")
        self.assertGreater(self.report["scorecard"]["budget_multiplier"], 1.0, "Multiplier should be > 1")

        # Validate HTML renders without error
        html_output = render_html(self.report)
        self.assertIn("<!doctype html>", html_output)
        self.assertGreater(len(html_output), 1000, "HTML should be substantial")

    def test_scenario_token_savings(self):
        """Each scenario should show positive token savings."""
        for r in self.results:
            if "Error" not in r.description and r.total_tokens_baseline > 0:
                self.assertGreaterEqual(
                    r.token_savings_pct, 0,
                    f"{r.name}: expected non-negative savings, got {r.token_savings_pct}%"
                )

    def test_baseline_reads_each_file_at_most_once(self):
        """The honest baseline must not model repeated reads of the same file.

        Baseline read steps record the files they ingest in
        StepResult.detail as "reads:a.py;b.py". Within a scenario, no file
        may be read more than once by the baseline (the old strawman
        re-read the same file 3-4 times).
        """
        checked_any = False
        for r in self.results:
            if "Error" in r.description:
                continue
            read_counts: dict = {}
            for step in r.steps_baseline:
                detail = getattr(step, "detail", "") or ""
                if not detail.startswith("reads:"):
                    continue
                for rel in detail[len("reads:"):].split(";"):
                    rel = rel.strip()
                    if rel:
                        read_counts[rel] = read_counts.get(rel, 0) + 1
                        checked_any = True
            for rel, count in read_counts.items():
                self.assertLessEqual(
                    count, 1,
                    f"{r.name}: baseline read {rel!r} {count} times — "
                    "the honest baseline must read each file at most once"
                )
        self.assertTrue(checked_any, "Baseline read steps should record reads: details")

    def test_session_longevity_projection(self):
        """Session longevity projections should be reasonable."""
        lon = self.report["session_longevity"]
        self.assertGreater(lon["estimated_turns_c3"], 0)
        self.assertGreater(lon["estimated_turns_baseline"], 0)
        self.assertGreaterEqual(lon["turn_multiplier"], 1.0, "C3 should not reduce session turns")


if __name__ == "__main__":
    unittest.main()
