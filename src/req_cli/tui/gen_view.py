"""Generate view: pick a spec, select endpoint groups, preview, validate, write."""

from __future__ import annotations

import typing
from pathlib import Path

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Input, SelectionList, Static
from textual.widgets.selection_list import Selection

from req_cli.core.config import global_store_dir, project_requests_dir
from req_cli.core.validate import validate_source
from req_cli.gen.generate import generate_modules, write_module
from req_cli.gen.openapi import SpecError, app_name, load_spec, parse_endpoints
from req_cli.tui.vim import SearchInput, VimLog, VimSelectionList

if typing.TYPE_CHECKING:
    from textual.app import ComposeResult


class GenScreen(Screen[None]):
    """Spec path input on top; module selection left, generated preview right."""

    BINDINGS: typing.ClassVar = [
        Binding("w", "write_local", "Write local"),
        Binding("W", "write_global", "Write global"),
        Binding("escape", "back", "Back"),
        Binding("/", "open_search", "Search"),
        Binding("1", "focus_pane('module-list')", "Modules", show=False),
        Binding("2", "focus_pane('preview')", "Preview", show=False),
    ]

    CSS = """
    #inputs { dock: top; height: auto; }
    #app-name-input { display: none; }
    #app-name-input.visible { display: block; }
    #modules { width: 40%; max-width: 50; border-right: solid $accent; }
    #preview { padding: 0 1; }
    #gen-status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    #search-input { dock: bottom; display: none; }
    #search-input.visible { display: block; }
    """

    def __init__(self) -> None:
        """Start with no generated sources loaded."""
        super().__init__()
        self._sources: dict[str, str] = {}
        self._search_target: VimSelectionList | VimLog | None = None

    def compose(self) -> ComposeResult:
        """Build the layout: spec input on top, modules left, preview right."""
        yield Header(show_clock=False)
        with Vertical(id="inputs"):
            yield Input(
                placeholder="path to openapi spec (yaml/json), then Enter",
                id="spec-input",
            )
            yield Input(placeholder="app name for the global store", id="app-name-input")
        with Horizontal():
            with Vertical(id="modules"):
                yield VimSelectionList(id="module-list")
            yield VimLog(id="preview", highlight=True, markup=False)
        yield SearchInput(placeholder="/", id="search-input")
        yield Static(id="gen-status")
        yield Footer()

    def on_mount(self) -> None:
        """Focus the spec path input."""
        self.sub_title = "generate from openapi"
        self.query_one("#spec-input", Input).focus()

    def _set_status(self, text: str) -> None:
        self.query_one("#gen-status", Static).update(text)

    def action_open_search(self) -> None:
        """Open the vim `/` prompt for the focused pane (module list by default)."""
        focused = self.focused
        self._search_target = (
            focused
            if isinstance(focused, VimSelectionList | VimLog)
            else self.query_one("#module-list", VimSelectionList)
        )
        prompt = self.query_one("#search-input", SearchInput)
        prompt.value = ""
        prompt.add_class("visible")
        prompt.focus()

    def _close_search(self) -> None:
        self.query_one("#search-input", SearchInput).remove_class("visible")
        if self._search_target is not None:
            self._search_target.focus()

    def on_search_input_cancelled(self, _event: SearchInput.Cancelled) -> None:
        """Close the search prompt without searching."""
        self._close_search()

    def _submit_search(self, pattern: str) -> None:
        self._close_search()
        if self._search_target is None:
            return
        count = self._search_target.vim_search(pattern)
        self._set_status(f"{count} match(es) for {pattern!r} - n/N to cycle")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Route input submissions: search prompt, app name, or spec path."""
        if event.input.id == "search-input":
            self._submit_search(event.value)
            return
        if event.input.id == "app-name-input":
            self.query_one("#module-list", VimSelectionList).focus()
            return
        if event.input.id != "spec-input":
            return
        path = Path(event.value).expanduser()
        try:
            spec = load_spec(path)
        except SpecError as exc:
            self._set_status(str(exc))
            return
        app_input = self.query_one("#app-name-input", Input)
        app_input.value = app_name(spec)
        app_input.add_class("visible")
        endpoints = parse_endpoints(spec)
        self._sources = generate_modules(endpoints, path.name)
        selection_list = self.query_one("#module-list", VimSelectionList)
        selection_list.clear_options()
        for name, source in self._sources.items():
            count = source.count("\ndef ")
            selection_list.add_option(
                Selection(f"{name} ({count} request(s))", name, initial_state=True),
            )
        self._set_status(
            f"{len(endpoints)} endpoint(s) in {len(self._sources)} module(s) - space to toggle, w/W to write",
        )
        selection_list.focus()

    def on_selection_list_selection_highlighted(
        self,
        event: SelectionList.SelectionHighlighted[str],
    ) -> None:
        """Preview the generated source of the highlighted module."""
        preview = self.query_one("#preview", VimLog)
        preview.clear()
        name = event.selection.value
        preview.write(self._sources.get(name, ""))

    def action_focus_pane(self, pane_id: str) -> None:
        """Focus the pane with the given id (1/2 pane jumps)."""
        self.query_one(f"#{pane_id}").focus()

    def action_back(self) -> None:
        """Return to the run view."""
        self.app.pop_screen()

    def action_write_local(self) -> None:
        """Write the selected modules into ./requests."""
        self._write(project_requests_dir(), "local")

    def action_write_global(self) -> None:
        """Write the selected modules into an app folder in the global store."""
        app = self.query_one("#app-name-input", Input).value.strip()
        if not app:
            self._set_status("set an app name for the global store first (field under the spec path)")
            self.query_one("#app-name-input", Input).focus()
            return
        self._write(global_store_dir() / app, f"global ({app})")

    def _write(self, dest: Path, label: str) -> None:
        selection_list = self.query_one("#module-list", VimSelectionList)
        selected = list(selection_list.selected)
        if not selected:
            self._set_status("nothing selected")
            return
        written, skipped, invalid = [], [], []
        for name in selected:
            source = self._sources[name]
            report = validate_source(source, dest / f"{name}.py")
            if not report.ok:
                invalid.append(f"{name}: {'; '.join(str(i) for i in report.issues)}")
                continue
            try:
                written.append(write_module(source, dest, name))
            except FileExistsError:
                skipped.append(name)
        parts = [f"wrote {len(written)} module(s) to {label}"]
        if skipped:
            parts.append(f"skipped existing: {', '.join(skipped)}")
        if invalid:
            parts.append(f"failed validation: {'; '.join(invalid)}")
        self._set_status(" | ".join(parts))
        if written:
            names = ", ".join(path.stem for path in written)
            self.notify(
                f"{names} -> {dest}",
                title=f"{len(written)} request module(s) saved",
            )
        if skipped or invalid:
            problems = len(skipped) + len(invalid)
            self.notify(
                " | ".join(parts[1:]),
                title=f"{problems} module(s) not written",
                severity="warning",
            )
