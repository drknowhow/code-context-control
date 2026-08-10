"""AgentCI — the act (container) execution engine.

Almost everything here is pure: command construction, log de-prefixing,
side-effect detection and engine selection are all decidable without a
container, which is what lets these run on a CI machine that has neither act
nor Docker. The one test that needs both is skipped when they are absent —
and skipped loudly, because "0 failures because nothing ran" is the exact
shape of dishonesty this module exists to avoid.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import ci_act  # noqa: E402
from services import ci_runner as cr  # noqa: E402
from services.ci_workflow import (  # noqa: E402
    build_dag,
    discover_workflows,
    host_os,
    parse_workflow,
)

ACT_STATE = ci_act.availability()
ACT_READY = bool(ACT_STATE.get("ok"))


def workflow(root: Path, body: str, name: str = "ci.yml") -> None:
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / name).write_text(body, encoding="utf-8")


def first_job(root: Path, job_id: str = ""):
    dag = build_dag([parse_workflow(p) for p in discover_workflows(root)])
    if job_id:
        return next(i for i in dag.instances if i.job_id == job_id)
    return dag.instances[0]


class ActTempProject(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestImages(unittest.TestCase):
    def test_known_ubuntu_labels_map_to_runner_images(self):
        self.assertIn("catthehacker", ci_act.image_for("ubuntu-latest"))
        self.assertIn("act-22.04", ci_act.image_for("ubuntu-22.04"))

    def test_unknown_label_falls_back_rather_than_failing(self):
        self.assertEqual(ci_act.image_for("weird-runner"), ci_act.DEFAULT_IMAGE)


class TestBuildCommand(ActTempProject):
    def test_carries_the_flags_that_make_act_usable(self):
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
""")
        cmd = ci_act.build_command(first_job(self.tmp), self.tmp, "act",
                                   event="push")
        # --bind: act's default copy-mode arrives EMPTY against a Windows host
        # path, and every step then fails on missing files.
        self.assertIn("--bind", cmd)
        # -P: without an explicit image act prompts interactively on first use,
        # which would hang any automated run.
        self.assertTrue(any(a.startswith("ubuntu-latest=") for a in cmd))
        self.assertIn("-j", cmd)
        self.assertIn("lint", cmd)
        self.assertEqual(cmd[1], "push")

    def test_scopes_to_the_workflow_file(self):
        # Two workflows sharing a job name is the case act itself warns about.
        workflow(self.tmp, "name: A\non: [push]\njobs:\n  build:\n"
                           "    runs-on: ubuntu-latest\n    steps:\n      - run: echo a\n",
                 name="a.yml")
        workflow(self.tmp, "name: B\non: [push]\njobs:\n  build:\n"
                           "    runs-on: ubuntu-latest\n    steps:\n      - run: echo b\n",
                 name="b.yml")
        dag = build_dag([parse_workflow(p) for p in discover_workflows(self.tmp)])
        inst = next(i for i in dag.instances if i.workflow == "B")
        cmd = ci_act.build_command(inst, self.tmp, "act")
        self.assertIn("-W", cmd)
        self.assertIn("b.yml", cmd[cmd.index("-W") + 1])

    def test_selects_a_single_matrix_cell(self):
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        py: ["3.11", "3.12"]
    steps:
      - run: pytest
