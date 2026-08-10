"""AgentCI — run-history analysis (Phase 9) and the GitHub status bridge (Phase 8).

Both modules exist to make a claim to somebody: "this job is flaky", "this
commit passed". The tests are therefore mostly about the claims they must
REFUSE to make — an unsupported flake verdict or an overstated commit status
is worse than no signal at all.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import ci_github, ci_intel  # noqa: E402
from services import ci_runner as cr  # noqa: E402

LOCAL_RUNNER = {"Windows": "windows-latest", "Darwin": "macos-latest"}.get(
    platform.system(), "ubuntu-latest")


class HistoryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_run(self, run_id: str, jobs: list, verdict: str = "FULL_CI_PASS"):
        """Synthesise a run record — analysis reads history, not live jobs."""
        run_dir = self.tmp / cr.CI_DIR / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        record = {"run_id": run_id, "verdict": verdict,
                  "started_at": f"2026-08-10T10:{run_id[-2:]}:00Z", "jobs": jobs}
        (run_dir / "run.json").write_text(json.dumps(record), encoding="utf-8")
        index = self.tmp / cr.CI_DIR / cr.INDEX_FILE
        index.parent.mkdir(parents=True, exist_ok=True)
        with open(index, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run_id": run_id, "verdict": verdict,
                                 "started_at": record["started_at"]}) + "\n")

    def job(self, key="CI::a", status=cr.PASSED, fingerprint="fp1", ms=1000):
        return {"key": key, "status": status, "fingerprint": fingerprint,
                "duration_ms": ms}


class TestHistory(HistoryBase):
    def test_no_history_says_so_rather_than_reporting_zeroes(self):
        data = ci_intel.analyse(self.tmp)
        self.assertEqual(data["runs_analysed"], 0)
        self.assertIn("No local runs", data["note"])

    def test_counts_pass_and_fail(self):
        self.write_run("r01", [self.job(status=cr.PASSED)])
        self.write_run("r02", [self.job(status=cr.FAILED)])
        job = ci_intel.analyse(self.tmp)["jobs"][0]
        self.assertEqual((job["passed"], job["failed"], job["executed"]),
                         (1, 1, 2))

    def test_same_fingerprint_passing_and_failing_is_a_flake(self):
        # The only local flake signal that is not a guess: identical inputs,
        # two different answers, so the code cannot be the difference.
        self.write_run("r01", [self.job(status=cr.PASSED, fingerprint="same")])
        self.write_run("r02", [self.job(status=cr.FAILED, fingerprint="same")])
        data = ci_intel.analyse(self.tmp)
        self.assertEqual(len(data["flaky"]), 1)
        self.assertIn("flake", data["note"])

    def test_different_fingerprints_are_not_a_flake(self):
        # Passing then failing after an edit is just a regression.
        self.write_run("r01", [self.job(status=cr.PASSED, fingerprint="a")])
        self.write_run("r02", [self.job(status=cr.FAILED, fingerprint="b")])
        self.assertEqual(ci_intel.analyse(self.tmp)["flaky"], [])

    def test_cached_runs_are_not_observations(self):
        # A reused result is not evidence about this run's behaviour.
        self.write_run("r01", [self.job(status=cr.PASSED)])
        self.write_run("r02", [self.job(status=cr.CACHED)])
        job = ci_intel.analyse(self.tmp)["jobs"][0]
        self.assertEqual(job["executed"], 1)
        self.assertEqual(job["cached"], 1)

    def test_jobs_that_never_ran_are_not_counted(self):
        self.write_run("r01", [self.job(status=cr.DESELECTED),
                               self.job(key="CI::b", status=cr.FOREIGN)])
        self.assertEqual(ci_intel.analyse(self.tmp)["jobs"], [])

    def test_small_samples_are_labelled_not_trusted(self):
        self.write_run("r01", [self.job(status=cr.FAILED)])
        data = ci_intel.analyse(self.tmp)
        self.assertFalse(data["jobs"][0]["confident"])
        self.assertIn("noise rather than trend", data["note"])

    def test_failure_is_new_distinguishes_unknown_from_no(self):
        # "We have no history" and "it has never failed" are different, and
        # conflating them would let an agent treat silence as reassurance.
        self.assertFalse(ci_intel.failure_is_new(self.tmp, "CI::a")["known"])
        self.write_run("r01", [self.job(status=cr.FAILED)])
        res = ci_intel.failure_is_new(self.tmp, "CI::a")
        self.assertTrue(res["known"])
        self.assertTrue(res["failed_before"])


class TestStatusBridge(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", "."], cwd=self.tmp,
                       capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_record(self, verdict="FULL_CI_PASS", dirty=False, sha="a" * 40):
        return {"run_id": "r1", "verdict": verdict, "host_os": "Linux",
                "note": "n", "jobs": [{"engine": "act"}],
                "fingerprint": {"sha": sha, "dirty": dirty, "dirty_files": 3}}

    def test_a_dirty_tree_is_refused(self):
        # The status attaches to a commit; if the tree differs, the thing that
        # ran is not the thing being labelled. This is the guard that matters.
        checks = ci_github.preflight(self.tmp, self.run_record(dirty=True))
        self.assertFalse(checks["ok"])
        self.assertIn("uncommitted", checks["reason"])

    def test_partial_is_not_published_as_success(self):
        checks = ci_github.preflight(self.tmp, self.run_record("PARTIAL_PASS"))
        self.assertFalse(checks["ok"])
        self.assertEqual(checks["state"], "pending")
        self.assertIn("overstate", checks["reason"])

    def test_full_pass_maps_to_success_and_says_where_it_ran(self):
        checks = ci_github.preflight(self.tmp, self.run_record())
        self.assertTrue(checks["ok"])
        self.assertEqual(checks["state"], "success")
        self.assertIn("local", checks["description"])

    def test_fail_maps_to_failure(self):
        self.assertEqual(
            ci_github.preflight(self.tmp, self.run_record("FAIL"))["state"],
            "failure")

    def test_missing_gh_is_explained_not_crashed(self):
        with mock.patch.object(ci_github.shutil, "which", return_value=None):
            avail = ci_github.availability(self.tmp)
        self.assertFalse(avail["ok"])
        self.assertIn("gh", avail["reason"])

    def test_an_unpushed_commit_is_refused(self):
        with mock.patch.object(ci_github, "availability",
                               return_value={"ok": True, "gh": "gh",
                                             "repo": "o/r"}), \
             mock.patch.object(ci_github, "_git", return_value=""):
            res = ci_github.publish(self.tmp, self.run_record())
        self.assertFalse(res["published"])
        self.assertIn("not on any remote branch", res["reason"])

    def test_dry_run_shows_the_request_without_making_it(self):
        with mock.patch.object(ci_github, "availability",
                               return_value={"ok": True, "gh": "gh",
                                             "repo": "o/r"}), \
             mock.patch.object(ci_github, "_git", return_value="origin/main"), \
             mock.patch.object(ci_github, "_run") as runner:
            res = ci_github.publish(self.tmp, self.run_record(), dry_run=True)
        runner.assert_not_called()
        self.assertTrue(res["dry_run"])
        self.assertIn("statuses/", res["endpoint"])


if __name__ == "__main__":
    unittest.main()
