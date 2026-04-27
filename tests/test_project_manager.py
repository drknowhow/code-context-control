import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
        with patch.object(self.pm, "_read_projects", return_value=[{"name": "Proj", "path": "/tmp/proj", "added_at": now}]), \
             patch.object(self.pm, "_read_registry", return_value=[{"project_path": "/tmp/proj", "port": None, "started_at": now}]), \
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
            "path": "/tmp/proj",
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
        with patch.object(self.pm, "_read_projects", return_value=[{"name": "Proj", "path": "/tmp/proj", "added_at": now}]), \
             patch.object(self.pm, "_read_registry", return_value=[]), \
             patch.object(self.pm, "_get_live_session_info", return_value=live_session), \
             patch.object(self.pm, "_read_project_config", return_value={}), \
             patch.object(self.pm, "_get_budget_info", return_value={"response_tokens": 123}):
            projects = self.pm.list_projects()
        self.assertTrue(projects[0]["session_active"])
        self.assertTrue(projects[0]["active"])
        self.assertIsNone(projects[0]["port"])
        self.assertEqual(projects[0]["live_session_id"], "sess-1")
        self.assertEqual(projects[0]["budget"], {"response_tokens": 123})

    def test_get_active_sessions_includes_live_session_without_ui_port(self):
        now = datetime.now(timezone.utc).isoformat()
        with patch.object(self.pm, "_read_registry", return_value=[]), \
             patch.object(self.pm, "list_projects", return_value=[{
                 "name": "Proj",
                 "path": "/tmp/proj",
                 "session_active": True,
                 "ui_active": False,
                 "started_at": now,
                 "live_session_id": "sess-2",
             }]):
            sessions = self.pm.get_active_sessions()
        self.assertEqual(sessions, [{
            "project_path": "/tmp/proj",
            "project_name": "Proj",
            "port": None,
            "started_at": now,
            "live_session_id": "sess-2",
        }])


if __name__ == "__main__":
    unittest.main()
