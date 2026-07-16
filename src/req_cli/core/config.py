"""
Configuration: header profiles, base URLs, proxies, and store locations.

Config is TOML, merged from two locations (project wins over global):

- global: ``~/.config/req-cli/config.toml``
- project: ``./requests/config.toml`` (relative to the project root)

Example::

    default_profile = "local"

    [profiles.local]
    base_url = "http://127.0.0.1:8000"

    [profiles.local.headers]
    X-Example-Header = "value"

    [profiles.live]
    base_url = "https://api.example.test"

    [profiles.live.headers]
    X-Example-Token = "<token>"

    [profiles.live.proxies]
    http = "http://proxy.example.test:8080"
    https = "http://proxy.example.test:8080"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REQUESTS_DIR_NAME = "requests"
CONFIG_FILE_NAME = "config.toml"

CONFIG_TEMPLATE = """\
# req-cli configuration
#
# Profiles bundle a base URL with the headers and proxies to send with it.
# Switch profiles in the TUI with `p`, or `req run --profile <name>`.

default_profile = "local"

[profiles.local]
base_url = "http://127.0.0.1:8000"

# [profiles.local.headers]
# X-Example-Header = "value"

# [profiles.live]
# base_url = "https://api.example.test"
# timeout = 30.0
#
# [profiles.live.headers]
# X-Example-Token = "<token>"
#
# [profiles.live.proxies]
# http = "http://proxy.example.test:8080"
# https = "http://proxy.example.test:8080"
"""


class ConfigError(Exception):
    """Raised when configuration is missing or malformed."""


@dataclass(frozen=True)
class Profile:
    """A named request profile: where to send requests and how to authenticate."""

    name: str
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)
    proxies: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0


@dataclass(frozen=True)
class Config:
    """Merged configuration for a project."""

    profiles: dict[str, Profile]
    default_profile: str = "local"

    def profile(self, name: str | None = None) -> Profile:
        """Return the named profile, or the default profile when *name* is None."""
        key = name or self.default_profile
        try:
            return self.profiles[key]
        except KeyError:
            available = ", ".join(sorted(self.profiles)) or "<none>"
            msg = f"unknown profile {key!r} (available: {available})"
            raise ConfigError(msg) from None


def global_config_dir() -> Path:
    """Return the global config directory (XDG aware)."""
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(base).expanduser() / "req-cli"


def global_store_dir() -> Path:
    """Return the global saved-requests directory (XDG aware)."""
    base = os.environ.get("XDG_DATA_HOME", "~/.local/share")
    return Path(base).expanduser() / "req-cli" / REQUESTS_DIR_NAME


def project_requests_dir(project_root: Path | None = None) -> Path:
    """Return the project-local requests directory."""
    root = project_root or Path.cwd()
    return root / REQUESTS_DIR_NAME


def _parse_profiles(raw: dict[str, object]) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    raw_profiles = raw.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        msg = "'profiles' must be a table"
        raise ConfigError(msg)
    for name, body in raw_profiles.items():
        if not isinstance(body, dict):
            msg = f"profile {name!r} must be a table"
            raise ConfigError(msg)
        base_url = body.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            msg = f"profile {name!r} is missing 'base_url'"
            raise ConfigError(msg)
        headers = body.get("headers", {})
        proxies = body.get("proxies", {})
        timeout = body.get("timeout", 30.0)
        if not isinstance(headers, dict) or not isinstance(proxies, dict):
            msg = f"profile {name!r}: 'headers' and 'proxies' must be tables"
            raise ConfigError(msg)
        if not isinstance(timeout, int | float):
            msg = f"profile {name!r}: 'timeout' must be a number"
            raise ConfigError(msg)
        profiles[name] = Profile(
            name=name,
            base_url=base_url.rstrip("/"),
            headers={str(k): str(v) for k, v in headers.items()},
            proxies={str(k): str(v) for k, v in proxies.items()},
            timeout=float(timeout),
        )
    return profiles


def _load_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path}: invalid TOML: {exc}"
        raise ConfigError(msg) from exc


def config_path(scope: str = "local", project_root: Path | None = None) -> Path:
    """Return the config.toml path for *scope* (``local`` project or ``global``)."""
    if scope == "global":
        return global_config_dir() / CONFIG_FILE_NAME
    return project_requests_dir(project_root) / CONFIG_FILE_NAME


def ensure_config_file(scope: str = "local", project_root: Path | None = None) -> Path:
    """Return the config path for *scope*, seeding it with a template if missing."""
    path = config_path(scope, project_root)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(CONFIG_TEMPLATE)
    return path


def save_profile(
    profile: Profile,
    scope: str = "local",
    project_root: Path | None = None,
    *,
    make_default: bool = False,
) -> Path:
    """Add or replace *profile* in the scope's config.toml, preserving other entries."""
    import tomli_w  # noqa: PLC0415 - only needed on the write path

    path = config_path(scope, project_root)
    raw = _load_toml(path)
    profiles = raw.setdefault("profiles", {})
    if not isinstance(profiles, dict):
        msg = f"{path}: 'profiles' must be a table"
        raise ConfigError(msg)
    entry: dict[str, object] = {"base_url": profile.base_url}
    if profile.headers:
        entry["headers"] = profile.headers
    if profile.proxies:
        entry["proxies"] = profile.proxies
    if profile.timeout != 30.0:  # noqa: PLR2004 - the documented default
        entry["timeout"] = profile.timeout
    profiles[profile.name] = entry
    if make_default or "default_profile" not in raw:
        raw["default_profile"] = profile.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(raw))
    return path


def load_config(project_root: Path | None = None) -> Config:
    """
    Load and merge global + project config.

    Project profiles override global profiles of the same name. When no config
    exists anywhere, a bare ``local`` profile pointing at 127.0.0.1:8000 is
    provided so the tool works out of the box.
    """
    global_raw = _load_toml(global_config_dir() / CONFIG_FILE_NAME)
    project_raw = _load_toml(project_requests_dir(project_root) / CONFIG_FILE_NAME)

    profiles = _parse_profiles(global_raw)
    profiles.update(_parse_profiles(project_raw))
    if not profiles:
        profiles["local"] = Profile(name="local", base_url="http://127.0.0.1:8000")

    default = project_raw.get("default_profile") or global_raw.get("default_profile")
    if default is None:
        default = "local" if "local" in profiles else next(iter(sorted(profiles)))
    if not isinstance(default, str):
        msg = "'default_profile' must be a string"
        raise ConfigError(msg)
    if default not in profiles:
        msg = f"default_profile {default!r} does not match any profile"
        raise ConfigError(msg)

    return Config(profiles=profiles, default_profile=default)
