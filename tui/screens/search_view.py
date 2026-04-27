import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Log


class SearchView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Context Search", ""):
            with Horizontal(classes="input-row"):
                yield Label("Query: ", classes="input-label")
                yield Input(placeholder="e.g. how does auth work?", id="search_input")
            with Horizontal(classes="action-row"):
                yield Button("Search Context", id="search_btn", variant="primary")

        with Card("Search Results", ""):
            yield Log(id="search_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "search_btn":
            query = self.query_one("#search_input").value
            if not query:
                return
            self.query_one("#search_log").clear()
            self.query_one("#search_btn").disabled = True
            self.run_search(query)

    @work(exclusive=True)
    async def run_search(self, query: str) -> None:
        c3_path, root_dir = get_c3_path()
        env = os.environ.copy()
        env["PYTHONPATH"] = root_dir

        cmd = [sys.executable, c3_path, "context", query]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env
        )

        log_widget = self.query_one("#search_log")
        while True:
            line = await process.stdout.readline()
            if not line: break
            log_widget.write_line(line.decode(errors="replace").rstrip())

        await process.wait()
        self.query_one("#search_btn").disabled = False
