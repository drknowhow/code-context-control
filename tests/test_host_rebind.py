"""cli/mcp_server._rebind_host_session / _check_handoff (2.155.0).

After a /clear the server must run under the NEW host session id: grants and
leases are keyed by it, the link file and heartbeat name it, and C3 Desk's
sentinel waits for `C3 connected <new id>` within 30 s of the hook's
`Session started <new id>`.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cli import mcp_server  # noqa: E402
from cli.tools import _grants  # noqa: E402
from core.host import HostContext  # noqa: E402
from services import host_handoff, session_live  # noqa: E402
from services.host_sessions import linked_c3_session_id  # noqa: E402

OLD = "11111111-aaaa-4bbb-8ccc-000000000001"
NEW = "22222222-aaaa-4bbb-8ccc-000000000002"
C3_ID = "20261012_120000_abcdef012345"


class _Log:
    def __init__(self):
        self.rows = []

    def log(self, event_type, data):
        self.rows.append((event_type, dict(data)))


class TestRebind(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name)
        (self.project / ".c3").mkdir()
        self.session = {"id": C3_ID, "host_session_id": OLD}
        self.svc = SimpleNamespace(
            project_path=str(self.project), ide_name="claude-code",
            host_context=HostContext("claude-code", OLD),
            session_mgr=SimpleNamespace(current_session=self.session, host_context=None),
            activity_log=_Log(),
        )
        self.notified = []
        p = mock.patch("services.notifications.notify",
                       side_effect=lambda *a, **k: self.notified.append(k))
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": OLD})
        env.start()
        self.addCleanup(env.stop)
        with mcp_server._host_sid_lock:
            mcp_server._host_sid_cache.update({"value": OLD, "read_at": 0.0, "linked": OLD})

    def tearDown(self):
        self.tmp.cleanup()

    def test_rebind_redoes_everything_derived_from_the_old_id(self):
        self.assertTrue(mcp_server._rebind_host_session(self.svc, NEW, "clear"))
        self.assertEqual(self.session["host_session_id"], NEW)
        self.assertEqual(_grants.session_id(self.svc), NEW)            # grants / access guard
        self.assertEqual(os.environ["CLAUDE_CODE_SESSION_ID"], NEW)    # proxy runtimes
        self.assertEqual(self.svc.host_context.host_session_id, NEW)
        self.assertEqual(self.svc.session_mgr.host_context.host_session_id, NEW)
        self.assertEqual(linked_c3_session_id(self.project, "claude", NEW), C3_ID)
        beats = session_live.live_sessions(self.project)
        self.assertEqual([b["host_session_id"] for b in beats], [NEW])
        rows = [r for r in self.svc.activity_log.rows if r[0] == "session_rebind"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][1]["previous_host_session_id"], rows[0][1]["host_session_id"],
                          rows[0][1]["start_source"]), (OLD, NEW, "clear"))
        self.assertEqual(len(self.notified), 1)
        self.assertEqual(self.notified[0]["kind"], "mcp")
        self.assertEqual(self.notified[0]["title"], f"C3 connected {NEW[:8]}")
        self.assertEqual(self.notified[0]["ref_id"], C3_ID)
        self.assertIn(NEW, self.notified[0]["message"])

    def test_same_or_empty_id_changes_nothing(self):
        self.assertFalse(mcp_server._rebind_host_session(self.svc, OLD))
        self.assertFalse(mcp_server._rebind_host_session(self.svc, ""))
        self.assertEqual(self.notified, [])
        self.assertEqual(self.svc.activity_log.rows, [])

    def test_check_handoff_consumes_the_file_once(self):
        self.assertFalse(mcp_server._check_handoff(self.svc))
        host_handoff.write_handoff(self.project, C3_ID, host_session_id=NEW,
                                   previous_host_session_id=OLD, start_source="clear")
        self.assertTrue(mcp_server._check_handoff(self.svc))
        self.assertEqual(self.session["host_session_id"], NEW)
        self.assertFalse(host_handoff.handoff_path(self.project, C3_ID).exists())
        self.assertFalse(mcp_server._check_handoff(self.svc))
        self.assertEqual(len(self.notified), 1)

    def test_a_handoff_to_the_current_id_is_cleared_silently(self):
        host_handoff.write_handoff(self.project, C3_ID, host_session_id=OLD)
        self.assertFalse(mcp_server._check_handoff(self.svc))
        self.assertFalse(host_handoff.handoff_path(self.project, C3_ID).exists())
        self.assertEqual(self.notified, [])

    def test_broken_services_never_raise(self):
        broken = SimpleNamespace(project_path=str(self.project), session_mgr=None)
        self.assertFalse(mcp_server._check_handoff(broken))
        self.assertFalse(mcp_server._rebind_host_session(broken, NEW))
        self.assertFalse(mcp_server._check_handoff(object()))

    def test_hook_then_server_round_trip(self):
        """The real sequence: the server beats with its host pid, the hook
        (same host pid) hands off the new id, the server picks it up."""
        session_live.beat(self.project, C3_ID, host_session_id=OLD, ide="claude-code", host_pid=777)
        self.assertEqual(host_handoff.hand_off(self.project, "claude", NEW, "clear", host_pid=777), [C3_ID])
        self.assertTrue(mcp_server._check_handoff(self.svc))
        self.assertEqual(self.session["host_session_id"], NEW)
        self.assertEqual(self.notified[-1]["title"], f"C3 connected {NEW[:8]}")


if __name__ == "__main__":
    unittest.main()
