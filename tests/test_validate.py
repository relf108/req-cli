"""Tests for module validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

from req_cli.core.validate import validate_file, validate_source

GOOD = textwrap.dedent(
    '''
    """Requests for items."""

    from req_cli.core.client import Result, send


    def get_item(item_id: str = "abc") -> Result:
        """GET /v1/items/{item_id}."""
        return send("GET", "/v1/items/{item_id}", path_params={"item_id": item_id})
    ''',
)


def _messages(source: str) -> list[str]:
    return [str(issue) for issue in validate_source(source, Path("mod.py")).issues]


def test_good_module_passes() -> None:
    report = validate_source(GOOD, Path("items.py"))
    assert report.ok
    assert report.issues == ()


def test_syntax_error() -> None:
    report = validate_source("def broken(:\n", Path("bad.py"))
    assert not report.ok
    assert "syntax error" in str(report.issues[0])


def test_no_public_functions() -> None:
    report = validate_source("X = 1\n\ndef _private() -> None: ...\n", Path("mod.py"))
    assert not report.ok
    assert any("no public request functions" in message for message in _messages("X = 1"))


def test_missing_docstring_is_error() -> None:
    source = "def get_x():\n    return 1\n"
    assert any("missing docstring" in message for message in _messages(source))


def test_docstring_without_endpoint_is_warning() -> None:
    source = 'def get_x():\n    """Does things."""\n    return 1\n'
    report = validate_source(source, Path("mod.py"))
    assert any(
        issue.level == "warning" and "docstring should name the endpoint" in issue.message for issue in report.issues
    )


def test_required_args_are_error() -> None:
    source = 'def get_x(item_id):\n    """GET /x."""\n    return item_id\n'
    report = validate_source(source, Path("mod.py"))
    assert not report.ok
    assert any("no defaults" in issue.message for issue in report.issues)


def test_eval_dispatch_is_error() -> None:
    source = GOOD + "\nimport sys\neval(sys.argv[1])()\n"
    report = validate_source(source, Path("mod.py"))
    assert not report.ok
    assert any("eval()" in issue.message for issue in report.issues)


def test_missing_req_cli_import_is_warning() -> None:
    source = 'def get_x():\n    """GET /x."""\n    return 1\n'
    report = validate_source(source, Path("mod.py"))
    assert any(issue.level == "warning" and "does not import req_cli" in issue.message for issue in report.issues)


def test_hardcoded_bearer_token_is_error() -> None:
    token = "Bearer ey" + "J" * 40
    source = GOOD.replace('"abc"', f'"{token}"')
    report = validate_source(source, Path("mod.py"))
    assert not report.ok
    assert any("secret" in issue.message for issue in report.issues)


def test_hardcoded_password_is_error() -> None:
    source = GOOD + '\nPASSWORD = "supersecretpassword123"\npassword = PASSWORD\n'
    source = source.replace("password = PASSWORD", 'password = "supersecretpassword123"')
    report = validate_source(source, Path("mod.py"))
    assert not report.ok


def test_validate_file_missing(tmp_path: Path) -> None:
    report = validate_file(tmp_path / "nope.py")
    assert not report.ok
    assert "cannot read" in str(report.issues[0])


def test_validate_file_good(tmp_path: Path) -> None:
    path = tmp_path / "items.py"
    path.write_text(GOOD)
    assert validate_file(path).ok
