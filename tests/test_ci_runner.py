"""AgentCI — local execution, verdicts, failure parsing, and the tool surface.

The verdict rules are the product, so most of this file is about them:

  FULL_CI_PASS   every job ran ON THIS HOST and passed. The only verdict that
                 means "safe to push".
  PARTIAL_PASS   nothing failed, but something did not run — another OS, an
                 unsupported action, or a selection. NOT a green light.
  FAIL           something failed, or a dependency did.

A test that let PARTIAL read as FULL would be waving through the exact bug
this module exists to prevent, so the boundary is asserted from both sides.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import ci_failures  # noqa: E402
from services import ci_runner as cr
from services.ci_workflow import host_os  # noqa: E402

# The label matching THIS host, so every runnable-job test behaves the same on
# all three CI operating systems.
LOCAL_RUNNER = {"Linux": "ubuntu-latest", "Darwin": "macos-latest",
                "Windows": "windows-latest"}[host_os()]
OTHER_RUNNER = "windows-latest" if host_os() != "Windows" else "ubuntu-latest"


def workflow(root: Path, body: str, name: str = "ci.yml") -> None:
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(body, encoding="utf-8")


class RunnerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def by_id(self, result) -> dict:
        return {j.job_id: j for j in result.jobs}


class TestExecution(RunnerBase):
    def test_all_pass_is_a_full_pass(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - uses: actions/checkout@v4
      - run: echo one
  b:
    needs: [a]
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo two
""")
        res = cr.run_ci(self.tmp)
        self.assertEqual(res.verdict, cr.FULL_PASS)
        self.assertEqual({j.status for j in res.jobs}, {cr.PASSED})

    def test_failure_fails_the_run_and_skips_dependents(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  lint:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo fine
  test:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: exit 1
  build:
    needs: [lint, test]
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo built
""")
        res = cr.run_ci(self.tmp)
        jobs = self.by_id(res)
        self.assertEqual(res.verdict, cr.FAIL)
        self.assertEqual(jobs["lint"].status, cr.PASSED)
        self.assertEqual(jobs["test"].status, cr.FAILED)
        # The whole point of `needs`: downstream did NOT run, so it is skipped,
        # never passed.
        self.assertEqual(jobs["build"].status, cr.SKIPPED)
        self.assertIn("test", jobs["build"].reason)

    def test_later_steps_do_not_run_after_a_failed_step(self):
        marker = self.tmp / "should-not-exist.txt"
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: exit 3
      - run: echo x > "{marker.as_posix()}"
""")
        res = cr.run_ci(self.tmp)
        self.assertEqual(res.verdict, cr.FAIL)
        self.assertFalse(marker.exists())

    def test_shim_steps_do_not_execute_anything(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - run: echo done
""")
        res = cr.run_ci(self.tmp)
        steps = res.jobs[0].steps
        self.assertEqual([s.status for s in steps[:2]], ["shim", "shim"])
        self.assertEqual(res.verdict, cr.FULL_PASS)


class TestCoverageHonesty(RunnerBase):
    # These two pin the NATIVE engine deliberately. Since the act engine
    # landed, a Linux job on a non-Linux host is containerised rather than
    # refused — correct, and covered in test_ci_act.py. What is asserted here
    # is the native engine's own refusal semantics, which must not quietly
    # depend on whether act happens to be installed on the machine running
    # the suite.
    def test_foreign_runner_is_refused_by_the_native_engine(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  elsewhere:
    runs-on: {OTHER_RUNNER}
    steps:
      - run: echo hi
""")
        res = cr.run_ci(self.tmp, engine="native")
        self.assertEqual(res.jobs[0].status, cr.FOREIGN)
        # Nothing failed, but nothing ran either — that is NOT a pass.
        self.assertEqual(res.verdict, cr.PARTIAL_PASS)
        self.assertIn("did not run here", res.note)

    def test_allow_foreign_runs_it_but_never_yields_full_pass(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  elsewhere:
    runs-on: {OTHER_RUNNER}
    steps:
      - run: echo hi
""")
        res = cr.run_ci(self.tmp, engine="native", allow_foreign=True)
        job = res.jobs[0]
        self.assertEqual(job.status, cr.PASSED)
        self.assertTrue(job.cross_os)
        self.assertEqual(job.fidelity, cr.FIDELITY_CROSS_OS)
        self.assertEqual(res.verdict, cr.PARTIAL_PASS)
        self.assertIn("indicative", res.note)

    def test_unsupported_job_blocks_and_is_not_a_pass(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - uses: exotic/thing@v1
""")
        res = cr.run_ci(self.tmp)
        self.assertEqual(res.jobs[0].status, cr.UNSUPPORTED)
        self.assertEqual(res.verdict, cr.PARTIAL_PASS)

    def test_unsupported_dependency_skips_its_dependents(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - uses: exotic/thing@v1
  b:
    needs: [a]
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo hi
""")
        res = cr.run_ci(self.tmp)
        self.assertEqual(self.by_id(res)["b"].status, cr.SKIPPED)

    def test_selecting_one_job_can_never_be_a_full_pass(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo a
  b:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo b
""")
        res = cr.run_ci(self.tmp, selector="a")
        self.assertEqual(self.by_id(res)["a"].status, cr.PASSED)
        self.assertEqual(self.by_id(res)["b"].status, cr.DESELECTED)
        self.assertEqual(res.verdict, cr.PARTIAL_PASS)

    def test_no_jobs_selected_is_a_failure_not_a_vacuous_pass(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo a
""")
        res = cr.run_ci(self.tmp, selector="does-not-exist")
        self.assertEqual(res.verdict, cr.FAIL)
        self.assertIn("no job matches", res.note)

    def test_empty_project_is_a_failure(self):
        res = cr.run_ci(self.tmp)
        self.assertEqual(res.verdict, cr.FAIL)
        self.assertIn("no runnable workflow jobs", res.note)

    def test_cycle_refuses_the_run(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    needs: [b]
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo a
  b:
    needs: [a]
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo b
""")
        res = cr.run_ci(self.tmp)
        self.assertEqual(res.verdict, cr.FAIL)
        self.assertIn("cycle", res.note.lower())