""")
        dag = build_dag([parse_workflow(p) for p in discover_workflows(self.tmp)])
        inst = next(i for i in dag.instances if i.matrix.get("py") == "3.12")
        cmd = ci_act.build_command(inst, self.tmp, "act")
        self.assertIn("--matrix", cmd)
        self.assertIn("py:3.12", cmd)

    def test_never_hands_act_the_repository_secrets(self):
        # act reads .secrets and .env from the repo by default. A local run
        # must not be able to authenticate as the real project.
        workflow(self.tmp, "name: CI\non: [push]\njobs:\n  a:\n"
                           "    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n")
        cmd = ci_act.build_command(first_job(self.tmp), self.tmp, "act")
        self.assertIn("--secret-file", cmd)
        self.assertIn("--env-file", cmd)
        secret = cmd[cmd.index("--secret-file") + 1]
        self.assertNotIn(".secrets", secret)


class TestProgramOutput(unittest.TestCase):
    def test_strips_act_prefixes_so_paths_survive(self):
        # Regression: parsing the raw log captured act's prefix as the
        # filename — file='[CI/lint] * Run Main echo "app/x.py'.
        raw = ('[CI/lint] Run Main ruff check .\n'
               '[CI/lint]   | app/x.py:3:1: F401 unused\n'
               '[CI/lint]   | app/y.py:9:2: E501 too long\n'
               '[CI/lint]   Failure - Main ruff check .\n')
        out = ci_act.program_output(raw)
        self.assertEqual(out, "app/x.py:3:1: F401 unused\n"
                              "app/y.py:9:2: E501 too long")

    def test_falls_back_to_raw_when_nothing_matches(self):
        # An act failure that never reached a command still has to be readable.
        raw = "Error: Cannot connect to the Docker daemon"
        self.assertEqual(ci_act.program_output(raw), raw)

    def test_parses_cleanly_end_to_end(self):
        from services import ci_failures
        raw = '[CI/lint]   | app/thing.py:14:5: F401 unused import\n'
        res = ci_failures.parse(ci_act.program_output(raw), exit_code=1)
        self.assertEqual(res.parser, "ruff")
        self.assertEqual(res.failures[0].file, "app/thing.py")
        self.assertEqual(res.failures[0].line, 14)


class TestSideEffects(ActTempProject):
    def test_flags_publishing_actions(self):
        workflow(self.tmp, """
name: R
on: [push]
jobs:
  pub:
    runs-on: ubuntu-latest
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
""")
        self.assertTrue(ci_act.side_effects(first_job(self.tmp)))

    def test_flags_publishing_commands(self):
        workflow(self.tmp, """
