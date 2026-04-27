import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Input, Label, Log, Select


class InitView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Project Initialization", ""):
            yield Label("Initialize or update C3 in a project.", classes="card-title")
            yield Label("IDE sets the MCP config target. Force skips prompts. Clear removes C3 files.", classes="info-text")
            with Horizontal(classes="input-row"):
                yield Label("Path:", classes="input-label")
                yield Input(".", id="path_input")

            with Horizontal(classes="input-row"):
                yield Label("IDE:", classes="input-label")
                ide_options = [(ide, ide) for ide in ["auto", "claude", "vscode", "cursor", "codex", "gemini", "antigravity"]]
                yield Select(ide_options, value="auto", id="ide_select")
                yield Label("MCP Mode:", classes="input-label")
                yield Select([("direct", "direct"), ("proxy", "proxy")], value="direct", id="mcp_mode_select")

            with Horizontal(classes="input-row"):
                yield Checkbox("Force re-init", id="force_check")
                yield Checkbox("Clear all C3 files", id="clear_check")
                yield Checkbox("Init Git repo", id="git_check")

            with Horizontal(classes="action-row"):
                yield Button("Initialize", id="run_btn", variant="primary")

        with Card("Setup Logs", ""):
            yield Log(id="output_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_btn":
            path = self.query_one("#path_input").value
            force = self.query_one("#force_check").value
            clear = self.query_one("#clear_check").value
            git = self.query_one("#git_check").value
            ide = self.query_one("#ide_select").value
            mcp_mode = self.query_one("#mcp_mode_select").value

            self.query_one("#output_log").clear()
            self.query_one("#run_btn").disabled = True

            args = []
            if force: args.append("--force")
            if clear: args.append("--clear")
            if git: args.append("--git")
            if ide and ide != "auto":
                args.extend(["--ide", ide])
            if mcp_mode and mcp_mode != "direct":
                args.extend(["--mcp-mode", mcp_mode])

            self.run_init(path, args)

    @work(exclusive=True)
    async def run_init(self, path: str, additional_args: list) -> None:
        c3_path, root_dir = get_c3_path()
        args = ["init"] + additional_args
        if path:
            args.append(path)

        cmd = [sys.executable, c3_path] + args
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
