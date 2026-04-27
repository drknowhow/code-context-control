"""Smoke tests for the c3 CLI entry-point.

Verifies the CLI starts up, parses --version / --help, and exposes the
declared subcommands. Subprocesses use sys.executable on cli/c3.py so
the test works without `pip install`.
"""
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
C3_SCRIPT = REPO_ROOT / "cli" / "c3.py"


def _run(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(C3_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=REPO_ROOT,
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONIOENCODING": "utf-8",
            "SENTRY_DSN": "",
            "C3_TELEMETRY_OPT_IN": "0",
            "PATH": __import__("os").environ.get("PATH", ""),
            "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        },
    )


class TestCliSmoke(unittest.TestCase):
    def test_version_flag_prints_semver(self):
        proc = _run("--version")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # Version should appear in stdout (argparse default for --version)
        # Format is typically "c3 X.Y.Z" or just "X.Y.Z"
        out = (proc.stdout + proc.stderr).strip()
        self.assertRegex(out, r"\d+\.\d+\.\d+", msg=f"no semver in output: {out!r}")

    def test_help_lists_core_subcommands(self):
        proc = _run("--help")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        out = proc.stdout
        for cmd in ("init", "install-mcp", "ui", "hub", "stats"):
            self.assertIn(cmd, out, msg=f"subcommand {cmd!r} missing from --help")

    def test_no_args_prints_help_and_exits_zero(self):
        proc = _run()
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # Either help text or interactive intro — must not crash.
        self.assertGreater(len(proc.stdout) + len(proc.stderr), 0)


if __name__ == "__main__":
    unittest.main()