class TestMvpLoop(RunnerBase):
    """AgentCI spec §34 — the loop the whole product is judged on."""

    def _write(self, failing: bool):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  lint:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo lint-ok
  test:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: {"exit 1" if failing else "echo tests-pass"}
  build:
    needs: [lint, test]
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo built
""")

    def test_fail_fix_rerun_full(self):
        self._write(failing=True)
        first = cr.run_ci(self.tmp)
        self.assertEqual(first.verdict, cr.FAIL)
        self.assertEqual(first.failed_keys, ["CI::test"])

        self._write(failing=False)                      # the agent fixes it
        rerun = cr.run_ci(self.tmp, only=first.failed_keys)
        self.assertEqual(rerun.verdict, cr.PARTIAL_PASS)  # a rerun is never full
        self.assertEqual(self.by_id(rerun)["test"].status, cr.PASSED)

        full = cr.run_ci(self.tmp)
        self.assertEqual(full.verdict, cr.FULL_PASS)


class TestPersistence(RunnerBase):
    def setUp(self):
        super().setUp()
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo recorded
""")

    def test_run_is_indexed_and_reloadable(self):
        res = cr.run_ci(self.tmp)
        rows = cr.list_runs(self.tmp)
        self.assertEqual(rows[0]["run_id"], res.run_id)
        self.assertEqual(cr.load_run(self.tmp, res.run_id)["verdict"], cr.FULL_PASS)

    def test_latest_run_loads_without_an_id(self):
        cr.run_ci(self.tmp)
        second = cr.run_ci(self.tmp)
        self.assertEqual(cr.load_run(self.tmp)["run_id"], second.run_id)

    def test_log_is_written_and_tailable(self):
        res = cr.run_ci(self.tmp)
        log = cr.read_log(self.tmp, res.run_id, res.jobs[0].key)
        self.assertIn("recorded", log)

    def test_fingerprint_records_a_dirty_tree_rather_than_refusing(self):
        # An agent runs CI mid-edit; dirty is the normal case, not an error.
        res = cr.run_ci(self.tmp)
        self.assertIn("dirty", res.fingerprint)

    def test_missing_run_returns_empty_not_an_exception(self):
        self.assertEqual(cr.load_run(self.tmp, "nope"), {})
        self.assertEqual(cr.read_log(self.tmp, "nope", "x"), "")


