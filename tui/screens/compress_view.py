import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Log, Select


class CompressView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Token Efficiency", ""):
            with Horizontal(classes="input-row"):
                yield Label("File Path: ", classes="input-label")
                yield Input(placeholder="path/to/file.py", id="file_input")
            with Horizontal(classes="input-row"):
                yield Label("Mode: ", classes="input-label")
                yield Select([("Smart", "smart"), ("Map", "map"), ("Dense Map", "dense_map"), ("Diff", "diff")], value="smart", id="mode_select")
            with Horizontal(classes="action-row"):
                yield Button("Compress File", id="run_btn", variant="primary")

        with Card("Compressed Output", ""):
            yield Log(id="output_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_btn":
            path = self.query_one("#file_input").value
            mode = self.query_one("#mode_select").value
            if not path: return

            self.query_one("#output_log").clear()
            self.query_one("#run_btn").disabled = True
            self.run_compress(path, mode)

    @work(exclusive=True)
    async def run_compress(self, path: str, mode: str) -> None:
        c3_path, root_dir = get_c3_path()
        cmd = [sys.executable, c3_path, "compress", path, "--mode", mode]
        env = os.environ.copy()
        env["PYTHONPATH"] = root_dir

        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
        log_widget = self.query_one("#output_log")
        while True:
            line = await process.stdout.readline()
            if not line: break
            log_widget.write_line(line.decode(errors="replace").rstrip())
        await process.wait()
        self.query_one("#run_btn").disabled = False
