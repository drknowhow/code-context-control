"""AgentCI — required-mode planning and the host-mutation guard.

Two things are pinned here, and the second was learned by breaking something.

**The planner is conservative.** A job wrongly skipped is a false green handed
over at the moment someone decides to push. A job wrongly run costs seconds. So
anything the planner cannot reason about is RUN, and every decision carries a
reason (spec §14).

**The native engine has no isolation.** A required-mode run selected every job
and executed this repository's own `test` job natively — whose steps are
`python -m pip install -e ".[dev]"`. That uninstalled C3 from site-packages,
replaced it with an editable install, and was killed mid-write, taking every
project's hooks with it. "Run the repo's real CI" means "let a YAML file
reconfigure this machine" unless something stops it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import ci_act, ci_impact  # noqa: E402
from services import ci_runner as cr  # noqa: E402
from services.ci_workflow import (  # noqa: E402
    build_dag,
    discover_workflows,
    host_os,
    parse_workflow,
)

LOCAL_RUNNER = {"Linux": "ubuntu-latest", "Darwin": "macos-latest",
                "Windows": "windows-latest"}[host_os()]


class ImpactBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        subprocess.run(["git", "init", "-q", "."], cwd=self.tmp,
                       capture_output=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def workflow(self, body: str, name: str = "ci.yml"):
        wf = self.tmp / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / name).write_text(body, encoding="utf-8")

    def touch(self, rel: str, text: str = "x"):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def plan(self, changed=None, **kw):
        wfs = [parse_workflow(p) for p in discover_workflows(self.tmp)]
        dag = build_dag([w for w in wfs if not w.error])
        return ci_impact.plan_required(self.tmp, dag.topo_order(), wfs,
                                       changed=changed, **kw)


class TestChangedPaths(ImpactBase):
    def test_unstaged_path_keeps_its_first_character(self):
        # Regression: `git status --porcelain` marks an unstaged edit with a
        # LEADING SPACE (` M path`). Stripping the whole output ate that space
        # on the first line only, shifting the slice by one and yielding
        # `ervices/ci_runner.py`. A path that loses a character matches no rule,
        # so the planner silently mis-decided one job per run.
        self.touch("services/thing.py")
        subprocess.run(["git", "add", "-A"], cwd=self.tmp, capture_output=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"], cwd=self.tmp, capture_output=True)
        (self.tmp / "services" / "thing.py").write_text("changed", encoding="utf-8")

        paths = ci_impact.changed_paths(self.tmp)
        self.assertIn("services/thing.py", paths)
        self.assertNotIn("ervices/thing.py", paths)

    def test_untracked_files_count(self):
        self.touch("brand_new.py")
        self.assertIn("brand_new.py", ci_impact.changed_paths(self.tmp))


class TestPlanner(ImpactBase):
    WF = """
name: CI
on: [push]
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
  docs:
    runs-on: ubuntu-latest
    steps:
      - run: mkdocs build
"""

    def test_unmapped_job_runs_rather_than_being_guessed_away(self):
        self.workflow(self.WF)
        plan = self.plan(changed=["src/app.py"])
        self.assertEqual(len(plan.selected), 2)
        self.assertTrue(all(d.decision == ci_impact.RUN for d in plan.decisions))
        self.assertIn("risks a false pass", plan.decisions[0].reason)

    def test_every_decision_carries_a_reason(self):
        # Spec §14 requires it: a plan you cannot argue with is one you must
        # simply trust.
        self.workflow(self.WF)
        for d in self.plan(changed=["src/app.py"]).decisions:
            self.assertTrue(d.reason.strip(), f"{d.job} has no reason")

    def test_required_map_narrows(self):
        self.workflow(self.WF)
        (self.tmp / ".c3" / "config.json").write_text(json.dumps(
            {"ci": {"required_map": {"docs": ["docs/**", "*.md"],
                                     "unit": ["src/**"]}}}), encoding="utf-8")
        plan = self.plan(changed=["src/app.py"])
        decisions = {d.job: d for d in plan.decisions}
        self.assertEqual(decisions["CI::unit"].decision, ci_impact.RUN)
        self.assertEqual(decisions["CI::docs"].decision, ci_impact.SKIP)
        self.assertIn("required_map", decisions["CI::docs"].reason)

    def test_workflow_path_filter_is_honoured(self):
        self.workflow("""
