"""AgentCI — C3 owns the DAG: job outputs, run-time `needs.*`, one job per act.

Three facts, each measured on 2026-09-12 with a two-job probe workflow
before a line of this was written:

  * `act -j b` runs `a` first when `b` needs it. Under C3's per-job act
    invocation that meant every dependency ran twice and an aggregator job
    replayed the whole workflow inside its container.
  * act logs each GITHUB_OUTPUT write as `::set-output:: k=v`, without the
    step id.
  * With a declared event and no payload, `github.event.*` is "" — the same
    thing GitHub yields for a field a sparse payload lacks.

The tests below hold C3 to those facts: a derived single-job workflow for
act, real `needs.<job>.outputs.<k>` for both engines, and `github.event.*`
that is "" only when the caller declared the event.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import ci_act  # noqa: E402
from services import ci_runner as cr  # noqa: E402
from services import ci_workflow as cw  # noqa: E402
from services.ci_workflow import (  # noqa: E402
    build_dag,
    discover_workflows,
    host_os,
    parse_workflow,
)

LOCAL_RUNNER = {"Linux": "ubuntu-latest", "Darwin": "macos-latest",
                "Windows": "windows-latest"}[host_os()]


def workflow(root: Path, body: str, name: str = "ci.yml") -> Path:
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    path = wf / name
    path.write_text(body, encoding="utf-8")
    return path


def instances(root: Path, event: str = "") -> dict:
    dag = build_dag([parse_workflow(p) for p in discover_workflows(root)],
                    event=event)
    return {i.job_id: i for i in dag.instances}


class TempProject(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── Classification at DAG-build time ────────────────────────────────────────

class TestClassification(unittest.TestCase):
    def test_three_classes(self):
        hard, deferred, event = cw.classify_unresolved([
            "secrets.TOKEN", "needs.build.result", "steps.x.outputs.y",
            "github.event.before", "vars.REGION",
        ])
        self.assertEqual(hard, ["secrets.TOKEN", "vars.REGION"])
        self.assertEqual(deferred, ["needs.build.result", "steps.x.outputs.y"])
        self.assertEqual(event, ["github.event.before"])

    def test_late_substitute_fills_needs_and_blanks_event_only_when_declared(self):
        runtime = {"needs": {"a": {"result": "success", "outputs": {"v": "42"}}},
                   "steps": {}}
        text = "r=${{ needs.a.result }} v=${{ needs.a.outputs.v }} e=[${{ github.event.before }}]"
        undeclared = cw.late_substitute(text, runtime, event_declared=False)
        self.assertEqual(undeclared.unresolved, ["github.event.before"])
        declared = cw.late_substitute(text, runtime, event_declared=True)
        self.assertEqual(declared.text, "r=success v=42 e=[]")
        self.assertEqual(declared.unresolved, [])

    def test_late_substitute_reports_a_genuine_gap(self):
        res = cw.late_substitute("${{ needs.a.outputs.missing }}",
                                 {"needs": {"a": {"result": "success", "outputs": {}}},
                                  "steps": {}}, True)
        self.assertEqual(res.unresolved, ["needs.a.outputs.missing"])


class TestBuildTimeBlockers(TempProject):
    BODY = """
name: CI
on: [push, pull_request]
jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      code: ${{ steps.decide.outputs.code }}
    steps:
      - id: decide
        run: echo "base=${{ github.event.pull_request.base.sha }}"; echo "code=true" >> "$GITHUB_OUTPUT"
  test:
    needs: changes
    if: needs.changes.outputs.code == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: echo "changes said ${{ needs.changes.result }}"
  secretive:
    runs-on: ubuntu-latest
    steps:
      - run: echo ${{ secrets.TOKEN }}
"""

    def test_needs_refs_are_not_blockers_on_either_engine(self):
        by = instances(self.tmp_with(self.BODY))
        self.assertEqual(by["test"].blockers, [])
        self.assertEqual(by["test"].act_blockers, [])
        self.assertEqual(by["test"].needs, ["changes"])

    def test_event_refs_block_native_until_the_event_is_declared_never_act(self):
        by = instances(self.tmp_with(self.BODY))
        self.assertTrue(any("github.event.pull_request.base.sha" in b
                            for b in by["changes"].blockers))
        self.assertEqual(by["changes"].act_blockers, [])
        declared = instances(self.tmp, event="pull_request")
        self.assertEqual(declared["changes"].blockers, [])

    def test_secrets_still_block_both_engines(self):
        by = instances(self.tmp_with(self.BODY))
        self.assertTrue(any("secrets.TOKEN" in b for b in by["secretive"].blockers))
        self.assertTrue(any("secrets.TOKEN" in b for b in by["secretive"].act_blockers))

    def test_outputs_and_step_ids_survive_instantiation(self):
        by = instances(self.tmp_with(self.BODY))
        self.assertEqual(by["changes"].outputs,
                         {"code": "${{ steps.decide.outputs.code }}"})
        self.assertEqual(by["changes"].steps[0].id, "decide")

    def tmp_with(self, body: str) -> Path:
        workflow(self.tmp, body)
        return self.tmp


# ── The derived single-job workflow for act ─────────────────────────────────

class TestDeriveWorkflow(TempProject):
    BODY = """
