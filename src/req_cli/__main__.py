"""Entry point: `req` opens the TUI; subcommands offer headless equivalents."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from req_cli.core import discovery
from req_cli.core.client import set_profile_override
from req_cli.core.config import (
    ConfigError,
    Profile,
    global_store_dir,
    load_config,
    project_requests_dir,
    save_profile,
)
from req_cli.core.render import render_result
from req_cli.core.validate import validate_file, validate_source
from req_cli.gen.generate import generate_modules, write_module
from req_cli.gen.openapi import SpecError, app_name, load_spec, parse_endpoints


def _cmd_run(args: argparse.Namespace) -> int:
    target: str = args.target
    if "." not in target:
        print(f"expected <module>.<function>, got {target!r}", file=sys.stderr)
        return 2
    module_name, function_name = target.rsplit(".", 1)
    modules = {(m.scope, m.name): m for m in discovery.discover()}
    module = modules.get(("local", module_name)) or modules.get(("global", module_name))
    if module is None:
        # allow a bare module name to match a unique global `<app>/<module>`
        suffix_matches = [m for m in modules.values() if m.name.split("/")[-1] == module_name]
        if len(suffix_matches) == 1:
            module = suffix_matches[0]
        elif len(suffix_matches) > 1:
            names = ", ".join(sorted(m.name for m in suffix_matches))
            print(f"{module_name!r} is ambiguous: {names}", file=sys.stderr)
            return 2
    if module is None:
        print(f"no saved request module named {module_name!r}", file=sys.stderr)
        return 2
    if args.profile:
        set_profile_override(args.profile)
    try:
        func = discovery.load_function(module.path, function_name)
        result = func()
    except (discovery.DiscoveryError, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(render_result(result, verbose=args.verbose))
    return 0 if result.ok else 1


def _cmd_list(_args: argparse.Namespace) -> int:
    for module in discovery.discover():
        print(f"[{module.scope}] {module.name}  ({module.path})")
        for function in module.functions:
            doc = f"  {function.docstring.splitlines()[0]}" if function.docstring else ""
            print(f"    {module.name}.{function.name}{doc}")
    return 0


def _cmd_gen(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec).expanduser()
    try:
        spec = load_spec(spec_path)
    except SpecError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    endpoints = parse_endpoints(spec)
    if not endpoints:
        print("no endpoints found in spec", file=sys.stderr)
        return 1
    sources = generate_modules(endpoints, spec_path.name)
    if args.dest == "global":
        app = args.app_name or app_name(spec)
        dest = global_store_dir() / app
    else:
        dest = project_requests_dir()
    status = 0
    for name, source in sources.items():
        if args.dry_run:
            print(f"# --- {name}.py ---")
            print(source)
            continue
        report = validate_source(source, dest / f"{name}.py")
        if not report.ok:
            print(f"{name}: generated source failed validation:", file=sys.stderr)
            for issue in report.issues:
                print(f"  {issue}", file=sys.stderr)
            status = 1
            continue
        try:
            path = write_module(source, dest, name, overwrite=args.overwrite)
            print(f"wrote {path}")
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            status = 1
    return status


def _parse_pairs(pairs: list[str], flag: str, separator: str) -> dict[str, str]:
    parsed = {}
    for pair in pairs:
        key, sep, value = pair.partition(separator)
        if not sep or not key:
            print(f"{flag} expects KEY{separator}VALUE, got {pair!r}", file=sys.stderr)
            raise SystemExit(2)
        parsed[key] = value
    return parsed


def _cmd_profile_add(args: argparse.Namespace) -> int:
    profile = Profile(
        name=args.name,
        base_url=args.base_url.rstrip("/"),
        headers=_parse_pairs(args.header, "--header", ":"),
        proxies=_parse_pairs(args.proxy, "--proxy", "="),
        timeout=args.timeout,
    )
    path = save_profile(
        profile,
        scope="global" if args.dest == "global" else "local",
        make_default=args.default,
    )
    print(f"saved profile {args.name!r} to {path}")
    return 0


def _cmd_profile_list(_args: argparse.Namespace) -> int:
    config = load_config()
    for name in sorted(config.profiles):
        profile = config.profiles[name]
        marker = " (default)" if name == config.default_profile else ""
        extras = []
        if profile.headers:
            extras.append(f"{len(profile.headers)} header(s)")
        if profile.proxies:
            extras.append("proxied")
        detail = f"  [{', '.join(extras)}]" if extras else ""
        print(f"{name}{marker}: {profile.base_url}{detail}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate_file(Path(args.file).expanduser())
    for issue in report.issues:
        print(f"{report.path}: {issue}")
    if report.ok:
        print(f"{report.path}: OK")
        return 0
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="req", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="send one saved request")
    run_parser.add_argument("target", help="<module>.<function>, e.g. items.get_item")
    run_parser.add_argument("--profile", help="profile to send with")
    run_parser.add_argument("-v", "--verbose", action="store_true", help="show response headers")
    run_parser.set_defaults(func=_cmd_run)

    list_parser = sub.add_parser("list", help="list saved request modules and functions")
    list_parser.set_defaults(func=_cmd_list)

    gen_parser = sub.add_parser("gen", help="generate request modules from an OpenAPI spec")
    gen_parser.add_argument("spec", help="path to openapi yaml/json")
    gen_parser.add_argument("--dest", choices=("local", "global"), default="local")
    gen_parser.add_argument(
        "--app-name",
        help="global store folder name (default: derived from the spec's info.title)",
    )
    gen_parser.add_argument("--overwrite", action="store_true")
    gen_parser.add_argument("--dry-run", action="store_true", help="print instead of writing")
    gen_parser.set_defaults(func=_cmd_gen)

    profile_parser = sub.add_parser("profile", help="manage header/proxy profiles")
    profile_sub = profile_parser.add_subparsers(dest="profile_command", required=True)
    add_parser = profile_sub.add_parser("add", help="add or replace a profile")
    add_parser.add_argument("name")
    add_parser.add_argument("--base-url", required=True)
    add_parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="KEY:VALUE",
        help="header to send (repeatable)",
    )
    add_parser.add_argument(
        "--proxy",
        action="append",
        default=[],
        metavar="SCHEME=URL",
        help="proxy mapping (repeatable), e.g. https=http://proxy.example.test:8080",
    )
    add_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="request timeout in seconds (default: wait indefinitely)",
    )
    add_parser.add_argument("--dest", choices=("local", "global"), default="local")
    add_parser.add_argument("--default", action="store_true", help="make this the default profile")
    add_parser.set_defaults(func=_cmd_profile_add)
    profile_list_parser = profile_sub.add_parser("list", help="list configured profiles")
    profile_list_parser.set_defaults(func=_cmd_profile_list)

    validate_parser = sub.add_parser("validate", help="validate a premade request module")
    validate_parser.add_argument("file")
    validate_parser.set_defaults(func=_cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = _build_parser().parse_args(argv)
    if args.command is None:
        # deferred so headless subcommands never pay the textual import cost
        from req_cli.tui.app import run  # noqa: PLC0415

        run()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
