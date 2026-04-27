import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Input, Label, Log


class UIView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Web Dashboard Control", ""):
            with Horizontal(classes="input-row"):
                yield Label("Port: ", classes="input-label")
                yield Input("3333", id="port_input")
            with Horizontal(classes="input-row"):
                yield Checkbox("Nano Mode", id="nano_check")
            with Horizontal(classes="action-row"):
                yield Button("Launch Web UI", id="run_btn", variant="primary")

        with Card("Server Status", ""):
            yield Log(id="output_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_btn":
            port = self.query_one("#port_input").value or "3333"
            nano = self.query_one("#nano_check").value
            self.query_one("#output_log").clear()
            self.launch_ui(port, nano)

    @work(exclusive=True)
    async def launch_ui(self, port: str, nano: bool) -> None:
        c3_path, root_dir = get_c3_path()
        args = ["ui", "--port", port]
        if nano: args.append("--nano")
        args.append("--no-browser") # TUI handle manually or just show URL

        cmd = [sys.executable, c3_path] + args
        env = os.environ.copy()
        env["PYTHONPATH"] = root_dir

        log_widget = self.query_one("#output_log")
        log_widget.write_line(f"Starting Web UI on http://localhost:{port}...")

        # We don't wait for this one to finish as it's a server
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)

        while True:
            line = await process.stdout.readline()
            if not line: break
            log_widget.write_line(line.decode(errors="replace").rstrip())
