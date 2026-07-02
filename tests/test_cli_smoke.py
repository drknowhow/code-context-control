"""Smoke tests for the c3 CLI entry-point.

Verifies the CLI starts up, parses --version / --help, and exposes the
declared subcommands. Subprocesses use sys.executable on cli/c3.py so
the test works without `pip install`.

The runner deliberately avoids subprocess.run(timeout=...): on Windows its
timeout kills only the DIRECT child, and any grandchild that inherited the
stdout/stderr pipe handles keeps communicate() blocked forever afterwards.
That is exactly what hung this suite when bare `c3` spawned the TUI child
(tui/main.py). Instead we use the repo convention: Popen + stdin=DEVNULL +
communicate-with-timeout + taskkill /F /T tree kill — so even a future
regression FAILS fast with a clear message instead of hanging pytest.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
C3_SCRIPT = REPO_ROOT / "cli" / "c3.py"


def _kill_tree(pid: int) -> None:
    """Force-kill *pid* and every descendant.

    Windows: taskkill /F /T (tree kill) — Popen.kill() alone leaves
    grandchildren alive holding the captured pipe handles.
    POSIX: kill the process group (the child is started in its own session).
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )
    else:
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _run(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    popen_kwargs = {}
    if sys.platform != "win32":
        # Own session/process group so _kill_tree can killpg without
        # touching the pytest process group.
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, str(C3_SCRIPT), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
        env={
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONIOENCODING": "utf-8",
            "SENTRY_DSN": "",
            "C3_TELEMETRY_OPT_IN": "0",
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        },
        **popen_kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        # The whole tree is dead now, so the pipe write ends are closed and
        # this drain returns promptly instead of blocking.
        try:
            stdout, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        cmd_repr = " ".join(args) if args else "<no args>"
        raise AssertionError(
            f"`c3 {cmd_repr}` did not exit within {timeout}s; its process "
            f"tree was force-killed. The CLI most likely spawned a child "
            f"(e.g. the TUI) that inherited the test's pipes. "
            f"Partial stdout: {stdout[:300]!r} stderr: {stderr[:300]!r}"
        ) from None
    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


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
        # With piped (non-tty) stdio, bare `c3` must print help and exit —
        # never spawn the TUI child (which would inherit and hold our pipes).
        proc = _run()
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        # Either help text or interactive intro — must not crash.
        self.assertGreater(len(proc.stdout) + len(proc.stderr), 0)

    def test_sub_help(self):
        proc = _run("sub", "--help")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        for token in ("add", "remove", "check", "--parent", "--clear"):
            self.assertIn(token, proc.stdout)

    def test_sub_list_outside_project_is_friendly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            proc = _run("sub", "list", "--parent", td)
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("No .c3 found", proc.stdout)


if __name__ == "__main__":
    unittest.main()
