"""Built-in follow-ups (#32) in the orchestrator: the pattern prompt calls
the built-in tool instead of generate_custom_tool, and declining that
built-in's approval is remembered as a rejection."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.orchestrator import Orchestrator
from events.event_bus import ContextEvent
from schemas.ws_messages import ApprovalResponsePayload

IDEA = {
    "tool_name": "add_email_invite_to_calendar",
    "description": "Read the invite email and add it with create_event.",
    "expected_logic": "",
    "source": "next_step",
    "builtin_tool": "create_event",
    "event_details": {"subject": "Invitation: Sync @ 2pm", "message_id": 42},
}


def test_builtin_prompt_names_the_builtin_and_forbids_generation() -> None:
    prompt = Orchestrator._build_pattern_prompt(
        ContextEvent(event_type="pattern_detected", metadata=IDEA)
    )
    assert "`create_event`" in prompt
    assert "Do NOT call `generate_custom_tool`" in prompt
    assert "message_id" in prompt and "42" in prompt


def test_plain_idea_still_generates() -> None:
    meta = {k: v for k, v in IDEA.items() if k not in ("builtin_tool", "event_details")}
    prompt = Orchestrator._build_pattern_prompt(
        ContextEvent(event_type="pattern_detected", metadata=meta)
    )
    assert "Call the `generate_custom_tool` tool exactly once" in prompt


def _orch(recorded: list[tuple]) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._pending = {}
    orch.approval_timeout = 2.0
    orch._seq = 0
    orch._next_seq = lambda: 1  # type: ignore[method-assign]

    async def send(_msg: Any) -> None:
        return None

    orch._send = send  # type: ignore[method-assign]
    orch.idea_sources = [SimpleNamespace(record_feedback=lambda n, d, a: recorded.append((n, d, a)))]
    return orch


def _approve_soon(orch: Orchestrator, approved: bool) -> None:
    async def later() -> None:
        while not orch._pending:
            await asyncio.sleep(0.01)
        rid, fut = next(iter(orch._pending.items()))
        fut.set_result(ApprovalResponsePayload(request_id=rid, approved=approved))
    asyncio.get_running_loop().create_task(later())


def test_declined_builtin_is_recorded_as_rejection() -> None:
    recorded: list[tuple] = []

    async def scenario() -> None:
        orch = _orch(recorded)
        orch._current_builtin_idea = IDEA
        _approve_soon(orch, approved=False)
        await orch._request_approval("evt", "create_event", {"kwargs": {"title": "Sync"}}, "r")
        # A different tool in the same turn is not the idea's built-in.
        _approve_soon(orch, approved=False)
        await orch._request_approval("evt", "open_url", {"kwargs": {"url": "x"}}, "r")

    asyncio.run(scenario())
    assert recorded == [(IDEA["tool_name"], IDEA["description"], False)]


def test_no_builtin_idea_records_nothing() -> None:
    recorded: list[tuple] = []

    async def scenario() -> None:
        orch = _orch(recorded)
        orch._current_builtin_idea = None
        _approve_soon(orch, approved=False)
        await orch._request_approval("evt", "create_event", {"kwargs": {"title": "Sync"}}, "r")

    asyncio.run(scenario())
    assert recorded == []
