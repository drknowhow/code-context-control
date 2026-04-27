import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Log, Select


class MCPView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("MCP Server Management", ""):
            yield Label("Configure and register MCP servers.", classes="card-title")
            yield Label("Targets: Optional project path and/or IDE shorthand (e.g. . claude)", classes="info-text")
            with Horizontal(classes="input-row"):
                yield Label("Targets (Path/IDE): ", classes="input-label")
                yield Input(placeholder=". claude", id="targets_input")

            with Horizontal(classes="input-row"):
                yield Label("IDE:", classes="input-label")
                ide_options = [(ide, ide) for ide in ["auto", "claude", "vscode", "cursor", "codex", "gemini", "antigravity"]]
                yield Select(ide_options, value="auto", id="ide_select")
                yield Label("MCP Mode:", classes="input-label")
                yield Select([("direct", "direct"), ("proxy", "proxy")], value="direct", id="mcp_mode_select")

            with Horizontal(classes="action-row"):
                yield Button("Register Server", id="run_btn", variant="primary")

        with Card("Status", ""):
            yield Log(id="output_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_btn":
            targets_raw = self.query_one("#targets_input").value
            ide = self.query_one("#ide_select").value
            mcp_mode = self.query_one("#mcp_mode_select").value

            self.query_one("#output_log").clear()
            self.query_one("#run_btn").disabled = True

            args = []
            if ide and ide != "auto":
                args.extend(["--ide", ide])
            if mcp_mode and mcp_mode != "direct":
                args.extend(["--mcp-mode", mcp_mode])

            # Parse targets (space separated)
            targets = []
            if targets_raw:
                targets = targets_raw.split()

            self.run_mcp(args, targets)

    @work(exclusive=True)
    async def run_mcp(self, args: list, targets: list) -> None:
        c3_path, root_dir = get_c3_path()
        cmd_args = ["install-mcp"] + args + targets
        cmd = [sys.executable, c3_path] + cmd_args
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
