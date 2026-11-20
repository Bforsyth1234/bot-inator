"""#142: a chat turn finishes whether or not the app is listening.

The user left the Chat window while a turn was thinking and the answer never
showed. These tests pin the daemon side of that: a ``user_message`` turn run
through the real orchestrator loop (``start`` -> ``_run`` -> ``_handle_event``
-> ``_handle_user_message``) completes, emits its ``complete`` thought under
the turn's event id and writes the assistant row to ``chat_log``

* with no WebSocket subscriber at all,
* when the only subscriber goes away mid-turn (window / socket gone), and
* when the subscriber's socket is dead (its send raises).

The agent run is a fake that sleeps (a slow model) and returns a fixed reply;
the memory is an in-memory SQLite DB. So a failure here is a daemon bug, not
a UI one, and the fix would belong to ``ai/orchestrator.py``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock, patch

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.memory import Memory, _fallback_embed
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from events.event_bus import ContextEvent, EventBus

QUESTION = "how many emails have i gotten today"
REPLY = "You got 7 emails today."
EVENT_ID = "msg_142abc"


async def _run_turn(
    *,
    subscriber: Optional[Any] = None,
    during_agent: Optional[Any] = None,
) -> tuple[list[tuple[str, str, str]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Push one chat turn through the orchestrator loop.

    Returns (thoughts emitted, chat_log while the agent was thinking,
    chat_log after the turn).
    """
    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())
    engine = MLXEngine()
    engine.evaluate_event = AsyncMock(return_value="")
    mem = Memory(db_path=":memory:", embedder=lambda t: _fallback_embed(t))
    emitted: list[tuple[str, str, str]] = []
    mid_turn_log: list[dict[str, Any]] = []
    done = asyncio.Event()

    with patch.object(Orchestrator, "_build_agent", return_value=None):
        orch = Orchestrator(engine=engine, event_bus=bus, memory=mem, tools=[])
        if subscriber is not None:
            orch.add_subscriber(subscriber)

        real_emit = orch._emit_thought

        async def spy_emit(event_id: str, stage: str, content: str) -> None:
            emitted.append((event_id, stage, content))
            await real_emit(event_id, stage, content)
            if stage == "complete" and event_id == EVENT_ID:
                done.set()

        orch._emit_thought = spy_emit  # type: ignore[method-assign]

        async def slow_agent(eid: str, prompt: str, max_steps: Optional[int] = None) -> str:
            mid_turn_log.extend(mem.get_chat_log(50))
            if during_agent is not None:
                during_agent(orch)
            await asyncio.sleep(0.2)  # the model is thinking
            return REPLY

        orch._run_agent = slow_agent  # type: ignore[method-assign]

        await orch.start()
        try:
            bus.push_threadsafe(ContextEvent(
                event_type="user_message",
                metadata={"event_id": EVENT_ID, "text": QUESTION},
            ))
            await asyncio.wait_for(done.wait(), timeout=10)
            # The assistant row is written after the complete thought.
            for _ in range(100):
                if any(r["role"] == "assistant" for r in mem.get_chat_log(50)):
                    break
                await asyncio.sleep(0.02)
        finally:
            await orch.stop()
    after = mem.get_chat_log(50)
    mem.close()
    return emitted, mid_turn_log, after


def _check_completed(emitted, mid_turn_log, after) -> None:
    completes = [c for (eid, stage, c) in emitted if stage == "complete" and eid == EVENT_ID]
    assert completes == [REPLY]
    stages = [stage for (eid, stage, _c) in emitted if eid == EVENT_ID]
    assert stages[0] == "event_received" and stages[-1] == "complete"
    # Every thought of the turn carries the user's message id: the UI keys
    # the turn by it.
    assert {eid for (eid, _s, _c) in emitted} == {EVENT_ID}
    # While thinking, history has the user row and no assistant row: that is
    # what the app must show as pending after it comes back (#142).
    assert [(r["event_id"], r["role"]) for r in mid_turn_log] == [(EVENT_ID, "user")]
    assert [(r["event_id"], r["role"], r["text"]) for r in after] == [
        (EVENT_ID, "user", QUESTION),
        (EVENT_ID, "assistant", REPLY),
    ]
    assert all(r.get("created_at") for r in after)


def test_chat_turn_completes_with_no_client_connected() -> None:
    emitted, mid, after = asyncio.run(_run_turn())
    _check_completed(emitted, mid, after)


def test_chat_turn_completes_when_the_client_leaves_mid_turn() -> None:
    received: list[Any] = []

    async def sub(message: Any) -> None:
        received.append(message)

    emitted, mid, after = asyncio.run(_run_turn(
        subscriber=sub, during_agent=lambda orch: orch.remove_subscriber(sub),
    ))
    _check_completed(emitted, mid, after)
    # The client saw the start of the turn, not the end: the reply reaches
    # it only through chat_log (GET /api/chats).
    stages = [m.payload.stage for m in received if hasattr(m.payload, "stage")]
    assert "event_received" in stages and "complete" not in stages


def test_chat_turn_completes_when_the_client_socket_is_dead() -> None:
    async def dead(message: Any) -> None:
        raise ConnectionResetError("socket closed")

    emitted, mid, after = asyncio.run(_run_turn(subscriber=dead))
    _check_completed(emitted, mid, after)
