import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Log


class PipeView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Tool Pipeline", ""):
            yield Label("Executes a full context-to-answer pipeline.")
            with Horizontal(classes="input-row"):
                yield Label("Query: ", classes="input-label")
                yield Input(placeholder="How do I fix the TUI?", id="query_input")
            with Horizontal(classes="action-row"):
                yield Button("Run Pipeline", id="run_btn", variant="primary")

        with Card("Pipeline Output", ""):
            yield Log(id="output_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_btn":
            query = self.query_one("#query_input").value
            if not query: return
            self.query_one("#output_log").clear()
            self.run_pipe(query)

    @work(exclusive=True)
    async def run_pipe(self, query: str) -> None:
        c3_path, root_dir = get_c3_path()
        cmd = [sys.executable, c3_path, "pipe", query]
        env = os.environ.copy()
        env["PYTHONPATH"] = root_dir

        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        log_widget = self.query_one("#output_log")
        while True:
            line = await process.stdout.readline()
            if not line: break
            log_widget.write_line(line.decode(errors="replace").rstrip())
        await process.wait()
