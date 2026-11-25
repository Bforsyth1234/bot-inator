"""#150: POST /api/restart?when=idle waits for no turn and no open card."""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
from ai.deferred_restart import NOW, PENDING, DeferredRestart, daemon_idle  # noqa: E402
from events.event_bus import ContextEvent, EventBus  # noqa: E402


# ── idle check ───────────────────────────────────────────────────────────

class _Loop:
    def done(self) -> bool:
        return False


def _orch(**kw):
    return SimpleNamespace(_pending={}, _pending_code={}, _task=_Loop(), **kw)


def test_idle_when_nothing_runs() -> None:
    assert daemon_idle(_orch(), EventBus()) is True


def test_open_approval_card_is_not_idle() -> None:
    orch = _orch()
    orch._pending["req_1"] = object()
    assert daemon_idle(orch, EventBus()) is False


def test_open_code_review_is_not_idle() -> None:
    orch = _orch()
    orch._pending_code["req_2"] = object()
    assert daemon_idle(orch, EventBus()) is False


def test_turn_running_is_not_idle_until_next_consume() -> None:
    async def run() -> None:
        bus = EventBus()
        orch = _orch()
        await bus.push(ContextEvent(event_type="user_message"))
        assert daemon_idle(orch, bus) is False  # queued
        await bus.consume()  # the loop took it: a turn is running
        assert bus.consumer_busy is True
        assert daemon_idle(orch, bus) is False
        waiter = asyncio.ensure_future(bus.consume())  # turn done, waiting
        await asyncio.sleep(0)
        assert daemon_idle(orch, bus) is True
        waiter.cancel()

    asyncio.run(run())


def test_stopped_loop_does_not_block_restart() -> None:
    async def run() -> None:
        bus = EventBus()
        await bus.push(ContextEvent(event_type="x"))
        await bus.consume()
        orch = _orch()
        orch._task = None  # loop not running (e.g. cancelled mid-turn)
        assert daemon_idle(orch, bus) is True

    asyncio.run(run())


# ── scheduler ────────────────────────────────────────────────────────────

def _scheduler(state: dict, reexec: MagicMock) -> DeferredRestart:
    return DeferredRestart(
        is_idle=lambda: state["idle"], reexec=reexec,
        poll_seconds=0.01, reply_delay=0.0,
    )


def test_idle_reexecs_at_once() -> None:
    async def run() -> None:
        reexec = MagicMock()
        r = _scheduler({"idle": True}, reexec)
        assert r.request(when_idle=True) == NOW
        await asyncio.sleep(0.02)
        reexec.assert_called_once()

    asyncio.run(run())


def test_busy_waits_then_reexecs_once_idle() -> None:
    async def run() -> None:
        state = {"idle": False}
        reexec = MagicMock()
        r = _scheduler(state, reexec)
        assert r.request(when_idle=True) == PENDING
        assert r.pending
        await asyncio.sleep(0.05)
        reexec.assert_not_called()
        state["idle"] = True
        await asyncio.sleep(0.05)
        reexec.assert_called_once()
        assert not r.pending

    asyncio.run(run())


def test_two_pending_requests_reexec_once() -> None:
    async def run() -> None:
        state = {"idle": False}
        reexec = MagicMock()
        r = _scheduler(state, reexec)
        assert r.request(when_idle=True) == PENDING
        assert r.request(when_idle=True) == PENDING
        state["idle"] = True
        await asyncio.sleep(0.05)
        reexec.assert_called_once()

    asyncio.run(run())


def test_manual_restart_while_pending_is_immediate() -> None:
    async def run() -> None:
        reexec = MagicMock()
        r = _scheduler({"idle": False}, reexec)
        assert r.request(when_idle=True) == PENDING
        assert r.request(when_idle=False) == NOW
        await asyncio.sleep(0.02)
        reexec.assert_called_once()
        assert not r.pending  # the wait was cancelled

    asyncio.run(run())


def test_turn_starting_in_reply_delay_defers() -> None:
    async def run() -> None:
        state = {"idle": True}
        reexec = MagicMock()
        r = DeferredRestart(is_idle=lambda: state["idle"], reexec=reexec,
                            poll_seconds=0.01, reply_delay=0.02)
        assert r.request(when_idle=True) == NOW
        state["idle"] = False  # a turn starts before the re-exec
        await asyncio.sleep(0.05)
        reexec.assert_not_called()
        assert r.pending
        state["idle"] = True
        await asyncio.sleep(0.05)
        reexec.assert_called_once()

    asyncio.run(run())


# ── HTTP route ───────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    with patch("ai.orchestrator.Orchestrator._build_agent", return_value=None), patch(
        "ai.mlx_engine.MLXEngine.evaluate_event", new_callable=AsyncMock, return_value="",
    ), patch("ai.mlx_engine.MLXEngine.generate_chat_sync", return_value="NO_PATTERN"):
        import main

        reexec = MagicMock()
        main._deferred_restart = DeferredRestart(
            is_idle=lambda: daemon_idle(main.app.state.orchestrator,
                                        main.app.state.event_bus),
            reexec=reexec, poll_seconds=0.02, reply_delay=0.0,
        )
        with TestClient(main.app, headers=auth.auth_header()) as test_client:
            test_client.reexec = reexec
            yield test_client
        main._deferred_restart = None


def _wait_for(pred, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_route_card_open_waits_then_restarts(client) -> None:
    import main

    orch = main.app.state.orchestrator
    with patch("main._can_self_restart", return_value=True):
        orch._pending["req_card"] = object()  # an approval card is open
        try:
            body = client.post("/api/restart?when=idle").json()
            assert body["restart"] == "pending"
            assert client.get("/api/settings").json()["restart_pending"] is True
            time.sleep(0.15)
            client.reexec.assert_not_called()
        finally:
            orch._pending.pop("req_card", None)  # the card is answered
        assert _wait_for(lambda: client.reexec.called)
        client.reexec.assert_called_once()
        assert client.get("/api/settings").json()["restart_pending"] is False


def test_route_idle_restarts_now(client) -> None:
    with patch("main._can_self_restart", return_value=True):
        assert client.post("/api/restart?when=idle").json()["restart"] == "now"
    assert _wait_for(lambda: client.reexec.called)


def test_route_manual_restart_cancels_wait(client) -> None:
    import main

    orch = main.app.state.orchestrator
    with patch("main._can_self_restart", return_value=True):
        orch._pending["req_card"] = object()
        try:
            assert client.post("/api/restart?when=idle").json()["restart"] == "pending"
            assert client.post("/api/restart").json()["restart"] == "now"
            assert _wait_for(lambda: client.reexec.called)
            assert client.get("/api/settings").json()["restart_pending"] is False
        finally:
            orch._pending.pop("req_card", None)
    client.reexec.assert_called_once()


def test_route_cannot_restart_is_409(client) -> None:
    with patch("main._can_self_restart", return_value=False):
        assert client.post("/api/restart?when=idle").status_code == 409
        assert client.post("/api/restart").status_code == 409
    time.sleep(0.05)
    client.reexec.assert_not_called()


def test_route_rejects_unknown_when(client) -> None:
    with patch("main._can_self_restart", return_value=True):
        assert client.post("/api/restart?when=later").status_code == 400
    client.reexec.assert_not_called()
