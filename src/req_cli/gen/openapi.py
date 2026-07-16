"""
OpenAPI spec parsing: turn a yaml/json spec into endpoint models.

Parsing and validation are delegated to ``openapi-pydantic`` (which selects
3.0 or 3.1 models from the spec's ``openapi`` field). This module resolves
local ``#/components`` references and flattens operations into the small
:class:`Endpoint` model that the code generator consumes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from openapi_pydantic import parse_obj
from pydantic import ValidationError

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")
MAX_EXAMPLE_DEPTH = 6
"""Recursion cutoff when building example bodies from nested schemas."""


class SpecError(Exception):
    """Raised when a spec cannot be read, parsed, or validated."""


@dataclass(frozen=True)
class Param:
    """A path or query parameter."""

    name: str
    required: bool = False
    schema_type: str = "string"
    example: Any = None


@dataclass(frozen=True)
class Endpoint:
    """One method+path operation from the spec."""

    method: str
    path: str
    function_name: str
    group: str
    summary: str = ""
    path_params: tuple[Param, ...] = field(default_factory=tuple)
    query_params: tuple[Param, ...] = field(default_factory=tuple)
    body_example: Any = None


def _snake(name: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name)
    return re.sub(r"_+", "_", name).strip("_").lower()


class _Resolver:
    """Resolves local ``#/components/...`` references against the typed spec."""

    def __init__(self, spec: Any) -> None:
        self._spec = spec

    def resolve(self, node: Any) -> Any:
        """Follow ``Reference`` nodes until a concrete object (or None)."""
        seen: set[str] = set()
        while isinstance(ref := getattr(node, "ref", None), str):
            if not ref.startswith("#/components/") or ref in seen:
                return None
            seen.add(ref)
            node = self._lookup(ref)
        return node

    def _lookup(self, ref: str) -> Any:
        _, _, section, name = ref.split("/", 3)
        components = self._spec.components
        if components is None:
            return None
        bucket = getattr(components, section, None)
        if not isinstance(bucket, dict):
            return None
        return bucket.get(name)


def _schema_type(schema: Any) -> str:
    """Normalise Schema.type (enum, list of enums in 3.1, or None) to a string."""
    raw = getattr(schema, "type", None)
    if isinstance(raw, list):
        non_null = [t for t in raw if getattr(t, "value", t) != "null"]
        raw = non_null[0] if non_null else None
    value = getattr(raw, "value", raw)
    return str(value) if value is not None else "string"


def _schema_example(schema: Any) -> Any:
    """First explicit example/default declared on a schema, else None."""
    examples = getattr(schema, "examples", None)
    if isinstance(examples, list) and examples:
        return examples[0]
    if getattr(schema, "example", None) is not None:
        return schema.example
    if getattr(schema, "default", None) is not None:
        return schema.default
    enum = getattr(schema, "enum", None)
    if isinstance(enum, list) and enum:
        return enum[0]
    return None


def _example_from_schema(resolver: _Resolver, schema: Any, depth: int = 0) -> Any:
    """Build a skeleton example value from a schema."""
    schema = resolver.resolve(schema)
    if schema is None or depth > MAX_EXAMPLE_DEPTH:
        return None
    explicit = _schema_example(schema)
    if explicit is not None:
        return explicit
    for combiner in ("allOf", "anyOf", "oneOf"):
        subschemas = getattr(schema, combiner, None)
        if subschemas:
            return _example_from_schema(resolver, subschemas[0], depth + 1)
    schema_type = _schema_type(schema)
    if getattr(schema, "properties", None):
        return {name: _example_from_schema(resolver, prop, depth + 1) for name, prop in schema.properties.items()}
    if schema_type == "array":
        item = _example_from_schema(resolver, getattr(schema, "items", None), depth + 1)
        return [item] if item is not None else []
    defaults: dict[str, Any] = {"string": "", "integer": 0, "number": 0.0, "boolean": False}
    return defaults.get(schema_type)


