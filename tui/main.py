import os

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Label, ListItem, ListView


class SidebarItem(ListItem):
    def __init__(self, label: str, view_id: str, **kwargs):
        super().__init__(**kwargs)
        self.label = label
        self.view_id = view_id

    def compose(self) -> ComposeResult:
        yield Label(self.label)

class C3App(App):
    CSS_PATH = "theme.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("tab", "toggle_focus", "Focus: NAV/CONTENT", show=True),
        Binding("ctrl+t", "toggle_theme", "Theme", show=True),
    ]

    def compose(self) -> ComposeResult:
        from screens.stats import StatsWidget
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield ListView(
                    SidebarItem("Projects", "projects"),
                    SidebarItem("Index", "index"),
                    SidebarItem("Search", "search"),
                    SidebarItem("Compress", "compress"),
                    SidebarItem("Session", "session"),
                    SidebarItem("ClaudeMD", "claudemd"),
                    SidebarItem("Pipe", "pipe"),
                    SidebarItem("Optimize", "optimize"),
                    SidebarItem("Init", "init"),
                    SidebarItem("MCP", "mcp"),
                    SidebarItem("Benchmark", "benchmark"),
                    SidebarItem("Web UI", "web_ui"),
                    id="nav_list"
                )
            with Container(id="content"):
                pass
            with Vertical(id="right-sidebar"):
                yield StatsWidget()
                yield Button("Theme: Dark", id="theme_toggle_btn", classes="quick-btn")
        yield Footer()
        yield Label(f" {os.getcwd()} ", id="cwd_label")

    def on_mount(self) -> None:
        self.title = "C3 COMPANION"
        self.sub_title = "v2.28.0 (Textual)"
        self.theme_mode = "dark"
        self.screen.add_class("theme-dark")
        self._sync_theme_button()

        # Open Projects hub by default
        nav_list = self.query_one("#nav_list", ListView)
        nav_list.index = 0
        self.mount_view("projects", "Projects")
        self.action_toggle_focus()

    def action_toggle_focus(self) -> None:
        nav_list = self.query_one("#nav_list")
        if self.focused == nav_list:
            self.query_one("#content").focus()
        else:
            nav_list.focus()

    def action_toggle_theme(self) -> None:
        if self.theme_mode == "dark":
            self.theme_mode = "light"
            self.screen.remove_class("theme-dark")
            self.screen.add_class("theme-light")
        else:
            self.theme_mode = "dark"
            self.screen.remove_class("theme-light")
            self.screen.add_class("theme-dark")
        self._sync_theme_button()
        self.notify(f"{self.theme_mode.title()} mode enabled", timeout=1.5)

    def _sync_theme_button(self) -> None:
        self.query_one("#theme_toggle_btn", Button).label = (
            f"Theme: {self.theme_mode.title()}"
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, SidebarItem):
            self.mount_view(item.view_id, item.label)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "theme_toggle_btn":
            self.action_toggle_theme()

    def mount_view(self, view_id: str, label: str) -> None:
        content_container = self.query_one("#content")
        # Remove existing view
        content_container.query("*").remove()

        # Mount new view
        if view_id == "projects":
            from screens.projects_view import ProjectsView
            content_container.mount(ProjectsView())
        elif view_id == "index":
            from screens.index_view import IndexWidget
            content_container.mount(IndexWidget())
        elif view_id == "search":
            from screens.search_view import SearchView
            content_container.mount(SearchView())
        elif view_id == "compress":
            from screens.compress_view import CompressView
            content_container.mount(CompressView())
        elif view_id == "session":
            from screens.session_view import SessionView
            content_container.mount(SessionView())
        elif view_id == "claudemd":
            from screens.claudemd_view import ClaudeMDView
            content_container.mount(ClaudeMDView())
        elif view_id == "pipe":
            from screens.pipe_view import PipeView
            content_container.mount(PipeView())
        elif view_id == "optimize":
            from screens.optimize_view import OptimizeView
            content_container.mount(OptimizeView())
        elif view_id == "init":
            from screens.init_view import InitView
            content_container.mount(InitView())
        elif view_id == "mcp":
            from screens.mcp_view import MCPView
            content_container.mount(MCPView())
        elif view_id == "benchmark":
            from screens.benchmark_view import BenchmarkView
            content_container.mount(BenchmarkView())
        elif view_id == "web_ui":
            from screens.ui_view import UIView
            content_container.mount(UIView())

if __name__ == "__main__":
    app = C3App()
    app.run()
