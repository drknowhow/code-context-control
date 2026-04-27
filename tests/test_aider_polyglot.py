"""Tests for the Aider Polyglot external benchmark adapter."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.bench.external.aider_polyglot import (
    LANGUAGE_TEST_COMMANDS,
    AiderPolyglotBenchmark,
    AiderPolyglotReport,
    AiderPolyglotResult,
    _parse_aider_tokens_cost,
    _to_int,
    detect_aider,
    find_polyglot_repo,
    save_report,
)


class TestAiderPolyglotResult(unittest.TestCase):
    def test_to_dict(self):
        r = AiderPolyglotResult(
            exercise="hamming", language="python", mode="with_c3",
            passed=True, latency_s=12.5, input_tokens=1000, output_tokens=200,
            cost_usd=0.0025, model="gpt-4o-mini",
        )
        d = r.to_dict()
        self.assertEqual(d["exercise"], "hamming")
        self.assertEqual(d["mode"], "with_c3")
        self.assertTrue(d["passed"])
        self.assertEqual(d["input_tokens"], 1000)


class TestAiderPolyglotReport(unittest.TestCase):
    def _make_results(self, c3_passes, base_passes, total=5):
        """Build interleaved with_c3/baseline results."""
        results = []
        for i in range(total):
            results.append(AiderPolyglotResult(
                exercise=f"ex_{i}", language="python", mode="with_c3",
                passed=(i < c3_passes), latency_s=10.0, input_tokens=500, output_tokens=100,
                cost_usd=0.01, model="gpt-4o-mini",
            ))
            results.append(AiderPolyglotResult(
                exercise=f"ex_{i}", language="python", mode="baseline",
                passed=(i < base_passes), latency_s=15.0, input_tokens=800, output_tokens=200,
                cost_usd=0.02, model="gpt-4o-mini",
            ))
        return results

    def test_scorecard_pass_rate_calculation(self):
        report = AiderPolyglotReport(
            timestamp="2026-04-18T10:00:00", project_path="/p",
            model="gpt-4o-mini", languages=["python"], exercises_run=5,
            results=self._make_results(c3_passes=4, base_passes=2),
        )
        sc = report.to_dict()["scorecard"]
        self.assertEqual(sc["with_c3_pass_rate"], 80.0)
        self.assertEqual(sc["baseline_pass_rate"], 40.0)
        self.assertEqual(sc["pass_rate_delta"], 40.0)
        self.assertEqual(sc["with_c3_count"], 5)
        self.assertEqual(sc["baseline_count"], 5)

    def test_scorecard_negative_delta(self):
        """C3 should also honestly report when it underperforms."""
        report = AiderPolyglotReport(
            timestamp="2026-04-18T10:00:00", project_path="/p",
            model="gpt-4o-mini", languages=["python"], exercises_run=5,
            results=self._make_results(c3_passes=1, base_passes=4),
        )
        sc = report.to_dict()["scorecard"]
        self.assertEqual(sc["pass_rate_delta"], -60.0)

    def test_scorecard_empty(self):
        report = AiderPolyglotReport(
            timestamp="t", project_path="/p", model="x", languages=["python"],
        )
        sc = report.to_dict()["scorecard"]
        self.assertEqual(sc["with_c3_pass_rate"], 0.0)
        self.assertEqual(sc["baseline_pass_rate"], 0.0)

    def test_report_to_dict_top_level(self):
        report = AiderPolyglotReport(
            timestamp="2026-04-18T10:00:00", project_path="/p",
            model="gpt-4o-mini", languages=["python"], exercises_run=3,
        )
        d = report.to_dict()
        self.assertEqual(d["benchmark_type"], "aider_polyglot")
        self.assertEqual(d["tier"], "external")
        self.assertEqual(d["suite"], "aider-polyglot")
        self.assertIn("scorecard", d)
        self.assertIn("results", d)


class TestParseAiderTokensCost(unittest.TestCase):
    def test_parse_standard_format(self):
        output = """