def _parse_param(resolver: _Resolver, raw: Any) -> tuple[str, Param] | None:
    parameter = resolver.resolve(raw)
    if parameter is None:
        return None
    location = getattr(parameter.param_in, "value", str(parameter.param_in))
    if location not in ("path", "query"):
        return None
    schema = resolver.resolve(getattr(parameter, "param_schema", None))
    example = getattr(parameter, "example", None)
    if example is None and schema is not None:
        example = _schema_example(schema)
    return location, Param(
        name=parameter.name,
        required=bool(parameter.required or location == "path"),
        schema_type=_schema_type(schema) if schema is not None else "string",
        example=example,
    )


def _function_name(method: str, path: str, operation: Any) -> str:
    operation_id = operation.operationId
    if operation_id:
        return _snake(operation_id)
    slug = _snake(re.sub(r"[{}]", "", path)) or "root"
    return f"{method}_{slug}"


def _group(path: str, operation: Any) -> str:
    tags = operation.tags
    if tags:
        return _snake(tags[0])
    for segment in path.strip("/").split("/"):
        if segment and not segment.startswith("{") and not re.fullmatch(r"v\d+", segment):
            return _snake(segment)
    return "root"


def _body_example(resolver: _Resolver, operation: Any) -> Any:
    body = resolver.resolve(operation.requestBody)
    if body is None:
        return None
    for content_type, media in (body.content or {}).items():
        if "json" in content_type:
            if getattr(media, "example", None) is not None:
                return media.example
            return _example_from_schema(resolver, getattr(media, "media_type_schema", None))
    return None


def load_spec(path: Path) -> Any:
    """Load and validate an OpenAPI spec from a yaml or json file."""
    try:
        text = path.read_text()
    except OSError as exc:
        msg = f"cannot read spec {path}: {exc}"
        raise SpecError(msg) from exc
    try:
        raw = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        msg = f"cannot parse spec {path}: {exc}"
        raise SpecError(msg) from exc
    if not isinstance(raw, dict) or "openapi" not in raw:
        msg = f"{path} does not look like an OpenAPI 3.x spec (no 'openapi' field)"
        raise SpecError(msg)
    try:
        return parse_obj(raw)
    except ValidationError as exc:
        msg = f"{path} failed OpenAPI validation: {exc}"
        raise SpecError(msg) from exc


def app_name(spec: Any) -> str:
    """Derive an app slug from the spec's ``info.title`` (e.g. ``demo_app``)."""
    info = getattr(spec, "info", None)
    title = getattr(info, "title", None) or ""
    return _snake(title) or "app"


def parse_endpoints(spec: Any) -> list[Endpoint]:
    """Extract all endpoints from a validated spec, ordered by path then method."""
    resolver = _Resolver(spec)
    endpoints = []
    for path, raw_item in sorted((spec.paths or {}).items()):
        path_item = resolver.resolve(raw_item)
        if path_item is None:
            continue
        shared_params = path_item.parameters or []
        for method in HTTP_METHODS:
            operation = getattr(path_item, method, None)
            if operation is None:
                continue
            path_params: list[Param] = []
            query_params: list[Param] = []
            for raw in [*shared_params, *(operation.parameters or [])]:
                parsed = _parse_param(resolver, raw)
                if parsed is None:
                    continue
                location, param = parsed
                bucket = path_params if location == "path" else query_params
                if all(existing.name != param.name for existing in bucket):
                    bucket.append(param)
            endpoints.append(
                Endpoint(
                    method=method.upper(),
                    path=str(path),
                    function_name=_function_name(method, str(path), operation),
                    group=_group(str(path), operation),
                    summary=operation.summary or "",
                    path_params=tuple(path_params),
                    query_params=tuple(query_params),
                    body_example=_body_example(resolver, operation),
                ),
            )
    return endpoints
