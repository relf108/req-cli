"""The req-cli Textual application shell."""

from __future__ import annotations

from textual.app import App

from req_cli.tui.gen_view import GenScreen
from req_cli.tui.run_view import RunScreen


class ReqApp(App[None]):
    """Lazygit-style TUI: run view by default, generate view on demand."""

    TITLE = "req"
    SCREENS = {  # noqa: RUF012 - textual class-level convention
        "run": RunScreen,
        "generate": GenScreen,
    }

    def on_mount(self) -> None:
        """Show the run view on startup."""
        self.push_screen("run")


def run() -> None:
    """Launch the TUI."""
    ReqApp().run()
