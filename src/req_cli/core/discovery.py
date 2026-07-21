"""
Discovery of saved request modules and their request functions.

Two sources are scanned:

- **local**: ``./requests/`` in the current project
- **global**: ``~/.local/share/req-cli/requests/``

Listing uses ``ast`` so that browsing the TUI never executes module-level
code; a module is only imported when a function is actually invoked.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from req_cli.core.client import Result
from req_cli.core.config import CONFIG_FUNCTIONS_FILE_NAME, global_store_dir, project_requests_dir

Scope = Literal["local", "global"]


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


def _scan_dir(directory: Path, scope: Scope) -> list[RequestModule]:
    if not directory.is_dir():
        return []
    paths = sorted(set(directory.glob("*.py")) | set(directory.glob("*/*.py")))
    modules = []
    for path in paths:
        relative = path.relative_to(directory)
        if path.name == CONFIG_FUNCTIONS_FILE_NAME or any(part.startswith(("_", ".")) for part in relative.parts):
            continue
        try:
            source = path.read_text()
        except OSError:
            continue
        modules.append(
            RequestModule(
                path=path,
                name=str(relative.with_suffix("")),
                scope=scope,
                functions=_functions_from_source(source),
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
