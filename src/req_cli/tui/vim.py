"""
Vim motions as first-class citizens for the TUI widgets.

Every navigable widget gets the standard set:

- ``j`` / ``k``            - down / up
- ``h`` / ``l``            - collapse/parent, expand/descend (tree); left/right scroll (log)
- ``gg`` / ``G``           - top / bottom
- ``ctrl+d`` / ``ctrl+u``  - half page down / up
- ``/`` then ``n`` / ``N`` - search, next / previous match

Arrow keys and the default Textual bindings keep working alongside. The
``n``/``N`` bindings are disabled until a search is active, so ``n`` stays
free for screen-level actions (e.g. Generate) until you search.
"""

from __future__ import annotations

import time
import typing

from textual.binding import Binding
from textual.message import Message
from textual.widgets import Input, RichLog, SelectionList, Tree

if typing.TYPE_CHECKING:
    from textual.widgets.tree import TreeNode

GG_INTERVAL = 0.6
"""Max seconds between the two ``g`` presses of a ``gg`` motion."""

_SEARCH_BINDINGS = [
    Binding("n", "vim_search_next", "Next match", show=False),
    Binding("N", "vim_search_prev", "Previous match", show=False),
]


class SearchInput(Input):
    """The ``/`` prompt; escape cancels back to the searched pane."""

    class Cancelled(Message):
        """Posted when the user aborts the search prompt."""

    BINDINGS: typing.ClassVar = [Binding("escape", "cancel", "Cancel search", show=False)]

    def action_cancel(self) -> None:
        """Abort the search prompt."""
        self.post_message(self.Cancelled())


class _GgMixin:
    """Tracks double-``g`` presses; subclasses implement :meth:`vim_go_top`."""

    _last_g: float = 0.0

    def action_vim_g(self) -> None:
        """Register a ``g`` press; two within the interval go to the top."""
        now = time.monotonic()
        if now - self._last_g <= GG_INTERVAL:
            self._last_g = 0.0
            self.vim_go_top()
        else:
            self._last_g = now

    def vim_go_top(self) -> None:
        raise NotImplementedError


class _SearchMixin:
    """Vim ``/`` search state; subclasses implement matching and jumping."""

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        super().__init__(*args, **kwargs)
        self._search_matches: list[typing.Any] = []
        self._search_index: int = -1

    def vim_search(self, pattern: str) -> int:
        """Search for *pattern* (case-insensitive), jump to the first match."""
        self._search_matches = self._find_matches(pattern.lower()) if pattern else []
        self._search_index = -1
        if self._search_matches:
            self.action_vim_search_next()
        return len(self._search_matches)

    def action_vim_search_next(self) -> None:
        """Jump to the next match (``n``)."""
        self._step_match(1)

    def action_vim_search_prev(self) -> None:
        """Jump to the previous match (``N``)."""
        self._step_match(-1)

    def _step_match(self, delta: int) -> None:
        if not self._search_matches:
            return
        self._search_index = (self._search_index + delta) % len(self._search_matches)
        self._go_to_match(self._search_matches[self._search_index])

    def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
        """Disable ``n``/``N`` until a search is active so screens can use them."""
        if action in ("vim_search_next", "vim_search_prev"):
            return bool(self._search_matches)
        return True

    def _find_matches(self, needle: str) -> list[typing.Any]:
        raise NotImplementedError

    def _go_to_match(self, match: typing.Any) -> None:
        raise NotImplementedError


