"""Run view: browse saved request modules (local + global panes) and send them."""

from __future__ import annotations

import os
import shlex
import subprocess
import typing
from dataclasses import dataclass
from pathlib import Path

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static
from textual.worker import get_current_worker

from req_cli.core import discovery
from req_cli.core.client import Result, set_profile_override
from req_cli.core.config import Config, ConfigError, ensure_config_file, load_config
from req_cli.core.render import render_result
from req_cli.tui.vim import SearchInput, VimLog, VimTree

if typing.TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widgets.tree import TreeNode

_LINE_JUMP_EDITORS = frozenset({"vi", "vim", "nvim", "nano", "micro", "kak", "emacs"})
"""Editors known to accept a ``+<line>`` argument."""


@dataclass(frozen=True)
class _FunctionRef:
    module: discovery.RequestModule
    function: discovery.RequestFunction


class RunScreen(Screen[None]):
    """Lazygit-style layout: request tree on the left, response on the right."""

    BINDINGS: typing.ClassVar = [
        Binding("enter", "send", "Send"),
        Binding("escape", "cancel_send", "Cancel send", show=False),
        Binding("e", "edit_request", "Edit"),
        Binding("/", "open_search", "Search"),
        Binding("r", "refresh", "Refresh"),
        Binding("p", "cycle_profile", "Profile"),
        Binding("P", "back_cycle_profile", "Profile"),
        Binding("n", "generate", "Generate"),
        Binding("c", "edit_config('local')", "Config"),
        Binding("C", "edit_config('global')", "Global config", show=False),
        Binding("1", "focus_pane('request-tree')", "Requests", show=False),
        Binding("2", "focus_pane('response')", "Response", show=False),
        Binding("q", "app.quit", "Quit"),
    ]

    CSS = """
    #sidebar { width: 45%; max-width: 60; border-right: solid $accent; }
    #response { padding: 0 1; }
    #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
    #search-input { dock: bottom; display: none; }
    #search-input.visible { display: block; }
    """

    def __init__(self) -> None:
        """Load config and profile state for the session."""
        super().__init__()
        self._config: Config = load_config()
        self._profile_names = sorted(self._config.profiles)
        self._profile = self._config.default_profile
        self._search_target: VimTree | VimLog | None = None

    def compose(self) -> ComposeResult:
        """Build the two-pane layout: request tree left, response log right."""
        yield Header(show_clock=False)
        with Horizontal():
            with Vertical(id="sidebar"):
                yield VimTree("requests", id="request-tree")
            yield VimLog(id="response", highlight=True, markup=False, wrap=True)
        yield SearchInput(placeholder="/", id="search-input")
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        """Apply the default profile and populate the tree."""
        self.sub_title = f"profile: {self._profile}"
        set_profile_override(self._profile)
        self._reload_tree()

    def action_focus_pane(self, pane_id: str) -> None:
        """Focus the pane with the given id (1/2 pane jumps)."""
        self.query_one(f"#{pane_id}").focus()

    def action_open_search(self) -> None:
        """Open the vim `/` prompt for the focused pane (tree by default)."""
        focused = self.focused
        self._search_target = (
            focused if isinstance(focused, VimTree | VimLog) else self.query_one("#request-tree", VimTree)
        )
        prompt = self.query_one("#search-input", SearchInput)
        prompt.value = ""
        prompt.add_class("visible")
        prompt.focus()

    def _close_search(self) -> None:
        self.query_one("#search-input", SearchInput).remove_class("visible")
        if self._search_target is not None:
            self._search_target.focus()

    def on_input_submitted(self, event: SearchInput.Submitted) -> None:
        """Run the search against the targeted pane and jump to the first hit."""
        if event.input.id != "search-input":
            return
        self._close_search()
        if self._search_target is None:
            return
        count = self._search_target.vim_search(event.value)
        self._set_status(f"{count} match(es) for {event.value!r} - n/N to cycle")

    def on_search_input_cancelled(self, _event: SearchInput.Cancelled) -> None:
        """Close the search prompt without searching."""
        self._close_search()

    def _reload_tree(self) -> None:
        tree = self.query_one("#request-tree", VimTree)
        tree.clear()
        tree.show_root = False
        modules = discovery.discover()
        scopes: dict[str, TreeNode[object]] = {
            "local": tree.root.add("local ./requests", expand=True),
            "global": tree.root.add("global store", expand=True),
        }
        counts = {"local": 0, "global": 0}
        app_nodes: dict[str, TreeNode[object]] = {}
        for module in modules:
            parent = scopes[module.scope]
            label = module.name
            if module.scope == "global" and "/" in module.name:
                app, _, label = module.name.partition("/")
                if app not in app_nodes:
                    app_nodes[app] = parent.add(app, expand=True)
                parent = app_nodes[app]
            node = parent.add(label, expand=True)
            node.data = module
            counts[module.scope] += 1
            for function in module.functions:
                label = function.name
                if function.docstring:
                    label += f"  [dim]{function.docstring.splitlines()[0]}[/dim]"
                leaf = node.add_leaf(label)
                leaf.data = _FunctionRef(module, function)
        for scope, root in scopes.items():
            if not counts[scope]:
                root.add_leaf("<empty>")
        self._set_status(f"{len(modules)} module(s) discovered")

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def action_refresh(self) -> None:
        """Re-discover request modules."""
        self._reload_tree()

    def action_generate(self) -> None:
        """Open the generate-from-openapi view."""
        self.app.push_screen("generate")

    def _open_in_editor(self, path: Path, line: int = 0) -> None:
        """Suspend the TUI and open *path* in $EDITOR, optionally at *line*."""
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
        command = [*shlex.split(editor)]
        if line and Path(command[0]).name in _LINE_JUMP_EDITORS:
            command.append(f"+{line}")
        command.append(str(path))
        try:
            with self.app.suspend():
                subprocess.run(command, check=False)  # noqa: S603
        except Exception as exc:  # noqa: BLE001 - e.g. suspend unsupported by driver
            self.notify(str(exc), title="cannot open editor", severity="error")

    def action_edit_request(self) -> None:
        """Open the module under the cursor in $EDITOR, at the function line."""
        tree = self.query_one("#request-tree", VimTree)
        node = tree.cursor_node
        data = node.data if node is not None else None
        if isinstance(data, _FunctionRef):
            self._open_in_editor(data.module.path, data.function.lineno)
        elif isinstance(data, discovery.RequestModule):
            self._open_in_editor(data.path)
        else:
            self._set_status("select a request or module to edit")
            return
        self._reload_tree()
        self._set_status("edited - request list reloaded")

    def action_edit_config(self, scope: str) -> None:
        """Open the local/global config.toml in $EDITOR, then reload profiles."""
        path = ensure_config_file(scope)
        self._open_in_editor(path)
        self._reload_config(scope, path)

    def _reload_config(self, scope: str, path: object) -> None:
        try:
            self._config = load_config()
        except ConfigError as exc:
            self.notify(str(exc), title="config error", severity="error")
            return
        self._profile_names = sorted(self._config.profiles)
        if self._profile not in self._profile_names:
            self._profile = self._config.default_profile
        set_profile_override(self._profile)
        self.sub_title = f"profile: {self._profile}"
        self.notify(
            f"{len(self._profile_names)} profile(s): {', '.join(self._profile_names)}",
            title=f"{scope} config reloaded",
        )
        self._set_status(f"config reloaded from {path}")

    def action_cycle_profile(self) -> None:
        """Switch to the next configured profile."""
        if len(self._profile_names) <= 1:
            self._set_status(
                f"only one profile configured ({self._profile}) - add [profiles.<name>] "
                "entries to requests/config.toml or ~/.config/req-cli/config.toml",
            )
            return
        previous = self._profile
        index = self._profile_names.index(self._profile)
        self._profile = self._profile_names[(index + 1) % len(self._profile_names)]
        set_profile_override(self._profile)
        self.sub_title = f"profile: {self._profile}"
        self._set_status(f"profile: {previous} -> {self._profile}")

    def action_back_cycle_profile(self) -> None:
        """Switch to the previous configured profile."""
        if len(self._profile_names) <= 1:
            self._set_status(
                f"only one profile configured ({self._profile}) - add [profiles.<name>] "
                "entries to requests/config.toml or ~/.config/req-cli/config.toml",
            )
            return
        previous = self._profile
        index = self._profile_names.index(self._profile)
        self._profile = self._profile_names[(index - 1) % len(self._profile_names)]
        set_profile_override(self._profile)
        self.sub_title = f"profile: {self._profile}"
        self._set_status(f"profile: {previous} -> {self._profile}")

    def on_tree_node_selected(self, event: VimTree.NodeSelected[object]) -> None:
        """Send the selected leaf (mouse click, or vim `l`)."""
        if isinstance(event.node.data, _FunctionRef):
            self.action_send()

    def action_send(self) -> None:
        """Send the request function under the cursor in a worker thread."""
        tree = self.query_one("#request-tree", VimTree)
        node = tree.cursor_node
        ref = node.data if node is not None else None
        if not isinstance(ref, _FunctionRef):
            return
        self.query_one("#response", VimLog).loading = True
        self._set_status(f"sending {ref.module.name}.{ref.function.name} ... (esc to cancel)")
        self.run_worker(
            lambda: self._send(ref),
            thread=True,
            exclusive=True,
            group="send",
        )

    def action_cancel_send(self) -> None:
        """
        Abandon the in-flight request (``escape``).

        The worker thread cannot interrupt a blocked socket read, so the
        request keeps running in the background; its response is discarded
        when it eventually lands.
        """
        if not self.app.workers.cancel_group(self, "send"):
            return
        self.query_one("#response", VimLog).loading = False
        self._set_status("send cancelled - response discarded")

    def _send(self, ref: _FunctionRef) -> None:
        log = self.query_one("#response", VimLog)
        worker = get_current_worker()
        try:
            func = discovery.load_function(ref.module.path, ref.function.name)
            result = func()
        except Exception as exc:  # noqa: BLE001 - report any failure in the UI, never crash
            if not worker.is_cancelled:
                self.app.call_from_thread(self._show_error, ref, str(exc))
            return
        if worker.is_cancelled:
            return
        if not isinstance(result, Result):
            self.app.call_from_thread(
                self._show_error,
                ref,
                f"function returned {type(result).__name__}, not Result",
            )
            return
        self.app.call_from_thread(self._show_result, ref, result, log)

    def _show_result(self, ref: _FunctionRef, result: Result, log: VimLog) -> None:
        log.loading = False
        log.clear()
        log.write(render_result(result))
        status = "ok" if result.ok else "failed"
        self._set_status(f"{ref.module.name}.{ref.function.name}: {status} ({result.elapsed:.3f}s)")

    def _show_error(self, ref: _FunctionRef, message: str) -> None:
        log = self.query_one("#response", VimLog)
        log.loading = False
        log.clear()
        log.write(f"error running {ref.module.name}.{ref.function.name}:\n{message}")
        self._set_status("error")
