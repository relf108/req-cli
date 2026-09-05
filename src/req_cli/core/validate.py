"""
Validation of request modules before they are written into a project.

Checks (syntax + contract):

- source parses and compiles
- at least one public request function exists
- request functions have docstrings naming the endpoint (method + path)
- the module uses the shared client (``req_cli.core.client``) rather than
  hand-rolled ``requests`` calls or ``eval`` dispatch
- no obvious secrets (bearer tokens, long opaque credentials) are embedded
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Level = Literal["error", "warning"]

_DOC_ENDPOINT_RE = re.compile(r"\b(GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH)\b\s+/", re.IGNORECASE)
_SECRET_RES = (
    re.compile(r"Bearer\s+ey[A-Za-z0-9_\-]{20,}"),  # JWTs
    re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[=:]\s*['\"][^'\"]{16,}['\"]"),
)


@dataclass(frozen=True)
class Issue:
    """A single validation finding."""

    level: Level
    message: str
    lineno: int = 0

    def __str__(self) -> str:
        """Render as ``[level] line N: message``."""
        prefix = f"line {self.lineno}: " if self.lineno else ""
        return f"[{self.level}] {prefix}{self.message}"


@dataclass(frozen=True)
class ValidationReport:
    """All findings for one module."""

    path: Path
    issues: tuple[Issue, ...]

    @property
    def ok(self) -> bool:
        """True when there are no error-level issues."""
        return all(issue.level != "error" for issue in self.issues)


def _check_secrets(source: str) -> list[Issue]:
    issues = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        for pattern in _SECRET_RES:
            if pattern.search(line):
                issues.append(
                    Issue(
                        level="error",
                        message="possible hardcoded secret - move it to a config profile",
                        lineno=lineno,
                    ),
                )
                break
    return issues


def _imports_req_cli(node: ast.AST) -> bool:
    """Return True when *node* imports anything from the ``req_cli`` package."""
    if isinstance(node, ast.ImportFrom):
        return node.module is not None and node.module.startswith("req_cli")
    return isinstance(node, ast.Import) and any(a.name.startswith("req_cli") for a in node.names)


def _check_style(tree: ast.Module) -> list[Issue]:
    """Flag ``eval`` dispatch and modules bypassing the shared client in one walk."""
    uses_shared_client = False
    issues = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "eval":
            issues.append(
                Issue(
                    level="error",
                    message="uses eval() dispatch - req-cli imports and calls functions directly",
                    lineno=node.lineno,
                ),
            )
        elif not uses_shared_client and _imports_req_cli(node):
            uses_shared_client = True
    if not uses_shared_client:
        issues.append(
            Issue(
                level="warning",
                message="module does not import req_cli - request functions should use "
                "req_cli.core.client.send for profiles/proxies to apply",
            ),
        )
    return issues


def _check_functions(tree: ast.Module) -> list[Issue]:
    issues = []
    public = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_")
    ]
    if not public:
        issues.append(Issue(level="error", message="no public request functions found"))
    for node in public:
        docstring = ast.get_docstring(node) or ""
        if not docstring:
            issues.append(
                Issue(
                    level="error",
                    message=f"{node.name}: missing docstring naming the endpoint",
                    lineno=node.lineno,
                ),
            )
        elif not _DOC_ENDPOINT_RE.search(docstring):
            issues.append(
                Issue(
                    level="warning",
                    message=f'{node.name}: docstring should name the endpoint, e.g. "GET /v1/items"',
                    lineno=node.lineno,
                ),
            )
        required = [arg.arg for arg, default in _args_with_defaults(node) if default is None and arg.arg != "self"]
        if required:
            issues.append(
                Issue(
                    level="error",
                    message=f"{node.name}: parameters {required} have no defaults - "
                    "request functions must be callable with no arguments",
                    lineno=node.lineno,
                ),
            )
    return issues


def _args_with_defaults(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[ast.arg, ast.expr | None]]:
    args = node.args
    positional = args.posonlyargs + args.args
    padded: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    pairs = list(zip(positional, padded + list(args.defaults), strict=True))
    pairs += list(zip(args.kwonlyargs, args.kw_defaults, strict=True))
    return pairs


def validate_source(source: str, path: Path) -> ValidationReport:
    """Validate module *source*, returning every finding."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        issue = Issue(level="error", message=f"syntax error: {exc.msg}", lineno=exc.lineno or 0)
        return ValidationReport(path=path, issues=(issue,))
    issues = _check_functions(tree) + _check_style(tree) + _check_secrets(source)
    issues.sort(key=lambda issue: issue.lineno)
    return ValidationReport(path=path, issues=tuple(issues))


def validate_file(path: Path) -> ValidationReport:
    """Validate the module at *path*."""
    try:
        source = path.read_text()
    except OSError as exc:
        issue = Issue(level="error", message=f"cannot read file: {exc}")
        return ValidationReport(path=path, issues=(issue,))
    return validate_source(source, path)
