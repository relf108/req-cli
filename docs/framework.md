# Using the req Framework

This guide describes how to generate, author, configure, discover, and run
requests with `req-cli`. All paths are resolved from the current working
directory unless stated otherwise.

## Concepts

The framework has four main parts:

1. A generator converts an OpenAPI document into editable Python modules.
2. Request functions call the shared `req_cli.core.client.send` function.
3. Profiles provide base URLs, headers, proxies, and timeouts.
4. The CLI and TUI discover and execute public request functions.

Request modules are ordinary Python files. They can be generated once and then
edited, reviewed, and versioned with the application they exercise.

## Installation

`req-cli` requires Python 3.13 or later.

Install from the repository with Nix:

```sh
nix profile install .
```

Set up a development checkout with `uv`:

```sh
uv sync
uv run req --help
```

The remaining examples assume `req` is available on `PATH`. Prefix commands
with `uv run` when running directly from a development checkout.

## Project Quick Start

Run commands from the root of the project that will own `requests/`:

```sh
req profile add local \
    --base-url http://127.0.0.1:8000 \
    --default

req gen ./openapi.yaml
req list
req run items.get_item
```

Open the TUI with:

```sh
req
```

`req` does not search parent directories for a repository root. Starting it
from another directory selects a different project-local `requests/` path.

## Files And Discovery

The default locations are:

```text
Project requests:
  <current-directory>/requests/

Project configuration:
  <current-directory>/requests/config.toml
  <current-directory>/requests/config_functions.py

Global requests:
  ${XDG_CONFIG_HOME:-~/.config}/req-cli/requests/<app>/

Global configuration:
  ${XDG_CONFIG_HOME:-~/.config}/req-cli/config.toml
  ${XDG_CONFIG_HOME:-~/.config}/req-cli/config_functions.py
```

Discovery scans Python files directly under a request directory and one level
below it. Local modules are listed before global modules. A local module takes
precedence over a global module with the same name.

Paths whose names begin with `_` or `.` are ignored. The reserved
`config_functions.py` helper is also ignored, so its functions never appear as
runnable requests.

Listing uses Python's AST parser and does not execute module-level code. A
module is imported only when one of its functions is run.

## Profiles

A profile defines where and how a request is sent:

```toml
default_profile = "staging"

[profiles.staging]
base_url = "https://api.example.test"
timeout = 30

[profiles.staging.headers]
Accept = "application/json"
X-Client-Name = "req-cli"

[profiles.staging.proxies]
https = "http://proxy.example.test:8080"
```

Create the same profile from the CLI:

```sh
req profile add staging \
    --base-url https://api.example.test \
    --header 'Accept:application/json' \
    --header 'X-Client-Name:req-cli' \
    --proxy 'https=http://proxy.example.test:8080' \
    --timeout 30 \
    --default
```

Use `--dest global` to write a shared profile. Project profiles override global
profiles with the same name as complete profiles; individual fields are not
merged. A project `default_profile` takes precedence over the global default.

Per-request headers passed to `send()` override profile headers. A profile
selected with `req run --profile` or in the TUI takes precedence over a
`profile=` value in a request function.

Requests wait indefinitely when neither the function nor the profile sets a
timeout. Editing TOML directly is required to delete a profile. `req profile
add` rewrites the destination TOML and may remove hand-written comments.

## Dynamic Header Values

Header strings can contain one or more `{function_name}` placeholders:

```toml
[profiles.staging]
base_url = "https://api.example.test"

[profiles.staging.headers]
Authorization = "Bearer {get_access_token}"
X-Request-Context = "client-{get_client_id}"
```

Define the functions in `config_functions.py` beside that `config.toml`:

```python
import os


def get_access_token() -> str:
    return os.environ["API_TOKEN"]


def get_client_id() -> str:
    return os.environ.get("API_CLIENT_ID", "example-client")
```

Each helper must accept no arguments and return a string. Project profiles use
project config functions, while global profiles use global config functions.
Missing functions, exceptions, and non-string return values are reported as
configuration errors with profile and header context.

Functions run when their profile is first selected for use, not when
configuration is loaded or profiles are listed. Resolved headers are cached
while configuration remains unchanged. Within that first resolution, each
placeholder invokes its helper, so helpers should remain safe to repeat.

Use `Config.profile()` when consuming a profile. `Config.profiles` retains
unevaluated header templates so callers can list profiles without running
their helpers.

Only header values support interpolation. Text matching `{identifier}` is
treated as a function call; there is currently no escape syntax for a literal
placeholder of that shape.

`config_functions.py` is arbitrary executable Python. Use helpers only from
configuration sources you trust, and obtain credentials from an appropriate
secret provider or environment rather than storing them in request modules.

## Writing Request Modules

The intended request-authoring interface is `Result` and `send` from
`req_cli.core.client`:

