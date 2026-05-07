"""Smoke test for `c3 bitbucket status` — verifies the subparser is wired
and the command runs without an active account configured.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
C3_SCRIPT = REPO_ROOT / "cli" / "c3.py"


def _run(args, cwd, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(C3_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONIOENCODING": "utf-8",
            "SENTRY_DSN": "",
            "C3_TELEMETRY_OPT_IN": "0",
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        },
    )


class TestBitbucketCliSmoke(unittest.TestCase):
    def test_help_lists_bitbucket(self):
        proc = _run(["--help"], cwd=REPO_ROOT)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("bitbucket", proc.stdout)

    def test_bitbucket_subhelp_lists_subcommands(self):
        proc = _run(["bitbucket", "--help"], cwd=REPO_ROOT)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = proc.stdout
        for sub in ("login", "logout", "status", "use", "set-default"):
            self.assertIn(sub, out, msg=f"`c3 bitbucket {sub}` missing from help")

    def test_status_in_clean_project_is_graceful(self):
        with tempfile.TemporaryDirectory() as td:
            proc = _run(["bitbucket", "status", td], cwd=td)
            # Must not crash; output should mention bitbucket status header.
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            combined = proc.stdout + proc.stderr
            self.assertIn("bitbucket:status", combined)
            self.assertIn("Active", combined)


if __name__ == "__main__":
    unittest.main()
