"""AgentCI — `if:` condition evaluation.

Before this, `if:` was parsed and ignored, so every guarded step ran locally.
The tests below pin the three properties that make evaluating it an
improvement rather than a new way to be wrong:

  - an expression we cannot parse BLOCKS, rather than defaulting either way;
  - a reference with no honest local value BLOCKS, rather than being invented
    (`github.event_name` is the one that matters);
  - a job skipped by its own `if:` is faithful reproduction, so it does NOT
    count as lost coverage — otherwise no conditional workflow could ever
    reach FULL_CI_PASS.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import ci_expr  # noqa: E402
from services import ci_runner as cr  # noqa: E402
from services.ci_workflow import host_os, inspect_project  # noqa: E402

LOCAL_RUNNER = {"Linux": "ubuntu-latest", "Darwin": "macos-latest",
                "Windows": "windows-latest"}[host_os()]


def ctx(**values) -> ci_expr.EvalContext:
    base = {"github": {"event_name": "push", "ref": "refs/heads/main"},
            "env": {"STAGE": "ci"}, "matrix": {"os": "ubuntu-latest"},
            "runner": {"os": "Linux"}, "job": {"status": "success"},
            "needs": {}, "steps": {}, "strategy": {}}
    base.update(values.pop("values", {}))
    return ci_expr.EvalContext(values=base, **values)


class TestNormalize(unittest.TestCase):
    def test_bare_condition_gets_implicit_success(self):
        # This is why `if: always()` is the documented way to run after a
        # failure — everything else is implicitly gated on success().
        self.assertEqual(ci_expr.normalize("a == 'b'"), "success() && (a == 'b')")

    def test_status_function_suppresses_the_implicit_gate(self):
        self.assertEqual(ci_expr.normalize("always()"), "always()")
        self.assertEqual(ci_expr.normalize("failure()"), "failure()")

    def test_optional_expression_braces_are_stripped(self):
        self.assertEqual(ci_expr.normalize("${{ always() }}"), "always()")

    def test_empty_is_empty(self):
        self.assertEqual(ci_expr.normalize("  "), "")


class TestEvaluate(unittest.TestCase):
    def ev(self, src, **kw):
        return ci_expr.evaluate(src, ctx(**kw))

    def test_status_functions(self):
        self.assertTrue(self.ev("success()"))
        self.assertFalse(self.ev("failure()"))
        self.assertTrue(self.ev("always()"))
        self.assertFalse(self.ev("cancelled()"))

    def test_status_functions_after_a_failure(self):
        self.assertFalse(self.ev("success()", failed=True))
        self.assertTrue(self.ev("failure()", failed=True))
        self.assertTrue(self.ev("always()", failed=True))

    def test_context_comparison(self):
        self.assertTrue(self.ev("github.event_name == 'push'"))
        self.assertFalse(self.ev("github.event_name == 'pull_request'"))

    def test_comparison_is_case_insensitive_like_github(self):
        self.assertTrue(self.ev("github.event_name == 'PUSH'"))

    def test_boolean_operators_and_precedence(self):
        self.assertTrue(self.ev("github.event_name == 'push' && env.STAGE == 'ci'"))
        self.assertFalse(self.ev("github.event_name == 'x' && env.STAGE == 'ci'"))
        self.assertTrue(self.ev("github.event_name == 'x' || env.STAGE == 'ci'"))
        self.assertTrue(self.ev("!(github.event_name == 'x')"))

    def test_short_circuit_protects_an_unknown_branch(self):
        # `false && unknown.thing` must not raise: GitHub short-circuits, and
        # so an honest condition stays evaluable.
        self.assertFalse(self.ev("github.event_name == 'nope' && needs.x.result == 'success'"))

    def test_functions(self):
        self.assertTrue(self.ev("startsWith(github.ref, 'refs/heads/')"))
        self.assertTrue(self.ev("endsWith(github.ref, 'main')"))
        self.assertTrue(self.ev("contains(github.ref, 'HEADS')"))
        self.assertEqual(
            ci_expr.evaluate("format('{0}-{1}', 'a', 'b') == 'a-b'", ctx()), True)

    def test_numeric_comparison(self):
        self.assertTrue(ci_expr.evaluate("always() && 3 > 2", ctx()))
        self.assertFalse(ci_expr.evaluate("always() && 1 >= 2", ctx()))

    def test_needs_results(self):
        c = ctx(values={"needs": {"build": {"result": "failure"}}})
        self.assertTrue(ci_expr.evaluate("always() && needs.build.result == 'failure'", c))

    def test_empty_condition_is_true(self):
        self.assertTrue(self.ev(""))


class TestRefusals(unittest.TestCase):
    def test_unparseable_raises(self):
        with self.assertRaises(ci_expr.ExprError):
            ci_expr.evaluate("always() && ((", ctx())

    def test_unknown_root_raises(self):
        with self.assertRaises(ci_expr.UnknownRef):
            ci_expr.evaluate("always() && secrets.TOKEN == 'x'", ctx())

    def test_unknown_field_raises(self):
        with self.assertRaises(ci_expr.UnknownRef):
            ci_expr.evaluate("always() && github.actor == 'me'", ctx())

    def test_validate_reports_a_parse_error(self):
        self.assertIn("cannot parse", ci_expr.validate("a &&"))

    def test_validate_reports_an_unavailable_context(self):
        msg = ci_expr.validate("secrets.TOKEN != ''", {"ref"})
        self.assertIn("secrets", msg)

    def test_validate_reports_event_name_with_a_hint(self):
        msg = ci_expr.validate("github.event_name == 'push'", {"ref", "sha"})
        self.assertIn("event_name", msg)
        self.assertIn("--event", msg)

    def test_validate_accepts_event_name_once_declared(self):
        self.assertEqual(
            ci_expr.validate("github.event_name == 'push'",
                             {"ref", "sha", "event_name"}), "")

    def test_validate_allows_runtime_only_references(self):
        # needs/steps resolve later; they must not block at inspect time.
        self.assertEqual(ci_expr.validate("needs.build.result == 'success'", set()), "")


class IfIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, body: str):
        wf = self.tmp / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "ci.yml").write_text(body, encoding="utf-8")

    def statuses(self, **kw) -> dict:
        res = cr.run_ci(self.tmp, **kw)
        return {j.job_id: j.status for j in res.jobs}, res


class TestJobIf(IfIntegration):
    def test_event_condition_needs_a_declared_event(self):
        self.write(f"""