class TestFailureParsers(unittest.TestCase):
    def test_passing_job_never_reports_failures(self):
        # "FAILED" can appear in a green log (a fixture name, a doctest).
        noisy = "test_handles_FAILED_case PASSED\n1 passed"
        self.assertEqual(ci_failures.parse(noisy, exit_code=0).failures, [])

    def test_pytest_summary(self):
        log = ("FAILED tests/test_auth.py::test_login - AssertionError: 401 != 200\n"
               "1 failed, 3 passed")
        res = ci_failures.parse(log)
        self.assertEqual(res.parser, "pytest")
        self.assertEqual(res.failures[0].test, "tests/test_auth.py::test_login")
        self.assertIn("401", res.failures[0].message)

    def test_ruff(self):
        res = ci_failures.parse("cli/x.py:12:5: F401 `os` imported but unused")
        self.assertEqual(res.parser, "ruff")
        self.assertEqual(res.failures[0].file, "cli/x.py")
        self.assertEqual(res.failures[0].line, 12)
        self.assertEqual(res.failures[0].rule, "F401")

    def test_tsc(self):
        res = ci_failures.parse("src/a.ts(9,3): error TS2322: Type 'x' is bad.")
        self.assertEqual(res.parser, "tsc")
        self.assertEqual(res.failures[0].line, 9)

    def test_traceback(self):
        log = ('Traceback (most recent call last):\n'
               '  File "run.py", line 4, in <module>\n'
               '    boom()\n'
               'ValueError: nope\n')
        res = ci_failures.parse(log)
        self.assertEqual(res.parser, "traceback")
        self.assertIn("ValueError", res.failures[0].message)

    def test_unrecognised_failure_is_never_zero_failures(self):
        # The dangerous case: job failed, nothing matched. "0 failures" would
        # read as "it passed".
        res = ci_failures.parse("some proprietary tool exploded", exit_code=2)
        self.assertEqual(res.parser, "unparsed")
        self.assertEqual(len(res.failures), 1)
        self.assertIn("exit 2", res.failures[0].message)

    def test_runner_attaches_structured_failures(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            # Block scalar: the command contains ": ", which a plain YAML
            # scalar treats as a mapping and refuses to parse.
            workflow(tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: |
          echo "cli/x.py:3:1: F401 unused import"
          exit 1
""")
            res = cr.run_ci(tmp)
            job = res.jobs[0]
            self.assertEqual(job.status, cr.FAILED)
            self.assertEqual(job.parser, "ruff")
            self.assertEqual(job.failures[0]["rule"], "F401")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestToolSurface(RunnerBase):
    """c3_ci — what the agent actually reads."""

    def _call(self, action, **kw):
        from cli.tools.ci import handle_ci
        svc = SimpleNamespace(project_path=str(self.tmp))
        captured = {}

        def finalize(name, args, resp, summ, **_):
            captured["summary"] = summ
            return resp

        out = handle_ci(action, kw.get("job", ""), kw.get("run_id", ""),
                        kw.get("allow_foreign", False), kw.get("workflow", ""),
                        kw.get("tail", 200), kw.get("timeout", 0), svc, finalize)
        return out, captured.get("summary", "")

    def test_inspect_reports_no_workflows_clearly(self):
        out, _ = self._call("inspect")
        self.assertIn("No GitHub workflows found", out)

    def test_inspect_lists_the_graph(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  lint:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo l
  build:
    needs: [lint]
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo b
""")
        out, _ = self._call("inspect")
        self.assertIn("CI::lint", out)
        self.assertIn("needs=lint", out)
        self.assertIn("Runnable here: 2 of 2", out)

    def test_run_then_failures_then_rerun(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: |
          echo "x.py:1:1: E999 broken"
          exit 1
""")
        out, summary = self._call("run")
        self.assertIn("FAIL", out)
        self.assertIn("E999", out)
        self.assertIn("FAIL", summary)

        fails, _ = self._call("failures")
        self.assertIn("E999", fails)

        status, _ = self._call("status")
        self.assertIn("FAIL", status)

        logs, _ = self._call("logs", job="CI::a")
        self.assertIn("E999", logs)

        runs, _ = self._call("runs")
        self.assertIn("FAIL", runs)

    def test_rerun_without_a_prior_run_is_explicit(self):
        out, _ = self._call("rerun")
        self.assertIn("No previous run", out)

    def test_rerun_when_nothing_failed_says_so(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo ok
""")
        self._call("run")
        out, _ = self._call("rerun")
        self.assertIn("no failed jobs", out)

    def test_full_pass_is_marked_ok_and_partial_is_not(self):
        # Two jobs, so selecting one genuinely leaves coverage behind. With a
        # single-job repo, `--job a` IS full coverage and correctly reports
        # FULL_CI_PASS — partial is about what did not run, not about whether
        # a selector was passed.
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo ok
  b:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo ok
""")
        full, _ = self._call("run")
        self.assertIn("[OK] FULL_CI_PASS", full)

        partial, _ = self._call("run", job="a")   # leaves b unrun
        self.assertIn("[!!] PARTIAL_PASS", partial)
        self.assertNotIn("[OK]", partial)

    def test_unknown_action_is_refused(self):
        out, _ = self._call("teleport")
        self.assertIn("Unknown action", out)


if __name__ == "__main__":
    unittest.main()
