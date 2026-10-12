"""services/host_handoff (2.155.0): an MCP server follows /clear to its new host id.

`/clear` starts a new host session but keeps the MCP server, which had read
CLAUDE_CODE_SESSION_ID once. The hook and the server share a host process;
the hook finds the server's heartbeat by that pid and leaves it a handoff.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import host_handoff, session_live  # noqa: E402

# A tree: 1 init → 10 terminal → 20 claude.exe → 30 cmd.exe → 40 python.exe (hook)
#                                             └→ 31 c3-mcp.exe → 41 python.exe (mcp)
#         20 → 32 bash.exe → 42 bash.exe → 43 python.exe (nested shells)
TABLE = {
    1: (0, "init"),
    10: (1, "WindowsTerminal.exe"),
    20: (10, "claude.exe"),
    30: (20, "cmd.exe"), 40: (30, "python.exe"),
    31: (20, "c3-mcp.exe"), 41: (31, "python.exe"),
    32: (20, "bash.exe"), 42: (32, "bash.exe"), 43: (42, "pythonw.exe"),
    50: (10, "node"), 60: (50, "sh"), 61: (60, "python3"),
    70: (71, "python.exe"), 71: (70, "cmd.exe"),          # a cycle (pid reuse)
}


class TestProcessTree(unittest.TestCase):
    def test_hook_and_server_resolve_to_the_same_host(self):
        self.assertEqual(host_handoff.host_process_id(40, TABLE), 20)
        self.assertEqual(host_handoff.host_process_id(41, TABLE), 20)
        self.assertEqual(host_handoff.host_process_id(43, TABLE), 20)

    def test_node_is_a_host_not_a_wrapper(self):
        self.assertEqual(host_handoff.host_process_id(61, TABLE), 50)

    def test_unknown_pid_cycle_and_root_are_zero(self):
        self.assertEqual(host_handoff.host_process_id(999, TABLE), 0)
        self.assertEqual(host_handoff.host_process_id(70, TABLE), 0)
        self.assertEqual(host_handoff.host_process_id(1, TABLE), 0)
        self.assertEqual(host_handoff.host_process_id(20, {}), 0)

    def test_ancestors_walk_upward_and_stop_at_the_root(self):
        self.assertEqual([p for p, _ in host_handoff.ancestors(40, TABLE)], [30, 20, 10, 1])
        self.assertEqual(host_handoff.ancestors(70, TABLE), [(71, "cmd.exe")])

    def test_real_table_contains_this_process_and_its_parent(self):
        table = host_handoff.process_table()
        if not table:
            self.skipTest("process table not readable on this platform")
        self.assertIn(os.getpid(), table)
        if os.getppid() in table:
            self.assertEqual(host_handoff.ancestors()[0][0], os.getppid())
        self.assertIsInstance(host_handoff.host_process_id(), int)


class TestHandoffFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_read_clear(self):
        self.assertIsNone(host_handoff.write_handoff(self.project, "", host_session_id="h"))
        self.assertIsNone(host_handoff.write_handoff(self.project, "c3-1", host_session_id=""))
        path = host_handoff.write_handoff(
            self.project, "c3-1", host_session_id="new", previous_host_session_id="old",
            host_pid=20, start_source="clear")
        self.assertTrue(path.is_file())
        data = host_handoff.read_handoff(self.project, "c3-1")
        self.assertEqual((data["host_session_id"], data["previous_host_session_id"],
                          data["host_pid"], data["start_source"]), ("new", "old", 20, "clear"))
        self.assertEqual(host_handoff.read_handoff(self.project, "c3-2"), {})
        self.assertTrue(host_handoff.clear_handoff(self.project, "c3-1"))
        self.assertFalse(host_handoff.clear_handoff(self.project, "c3-1"))
        self.assertEqual(host_handoff.read_handoff(self.project, "c3-1"), {})

    def test_a_handoff_is_never_mistaken_for_a_heartbeat(self):
        session_live.beat(self.project, "c3-1", host_session_id="old", ide="claude-code")
        host_handoff.write_handoff(self.project, "c3-1", host_session_id="new")
        live = session_live.live_sessions(self.project)
        self.assertEqual([s["session_id"] for s in live], ["c3-1"])
        self.assertTrue(host_handoff.handoff_path(self.project, "c3-1").is_file())

    def test_unsafe_session_id_stays_inside_the_handoff_dir(self):
        path = host_handoff.write_handoff(self.project, "../../evil", host_session_id="h")
        self.assertEqual(path.parent, host_handoff.handoff_dir(self.project))


class TestHandOff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _beat(self, sid, host="old", ide="claude-code", host_pid=0):
        session_live.beat(self.project, sid, host_session_id=host, ide=ide, host_pid=host_pid)

    def _pending(self):
        d = host_handoff.handoff_dir(self.project)
        return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

    def test_matches_the_server_under_the_same_host_pid(self):
        self._beat("mine", host_pid=20)
        self._beat("other-window", host="w2", host_pid=21)
        done = host_handoff.hand_off(self.project, "claude", "new", "clear", host_pid=20)
        self.assertEqual(done, ["mine"])
        self.assertEqual(self._pending(), ["mine"])
        data = host_handoff.read_handoff(self.project, "mine")
        self.assertEqual((data["host_session_id"], data["previous_host_session_id"], data["host_pid"]),
                         ("new", "old", 20))

    def test_same_id_already_is_a_noop(self):
        self._beat("mine", host="new", host_pid=20)
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "clear", host_pid=20), [])
        self.assertEqual(self._pending(), [])

    def test_single_session_fallback_only_for_clear_or_resume(self):
        self._beat("only")                                  # no host_pid recorded (older server)
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "startup", host_pid=0), [])
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "clear", host_pid=0), ["only"])
        host_handoff.clear_handoff(self.project, "only")
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "resume", host_pid=20), ["only"])

    def test_two_sessions_without_a_pid_match_are_ambiguous(self):
        self._beat("a")
        self._beat("b", host="old-b")
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "clear", host_pid=0), [])
        self.assertEqual(self._pending(), [])

    def test_another_providers_session_is_not_a_candidate(self):
        self._beat("codex-one", ide="codex", host_pid=20)
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "clear", host_pid=20), [])
        self.assertEqual(host_handoff.hand_off(self.project, "codex", "new", "clear", host_pid=20), ["codex-one"])

    def test_provider_aliases_join(self):
        self._beat("mine", ide="claude-code", host_pid=20)
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "clear", host_pid=20), ["mine"])

    def test_uses_the_process_tree_when_no_pid_is_given(self):
        self._beat("mine", host_pid=4242)
        with mock.patch.object(host_handoff, "host_process_id", return_value=4242):
            self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "clear"), ["mine"])

    def test_nothing_live_or_no_id_is_a_noop(self):
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "new", "clear", host_pid=20), [])
        self._beat("mine", host_pid=20)
        self.assertEqual(host_handoff.hand_off(self.project, "claude", "", "clear", host_pid=20), [])


if __name__ == "__main__":
    unittest.main()
