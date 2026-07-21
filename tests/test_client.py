"""Tests for request execution and rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from req_cli.core import client
from req_cli.core.client import Result, build_url, send, set_profile_override
from req_cli.core.config import Config, Profile
from req_cli.core.render import render_body, render_result


class FakeResponse:
    def __init__(self, status: int = 200, json_body: Any = None, text: str = "") -> None:
        self.status_code = status
        self._json = json_body
        self.text = text if json_body is None else '{"stub": true}'
        self.url = "http://example.com/final"
        self.headers = {"content-type": "application/json"}

    def json(self) -> Any:
        if self._json is None:
            msg = "no json"
            raise ValueError(msg)
        return self._json


@pytest.fixture
def config() -> Config:
    return Config(
        profiles={
            "local": Profile(name="local", base_url="http://127.0.0.1:8000"),
            "live": Profile(
                name="live",
                base_url="https://api.example.test",
                headers={"X-Example-Header": "value"},
                proxies={"https": "http://proxy.example.test:8080"},
            ),
        },
        default_profile="local",
    )


@pytest.fixture(autouse=True)
def clear_override() -> Any:
    yield
    set_profile_override(None)


def test_build_url_substitutes_path_params() -> None:
    url = build_url("http://x/", "/v1/items/{item_id}", {"item_id": "abc"})
    assert url == "http://x/v1/items/abc"


def test_build_url_missing_param_raises() -> None:
    with pytest.raises(client.RequestBuildError, match="item_id"):
        build_url("http://x", "/v1/items/{item_id}", {})


def test_send_success(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    captured: dict[str, Any] = {}

    def fake_request(**kwargs: Any) -> FakeResponse:
        captured.update(kwargs)
        return FakeResponse(json_body={"ok": True})

    monkeypatch.setattr(requests, "request", fake_request)
    result = send(
        "get",
        "/v1/items",
        params={"page": 1},
        body="raw body",
        headers={"x-extra": "1"},
        config=config,
        verify=False,
    )
    assert result.status == 200
    assert result.ok
    assert result.json_body == {"ok": True}
    assert captured["method"] == "GET"
    assert captured["url"] == "http://127.0.0.1:8000/v1/items"
    assert captured["data"] == "raw body"
    assert captured["headers"] == {"x-extra": "1"}
    assert captured["proxies"] is None
    assert captured["timeout"] is None  # no timeout by default: requests wait indefinitely
    assert captured["verify"] is False


def test_send_profile_headers_and_proxies(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(requests, "request", lambda **kw: captured.update(kw) or FakeResponse())
    send("get", "/x", profile="live", config=config)
    assert captured["headers"] == {"X-Example-Header": "value"}
    assert captured["proxies"] == {"https": "http://proxy.example.test:8080"}


def test_profile_override_wins(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(requests, "request", lambda **kw: captured.update(kw) or FakeResponse())
    set_profile_override("live")
    send("get", "/x", profile="local", config=config)
    assert captured["url"].startswith("https://api.example.test")


def test_send_transport_error_captured(monkeypatch: pytest.MonkeyPatch, config: Config) -> None:
    def boom(**kwargs: Any) -> Any:
        msg = "refused"
        raise requests.ConnectionError(msg)

    monkeypatch.setattr(requests, "request", boom)
    result = send("get", "/x", config=config)
    assert result.status is None
    assert not result.ok
    assert result.error is not None
    assert "refused" in result.error


def test_render_result_json() -> None:
    result = Result(
        method="GET",
        url="http://x/v1",
        elapsed=0.123,
        status=200,
        json_body={"a": 1},
    )
    text = render_result(result)
    assert "HTTP 200 (0.123s)" in text
    assert '"a": 1' in text


def test_render_result_error() -> None:
    result = Result(method="GET", url="http://x", elapsed=0.5, error="ConnectionError: x")
    text = render_result(result)
    assert "ERROR" in text
    assert "ConnectionError" in text


def test_render_body_non_json() -> None:
    result = Result(method="GET", url="u", elapsed=0.0, status=500, body="<html>boom</html>")
    assert render_body(result) == "<html>boom</html>"


def test_render_body_empty() -> None:
    result = Result(method="GET", url="u", elapsed=0.0, status=204)
    assert render_body(result) == "<empty body>"


def test_render_verbose_includes_headers() -> None:
    result = Result(
        method="GET",
        url="u",
        elapsed=0.0,
        status=200,
        headers={"content-type": "application/json"},
        body="{}",
        json_body={},
    )
    assert "content-type: application/json" in render_result(result, verbose=True)


def test_send_respects_project_root(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dirs: dict[str, Path],
) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.local]\nbase_url = "http://127.0.0.1:9999"\n',
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(requests, "request", lambda **kw: captured.update(kw) or FakeResponse())
    send("get", "/ping", project_root=isolated_dirs["project"])
    assert captured["url"] == "http://127.0.0.1:9999/ping"
