"""Tests for OpenAPI parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from req_cli.gen.openapi import SpecError, app_name, load_spec, parse_endpoints


def test_load_spec_rejects_non_spec(tmp_path: Path) -> None:
    path = tmp_path / "nope.yaml"
    path.write_text("just: yaml")
    with pytest.raises(SpecError, match="openapi"):
        load_spec(path)


def test_load_spec_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match="cannot read"):
        load_spec(tmp_path / "missing.yaml")


def test_load_spec_rejects_invalid(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("openapi: 3.0.3\ninfo: {title: x}\npaths: {}\n")  # missing info.version
    with pytest.raises(SpecError, match="validation"):
        load_spec(path)


def test_parse_endpoints(spec_path: Path) -> None:
    endpoints = parse_endpoints(load_spec(spec_path))
    by_name = {endpoint.function_name: endpoint for endpoint in endpoints}
    assert set(by_name) == {"get_item", "patch_detail", "get_v1_status"}

    item = by_name["get_item"]
    assert item.method == "GET"
    assert item.group == "items"
    assert item.summary == "Get item"
    # path-level parameter is merged in
    assert [param.name for param in item.path_params] == ["item_id"]
    assert {param.name: param.example for param in item.query_params} == {
        "page": 1,
        "limit": 50,
    }
    page = next(param for param in item.query_params if param.name == "page")
    assert page.required

    patch = by_name["patch_detail"]
    assert patch.group == "details"
    assert [param.name for param in patch.path_params] == ["item_id", "detail_id"]
    # $ref resolved; example values pulled from schema, enum picks first value
    assert patch.body_example == {
        "count": 40,
        "enabled": False,
        "nested": {"status": "active"},
    }

    # no operationId and no tags: name from method+path, group from first real segment
    status = by_name["get_v1_status"]
    assert status.group == "status"
    assert status.body_example is None


def test_app_name_from_title(spec_path: Path) -> None:
    assert app_name(load_spec(spec_path)) == "demo"


def test_app_name_slugs_title(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(
        "openapi: 3.0.3\ninfo: {title: 'Demo App (EU)', version: '1'}\npaths: {}\n",
    )
    assert app_name(load_spec(path)) == "demo_app_eu"


def test_parse_json_spec(tmp_path: Path) -> None:
    path = tmp_path / "spec.json"
    path.write_text(
        '{"openapi": "3.1.0", "info": {"title": "t", "version": "1"},'
        ' "paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}}}',
    )
    endpoints = parse_endpoints(load_spec(path))
    assert len(endpoints) == 1
    assert endpoints[0].function_name == "get_ping"