```python
from req_cli.core.client import Result, send


def get_item(item_id: str = "example-item") -> Result:
    """GET /v1/items/{item_id}."""
    return send(
        "GET",
        "/v1/items/{item_id}",
        path_params={"item_id": item_id},
        params={"include_details": True},
        headers={"X-Request-Source": "example"},
    )
```

Runnable functions must be synchronous, public, callable without arguments,
and return `Result`. Function parameters are useful for editing defaults, but
every parameter needs a default because the CLI and TUI invoke the function
without arguments.

Public functions are discovered as requests. Prefix utility functions with `_`
to hide them:

```python
def _example_payload() -> dict[str, object]:
    return {"enabled": True}
```

`send()` accepts these request controls:

| Argument | Purpose |
|---|---|
| `method`, `path` | HTTP method and path relative to the profile base URL. |
| `path_params` | Values substituted into `{name}` path placeholders. |
| `params` | Query-string values. |
| `json` | JSON request body. |
| `body` | Raw or form request body passed as request data. |
| `headers` | Headers merged over profile headers. |
| `profile` | Profile used when no CLI or TUI override is active. |
| `timeout` | Per-request timeout overriding the profile timeout. |
| `verify` | TLS certificate verification flag. |

Transport failures are returned in `Result.error`. HTTP error responses remain
normal results with `ok` set to `False`. A result exposes the request method,
final URL, elapsed time, status, response headers, text body, decoded JSON when
available, error text, and the `ok` convenience property.

## Generating From OpenAPI

Generate local modules:

```sh
req gen ./openapi.yaml
```

Preview without writing:

```sh
req gen ./openapi.yaml --dry-run
```

Replace existing generated modules explicitly:

```sh
req gen ./openapi.yaml --overwrite
```

Generate into the global request store:

```sh
req gen ./openapi.yaml \
    --dest global \
    --app-name demo_app
```

Use a simple app slug. Run a global request with its qualified module name:

```sh
req run demo_app/items.get_item
```

The generator supports OpenAPI 3.0 and 3.1 YAML or JSON. It creates one module
per first operation tag, falling back to a meaningful path segment. Function
names come from `operationId` when present.

Generated code is scaffolding. Review it before running or committing it,
especially examples copied from an OpenAPI document. Current generation has
these intentional limits:

- It generates path and query parameters, but not header or cookie parameters.
- It creates JSON bodies from explicit examples or schema skeletons.
- It resolves local `#/components/...` references, not external references.
- It does not configure base URLs from OpenAPI `servers` entries.
- It does not configure authentication from OpenAPI security schemes.
- Optional query parameters may be emitted with placeholder values rather than omitted.
- TUI generation does not overwrite existing files.

## Running Requests From The CLI

List available targets:

```sh
req list
```

Run a local request:

```sh
req run items.get_item
```

Select a profile and include response headers:

```sh
req run items.get_item --profile staging --verbose
```

Local targets win over exact global-name matches. A short global module name is
accepted only when it resolves uniquely; the `<app>/<module>.<function>` form
is unambiguous.

## Using The TUI

Start the TUI from the project directory:

```sh
req
```

Run view keys:

| Key | Action |
|---|---|
| `enter` or `l` | Send the selected request. |
| `e` | Edit the selected function in `$EDITOR`. |
| `r` | Refresh request discovery. |
| `p` | Cycle configured profiles. |
| `n` | Open the generation view. |
| `c` / `C` | Edit project/global configuration. |
| `1` / `2` | Focus requests/response. |
| `q` | Quit. |

Generation view keys:

| Key | Action |
|---|---|
| `enter` | Load an OpenAPI path or toggle a module. |
| `space` | Toggle the selected module. |
| `w` / `W` | Write selected modules locally/globally. |
| `1` / `2` | Focus modules/preview. |
| `escape` | Return to the run view. |

Shared navigation uses `j`/`k`, `h`/`l`, `gg`/`G`, and
`ctrl+d`/`ctrl+u`. Search with `/` and cycle matches with `n`/`N`. Press `y`
to copy a response or preview, or use `v`, movement keys, and `y` to copy a
line selection.

Cancelling a request in the TUI discards its eventual result but cannot
interrupt a socket operation already blocked in the worker thread. Configure a
timeout for services that may not respond.

## Validation And Security

Validate a hand-written module:

```sh
req validate ./requests/items.py
```

Validation reports errors for invalid syntax, missing public functions,
required arguments without defaults, missing function docstrings, `eval()`
dispatch, and a narrow set of obvious hard-coded secret patterns. It warns
about unusual request docstrings and missing `req_cli` imports.

Validation does not copy or install a module. Place a successful file under the
project request directory or global request store yourself. Secret validation
is a guardrail rather than a credential scanner; review generated and
hand-written files before committing them.

## Suggested Workflow

1. Add a profile with a non-production endpoint.
2. Generate modules with `--dry-run` first.
3. Write the modules and review every generated default.
4. Move credentials into profile headers or `config_functions.py`.
5. Validate edited modules.
6. Run a harmless read-only endpoint from the CLI.
7. Use the TUI for iterative request development.
