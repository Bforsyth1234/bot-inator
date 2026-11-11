"""A tiny stdio MCP server for ``tests/test_mcp_client.py`` (#69).

Tools:
- ``echo(text)``   hinted read-only by the server; returns ``echo:<text>``
  plus the value of ``FAKE_SECRET`` (proves env reaches the child).
- ``add(a, b)``    returns the sum.
- ``crash()``      exits the process without replying.
- ``web-search(q)`` a name with a dash (sanitized to ``web_search``).
- ``t0``..``t<N-1>`` when ``FAKE_TOOL_COUNT=N`` (for the load cap).

``FAKE_DIE_AFTER=<seconds>`` makes the server exit on its own after that
long (a crash the health check must notice).
"""
from __future__ import annotations

import os
import sys
import threading

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

server = MCPServer("fake")


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}:{os.environ.get('FAKE_SECRET', '')}"


@server.tool()
def add(a: int, b: int = 1) -> str:
    """Add two numbers."""
    return str(a + b)


@server.tool()
def crash() -> str:
    """Exit the server without answering."""
    os._exit(3)


@server.tool(name="web-search")
def web_search(q: str) -> str:
    """Pretend to search."""
    return f"results for {q}"


def _make(i: int):
    def extra() -> str:
        return f"t{i}"

    extra.__doc__ = f"Extra tool number {i}."
    return extra


for _i in range(int(os.environ.get("FAKE_TOOL_COUNT", "0") or 0)):
    server.tool(name=f"t{_i}")(_make(_i))

if os.environ.get("FAKE_DIE_AFTER"):
    threading.Timer(float(os.environ["FAKE_DIE_AFTER"]), lambda: os._exit(4)).start()

print("fake mcp server starting", file=sys.stderr)
server.run()
