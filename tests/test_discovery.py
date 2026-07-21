"""Tests for request module discovery and loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from req_cli.core import discovery
from req_cli.core.config import global_store_dir

MODULE = '''
"""Saved requests."""

CONSTANT = "left alone"


def get_item():
    """GET /v1/items/{item_id}."""
    return "item-result"


def _helper():
    return None


async def get_async():
    """GET /v1/async."""
    return None
'''


def test_discover_empty(isolated_dirs: dict[str, Path]) -> None:
    assert discovery.discover() == []


def test_discover_local_and_global(isolated_dirs: dict[str, Path]) -> None:
    local = isolated_dirs["project"] / "requests"
    local.mkdir()
    (local / "items.py").write_text(MODULE)
    (local / "config_functions.py").write_text("def get_token():\n    return 'token'\n")
    (local / "_private.py").write_text("def x():\n    pass\n")
    store = global_store_dir()
    store.mkdir(parents=True)
    (store / "shared.py").write_text(MODULE)

    modules = discovery.discover()
    assert [(module.scope, module.name) for module in modules] == [
        ("local", "items"),
        ("global", "shared"),
    ]


def test_discover_global_app_folders(isolated_dirs: dict[str, Path]) -> None:
    store = global_store_dir()
    (store / "demo_app").mkdir(parents=True)
    (store / "shared_app").mkdir(parents=True)
    (store / "demo_app" / "items.py").write_text(MODULE)
    (store / "shared_app" / "events.py").write_text(MODULE)
    (store / "demo_app" / "_skip.py").write_text(MODULE)

    modules = discovery.discover()
    assert [module.name for module in modules] == ["demo_app/items", "shared_app/events"]
    assert all(module.scope == "global" for module in modules)
    items = modules[0]
    assert [function.name for function in items.functions] == ["get_item", "get_async"]
    assert items.functions[0].docstring == "GET /v1/items/{item_id}."


def test_discover_tolerates_broken_syntax(isolated_dirs: dict[str, Path]) -> None:
    local = isolated_dirs["project"] / "requests"
    local.mkdir()
    (local / "broken.py").write_text("def broken(:\n")
    modules = discovery.discover()
    assert len(modules) == 1
    assert modules[0].functions == ()


def test_load_function(isolated_dirs: dict[str, Path]) -> None:
    local = isolated_dirs["project"] / "requests"
    local.mkdir()
    path = local / "items.py"
    path.write_text(MODULE)
    func = discovery.load_function(path, "get_item")
    assert func() == "item-result"


def test_load_function_missing(isolated_dirs: dict[str, Path]) -> None:
    local = isolated_dirs["project"] / "requests"
    local.mkdir()
    path = local / "items.py"
    path.write_text(MODULE)
    with pytest.raises(discovery.DiscoveryError, match="no function"):
        discovery.load_function(path, "nope")


def test_load_function_import_error(isolated_dirs: dict[str, Path]) -> None:
    local = isolated_dirs["project"] / "requests"
    local.mkdir()
    path = local / "bad.py"
    path.write_text("raise RuntimeError('boom')\n")
    with pytest.raises(discovery.DiscoveryError, match="boom"):
        discovery.load_function(path, "anything")
