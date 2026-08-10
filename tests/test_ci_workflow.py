"""AgentCI — workflow parsing, matrix expansion, and the job DAG.

The property under test throughout is honesty about coverage: a construct we
cannot reproduce must surface as a blocker, never as a silently narrowed job.
A parser that "helpfully" ignores an unknown action is how a local PASS stops
meaning anything.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.ci_workflow import (  # noqa: E402
    CycleError,
    build_dag,
    discover_workflows,
    expand_matrix,
    host_os,
    inspect_project,
    instantiate,
    parse_workflow,
    runner_os,
    substitute,
)


def write_workflow(root: Path, name: str, body: str) -> Path:
    wf_dir = root / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    path = wf_dir / name
    path.write_text(body, encoding="utf-8")
    return path


class CiTempProject(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRunnerOs(unittest.TestCase):
    def test_known_labels(self):
        self.assertEqual(runner_os("ubuntu-latest"), "Linux")
        self.assertEqual(runner_os("macos-14"), "Darwin")
        self.assertEqual(runner_os("windows-2022"), "Windows")

    def test_unknown_label_is_empty_not_a_guess(self):
        # A self-hosted label we cannot classify must not be assumed to match
        # this host — "" means "unknown", and the caller treats it as runnable
        # rather than inventing an OS.
        self.assertEqual(runner_os("self-hosted-gpu-box"), "")


class TestSubstitute(unittest.TestCase):
    def test_resolves_matrix_and_env(self):
        res = substitute("py${{ matrix.python }} on ${{ env.STAGE }}",
                         {"matrix": {"python": "3.12"}, "env": {"STAGE": "ci"}})
        self.assertEqual(res.text, "py3.12 on ci")
        self.assertEqual(res.unresolved, [])

    def test_unknown_context_is_reported_not_blanked(self):
        # The dangerous failure: `${{ secrets.TOKEN }}` -> "" would make a job
        # pass locally and fail in CI. It must stay literal AND be reported.
        res = substitute("token=${{ secrets.TOKEN }}", {"matrix": {}, "env": {}})
        self.assertIn("secrets.TOKEN", res.unresolved)
        self.assertIn("${{ secrets.TOKEN }}", res.text)

    def test_operator_expression_is_unresolved(self):
        res = substitute("${{ github.ref == 'refs/heads/main' }}", {"github": {}})
        self.assertTrue(res.unresolved)

    def test_plain_text_untouched(self):
        self.assertEqual(substitute("ruff check .", {}).text, "ruff check .")


class TestExpandMatrix(unittest.TestCase):
    def test_no_matrix_is_one_instance(self):
        self.assertEqual(expand_matrix({}), [{}])

    def test_cartesian_product(self):
        combos = expand_matrix({"matrix": {"os": ["a", "b"], "py": ["1", "2"]}})
        self.assertEqual(len(combos), 4)

    def test_exclude_removes(self):
        combos = expand_matrix({"matrix": {
            "os": ["a", "b"], "py": ["1", "2"],
            "exclude": [{"os": "a", "py": "1"}]}})
        self.assertEqual(len(combos), 3)
        self.assertNotIn({"os": "a", "py": "1"}, combos)

    def test_include_extends_matching_combo(self):
        combos = expand_matrix({"matrix": {
            "os": ["a", "b"], "include": [{"os": "a", "extra": "x"}]}})
        got = {c["os"]: c.get("extra") for c in combos}
        self.assertEqual(got["a"], "x")
        self.assertIsNone(got["b"])

    def test_include_appends_standalone(self):
        combos = expand_matrix({"matrix": {
            "os": ["a"], "include": [{"os": "z"}]}})
        self.assertEqual(sorted(c["os"] for c in combos), ["a", "z"])


class TestParse(CiTempProject):
    def test_on_keyword_is_not_swallowed_by_yaml_true(self):
        # `on:` parses as the boolean True in YAML 1.1. Getting this wrong
        # reports "no triggers" for essentially every workflow in existence.
        write_workflow(self.tmp, "ci.yml",
                       "name: CI\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
                       "    steps:\n      - run: echo hi\n")
        wf = parse_workflow(discover_workflows(self.tmp)[0])
        self.assertEqual(wf.name, "CI")
        self.assertIn("push", wf.on)
        self.assertEqual(wf.error, "")

    def test_malformed_yaml_is_carried_not_raised(self):
        write_workflow(self.tmp, "bad.yml", "name: [unclosed\n")
        wf = parse_workflow(discover_workflows(self.tmp)[0])
        self.assertTrue(wf.error)
        self.assertEqual(wf.jobs, {})

    def test_discovers_yml_and_yaml(self):
        write_workflow(self.tmp, "a.yml", "name: A\non: [push]\njobs: {}\n")
        write_workflow(self.tmp, "b.yaml", "name: B\non: [push]\njobs: {}\n")
        self.assertEqual(len(discover_workflows(self.tmp)), 2)

    def test_no_workflow_dir_is_empty_not_error(self):
        self.assertEqual(discover_workflows(self.tmp), [])


class TestInstantiate(CiTempProject):
    def _jobs(self, body: str) -> dict:
        write_workflow(self.tmp, "ci.yml", body)
        wf = parse_workflow(discover_workflows(self.tmp)[0])
        return {i.id: i for i in instantiate(wf)}

    def test_matrix_produces_one_instance_per_cell(self):
        jobs = self._jobs(
            "name: CI\non: [push]\njobs:\n"
            "  test:\n"
            "    runs-on: ${{ matrix.os }}\n"
            "    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest, windows-latest]\n"
            "        py: ['3.11', '3.12']\n"
            "    steps:\n      - run: pytest\n")
        self.assertEqual(len(jobs), 4)
        # runs-on resolved per cell, which is what makes foreign detection work
        self.assertEqual({j.runs_on for j in jobs.values()},
                         {"ubuntu-latest", "windows-latest"})

    def test_unknown_action_blocks_the_job(self):
        jobs = self._jobs(
            "name: CI\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: some/exotic-action@v1\n")
        job = jobs["a"]
        self.assertFalse(job.supported)
        self.assertIn("exotic-action", job.blockers[0])

    def test_shimmed_action_does_not_block(self):
        jobs = self._jobs(
            "name: CI\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - uses: actions/checkout@v4\n      - run: echo hi\n")
        self.assertTrue(jobs["a"].supported)
        self.assertTrue(jobs["a"].steps[0].shim)

    def test_unresolved_expression_in_run_blocks_the_job(self):
        # Running a command containing a literal ${{ }} would execute
        # something other than what CI executes.
        jobs = self._jobs(
            "name: CI\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: deploy --token ${{ secrets.TOKEN }}\n")
        self.assertFalse(jobs["a"].supported)
        self.assertIn("secrets.TOKEN", jobs["a"].blockers[0])

    def test_container_and_services_block(self):
        jobs = self._jobs(
            "name: CI\non: [push]\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    container: python:3.12\n"
            "    steps:\n      - run: echo hi\n")
        self.assertFalse(jobs["a"].supported)

    def test_reusable_workflow_is_visible_but_blocked(self):
        jobs = self._jobs(
            "name: CI\non: [push]\njobs:\n  a:\n    uses: ./.github/workflows/other.yml\n")
        self.assertIn("a", jobs)          # visible in inspect …
        self.assertFalse(jobs["a"].supported)   # … but never run


class TestDag(CiTempProject):
    def test_needs_are_workflow_scoped(self):
        # Two workflows each defining `build` must not cross-link. This was a
        # real bug: a global needs map made Release::build depend on CI::lint.
        write_workflow(self.tmp, "ci.yml",
                       "name: CI\non: [push]\njobs:\n"
                       "  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo a\n"
                       "  build:\n    needs: [lint]\n    runs-on: ubuntu-latest\n"
                       "    steps:\n      - run: echo b\n")
        write_workflow(self.tmp, "release.yml",
                       "name: Release\non: [push]\njobs:\n"
                       "  verify:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo c\n"
                       "  build:\n    needs: [verify]\n    runs-on: ubuntu-latest\n"
                       "    steps:\n      - run: echo d\n")
        dag = build_dag([parse_workflow(p) for p in discover_workflows(self.tmp)])
        builds = {i.workflow: i for i in dag.instances if i.job_id == "build"}
        self.assertEqual({d.job_id for d in dag.deps_of(builds["CI"])}, {"lint"})
        self.assertEqual({d.job_id for d in dag.deps_of(builds["Release"])}, {"verify"})

    def test_keys_are_unique_across_workflows(self):
        write_workflow(self.tmp, "a.yml", "name: A\non: [push]\njobs:\n"
                       "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo x\n")
        write_workflow(self.tmp, "b.yml", "name: B\non: [push]\njobs:\n"
                       "  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo y\n")
        dag = build_dag([parse_workflow(p) for p in discover_workflows(self.tmp)])
        keys = [i.key for i in dag.instances]
        self.assertEqual(len(keys), len(set(keys)))

    def test_topo_order_places_dependents_last(self):
        write_workflow(self.tmp, "ci.yml",
                       "name: CI\non: [push]\njobs:\n"
                       "  build:\n    needs: [lint, test]\n    runs-on: ubuntu-latest\n"
                       "    steps:\n      - run: echo b\n"
                       "  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo l\n"
                       "  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo t\n")
        dag = build_dag(parse_workflow(discover_workflows(self.tmp)[0]))
        order = [i.job_id for i in dag.topo_order()]
        self.assertLess(order.index("lint"), order.index("build"))
        self.assertLess(order.index("test"), order.index("build"))

    def test_cycle_is_reported_with_the_jobs(self):
        write_workflow(self.tmp, "ci.yml",
                       "name: CI\non: [push]\njobs:\n"
                       "  a:\n    needs: [b]\n    runs-on: ubuntu-latest\n"
                       "    steps:\n      - run: echo a\n"
                       "  b:\n    needs: [a]\n    runs-on: ubuntu-latest\n"
                       "    steps:\n      - run: echo b\n")
        dag = build_dag(parse_workflow(discover_workflows(self.tmp)[0]))
        with self.assertRaises(CycleError) as ctx:
            dag.topo_order()
        self.assertIn("a", str(ctx.exception))

    def test_unknown_need_is_reported(self):
        write_workflow(self.tmp, "ci.yml",
                       "name: CI\non: [push]\njobs:\n"
                       "  a:\n    needs: [ghost]\n    runs-on: ubuntu-latest\n"
                       "    steps:\n      - run: echo a\n")
        dag = build_dag(parse_workflow(discover_workflows(self.tmp)[0]))
        self.assertEqual(dag.unknown_needs()[0]["missing_need"], "ghost")

    def test_resolve_accepts_key_id_and_bare_job(self):
        write_workflow(self.tmp, "ci.yml",
                       "name: CI\non: [push]\njobs:\n  test:\n"
                       "    runs-on: ${{ matrix.os }}\n"
                       "    strategy:\n      matrix:\n        os: [ubuntu-latest, windows-latest]\n"
                       "    steps:\n      - run: echo t\n")
        dag = build_dag(parse_workflow(discover_workflows(self.tmp)[0]))
        self.assertEqual(len(dag.resolve("test")), 2)          # all cells
        one = dag.instances[0]
        self.assertEqual(dag.resolve(one.key), [one])          # exact key
        self.assertEqual(dag.resolve(one.id), [one])           # matrix id
        self.assertEqual(dag.resolve("nope"), [])


class TestInspectProject(CiTempProject):
    def test_partitions_runnable_foreign_and_unsupported(self):
        other = {"Linux": "windows-latest"}.get(host_os(), "ubuntu-latest")
        write_workflow(self.tmp, "ci.yml",
                       f"name: CI\non: [push]\njobs:\n"
                       f"  here:\n    runs-on: self-hosted-unknown\n"
                       f"    steps:\n      - run: echo ok\n"
                       f"  elsewhere:\n    runs-on: {other}\n"
                       f"    steps:\n      - run: echo ok\n"
                       f"  broken:\n    runs-on: self-hosted-unknown\n"
                       f"    steps:\n      - uses: exotic/thing@v1\n")
        # engine="native" pins the pre-container partition. With act installed
        # the Linux job would be containerised instead of refused — correct,
        # and covered in test_ci_act.py — but this test is about the native
        # split and must not depend on what the machine happens to have.
        data = inspect_project(self.tmp, engine="native")
        self.assertIn("CI::here", data["runnable"])
        self.assertIn("CI::elsewhere", [f["key"] for f in data["foreign"]])
        self.assertIn("CI::broken", [u["key"] for u in data["unsupported"]])

    def test_empty_project_reports_nothing_rather_than_failing(self):
        data = inspect_project(self.tmp)
        self.assertEqual(data["workflows"], [])
        self.assertEqual(data["jobs"], [])


if __name__ == "__main__":
    unittest.main()