name: probe
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    outputs:
      val: ${{ steps.s.outputs.val }}
    steps:
      - id: s
        run: echo "val=42" >> "$GITHUB_OUTPUT"
  b:
    needs: a
    if: needs.a.outputs.val == '42'
    runs-on: ubuntu-latest
    strategy:
      matrix:
        n: [1, 2]
    steps:
      - run: echo "r=${{ needs.a.result }} v=${{ needs.a.outputs.val }} n=${{ matrix.n }}"
"""

    def test_holds_one_job_without_needs_or_if_and_spells_needs_out(self):
        workflow(self.tmp, self.BODY)
        b = next(i for i in instances(self.tmp).values() if i.job_id == "b")
        text, unresolved = ci_act.derive_workflow(
            b, {"a": {"result": "success", "outputs": {"val": "42"}}})
        self.assertEqual(unresolved, [])
        derived = parse_workflow_text(text)
        self.assertEqual(list(derived["jobs"]), ["b"])
        self.assertNotIn("needs", derived["jobs"]["b"])
        self.assertNotIn("if", derived["jobs"]["b"])
        run = derived["jobs"]["b"]["steps"][0]["run"]
        self.assertIn("r=success v=42", run)
        self.assertIn("${{ matrix.n }}", run)      # act still expands matrix
        self.assertIn("on", derived)               # not the YAML-1.1 `True`
        self.assertEqual(derived["name"], "probe")

    def test_an_output_c3_never_recorded_is_reported_not_blanked(self):
        workflow(self.tmp, self.BODY)
        b = next(i for i in instances(self.tmp).values() if i.job_id == "b")
        text, unresolved = ci_act.derive_workflow(
            b, {"a": {"result": "success", "outputs": {}}})
        self.assertEqual(unresolved, ["${{ needs.a.outputs.val }}"])

    def test_build_command_prefers_the_derived_file(self):
        workflow(self.tmp, self.BODY)
        b = next(i for i in instances(self.tmp).values() if i.job_id == "b")
        cmd = ci_act.build_command(b, self.tmp, "act", event="push",
                                   workflow_file=r"C:\tmp\derived.yml")
        self.assertIn("C:/tmp/derived.yml", cmd)
        self.assertIn("-j", cmd)
        self.assertNotIn(".github/workflows/ci.yml", " ".join(cmd))

    def test_run_job_refuses_before_starting_act_on_an_unresolved_need(self):
        workflow(self.tmp, self.BODY)
        b = next(i for i in instances(self.tmp).values() if i.job_id == "b")
        outcome = ci_act.run_job(b, self.tmp, act_path="act-that-does-not-exist",
                                 needs_ctx={"a": {"result": "success", "outputs": {}}})
        self.assertEqual(outcome["unresolved"], ["${{ needs.a.outputs.val }}"])
        self.assertEqual(outcome["command"], "")


def parse_workflow_text(text: str) -> dict:
    import yaml
    return yaml.safe_load(text)


class TestPullPolicy(TempProject):
    BODY = """
