"""
Response rendering: status line, timing, and pretty-printed bodies.

This is the shared replacement for the ``show()`` helpers previously copied
into every requests folder - it tolerates non-JSON bodies (e.g. HTML 500
pages) instead of crashing.
"""

from __future__ import annotations

import json

from req_cli.core.client import Result

MAX_BODY_CHARS = 200_000


def render_result(result: Result, *, verbose: bool = False) -> str:
    """Render a :class:`Result` as displayable text."""
    lines = [f"{result.method} {result.url}"]

    if result.error is not None:
        lines.append(f"ERROR ({result.elapsed:.3f}s)")
        lines.append(result.error)
        return "\n".join(lines)

    lines.append(f"HTTP {result.status} ({result.elapsed:.3f}s)")
    if verbose:
        lines.extend(f"{k}: {v}" for k, v in sorted(result.headers.items()))
        lines.append("")
    lines.append(render_body(result))
    return "\n".join(lines)


def render_body(result: Result) -> str:
    """Render just the response body: pretty JSON when possible, raw otherwise."""
    if result.json_body is not None:
        rendered = json.dumps(result.json_body, indent=2, ensure_ascii=False, sort_keys=False)
    else:
        rendered = result.body or "<empty body>"
    if len(rendered) > MAX_BODY_CHARS:
        rendered = rendered[:MAX_BODY_CHARS] + "\n... <truncated>"
    return rendered
