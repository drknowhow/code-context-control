"""Allow/deny matrix for cli/hook_pretool_enforce.run().

Drives the enforcement logic in-process (no subprocess) via the importable
run(payload, project_path) entry point added for the v2.42 dispatcher:
  - no state → read-class advisory, write-class deny
  - fresh signal in consolidated state → allow (write tools only for
    write-granting c3 tools)
  - stale TTL → advisory
  - sticky per-file unlock → allow + drift-guard nudge
  - corrupted state file → advisory fallback + critical warning + quarantine
  - legacy last_c3_call.json / unlocked_files.json fallback still honored
  - session mismatch → state treated as stale (advisory, no stale unlocks)
"""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from cli import _hook_utils  # noqa: E402

# hook_pretool_enforce imports plain `_hook_utils`; alias so both spellings
# resolve to the same module instance (mirrors cli/hook_dispatch.py).
sys.modules.setdefault("_hook_utils", _hook_utils)

from cli.hook_pretool_enforce import run  # noqa: E402


def _now_iso(offset_secs: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_secs)).isoformat()


class EnforceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".c3").mkdir()
        _hook_utils.drain_state_warnings()

    def tearDown(self):
        _hook_utils.drain_state_warnings()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, tool_name, tool_input, session_id=""):
        payload = {"tool_name": tool_name, "tool_input": tool_input}
        if session_id:
            payload["session_id"] = session_id
        return run(payload, project_path=self.tmp)

    def _write_state(self, tool="", read_unlocked=False, ts=None,
                     unlocked=None, session_id=""):
        state = {"session_id": session_id, "last_c3_call": None,
                 "unlocked_files": unlocked or {}}
        if tool:
            state["last_c3_call"] = {
                "ts": ts if ts is not None else _now_iso(),
                "tool": tool,
                "read_unlocked": read_unlocked,
            }
        (self.tmp / ".c3" / "enforcement_state.json").write_text(
            json.dumps(state), encoding="utf-8")

    # ── assertion helpers ────────────────────────────────────────────────────
    def assert_advisory(self, out):
        self.assertIsNotNone(out)
        self.assertNotIn("hookSpecificOutput", out)
        self.assertIn("[c3:hint]", out.get("additionalContext", ""))

    def assert_denied(self, out):
        self.assertIsNotNone(out)
        hso = out.get("hookSpecificOutput") or {}
        self.assertEqual(hso.get("permissionDecision"), "deny")
        self.assertIn("ledger", hso.get("permissionDecisionReason", "").lower())

    def assert_allowed_silently(self, out):
        self.assertIsNone(out)


class TestNoState(EnforceBase):
    def test_read_is_advisory(self):
        self.assert_advisory(self._run("Read", {"file_path": "foo.py"}))

    def test_grep_is_advisory(self):
        self.assert_advisory(self._run("Grep", {"pattern": "foo"}))

    def test_glob_is_advisory(self):
        self.assert_advisory(self._run("Glob", {"pattern": "**/*.py"}))

    def test_edit_is_denied(self):
        self.assert_denied(self._run("Edit", {"file_path": "foo.py",
                                              "old_string": "a", "new_string": "b"}))

    def test_write_is_denied(self):
        self.assert_denied(self._run("Write", {"file_path": "foo.py", "content": "x"}))

    def test_unknown_tool_passes_through(self):
        self.assert_allowed_silently(self._run("SomeRandomTool", {}))


class TestSignal(EnforceBase):
    def test_fresh_read_signal_allows_read(self):
        self._write_state(tool="c3_search", read_unlocked=True)
        self.assert_allowed_silently(self._run("Read", {"file_path": "foo.py"}))

    def test_fresh_signal_records_sticky_unlock(self):
        self._write_state(tool="c3_search", read_unlocked=True)
        self._run("Read", {"file_path": str(self.tmp / "foo.py")})
        state = _hook_utils.load_enforcement_state(self.tmp)
        normalized = str((self.tmp / "foo.py").resolve())
        self.assertIn("read", state["unlocked_files"].get(normalized, []))

    def test_read_signal_does_not_unlock_edit(self):
        self._write_state(tool="c3_search", read_unlocked=True)
        self.assert_denied(self._run("Edit", {"file_path": "foo.py",
                                              "old_string": "a", "new_string": "b"}))

    def test_status_signal_does_not_unlock_write(self):
        self._write_state(tool="c3_status", read_unlocked=False)
        self.assert_denied(self._run("Write", {"file_path": "foo.py", "content": "x"}))

    def test_edit_signal_unlocks_edit(self):
        self._write_state(tool="c3_edit", read_unlocked=False)
        self.assert_allowed_silently(self._run("Edit", {"file_path": "foo.py",
                                                        "old_string": "a", "new_string": "b"}))

    def test_agent_signal_unlocks_multiedit(self):
        self._write_state(tool="c3_agent", read_unlocked=False)
        self.assert_allowed_silently(self._run("MultiEdit", {"file_path": "foo.py", "edits": []}))

    def test_stale_ttl_signal_is_advisory(self):
        # 700s old signal > 600s TTL → treated as absent
        self._write_state(tool="c3_search", read_unlocked=True, ts=_now_iso(-700))
        self.assert_advisory(self._run("Read", {"file_path": "foo.py"}))

    def test_stale_ttl_signal_keeps_edit_denied(self):
        self._write_state(tool="c3_edit", read_unlocked=False, ts=_now_iso(-700))
        self.assert_denied(self._run("Edit", {"file_path": "foo.py",
                                              "old_string": "a", "new_string": "b"}))

    def test_non_read_unlocking_signal_grep_without_target(self):
        # Fix 5 parity: c3_memory signal does not unlock bare Grep/Glob
        self._write_state(tool="c3_memory", read_unlocked=False)
        self.assert_advisory(self._run("Grep", {}))


