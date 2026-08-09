"""Access Guard T2c surface tests — c3_shell, c3_project, scanner, c3_delegate.

Mirrors tests/test_access_guard.py GuardBase fixtures (tmp project + .c3 +
_write_access), plus global-scope isolation so a developer's real ~/.c3
rules cannot leak in. The evaluator itself is covered by test_access_guard;
this file covers the T2c enforcement wiring:

- handle_shell: hard cwd deny, ADVISORY token scan (relative, absolute, and
  MSYS spellings), post-credential-expansion scan, denial activity logging.
- handle_project: S5 proxy refusal via target-realm rules; unregistered
  directories refused beyond discovery actions.
- scanner.iter_files: index-time exclusion of denied paths (fail closed on
  corrupt config).
- handle_delegate: byte-identical no-rules behavior; codex sandbox pin and
  write-capable backend refusal when rules are active.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli.tools import delegate as delegate_mod  # noqa: E402
from cli.tools import project as project_mod  # noqa: E402
from cli.tools import shell as shell_mod  # noqa: E402
from services import access_guard as ag  # noqa: E402
from services.scanner import iter_files  # noqa: E402

WIN = os.name == "nt"

_OK_RESULT = {"exit_code": 0, "stdout": "ok", "stderr": "",
              "duration_ms": 1, "timed_out": False, "shell": "sh"}


def _finalize(name, args, resp, summ, **kw):
    return resp


def _run(coro):
    return asyncio.run(coro)


def _fake_svc(tmp: Path):
    """Minimal svc stub with project_path + log sinks (mirrors test_c3_shell)."""
    activity: list[tuple[str, dict]] = []

    class _ActivityLog:
        def log(self, kind, payload):
            activity.append((kind, payload))

    svc = SimpleNamespace(
        project_path=str(tmp),
        activity_log=_ActivityLog(),
        edit_ledger=None,
        _agent_progress_cb=None,
    )
    return svc, activity


class GuardBase(unittest.TestCase):
    """Mirror of tests/test_access_guard.py GuardBase + global isolation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        self._gb = mock.patch.object(ag, "_global_base", return_value=None)
        self._gb.start()
        # delegate's response cache is a module global that outlives a test.
        # The codex cases below key their task on id(svc), and CPython reuses
        # an address once the previous svc is collected — so the second test
        # could hit the FIRST one's cached "CODEX-OUT", return without ever
        # calling the mocked _run_codex, and assert against an empty capture
        # (`sandbox` is None, not the pinned value). That is allocator luck,
        # which is why it only ever failed on some runners.
        delegate_mod._delegate_cache.clear()

    def tearDown(self):
        self._gb.stop()
        self._tmp.cleanup()

    def _write_access(self, section, base=None):
        cfg = (base or self.proj) / ".c3" / "config.json"
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text(json.dumps({"access": section}), encoding="utf-8")


# ── c3_shell: hard cwd deny ────────────────────────────────────────────────


class TestShellCwdDeny(GuardBase):
    def test_denied_cwd_refuses_without_spawn(self):
        self._write_access({"deny": ["secrets/**"]})
        (self.proj / "secrets").mkdir()
        svc, activity = _fake_svc(self.proj)
        with mock.patch.object(shell_mod, "_run_sync",
                               side_effect=AssertionError("must not spawn")):
            out = _run(shell_mod.handle_shell(
                "echo hi", str(self.proj / "secrets"), 10, True, True,
                svc, _finalize, enable_creds=False))
        self.assertIn("[c3-access:denied]", out)
        self.assertIn("'secrets/**'", out)
        denied = [p for k, p in activity if k == "access_denied"]
        self.assertEqual(len(denied), 1)
        self.assertTrue(denied[0]["cwd"].casefold().endswith("secrets"))

    def test_read_only_cwd_still_runs_and_logs_cwd(self):
        self._write_access({"read_only": ["docs/**"]})
        (self.proj / "docs").mkdir()
        svc, activity = _fake_svc(self.proj)
        with mock.patch.object(shell_mod, "_run_sync",
                               return_value=dict(_OK_RESULT)):
            out = _run(shell_mod.handle_shell(
                "echo hi", str(self.proj / "docs"), 10, True, True,
                svc, _finalize, enable_creds=False))
        self.assertIn("[c3_shell:OK]", out)
        execs = [p for k, p in activity if k == "shell_exec"]
        self.assertEqual(len(execs), 1)
        self.assertTrue(execs[0]["cwd"])  # effective cwd always logged


# ── c3_shell: advisory token scan ──────────────────────────────────────────


