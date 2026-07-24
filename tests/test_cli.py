"""Tests for the headless CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import requests

from req_cli.__main__ import main

if TYPE_CHECKING:
    from pathlib import Path


def test_gen_writes_modules(
    spec_path: Path,
    isolated_dirs: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["gen", str(spec_path)]) == 0
    requests_dir = isolated_dirs["project"] / "requests"
    written = sorted(path.name for path in requests_dir.glob("*.py"))
    assert written == ["details.py", "items.py", "status.py"]
    out = capsys.readouterr().out
    assert "wrote" in out


def test_gen_refuses_overwrite(spec_path: Path, isolated_dirs: dict[str, Path]) -> None:
    assert main(["gen", str(spec_path)]) == 0
    assert main(["gen", str(spec_path)]) == 1
    assert main(["gen", str(spec_path), "--overwrite"]) == 0


def test_gen_dry_run_writes_nothing(
    spec_path: Path,
    isolated_dirs: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["gen", str(spec_path), "--dry-run"]) == 0
    assert not (isolated_dirs["project"] / "requests").exists()
    assert "def get_item" in capsys.readouterr().out


def test_gen_to_global_store_uses_app_folder(
    spec_path: Path,
    isolated_dirs: dict[str, Path],
) -> None:
    assert main(["gen", str(spec_path), "--dest", "global"]) == 0
    store = isolated_dirs["config"] / "req-cli" / "requests"
    # spec title is "demo" -> app folder "demo"
    assert (store / "demo" / "items.py").exists()
    assert not (store / "items.py").exists()


def test_gen_global_app_name_override(spec_path: Path, isolated_dirs: dict[str, Path]) -> None:
    assert main(["gen", str(spec_path), "--dest", "global", "--app-name", "custom"]) == 0
    store = isolated_dirs["config"] / "req-cli" / "requests"
    assert (store / "custom" / "items.py").exists()


def test_run_qualified_and_bare_global_target(
    spec_path: Path,
    isolated_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeResponse:
        status_code = 200
        text = "{}"
        url = "http://127.0.0.1:8000/x"
        headers: dict[str, str] = {}  # noqa: RUF012

        def json(self) -> dict[str, str]:
            return {}

    monkeypatch.setattr(requests, "request", lambda **kwargs: FakeResponse())
    main(["gen", str(spec_path), "--dest", "global"])
    capsys.readouterr()
    # fully qualified app/module.function
    assert main(["run", "demo/status.get_v1_status"]) == 0
    # bare module name resolves when unique
    assert main(["run", "status.get_v1_status"]) == 0


def test_profile_add_and_list(
    isolated_dirs: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "profile",
                "add",
                "live",
                "--base-url",
                "https://api.example.test/",
                "--header",
                "X-Example-Header:value",
                "--proxy",
                "https=http://proxy.example.test:8080",
                "--default",
            ],
        )
        == 0
    )
    capsys.readouterr()
    assert main(["profile", "list"]) == 0
    out = capsys.readouterr().out
    assert "live (default): https://api.example.test  [1 header(s), proxied]" in out


def test_profile_add_bad_header(isolated_dirs: dict[str, Path]) -> None:
    with pytest.raises(SystemExit):
        main(["profile", "add", "x", "--base-url", "http://x", "--header", "no-colon"])


def test_gen_bad_spec(tmp_path: Path, isolated_dirs: dict[str, Path]) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: a spec")
    assert main(["gen", str(bad)]) == 1


def test_list_shows_generated_modules(
    spec_path: Path,
    isolated_dirs: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["gen", str(spec_path)])
    capsys.readouterr()
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "[local] items" in out
    assert "items.get_item" in out


def test_validate_command(
    spec_path: Path,
    isolated_dirs: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["gen", str(spec_path)])
    module = isolated_dirs["project"] / "requests" / "items.py"
    assert main(["validate", str(module)]) == 0
    assert "OK" in capsys.readouterr().out

    bad = isolated_dirs["project"] / "bad.py"
    bad.write_text("def broken(:\n")
    assert main(["validate", str(bad)]) == 1


def test_run_unknown_module(isolated_dirs: dict[str, Path]) -> None:
    assert main(["run", "nope.missing"]) == 2


def test_run_bad_target(isolated_dirs: dict[str, Path]) -> None:
    assert main(["run", "no-dot"]) == 2


def test_run_executes_function(
    isolated_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeResponse:
        status_code = 200
        text = '{"ok": true}'
        url = "http://127.0.0.1:8000/ping"
        headers: dict[str, str] = {}  # noqa: RUF012

        def json(self) -> dict[str, bool]:
            return {"ok": True}

    monkeypatch.setattr(requests, "request", lambda **kwargs: FakeResponse())
    local = isolated_dirs["project"] / "requests"
    local.mkdir()
    (local / "misc.py").write_text(
        '"""Misc."""\n'
        "from req_cli.core.client import Result, send\n"
        "\n"
        "\n"
        "def ping() -> Result:\n"
        '    """GET /ping."""\n'
        '    return send("GET", "/ping")\n',
    )
    assert main(["run", "misc.ping"]) == 0
    out = capsys.readouterr().out
    assert "HTTP 200" in out
    assert '"ok": true' in out
