# req-cli

Generate and send saved API requests via a lazygit-style TUI.

`req` replaces the hand-rolled `requests/` folders scattered across API repos.
It has two roles:

1. **Generate** — parse an OpenAPI spec and generate plain Python request
   modules (one function per endpoint) that hook into shared handlers for URL
   building, headers, proxies, and response rendering. Premade files are
   validated (syntax + contract) before being written into a project.
2. **Run** — browse saved request modules (project-local `requests/` folder or
   the global store) and send them, viewing timing and pretty-printed
   responses.

## Install

```sh
nix profile install .        # via flake
# or for development:
uv sync
uv run req
```

## Usage

```sh
req                  # open the TUI in the current project
req run items.get_item      # headless: send one saved request
req run demo_app/items.get_item  # global-store request, qualified by app
req gen openapi.yaml         # headless: generate request modules from a spec
req gen openapi.yaml --dest global --app-name demo_app   # into the global store
req validate my_requests.py  # validate a premade file
req profile add live --base-url https://api.example.test \
    --header "X-Example-Header:value" --proxy https=http://proxy.example.test:8080
req profile list
```

## Keys

Vim motions are first-class everywhere: `j`/`k` move, `h`/`l`
collapse/expand (and `l` on a request sends it), `gg`/`G` jump top/bottom,
`ctrl+d`/`ctrl+u` half-page, `/` to search the focused pane with `n`/`N`
cycling matches (escape cancels). Arrow keys also work.

In a focused response or preview pane, `y` copies the complete contents. Press
`v` for linewise visual mode, extend the highlighted selection with `j`/`k`,
`gg`, or `G`, then press `y` to copy it; `escape` cancels the selection.

| Key | Run view | Generate view |
|-----|----------|---------------|
| `enter` | send selected request | load spec / toggle |
| `e` | edit request in `$EDITOR` (jumps to the function) | |
| `r` | refresh request list | |
| `p` | cycle profile (needs >1 profile configured) | |
| `n` | open generate view | |
| `c` / `C` | edit local / global config in `$EDITOR` | |
| `w` / `W` | | write selected modules local / global |
| `1` / `2` | focus requests / response pane | focus modules / preview pane |
| `y` / `v` | copy response / visually select lines | copy preview / visually select lines |
| `escape` | | back to run view |
| `q` | quit | |

## Layout

- Project-local requests live in `./requests/` of whatever repo you run `req` from.
- Global requests live in `~/.local/share/req-cli/requests/<app>/`, one folder
  per app named from the spec's `info.title` (editable at generation time).
- Config (base URLs, header profiles, proxies) is read from
  `./requests/config.toml` merged over `~/.config/req-cli/config.toml`.
  Edit it with `c`/`C` in the TUI (opens `$EDITOR`, seeded with a commented
  template) or `req profile add`.