class TestShellTokenScan(GuardBase):
    def setUp(self):
        super().setUp()
        self._write_access({"deny": ["secrets/**"]})
        self.svc, self.activity = _fake_svc(self.proj)

    def _shell(self, cmd):
        with mock.patch.object(shell_mod, "_run_sync",
                               return_value=dict(_OK_RESULT)):
            return _run(shell_mod.handle_shell(
                cmd, str(self.proj), 10, True, True, self.svc, _finalize,
                enable_creds=False))

    def test_relative_denied_token_refused(self):
        out = self._shell("cat secrets/key.txt")
        self.assertIn("[c3-access:denied]", out)
        self.assertIn("best-effort", out)  # never claims enforcement

    def test_absolute_denied_token_refused(self):
        p = str(self.proj / "secrets" / "key.txt").replace("\\", "/")
        out = self._shell(f"cat {p}")
        self.assertIn("[c3-access:denied]", out)

    @unittest.skipUnless(WIN, "MSYS drive spellings are Windows-only")
    def test_msys_spelling_refused(self):
        p = str(self.proj / "secrets" / "key.txt").replace("\\", "/")
        msys = "/" + p[0].lower() + p[2:]  # C:/x → /c/x
        out = self._shell(f"cat {msys}")
        self.assertIn("[c3-access:denied]", out)

    def test_benign_command_runs(self):
        out = self._shell("echo hello")
        self.assertIn("[c3_shell:OK]", out)

    def test_scan_denial_logged_with_cwd(self):
        self._shell("cat secrets/key.txt")
        denied = [p for k, p in self.activity if k == "access_denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["surface"], "token_scan")
        self.assertTrue(denied[0]["cwd"])


# ── c3_shell: post-credential-expansion scan ───────────────────────────────


class TestShellPostExpansionScan(GuardBase):
    """{{cred:NAME}} commands are scanned before AND after expansion."""

    def setUp(self):
        super().setUp()
        self._write_access({"deny": ["secrets/**"]})
        self.svc, _ = _fake_svc(self.proj)

    def _shell_with_creds(self, cmd, expanded, used):
        from services import credential_store as creds
        patches = [
            mock.patch.object(creds, "expand_templates",
                              return_value=(expanded, used, [])),
            mock.patch.object(creds, "list_entries", return_value={}),
            mock.patch.object(creds, "resolve", return_value=({}, [])),
            mock.patch.object(creds, "redact_text", side_effect=lambda t: t),
            mock.patch.object(creds, "touch_last_used", return_value=None),
            mock.patch.object(shell_mod, "_run_sync",
                              side_effect=AssertionError("must not spawn")),
        ]
        for p in patches:
            p.start()
        try:
            return _run(shell_mod.handle_shell(
                cmd, str(self.proj), 10, True, True, self.svc, _finalize))
        finally:
            for p in patches:
                p.stop()

    def test_template_command_still_scanned(self):
        # A benign expansion must not exempt the command from scanning.
        out = self._shell_with_creds(
            "deploy --token={{cred:TOK}} secrets/key.txt",
            "deploy --token=benignvalue secrets/key.txt", ["TOK"])
        self.assertIn("[c3-access:denied]", out)

    def test_expansion_introduced_denied_path_refused(self):
        # The raw template is clean; only the POST-expansion string contains
        # the denied path — proving the expanded command is scanned.
        out = self._shell_with_creds(
            "cat {{cred:PATHY}}", "cat secrets/key.txt", ["PATHY"])
        self.assertIn("[c3-access:denied]", out)


# ── c3_project: proxy guard + registration gate ────────────────────────────


class TestProjectProxy(GuardBase):
    def setUp(self):
        super().setUp()
        self._tmp2 = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp2.name)
        (self.target / ".c3").mkdir()
        (self.target / "secrets").mkdir()
        (self.target / "secrets" / "k.txt").write_text("x", encoding="utf-8")
        self._write_access({"deny": ["secrets/**"]}, base=self.target)
        self.svc, _ = _fake_svc(self.proj)

    def tearDown(self):
        self._tmp2.cleanup()
        super().tearDown()

    def _registry(self, entries):
        return mock.patch("services.project_runtime._read_registry",
                          return_value=entries)

    def test_proxied_read_of_denied_file_gets_s5(self):
        reg = [{"name": "tgt", "path": str(self.target)}]
        with self._registry(reg), \
                mock.patch.object(project_mod, "_runtime_for",
                                  side_effect=AssertionError("no runtime")):
            out = project_mod.handle_project(
                "read", self.svc, _finalize, project=str(self.target),
                file_path="secrets/k.txt")
        self.assertIn("[c3-access:denied]", out)
        self.assertIn("through project 'tgt'", out)
        self.assertIn("'secrets/**'", out)

    def test_unregistered_directory_refused(self):
        with self._registry([]), \
                mock.patch.object(project_mod, "_runtime_for",
                                  side_effect=AssertionError("no runtime")):
            out = project_mod.handle_project(
                "read", self.svc, _finalize, project=str(self.target),
                file_path="README.md")
        self.assertIn("not a registered project", out)
        self.assertIn("register", out)

    def test_registered_clean_file_reaches_runtime(self):
        reg = [{"name": "tgt", "path": str(self.target)}]
        with self._registry(reg), \
                mock.patch.object(project_mod, "_runtime_for",
                                  side_effect=RuntimeError("sentinel")):
            out = project_mod.handle_project(
                "read", self.svc, _finalize, project=str(self.target),
                file_path="ok.txt")
        self.assertIn("sentinel", out)  # guard passed; runtime was borrowed


