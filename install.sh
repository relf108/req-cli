#!/bin/sh

set -eu

if command -v uv >/dev/null 2>&1; then
    uv_bin=$(command -v uv)
else
    if ! command -v curl >/dev/null 2>&1; then
        echo "error: curl is required to install uv" >&2
        exit 1
    fi

    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    uv_bin="${UV_INSTALL_DIR:-${XDG_BIN_HOME:-$HOME/.local/bin}}/uv"

    if [ ! -x "$uv_bin" ]; then
        echo "error: uv was installed but could not be found" >&2
        exit 1
    fi
fi

echo "Installing req-cli..."
"$uv_bin" tool install --python 3.13 git+https://github.com/relf108/req-cli.git
"$uv_bin" tool update-shell

echo "req-cli installed globally for this user. Open a new shell, then run 'req'."
