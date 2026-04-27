import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Log


class OptimizeView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Context Optimization", ""):
            yield Label("Analyzes and optimizes the project index and context usage.")
            with Horizontal(classes="action-row"):
                yield Button("Run Optimization", id="run_btn", variant="primary")

        with Card("Analysis Report", ""):
            yield Log(id="output_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_btn":
            self.query_one("#output_log").clear()
            self.run_optimize()

    @work(exclusive=True)
    async def run_optimize(self) -> None:
        c3_path, root_dir = get_c3_path()
        cmd = [sys.executable, c3_path, "optimize"]
        env = os.environ.copy()
        env["PYTHONPATH"] = root_dir

        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        log_widget = self.query_one("#output_log")
        while True:
            line = await process.stdout.readline()
            if not line: break
            log_widget.write_line(line.decode(errors="replace").rstrip())
        await process.wait()
