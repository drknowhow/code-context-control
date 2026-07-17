import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Log


class ClaudeMDView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Documentation Generator", ""):
            yield Label("Generates or updates CLAUDE.md / AGENTS.md instructions.")
            with Horizontal(classes="action-row"):
                yield Button("Generate CLAUDE.md", id="gen_btn", variant="primary")
                yield Button("Check Health", id="check_btn")

        with Card("Process Output", ""):
            yield Log(id="output_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        log = self.query_one("#output_log")
        log.clear()

        if event.button.id == "gen_btn":
            self.run_task("claudemd", "generate")
        elif event.button.id == "check_btn":
            self.run_task("claudemd", "check")

    @work(exclusive=True)
    async def run_task(self, *args) -> None:
        c3_path, root_dir = get_c3_path()
        cmd = [sys.executable, c3_path] + list(args)
        env = os.environ.copy()
        env["PYTHONPATH"] = root_dir

        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        log_widget = self.query_one("#output_log")
        while True:
            line = await process.stdout.readline()
            if not line: break
            log_widget.write_line(line.decode(errors="replace").rstrip())
        await process.wait()
