import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Log


class IndexWidget(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Configuration", ""):
            with Horizontal(id="index-config-row"):
                yield Label("Max Files to Index: ", classes="input-label")
                # Empty by default: an unset cap falls through to
                # index_max_files in .c3/config.json (2000). Pre-filling a
                # number here silently truncated every index started from
                # the TUI on any repo larger than that number.
                yield Input("", placeholder="config default (2000)",
                            id="max_files_input")
            with Horizontal(id="index-actions-row"):
                yield Button("Start Indexing", id="start_btn", variant="primary")
                yield Button("Stop", id="stop_btn", variant="error")

        with Card("Status Output", ""):
            yield Log(id="index_log", highlight=True)

    def on_mount(self) -> None:
        self.process = None
        self.query_one("#stop_btn").disabled = True

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start_btn":
            max_files = self.query_one("#max_files_input").value.strip()
            self.query_one("#index_log").clear()
            self.query_one("#index_log").write_line(
                f"Starting indexer with max files: {max_files}..." if max_files
                else "Starting indexer (cap: index_max_files from .c3/config.json)...")
            self.query_one("#start_btn").disabled = True
            self.query_one("#stop_btn").disabled = False
            self.run_indexer(max_files)
        elif event.button.id == "stop_btn":
            if self.process:
                self.process.terminate()
            self.query_one("#start_btn").disabled = False
            self.query_one("#stop_btn").disabled = True
            self.query_one("#index_log").write_line("[bold red]Indexing stopped by user.[/]")

    @work(exclusive=True, thread=False)
    async def run_indexer(self, max_files: str) -> None:
        c3_path, root_dir = get_c3_path()
        env = os.environ.copy()
        env["PYTHONPATH"] = root_dir

        cmd = [sys.executable, c3_path, "index"]
        if max_files:
            cmd += ["--max-files", max_files]
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env
        )

        log_widget = self.query_one("#index_log")

        while True:
            line = await self.process.stdout.readline()
            if not line:
                break
            log_widget.write_line(line.decode(errors="replace").rstrip())

        await self.process.wait()

        # Only update if not already stopped/reset by the Stop button
        if self.process is not None:
            self.query_one("#start_btn").disabled = False
            self.query_one("#stop_btn").disabled = True
            log_widget.write_line("[bold green]Indexing completed.[/]")
            self.process = None
