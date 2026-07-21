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

from rich.cells import cell_len
from rich.segment import Segment
from textual.binding import Binding
from textual.message import Message
from textual.strip import Strip
from textual.widgets import Input, RichLog, SelectionList, Tree

if typing.TYPE_CHECKING:
    from rich.console import RenderableType
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

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Disable ``n``/``N`` until a search is active so screens can use them."""
        del parameters
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
    """RichLog with vim motions, search, and linewise visual yanking."""

    can_focus = True

    COMPONENT_CLASSES: typing.ClassVar[set[str]] = {
        "vimlog--search-match",
        "vimlog--visual-selection",
    }

    DEFAULT_CSS = """
    VimLog .vimlog--search-match {
        background: yellow 40%;
        text-style: bold;
    }
    VimLog .vimlog--visual-selection {
        background: $accent 40%;
    }
    """

    _search_needle: str = ""
    """Lowercased active search pattern; empty when no highlight should show."""

    BINDINGS: typing.ClassVar = [
        Binding("j", "vim_down", "Down", show=False),
        Binding("k", "vim_up", "Up", show=False),
        Binding("h", "scroll_left", "Left", show=False),
        Binding("l", "scroll_right", "Right", show=False),
        Binding("g", "vim_g", "Top (gg)", show=False),
        Binding("G", "vim_bottom", "Bottom", show=False),
        Binding("ctrl+d", "vim_half_page(1)", "Half page down", show=False),
        Binding("ctrl+u", "vim_half_page(-1)", "Half page up", show=False),
        Binding("v", "vim_visual", "Visual select", show=False),
        Binding("y", "vim_yank", "Yank", show=False),
        Binding("escape", "vim_cancel_visual", "Cancel selection", show=False),
        *_SEARCH_BINDINGS,
    ]

    def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        """Initialize visual selection and unwrapped yank state."""
        super().__init__(*args, **kwargs)
        self._visual_anchor: int | None = None
        self._visual_cursor: int | None = None
        self._written_text: list[str] = []

    @typing.override
    def write(
        self,
        content: RenderableType | object,
        width: int | None = None,
        expand: bool = False,
        shrink: bool = True,
        scroll_end: bool | None = None,
        animate: bool = False,
    ) -> typing.Self:
        """Write content and retain its unwrapped text for whole-log yanks."""
        if self._size_known:
            self._written_text.append(str(content))
        return super().write(content, width, expand, shrink, scroll_end, animate)

    def clear(self) -> typing.Self:
        """Clear content and any active visual selection."""
        self._written_text.clear()
        self._cancel_visual()
        return super().clear()

    def vim_go_top(self) -> None:
        """Scroll to the top, extending an active visual selection."""
        if self._visual_cursor is None:
            self.scroll_home(animate=False)
        else:
            self._move_visual_cursor(0)

    def action_vim_bottom(self) -> None:
        """Scroll to the bottom, extending an active visual selection."""
        if self._visual_cursor is None:
            self.scroll_end(animate=False)
        elif self.lines:
            self._move_visual_cursor(len(self.lines) - 1)

    def action_vim_down(self) -> None:
        """Scroll down, or extend a visual selection down one line."""
        if self._visual_cursor is None:
            self.scroll_down(animate=False)
        else:
            self._move_visual_cursor(self._visual_cursor + 1)

    def action_vim_up(self) -> None:
        """Scroll up, or extend a visual selection up one line."""
        if self._visual_cursor is None:
            self.scroll_up(animate=False)
        else:
            self._move_visual_cursor(self._visual_cursor - 1)

    def action_vim_half_page(self, direction: int) -> None:
        """Scroll half a page (``ctrl+d``/``ctrl+u``)."""
        step = max(1, self.scrollable_content_region.height // 2) * direction
        self.scroll_relative(y=step, animate=False)

    def action_vim_visual(self) -> None:
        """Enter or leave linewise visual mode at the first visible line."""
        if self._visual_anchor is not None:
            self._cancel_visual()
            return
        if not self.lines:
            return
        line = min(int(self.scroll_offset.y), len(self.lines) - 1)
        self._visual_anchor = self._visual_cursor = line
        self.refresh()

    def action_vim_cancel_visual(self) -> None:
        """Leave visual mode without copying."""
        self._cancel_visual()

    def action_vim_yank(self) -> None:
        """Copy the visual line range, or the entire log when none is selected."""
        bounds = self._visual_bounds
        if bounds is None:
            text = "\n".join(self._written_text)
        else:
            start, end = bounds
            text = "\n".join(self.lines[index].text.rstrip() for index in range(start, end + 1))
        if not text:
            return
        self.app.copy_to_clipboard(text)
        self.notify(f"Copied {text.count(chr(10)) + 1} line(s)", title="Yanked")
        self._cancel_visual()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Let screen-level escape bindings run unless visual mode is active."""
        if action == "vim_cancel_visual":
            return self._visual_anchor is not None
        return super().check_action(action, parameters)

    @property
    def _visual_bounds(self) -> tuple[int, int] | None:
        if self._visual_anchor is None or self._visual_cursor is None:
            return None
        start, end = sorted((self._visual_anchor, self._visual_cursor))
        return start, end

    def _move_visual_cursor(self, line: int) -> None:
        if not self.lines:
            return
        self._visual_cursor = max(0, min(line, len(self.lines) - 1))
        top = int(self.scroll_offset.y)
        height = max(1, self.scrollable_content_region.height)
        if self._visual_cursor < top:
            self.scroll_to(y=self._visual_cursor, animate=False)
        elif self._visual_cursor >= top + height:
            self.scroll_to(y=self._visual_cursor - height + 1, animate=False)
        self.refresh()

    def _cancel_visual(self) -> None:
        self._visual_anchor = None
        self._visual_cursor = None
        self.refresh()

    def vim_search(self, pattern: str) -> int:
        """Search and repaint so occurrences pick up the match highlight."""
        self._search_needle = pattern.lower()
        count = super().vim_search(pattern)
        self.refresh()
        return count

    def render_line(self, y: int) -> Strip:
        """Overlay search and visual-selection styles on a visible line."""
        strip = super().render_line(y)
        needle = self._search_needle
        text = strip.text
        lower = text.lower()
        if needle and needle in lower:
            style = self.get_component_rich_style("vimlog--search-match")
            spans: list[tuple[int, int]] = []
            start = lower.find(needle)
            while start != -1:
                end = start + len(needle)
                spans.append((cell_len(text[:start]), cell_len(text[:end])))
                start = lower.find(needle, end)
            pieces: list[Strip] = []
            position = 0
            for start_cell, end_cell in spans:
                if start_cell > position:
                    pieces.append(strip.crop(position, start_cell))
                match = strip.crop(start_cell, end_cell)
                segments = [
                    Segment(segment.text, style if segment.style is None else segment.style + style)
                    for segment in match
                ]
                pieces.append(Strip(segments, match.cell_length))
                position = end_cell
            if position < strip.cell_length:
                pieces.append(strip.crop(position, strip.cell_length))
            strip = Strip.join(pieces)

        bounds = self._visual_bounds
        line = int(self.scroll_offset.y) + y
        if bounds is not None and bounds[0] <= line <= bounds[1]:
            style = self.get_component_rich_style("vimlog--visual-selection")
            segments = [
                Segment(segment.text, style if segment.style is None else segment.style + style) for segment in strip
            ]
            strip = Strip(segments, strip.cell_length)
        return strip

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
