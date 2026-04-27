import os
import socket
import sys
import webbrowser

from backend import get_c3_path
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Label, Static


class Card(Vertical):
    def __init__(self, title: str, content: str, **kwargs):
        super().__init__(**kwargs)
        self.card_title = title
        self.card_content = content
        self.add_class("card")

    def compose(self) -> ComposeResult:
        yield Label(self.card_title, classes="card-title")
        if self.card_content:
            yield Static(self.card_content)

class StatsWidget(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Quick Actions", ""):
            yield Button("Project Init", id="quick_init_btn", variant="primary", classes="quick-btn")
            yield Button("MCP Install", id="quick_mcp_btn", variant="primary", classes="quick-btn")
            yield Button("Refresh Stats", id="refresh_stats_btn", classes="quick-btn")

        yield Label("System Metrics", classes="card-title")
        yield DataTable(id="stats-table")
        yield Button("Web UI Status: Checking...", id="web_ui_status_btn", classes="ui-btn-offline")
        yield Button("Terminate UI", id="terminate_ui_btn", variant="error", classes="quick-btn")

    def on_mount(self) -> None:
        table = self.query_one("#stats-table", DataTable)
        table.add_columns("Component", "Metric", "Value")
        table.cursor_type = "row"
        table.zebra_stripes = True

        self.query_one("#terminate_ui_btn").display = False

        self.load_stats()
        self.check_ui_status()
        self.set_interval(5.0, self.check_ui_status)

    @work(exclusive=True, thread=True)
    def check_ui_status(self) -> None:
        port = 3333
        is_open = False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            is_open = (s.connect_ex(('localhost', port)) == 0)

        self.app.call_from_thread(self._update_ui_btn, is_open, port)

    def _update_ui_btn(self, is_open: bool, port: int) -> None:
        btn = self.query_one("#web_ui_status_btn", Button)
        term_btn = self.query_one("#terminate_ui_btn", Button)
        if is_open:
            btn.label = f"Web UI: Running (Port {port})"
            btn.remove_class("ui-btn-offline")
            btn.add_class("ui-btn-online")
            term_btn.display = True
            self._current_ui_url = f"http://localhost:{port}"
        else:
            btn.label = "Web UI: Offline"
            btn.remove_class("ui-btn-online")
            btn.add_class("ui-btn-offline")
            term_btn.display = False
            self._current_ui_url = None

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "web_ui_status_btn" and getattr(self, "_current_ui_url", None):
            webbrowser.open(self._current_ui_url)
        elif event.button.id == "terminate_ui_btn":
            self.terminate_web_ui()
        elif event.button.id == "refresh_stats_btn":
            table = self.query_one("#stats-table", DataTable)
            table.clear()
            self.load_stats()
        elif event.button.id == "quick_init_btn":
            self.app.mount_view("init", "Init")
            self.app.query_one("#nav_list").index = 9
        elif event.button.id == "quick_mcp_btn":
            self.app.mount_view("mcp", "MCP")
            self.app.query_one("#nav_list").index = 10

    @work(exclusive=True, thread=True)
    def terminate_web_ui(self) -> None:
        port = 3333
        try:
            import subprocess
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            if sys.platform == "win32":
                result = subprocess.run(f"netstat -ano | findstr :{port}", shell=True, capture_output=True, text=True, **kwargs)
                lines = result.stdout.strip().split('\n')
                pids = set()
                for line in lines:
                    if f":{port}" in line and "LISTENING" in line:
                        parts = line.strip().split()
                        if len(parts) >= 5:
                            pids.add(parts[-1])
                for pid in pids:
                    subprocess.run(f"taskkill /PID {pid} /F", shell=True, capture_output=True, **kwargs)
            else:
                subprocess.run(f"lsof -ti:{port} | xargs kill -9", shell=True, capture_output=True)
            self.check_ui_status()
        except Exception:
            pass

    @work(exclusive=True, thread=True)
    def load_stats(self) -> None:
        try:
            import subprocess
            c3_path, root_dir = get_c3_path()
            env = os.environ.copy()
            env["PYTHONPATH"] = root_dir
            env["NO_COLOR"] = "1"
            env["TERM"] = "dumb"

            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                [sys.executable, c3_path, "stats"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                **kwargs
            )
            self.app.call_from_thread(self._render_stats, result.stdout)
        except Exception:
            pass

    def _render_stats(self, output: str) -> None:
        try:
            table = self.query_one("#stats-table", DataTable)
            table.clear()

            for line in output.splitlines():
                if not line.strip() or line.startswith("+") or line.startswith("System") or "C3 Statistics" in line:
                    continue
                parts = [p.strip() for p in line.split("|")]
                parts = [p for p in parts if p] # Remove empty splits

                if len(parts) >= 3 and parts[0] != "Component" and not parts[0].startswith("-"):
                    table.add_row(parts[0], parts[1], parts[2])
        except Exception:
            pass