class TestStickyUnlock(EnforceBase):
    def _unlock(self, name, cats):
        normalized = str((self.tmp / name).resolve())
        self._write_state(unlocked={normalized: cats})
        return str(self.tmp / name)

    def test_read_unlock_allows_read_with_drift_guard(self):
        fp = self._unlock("foo.py", ["read"])
        out = self._run("Read", {"file_path": fp})
        self.assertIsNotNone(out)
        self.assertIn("[c3:drift-guard]", out.get("additionalContext", ""))
        self.assertNotIn("hookSpecificOutput", out)

    def test_read_unlock_does_not_allow_edit(self):
        fp = self._unlock("foo.py", ["read"])
        self.assert_denied(self._run("Edit", {"file_path": fp,
                                              "old_string": "a", "new_string": "b"}))

    def test_edit_unlock_allows_edit(self):
        fp = self._unlock("foo.py", ["edit"])
        out = self._run("Edit", {"file_path": fp, "old_string": "a", "new_string": "b"})
        self.assertIsNotNone(out)
        self.assertIn("[c3:drift-guard]", out.get("additionalContext", ""))

    def test_unlock_is_per_file(self):
        self._unlock("foo.py", ["read"])
        self.assert_advisory(self._run("Read", {"file_path": str(self.tmp / "bar.py")}))


class TestCorruptedState(EnforceBase):
    def test_corrupted_state_falls_back_to_advisory(self):
        state_path = self.tmp / ".c3" / "enforcement_state.json"
        state_path.write_text("{not valid json !!!", encoding="utf-8")
        out = self._run("Read", {"file_path": "foo.py"})
        self.assert_advisory(out)

    def test_corrupted_state_is_quarantined_and_warned(self):
        state_path = self.tmp / ".c3" / "enforcement_state.json"
        state_path.write_text("{not valid json !!!", encoding="utf-8")
        self._run("Read", {"file_path": "foo.py"})
        self.assertFalse(state_path.exists(), "corrupt state file must be quarantined")
        self.assertTrue((self.tmp / ".c3" / "enforcement_state.json.corrupt").exists())
        warnings = _hook_utils.drain_state_warnings()
        self.assertTrue(any("[c3:hook-error]" in w for w in warnings), warnings)

    def test_corrupted_state_keeps_write_denied(self):
        # Fail-open for reads must never fail-open for writes.
        (self.tmp / ".c3" / "enforcement_state.json").write_text("garbage", encoding="utf-8")
        self.assert_denied(self._run("Write", {"file_path": "foo.py", "content": "x"}))


class TestLegacyFallback(EnforceBase):
    def test_legacy_signal_file_honored(self):
        (self.tmp / ".c3" / "last_c3_call.json").write_text(json.dumps({
            "timestamp": _now_iso(), "tool": "c3_search", "read_unlocked": True,
        }), encoding="utf-8")
        self.assert_allowed_silently(self._run("Read", {"file_path": "foo.py"}))

    def test_legacy_unlock_file_honored(self):
        normalized = str((self.tmp / "foo.py").resolve())
        (self.tmp / ".c3" / "unlocked_files.json").write_text(
            json.dumps({normalized: ["read"]}), encoding="utf-8")
        out = self._run("Read", {"file_path": str(self.tmp / "foo.py")})
        self.assertIsNotNone(out)
        self.assertIn("[c3:drift-guard]", out.get("additionalContext", ""))

    def test_new_state_wins_over_legacy(self):
        # Legacy has a fresh signal but the new file exists (empty) → new wins.
        (self.tmp / ".c3" / "last_c3_call.json").write_text(json.dumps({
            "timestamp": _now_iso(), "tool": "c3_search", "read_unlocked": True,
        }), encoding="utf-8")
        self._write_state()  # empty consolidated state
        self.assert_advisory(self._run("Read", {"file_path": "foo.py"}))


class TestSessionScoping(EnforceBase):
    def test_session_mismatch_ignores_signal(self):
        self._write_state(tool="c3_search", read_unlocked=True, session_id="session-A")
        out = self._run("Read", {"file_path": "foo.py"}, session_id="session-B")
        self.assert_advisory(out)

    def test_session_mismatch_ignores_write_unlock(self):
        self._write_state(tool="c3_edit", read_unlocked=False, session_id="session-A")
        self.assert_denied(self._run(
            "Edit", {"file_path": "foo.py", "old_string": "a", "new_string": "b"},
            session_id="session-B",
        ))

    def test_same_session_signal_allows(self):
        self._write_state(tool="c3_search", read_unlocked=True, session_id="session-A")
        self.assert_allowed_silently(
            self._run("Read", {"file_path": "foo.py"}, session_id="session-A"))

    def test_unscoped_legacy_state_still_accepted(self):
        # State without a session_id (legacy writers) is accepted by any session.
        self._write_state(tool="c3_search", read_unlocked=True, session_id="")
        self.assert_allowed_silently(
            self._run("Read", {"file_path": "foo.py"}, session_id="session-B"))


if __name__ == "__main__":
    unittest.main()
