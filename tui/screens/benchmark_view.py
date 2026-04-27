import asyncio
import os
import sys

from backend import get_c3_path
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, Input, Label, Log


class BenchmarkView(Vertical):
    def compose(self) -> ComposeResult:
        with Card("Benchmark Configuration", ""):
            yield Label("Runs a series of tests to measure C3 performance.", classes="card-title")
            with Horizontal(classes="input-row"):
                yield Label("Path:", classes="input-label")
                yield Input(".", id="project_path")
                yield Label("Sample Size:", classes="input-label")
                yield Input("50", placeholder="e.g. 50", id="sample_size")

            with Horizontal(classes="input-row"):
                yield Label("Min Tokens:", classes="input-label")
                yield Input("100", placeholder="e.g. 100", id="min_tokens")
                yield Label("Top K:", classes="input-label")
                yield Input("5", placeholder="e.g. 5", id="top_k")

            with Horizontal(classes="input-row"):
                yield Label("Max Tokens:", classes="input-label")
                yield Input("10000", placeholder="e.g. 10000", id="max_tokens")
                yield Label("System Name:", classes="input-label")
                yield Input("c3", placeholder="e.g. codex", id="sys_name")

            with Horizontal(classes="input-row"):
                yield Label("Sys Label:", classes="input-label")
                yield Input(placeholder="e.g. OpenAI", id="sys_label")
                yield Label("Sys Version:", classes="input-label")
                yield Input(placeholder="e.g. v1.0", id="sys_version")

            with Horizontal(classes="input-row"):
                yield Label("JSON Out:", classes="input-label")
                yield Input(placeholder="out.json", id="out_json")
                yield Label("HTML Out:", classes="input-label")
                yield Input(placeholder="report.html", id="out_html")

            with Horizontal(classes="action-row"):
                yield Checkbox("Print JSON", id="json_check")
                yield Checkbox("No HTML", id="no_html_check")
                yield Button("Start Benchmark", id="run_btn", variant="primary")

        with Card("Results", ""):
            yield Log(id="output_log", highlight=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run_btn":
            self.query_one("#output_log").clear()
            self.query_one("#run_btn").disabled = True

            # Gather all kwargs
            args = []

            # Options with values
            val_map = {
                "--sample-size": "sample_size",
                "--min-tokens": "min_tokens",
                "--top-k": "top_k",
                "--max-tokens": "max_tokens",
                "--system-name": "sys_name",
                "--system-label": "sys_label",
                "--system-version": "sys_version",
                "--output": "out_json",
                "--html-output": "out_html"
            }

            for flag, widget_id in val_map.items():
                val = self.query_one(f"#{widget_id}").value
                if val:
                    args.extend([flag, val])

            # Flags
            if self.query_one("#json_check").value:
                args.append("--json")
            if self.query_one("#no_html_check").value:
                args.append("--no-html")

            # Positional path
            path = self.query_one("#project_path").value
            if path:
                args.append(path)

            self.run_benchmark(args)

    @work(exclusive=True)
    async def run_benchmark(self, args: list) -> None:
        c3_path, root_dir = get_c3_path()
        cmd = [sys.executable, c3_path, "benchmark"] + args
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
