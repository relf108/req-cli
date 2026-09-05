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

import importlib.util
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

REQUESTS_DIR_NAME = "requests"
CONFIG_FILE_NAME = "config.toml"
CONFIG_FUNCTIONS_FILE_NAME = "config_functions.py"
_FUNCTION_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_CACHE_MAX_ENTRIES = 1024
"""Upper bound for each memoization cache; hit only by pathological path churn."""

_FileStat = tuple[int, int, bool]
"""``(mtime_ns, size, exists)`` snapshot used to validate a cached entry."""

_toml_cache: dict[str, tuple[_FileStat, dict[str, object]]] = {}
"""Parsed TOML by config path; entries are read-only and valid while the stat matches."""

_functions_cache: dict[str, tuple[_FileStat, ModuleType]] = {}
"""Loaded config-function modules by file path, valid while the stat matches."""

_config_cache: dict[tuple[str, _FileStat, str, _FileStat, _FileStat, _FileStat], Config] = {}
"""Merged configs keyed by both TOML stats plus both adjacent functions-file stats."""


def _stat_of(path: Path) -> _FileStat:
    """Snapshot *path* for cache validation without reading it."""
    try:
        info = path.stat()
    except OSError:
        return (0, 0, False)
    return (info.st_mtime_ns, info.st_size, True)


def _invalidate_caches_for(path: Path) -> None:
    """Drop cached entries that could reflect the previous content of *path*."""
    key = str(path)
    _toml_cache.pop(key, None)
    _functions_cache.pop(key, None)
    stale = [cache_key for cache_key in _config_cache if cache_key[0] == key or cache_key[2] == key]
    for cache_key in stale:
        del _config_cache[cache_key]


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
# Authorization = "Bearer {get_token}"  # calls get_token() from requests/config_functions.py

# [profiles.live]
# base_url = "https://api.example.test"
# timeout = 30.0  # seconds; omit to wait indefinitely (useful when debugging a server)
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
    timeout: float | None = None
    """Seconds before a request times out; ``None`` (the default) waits forever."""


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
    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return Path(base).expanduser() / "req-cli" / REQUESTS_DIR_NAME


def project_requests_dir(project_root: Path | None = None) -> Path:
    """Return the project-local requests directory."""
    root = project_root or Path.cwd()
    return root / REQUESTS_DIR_NAME


def _load_config_functions(path: Path) -> ModuleType:
    """Load trusted Python helpers used by the adjacent TOML config."""
    key = str(path)
    stat = _stat_of(path)
    cached = _functions_cache.get(key)
    if cached is not None and cached[0] == stat:
        return cached[1]
    if not path.is_file():
        msg = f"{path}: required for config function calls"
        raise ConfigError(msg)
    spec = importlib.util.spec_from_file_location("_req_cli_user_config_functions", path)
    if spec is None or spec.loader is None:
        msg = f"{path}: could not load config functions"
        raise ConfigError(msg)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        msg = f"{path}: failed to load config functions: {exc}"
        raise ConfigError(msg) from exc
    _functions_cache[key] = (stat, module)
    if len(_functions_cache) > _CACHE_MAX_ENTRIES:
        _functions_cache.clear()
        _functions_cache[key] = (stat, module)
    return module


