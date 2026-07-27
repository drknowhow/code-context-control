"""T2b tests — the access-guard pretool sub-hook + fail-closed dispatch.

Proves: native tool verdicts fire BEFORE unlock logic (sticky-unlock
immunity), the dispatcher denies write-class tools when the guard itself
breaks, explicit-path search denial vs rootless advisory, the best-effort
shell scan, Bash matcher registration, and canonical-key parity between the
unlock map and the evaluator.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

sys.modules.setdefault("_hook_utils", _hook_utils)

import cli.hook_access_guard as hag  # noqa: E402
import cli.hook_dispatch as hd  # noqa: E402
from services import access_guard as ag  # noqa: E402


class HookGuardBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        (self.proj / ".c3").mkdir()
        self._write_access({"deny": ["secrets/**"], "read_only": ["docs/**"]})
        (self.proj / "secrets").mkdir()
        (self.proj / "secrets" / "key.txt").write_text("k", encoding="utf-8")
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        _hook_utils.drain_state_warnings()
        self._tmp.cleanup()

    def _write_access(self, section):
        (self.proj / ".c3" / "config.json").write_text(
            json.dumps({"access": section}), encoding="utf-8")

    def _run(self, tool, tool_input):
        return hag.run({"tool_name": tool, "tool_input": tool_input},
                       project_path=self.proj)

    def _reason(self, out):
        return (out or {}).get("hookSpecificOutput", {}).get(
            "permissionDecisionReason", "")


class TestNativeVerdicts(HookGuardBase):
    def test_write_tools_denied_on_deny_rule(self):
        for tool in ("Edit", "Write", "MultiEdit"):
            out = self._run(tool, {"file_path": str(self.proj / "secrets/key.txt")})
            self.assertIn("[c3-access:denied]", self._reason(out), tool)

    def test_notebook_edit_uses_notebook_path(self):
        out = self._run("NotebookEdit",
                        {"notebook_path": str(self.proj / "secrets/nb.ipynb")})
        self.assertIn("[c3-access:denied]", self._reason(out))

    def test_read_denied_on_deny_rule(self):
        out = self._run("Read", {"file_path": str(self.proj / "secrets/key.txt")})
        self.assertIn("[c3-access:denied]", self._reason(out))

    def test_read_only_rule_allows_read_denies_write(self):
        (self.proj / "docs").mkdir()
        target = str(self.proj / "docs" / "a.md")
        self.assertIsNone(self._run("Read", {"file_path": target}))
        out = self._run("Edit", {"file_path": target})
        self.assertIn("[c3-access:read_only]", self._reason(out))

    def test_clean_file_passes(self):
        self.assertIsNone(self._run("Edit",
                                    {"file_path": str(self.proj / "src.py")}))


class TestSearchTools(HookGuardBase):
    def test_explicit_denied_root_hard_denied(self):
        out = self._run("Grep", {"pattern": "x",
                                 "path": str(self.proj / "secrets")})
        self.assertIn("[c3-access:denied]", self._reason(out))

    def test_rootless_search_gets_advisory_footer(self):
        out = self._run("Grep", {"pattern": "x"})
        self.assertIsNotNone(out)
        self.assertIn("[c3-access:limited]", out.get("additionalContext", ""))
        self.assertNotIn("hookSpecificOutput", out)


class TestShellScan(HookGuardBase):
    def test_existing_denied_path_in_command_denied(self):
        target = str(self.proj / "secrets" / "key.txt")
        out = self._run("Bash", {"command": f"cat {target}"})
        self.assertIn("[c3-access:denied]", self._reason(out))
        self.assertIn("best-effort shell scan", self._reason(out))

    def test_regex_lookalike_token_not_denied(self):
        # '\.env' is a grep pattern, not a file — existence gate must pass it.
        out = self._run("Bash", {"command": r'grep -r "\.env" docs/'})
        self.assertIsNone(out)

    def test_benign_command_passes(self):
        self.assertIsNone(self._run("Bash", {"command": "git status"}))


class TestDispatchFailClosed(HookGuardBase):
    def _dispatch(self, tool, tool_input):
        return hd.dispatch("pretool",
                           {"tool_name": tool, "tool_input": tool_input},
                           project_path=self.proj)

    def test_import_failure_denies_write_class(self):
        with mock.patch.object(hd, "_load_run",
                               side_effect=lambda m: (None, "ImportError: x")):
            out = self._dispatch("Edit", {"file_path": str(self.proj / "a.py")})
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("[c3-access:error]", reason)
        self.assertIn("fail-closed", reason)

    def test_runtime_exception_denies_write_class(self):
        real_load = hd._load_run

        def loader(mod):
            if mod == "hook_access_guard":
                return (lambda p, pp=None: 1 / 0), ""
            return real_load(mod)

        with mock.patch.object(hd, "_load_run", side_effect=loader):
            out = self._dispatch("Write", {"file_path": str(self.proj / "a.py")})
        self.assertEqual(
            out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("[c3-access:error]",
                      out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_read_class_fails_open_with_warning(self):
        with mock.patch.object(hd, "_load_run",
                               side_effect=lambda m: (None, "ImportError: x")):
            out = self._dispatch("Read", {"file_path": str(self.proj / "a.py")})
        self.assertNotIn("hookSpecificOutput", out or {})
        self.assertIn("[c3:hook-error]", (out or {}).get("additionalContext", ""))


class TestStickyUnlockImmunity(HookGuardBase):
    def test_unlocked_denied_file_still_denied(self):
        target = str(self.proj / "secrets" / "key.txt")
        _hook_utils.record_unlocked_files(
            [target], {"edit", "read"}, session_id="s1",
            project_path=self.proj)
        out = hd.dispatch(
            "pretool",
            {"tool_name": "Edit", "session_id": "s1",
             "tool_input": {"file_path": target}},
            project_path=self.proj)
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("[c3-access:denied]", reason)
        self.assertNotIn("[c3:enforce]", reason)


class TestCanonicalParity(unittest.TestCase):
    def test_unlock_key_matches_evaluator_canon(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "A File.TXT"
            f.write_text("x", encoding="utf-8")
            self.assertEqual(_hook_utils.canonical_key(f),
                             ag.canonicalize(str(f), tmp)[0])


class TestInstallMatchers(unittest.TestCase):
    def test_pre_matchers_include_shell(self):
        source = (REPO_ROOT / "cli" / "c3.py").read_text(encoding="utf-8")
        block = source.split("_pre_matcher_names = [", 1)[1].split("]", 1)[0]
        self.assertIn("shell_matcher", block)
        self.assertIn("run_shell_command", block)


if __name__ == "__main__":
    unittest.main()
