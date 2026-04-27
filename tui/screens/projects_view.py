"""Project Hub - central manager for all C3 projects and their sessions."""

import sys
import time
import webbrowser
from pathlib import Path

from rich.text import Text
from screens.stats import Card
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, Static


class ProjectsView(Vertical):
    """Central project tracker and session manager."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._projects: list = []
        self._view_mode = "table"
        self._selected_project_path: str | None = None

    def compose(self) -> ComposeResult:
        with Card("Add / Register Project", ""):
            with Horizontal(classes="proj-add-row"):
                yield Label("Path:", classes="input-label")
                yield Input(
                    placeholder="Absolute path to project  (e.g. C:/projects/myapp)",
                    id="proj_path_input",
                    classes="proj-path-input",
                )
                yield Label("Name:", classes="input-label")
                yield Input(
                    placeholder="Optional display name",
                    id="proj_name_input",
                    classes="proj-name-input",
                )
                yield Button("Add", id="add_btn", variant="primary")

        with Card("Registered Projects", ""):
            with Horizontal(classes="proj-toolbar-row"):
                yield Static(
                    "Browse projects as a table or a card grid.",
                    classes="info-text",
                )
                yield Button("List View", id="table_view_btn", variant="primary")
                yield Button("Grid View", id="grid_view_btn")
            yield DataTable(id="projects_table")
            yield Vertical(id="projects_grid")

        with Card("Session Actions", ""):
            with Horizontal(classes="proj-action-row"):
                yield Button("Refresh", id="refresh_btn")
                yield Button("Start Session", id="start_btn", variant="primary")
                yield Button("Open Browser", id="open_btn")
                yield Button("Stop Session", id="stop_btn", variant="error")
                yield Button("Remove Project", id="remove_btn")
            yield Static("Select a project row to manage it.", id="status_label")

    def on_mount(self) -> None:
        table = self.query_one("#projects_table", DataTable)
        table.add_columns("Name", "Path", "IDE", "Status", "Port", "Last Session", "Added")
        table.cursor_type = "row"
        table.zebra_stripes = True
        self.query_one("#projects_grid", Vertical).display = False
        self.load_projects()
        self.set_interval(15.0, self.load_projects)

    @work(exclusive=True, thread=True)
    def load_projects(self) -> None:
        try:
            pm = self._get_pm()
            projects = pm.list_projects()
            self.app.call_from_thread(self._render_projects, projects)
        except Exception as e:
            self.app.call_from_thread(
                self._set_status, f"[red]Error loading projects: {e}[/]"
            )

    def _render_projects(self, projects: list) -> None:
        self._projects = projects
        table = self.query_one("#projects_table", DataTable)
        grid = self.query_one("#projects_grid", Vertical)
        table.clear()

        if self._selected_project_path and not any(
            p.get("path") == self._selected_project_path for p in projects
        ):
            self._selected_project_path = None
        if not self._selected_project_path and projects:
            self._selected_project_path = projects[0].get("path")

        for project in projects:
            status = (
                Text("IN PROGRESS", style="bold green")
                if project.get("session_active")
                else Text("UI ACTIVE", style="bold green")
                if project.get("ui_active")
                else Text("stopped", style="dim #6B7280")
            )
            port_str = str(project["port"]) if project.get("port") else "-"
            last = (project.get("last_session") or "never")
            if last != "never":
                last = last[:10]
            added = (project.get("added_at") or "")[:10]
            path = project.get("path", "")
            display_path = ("..." + path[-37:]) if len(path) > 40 else path

            table.add_row(
                project.get("name", "?"),
                display_path,
                project.get("ide", "?"),
                status,
                port_str,
                last,
                added,
                key=project.get("path", ""),
            )

        grid.query("*").remove()
        if not projects:
            grid.mount(
                Static(
                    "No projects registered yet. Add one above to populate the hub.",
                    classes="projects-empty-state",
                )
            )
        else:
            for start in range(0, len(projects), 2):
                row = Horizontal(classes="project-grid-row")
                for index, project in enumerate(projects[start : start + 2], start=start):
                    row.mount(
                        Button(
                            self._project_card_label(project),
                            id=f"project_card__{index}",
                            classes="project-card",
                            variant=(
                                "primary"
                                if project.get("path") == self._selected_project_path
                                else "default"
                            ),
                        )
                    )
                grid.mount(row)

        active_count = sum(1 for p in projects if p.get("active"))
        self._set_status(
            f"{len(projects)} project(s) registered - "
            f"[bold green]{active_count} active[/] session(s). "
            f"{self._selection_hint()}"
        )
        self._sync_view_mode_buttons()
        self._update_view_visibility()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        btn_id = event.button.id

        if btn_id == "refresh_btn":
            self.load_projects()
            return

        if btn_id == "table_view_btn":
            self._view_mode = "table"
            self._sync_view_mode_buttons()
            self._update_view_visibility()
            self._set_status("[green]Projects view switched to list mode.[/]")
            return

        if btn_id == "grid_view_btn":
            self._view_mode = "grid"
            self._sync_view_mode_buttons()
            self._update_view_visibility()
            self._set_status("[green]Projects view switched to grid mode.[/]")
            return

        if btn_id == "add_btn":
            path = self.query_one("#proj_path_input", Input).value.strip()
            name = self.query_one("#proj_name_input", Input).value.strip() or None
            if not path:
                self._set_status("[yellow]Enter a project path first.[/]")
                return
            self._do_add(path, name)
            return

        if btn_id and btn_id.startswith("project_card__"):
            index = int(btn_id.split("__", 1)[1])
            if 0 <= index < len(self._projects):
                project = self._projects[index]
                self._selected_project_path = project.get("path")
                self._render_projects(self._projects)
                self._set_status(
                    f"[green]Selected [bold]{project.get('name', '?')}[/] in grid view.[/]"
                )
            return

        proj = self._selected_project()
        if proj is None:
            self._set_status(f"[yellow]{self._selection_hint()}[/]")
            return

        if btn_id == "start_btn":
            if proj.get("ui_active"):
                self._set_status(
                    f"[yellow]Session already running on port {proj['port']}.[/]"
                )
            else:
                self._do_launch(proj["path"])

        elif btn_id == "open_btn":
            if not proj.get("ui_active"):
                self._set_status("[yellow]No active session. Start one first.[/]")
            else:
                webbrowser.open(f"http://localhost:{proj['port']}")
                self._set_status(f"[green]Opened http://localhost:{proj['port']}[/]")

        elif btn_id == "stop_btn":
            if not proj.get("ui_active"):
                self._set_status("[yellow]No active session to stop.[/]")
            else:
                self._do_stop(proj["port"])

        elif btn_id == "remove_btn":
            self._do_remove(proj["path"])

    @work(exclusive=False, thread=True)
    def _do_add(self, path: str, name) -> None:
        try:
            pm = self._get_pm()
            entry = pm.add_project(path, name)
            self.app.call_from_thread(
                self._set_status,
                f"[green]Registered: [bold]{entry['name']}[/] - {entry['path']}[/]",
            )
            self.app.call_from_thread(self._clear_add_inputs)
            self.load_projects()
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"[red]Error: {e}[/]")

    @work(exclusive=False, thread=True)
    def _do_launch(self, path: str) -> None:
        try:
            pm = self._get_pm()
            ok = pm.launch_session(path)
            if ok:
                time.sleep(2.5)
                self.load_projects()
                self.app.call_from_thread(
                    self._set_status,
                    f"[green]Session launched for [bold]{path}[/][/]",
                )
            else:
                self.app.call_from_thread(
                    self._set_status, "[red]Failed to launch session.[/]"
                )
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"[red]Error: {e}[/]")

    @work(exclusive=False, thread=True)
    def _do_stop(self, port: int) -> None:
        try:
            pm = self._get_pm()
            pm.stop_session(port)
            time.sleep(1)
            self.load_projects()
            self.app.call_from_thread(
                self._set_status, f"[green]Session on port {port} stopped.[/]"
            )
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"[red]Error: {e}[/]")

    @work(exclusive=False, thread=True)
    def _do_remove(self, path: str) -> None:
        try:
            pm = self._get_pm()
            removed = pm.remove_project(path)
            msg = (
                f"[green]Removed project: {path}[/]"
                if removed
                else "[yellow]Project not found in registry.[/]"
            )
            self.app.call_from_thread(self._set_status, msg)
            self.load_projects()
        except Exception as e:
            self.app.call_from_thread(self._set_status, f"[red]Error: {e}[/]")

    def _get_pm(self):
        root = str(Path(__file__).parent.parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from services.project_manager import ProjectManager

        return ProjectManager()

    def _selected_project(self):
        if self._view_mode == "grid":
            if not self._selected_project_path:
                return None
            for project in self._projects:
                if project.get("path") == self._selected_project_path:
                    return project
            return None

        table = self.query_one("#projects_table", DataTable)
        idx = table.cursor_row
        if idx is not None and self._projects and idx < len(self._projects):
            return self._projects[idx]
        return None

    def _project_card_label(self, project: dict) -> str:
        if project.get("session_active"):
            status = "IN PROGRESS"
        elif project.get("ui_active"):
            status = "UI ACTIVE"
        else:
            status = "stopped"
        port = project.get("port") or "-"
        last = (project.get("last_session") or "never")[:10]
        added = (project.get("added_at") or "")[:10] or "unknown"
        return (
            f"{project.get('name', '?')}\n"
            f"{project.get('ide', '?').upper()}  {status}\n"
            f"Port: {port}    Last: {last}\n"
            f"{project.get('path', '')}\n"
            f"Added: {added}"
        )

    def _selection_hint(self) -> str:
        if self._view_mode == "grid":
            return "Select a card, then use the buttons below."
        return "Select a row, then use the buttons below."

    def _sync_view_mode_buttons(self) -> None:
        table_btn = self.query_one("#table_view_btn", Button)
        grid_btn = self.query_one("#grid_view_btn", Button)
        if self._view_mode == "table":
            table_btn.variant = "primary"
            grid_btn.variant = "default"
        else:
            table_btn.variant = "default"
            grid_btn.variant = "primary"

    def _update_view_visibility(self) -> None:
        table = self.query_one("#projects_table", DataTable)
        grid = self.query_one("#projects_grid", Vertical)
        table.display = self._view_mode == "table"
        grid.display = self._view_mode == "grid"

    def _set_status(self, msg: str) -> None:
        self.query_one("#status_label", Static).update(msg)

    def _clear_add_inputs(self) -> None:
        self.query_one("#proj_path_input", Input).value = ""
        self.query_one("#proj_name_input", Input).value = ""
