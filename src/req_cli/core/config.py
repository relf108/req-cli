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
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Condition, RLock, get_ident
from types import ModuleType
from typing import cast

REQUESTS_DIR_NAME = "requests"
CONFIG_FILE_NAME = "config.toml"
CONFIG_FUNCTIONS_FILE_NAME = "config_functions.py"
_FUNCTION_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_CACHE_MAX_ENTRIES = 1024
"""Upper bound for each memoization cache; hit only by pathological path churn."""

_FileStat = tuple[int, int, bool]
"""``(mtime_ns, size, exists)`` snapshot used to validate a cached entry."""


_NO_RESULT = object()


@dataclass
class _InFlight:
    """One process-wide in-flight configuration operation."""

    owner_thread_id: int
    done: bool = False
    result: object = _NO_RESULT
    error: BaseException | None = None


_toml_cache: dict[str, tuple[_FileStat, dict[str, object]]] = {}
"""Parsed TOML by config path; entries are read-only and valid while the stat matches."""

_functions_cache: dict[str, tuple[_FileStat, ModuleType]] = {}
"""Loaded config-function modules by file path, valid while the stat matches."""

_config_cache: dict[tuple[str, _FileStat, str, _FileStat, _FileStat, _FileStat], Config] = {}
"""Merged configs keyed by both TOML stats plus both adjacent functions-file stats."""

_cache_lock = RLock()
"""Serialize cache population so concurrent cold loads share one config."""

_work_condition = Condition()
_in_flight: dict[tuple[object, ...], _InFlight] = {}
_thread_waits_for: dict[int, tuple[object, ...]] = {}


def _stat_of(path: Path) -> _FileStat:
    """Snapshot *path* for cache validation without reading it."""
    try:
        info = path.stat()
    except OSError:
        return (0, 0, False)
    return (info.st_mtime_ns, info.st_size, True)


def _wait_would_cycle(waiter_thread_id: int, owner_thread_id: int) -> bool:
    """Return whether waiting for *owner_thread_id* closes a config dependency cycle."""
    visited: set[int] = set()
    current_thread_id = owner_thread_id
    while current_thread_id not in visited:
        if current_thread_id == waiter_thread_id:
            return True
        visited.add(current_thread_id)
        waiting_key = _thread_waits_for.get(current_thread_id)
        if waiting_key is None:
            return False
        work = _in_flight.get(waiting_key)
        if work is None:
            return False
        current_thread_id = work.owner_thread_id
    return True


def _run_singleflight[T](key: tuple[object, ...], operation: Callable[[], T]) -> T:
    """Run one configuration operation per key and share its result with waiters."""
    thread_id = get_ident()
    with _work_condition:
        work = _in_flight.get(key)
        if work is not None:
            if work.owner_thread_id == thread_id:
                msg = f"recursive configuration resolution for {key!r}"
                raise ConfigError(msg)
            _thread_waits_for[thread_id] = key
            if _wait_would_cycle(thread_id, work.owner_thread_id):
                del _thread_waits_for[thread_id]
                msg = f"cyclic configuration resolution for {key!r}"
                raise ConfigError(msg)
            try:
                while not work.done:
                    _work_condition.wait()
            finally:
                del _thread_waits_for[thread_id]
            if work.error is not None:
                raise work.error
            if work.result is _NO_RESULT:
                msg = f"configuration resolution produced no result for {key!r}"
                raise ConfigError(msg)
            return cast("T", work.result)
        work = _InFlight(owner_thread_id=thread_id)
        _in_flight[key] = work

    try:
        result = operation()
    except BaseException as exc:
        with _work_condition:
            work.error = exc
            work.done = True
            del _in_flight[key]
            _work_condition.notify_all()
        raise

    with _work_condition:
        work.result = result
        work.done = True
        del _in_flight[key]
        _work_condition.notify_all()
    return result


