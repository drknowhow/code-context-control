"""AgentCI — fingerprints, cached result reuse, and the dependency cache.

A wrong cache is worse than no cache: it returns a green for code nobody
checked. So the properties pinned here are mostly about INVALIDATION, not about
hits — a cache that never hits is merely slow, while one that hits when it
should not is the false pass this whole module exists to prevent.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services import ci_cache  # noqa: E402
from services import ci_runner as cr  # noqa: E402

LOCAL_RUNNER = {"Windows": "windows-latest", "Darwin": "macos-latest"}.get(
    platform.system(), "ubuntu-latest")


class CacheBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        (self.tmp / ".github" / "workflows").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "."], cwd=self.tmp,
                       capture_output=True)
        self.workflow()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def workflow(self, body: str = ""):
        (self.tmp / ".github" / "workflows" / "ci.yml").write_text(body or f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo hello
""", encoding="utf-8")

    def rules(self, mapping: dict):
        (self.tmp / ".c3" / "config.json").write_text(
            json.dumps({"ci": {"required_map": mapping}}), encoding="utf-8")


class TestInternalPaths(unittest.TestCase):
    def test_c3_state_is_not_a_job_input(self):
        # Regression: `lstrip("./")` strips a CHARACTER SET, not a prefix, so
        # ".c3/" became "c3/" and the guard silently never matched. C3 then
        # invalidated its own cache on every run by writing .c3/ci/... — a
        # cache that could never hit.
        for path in (".c3", ".c3/", ".c3/ci/index.jsonl", "./.c3/x", ".git/x"):
            self.assertTrue(ci_cache._is_c3_internal(path), path)

    def test_real_source_paths_are_inputs(self):
        for path in ("src/app.py", "c3/thing.py", "docs/.c3notes.md"):
            self.assertFalse(ci_cache._is_c3_internal(path), path)


class TestResultReuse(CacheBase):
    def test_second_identical_run_is_reused(self):
        first = cr.run_ci(self.tmp)
        second = cr.run_ci(self.tmp)
        self.assertEqual(first.jobs[0].status, cr.PASSED)
        self.assertEqual(second.jobs[0].status, cr.CACHED)
        self.assertIn("reused from cache", second.note)

    def test_a_cached_run_is_still_a_full_pass_but_says_so(self):
        # It genuinely passed for these inputs; hiding that it was not
        # re-executed would be the dishonest half.
        cr.run_ci(self.tmp)
        again = cr.run_ci(self.tmp)
        self.assertEqual(again.verdict, cr.FULL_PASS)
        self.assertIn("identical inputs", again.note)

    def test_editing_any_file_invalidates_an_unmapped_job(self):
        cr.run_ci(self.tmp)
        (self.tmp / "unrelated.py").write_text("x", encoding="utf-8")
        self.assertEqual(cr.run_ci(self.tmp).jobs[0].status, cr.PASSED)

    def test_changing_the_job_definition_invalidates(self):
        cr.run_ci(self.tmp)
        self.workflow(f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: echo DIFFERENT
""")
        self.assertEqual(cr.run_ci(self.tmp).jobs[0].status, cr.PASSED)

    def test_no_cache_forces_execution(self):
        cr.run_ci(self.tmp)
        self.assertEqual(cr.run_ci(self.tmp, no_cache=True).jobs[0].status,
                         cr.PASSED)

    def test_a_failed_job_is_never_cached(self):
        self.workflow(f"""
name: CI
on: [push]
jobs:
  a:
    runs-on: {LOCAL_RUNNER}
    steps:
      - run: exit 1
""")
        cr.run_ci(self.tmp)
        again = cr.run_ci(self.tmp)
        self.assertEqual(again.jobs[0].status, cr.FAILED)
        self.assertEqual(again.verdict, cr.FAIL)

    def test_declared_inputs_survive_an_unrelated_edit(self):
        # The payoff of ci.required_map: a job that states what it reads is
        # not invalidated by edits it cannot see.
        self.rules({"a": ["src/**"]})
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "app.py").write_text("v1", encoding="utf-8")
        cr.run_ci(self.tmp)
        (self.tmp / "docs.md").write_text("unrelated", encoding="utf-8")
        self.assertEqual(cr.run_ci(self.tmp).jobs[0].status, cr.CACHED)

    def test_declared_inputs_still_invalidate_when_they_change(self):
        self.rules({"a": ["src/**"]})
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "app.py").write_text("v1", encoding="utf-8")
        cr.run_ci(self.tmp)
        (self.tmp / "src" / "app.py").write_text("v2", encoding="utf-8")
        self.assertEqual(cr.run_ci(self.tmp).jobs[0].status, cr.PASSED)


class TestFingerprint(CacheBase):
    def _inst(self):
        from services.ci_workflow import (
            build_dag,
            discover_workflows,
            parse_workflow,
        )
        dag = build_dag([parse_workflow(p) for p in discover_workflows(self.tmp)])
        return dag.instances[0]

    def test_engine_is_part_of_the_identity(self):
        inst = self._inst()
        native = ci_cache.job_fingerprint(self.tmp, inst, "native", "")
        container = ci_cache.job_fingerprint(self.tmp, inst, "act", "img")
        self.assertNotEqual(native, container)

    def test_same_inputs_give_the_same_fingerprint(self):
        inst = self._inst()
        self.assertEqual(ci_cache.job_fingerprint(self.tmp, inst),
                         ci_cache.job_fingerprint(self.tmp, inst))

    def test_scope_limits_what_invalidates(self):
        inst = self._inst()
        (self.tmp / "src").mkdir()
        (self.tmp / "src" / "a.py").write_text("1", encoding="utf-8")
        before = ci_cache.job_fingerprint(self.tmp, inst, scope=["src/**"])
        (self.tmp / "elsewhere.txt").write_text("noise", encoding="utf-8")
        self.assertEqual(before,
                         ci_cache.job_fingerprint(self.tmp, inst, scope=["src/**"]))
        (self.tmp / "src" / "a.py").write_text("2", encoding="utf-8")
        self.assertNotEqual(before,
                            ci_cache.job_fingerprint(self.tmp, inst, scope=["src/**"]))


class TestDependencyCache(CacheBase):
    def test_save_then_restore_round_trip(self):
        (self.tmp / "deps").mkdir()
        (self.tmp / "deps" / "lib.txt").write_text("payload", encoding="utf-8")
        self.assertTrue(ci_cache.save_dependency(self.tmp, "k1", ["deps"]))
        shutil.rmtree(self.tmp / "deps")
        self.assertTrue(ci_cache.restore_dependency(self.tmp, "k1", ["deps"]))
        self.assertEqual((self.tmp / "deps" / "lib.txt").read_text(encoding="utf-8"),
                         "payload")

    def test_a_key_is_immutable_like_githubs(self):
        (self.tmp / "f.txt").write_text("one", encoding="utf-8")
        ci_cache.save_dependency(self.tmp, "k", ["f.txt"])
        (self.tmp / "f.txt").write_text("two", encoding="utf-8")
        self.assertFalse(ci_cache.save_dependency(self.tmp, "k", ["f.txt"]))

    def test_miss_on_an_unknown_key(self):
        self.assertFalse(ci_cache.restore_dependency(self.tmp, "nope", ["x"]))

    def test_clear_empties_the_store(self):
        cr.run_ci(self.tmp)
        self.assertGreater(ci_cache.stats(self.tmp)["results"], 0)
        ci_cache.clear(self.tmp)
        self.assertEqual(ci_cache.stats(self.tmp)["results"], 0)


if __name__ == "__main__":
    unittest.main()
