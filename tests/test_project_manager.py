import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services import session_live
from services.project_manager import ProjectManager


class _StubActivityLog:
    def __init__(self, events):
        self._events = events

    def get_recent(self, limit=1, event_type=None, since=None):
        items = list(self._events)
        if event_type:
            items = [event for event in items if event.get("type") == event_type]
        if since:
            items = [event for event in items if event.get("timestamp", "") >= since]
        items.sort(key=lambda event: event.get("timestamp", ""), reverse=True)
        return items[:limit]


class TestProjectManager(unittest.TestCase):
    def setUp(self):
        self.pm = ProjectManager()
        # Use a real existing dir so Path(p["path"]).is_dir() is True. The
        # previous hardcoded self.proj_path was accidentally truthy on dev
        # machines where the path happened to exist (e.g. U:\tmp\proj on
        # Windows) but False on clean CI runners — making the live-session
        # and last-session-derivation paths silently skip.
        self._tmp = tempfile.TemporaryDirectory()
        self.proj_path = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_live_session_info_ignores_stale_activity(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        events = [
            {"type": "session_start", "session_id": "s1", "timestamp": old, "description": "old"},
            {"type": "tool_call", "timestamp": old},
        ]
        with patch("services.project_manager.ActivityLog", return_value=_StubActivityLog(events)):
            result = self.pm._get_live_session_info("dummy")
        self.assertIsNone(result)

    def test_live_session_info_keeps_recent_activity(self):
        start = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        events = [
            {"type": "session_start", "session_id": "s2", "timestamp": start, "description": "recent"},
            {"type": "tool_call", "timestamp": recent},
        ]
        with patch("services.project_manager.ActivityLog", return_value=_StubActivityLog(events)):
            result = self.pm._get_live_session_info("dummy")
        self.assertIsNotNone(result)
        self.assertEqual(result["session_id"], "s2")

    def test_list_projects_does_not_mark_null_port_project_active_without_recent_session(self):
        now = datetime.now(timezone.utc).isoformat()
        with patch.object(self.pm, "_read_projects", return_value=[{"name": "Proj", "path": self.proj_path, "added_at": now}]), \
             patch.object(self.pm, "_read_registry", return_value=[{"project_path": self.proj_path, "port": None, "started_at": now}]), \
             patch.object(self.pm, "_port_alive", return_value=False), \
             patch.object(self.pm, "_get_live_session_info", return_value=None), \
             patch.object(self.pm, "_read_project_config", return_value={}):
            projects = self.pm.list_projects()
        self.assertEqual(len(projects), 1)
        self.assertFalse(projects[0]["ui_active"])
        self.assertFalse(projects[0]["session_active"])
        self.assertFalse(projects[0]["active"])
        self.assertIsNone(projects[0]["port"])

    def test_list_projects_derives_last_session_from_activity_log(self):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        recent = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        with patch.object(self.pm, "_read_projects", return_value=[{
            "name": "Proj",
            "path": self.proj_path,
            "added_at": old,
            "last_session": old,
        }]), \
             patch.object(self.pm, "_read_registry", return_value=[]), \
             patch.object(self.pm, "_get_live_session_info", return_value=None), \
             patch.object(self.pm, "_read_project_config", return_value={}), \
             patch("services.project_manager.ActivityLog", return_value=_StubActivityLog([
                 {"type": "session_start", "timestamp": recent},
                 {"type": "session_save", "timestamp": recent},
             ])):
            projects = self.pm.list_projects()
        self.assertEqual(projects[0]["last_session"], recent)

    def test_list_projects_marks_live_session_active_without_ui_port(self):
        now = datetime.now(timezone.utc).isoformat()
        live_session = {
            "session_id": "sess-1",
            "started_at": now,
            "last_activity": now,
            "description": "recent",
        }
        with patch.object(self.pm, "_read_projects", return_value=[{"name": "Proj", "path": self.proj_path, "added_at": now}]), \
             patch.object(self.pm, "_read_registry", return_value=[]), \
             patch.object(self.pm, "_get_live_sessions", return_value=[live_session]), \
             patch.object(self.pm, "_read_project_config", return_value={}), \
             patch.object(self.pm, "_get_budget_info", return_value={"response_tokens": 123}):
            projects = self.pm.list_projects()
        self.assertTrue(projects[0]["session_active"])
        self.assertTrue(projects[0]["active"])
        self.assertIsNone(projects[0]["port"])
        self.assertEqual(projects[0]["live_session_id"], "sess-1")
        self.assertEqual(projects[0]["session_count"], 1)
        self.assertEqual(projects[0]["sessions"], [live_session])
        self.assertEqual(projects[0]["budget"], {"response_tokens": 123})

    def test_get_active_sessions_includes_live_session_without_ui_port(self):
        now = datetime.now(timezone.utc).isoformat()
        with patch.object(self.pm, "_read_registry", return_value=[]), \
             patch.object(self.pm, "list_projects", return_value=[{
                 "name": "Proj",
                 "path": self.proj_path,
                 "session_active": True,
                 "ui_active": False,
                 "started_at": now,
                 "live_session_id": "sess-2",
             }]):
            sessions = self.pm.get_active_sessions()
        self.assertEqual(sessions, [{
            "project_path": self.proj_path,
            "project_name": "Proj",
            "port": None,
            "started_at": now,
            "live_session_id": "sess-2",
        }])


class TestLivenessSources(unittest.TestCase):
    """v2.128.0: heartbeat files are the truth, and session_end finally counts.

    Before this, `_get_live_session_info` ended a session only on a
    `session_save` row (written by the hub's end button) or after 20 minutes of
    tool-call silence — so a closed IDE read "live" for 20 minutes and a
    quiet-but-open session read dead.
    """

    def setUp(self):
        self.pm = ProjectManager()
        self._tmp = tempfile.TemporaryDirectory()
        self.proj_path = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _ago(self, **kwargs) -> str:
        return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()

    def _beat(self, session_id, *, host_session_id="", age_seconds=0):
        session_live.beat(self.proj_path, session_id,
                          host_session_id=host_session_id, ide="claude-code")
        if age_seconds:
            path = session_live.heartbeat_path(self.proj_path, session_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["ts"] = time.time() - age_seconds
            path.write_text(json.dumps(data), encoding="utf-8")

    def _sessions(self, events):
        with patch("services.project_manager.ActivityLog",
                   return_value=_StubActivityLog(events)):
            return self.pm._get_live_sessions(self.proj_path)

    # ── session_end is terminal ───────────────────────────────────────────

    def test_session_end_row_ends_the_session(self):
        start, recent = self._ago(minutes=5), self._ago(minutes=1)
        self.assertEqual(self._sessions([
            {"type": "session_start", "session_id": "s1", "timestamp": start},
            {"type": "tool_call", "timestamp": recent},
            {"type": "session_end", "session_id": "s1", "timestamp": recent},
        ]), [])

    def test_session_end_for_another_session_is_ignored(self):
        sessions = self._sessions([
            {"type": "session_end", "session_id": "s0", "timestamp": self._ago(minutes=6)},
            {"type": "session_start", "session_id": "s1", "timestamp": self._ago(minutes=5)},
            {"type": "tool_call", "timestamp": self._ago(minutes=1)},
        ])
        self.assertEqual([s["session_id"] for s in sessions], ["s1"])

    def test_session_end_matches_on_host_id_alone(self):
        """The hook writes session_id="" when no link file exists yet."""
        recent = self._ago(minutes=1)
        self.assertEqual(self._sessions([
            {"type": "session_start", "session_id": "s1", "host_session_id": "h1",
             "timestamp": self._ago(minutes=5)},
            {"type": "tool_call", "timestamp": recent},
            {"type": "session_end", "session_id": "", "host_session_id": "h1",
             "timestamp": recent},
        ]), [])

    def test_a_busy_log_no_longer_hides_the_running_session(self):
        """Traffic since the session started used to make it invisible.

        `get_recent(limit=1, event_type=...)` looks at the last 100 lines only;
        in C3's own repo the running session's `session_start` sat 319 lines
        from the end, so the hub called an active project idle. Uses the real
        ActivityLog — the point is the file scan, not the stub.
        """
        log = Path(self.proj_path) / ".c3" / "activity_log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"type": "session_start", "session_id": "s1",
                 "timestamp": self._ago(minutes=30), "description": "busy"}]
        rows += [{"type": "tool_call", "timestamp": self._ago(seconds=30)}
                 for _ in range(500)]
        log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        sessions = self.pm._get_live_sessions(self.proj_path)
        self.assertEqual([s["session_id"] for s in sessions], ["s1"])
        self.assertEqual(sessions[0]["source"], "activity_log")
        self.assertEqual(sessions[0]["description"], "busy")

    def test_stub_without_find_last_still_works(self):
        """Another checkout on an older C3 keeps serving the same project."""
        sessions = self._sessions([
            {"type": "session_start", "session_id": "s1", "timestamp": self._ago(minutes=5)},
            {"type": "tool_call", "timestamp": self._ago(minutes=1)},
        ])
        self.assertFalse(hasattr(_StubActivityLog([]), "find_last"))
        self.assertEqual([s["session_id"] for s in sessions], ["s1"])

    # ── heartbeats outrank inference ──────────────────────────────────────

    def test_heartbeat_keeps_a_long_idle_session_live(self):
        old = self._ago(hours=2)
        self._beat("s1", host_session_id="h1")
        sessions = self._sessions([
            {"type": "session_start", "session_id": "s1", "host_session_id": "h1",
             "timestamp": old, "description": "quiet but open"},
            {"type": "tool_call", "timestamp": old},
        ])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["source"], "heartbeat")
        self.assertEqual(sessions[0]["started_at"], old)
        self.assertEqual(sessions[0]["description"], "quiet but open")
        self.assertGreater(sessions[0]["idle_seconds"], 20 * 60)

    def test_stale_heartbeat_does_not_resurrect_a_dead_session(self):
        old = self._ago(hours=2)
        self._beat("s1", age_seconds=session_live.HEARTBEAT_TTL_S + 60)
        self.assertEqual(self._sessions([
            {"type": "session_start", "session_id": "s1", "timestamp": old},
            {"type": "tool_call", "timestamp": old},
        ]), [])

    def test_heartbeat_outranks_a_hub_session_save(self):
        """The process is still serving; a bookkeeping row does not kill it."""
        self._beat("s1")
        sessions = self._sessions([
            {"type": "session_start", "session_id": "s1", "timestamp": self._ago(minutes=10)},
            {"type": "session_save", "session_id": "s1", "timestamp": self._ago(minutes=1)},
        ])
        self.assertEqual([s["session_id"] for s in sessions], ["s1"])

    def test_heartbeat_without_a_start_row_still_counts(self):
        self._beat("s1")
        sessions = self._sessions([])
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["started_at"])

    def test_heartbeat_session_is_not_duplicated_by_the_inference(self):
        self._beat("s1")
        self.assertEqual(len(self._sessions([
            {"type": "session_start", "session_id": "s1", "timestamp": self._ago(minutes=5)},
            {"type": "tool_call", "timestamp": self._ago(minutes=1)},
        ])), 1)

    def test_two_heartbeats_are_two_sessions(self):
        older, newer = self._ago(hours=3), self._ago(minutes=4)
        self._beat("s-old")
        self._beat("s-new")
        events = [
            {"type": "session_start", "session_id": "s-old", "timestamp": older},
            {"type": "session_start", "session_id": "s-new", "timestamp": newer},
        ]
        self.assertEqual([s["session_id"] for s in self._sessions(events)],
                         ["s-new", "s-old"])
        with patch.object(self.pm, "_read_projects", return_value=[
                {"name": "Proj", "path": self.proj_path, "added_at": older}]), \
             patch.object(self.pm, "_read_registry", return_value=[]), \
             patch.object(self.pm, "_read_project_config", return_value={}), \
             patch("services.project_manager.ActivityLog",
                   return_value=_StubActivityLog(events)):
            projects = self.pm.list_projects()
        self.assertEqual(projects[0]["session_count"], 2)
        self.assertEqual(projects[0]["live_session_id"], "s-new")