class VimTree(_GgMixin, _SearchMixin, Tree[object]):
    """Tree with vim motions: j/k move, h collapse/up, l expand/descend/select."""

    BINDINGS: typing.ClassVar = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("h", "vim_left", "Collapse", show=False),
        Binding("l", "vim_right", "Expand", show=False),
        Binding("g", "vim_g", "Top (gg)", show=False),
        Binding("G", "vim_bottom", "Bottom", show=False),
        Binding("ctrl+d", "vim_half_page(1)", "Half page down", show=False),
        Binding("ctrl+u", "vim_half_page(-1)", "Half page up", show=False),
        *_SEARCH_BINDINGS,
    ]

    def vim_go_top(self) -> None:
        """Move the cursor to the first line (``gg``)."""
        self.cursor_line = 0
        self.scroll_home(animate=False)

    def action_vim_bottom(self) -> None:
        """Move the cursor to the last line (``G``)."""
        self.cursor_line = self.last_line
        self.scroll_end(animate=False)

    def action_vim_half_page(self, direction: int) -> None:
        """Move the cursor half a page (``ctrl+d``/``ctrl+u``)."""
        step = max(1, self.scrollable_content_region.height // 2) * direction
        self.cursor_line = max(0, min(self.last_line, self.cursor_line + step))
        self.scroll_to_line(self.cursor_line, animate=False)

    def action_vim_left(self) -> None:
        """Collapse the current node, or move to its parent (``h``)."""
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded and node.children and node != self.root:
            node.collapse()
        else:
            self.action_cursor_parent()

    def action_vim_right(self) -> None:
        """Expand/descend into the current node; on a leaf, select it (``l``)."""
        node = self.cursor_node
        if node is None:
            return
        if node.children and not node.is_expanded:
            node.expand()
        elif node.children:
            self.action_cursor_down()
        else:
            self.action_select_cursor()

    def _find_matches(self, needle: str) -> list[typing.Any]:
        found: list[TreeNode[object]] = []

        def walk(node: TreeNode[object]) -> None:
            for child in node.children:
                if needle in str(child.label).lower():
                    found.append(child)
                walk(child)

        walk(self.root)
        return list(found)

    def _go_to_match(self, match: typing.Any) -> None:
        node: TreeNode[object] = match
        parent = node.parent
        while parent is not None:
            parent.expand()
            parent = parent.parent
        self.move_cursor(node)
        self.scroll_to_line(self.cursor_line, animate=False)


class VimLog(_GgMixin, _SearchMixin, RichLog):
    """RichLog with vim motions; focusable so panes can be scrolled directly."""

    can_focus = True

    BINDINGS: typing.ClassVar = [
        Binding("j", "scroll_down", "Down", show=False),
        Binding("k", "scroll_up", "Up", show=False),
        Binding("h", "scroll_left", "Left", show=False),
        Binding("l", "scroll_right", "Right", show=False),
        Binding("g", "vim_g", "Top (gg)", show=False),
        Binding("G", "scroll_end", "Bottom", show=False),
        Binding("ctrl+d", "vim_half_page(1)", "Half page down", show=False),
        Binding("ctrl+u", "vim_half_page(-1)", "Half page up", show=False),
        *_SEARCH_BINDINGS,
    ]

    def vim_go_top(self) -> None:
        """Scroll to the top (``gg``)."""
        self.scroll_home(animate=False)

    def action_vim_half_page(self, direction: int) -> None:
        """Scroll half a page (``ctrl+d``/``ctrl+u``)."""
        step = max(1, self.scrollable_content_region.height // 2) * direction
        self.scroll_relative(y=step, animate=False)

    def _find_matches(self, needle: str) -> list[typing.Any]:
        return [index for index, strip in enumerate(self.lines) if needle in strip.text.lower()]

    def _go_to_match(self, match: typing.Any) -> None:
        self.scroll_to(y=match, animate=False)


class VimSelectionList(_GgMixin, _SearchMixin, SelectionList[str]):
    """SelectionList with vim motions; space still toggles, enter still selects."""

    BINDINGS: typing.ClassVar = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("g", "vim_g", "Top (gg)", show=False),
        Binding("G", "last", "Bottom", show=False),
        Binding("ctrl+d", "page_down", "Page down", show=False),
        Binding("ctrl+u", "page_up", "Page up", show=False),
        *_SEARCH_BINDINGS,
    ]

    def vim_go_top(self) -> None:
        """Highlight the first option (``gg``)."""
        self.action_first()

    def _find_matches(self, needle: str) -> list[typing.Any]:
        return [
            index for index in range(self.option_count) if needle in str(self.get_option_at_index(index).prompt).lower()
        ]

    def _go_to_match(self, match: typing.Any) -> None:
        self.highlighted = match
