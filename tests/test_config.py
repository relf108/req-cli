"""Tests for config loading and merging."""

from __future__ import annotations

from pathlib import Path

import pytest

from req_cli.core.config import (
    ConfigError,
    Profile,
    ensure_config_file,
    global_config_dir,
    global_store_dir,
    load_config,
    save_profile,
)


def test_defaults_when_no_config(isolated_dirs: dict[str, Path]) -> None:
    config = load_config()
    assert config.default_profile == "local"
    assert config.profile().base_url == "http://127.0.0.1:8000"


def test_project_overrides_global(isolated_dirs: dict[str, Path]) -> None:
    global_dir = global_config_dir()
    global_dir.mkdir(parents=True)
    (global_dir / "config.toml").write_text(
        """
        default_profile = "live"

        [profiles.live]
        base_url = "https://global.example.test"

        [profiles.live.proxies]
        https = "http://proxy.example.test:8080"
        """,
    )
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        """
        [profiles.live]
        base_url = "https://project.example.test/"

        [profiles.live.headers]
        X-Example-Header = "value"
        """,
    )
    config = load_config()
    live = config.profile("live")
    assert live.base_url == "https://project.example.test"  # trailing slash stripped
    assert live.headers == {"X-Example-Header": "value"}
    # project profile fully replaces the global one of the same name
    assert live.proxies == {}
    assert config.default_profile == "live"


def test_unknown_profile_raises(isolated_dirs: dict[str, Path]) -> None:
    config = load_config()
    with pytest.raises(ConfigError, match="unknown profile"):
        config.profile("nope")


def test_bad_default_profile(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text('default_profile = "missing"')
    with pytest.raises(ConfigError, match="default_profile"):
        load_config()


def test_missing_base_url(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text("[profiles.broken]\ntimeout = 5\n")
    with pytest.raises(ConfigError, match="base_url"):
        load_config()


def test_store_dirs_respect_xdg(isolated_dirs: dict[str, Path]) -> None:
    assert global_store_dir() == isolated_dirs["data"] / "req-cli" / "requests"
    assert global_config_dir() == isolated_dirs["config"] / "req-cli"


def test_ensure_config_file_seeds_template(isolated_dirs: dict[str, Path]) -> None:
    path = ensure_config_file("local")
    assert path == isolated_dirs["project"] / "requests" / "config.toml"
    assert "[profiles.local]" in path.read_text()
    # template parses and yields the default local profile
    assert load_config().profile("local").base_url == "http://127.0.0.1:8000"
    # second call must not clobber
    path.write_text('[profiles.custom]\nbase_url = "http://x"\n')
    ensure_config_file("local")
    assert "custom" in path.read_text()


def test_save_profile_roundtrip(isolated_dirs: dict[str, Path]) -> None:
    live = Profile(
        name="live",
        base_url="https://api.example.test",
        headers={"X-Example-Header": "value"},
        proxies={"https": "http://proxy.example.test:8080"},
        timeout=10.0,
    )
    path = save_profile(live, scope="local")
    assert path == isolated_dirs["project"] / "requests" / "config.toml"
    loaded = load_config().profile("live")
    assert loaded == live
    # first profile written becomes the default
    assert load_config().default_profile == "live"


def test_save_profile_preserves_existing(isolated_dirs: dict[str, Path]) -> None:
    save_profile(Profile(name="a", base_url="http://a"), scope="local")
    save_profile(Profile(name="b", base_url="http://b"), scope="local")
    config = load_config()
    assert set(config.profiles) == {"a", "b"}
    assert config.default_profile == "a"
    save_profile(Profile(name="b", base_url="http://b2"), scope="local", make_default=True)
    config = load_config()
    assert config.profile("b").base_url == "http://b2"
    assert config.default_profile == "b"


def test_save_profile_global_scope(isolated_dirs: dict[str, Path]) -> None:
    path = save_profile(Profile(name="shared", base_url="http://s"), scope="global")
    assert path == isolated_dirs["config"] / "req-cli" / "config.toml"
    assert load_config().profile("shared").base_url == "http://s"