name: CI
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
"""

    def test_an_image_already_on_disk_is_not_pulled(self):
        from unittest import mock
        workflow(self.tmp, self.BODY)
        a = instances(self.tmp)["a"]
        with mock.patch.object(ci_act, "image_present", return_value=True):
            outcome = ci_act.run_job(a, self.tmp, act_path="act-that-does-not-exist")
        self.assertIn("--pull=false", outcome["command"])

    def test_a_missing_image_is_pulled(self):
        from unittest import mock
        workflow(self.tmp, self.BODY)
        a = instances(self.tmp)["a"]
        with mock.patch.object(ci_act, "image_present", return_value=False):
            outcome = ci_act.run_job(a, self.tmp, act_path="act-that-does-not-exist")
        self.assertIn("--pull=true", outcome["command"])

    def test_each_run_gets_its_own_artifact_server_port(self):
        # act binds 34567 by default; two runs on one box collide there.
        from unittest import mock
        workflow(self.tmp, self.BODY)
        a = instances(self.tmp)["a"]
        with mock.patch.object(ci_act, "image_present", return_value=True), \
                mock.patch.object(ci_act, "free_port", return_value=45678):
            outcome = ci_act.run_job(a, self.tmp, act_path="act-that-does-not-exist",
                                     artifact_dir=str(self.tmp / "art"))
        self.assertIn("--artifact-server-port 45678", outcome["command"])
        self.assertNotIn("34567", outcome["command"])
        port = ci_act.free_port()
        self.assertTrue(1024 <= port <= 65535)


class TestActLogOutputs(unittest.TestCase):
    def test_reads_set_output_lines_last_write_wins(self):
        log = (
            "[probe/a] ⭐ Run Main echo\n"
            "[probe/a]   ⚙  ::set-output:: val=41\n"
            "[probe/a]   ⚙  ::set-output:: val=42\n"
            "[probe/a]   ⚙  ::set-output:: name=with spaces \n"
            "[probe/a] 🏁  Job succeeded\n"
        )
        self.assertEqual(ci_act.job_outputs_from_log(log),
                         {"val": "42", "name": "with spaces"})


# ── The native runner: outputs flow into `needs` ────────────────────────────

class TestNativeOutputs(TempProject):
    def test_outputs_reach_dependents_in_if_and_in_run_text(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    outputs:
      flag: ${{{{ steps.s.outputs.flag }}}}
    steps:
      - id: s
        run: echo "flag=yes" >> "$GITHUB_OUTPUT"
  b:
    needs: a
    if: needs.a.outputs.flag == 'yes'
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo "B-SAW result=${{{{ needs.a.result }}}} flag=${{{{ needs.a.outputs.flag }}}}"
  c:
    needs: a
    if: needs.a.outputs.flag == 'no'
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo never
""")
        res = cr.run_ci(self.tmp, engine="native")
        jobs = {j.job_id: j for j in res.jobs}
        self.assertEqual(jobs["a"].status, cr.PASSED, jobs["a"].reason)
        self.assertEqual(jobs["a"].outputs, {"flag": "yes"})
        self.assertEqual(jobs["b"].status, cr.PASSED, jobs["b"].reason)
        log = Path(jobs["b"].log_path).read_text(encoding="utf-8")
        self.assertIn("B-SAW result=success flag=yes", log)
        self.assertEqual(jobs["c"].status, cr.SKIPPED_IF)

    def test_a_declared_output_nobody_wrote_is_refused_not_blanked(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    outputs:
      flag: ${{{{ steps.s.outputs.flag }}}}
    steps:
      - id: s
        run: echo nothing
""")
        res = cr.run_ci(self.tmp, engine="native")
        job = res.jobs[0]
        self.assertEqual(job.status, cr.UNSUPPORTED)
        self.assertIn("flag", job.reason)

    def test_event_field_is_blank_only_under_a_declared_event(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo "before=[${{{{ github.event.before }}}}]"
""")
        undeclared = cr.run_ci(self.tmp, engine="native")
        self.assertEqual(undeclared.jobs[0].status, cr.UNSUPPORTED)
        declared = cr.run_ci(self.tmp, engine="native", event="push")
        self.assertEqual(declared.jobs[0].status, cr.PASSED, declared.jobs[0].reason)
        log = Path(declared.jobs[0].log_path).read_text(encoding="utf-8")
        self.assertIn("before=[]", log)

    def test_selecting_a_dependent_job_runs_what_it_needs(self):
        # `--job smokes` used to deselect `changes`, and then the `if:` that
        # reads changes' output was unjudgeable.
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  changes:
    runs-on: {LOCAL_RUNNER}
    outputs:
      code: ${{{{ steps.d.outputs.code }}}}
    steps:
      - id: d
        run: echo "code=true" >> "$GITHUB_OUTPUT"
  smokes:
    needs: changes
    if: needs.changes.outputs.code == 'true'
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo smoked
  unrelated:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo not me
""")
        res = cr.run_ci(self.tmp, engine="native", selector="smokes")
        jobs = {j.job_id: j for j in res.jobs}
        self.assertEqual(jobs["changes"].status, cr.PASSED, jobs["changes"].reason)
        self.assertEqual(jobs["smokes"].status, cr.PASSED, jobs["smokes"].reason)
        self.assertEqual(jobs["unrelated"].status, cr.DESELECTED)

    def test_jobs_that_publish_outputs_are_never_served_from_cache(self):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    outputs:
      flag: ${{{{ steps.s.outputs.flag }}}}
    steps:
      - id: s
        run: echo "flag=yes" >> "$GITHUB_OUTPUT"
""")
        cr.run_ci(self.tmp, engine="native")
        second = cr.run_ci(self.tmp, engine="native")
        self.assertEqual(second.jobs[0].status, cr.PASSED)
        self.assertNotEqual(second.jobs[0].status, cr.CACHED)
        self.assertEqual(second.jobs[0].outputs, {"flag": "yes"})


if __name__ == "__main__":
    unittest.main()