name: Docs
on:
  push:
    paths:
      - 'docs/**'
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: mkdocs build
""")
        skipped = self.plan(changed=["src/app.py"], event="push")
        self.assertEqual(skipped.decisions[0].decision, ci_impact.SKIP)
        selected = self.plan(changed=["docs/index.md"], event="push")
        self.assertEqual(selected.decisions[0].decision, ci_impact.RUN)

    def test_changing_the_workflow_runs_everything_in_it(self):
        self.workflow(self.WF)
        plan = self.plan(changed=[".github/workflows/ci.yml"])
        self.assertTrue(all(d.decision == ci_impact.RUN for d in plan.decisions))
        self.assertIn("job definition itself moved", plan.decisions[0].reason)

    def test_no_changes_selects_nothing_and_says_so(self):
        self.workflow(self.WF)
        plan = self.plan(changed=[])
        self.assertEqual(plan.selected, [])
        self.assertIn("no changed files", plan.note)

    def test_broad_selection_is_announced_not_hidden(self):
        self.workflow(self.WF)
        self.assertIn("as broad as full mode",
                      self.plan(changed=["src/app.py"]).note)


class TestRequiredModeRun(ImpactBase):
    def test_a_narrowed_run_can_never_be_a_full_pass(self):
        self.workflow(f"""
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
        (self.tmp / ".c3" / "config.json").write_text(json.dumps(
            {"ci": {"required_map": {"b": ["never/**"]}}}), encoding="utf-8")
        self.touch("src/app.py")
        res = cr.run_ci(self.tmp, mode=ci_impact.MODE_REQUIRED)
        by_id = {j.job_id: j for j in res.jobs}
        self.assertEqual(by_id["a"].status, cr.PASSED)
        self.assertEqual(by_id["b"].status, cr.DESELECTED)
        self.assertEqual(res.verdict, cr.PARTIAL_PASS)
        # The planner's reason must survive into the run report.
        self.assertIn("required_map", by_id["b"].reason)
        self.assertEqual(res.mode, ci_impact.MODE_REQUIRED)


class TestHostMutationGuard(ImpactBase):
    """The guard that would have prevented C3 uninstalling itself."""

    PIP_WF = f"""
name: CI
on: [push]
jobs:
  test:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: python -m pip install -e ".[dev]"
      - run: pytest -q
"""

    def test_detects_the_step_that_broke_the_install(self):
        self.workflow(self.PIP_WF)
        dag = build_dag([parse_workflow(p) for p in discover_workflows(self.tmp)])
        found = ci_act.host_mutations(dag.instances[0])
        self.assertTrue(found)
        self.assertIn("pip install", found[0])

    def test_native_engine_refuses_it_and_points_at_the_container(self):
        self.workflow(self.PIP_WF)
        res = cr.run_ci(self.tmp, engine="native")
        job = res.jobs[0]
        self.assertEqual(job.status, cr.UNSUPPORTED)
        self.assertIn("would modify THIS machine", job.reason)
        self.assertIn("container", job.reason)
        self.assertNotEqual(res.verdict, cr.FULL_PASS)

    def test_opt_in_allows_it(self):
        self.workflow(f"""
name: CI
on: [push]
jobs:
  test:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo "pretend pip install ran"
""")
        res = cr.run_ci(self.tmp, engine="native", allow_host_mutation=True)
        self.assertEqual(res.verdict, cr.FULL_PASS)

    def test_container_runs_are_not_gated(self):
        # act isolates the change, so there is nothing to protect against.
        self.workflow("""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: python -m pip install -e .
""")
        with mock.patch.object(ci_act, "availability", return_value={"ok": True}), \
             mock.patch.object(ci_act, "run_job",
                               return_value={"exit_code": 0, "output": "ok",
                                             "timed_out": False,
                                             "duration_ms": 1, "command": "act"}):
            res = cr.run_ci(self.tmp, engine="act")
        self.assertEqual(res.jobs[0].status, cr.PASSED)

    def test_an_ordinary_job_is_not_gated(self):
        self.workflow(f"""
name: CI
on: [push]
jobs:
  test:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: pytest -q
""")
        dag = build_dag([parse_workflow(p) for p in discover_workflows(self.tmp)])
        self.assertEqual(ci_act.host_mutations(dag.instances[0]), [])


if __name__ == "__main__":
    unittest.main()
