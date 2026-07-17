"""Smoke tests for the Textual TUI via the pilot."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
import requests

from req_cli.__main__ import main
from req_cli.tui.app import ReqApp
from req_cli.tui.gen_view import GenScreen
from req_cli.tui.run_view import RunScreen


@pytest.fixture
def project_with_requests(spec_path: Path, isolated_dirs: dict[str, Path]) -> dict[str, Path]:
    assert main(["gen", str(spec_path)]) == 0
    return isolated_dirs


async def test_run_screen_lists_modules(project_with_requests: dict[str, Path]) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        assert isinstance(app.screen, RunScreen)
        tree = app.screen.query_one("#request-tree")
        labels = [str(node.label) for node in tree.root.children]
        assert any("local" in label for label in labels)
        local_names = [str(child.label) for child in tree.root.children[0].children]
        assert local_names == ["details", "items", "status"]
        await pilot.pause()


async def test_send_from_tree(
    project_with_requests: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'
        url = "http://127.0.0.1:8000/x"
        headers: dict[str, str] = {}  # noqa: RUF012

        def json(self) -> Any:
            return {"ok": True}

    monkeypatch.setattr(requests, "request", lambda **kwargs: FakeResponse())
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        tree = screen.query_one("#request-tree")
        # local -> items -> get_item
        items = next(node for node in tree.root.children[0].children if "items" in str(node.label))
        function_node = items.children[0]
        tree.select_node(function_node)
        tree.focus()
        await pilot.press("enter")
        await pilot.pause(delay=0.2)
        status = str(screen.query_one("#status").render())
        assert "ok" in status


async def test_send_shows_throbber_and_escape_cancels(
    project_with_requests: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'
        url = "http://127.0.0.1:8000/x"
        headers: dict[str, str] = {}  # noqa: RUF012

        def json(self) -> Any:
            return {"ok": True}

    def slow_request(**_kwargs: Any) -> FakeResponse:
        release.wait(timeout=5)
        return FakeResponse()

    monkeypatch.setattr(requests, "request", slow_request)
    app = ReqApp()
    try:
        async with app.run_test() as pilot:
            screen = app.screen
            assert isinstance(screen, RunScreen)
            tree = screen.query_one("#request-tree")
            items = next(node for node in tree.root.children[0].children if "items" in str(node.label))
            tree.move_cursor(items.children[0])
            tree.focus()
            await pilot.press("enter")
            await pilot.pause()
            log = screen.query_one("#response")
            assert log.loading
            assert "esc to cancel" in str(screen.query_one("#status").render())
            # escape cancels: throbber goes away, UI is usable again
            await pilot.press("escape")
            await pilot.pause()
            assert not log.loading
            assert "cancelled" in str(screen.query_one("#status").render())
            # the abandoned worker's late response must not clobber the UI
            release.set()
            await pilot.pause(delay=0.2)
            assert "cancelled" in str(screen.query_one("#status").render())
            # escape with nothing in flight is a no-op
            await pilot.press("escape")
            assert "cancelled" in str(screen.query_one("#status").render())
    finally:
        release.set()


async def test_profile_cycle_and_gen_screen(
    project_with_requests: dict[str, Path],
    spec_path: Path,
) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        screen.query_one("#request-tree").focus()
        await pilot.press("p")
        assert "profile" in str(screen.query_one("#status").render())

        await pilot.press("n")
        await pilot.pause()
        gen = app.screen
        assert isinstance(gen, GenScreen)

        spec_input = gen.query_one("#spec-input")
        spec_input.focus()
        spec_input.value = str(spec_path)
        await pilot.press("enter")
        await pilot.pause()
        status = str(gen.query_one("#gen-status").render())
        assert "3 endpoint(s)" in status

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, RunScreen)


async def test_gen_screen_write_local(spec_path: Path, isolated_dirs: dict[str, Path]) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        run_screen = app.screen
        assert isinstance(run_screen, RunScreen)
        run_screen.query_one("#request-tree").focus()
        await pilot.press("n")
        await pilot.pause()
        gen = app.screen
        assert isinstance(gen, GenScreen)
        spec_input = gen.query_one("#spec-input")
        spec_input.focus()
        spec_input.value = str(spec_path)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        status = str(gen.query_one("#gen-status").render())
        assert "wrote 3 module(s) to local" in status
        written = sorted(p.name for p in (isolated_dirs["project"] / "requests").glob("*.py"))
        assert written == ["details.py", "items.py", "status.py"]


async def test_vim_tree_motions(project_with_requests: dict[str, Path]) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        tree = screen.query_one("#request-tree")
        tree.focus()
        start = tree.cursor_line
        await pilot.press("j", "j")
        assert tree.cursor_line == start + 2
        await pilot.press("k")
        assert tree.cursor_line == start + 1
        # G to bottom, gg back to top
        await pilot.press("G")
        assert tree.cursor_line == tree.last_line
        await pilot.press("g", "g")
        assert tree.cursor_line == 0
        # l expands a collapsed module node / h collapses it again
        await pilot.press("j")  # onto first module node
        node = tree.cursor_node
        assert node is not None
        assert node.children
        await pilot.press("h")
        assert not node.is_expanded
        await pilot.press("l")
        assert node.is_expanded


async def test_vim_l_on_leaf_sends(
    project_with_requests: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status_code = 200
        text = "{}"
        url = "http://127.0.0.1:8000/x"
        headers: dict[str, str] = {}  # noqa: RUF012

        def json(self) -> dict[str, str]:
            return {}

    monkeypatch.setattr(requests, "request", lambda **kwargs: FakeResponse())
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        tree = screen.query_one("#request-tree")
        items = next(node for node in tree.root.children[0].children if "items" in str(node.label))
        tree.move_cursor(items.children[0])
        tree.focus()
        await pilot.press("l")
        await pilot.pause(delay=0.2)
        assert "ok" in str(screen.query_one("#status").render())


async def test_vim_selection_list_motions(
    project_with_requests: dict[str, Path],
    spec_path: Path,
) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        run_screen = app.screen
        assert isinstance(run_screen, RunScreen)
        run_screen.query_one("#request-tree").focus()
        await pilot.press("n")
        await pilot.pause()
        gen = app.screen
        assert isinstance(gen, GenScreen)
        spec_input = gen.query_one("#spec-input")
        spec_input.focus()
        spec_input.value = str(spec_path)
        await pilot.press("enter")
        await pilot.pause()
        selection_list = gen.query_one("#module-list")
        assert selection_list.has_focus
        await pilot.press("j")
        first = selection_list.highlighted
        assert first is not None
        await pilot.press("j")
        assert selection_list.highlighted == first + 1
        await pilot.press("k")
        assert selection_list.highlighted == first
        await pilot.press("G")
        assert selection_list.highlighted == 2
        await pilot.press("g", "g")
        assert selection_list.highlighted == 0
        # pane jumps
        await pilot.press("2")
        assert gen.query_one("#preview").has_focus
        await pilot.press("1")
        assert selection_list.has_focus


async def test_single_profile_message(project_with_requests: dict[str, Path]) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        screen.query_one("#request-tree").focus()
        await pilot.press("p")
        assert "only one profile configured" in str(screen.query_one("#status").render())


async def test_edit_request_opens_editor_at_function(
    project_with_requests: dict[str, Path],
) -> None:
    app = ReqApp()
    opened: list[tuple[Path, int]] = []
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        screen._open_in_editor = lambda path, line=0: opened.append((path, line))  # type: ignore[method-assign]  # noqa: SLF001
        tree = screen.query_one("#request-tree")
        items = next(node for node in tree.root.children[0].children if "items" in str(node.label))
        # function leaf: opens at the function's line
        tree.move_cursor(items.children[0])
        tree.focus()
        await pilot.press("e")
        assert opened[-1][0].name == "items.py"
        assert opened[-1][1] > 0
        # module node: opens the file without a line jump
        tree.move_cursor(items)
        await pilot.press("e")
        assert opened[-1] == (items.data.path, 0)


async def test_slash_search_in_tree(project_with_requests: dict[str, Path]) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        tree = screen.query_one("#request-tree")
        tree.focus()
        await pilot.press("/")
        search = screen.query_one("#search-input")
        assert search.has_focus
        await pilot.press(*"patch")
        await pilot.press("enter")
        await pilot.pause()
        assert tree.has_focus
        node = tree.cursor_node
        assert node is not None
        assert "patch" in str(node.label)
        assert "1 match(es)" in str(screen.query_one("#status").render())
        # n cycles (single match: stays put), N reverses
        await pilot.press("n")
        assert tree.cursor_node is node
        # escape cancels a new search without searching
        await pilot.press("/")
        await pilot.press("escape")
        assert tree.has_focus


async def test_slash_search_in_response_log(
    project_with_requests: dict[str, Path],
) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        log = screen.query_one("#response")
        for index in range(50):
            log.write(f"line {index} {'NEEDLE' if index == 40 else ''}")
        log.focus()
        await pilot.press("/")
        await pilot.press(*"needle")
        await pilot.press("enter")
        await pilot.pause()
        assert log.has_focus
        assert "1 match(es)" in str(screen.query_one("#status").render())
        assert log.scroll_offset.y > 0


async def test_search_highlights_matches_in_response_log(
    project_with_requests: dict[str, Path],
) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        log = screen.query_one("#response")
        for index in range(50):
            log.write(f"line {index} {'NEEDLE' if index == 40 else ''}")
        log.focus()
        await pilot.press("/")
        await pilot.press(*"needle")
        await pilot.press("enter")
        await pilot.pause()
        match_style = log.get_component_rich_style("vimlog--search-match")
        strip = log.render_line(40 - log.scroll_offset.y)
        assert "NEEDLE" in strip.text
        highlighted = [seg for seg in strip if seg.style and seg.style.bgcolor == match_style.bgcolor]
        assert "".join(seg.text for seg in highlighted) == "NEEDLE"
        # an empty search clears the highlight
        await pilot.press("/")
        await pilot.press("enter")
        await pilot.pause()
        strip = log.render_line(40 - log.scroll_offset.y)
        assert not [seg for seg in strip if seg.style and seg.style.bgcolor == match_style.bgcolor]


async def test_n_still_generates_without_search(
    project_with_requests: dict[str, Path],
) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        screen = app.screen
        assert isinstance(screen, RunScreen)
        screen.query_one("#request-tree").focus()
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, GenScreen)


async def test_slash_search_in_module_list(
    project_with_requests: dict[str, Path],
    spec_path: Path,
) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        run_screen = app.screen
        assert isinstance(run_screen, RunScreen)
        run_screen.query_one("#request-tree").focus()
        await pilot.press("n")
        await pilot.pause()
        gen = app.screen
        assert isinstance(gen, GenScreen)
        spec_input = gen.query_one("#spec-input")
        spec_input.focus()
        spec_input.value = str(spec_path)
        await pilot.press("enter")
        await pilot.pause()
        module_list = gen.query_one("#module-list")
        await pilot.press("/")
        await pilot.press(*"items")
        await pilot.press("enter")
        await pilot.pause()
        assert module_list.has_focus
        highlighted = module_list.highlighted
        assert highlighted is not None
        assert "items" in str(module_list.get_option_at_index(highlighted).prompt)


async def test_q_quits(isolated_dirs: dict[str, Path]) -> None:
    app = ReqApp()
    called = []

    async def spy() -> None:
        called.append(True)

    app.action_quit = spy  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.press("q")
        await pilot.pause()
    assert called, "q binding did not reach app.quit"


async def test_gen_screen_write_global_app_folder(
    spec_path: Path,
    isolated_dirs: dict[str, Path],
) -> None:
    app = ReqApp()
    async with app.run_test() as pilot:
        run_screen = app.screen
        assert isinstance(run_screen, RunScreen)
        run_screen.query_one("#request-tree").focus()
        await pilot.press("n")
        await pilot.pause()
        gen = app.screen
        assert isinstance(gen, GenScreen)
        spec_input = gen.query_one("#spec-input")
        spec_input.focus()
        spec_input.value = str(spec_path)
        await pilot.press("enter")
        await pilot.pause()
        # app name prefilled from spec title, user edits it
        app_input = gen.query_one("#app-name-input")
        assert app_input.value == "demo"
        app_input.value = "renamed"
        await pilot.press("W")
        await pilot.pause()
        store = isolated_dirs["data"] / "req-cli" / "requests"
        assert (store / "renamed" / "items.py").exists()
        status = str(gen.query_one("#gen-status").render())
        assert "global (renamed)" in status


async def test_write_shows_notification(spec_path: Path, isolated_dirs: dict[str, Path]) -> None:
    app = ReqApp()
    notifications: list[tuple[str, str]] = []
    async with app.run_test() as pilot:
        run_screen = app.screen
        assert isinstance(run_screen, RunScreen)
        run_screen.query_one("#request-tree").focus()
        await pilot.press("n")
        await pilot.pause()
        gen = app.screen
        assert isinstance(gen, GenScreen)

        original_notify = gen.notify

        def capture(message: str, *, title: str = "", **kwargs: object) -> None:
            notifications.append((title, message))
            original_notify(message, title=title, **kwargs)  # type: ignore[arg-type]

        gen.notify = capture  # type: ignore[method-assign]
        spec_input = gen.query_one("#spec-input")
        spec_input.focus()
        spec_input.value = str(spec_path)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
    assert any("saved" in title for title, _ in notifications)
    # writing again without overwrite warns instead of silently skipping
    app2 = ReqApp()
    warnings: list[str] = []
    async with app2.run_test() as pilot:
        run_screen = app2.screen
        assert isinstance(run_screen, RunScreen)
        run_screen.query_one("#request-tree").focus()
        await pilot.press("n")
        await pilot.pause()
        gen = app2.screen
        assert isinstance(gen, GenScreen)
        gen.notify = lambda message, **kwargs: warnings.append(  # type: ignore[method-assign]
            str(kwargs.get("title", "")),
        )
        spec_input = gen.query_one("#spec-input")
        spec_input.focus()
        spec_input.value = str(spec_path)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
    assert any("not written" in title for title in warnings)


async def test_gen_screen_bad_spec(isolated_dirs: dict[str, Path], tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: a spec")
    app = ReqApp()
    async with app.run_test() as pilot:
        run_screen = app.screen
        assert isinstance(run_screen, RunScreen)
        run_screen.query_one("#request-tree").focus()
        await pilot.press("n")
        await pilot.pause()
        gen = app.screen
        assert isinstance(gen, GenScreen)
        spec_input = gen.query_one("#spec-input")
        spec_input.focus()
        spec_input.value = str(bad)
        await pilot.press("enter")
        await pilot.pause()
        status = str(gen.query_one("#gen-status").render())
        assert "does not look like an OpenAPI" in status
