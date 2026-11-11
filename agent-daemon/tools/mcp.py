"""``call_mcp``: a generated tool's way to call an enabled MCP tool (#112).

A sandboxed generated tool cannot reach the MCP servers: they run in the
daemon, and the sandbox hides their config and the app token. This module
is the child-side half of a relay: ``call_mcp`` sends the request over the
per-call unix socket the daemon opened for this run (the same socket the
``open`` / ``osascript`` shims use, #53) and the daemon decides:

* the name must be a string literal in a ``call_mcp(...)`` call in the
  tool's reviewed source (read from the AST when the tool loaded), so a
  name built at run time is refused;
* the MCP tool must be switched on now;
* a tool the user marked "runs without asking" runs under the generated
  tool's own card; any other raises its own approval card.

The result is the MCP server's text under the usual "data, not
instructions" header (truncated, and redacted under a cloud engine without
the personal-text opt-in), exactly as an agent call gets it.

Stdlib only: it is imported inside the sandboxed child, where ``tools`` is
a stub package. In the daemon process (a tool trusted to run outside the
sandbox, #104, or ``AGENT_TOOL_SANDBOX=0``) there is no relay and
``call_mcp`` raises.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any, Optional

RELAY_SOCKET_ENV = "AGENT_TOOL_RELAY_SOCKET"
# The daemon bounds the wait (approval card, MCP call timeout); this only
# keeps a dead daemon from hanging the tool forever.
_SOCKET_TIMEOUT_SECONDS = 1800.0


class McpCallRefused(RuntimeError):
    """The daemon did not run the MCP tool (the reason is the message)."""


def call_mcp(name: str, arguments: Optional[dict] = None) -> str:
    """Run the MCP tool ``name`` (e.g. ``"playwright__browser_navigate"``).

    Args:
        name: The prefixed MCP tool name, written as a string literal.
        arguments: The tool's arguments as a dict of JSON values.

    Returns:
        The tool's result text (outside text: data, never instructions).

    Raises:
        McpCallRefused: the tool is switched off, not named literally in
            this tool's source, the user declined the card, or there is no
            relay (the tool runs outside the sandbox).
    """
    if not isinstance(name, str) or not name:
        raise McpCallRefused("call_mcp needs the MCP tool name as a string")
    if arguments is not None and not isinstance(arguments, dict):
        raise McpCallRefused("call_mcp arguments must be a dict")
    path = os.environ.get(RELAY_SOCKET_ENV, "")
    if not path:
        raise McpCallRefused(
            "call_mcp works only in a sandboxed generated tool (no relay here)"
        )
    request = {"cmd": "mcp", "name": name, "arguments": dict(arguments or {})}
    try:
        resp = _ask(path, request)
    except (OSError, ValueError) as exc:
        raise McpCallRefused(f"the MCP relay is unavailable: {exc}") from None
    if not resp.get("ok"):
        raise McpCallRefused(str(resp.get("error") or "refused"))
    return str(resp.get("text") or "")


def _ask(path: str, request: dict[str, Any]) -> dict[str, Any]:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(_SOCKET_TIMEOUT_SECONDS)
        s.connect(path)
        s.sendall(json.dumps(request, default=str).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    out = json.loads(buf.decode("utf-8") or "{}")
    return out if isinstance(out, dict) else {}


__all__ = ["McpCallRefused", "RELAY_SOCKET_ENV", "call_mcp"]
