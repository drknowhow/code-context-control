"""C3 pins OpenBLAS to one thread for itself and hands users their own setting.

numpy's bundled OpenBLAS starts a worker per core and commits a buffer for
each as soon as numpy loads: 652 MB of commit charge and 19 threads per MCP
or UI server on a 20-core machine, for BLAS work C3 never does. The limit
has to be in the environment before numpy is imported, and commands C3 runs
for the user (c3_shell, shell jobs, local CI) must not inherit it.
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from core import blas_threads

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestLimit(unittest.TestCase):
    def test_sets_one_thread_and_marks_it(self):
        with mock.patch.dict(os.environ, clear=True):
            blas_threads.limit()
            self.assertEqual(os.environ["OPENBLAS_NUM_THREADS"], "1")
            self.assertEqual(os.environ[blas_threads._MARK], "1")

    def test_keeps_a_value_already_set(self):
        with mock.patch.dict(os.environ, {"OPENBLAS_NUM_THREADS": "8"}, clear=True):
            blas_threads.limit()
            self.assertEqual(os.environ["OPENBLAS_NUM_THREADS"], "8")
            self.assertNotIn(blas_threads._MARK, os.environ)


class TestUserEnv(unittest.TestCase):
    def test_drops_the_limit_c3_added(self):
        env = {"OPENBLAS_NUM_THREADS": "1", blas_threads._MARK: "1", "PATH": "x"}
        self.assertEqual(blas_threads.user_env(env), {"PATH": "x"})

    def test_keeps_a_users_own_value(self):
        env = {"OPENBLAS_NUM_THREADS": "1", "PATH": "x"}
        self.assertEqual(blas_threads.user_env(dict(env)), env)

    def test_keeps_a_value_changed_after_c3_set_it(self):
        env = {"OPENBLAS_NUM_THREADS": "4", blas_threads._MARK: "1"}
        self.assertEqual(blas_threads.user_env(env), {"OPENBLAS_NUM_THREADS": "4"})

    def test_c3_shell_children_get_the_users_setting(self):
        from cli.tools import shell
        with mock.patch.dict(os.environ, {"OPENBLAS_NUM_THREADS": "1",
                                          blas_threads._MARK: "1"}):
            env = shell._popen_kwargs({"EXTRA": "1"})["env"]
        self.assertNotIn("OPENBLAS_NUM_THREADS", env)
        self.assertNotIn(blas_threads._MARK, env)
        self.assertEqual(env["EXTRA"], "1")


class TestEntryPointsLimitBeforeNumpy(unittest.TestCase):
    """Only the standard library may be imported above the limit() call."""

    def test_entry_points(self):
        for rel in ("cli/c3.py", "cli/mcp_server.py"):
            tree = ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))
            before = []
            for node in tree.body:
                if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                        and ast.unparse(node.value.func) == "blas_threads.limit"):
                    break
                if isinstance(node, ast.Import):
                    before += [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    before.append(node.module or "")
            else:
                self.fail(f"{rel} never calls blas_threads.limit()")
            outside = [m for m in before if m.split(".")[0] not in sys.stdlib_module_names
                       and m not in ("__future__", "core")]
            self.assertEqual(outside, [], rel)


if __name__ == "__main__":
    unittest.main()