Some aider output...
Tokens: 2.3k sent, 450 received.
Cost: $0.0123 message, $0.0456 session.
"""
        inp, out, cost = _parse_aider_tokens_cost(output)
        self.assertEqual(inp, 2300)
        self.assertEqual(out, 450)
        self.assertAlmostEqual(cost, 0.0123)

    def test_parse_million_suffix(self):
        output = "Tokens: 1.2M sent, 500k received.\nCost: $1.23 message"
        inp, out, cost = _parse_aider_tokens_cost(output)
        self.assertEqual(inp, 1_200_000)
        self.assertEqual(out, 500_000)
        self.assertAlmostEqual(cost, 1.23)

    def test_parse_missing_returns_zeros(self):
        inp, out, cost = _parse_aider_tokens_cost("no tokens here")
        self.assertEqual(inp, 0)
        self.assertEqual(out, 0)
        self.assertEqual(cost, 0.0)

    def test_to_int_helper(self):
        self.assertEqual(_to_int("5", ""), 5)
        self.assertEqual(_to_int("1.5", "k"), 1500)
        self.assertEqual(_to_int("2", "M"), 2_000_000)
        self.assertEqual(_to_int("abc", "k"), 0)


class TestFindPolyglotRepo(unittest.TestCase):
    def test_finds_via_explicit_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "polyglot-benchmark"
            (repo / "python" / "exercises" / "practice").mkdir(parents=True)
            found = find_polyglot_repo(str(repo))
            self.assertIsNotNone(found)
            self.assertEqual(found, repo.resolve())

    def test_rejects_wrong_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "wrong"
            repo.mkdir()
            (repo / "random_dir").mkdir()
            self.assertIsNone(find_polyglot_repo(str(repo)))

    def test_finds_via_env_var(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "polyglot"
            (repo / "python" / "exercises" / "practice").mkdir(parents=True)
            with patch.dict(os.environ, {"POLYGLOT_BENCHMARK_PATH": str(repo)}):
                found = find_polyglot_repo()
                self.assertIsNotNone(found)


class TestSaveReport(unittest.TestCase):
    def test_writes_timestamped_json_and_latest(self):
        report = AiderPolyglotReport(
            timestamp="2026-04-18T10:00:00", project_path="/p",
            model="gpt-4o-mini", languages=["python"], exercises_run=1,
            results=[AiderPolyglotResult(
                exercise="hello", language="python", mode="with_c3",
                passed=True, latency_s=5.0, model="gpt-4o-mini",
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = save_report(Path(tmp), report)
            self.assertTrue(out.exists())
            self.assertTrue(out.name.startswith("aider_polyglot_"))
            latest = Path(tmp) / ".c3" / "external_benchmark" / "latest.json"
            self.assertTrue(latest.exists())
            parsed = json.loads(latest.read_text(encoding="utf-8"))
            self.assertEqual(parsed["benchmark_type"], "aider_polyglot")


class TestLanguageTestCommands(unittest.TestCase):
    def test_covers_all_polyglot_languages(self):
        for lang in ["python", "javascript", "go", "rust", "java", "cpp"]:
            self.assertIn(lang, LANGUAGE_TEST_COMMANDS)
            self.assertIsInstance(LANGUAGE_TEST_COMMANDS[lang], list)
            self.assertGreater(len(LANGUAGE_TEST_COMMANDS[lang]), 0)


class TestBenchmarkIntegration(unittest.TestCase):
    """Smoke tests that don't invoke real aider."""

    def test_run_all_with_missing_aider(self):
        """If aider is missing, every exercise should record the error
        cleanly — no crashes."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "polyglot"
            ex = repo / "python" / "exercises" / "practice" / "hello"
            ex.mkdir(parents=True)
            (ex / ".meta").mkdir()
            (ex / ".meta" / "config.json").write_text(
                json.dumps({"files": {"solution": ["hello.py"]}}),
                encoding="utf-8",
            )
            (ex / "hello.py").write_text("def hello(): pass\n", encoding="utf-8")
            (ex / "test_hello.py").write_text("def test(): assert False\n", encoding="utf-8")

            with patch("services.bench.external.aider_polyglot.detect_aider", return_value=None):
                bench = AiderPolyglotBenchmark(
                    repo_path=repo, project_path=Path(tmp),
                    languages=["python"], max_exercises=1, model="gpt-4o-mini",
                    verbose=False,
                )
                report = bench.run_all()
                # 1 exercise x 2 modes = 2 results, both failed with error
                self.assertEqual(len(report.results), 2)
                for r in report.results:
                    self.assertFalse(r.passed)
                    self.assertIn("aider", r.error.lower())


if __name__ == "__main__":
    unittest.main()
