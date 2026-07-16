"""Tests for code generation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from req_cli.core.validate import validate_source
from req_cli.gen.generate import generate_modules, write_module
from req_cli.gen.openapi import Endpoint, Param, load_spec, parse_endpoints


def _modules(spec_path: Path) -> dict[str, str]:
    return generate_modules(parse_endpoints(load_spec(spec_path)), spec_path.name)


def test_one_module_per_group(spec_path: Path) -> None:
    sources = _modules(spec_path)
    assert set(sources) == {"items", "details", "status"}


def test_generated_code_is_valid_python(spec_path: Path) -> None:
    for name, source in _modules(spec_path).items():
        compile(source, f"{name}.py", "exec")


def test_generated_code_passes_validation(spec_path: Path) -> None:
    for name, source in _modules(spec_path).items():
        report = validate_source(source, Path(f"{name}.py"))
        assert report.ok, f"{name}: {[str(issue) for issue in report.issues]}"


def test_generated_function_shape(spec_path: Path) -> None:
    source = _modules(spec_path)["items"]
    assert "from req_cli.core.client import Result, send" in source
    assert "def get_item(item_id: str = '') -> Result:" in source
    assert '"""GET /v1/items/{item_id} - Get item."""' in source
    assert '"page": 1,' in source
    assert '"limit": 50,  # optional' in source

    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert [function.name for function in functions] == ["get_item"]


def test_body_example_rendered(spec_path: Path) -> None:
    source = _modules(spec_path)["details"]
    assert "json={'count': 40," in source


def test_duplicate_function_names_deduped() -> None:
    endpoint = Endpoint(method="GET", path="/a", function_name="get_a", group="a", summary="")
    sources = generate_modules([endpoint, endpoint], "spec")
    assert "def get_a(" in sources["a"]
    assert "def get_a_2(" in sources["a"]


def test_reserved_words_and_bad_names_sanitised() -> None:
    endpoint = Endpoint(
        method="GET",
        path="/x/{class}",
        function_name="get_x",
        group="x",
        path_params=(Param(name="class", required=True),),
    )
    source = generate_modules([endpoint], "spec")["x"]
    compile(source, "x.py", "exec")
    assert "class_: str" in source
    assert '"class": class_' in source


def test_write_module_refuses_overwrite(tmp_path: Path) -> None:
    write_module("x = 1\n", tmp_path, "mod")
    with pytest.raises(FileExistsError):
        write_module("x = 2\n", tmp_path, "mod")
    write_module("x = 3\n", tmp_path, "mod", overwrite=True)
    assert (tmp_path / "mod.py").read_text() == "x = 3\n"