name: CI
on: [push]
jobs:
  a:
    if: github.event_name == 'pull_request'
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: echo hi}}]
""")
        # Undeclared: unjudgeable, so it blocks rather than guessing.
        data = inspect_project(self.tmp)
        self.assertIn("event_name", data["unsupported"][0]["blockers"][0])
        # Declared: now it is a real decision.
        got, _ = self.statuses(event="push")
        self.assertEqual(got["a"], cr.SKIPPED_IF)
        got, _ = self.statuses(event="pull_request")
        self.assertEqual(got["a"], cr.PASSED)

    def test_always_overrides_a_failed_dependency(self):
        self.write(f"""
name: CI
on: [push]
jobs:
  build:
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: exit 1}}]
  report:
    needs: [build]
    if: always()
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: echo report}}]
  deploy:
    needs: [build]
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: echo deploy}}]
  notify:
    needs: [build]
    if: failure()
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: echo notify}}]
""")
        got, _ = self.statuses(event="push")
        self.assertEqual(got["build"], cr.FAILED)
        self.assertEqual(got["report"], cr.PASSED)    # always() runs anyway
        self.assertEqual(got["deploy"], cr.SKIPPED)   # no if: -> gated
        self.assertEqual(got["notify"], cr.PASSED)    # failure() runs

    def test_ordinary_condition_still_respects_needs(self):
        # normalize() injects success(), so the needs gate is preserved even
        # though the `if:` now takes over the decision.
        self.write(f"""
name: CI
on: [push]
jobs:
  build:
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: exit 1}}]
  after:
    needs: [build]
    if: github.event_name == 'push'
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: echo after}}]
""")
        got, _ = self.statuses(event="push")
        self.assertEqual(got["after"], cr.SKIPPED_IF)

    def test_if_skipped_job_does_not_cost_coverage(self):
        # The key verdict property: a job CI would also skip is faithfully
        # reproduced by skipping, so a full pass stays reachable.
        self.write(f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: echo a}}]
  b:
    if: github.event_name == 'pull_request'
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: echo b}}]
""")
        got, res = self.statuses(event="push")
        self.assertEqual(got["b"], cr.SKIPPED_IF)
        self.assertEqual(res.verdict, cr.FULL_PASS)
        self.assertIn("skipped by their own `if:`", res.note)

    def test_unparseable_job_if_blocks(self):
        self.write(f"""
name: CI
on: [push]
jobs:
  a:
    if: "this is ((not valid"
    runs-on: {LOCAL_RUNNER}
    steps: [{{run: echo a}}]
""")
        got, _ = self.statuses(event="push")
        self.assertEqual(got["a"], cr.UNSUPPORTED)


class TestStepIf(IfIntegration):
    def test_always_step_runs_after_a_failure(self):
        self.write(f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: exit 1
      - name: cleanup
        if: always()
        run: echo cleanup
      - name: unreachable
        if: success()
        run: echo nope
""")
        res = cr.run_ci(self.tmp, event="push")
        job = res.jobs[0]
        self.assertEqual(job.status, cr.FAILED)
        by_index = {s.index: s.status for s in job.steps}
        self.assertEqual(by_index[0], cr.FAILED)
        self.assertEqual(by_index[1], cr.PASSED)     # ran despite the failure
        self.assertEqual(by_index[2], "skipped")     # success() is false now

    def test_false_step_condition_skips_without_failing_the_job(self):
        self.write(f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo one
      - if: github.event_name == 'pull_request'
        run: exit 1
""")
        res = cr.run_ci(self.tmp, event="push")
        self.assertEqual(res.verdict, cr.FULL_PASS)
        self.assertEqual(res.jobs[0].steps[1].status, "skipped")

    def test_step_without_a_condition_keeps_its_implicit_success_gate(self):
        # Regression: teaching the runner to continue past a failure (so
        # `always()` works) must not turn every UNGUARDED later step into one
        # that runs after a failure. A missing `if:` still means success().
        self.write(f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: exit 1
      - run: echo must-not-run
""")
        res = cr.run_ci(self.tmp, event="push")
        self.assertEqual(res.jobs[0].steps[1].status, "skipped")
        self.assertIn("implicit success()", res.jobs[0].steps[1].shim)

    def test_unparseable_step_if_blocks_the_job(self):
        self.write(f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - if: "&&&"
        run: echo a
""")
        res = cr.run_ci(self.tmp, event="push")
        self.assertEqual(res.jobs[0].status, cr.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
