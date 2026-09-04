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
import shutil
import subprocess
import sys
import tempfile
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


class TestTransportCeiling(unittest.TestCase):
    """A timeout this process cannot honour must not be silently accepted.

    An MCP client kills a tool call at ``MCP_TOOL_TIMEOUT`` — a limit c3 does
    not choose and cannot raise. Before this, ``timeout=600`` was accepted,
    clamped to 600, and killed by the client at 120s with our own deadline
    never arriving: the caller saw the call "moved to background" and then
    fail, with nothing naming the real limit. It cost two paid Higgsfield
    generations, one of which was billed and stranded because the download
    died at the ceiling.
    """

    def _ceiling(self, value):
        return patch.dict(os.environ, {"MCP_TOOL_TIMEOUT": value}, clear=False)

    # ---------------------------------------------------------- discovery

    def test_ms_are_converted_to_seconds(self):
        with self._ceiling("120000"):
            self.assertEqual(shell_mod._transport_ceiling_s(), 120)

    def test_an_unset_variable_caps_nothing(self):
        """A client with no such limit must keep the old behaviour exactly —
        guessing a ceiling here would cap legitimate long work."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_TOOL_TIMEOUT", None)
            self.assertIsNone(shell_mod._transport_ceiling_s())

    def test_a_garbage_value_caps_nothing(self):
        for raw in ("", "  ", "soon", "12x", "-5"):
            with self.subTest(raw=raw), self._ceiling(raw):
                self.assertIsNone(shell_mod._transport_ceiling_s())

    # ------------------------------------------------------------ capping

    def _capture_timeout(self, requested, ceiling):
        """Run handle_shell with _run_sync stubbed; return (timeout, output)."""
        svc, _, _ = _fake_svc(Path.cwd())
        seen = {}

        def _fake_run(cmd, cwd, timeout, env=None):
            seen["timeout"] = timeout
            return {"stdout": "", "stderr": "", "exit_code": 0,
                    "duration_ms": 1, "timed_out": False}

        with self._ceiling(ceiling), patch.object(shell_mod, "_run_sync", _fake_run):
            out = _run(shell_mod.handle_shell(
                "echo hi", "", requested, False, False, svc,
                _finalize_passthrough))
        return seen.get("timeout"), out

    def test_a_request_over_the_ceiling_runs_just_inside_it(self):
        """Inside, not at: OUR deadline has to fire first, or the client's
        kill leaves a subprocess we never reaped and a result nobody sees."""
        timeout, out = self._capture_timeout(600, "120000")
        self.assertEqual(timeout, 120 - shell_mod._TRANSPORT_MARGIN_S)
        self.assertIn("[c3_shell:capped]", out)
        self.assertIn("600s was requested", out)
        self.assertIn("run_in_background", out)   # names the escape hatch

    def test_a_request_inside_the_ceiling_is_untouched_and_silent(self):
        timeout, out = self._capture_timeout(30, "120000")
        self.assertEqual(timeout, 30)
        self.assertNotIn("capped", out)

    def test_no_ceiling_means_no_cap_and_no_note(self):
        svc, _, _ = _fake_svc(Path.cwd())
        seen = {}

        def _fake_run(cmd, cwd, timeout, env=None):
            seen["timeout"] = timeout
            return {"stdout": "", "stderr": "", "exit_code": 0,
                    "duration_ms": 1, "timed_out": False}

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_TOOL_TIMEOUT", None)
            with patch.object(shell_mod, "_run_sync", _fake_run):
                out = _run(shell_mod.handle_shell(
                    "echo hi", "", 600, False, False, svc,
                    _finalize_passthrough))
        self.assertEqual(seen["timeout"], 600)
        self.assertNotIn("capped", out)

    def test_the_documented_max_still_bounds_a_capless_client(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MCP_TOOL_TIMEOUT", None)
            svc, _, _ = _fake_svc(Path.cwd())
            seen = {}

            def _fake_run(cmd, cwd, t, env=None):
                seen["timeout"] = t
                return {"stdout": "", "stderr": "", "exit_code": 0,
                        "duration_ms": 1, "timed_out": False}

            with patch.object(shell_mod, "_run_sync", _fake_run):
                _run(shell_mod.handle_shell(
                    "echo hi", "", 99999, False, False, svc,
                    _finalize_passthrough))
        self.assertEqual(seen["timeout"], shell_mod._MAX_TIMEOUT)


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
        before = {
            "is_repo": True, "head": "a" * 40,
            "entries": {"a.py": "M "}, "files": {"a.py"},
        }
        after = {
            "is_repo": True, "head": "b" * 40,
            "entries": {}, "files": set(),
        }
        with patch.object(shell_mod, "_run_sync", return_value=commit_result), \
             patch.object(shell_mod, "_capture_git_state", side_effect=[before, after]), \
             patch.object(shell_mod, "_git_head_diff", return_value={"a.py", "b.py"}):
            _run(shell_mod.handle_shell("git commit -m msg", "", 10, False, True, svc, _finalize_passthrough))
        self.assertEqual(len(ledger), 2)
        self.assertEqual({e["file"] for e in ledger}, {"a.py", "b.py"})
        self.assertTrue(all(e["change_type"] == "shell_git" for e in ledger))

    def test_git_add_logs_worktree_snapshot_without_head_change(self):
        svc, _, ledger = _fake_svc(Path.cwd())
        result = {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1, "timed_out": False}
        before = {
            "is_repo": True, "head": "a" * 40,
            "entries": {"new.txt": "??", "other.py": " M"},
            "files": {"new.txt", "other.py"},
        }
        after = {
            "is_repo": True, "head": "a" * 40,
            "entries": {"new.txt": "A ", "other.py": " M"},
            "files": {"new.txt", "other.py"},
        }
        with patch.object(shell_mod, "_capture_git_state", return_value=after), \
             patch.object(shell_mod, "_git_head_diff", return_value=set()):
            files = shell_mod._maybe_refresh_ledger(
                "git add new.txt", result, svc, before=before)
        self.assertEqual(files, ["new.txt"])
        self.assertEqual([entry["file"] for entry in ledger], ["new.txt"])

    def test_chained_git_mutation_is_detected(self):
        self.assertIsNotNone(shell_mod._GIT_MUTATING.search("cd src && git add app.py"))

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_real_git_add_logs_only_changed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=root, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8",
                )

            git("init", "-q")
            git("config", "user.email", "c3@example.invalid")
            git("config", "user.name", "C3 Test")
            (root / "base.txt").write_text("base\n", encoding="utf-8")
            (root / "other.py").write_text("before\n", encoding="utf-8")
            git("add", "base.txt", "other.py")
            git("commit", "-qm", "base")
            (root / "other.py").write_text("dirty\n", encoding="utf-8")
            (root / "new.txt").write_text("new\n", encoding="utf-8")

            before = shell_mod._capture_git_state(str(root))
            git("add", "new.txt")
            svc, _, ledger = _fake_svc(root)
            result = {
                "exit_code": 0, "stdout": "", "stderr": "",
                "duration_ms": 1, "timed_out": False,
            }
            files = shell_mod._maybe_refresh_ledger(
                "git add new.txt", result, svc, before=before, cwd=str(root))

        self.assertEqual(files, ["new.txt"])
        self.assertEqual([entry["file"] for entry in ledger], ["new.txt"])

    def test_non_git_command_skips_ledger(self):
        svc, _, ledger = _fake_svc(Path.cwd())
        fake = {"exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1, "timed_out": False}
        with patch.object(shell_mod, "_run_sync", return_value=fake):
            _run(shell_mod.handle_shell("echo hello", "", 10, False, True, svc, _finalize_passthrough))
        self.assertEqual(ledger, [])

    def test_failed_git_command_skips_ledger(self):
        svc, _, ledger = _fake_svc(Path.cwd())
        fake = {"exit_code": 1, "stdout": "", "stderr": "nope\n", "duration_ms": 1, "timed_out": False}
        state = {
            "is_repo": True, "head": "a" * 40,
            "entries": {}, "files": set(),
        }
        with patch.object(shell_mod, "_run_sync", return_value=fake), \
             patch.object(shell_mod, "_capture_git_state", return_value=state):
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
    """Captures Popen construction args; emulates a clean exit with binary pipes
    (since 2.112.0 _run_sync streams them through ShellCapture)."""

    last_args: tuple = ()
    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        import io
        _FakePopen.last_args = args
        _FakePopen.last_kwargs = kwargs
        self.returncode = 0
        self.stdout = io.BytesIO(b"out\n")
        self.stderr = io.BytesIO(b"")

    def communicate(self, timeout=None):
        return ("out\n", "")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def kill(self):
        pass


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

    def test_posix_subprocess_starts_new_session(self):
        with patch.object(shell_mod.sys, "platform", "linux"), \
                patch.object(shell_mod.subprocess, "Popen", _FakePopen):
            shell_mod._run_sync("echo hi", ".", 5)
        self.assertIs(_FakePopen.last_kwargs["start_new_session"], True)

    def test_spawn_failure_returns_structured_result(self):
        with patch.object(shell_mod, "_select_bash", return_value=None), \
                patch.object(shell_mod.subprocess, "Popen",
                             side_effect=NotADirectoryError("bad cwd")):
            result = shell_mod._run_sync("echo hi", "missing", 5)
        self.assertEqual(result["exit_code"], 126)
        self.assertIn("NotADirectoryError", result["stderr"])
        self.assertFalse(result["timed_out"])

    def test_missing_jq_gets_portable_hint(self):
        result = {
            "exit_code": 127,
            "stderr": "bash: jq: command not found",
            "shell": "git-bash",
        }
        hint = shell_mod._dependency_hint("curl http://localhost | jq .", result)
        self.assertIn("python -m json.tool", hint)


class TestShellWarnGrants(unittest.TestCase):
    """shell_warn grant wiring (docs/confirm-guard.md §7): an approved grant
    suppresses the soft-warn exactly once; `_BLOCKED` never consults grants."""

    def setUp(self):
        import json
        from unittest import mock

        from services import override_policy as opol
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name).resolve()
        (self.proj / ".c3").mkdir()
        (self.proj / ".c3" / "config.json").write_text(json.dumps({
            "override": {"enabled": True,
                         "layers": {k: True for k in opol.LAYER_KEYS}},
        }), encoding="utf-8")
        self._patch = mock.patch.object(opol, "_global_base",
                                        return_value=None)
        self._patch.start()
        self.svc, _, _ = _fake_svc(self.proj)
        self.session = f"pid-{os.getpid()}"  # _grants.session_id fallback
        self.fake = {"exit_code": 0, "stdout": "done\n", "stderr": "",
                     "duration_ms": 1, "timed_out": False}

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _mint(self, path=None):
        from services import override_grants as og
        from services import override_policy as opol
        return og.mint(str(self.proj), session_id=self.session,
                       layer=opol.GATE_SHELL, rule=opol.RULE_SHELL_WARN,
                       tool="c3_shell", op="run",
                       path=str(path or self.proj))

    def _shell(self, cmd, cwd=""):
        with patch.object(shell_mod, "_run_sync", return_value=dict(self.fake)):
            return _run(shell_mod.handle_shell(
                cmd, cwd, 10, False, False, self.svc, _finalize_passthrough))

    def test_grant_suppresses_the_warn_exactly_once(self):
        self._mint()
        out = self._shell("git push --force")
        self.assertNotIn("[c3_shell:warn]", out)
        self.assertIn("[c3-override:granted]", out)
        # Single-use: the next run warns again.
        out = self._shell("git push --force")
        self.assertIn("[c3_shell:warn]", out)
        self.assertNotIn("[c3-override:granted]", out)

    def test_wrong_cwd_grant_keeps_the_warn(self):
        other = self.proj / "sub"
        other.mkdir()
        self._mint(path=other)  # grant bound to a different cwd
        out = self._shell("git push --force")  # runs in project root
        self.assertIn("[c3_shell:warn]", out)

    def test_layer_off_voids_a_live_grant(self):
        import json
        self._mint()
        (self.proj / ".c3" / "config.json").write_text(json.dumps({
            "override": {"enabled": True, "layers": {"shell_warn": False}},
        }), encoding="utf-8")
        out = self._shell("git push --force")
        self.assertIn("[c3_shell:warn]", out)

    def test_blocked_never_consults_grants(self):
        self._mint()
        with patch.object(shell_mod, "_run_sync") as runner:
            out = _run(shell_mod.handle_shell(
                "rm -rf /", "", 10, False, False, self.svc,
                _finalize_passthrough))
            runner.assert_not_called()
        self.assertIn("blocked pattern", out)


if __name__ == "__main__":
    unittest.main()
