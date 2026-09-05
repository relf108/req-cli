"""
Discovery of saved request modules and their request functions.

Two sources are scanned:

- **local**: ``./requests/`` in the current project
- **global**: ``~/.config/req-cli/requests/``

Listing uses ``ast`` so that browsing the TUI never executes module-level
code; a module is only imported when a function is actually invoked.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from req_cli.core.client import Result
from req_cli.core.config import CONFIG_FUNCTIONS_FILE_NAME, global_store_dir, project_requests_dir

Scope = Literal["local", "global"]

_SCAN_CACHE_MAX_ENTRIES = 1024
"""Upper bound for memoized per-file parses; hit only by pathological path churn."""

_scan_cache: dict[str, tuple[int, int, tuple[RequestFunction, ...]]] = {}
"""Parsed functions by module path: ``(mtime_ns, size, functions)``."""


class DiscoveryError(Exception):
    """Raised when a module or function cannot be loaded."""


@dataclass(frozen=True)
class RequestFunction:
    """A callable request saved in a module."""

    name: str
    docstring: str
    lineno: int


@dataclass(frozen=True)
class RequestModule:
    """A python file containing saved request functions."""

    path: Path
    name: str
    scope: Scope
    functions: tuple[RequestFunction, ...] = field(default_factory=tuple)


def _functions_from_source(source: str) -> tuple[RequestFunction, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ()
    return tuple(
        RequestFunction(
            name=node.name,
            docstring=ast.get_docstring(node) or "",
            lineno=node.lineno,
        )
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_")
    )


def _cached_functions(path: Path) -> tuple[RequestFunction, ...] | None:
    """Return memoized functions for *path*, or None when it must be reparsed."""
    try:
        info = path.stat()
    except OSError:
        _scan_cache.pop(str(path), None)
        return None
    cached = _scan_cache.get(str(path))
    if cached is not None and (cached[0], cached[1]) == (info.st_mtime_ns, info.st_size):
        return cached[2]
    return None


def _remember_functions(path: Path, functions: tuple[RequestFunction, ...]) -> None:
    """Memoize *functions* for *path* under its current stat snapshot."""
    try:
        info = path.stat()
    except OSError:
        return
    key = str(path)
    _scan_cache[key] = (info.st_mtime_ns, info.st_size, functions)
    if len(_scan_cache) > _SCAN_CACHE_MAX_ENTRIES:
        _scan_cache.clear()
        _scan_cache[key] = (info.st_mtime_ns, info.st_size, functions)


def _scan_one_level(dir_path: str) -> tuple[list[Path], list[str]]:
    """Return ``(py_files, subdirs)`` for one directory, hidden/private entries skipped."""
    files: list[Path] = []
    subdirs: list[str] = []
    try:
        with os.scandir(dir_path) as handle:
            entries = list(handle)
    except OSError:
        return ([], [])
    for entry in entries:
        name = entry.name
        if name.startswith(("_", ".")) or name == CONFIG_FUNCTIONS_FILE_NAME:
            continue
        try:
            if entry.is_dir():
                subdirs.append(entry.path)
            elif name.endswith(".py") and entry.is_file():
                files.append(Path(entry.path))
        except OSError:
            continue
    return (files, subdirs)


def _candidate_files(directory: Path) -> list[Path]:
    """List ``*.py`` files (top level plus one subdir level), skipping hidden/private ones."""
    candidates, subdirs = _scan_one_level(str(directory))
    for subdir in subdirs:
        sub_files, _ = _scan_one_level(subdir)
        candidates.extend(sub_files)
    candidates.sort()
    return candidates


def _scan_dir(directory: Path, scope: Scope) -> list[RequestModule]:
    if not directory.is_dir():
        return []
    modules = []
    for path in _candidate_files(directory):
        functions = _cached_functions(path)
        if functions is None:
            try:
                source = path.read_text()
            except OSError:
                continue
            functions = _functions_from_source(source)
            _remember_functions(path, functions)
        modules.append(
            RequestModule(
                path=path,
                name=str(path.relative_to(directory).with_suffix("")),
                scope=scope,
                functions=functions,
            ),
        )
    return modules


def discover(project_root: Path | None = None) -> list[RequestModule]:
    """
    Return all request modules, local project first, then the global store.

    Local modules live flat in ``./requests``; global modules live under one
    app folder per spec (``<store>/<app>/<module>.py``), and their names are
    qualified accordingly (``<app>/<module>``).
    """
    return _scan_dir(project_requests_dir(project_root), "local") + _scan_dir(
        global_store_dir(),
        "global",
    )


def load_function(module_path: Path, function_name: str) -> Callable[[], Result]:
    """Import *module_path* and return the named zero-arg request function."""
    spec = importlib.util.spec_from_file_location(f"req_cli_saved.{module_path.stem}", module_path)
    if spec is None or spec.loader is None:
        msg = f"cannot load module {module_path}"
        raise DiscoveryError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        msg = f"error importing {module_path}: {exc}"
        raise DiscoveryError(msg) from exc
    func = getattr(module, function_name, None)
    if not callable(func):
        msg = f"{module_path} has no function {function_name!r}"
        raise DiscoveryError(msg)
    return cast("Callable[[], Result]", func)
