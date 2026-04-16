"""Enforcement model tests: read-class advisory, write-class blocked.

Regression: the previous hook blocked every native tool without a prior
c3_* call. That turned every Windows edge case into a dead-end. The new
model uses additionalContext hints for read-only tools and hard-denies
only file mutations so the edit ledger stays authoritative.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_hook(tool_name: str, tool_input: dict) -> dict:
    """Spawn the hook in a throwaway project and parse its stdout JSON."""
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / ".c3").mkdir()
        (tmp / ".c3" / "activity_log.jsonl").write_text("", encoding="utf-8")
        shutil.copy(REPO_ROOT / "cli" / "hook_pretool_enforce.py", tmp / "hook.py")
        shutil.copy(REPO_ROOT / "cli" / "_hook_utils.py", tmp / "_hook_utils.py")

        payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input})
        res = subprocess.run(
            [sys.executable, str(tmp / "hook.py")],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(tmp),
            timeout=10,
        )
        out = (res.stdout or "").strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"raw": out, "stderr": res.stderr}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TestEnforcementFlip(unittest.TestCase):
    def _assert_advisory(self, tool_name: str, tool_input: dict):
        r = _run_hook(tool_name, tool_input)
        self.assertNotIn("hookSpecificOutput", r,
                         f"{tool_name} must not be blocked — should be advisory. Got {r}")
        self.assertIn("additionalContext", r,
                      f"{tool_name} advisory must emit additionalContext hint. Got {r}")
        self.assertIn("[c3:hint]", r["additionalContext"])

    def _assert_blocked(self, tool_name: str, tool_input: dict):
        r = _run_hook(tool_name, tool_input)
        hso = r.get("hookSpecificOutput") or {}
        self.assertEqual(hso.get("permissionDecision"), "deny",
                         f"{tool_name} must be hard-blocked. Got {r}")
        reason = hso.get("permissionDecisionReason", "")
        self.assertIn("ledger", reason.lower(),
                      f"{tool_name} deny reason should cite the edit ledger. Got: {reason}")

    def test_read_is_advisory(self):
        self._assert_advisory("Read", {"file_path": "foo.py"})

    def test_grep_is_advisory(self):
        self._assert_advisory("Grep", {"pattern": "foo"})

    def test_glob_is_advisory(self):
        self._assert_advisory("Glob", {"pattern": "**/*.py"})

    def test_edit_is_blocked(self):
        self._assert_blocked("Edit", {"file_path": "foo.py",
                                       "old_string": "a", "new_string": "b"})

    def test_write_is_blocked(self):
        self._assert_blocked("Write", {"file_path": "foo.py", "content": "x"})

    def test_multiedit_is_blocked(self):
        self._assert_blocked("MultiEdit", {"file_path": "foo.py", "edits": []})

    def test_unknown_tool_passes_through(self):
        r = _run_hook("SomeRandomTool", {})
        self.assertEqual(r, {}, "Unenforced tools must produce no output")


if __name__ == "__main__":
    unittest.main()
