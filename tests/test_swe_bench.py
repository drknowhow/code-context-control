"""Tests for the SWE-bench Lite external benchmark adapter."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.bench.external.swe_bench import (
    SWEBenchAdapter,
    SWEBenchReport,
    SWEBenchResult,
    SWEBenchTask,
    apply_resolution_results,
    load_tasks,
    save_report,
)


class TestSWEBenchTaskFromDict(unittest.TestCase):
    def test_basic_fields(self):
        d = {
            "instance_id": "django__django-11099",
            "repo": "django/django",
            "base_commit": "abc123def",
            "problem_statement": "Fix the bug in x",
            "hints_text": "Look at module y",
            "patch": "diff --git ...",
            "test_patch": "diff --git ... test",
            "FAIL_TO_PASS": ["tests/test_x.py::test_bug"],
            "PASS_TO_PASS": ["tests/test_x.py::test_other"],
            "version": "3.0",
        }
        task = SWEBenchTask.from_dict(d)
        self.assertEqual(task.instance_id, "django__django-11099")
        self.assertEqual(task.repo, "django/django")
        self.assertEqual(task.base_commit, "abc123def")
        self.assertEqual(task.fail_to_pass, ["tests/test_x.py::test_bug"])
        self.assertEqual(task.pass_to_pass, ["tests/test_x.py::test_other"])

    def test_handles_json_encoded_test_lists(self):
        """SWE-bench sometimes ships FAIL_TO_PASS as a JSON-encoded string."""
        d = {
            "instance_id": "x",
            "repo": "r/r",
            "base_commit": "c",
            "problem_statement": "p",
            "FAIL_TO_PASS": '["tests/a.py::t1", "tests/a.py::t2"]',
            "PASS_TO_PASS": '[]',
        }
        task = SWEBenchTask.from_dict(d)
        self.assertEqual(task.fail_to_pass, ["tests/a.py::t1", "tests/a.py::t2"])
        self.assertEqual(task.pass_to_pass, [])

    def test_missing_fields_default_safely(self):
        task = SWEBenchTask.from_dict({})
        self.assertEqual(task.instance_id, "")
        self.assertEqual(task.fail_to_pass, [])


class TestLoadTasks(unittest.TestCase):
    def test_loads_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tasks.jsonl"
            rows = [
                {"instance_id": "a", "repo": "x/y", "base_commit": "c1",
                 "problem_statement": "p1"},
                {"instance_id": "b", "repo": "x/y", "base_commit": "c2",
                 "problem_statement": "p2"},
            ]
            p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            tasks = load_tasks(str(p))
            self.assertEqual(len(tasks), 2)
            self.assertEqual(tasks[0].instance_id, "a")
            self.assertEqual(tasks[1].instance_id, "b")

    def test_loads_json_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tasks.json"
            rows = [{"instance_id": "x", "repo": "a/b", "base_commit": "c",
                     "problem_statement": "p"}]
            p.write_text(json.dumps(rows), encoding="utf-8")
            tasks = load_tasks(str(p))
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].instance_id, "x")

    def test_missing_dataset_without_hf(self):
        """When HF id is passed and `datasets` is unavailable, error is explicit."""
        with patch.dict("sys.modules", {"datasets": None}):
            with self.assertRaises(RuntimeError) as ctx:
                load_tasks("princeton-nlp/SWE-bench_Lite")
            self.assertIn("datasets", str(ctx.exception).lower())


class TestSWEBenchReportScorecard(unittest.TestCase):
    def _result(self, mode, *, patch_empty=False, resolved=None, latency=10.0, cost=0.02):
        return SWEBenchResult(
            instance_id=f"id_{mode}", repo="x/y", mode=mode,
            model_patch="diff" if not patch_empty else "",
            patch_empty=patch_empty, latency_s=latency, input_tokens=100, output_tokens=50,
            cost_usd=cost, resolved=resolved,
        )

    def test_patch_rate_without_evaluation(self):
        """When Docker eval is skipped, we can still score patch-gen rate honestly."""
        report = SWEBenchReport(
            timestamp="t", project_path="/p", agent="aider", model="m", dataset="d",
            tasks_run=2,
            results=[
                self._result("with_c3", patch_empty=False),
                self._result("baseline", patch_empty=False),
                self._result("with_c3", patch_empty=True),
                self._result("baseline", patch_empty=True),
            ],
        )
        sc = report.to_dict()["scorecard"]
        self.assertEqual(sc["with_c3_patch_rate"], 50.0)
        self.assertEqual(sc["baseline_patch_rate"], 50.0)
        self.assertFalse(sc["evaluated"])
        self.assertIsNone(sc["with_c3_pass_rate"])
        self.assertIsNone(sc["pass_rate_delta"])

    def test_resolution_delta_with_evaluation(self):
        report = SWEBenchReport(
            timestamp="t", project_path="/p", agent="aider", model="m", dataset="d",
            tasks_run=2,
            results=[
                self._result("with_c3", resolved=True),
                self._result("with_c3", resolved=True),
                self._result("with_c3", resolved=False),
                self._result("baseline", resolved=True),
                self._result("baseline", resolved=False),
                self._result("baseline", resolved=False),
            ],
        )
        sc = report.to_dict()["scorecard"]
        self.assertTrue(sc["evaluated"])
        self.assertAlmostEqual(sc["with_c3_pass_rate"], 66.7, places=1)
        self.assertAlmostEqual(sc["baseline_pass_rate"], 33.3, places=1)
        # 66.7 - 33.3 = 33.4 (rounding)
        self.assertAlmostEqual(sc["pass_rate_delta"], 33.4, places=1)

    def test_negative_delta_is_surfaced(self):
        """Never hide the case where C3 underperforms."""
        report = SWEBenchReport(
            timestamp="t", project_path="/p", agent="aider", model="m", dataset="d",
            tasks_run=2,
            results=[
                self._result("with_c3", resolved=False),
                self._result("with_c3", resolved=False),
                self._result("baseline", resolved=True),
                self._result("baseline", resolved=True),
            ],
        )
        sc = report.to_dict()["scorecard"]
        self.assertLess(sc["pass_rate_delta"], 0)
        self.assertEqual(sc["pass_rate_delta"], -100.0)

    def test_empty_report(self):
        report = SWEBenchReport(
            timestamp="t", project_path="/p", agent="aider", model="m", dataset="d",
        )
        sc = report.to_dict()["scorecard"]
        self.assertEqual(sc["with_c3_count"], 0)
        self.assertEqual(sc["with_c3_patch_rate"], 0.0)
        self.assertFalse(sc["evaluated"])


class TestApplyResolutionResults(unittest.TestCase):
    def test_merges_resolved_ids(self):
        report = SWEBenchReport(
            timestamp="t", project_path="/p", agent="aider", model="m", dataset="d",
            results=[
                SWEBenchResult(instance_id="a", repo="x", mode="with_c3"),
                SWEBenchResult(instance_id="b", repo="x", mode="with_c3"),
                SWEBenchResult(instance_id="a", repo="x", mode="baseline"),
            ],
        )
        apply_resolution_results(
            report, {"resolved_ids": ["a"], "unresolved_ids": ["b"]}, "with_c3",
        )
        # baseline entry should stay unevaluated
        baseline = next(r for r in report.results if r.mode == "baseline")
        self.assertIsNone(baseline.resolved)
        # with_c3 entries should be scored
        c3_a = next(r for r in report.results if r.mode == "with_c3" and r.instance_id == "a")
        c3_b = next(r for r in report.results if r.mode == "with_c3" and r.instance_id == "b")
        self.assertTrue(c3_a.resolved)
        self.assertFalse(c3_b.resolved)


class TestSaveReport(unittest.TestCase):
    def test_writes_timestamped_and_latest(self):
        report = SWEBenchReport(
            timestamp="t", project_path="/p", agent="aider", model="m",
            dataset="d", tasks_run=1,
            results=[SWEBenchResult(
                instance_id="x", repo="a/b", mode="with_c3",
                model_patch="diff --git a b", patch_empty=False, patch_lines=10,
                latency_s=30.0, cost_usd=0.05,
            )],
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = save_report(Path(tmp), report)
            self.assertTrue(out.exists())
            self.assertTrue(out.name.startswith("swe_bench_lite_"))
            parsed = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(parsed["benchmark_type"], "swe_bench_lite")
            self.assertEqual(parsed["tier"], "external")


class TestAdapterIntegration(unittest.TestCase):
    """Smoke tests that don't hit the network."""

    def test_aider_missing_records_error_no_crash(self):
        tasks = [SWEBenchTask(
            instance_id="x__y-1", repo="octocat/Hello-World",
            base_commit="abc", problem_statement="fix it",
        )]
        with tempfile.TemporaryDirectory() as tmp:
            with patch("services.bench.external.aider_polyglot.detect_aider",
                       return_value=None), \
                 patch("services.bench.external.swe_bench._clone_and_checkout",
                       return_value=None), \
                 patch("services.bench.external.swe_bench._diff_workspace",
                       return_value=""):
                adapter = SWEBenchAdapter(
                    project_path=Path(tmp), tasks=tasks,
                    agent="aider", model="gpt-4o-mini", verbose=False,
                )
                report = adapter.run_all(dataset_label="test")
                self.assertEqual(len(report.results), 2)  # 1 task x 2 modes
                for r in report.results:
                    self.assertTrue(r.patch_empty)
                    self.assertIn("aider", r.error.lower())


if __name__ == "__main__":
    unittest.main()