name: R
on: [push]
jobs:
  pub:
    runs-on: ubuntu-latest
    steps:
      - run: twine upload dist/*
""")
        risks = ci_act.side_effects(first_job(self.tmp))
        self.assertTrue(any("twine upload" in r for r in risks))

    def test_ordinary_job_is_not_flagged(self):
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest -q
""")
        self.assertEqual(ci_act.side_effects(first_job(self.tmp)), [])


class TestEngineBlockers(ActTempProject):
    def test_unknown_action_blocks_native_but_not_act(self):
        # This is the whole reason the act engine exists.
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: some/third-party-action@v3
""")
        inst = first_job(self.tmp)
        self.assertFalse(inst.supported_by("native"))
        self.assertTrue(inst.supported_by("act"))

    def test_missing_secret_blocks_both_engines(self):
        # No engine can reproduce a job whose input does not exist.
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: deploy --token ${{ secrets.TOKEN }}
""")
        inst = first_job(self.tmp)
        self.assertFalse(inst.supported_by("native"))
        self.assertFalse(inst.supported_by("act"))


class TestEngineSelection(ActTempProject):
    OK = {"ok": True}
    NO = {"ok": False, "reason": "act is not installed"}

    def _inst(self, runs_on: str):
        workflow(self.tmp, f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {runs_on}
    steps:
      - run: echo hi
""")
        return first_job(self.tmp)

    def test_matching_host_runs_native(self):
        local = {"Linux": "ubuntu-latest", "Darwin": "macos-latest",
                 "Windows": "windows-latest"}[host_os()]
        engine, fidelity, _ = cr._pick_engine(self._inst(local), "auto",
                                              self.OK, False)
        self.assertEqual((engine, fidelity), ("native", cr.FIDELITY_NATIVE))

    def test_linux_job_prefers_a_container_when_act_is_available(self):
        if host_os() == "Linux":
            self.skipTest("ubuntu-latest is native on a Linux host")
        engine, fidelity, _ = cr._pick_engine(self._inst("ubuntu-latest"),
                                              "auto", self.OK, False)
        self.assertEqual((engine, fidelity), ("act", cr.FIDELITY_CONTAINER))

    def test_linux_job_is_refused_without_act_and_says_why(self):
        if host_os() == "Linux":
            self.skipTest("ubuntu-latest is native on a Linux host")
        engine, _, why = cr._pick_engine(self._inst("ubuntu-latest"), "auto",
                                         self.NO, False)
        self.assertIsNone(engine)
        self.assertIn("act engine is unavailable", why)

    def test_macos_is_refused_and_the_reason_is_permanent(self):
        if host_os() == "Darwin":
            self.skipTest("macos-latest is native on a macOS host")
        engine, _, why = cr._pick_engine(self._inst("macos-latest"), "auto",
                                         self.OK, False)
        self.assertIsNone(engine)
        self.assertIn("no macOS containers", why)

    def test_allow_foreign_degrades_to_cross_os_rather_than_refusing(self):
        if host_os() == "Darwin":
            self.skipTest("macos-latest is native on a macOS host")
        engine, fidelity, _ = cr._pick_engine(self._inst("macos-latest"),
                                              "auto", self.OK, True)
        self.assertEqual((engine, fidelity), ("native", cr.FIDELITY_CROSS_OS))


class TestInspectPartition(ActTempProject):
    """The partition every surface reads — tool text, --json, and the Hub.

    Regression: the Hub re-derived runnability in JavaScript from
    `act_could_run`, which means "act has no blockers with this job" and NOT
    "act can run it here" — act only does Linux. Every macOS cell rendered as a
    green PASS it could never earn. The fix was to compute this once, here,
    and have the UI read it; these tests are what keep that true.
    """

    def _inspect(self, act_ok: bool):
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  linux:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
  mac:
    runs-on: macos-latest
    steps:
      - run: echo hi
  broken:
    runs-on: ubuntu-latest
    steps:
      - run: deploy --token ${{ secrets.TOKEN }}
""")
        from services.ci_workflow import inspect_project
        with mock.patch.object(ci_act, "availability",
                               return_value={"ok": act_ok, "reason": "stubbed"}):
            return inspect_project(self.tmp)

    def test_macos_is_never_container_runnable_even_with_act(self):
        data = self._inspect(act_ok=True)
        mac = "CI::mac"
        self.assertNotIn(mac, data["runnable_container"])
        self.assertNotIn(mac, data["runnable"])
        self.assertIn(mac, [f["key"] for f in data["foreign"]])

    def test_linux_moves_from_foreign_to_container_when_act_appears(self):
        if host_os() == "Linux":
            self.skipTest("ubuntu-latest is native on a Linux host")
        without = self._inspect(act_ok=False)
        self.assertIn("CI::linux", [f["key"] for f in without["foreign"]])
        with_act = self._inspect(act_ok=True)
        self.assertIn("CI::linux", with_act["runnable_container"])

    def test_a_job_blocked_on_every_engine_is_unsupported_not_foreign(self):
        # A missing secret is not a reachability problem, and sending the
        # reader to "install act" would be the wrong fix.
        data = self._inspect(act_ok=True)
        self.assertIn("CI::broken", [u["key"] for u in data["unsupported"]])
        self.assertNotIn("CI::broken", [f["key"] for f in data["foreign"]])

    def test_runnable_is_the_union_of_both_engines(self):
        data = self._inspect(act_ok=True)
        self.assertEqual(
            sorted(data["runnable"]),
            sorted(data["runnable_native"] + data["runnable_container"]))


class TestAvailability(unittest.TestCase):
    def test_missing_act_reports_how_to_install_it(self):
        with mock.patch.object(ci_act, "find_act", return_value=""):
            state = ci_act.availability()
        self.assertFalse(state["ok"])
        self.assertIn("install", state["reason"].lower())

    def test_requested_act_engine_fails_the_run_when_unavailable(self):
        # Silently falling back to native would be the wrong kindness: the
        # caller asked for container fidelity and would not have got it.
        tmp = Path(tempfile.mkdtemp())
        try:
            workflow(tmp, """
name: CI
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
""")
            with mock.patch.object(ci_act, "availability",
                                   return_value={"ok": False, "reason": "no act"}):
                res = cr.run_ci(tmp, engine="act")
            self.assertEqual(res.verdict, cr.FAIL)
            self.assertIn("no act", res.note)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSideEffectGate(ActTempProject):
    def test_publishing_job_is_refused_on_the_act_engine(self):
        workflow(self.tmp, """
name: R
on: [push]
jobs:
  pub:
    runs-on: ubuntu-latest
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
""")
        with mock.patch.object(ci_act, "availability", return_value={"ok": True}):
            res = cr.run_ci(self.tmp, engine="act")
        job = res.jobs[0]
        self.assertEqual(job.status, cr.UNSUPPORTED)
        self.assertIn("publishes or deploys", job.reason)

    def test_opt_in_lets_it_through_the_gate(self):
        workflow(self.tmp, """
name: R
on: [push]
jobs:
  pub:
    runs-on: ubuntu-latest
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
""")
        with mock.patch.object(ci_act, "availability", return_value={"ok": True}), \
             mock.patch.object(ci_act, "run_job",
                               return_value={"exit_code": 0, "output": "ok",
                                             "timed_out": False,
                                             "duration_ms": 1, "command": "act"}):
            res = cr.run_ci(self.tmp, engine="act", allow_side_effects=True)
        self.assertEqual(res.jobs[0].status, cr.PASSED)


class TestVerdictFidelity(ActTempProject):
    def test_container_fidelity_can_reach_a_full_pass(self):
        # A Linux job in a Linux container IS that job — unlike a cross-OS
        # approximation, it must not cap the verdict.
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
""")
        with mock.patch.object(ci_act, "availability", return_value={"ok": True}), \
             mock.patch.object(ci_act, "run_job",
                               return_value={"exit_code": 0, "output": "hi",
                                             "timed_out": False,
                                             "duration_ms": 5, "command": "act"}):
            res = cr.run_ci(self.tmp, engine="act")
        self.assertEqual(res.jobs[0].fidelity, cr.FIDELITY_CONTAINER)
        self.assertEqual(res.verdict, cr.FULL_PASS)

    def test_cross_os_fidelity_cannot(self):
        if host_os() == "Darwin":
            self.skipTest("macos-latest is native on a macOS host")
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  a:
    runs-on: macos-latest
    steps:
      - run: echo hi
""")
        res = cr.run_ci(self.tmp, allow_foreign=True)
        self.assertEqual(res.jobs[0].fidelity, cr.FIDELITY_CROSS_OS)
        self.assertEqual(res.verdict, cr.PARTIAL_PASS)


@unittest.skipUnless(ACT_READY, f"act+Docker unavailable: {ACT_STATE.get('reason')}")
class TestActIntegration(ActTempProject):
    """The one test that really starts a container."""

    def test_runs_a_linux_job_and_reports_a_clean_failure(self):
        workflow(self.tmp, """
name: CI
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: |
          echo "app/thing.py:14:5: F401 unused import"
          exit 1
""")
        subprocess.run(["git", "init", "-q", "."], cwd=self.tmp,
                       capture_output=True)
        res = cr.run_ci(self.tmp, engine="act", event="push")
        job = res.jobs[0]
        self.assertEqual(job.status, cr.FAILED)
        self.assertEqual(job.engine, "act")
        self.assertEqual(job.fidelity, cr.FIDELITY_CONTAINER)
        self.assertEqual(job.parser, "ruff")
        self.assertEqual(job.failures[0]["file"], "app/thing.py")


if __name__ == "__main__":
    unittest.main()