def _interpolate_header(value: str, module: ModuleType, path: Path, profile: str, header: str) -> str:
    """Replace function placeholders in one header value."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        function = getattr(module, name, None)
        if not callable(function):
            msg = f"{path}: profile {profile!r} header {header!r}: {name!r} is not a callable config function"
            raise ConfigError(msg)
        try:
            result = function()
        except Exception as exc:
            msg = f"{path}: profile {profile!r} header {header!r}: config function {name!r} failed: {exc}"
            raise ConfigError(msg) from exc
        if not isinstance(result, str):
            msg = (
                f"{path}: profile {profile!r} header {header!r}: "
                f"config function {name!r} returned {type(result).__name__}, not str"
            )
            raise ConfigError(msg)
        return result

    return _FUNCTION_PATTERN.sub(replace, value)


def _parse_profiles(raw: dict[str, object], config_path: Path) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    config_functions: ModuleType | None = None
    config_functions_path = config_path.with_name(CONFIG_FUNCTIONS_FILE_NAME)
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
        timeout = body.get("timeout")
        if not isinstance(headers, dict) or not isinstance(proxies, dict):
            msg = f"profile {name!r}: 'headers' and 'proxies' must be tables"
            raise ConfigError(msg)
        if timeout is not None and not isinstance(timeout, int | float):
            msg = f"profile {name!r}: 'timeout' must be a number"
            raise ConfigError(msg)
        parsed_headers = {str(k): str(v) for k, v in headers.items()}
        if any(_FUNCTION_PATTERN.search(value) for value in parsed_headers.values()):
            if config_functions is None:
                config_functions = _load_config_functions(config_functions_path)
            parsed_headers = {
                key: _interpolate_header(value, config_functions, config_functions_path, str(name), key)
                for key, value in parsed_headers.items()
            }
        profiles[name] = Profile(
            name=name,
            base_url=base_url.rstrip("/"),
            headers=parsed_headers,
            proxies={str(k): str(v) for k, v in proxies.items()},
            timeout=float(timeout) if timeout is not None else None,
        )
    return profiles


def _load_toml(path: Path) -> dict[str, object]:
    """Return parsed TOML, reusing the cached copy while the file is unchanged."""
    key = str(path)
    stat = _stat_of(path)
    cached = _toml_cache.get(key)
    if cached is not None and cached[0] == stat:
        return cached[1]
    try:
        with path.open("rb") as fh:
            parsed: dict[str, object] = tomllib.load(fh)
    except FileNotFoundError:
        parsed = {}
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path}: invalid TOML: {exc}"
        raise ConfigError(msg) from exc
    _toml_cache[key] = (stat, parsed)
    if len(_toml_cache) > _CACHE_MAX_ENTRIES:
        _toml_cache.clear()
        _toml_cache[key] = (stat, parsed)
    return parsed


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
        _invalidate_caches_for(path)
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
    if profile.timeout is not None:
        entry["timeout"] = profile.timeout
    profiles[profile.name] = entry
    if make_default or "default_profile" not in raw:
        raw["default_profile"] = profile.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomli_w.dumps(raw))
    _invalidate_caches_for(path)
    return path


def load_config(project_root: Path | None = None) -> Config:
    """
    Load and merge global + project config.

    Project profiles override global profiles of the same name. When no config
    exists anywhere, a bare ``local`` profile pointing at 127.0.0.1:8000 is
    provided so the tool works out of the box.

    Results are memoized while none of the underlying files change, so the
    per-request :func:`req_cli.core.client.send` path pays only a few stat
    calls instead of reparsing TOML and re-executing config functions.
    """
    global_path = global_config_dir() / CONFIG_FILE_NAME
    project_path = project_requests_dir(project_root) / CONFIG_FILE_NAME
    cache_key = (
        str(global_path),
        _stat_of(global_path),
        str(project_path),
        _stat_of(project_path),
        _stat_of(global_path.with_name(CONFIG_FUNCTIONS_FILE_NAME)),
        _stat_of(project_path.with_name(CONFIG_FUNCTIONS_FILE_NAME)),
    )
    cached_config = _config_cache.get(cache_key)
    if cached_config is not None:
        return cached_config

    global_raw = _load_toml(global_path)
    project_raw = _load_toml(project_path)

    profiles = _parse_profiles(global_raw, global_path)
    profiles.update(_parse_profiles(project_raw, project_path))
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

    config = Config(profiles=profiles, default_profile=default)
    _config_cache[cache_key] = config
    if len(_config_cache) > _CACHE_MAX_ENTRIES:
        _config_cache.clear()
        _config_cache[cache_key] = config
    return config