# ── scanner.iter_files: index-time exclusion ───────────────────────────────


class TestScannerExclusion(GuardBase):
    def setUp(self):
        super().setUp()
        (self.proj / "src").mkdir()
        (self.proj / "src" / "a.py").write_text("x=1", encoding="utf-8")
        (self.proj / "secrets").mkdir()
        (self.proj / "secrets" / "b.py").write_text("y=2", encoding="utf-8")

    def test_no_rules_yields_everything(self):
        names = {p.name for p in iter_files(self.proj, exts={".py"})}
        self.assertEqual(names, {"a.py", "b.py"})

    def test_denied_paths_never_enter_walk(self):
        self._write_access({"deny": ["secrets/**"]})
        names = {p.name for p in iter_files(self.proj, exts={".py"})}
        self.assertEqual(names, {"a.py"})

    def test_basename_deny_excluded(self):
        self._write_access({"deny": ["*.pem"]})
        (self.proj / "src" / "k.pem").write_text("p", encoding="utf-8")
        found = {p.name for p in iter_files(self.proj)}
        self.assertNotIn("k.pem", found)
        self.assertIn("a.py", found)

    def test_corrupt_config_excludes_everything(self):
        (self.proj / ".c3" / "config.json").write_text("{nope",
                                                       encoding="utf-8")
        self.assertEqual(list(iter_files(self.proj, exts={".py"})), [])


# ── c3_delegate: guard posture ─────────────────────────────────────────────


class TestDelegateGuard(GuardBase):
    def _svc(self, dcfg):
        svc, _ = _fake_svc(self.proj)
        svc.delegate_config = dcfg
        svc.ollama_client = None
        svc.compressor = None
        return svc

    def test_no_rules_gemini_path_unchanged(self):
        svc = self._svc({"enabled": True})
        with mock.patch.object(delegate_mod, "_handle_gemini_delegate",
                               return_value="GEMINI-SENTINEL") as h:
            out = delegate_mod.handle_delegate(
                "t-norules-gem", "ask", "", "", svc, _finalize,
                backend="gemini")
        self.assertEqual(out, "GEMINI-SENTINEL")
        h.assert_called_once()

    def test_rules_active_gemini_refused_without_opt_in(self):
        self._write_access({"deny": ["secrets/**"]})
        svc = self._svc({"enabled": True})
        with mock.patch.object(delegate_mod, "_handle_gemini_delegate",
                               return_value="GEMINI-SENTINEL") as h:
            out = delegate_mod.handle_delegate(
                "t-rules-gem", "ask", "", "", svc, _finalize,
                backend="gemini")
        self.assertIn("allow_write_delegation", out)
        h.assert_not_called()

    def test_rules_active_opt_in_passes_through(self):
        self._write_access({"deny": ["secrets/**"]})
        svc = self._svc({"enabled": True})
        with mock.patch.object(delegate_mod, "_handle_gemini_delegate",
                               return_value="GEMINI-SENTINEL"):
            out = delegate_mod.handle_delegate(
                "t-optin-gem", "ask", "", "", svc, _finalize,
                backend="gemini", allow_write_delegation=True)
        self.assertEqual(out, "GEMINI-SENTINEL")

    def _run_codex_capture(self, svc):
        seen = {}

        def fake_run(**kw):
            seen.update(kw)
            return "CODEX-OUT", True

        with mock.patch.object(delegate_mod, "_run_codex",
                               side_effect=fake_run), \
                mock.patch.object(delegate_mod, "_codex_available", True), \
                mock.patch.object(delegate_mod, "_codex_memory_bridge",
                                  return_value=None):
            # Key the task on the test name, not id(svc): an address is
            # reused as soon as the previous svc is collected, which made two
            # different tests share one cache entry.
            out = delegate_mod.handle_delegate(
                f"t-codex-{self.id()}", "ask", "", "", svc, _finalize,
                backend="codex")
        return out, seen

    def test_rules_active_codex_sandbox_pinned(self):
        self._write_access({"deny": ["secrets/**"]})
        svc = self._svc({"enabled": True, "codex_enabled": True,
                         "codex_default_sandbox": "workspace-write"})
        out, seen = self._run_codex_capture(svc)
        self.assertEqual(out, "CODEX-OUT")
        self.assertEqual(seen.get("sandbox"), "read-only")

    def test_no_rules_codex_sandbox_from_config(self):
        svc = self._svc({"enabled": True, "codex_enabled": True,
                         "codex_default_sandbox": "workspace-write"})
        out, seen = self._run_codex_capture(svc)
        self.assertEqual(out, "CODEX-OUT")
        self.assertEqual(seen.get("sandbox"), "workspace-write")


if __name__ == "__main__":
    unittest.main()
