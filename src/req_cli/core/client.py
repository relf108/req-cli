"""
Generic request execution.

This replaces the boilerplate previously copied into every ``requests/``
folder: URL building with path params, header profiles, proxy handling,
and response timing.

Generated request modules call :func:`send`::

    from req_cli.core.client import send

    def get_item(item_id: str = "example") -> Result:
        \"\"\"GET /v1/items/{item_id}\"\"\"
        return send(
            "GET",
            "/v1/items/{item_id}",
            path_params={"item_id": item_id},
            params={"page": 1, "limit": 50},
        )
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any

import requests

from req_cli.core.config import Config, load_config


class RequestBuildError(Exception):
    """Raised when a request cannot be constructed (e.g. missing path params)."""


_profile_override: str | None = None


def set_profile_override(name: str | None) -> None:
    """
    Force all subsequent :func:`send` calls onto the named profile.

    Used by the TUI/CLI to switch environment (e.g. local vs live) without
    editing saved request functions. Pass ``None`` to clear.
    """
    global _profile_override  # noqa: PLW0603 - module-level override is the feature
    _profile_override = name


@dataclass(frozen=True)
class Result:
    """Outcome of a sent request: response (or transport error) plus timing."""

    method: str
    url: str
    elapsed: float
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    json_body: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when a response was received with a non-error status."""
        return self.status is not None and self.status < HTTPStatus.BAD_REQUEST


def build_url(base_url: str, path: str, path_params: dict[str, Any] | None = None) -> str:
    """Join *base_url* and *path*, substituting ``{placeholders}`` from *path_params*."""
    try:
        filled = path.format(**(path_params or {}))
    except (KeyError, IndexError) as exc:
        msg = f"missing path parameter {exc} for path {path!r}"
        raise RequestBuildError(msg) from exc
    return f"{base_url.rstrip('/')}/{filled.lstrip('/')}"


def send(  # noqa: PLR0913 - request builders are wide by nature, all kwargs optional
    method: str,
    path: str,
    *,
    path_params: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    json: Any = None,  # noqa: ANN401 - request bodies are arbitrary JSON
    headers: dict[str, str] | None = None,
    profile: str | None = None,
    config: Config | None = None,
    project_root: Path | None = None,
    timeout: float | None = None,
) -> Result:
    """
    Send a request using the named profile and return a :class:`Result`.

    Profile resolution: *config* if given, otherwise loaded from
    *project_root* / cwd. Extra *headers* are merged over profile headers.
    Transport errors are captured in ``Result.error`` rather than raised.
    Requests wait indefinitely unless *timeout* or the profile's ``timeout``
    sets a limit.
    """
    cfg = config or load_config(project_root)
    prof = cfg.profile(_profile_override or profile)
    url = build_url(prof.base_url, path, path_params)
    merged_headers = {**prof.headers, **(headers or {})}

    start = time.perf_counter()
    try:
        res = requests.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json,
            headers=merged_headers,
            proxies=prof.proxies or None,
            timeout=timeout if timeout is not None else prof.timeout,
        )
    except requests.RequestException as exc:
        return Result(
            method=method.upper(),
            url=url,
            elapsed=time.perf_counter() - start,
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = time.perf_counter() - start

    try:
        json_body: Any = res.json()
    except ValueError:
        json_body = None

    return Result(
        method=method.upper(),
        url=res.url,
        elapsed=elapsed,
        status=res.status_code,
        headers=dict(res.headers),
        body=res.text,
        json_body=json_body,
    )
