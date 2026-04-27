from datetime import datetime, timezone
from pathlib import Path

from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Log

from services.activity_log import ActivityLog
from services.session_manager import SessionManager


class SessionView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Recent Sessions", ""):
            yield Log(id="session_list_log", highlight=True)
            yield Button("Refresh List", id="refresh_btn")

        with Card("Active Session Log", ""):
            yield Log(id="active_log", highlight=True)

    def on_mount(self) -> None:
        self.load_sessions()
        self.set_interval(5.0, self.load_sessions)

    @work(exclusive=True, thread=True)
    def load_sessions(self) -> None:
        project_path = Path.cwd()
        session_mgr = SessionManager(str(project_path))
        sessions = session_mgr.list_sessions(10)
        current = self._current_session(project_path)
        self.app.call_from_thread(self._render_sessions, sessions, current)

    def _current_session(self, project_path: Path) -> dict | None:
        activity = ActivityLog(str(project_path))
        starts = activity.get_recent(limit=1, event_type="session_start")
        if not starts:
            return None
        start_event = starts[0]
        session_id = start_event.get("session_id")
        started = start_event.get("timestamp")
        if not session_id or not started:
            return None

        saves = activity.get_recent(limit=1, event_type="session_save")
        if saves and saves[0].get("session_id") == session_id:
            return None

        events = activity.get_recent(limit=200, since=started)
        tool_calls = [e for e in events if e.get("type") == "tool_call"]
        decisions = [e for e in events if e.get("type") == "decision"]
        files = [e for e in events if e.get("type") == "file_change"]
        last_event = events[0] if events else start_event
        try:
            started_dt = datetime.fromisoformat(started)
            duration = datetime.now(timezone.utc) - started_dt
            duration_seconds = max(0, int(duration.total_seconds()))
        except Exception:
            duration_seconds = 0

        return {
            "id": session_id,
            "started": started,
            "description": start_event.get("description", ""),
            "source_system": start_event.get("source_system", ""),
            "source_ide": start_event.get("source_ide", ""),
            "tool_calls": len(tool_calls),
            "decisions": len(decisions),
            "files": len(files),
            "last_activity": last_event.get("timestamp", started),
            "recent_events": list(reversed(events[:12])),
            "duration": self._format_duration(duration_seconds),
        }

    def _render_sessions(self, sessions: list, current: dict | None) -> None:
        session_log = self.query_one("#session_list_log", Log)
        active_log = self.query_one("#active_log", Log)
        session_log.clear()
        active_log.clear()

        if current:
            session_log.write_line(
                f"LIVE  {current['id']}  {current['duration']}  "
                f"{current['tool_calls']} tools  {current['decisions']} decisions  {current['files']} files"
            )
            if current.get("description"):
                session_log.write_line(f"      {current['description']}")
            session_log.write_line("")
            active_log.write_line(f"Session: {current['id']}")
            active_log.write_line(f"Started: {current['started']}")
            active_log.write_line(f"Last activity: {current['last_activity']}")
            active_log.write_line(
                f"Source: {current.get('source_system') or '-'} / {current.get('source_ide') or '-'}"
            )
            active_log.write_line(
                f"Activity: {current['tool_calls']} tools, {current['decisions']} decisions, {current['files']} files"
            )
            if current.get("description"):
                active_log.write_line(f"Description: {current['description']}")
            active_log.write_line("")
            active_log.write_line("Recent events:")
            for event in current["recent_events"]:
                label = event.get("type", "event")
                detail = (
                    event.get("tool")
                    or event.get("decision")
                    or event.get("file")
                    or event.get("data")
                    or ""
                )
                active_log.write_line(f"{event.get('timestamp', '')}  {label}  {detail}")
        else:
            active_log.write_line("No live session detected.")

        if sessions:
            if current:
                session_log.write_line("Saved sessions:")
            for session in sessions:
                status = "DONE" if session.get("ended") else "IDLE"
                summary = session.get("summary") or session.get("description") or ""
                session_log.write_line(
                    f"{status:<5} {session.get('id', '')}  "
                    f"{session.get('duration') or '-':<8}  "
                    f"{session.get('tool_calls', 0)} tools  "
                    f"{summary[:80]}"
                )
        elif not current:
            session_log.write_line("No session history found.")

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds}s"
        minutes, secs = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {secs}s" if secs else f"{minutes}m"
        hours, mins = divmod(minutes, 60)
        return f"{hours}h {mins}m" if mins else f"{hours}h"

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh_btn":
            self.load_sessions()
