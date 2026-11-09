"""Approval wrapping must not stack when the agent is rebuilt (#48).

``_wrap_tool_for_approval`` replaces ``forward`` on the shared tool object
and ``_build_agent`` runs again after every dynamic-tool load. Before the
fix each rebuild wrapped the previous wrapper, so one call raised one
approval request per layer, and stale layers kept a closed event loop.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.orchestrator import Orchestrator
from smolagents import tool


@tool
def ping_tool(text: str) -> str:
    """Echo text back.

    Args:
        text: Text to echo.
    """
    return f"pong:{text}"


def _fake_orchestrator(loop: asyncio.AbstractEventLoop, requests: list[str]) -> Any:
    orch = Orchestrator.__new__(Orchestrator)
    orch.event_bus = SimpleNamespace(loop=loop)
    orch._turn_tool_calls = set()
    orch._current_event_id = "evt_test"
    orch.approval_timeout = 5.0

    async def request_approval(**kwargs: Any) -> tuple[bool, None]:
        requests.append(kwargs["tool_name"])
        return True, None

    async def emit_thought(*_: Any) -> None:
        return None

    orch._request_approval = request_approval  # type: ignore[method-assign]
    orch._emit_thought = emit_thought  # type: ignore[method-assign]
    return orch


def test_rewrapping_keeps_one_approval_per_call() -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    requests: list[str] = []
    try:
        orch = _fake_orchestrator(loop, requests)
        wrapped = ping_tool
        for _ in range(3):  # three rebuilds, even re-wrapping a wrapper
            wrapped = orch._wrap_tool_for_approval(wrapped)
        assert wrapped.forward("hi") == "pong:hi"
        assert requests == ["ping_tool"]
        # The shared tool object is untouched: no gate on direct calls.
        assert ping_tool.forward("direct") == "pong:direct"
        assert requests == ["ping_tool"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_new_orchestrator_replaces_stale_loop() -> None:
    """A wrapper from an earlier orchestrator (closed loop) never leaks."""
    old_loop = asyncio.new_event_loop()
    old_loop.close()
    _fake_orchestrator(old_loop, [])._wrap_tool_for_approval(ping_tool)
    assert ping_tool.forward("plain") == "pong:plain"

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    requests: list[str] = []
    try:
        wrapped = _fake_orchestrator(loop, requests)._wrap_tool_for_approval(ping_tool)
        assert wrapped.forward("x") == "pong:x"
        assert requests == ["ping_tool"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_null_kwargs_are_dropped_so_defaults_apply() -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        orch = _fake_orchestrator(loop, [])
        wrapped = orch._wrap_tool_for_approval(ping_tool)
        assert wrapped.forward(text="hi", unused=None) == "pong:hi"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
