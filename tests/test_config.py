"""Tests for config loading and merging."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock, Thread

import pytest

from req_cli.core import config as config_module
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


def test_project_header_calls_config_function(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        """
        [profiles.live]
        base_url = "https://example.test"

        [profiles.live.headers]
        Authorization = "Bearer {get_token}"
        X-Combined = "{prefix}-{get_token}"
        """,
    )
    (requests_dir / "config_functions.py").write_text(
        """
def get_token():
    return "secret"

def prefix():
    return "dynamic"
""",
    )

    headers = load_config().profile("live").headers

    assert headers == {"Authorization": "Bearer secret", "X-Combined": "dynamic-secret"}


def test_global_header_uses_global_config_function(isolated_dirs: dict[str, Path]) -> None:
    global_dir = global_config_dir()
    global_dir.mkdir(parents=True)
    (global_dir / "config.toml").write_text(
        """
        [profiles.live]
        base_url = "https://example.test"

        [profiles.live.headers]
        Authorization = "Bearer {get_token}"
        """,
    )
    (global_dir / "config_functions.py").write_text('def get_token():\n    return "global-token"\n')

    assert load_config().profile("live").headers["Authorization"] == "Bearer global-token"


@pytest.mark.parametrize("profile_scope", ["local", "global"])
@pytest.mark.parametrize(
    ("local_source", "expected"),
    [
        (None, "global-token"),
        ("def unrelated():\n    return 'unused'\n", "global-token"),
        ("def get_token():\n    return 'local-token'\n", "local-token"),
    ],
)
def test_config_function_lookup_prefers_local_then_global(
    isolated_dirs: dict[str, Path],
    profile_scope: str,
    local_source: str | None,
    expected: str,
) -> None:
    global_dir = global_config_dir()
    global_dir.mkdir(parents=True)
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    profile_dir = requests_dir if profile_scope == "local" else global_dir
    (profile_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    (global_dir / "config_functions.py").write_text("def get_token():\n    return 'global-token'\n")
    if local_source is not None:
        (requests_dir / "config_functions.py").write_text(local_source)

    assert load_config().profile("live").headers["Authorization"] == f"Bearer {expected}"


def test_header_can_mix_local_and_global_functions(isolated_dirs: dict[str, Path]) -> None:
    global_dir = global_config_dir()
    global_dir.mkdir(parents=True)
    (global_dir / "config_functions.py").write_text("def get_token():\n    return 'global-token'\n")
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "{prefix} {get_token}"\n',
    )
    (requests_dir / "config_functions.py").write_text("def prefix():\n    return 'Bearer'\n")

    assert load_config().profile("live").headers["Authorization"] == "Bearer global-token"


@pytest.mark.parametrize(
    ("local_source", "error"),
    [
        ("get_token = None\n", "not a callable config function"),
        ("def get_token():\n    return 42\n", "returned int, not str"),
        ("def get_token():\n    raise RuntimeError('expired')\n", "failed: expired"),
    ],
)
def test_invalid_local_function_does_not_fall_back_to_global(
    isolated_dirs: dict[str, Path],
    local_source: str,
    error: str,
) -> None:
    global_dir = global_config_dir()
    global_dir.mkdir(parents=True)
    (global_dir / "config_functions.py").write_text("def get_token():\n    return 'global-token'\n")
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    (requests_dir / "config_functions.py").write_text(local_source)

    with pytest.raises(ConfigError, match=error):
        load_config().profile("live")


def test_unused_global_functions_are_not_loaded(isolated_dirs: dict[str, Path]) -> None:
    global_dir = global_config_dir()
    global_dir.mkdir(parents=True)
    (global_dir / "config_functions.py").write_text("raise RuntimeError('must not load')\n")
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    (requests_dir / "config_functions.py").write_text("def get_token():\n    return 'local-token'\n")

    assert load_config().profile("live").headers["Authorization"] == "Bearer local-token"


def test_function_lookup_cache_tracks_both_scopes(isolated_dirs: dict[str, Path]) -> None:
    global_dir = global_config_dir()
    global_dir.mkdir(parents=True)
    global_functions = global_dir / "config_functions.py"
    global_functions.write_text("def get_token():\n    return 'global-token'\n")
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    original = load_config()
    assert original.profile("live").headers["Authorization"] == "Bearer global-token"

    global_functions.write_text("def get_token():\n    return 'updated-global-token'\n")
    updated = load_config()
    assert updated is not original
    assert updated.profile("live").headers["Authorization"] == "Bearer updated-global-token"

    local_functions = requests_dir / "config_functions.py"
    local_functions.write_text("def get_token():\n    return 'local-token'\n")
    overridden = load_config()
    assert overridden is not updated
    assert overridden.profile("live").headers["Authorization"] == "Bearer local-token"

    local_functions.unlink()
    restored = load_config()
    assert restored is not overridden
    assert restored.profile("live").headers["Authorization"] == "Bearer updated-global-token"


def test_dynamic_headers_are_not_evaluated_until_profile_is_selected(
    isolated_dirs: dict[str, Path],
) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.local]\nbase_url = "http://localhost"\n'
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )

    config = load_config()

    assert config.profile("local").headers == {}
    with pytest.raises(ConfigError, match=r"requests/config_functions\.py: required"):
        config.profile("live")


def test_resolved_dynamic_headers_are_cached(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    (requests_dir / "config_functions.py").write_text(
        "calls = 0\n"
        "def get_token():\n"
        "    global calls\n"
        "    calls += 1\n"
        "    if calls > 1:\n"
        "        raise RuntimeError('called twice')\n"
        "    return 'secret'\n",
    )

    config = load_config()

    assert config.profile("live").headers["Authorization"] == "Bearer secret"
    assert config.profile("live").headers["Authorization"] == "Bearer secret"


def test_concurrent_dynamic_header_resolution_runs_function_once(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    (requests_dir / "config_functions.py").write_text(
        "import time\n"
        "calls = 0\n"
        "def get_token():\n"
        "    global calls\n"
        "    calls += 1\n"
        "    time.sleep(0.05)\n"
        "    if calls > 1:\n"
        "        raise RuntimeError('called twice')\n"
        "    return 'secret'\n",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        profiles = list(executor.map(lambda _: load_config().profile("live"), range(2)))

    assert [profile.headers["Authorization"] for profile in profiles] == [
        "Bearer secret",
        "Bearer secret",
    ]


def test_concurrent_cold_loads_share_config(isolated_dirs: dict[str, Path], monkeypatch) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text('[profiles.local]\nbase_url = "http://localhost"\n')
    original_load_toml = config_module.__dict__["_load_toml"]
    call_lock = Lock()
    calls = 0

    def slow_load_toml(path):
        nonlocal calls
        with call_lock:
            calls += 1
        time.sleep(0.05)
        return original_load_toml(path)

    monkeypatch.setattr(config_module, "_load_toml", slow_load_toml)

    with ThreadPoolExecutor(max_workers=2) as executor:
        configs = list(executor.map(lambda _: load_config(), range(2)))

    assert configs[0] is configs[1]
    assert calls == 2


def test_dynamic_header_can_select_another_profile(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.first]\nbase_url = "https://first.example.test"\n'
        '[profiles.first.headers]\nX-First = "{first}"\n'
        '[profiles.second]\nbase_url = "https://second.example.test"\n'
        '[profiles.second.headers]\nX-Second = "{second}"\n',
    )
    (requests_dir / "config_functions.py").write_text(
        "from req_cli.core.config import load_config\n"
        "def first():\n"
        '    return load_config().profile("second").headers["X-Second"]\n'
        "def second():\n"
        "    return 'nested'\n",
    )
    result = []
    errors = []

    def resolve() -> None:
        try:
            result.append(load_config().profile("first").headers["X-First"])
        except Exception as exc:  # noqa: BLE001 - assertion reports helper failures
            errors.append(exc)

    thread = Thread(target=resolve, daemon=True)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive(), "dynamic profile resolution deadlocked"
    assert errors == []
    assert result == ["nested"]


def test_cross_thread_dynamic_profile_cycle_fails_without_deadlock(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.first]\nbase_url = "https://first.example.test"\n'
        '[profiles.first.headers]\nX-First = "{first}"\n'
        '[profiles.second]\nbase_url = "https://second.example.test"\n'
        '[profiles.second.headers]\nX-Second = "{second}"\n',
    )
    (requests_dir / "config_functions.py").write_text(
        "from threading import Barrier\n"
        "from req_cli.core.config import load_config\n"
        "barrier = Barrier(2)\n"
        "def first():\n"
        "    barrier.wait()\n"
        '    return load_config().profile("second").headers["X-Second"]\n'
        "def second():\n"
        "    barrier.wait()\n"
        '    return load_config().profile("first").headers["X-First"]\n',
    )
    errors = []

    def resolve(name: str) -> None:
        try:
            load_config().profile(name)
        except Exception as exc:  # noqa: BLE001 - assertion reports helper failures
            errors.append(exc)

    threads = [
        Thread(target=resolve, args=("first",), daemon=True),
        Thread(target=resolve, args=("second",), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert not any(thread.is_alive() for thread in threads), "cyclic profile resolution deadlocked"
    assert len(errors) == 2
    assert all(isinstance(error, ConfigError) for error in errors)
    assert all("cyclic configuration resolution" in str(error) for error in errors)


def test_config_function_module_load_does_not_hold_cache_lock(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    (requests_dir / "config_functions.py").write_text(
        "from threading import Thread\n"
        "from req_cli.core.config import load_config\n"
        "thread = Thread(target=load_config)\n"
        "thread.start()\n"
        "thread.join(timeout=1)\n"
        "if thread.is_alive():\n"
        "    raise RuntimeError('cache lock held during module load')\n"
        "def get_token():\n"
        "    return 'secret'\n",
    )

    assert load_config().profile("live").headers["Authorization"] == "Bearer secret"


def test_concurrent_profiles_load_config_function_module_once(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.first]\nbase_url = "https://first.example.test"\n'
        '[profiles.first.headers]\nX-First = "{first}"\n'
        '[profiles.second]\nbase_url = "https://second.example.test"\n'
        '[profiles.second.headers]\nX-Second = "{second}"\n',
    )
    load_log = isolated_dirs["project"] / "module-loads.log"
    (requests_dir / "config_functions.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(0.05)\n"
        f"with Path({str(load_log)!r}).open('a') as log:\n"
        "    log.write('loaded\\n')\n"
        "def first():\n"
        "    return 'first'\n"
        "def second():\n"
        "    return 'second'\n",
    )
    config = load_config()

    with ThreadPoolExecutor(max_workers=2) as executor:
        profiles = list(executor.map(config.profile, ("first", "second")))

    assert [profile.name for profile in profiles] == ["first", "second"]
    assert load_log.read_text().splitlines() == ["loaded"]


def test_relative_project_root_keeps_absolute_config_functions_path(
    isolated_dirs: dict[str, Path],
    monkeypatch,
) -> None:
    relative_root = Path("nested")
    requests_dir = relative_root / "requests"
    requests_dir.mkdir(parents=True)
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    (requests_dir / "config_functions.py").write_text("def get_token():\n    return 'secret'\n")

    config = load_config(relative_root)
    absolute_config = load_config((isolated_dirs["project"] / relative_root).resolve())
    monkeypatch.chdir(isolated_dirs["project"].parent)

    assert config is absolute_config
    assert config.profile("live").headers["Authorization"] == "Bearer secret"


def test_header_function_requires_adjacent_config_functions(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )

    with pytest.raises(ConfigError, match=r"requests/config_functions\.py: required"):
        load_config().profile("live")


@pytest.mark.parametrize(
    ("function", "error"),
    [
        ("get_token = 'not callable'", "not a callable config function"),
        ("def get_token():\n    return 42", "returned int, not str"),
        ("def get_token():\n    raise RuntimeError('expired')", "config function 'get_token' failed: expired"),
    ],
)
def test_invalid_header_function(isolated_dirs: dict[str, Path], function: str, error: str) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text(
        '[profiles.live]\nbase_url = "https://example.test"\n'
        '[profiles.live.headers]\nAuthorization = "Bearer {get_token}"\n',
    )
    (requests_dir / "config_functions.py").write_text(function)

    with pytest.raises(ConfigError, match=error):
        load_config().profile("live")


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


def test_timeout_defaults_to_none(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text('[profiles.local]\nbase_url = "http://x"\n')
    assert load_config().profile("local").timeout is None


def test_explicit_timeout_is_kept(isolated_dirs: dict[str, Path]) -> None:
    requests_dir = isolated_dirs["project"] / "requests"
    requests_dir.mkdir()
    (requests_dir / "config.toml").write_text('[profiles.local]\nbase_url = "http://x"\ntimeout = 5\n')
    assert load_config().profile("local").timeout == 5.0


def test_store_dirs_respect_xdg(isolated_dirs: dict[str, Path]) -> None:
    assert global_store_dir() == isolated_dirs["config"] / "req-cli" / "requests"
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
