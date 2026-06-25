"""Unit tests for c3_shell — cli/tools/shell.py.

Covers:
- exit_code propagation (success + non-zero)
- empty / blocked / soft-warn input classification
- timeout triggers hard kill path (platform-aware)
- filter engages past threshold
- activity_log receives shell_exec record
- git mutating command triggers ledger refresh probe
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from cli.tools import shell as shell_mod  # noqa: E402


def _fake_svc(tmp: Path):
    """Minimal svc stub with project_path + log sinks."""
    activity_records: list[tuple[str, dict]] = []
    ledger_records: list[dict] = []

    class _ActivityLog:
        def log(self, kind, payload):
            activity_records.append((kind, payload))

    class _EditLedger:
        def log_edit(self, **kw):
            ledger_records.append(kw)
            return {"id": f"edit_{len(ledger_records)}"}

    svc = SimpleNamespace(
        project_path=str(tmp),
        activity_log=_ActivityLog(),
        edit_ledger=_EditLedger(),
    )
    return svc, activity_records, ledger_records


def _finalize_passthrough(name, args, resp, summ, **kw):
    """Return response unchanged — skip session-mgr plumbing."""
    return resp


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if sys.version_info < (3, 10) else asyncio.run(coro)


class TestShellClassification(unittest.TestCase):
    def test_empty_cmd_is_rejected(self):
        svc, _, _ = _fake_svc(Path.cwd())
        out = _run(shell_mod.handle_shell("", "", 10, True, False, svc, _finalize_passthrough))
        self.assertIn("empty command", out)

    def test_blocked_rm_root_rejected_without_spawn(self):
        svc, _, _ = _fake_svc(Path.cwd())
        with patch.object(shell_mod, "_run_sync") as runner:
            out = _run(shell_mod.handle_shell("rm -rf / ", "", 10, True, False, svc, _finalize_passthrough))
            runner.assert_not_called()
        self.assertIn("blocked pattern", out)

    def test_fork_bomb_rejected(self):
        svc, _, _ = _fake_svc(Path.cwd())
        with patch.object(shell_mod, "_run_sync") as runner:
            out = _run(shell_mod.handle_shell(":(){ :|: };:", "", 10, True, False, svc, _finalize_passthrough))
            runner.assert_not_called()
        self.assertIn("blocked pattern", out)

    def test_blocked_rm_root_glob_rejected(self):
        svc, _, _ = _fake_svc(Path.cwd())
        with patch.object(shell_mod, "_run_sync") as runner:
            out = _run(shell_mod.handle_shell("rm -rf /*", "", 10, True, False, svc, _finalize_passthrough))
            runner.assert_not_called()
        self.assertIn("blocked pattern", out)

    def test_blocked_rm_top_level_system_dir_rejected(self):
        svc, _, _ = _fake_svc(Path.cwd())
        with patch.object(shell_mod, "_run_sync") as runner:
            out = _run(shell_mod.handle_shell("rm -rf /etc", "", 10, True, False, svc, _finalize_passthrough))
            runner.assert_not_called()
        self.assertIn("blocked pattern", out)

    def test_blocked_windows_drive_wipe_rejected(self):
        svc, _, _ = _fake_svc(Path.cwd())
        with patch.object(shell_mod, "_run_sync") as runner:
            out = _run(shell_mod.handle_shell("del /s /q C:\\", "", 10, True, False, svc, _finalize_passthrough))
            runner.assert_not_called()
        self.assertIn("blocked pattern", out)

    def test_nested_path_delete_not_blocked(self):
        # Deleting a nested project path must NOT trip the catastrophic-command
        # guard (only whole-root / top-level-dir / drive wipes are blocked).
        svc, _, _ = _fake_svc(Path.cwd())
        fake = {"exit_code": 0, "stdout": "", "stderr": "",
                "duration_ms": 1, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake) as runner:
            out = _run(shell_mod.handle_shell(
                "rm -rf /home/me/project/build", "", 10, True, False,
                svc, _finalize_passthrough))
            runner.assert_called_once()
        self.assertNotIn("blocked pattern", out)


class TestShellExecution(unittest.TestCase):
    def test_exit_code_success(self):
        svc, _, _ = _fake_svc(Path.cwd())
        fake = {"exit_code": 0, "stdout": "hello\n", "stderr": "", "duration_ms": 5, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake):
            out = _run(shell_mod.handle_shell("echo hello", "", 10, False, False, svc, _finalize_passthrough))
        self.assertIn("[c3_shell:OK]", out)
        self.assertIn("hello", out)

    def test_exit_code_nonzero_reports_fail(self):
        svc, _, _ = _fake_svc(Path.cwd())
        fake = {"exit_code": 7, "stdout": "", "stderr": "boom\n", "duration_ms": 3, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake):
            out = _run(shell_mod.handle_shell("false", "", 10, False, False, svc, _finalize_passthrough))
        self.assertIn("[c3_shell:FAIL(7)]", out)
        self.assertIn("boom", out)

    def test_timeout_reports_timeout_status(self):
        svc, _, _ = _fake_svc(Path.cwd())
        fake = {"exit_code": -1, "stdout": "", "stderr": "", "duration_ms": 100, "timed_out": True}
        with patch.object(shell_mod, "_run_sync", return_value=fake):
            out = _run(shell_mod.handle_shell("sleep 999", "", 1, False, False, svc, _finalize_passthrough))
        self.assertIn("[c3_shell:TIMEOUT]", out)

    def test_soft_warn_prepended_but_cmd_runs(self):
        svc, _, _ = _fake_svc(Path.cwd())
        fake = {"exit_code": 0, "stdout": "done\n", "stderr": "", "duration_ms": 1, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake):
            out = _run(shell_mod.handle_shell("git push --force", "", 10, False, False, svc, _finalize_passthrough))
        self.assertIn("[c3_shell:warn]", out)
        self.assertIn("[c3_shell:OK]", out)


class TestShellFilter(unittest.TestCase):
    def test_filter_engages_past_threshold(self):
        svc, _, _ = _fake_svc(Path.cwd())
        long_stdout = "\n".join(f"line {i}" for i in range(100)) + "\n"
        fake = {"exit_code": 0, "stdout": long_stdout, "stderr": "", "duration_ms": 1, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake), \
             patch.object(shell_mod, "handle_filter", return_value="[filtered]") as filt:
            out = _run(shell_mod.handle_shell("yes | head -100", "", 10, True, False, svc, _finalize_passthrough))
            filt.assert_called_once()
        self.assertIn("[stdout filtered]", out)
        self.assertIn("[filtered]", out)

    def test_filter_skipped_when_short(self):
        svc, _, _ = _fake_svc(Path.cwd())
        fake = {"exit_code": 0, "stdout": "short\n", "stderr": "", "duration_ms": 1, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake), \
             patch.object(shell_mod, "handle_filter") as filt:
            out = _run(shell_mod.handle_shell("echo short", "", 10, True, False, svc, _finalize_passthrough))
            filt.assert_not_called()
        self.assertNotIn("[stdout filtered]", out)


class TestShellLogging(unittest.TestCase):
    def test_activity_log_captures_shell_exec(self):
        svc, activity, _ = _fake_svc(Path.cwd())
        fake = {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 42, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake):
            _run(shell_mod.handle_shell("echo x", "", 10, False, True, svc, _finalize_passthrough))
        kinds = [k for k, _ in activity]
        self.assertIn("shell_exec", kinds)
        payload = next(p for k, p in activity if k == "shell_exec")
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["duration_ms"], 42)

    def test_git_mutating_triggers_ledger_probe(self):
        svc, _, ledger = _fake_svc(Path.cwd())
        commit_result = {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 5, "timed_out": False}
        probe_result = {"exit_code": 0, "stdout": "a.py\nb.py\n", "stderr": "", "duration_ms": 2, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", side_effect=[commit_result, probe_result]):
            _run(shell_mod.handle_shell("git commit -m msg", "", 10, False, True, svc, _finalize_passthrough))
        self.assertEqual(len(ledger), 2)
        self.assertEqual({e["file"] for e in ledger}, {"a.py", "b.py"})
        self.assertTrue(all(e["change_type"] == "shell_git" for e in ledger))

    def test_non_git_command_skips_ledger(self):
        svc, _, ledger = _fake_svc(Path.cwd())
        fake = {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake):
            _run(shell_mod.handle_shell("echo hello", "", 10, False, True, svc, _finalize_passthrough))
        self.assertEqual(ledger, [])

    def test_failed_git_command_skips_ledger(self):
        svc, _, ledger = _fake_svc(Path.cwd())
        fake = {"exit_code": 1, "stdout": "", "stderr": "nope\n", "duration_ms": 1, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake):
            _run(shell_mod.handle_shell("git commit -m x", "", 10, False, True, svc, _finalize_passthrough))
        self.assertEqual(ledger, [])


class TestShellRealSubprocess(unittest.TestCase):
    """One smoke test hitting the real subprocess path (cheap, portable)."""

    def test_real_echo_returns_exit_0_and_stdout(self):
        svc, _, _ = _fake_svc(Path.cwd())
        cmd = "echo c3_smoke" if sys.platform != "win32" else "cmd /c echo c3_smoke"
        out = _run(shell_mod.handle_shell(cmd, "", 10, False, False, svc, _finalize_passthrough))
        self.assertIn("[c3_shell:OK]", out)
        self.assertIn("c3_smoke", out)


class _FakePopen:
    """Captures Popen construction args; emulates a clean exit."""

    last_args: tuple = ()
    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        _FakePopen.last_args = args
        _FakePopen.last_kwargs = kwargs
        self.returncode = 0

    def communicate(self, timeout=None):
        return ("out\n", "")


class TestShellSelection(unittest.TestCase):
    """Windows Git Bash discovery/selection and _run_sync shell routing."""

    def setUp(self):
        shell_mod._bash_cache.clear()
        os.environ.pop("C3_SHELL_BASH", None)

    def tearDown(self):
        shell_mod._bash_cache.clear()
        os.environ.pop("C3_SHELL_BASH", None)

    def test_non_win32_returns_none(self):
        with patch.object(shell_mod.sys, "platform", "linux"):
            self.assertIsNone(shell_mod._select_bash())

    def test_env_override_zero_forces_default_shell(self):
        os.environ["C3_SHELL_BASH"] = "0"
        with patch.object(shell_mod.sys, "platform", "win32"):
            self.assertIsNone(shell_mod._select_bash())

    def test_env_override_explicit_path_honored(self):
        os.environ["C3_SHELL_BASH"] = __file__  # an existing file stands in for bash
        with patch.object(shell_mod.sys, "platform", "win32"):
            self.assertEqual(shell_mod._select_bash(), __file__)

    def test_discovers_git_bash_from_program_files(self):
        base = r"C:\PF"
        expected = os.path.join(base, "Git", "bin", "bash.exe")
        with patch.dict(os.environ, {"ProgramFiles": base}, clear=False), \
                patch.object(shell_mod.os.path, "isfile", lambda p: p == expected):
            self.assertEqual(shell_mod._discover_git_bash(), expected)

    def test_rejects_wsl_system32_bash(self):
        with patch.object(shell_mod.os.path, "isfile", lambda p: False), \
                patch.object(shell_mod.shutil, "which",
                             return_value=r"C:\Windows\System32\bash.exe"):
            self.assertIsNone(shell_mod._discover_git_bash())

    def test_accepts_non_system_path_bash_from_which(self):
        with patch.object(shell_mod.os.path, "isfile", lambda p: False), \
                patch.object(shell_mod.shutil, "which",
                             return_value=r"C:\tools\msys\bash.exe"):
            self.assertEqual(shell_mod._discover_git_bash(), r"C:\tools\msys\bash.exe")

    def test_run_sync_uses_bash_argv_when_selected(self):
        with patch.object(shell_mod, "_select_bash", return_value="FAKEBASH"), \
                patch.object(shell_mod.subprocess, "Popen", _FakePopen):
            shell_mod._run_sync("echo hi", ".", 5)
        self.assertEqual(_FakePopen.last_args[0], ["FAKEBASH", "-c", "echo hi"])
        self.assertIs(_FakePopen.last_kwargs["shell"], False)

    def test_run_sync_falls_back_to_default_shell_string(self):
        with patch.object(shell_mod, "_select_bash", return_value=None), \
                patch.object(shell_mod.subprocess, "Popen", _FakePopen):
            shell_mod._run_sync("echo hi", ".", 5)
        self.assertEqual(_FakePopen.last_args[0], "echo hi")
        self.assertIs(_FakePopen.last_kwargs["shell"], True)


if __name__ == "__main__":
    unittest.main()