class TestRegistryReaping(unittest.TestCase):
    """Only session-launched UI servers are ever stopped automatically."""

    def setUp(self):
        self.pm = ProjectManager()
        self._tmp = tempfile.TemporaryDirectory()
        self.proj_path = self._tmp.name
        self.log = Path(self.proj_path) / ".c3" / "activity_log.jsonl"
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _entry(self, **extra) -> dict:
        entry = {"port": 4321, "project_path": self.proj_path,
                 "project_name": "Proj", "started_at": time.time() - 7200,
                 "owner": "session", "owner_session": "s1", "pid": 999999}
        entry.update(extra)
        return entry

    def _silent_for(self, seconds: float):
        when = time.time() - seconds
        os.utime(self.log, (when, when))

    def _sweep(self, entry):
        registry_file = Path(self.proj_path) / "registry.json"
        with patch.object(self.pm, "_read_registry", return_value=[entry]), \
             patch.object(self.pm, "_port_alive", return_value=True), \
             patch.object(self.pm, "_stop_entry", return_value=True) as stop, \
             patch("services.project_manager._REGISTRY_FILE", registry_file):
            return self.pm.sweep_registry(), stop

    def test_orphaned_session_ui_is_reaped(self):
        self._silent_for(3600)
        result, stop = self._sweep(self._entry())
        self.assertEqual(result["reaped"], [4321])
        stop.assert_called_once()

    def test_user_owned_ui_is_never_reaped(self):
        self._silent_for(3600)
        result, stop = self._sweep(self._entry(owner="user"))
        self.assertEqual(result["reaped"], [])
        stop.assert_not_called()

    def test_recent_activity_protects_the_ui(self):
        self._silent_for(60)
        result, _ = self._sweep(self._entry())
        self.assertEqual(result["reaped"], [])

    def test_a_live_session_protects_the_ui(self):
        self._silent_for(3600)
        session_live.beat(self.proj_path, "s1")
        result, _ = self._sweep(self._entry())
        self.assertEqual(result["reaped"], [])

    def test_reaping_can_be_disabled(self):
        self._silent_for(3600)
        with patch.object(self.pm, "_reap_minutes", return_value=0):
            result, stop = self._sweep(self._entry())
        self.assertEqual(result["reaped"], [])
        stop.assert_not_called()

    def test_dead_port_is_dropped_not_reaped(self):
        with patch.object(self.pm, "_read_registry", return_value=[self._entry()]), \
             patch.object(self.pm, "_port_alive", return_value=False), \
             patch.object(self.pm, "_stop_entry", return_value=True) as stop, \
             patch("services.project_manager._REGISTRY_FILE",
                   Path(self.proj_path) / "registry.json"):
            result = self.pm.sweep_registry()
        self.assertEqual(result, {"dropped": 1, "reaped": []})
        stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
