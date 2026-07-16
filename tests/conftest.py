"""Shared fixtures."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

SAMPLE_SPEC = textwrap.dedent(
    """
    openapi: 3.0.3
    info: {title: demo, version: '1'}
    paths:
      /v1/items/{item_id}:
        parameters:
          - {name: item_id, in: path, required: true, schema: {type: string}}
        get:
          operationId: getItem
          tags: [items]
          summary: Get item
          parameters:
            - {name: page, in: query, required: true, schema: {type: integer, default: 1}}
            - {name: limit, in: query, schema: {type: integer, example: 50}}
          responses: {'200': {description: ok}}
      /v1/items/{item_id}/details/{detail_id}:
        patch:
          operationId: patchDetail
          tags: [details]
          summary: Update detail
          parameters:
            - {name: item_id, in: path, required: true, schema: {type: string}}
            - {name: detail_id, in: path, required: true, schema: {type: string}}
          requestBody:
            content:
              application/json:
                schema:
                  $ref: '#/components/schemas/DetailPatch'
          responses: {'200': {description: ok}}
      /v1/status:
        get:
          responses: {'200': {description: ok}}
    components:
      schemas:
        DetailPatch:
          type: object
          properties:
            count: {type: integer, example: 40}
            enabled: {type: boolean}
            nested:
              type: object
              properties:
                status: {type: string, enum: [active, inactive]}
    """,
)


@pytest.fixture
def spec_path(tmp_path: Path) -> Path:
    path = tmp_path / "openapi.yaml"
    path.write_text(SAMPLE_SPEC)
    return path


@pytest.fixture
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Redirect global config/store and project root into tmp_path."""
    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.chdir(project)
    return {"config": config_home, "data": data_home, "project": project}
