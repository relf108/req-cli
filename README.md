# req-cli

Generate, save, and send API requests through a lazygit-style terminal UI or a
headless CLI.

`req` turns an OpenAPI document into editable Python request modules. It then
discovers those modules from a project-local `requests/` directory or a global
request store and runs them with reusable URL, header, proxy, timeout, and
response handling.

## Features

- Generate plain Python request functions from OpenAPI 3.0 and 3.1 documents.
- Browse and send saved requests from a keyboard-driven TUI.
- Run and list requests from scripts or a shell.
- Share profiles globally while overriding them per project.
- Resolve dynamic header values through trusted Python helper functions.
- Validate hand-written request modules before using them.

## Install

The project requires Python 3.13 or later.

On Linux or macOS without Nix:

```sh
curl -LsSf https://raw.githubusercontent.com/relf108/req-cli/main/install.sh | sh
```

The script installs `uv` if needed, then uses it to install `req-cli` and its
Python runtime globally for the current user. It also updates the shell PATH so
`req` is available in new shells.

With Nix:

```sh
nix profile install .
```

For development:

```sh
uv sync
uv run req
```

## Quick Start

Run these commands from the project that will own the saved requests:

```sh
req profile add local \
    --base-url http://127.0.0.1:8000 \
    --default

req gen ./openapi.yaml
req list
req run items.get_item
req
```

Generation writes editable modules to `./requests/`. Review generated defaults,
parameters, and request bodies before sending them.

## Commands

| Command | Purpose |
|---|---|
| `req` | Open the TUI. |
| `req list` | List discovered request modules and functions. |
| `req run <module>.<function>` | Send one saved request. |
| `req gen <openapi.yaml>` | Generate request modules from an OpenAPI document. |
| `req validate <file.py>` | Validate an existing request module. |
| `req profile add <name> --base-url <url>` | Add or replace a profile. |
| `req profile list` | List the effective profiles. |

Useful examples:

```sh
req run items.get_item --profile staging --verbose
req gen ./openapi.yaml --dry-run
req gen ./openapi.yaml --dest global --app-name demo_app
req run demo_app/items.get_item
req profile add staging \
    --base-url https://api.example.test \
    --header 'X-Client-Name:req-cli' \
    --proxy 'https=http://proxy.example.test:8080' \
    --timeout 30
```

## Configuration

Project configuration is read from `./requests/config.toml` and overrides
global configuration from `~/.config/req-cli/config.toml`. Header profiles can
use a zero-argument function from the adjacent `config_functions.py`:

```toml
[profiles.staging]
base_url = "https://api.example.test"

[profiles.staging.headers]
Authorization = "Bearer {get_access_token}"
```

```python
# ./requests/config_functions.py
import os


def get_access_token() -> str:
    return os.environ["API_TOKEN"]
```

`config_functions.py` is executable trusted code and is hidden from request
module discovery.

## Storage

| Content | Default location |
|---|---|
| Project requests | `./requests/*.py` |
| Project configuration | `./requests/config.toml` |
| Project config functions | `./requests/config_functions.py` |
| Global requests | `~/.local/share/req-cli/requests/<app>/*.py` |
| Global configuration | `~/.config/req-cli/config.toml` |
| Global config functions | `~/.config/req-cli/config_functions.py` |

The global locations respect `XDG_DATA_HOME` and `XDG_CONFIG_HOME`.

## TUI

Vim motions work throughout the UI. Use `j`/`k` to move, `h`/`l` to
collapse or expand, `gg`/`G` to jump, `/` to search, and `n`/`N` to cycle
matches. In the run view, `enter` sends a request, `p` changes profile, `e`
opens the selected request in `$EDITOR`, and `n` opens generation.

## Documentation

See [Using the req Framework](docs/framework.md) for request authoring,
profiles, dynamic headers, OpenAPI generation, validation, TUI workflows, and
current limitations.