def _invalidate_caches_for(path: Path) -> None:
    """Drop cached entries that could reflect the previous content of *path*."""
    with _cache_lock:
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
    _config_functions_path: Path | None = field(default=None, repr=False, compare=False)

    @property
    def has_dynamic_headers(self) -> bool:
        """Whether this profile has header functions awaiting evaluation."""
        return self._config_functions_path is not None

    def resolved(self) -> Profile:
        """Return this profile with dynamic header values evaluated."""
        if self._config_functions_path is None:
            return self
        module = _load_config_functions(self._config_functions_path)
        headers = {
            header: _interpolate_header(value, module, self._config_functions_path, self.name, header)
            for header, value in self.headers.items()
        }
        return replace(self, headers=headers, _config_functions_path=None)


@dataclass(frozen=True)
class Config:
    """Merged configuration for a project."""

    profiles: dict[str, Profile]
    default_profile: str = "local"
    _resolved_profiles: dict[str, Profile] = field(default_factory=dict, init=False, repr=False, compare=False)
    _resolved_lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def profile(self, name: str | None = None) -> Profile:
        """Return the named profile, or the default profile when *name* is None."""
        key = name or self.default_profile
        try:
            profile = self.profiles[key]
        except KeyError:
            available = ", ".join(sorted(self.profiles)) or "<none>"
            msg = f"unknown profile {key!r} (available: {available})"
            raise ConfigError(msg) from None

        if not profile.has_dynamic_headers:
            return profile
        return self._resolve_dynamic_profile(key, profile)

    def _resolve_dynamic_profile(self, key: str, profile: Profile) -> Profile:
        """Resolve one dynamic profile, sharing in-flight work across threads."""
        with self._resolved_lock:
            resolved = self._resolved_profiles.get(key)
        if resolved is not None:
            return resolved

        def resolve_and_cache() -> Profile:
            resolved = profile.resolved()
            with self._resolved_lock:
                self._resolved_profiles[key] = resolved
            return resolved

        return _run_singleflight(("profile", id(self), key), resolve_and_cache)


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


def _exec_config_functions(path: Path) -> ModuleType:
    """Execute and return one trusted config-function module."""
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
    return module


def _load_config_functions(path: Path) -> ModuleType:
    """Load trusted Python helpers used by the adjacent TOML config."""
    key = str(path)
    stat = _stat_of(path)
    with _cache_lock:
        cached = _functions_cache.get(key)
        if cached is not None and cached[0] == stat:
            return cached[1]

    def load_and_cache() -> ModuleType:
        module = _exec_config_functions(path)
        with _cache_lock:
            cached = _functions_cache.get(key)
            if cached is not None and cached[0] == stat:
                return cached[1]
            if _stat_of(path) == stat:
                _functions_cache[key] = (stat, module)
                if len(_functions_cache) > _CACHE_MAX_ENTRIES:
                    _functions_cache.clear()
                    _functions_cache[key] = (stat, module)
        return module

    return _run_singleflight(("config-functions", key, stat), load_and_cache)


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
    config_functions_path = config_path.with_name(CONFIG_FUNCTIONS_FILE_NAME).resolve()
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
        has_dynamic_headers = any(_FUNCTION_PATTERN.search(value) for value in parsed_headers.values())
        profiles[name] = Profile(
            name=name,
            base_url=base_url.rstrip("/"),
            headers=parsed_headers,
            proxies={str(k): str(v) for k, v in proxies.items()},
            timeout=float(timeout) if timeout is not None else None,
            _config_functions_path=config_functions_path if has_dynamic_headers else None,
        )
    return profiles


def _load_toml(path: Path) -> dict[str, object]:
    """Return parsed TOML, reusing the cached copy while the file is unchanged."""
    with _cache_lock:
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
    with _cache_lock:
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
    with _cache_lock:
        return _save_profile(profile, scope, project_root, make_default=make_default)


def _save_profile(
    profile: Profile,
    scope: str,
    project_root: Path | None,
    *,
    make_default: bool,
) -> Path:
    """Write a profile while the process-wide cache lock is held."""
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

    Results are memoized while none of the underlying files change. Dynamic
    headers are evaluated only when their profile is selected, then cached on
    the loaded config.
    """
    with _cache_lock:
        return _load_config(project_root)


def _load_config(project_root: Path | None) -> Config:
    """Load config while the process-wide cache lock is held."""
    global_path = (global_config_dir() / CONFIG_FILE_NAME).resolve()
    project_path = (project_requests_dir(project_root) / CONFIG_FILE_NAME).resolve()
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
