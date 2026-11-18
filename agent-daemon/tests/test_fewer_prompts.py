"""#115 one card per burst of email suggestions, #116 Always allow,
#117 the countdown on undoable cards."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import always_allow, card_batch
from ai.action_journal import UNDOABLE_TOOLS, ActionJournal
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from events.event_bus import EventBus
from schemas.ws_messages import ApprovalResponsePayload
from tests.test_notices_privacy import _LoopThread, _drain


class _ManualTimer:
    def __init__(self, delay: float, fn: Any) -> None:
        self.fn, self.started, self.cancelled = fn, False, False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


def _reminder_tool(calls: list[dict[str, Any]]) -> Any:
    def forward(title: str, due: str = "", list_name: str = "", notes: str = "") -> dict[str, Any]:
        calls.append({"title": title})
        return {"status": "ok", "reminder_id": f"r{len(calls)}", "title": title}

    return SimpleNamespace(name="create_reminder", forward=forward,
                           inputs={"title": {}, "due": {}, "list_name": {}, "notes": {}})


def _orch(loop: asyncio.AbstractEventLoop, tool: Any) -> Orchestrator:
    bus = EventBus()
    bus.bind_loop(loop)
    return Orchestrator(engine=MLXEngine(), event_bus=bus, tools=[tool])


def _mail_turn(orch: Orchestrator) -> None:
    orch._turn_kind = "event"
    orch._current_event_type = "mail_received"
    orch._current_idea = None
    orch._turn_done = {}
    orch._turn_tool_calls = set()


# ---- #115 batching ------------------------------------------------------


def test_mail_turn_reminders_queue_into_one_card() -> None:
    pushed: list[Any] = []
    timers: list[_ManualTimer] = []
    batcher = card_batch.CardBatcher(
        pushed.append, window_s=180,
        timer_factory=lambda d, fn: timers.append(_ManualTimer(d, fn)) or timers[-1],
    )
    calls: list[dict[str, Any]] = []
    with _LoopThread() as loop:
        orch = _orch(loop, _reminder_tool(calls))
        orch.card_batcher = batcher
        cards: list[Any] = []

        async def request_approval(**kw: Any) -> tuple[bool, None]:
            cards.append(kw)
            return True, None

        orch._request_approval = request_approval  # type: ignore[method-assign]
        wrapped = orch._wrap_tool_for_approval(orch.tools[0])
        results = []
        for title in ("Pay invoice", "Call Sam"):
            _mail_turn(orch)
            results.append(asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(wrapped.forward, title=title), loop).result(5))
        _drain(loop)
    assert cards == [] and calls == []
    assert [r["queued"] for r in results] == [True, True]
    assert len(timers) == 1 and timers[0].started
    assert batcher.flush() is True
    assert len(pushed) == 1
    event = pushed[0]
    assert event.event_type == card_batch.BATCH_EVENT_TYPE
    actions = event.metadata["prepared_offer"]["actions"]
    assert [a["args"]["title"] for a in actions] == ["Pay invoice", "Call Sam"]
    assert batcher.flush() is False  # empty now


def test_chat_turn_still_asks_at_once() -> None:
    batcher = card_batch.CardBatcher(lambda e: None, window_s=180,
                                     timer_factory=lambda d, fn: _ManualTimer(d, fn))
    assert not batcher.eligible("create_reminder", "user_message", "chat")
    assert not batcher.eligible("draft_email", "mail_received", "event")
    assert batcher.eligible("create_reminder", "mail_received", "event")
    off = card_batch.CardBatcher(lambda e: None, window_s=0)
    assert not off.eligible("create_reminder", "mail_received", "event")


def test_max_items_flushes_without_the_timer() -> None:
    pushed: list[Any] = []
    batcher = card_batch.CardBatcher(pushed.append, window_s=180, max_items=2,
                                     timer_factory=lambda d, fn: _ManualTimer(d, fn))
    batcher.add("create_reminder", {"title": "a"}, event_id="e1", trigger="mail_received")
    batcher.add("create_reminder", {"title": "b"}, event_id="e2", trigger="mail_received")
    assert len(pushed) == 1 and batcher.pending() == 0


@pytest.mark.parametrize("approve", [True, False])
def test_batch_card_runs_only_ticked_actions_after_approval(approve: bool) -> None:
    calls: list[dict[str, Any]] = []
    event = card_batch.build_event([
        card_batch._Item("create_reminder", {"title": "keep"}, "e1", "mail_received"),
        card_batch._Item("create_reminder", {"title": "drop"}, "e2", "mail_received"),
    ])
    with _LoopThread() as loop:
        orch = _orch(loop, _reminder_tool(calls))
        cards: list[Any] = []

        async def request_approval(**kw: Any) -> tuple[bool, Any]:
            cards.append(kw)
            edited = {"actions": [
                {"tool": "create_reminder", "enabled": True},
                {"tool": "create_reminder", "enabled": False},
            ]}
            return approve, edited

        orch._request_approval = request_approval  # type: ignore[method-assign]
        asyncio.run_coroutine_threadsafe(orch._handle_event(event), loop).result(5)
        _drain(loop)
    assert len(cards) == 1 and cards[0]["tool_name"] == "prepared_offer"
    assert calls == ([{"title": "keep"}] if approve else [])


# ---- #116 Always allow --------------------------------------------------


def test_eligible_tools_are_the_undoable_ones() -> None:
    assert always_allow.ELIGIBLE_TOOLS == UNDOABLE_TOOLS
    store = always_allow.AlwaysAllow(None)
    assert not store.grant("draft_email", "mail_received")
    assert not store.grant("send_imessage", "user_message")
    assert store.grant("create_reminder", "mail_received")
    assert store.allows("create_reminder", "mail_received")
    assert not store.allows("create_reminder", "user_message")
    assert store.revoke("create_reminder|mail_received")
    assert not store.allows("create_reminder", "mail_received")


def test_always_allowed_call_runs_without_a_card_and_is_journaled() -> None:
    calls: list[dict[str, Any]] = []
    with _LoopThread() as loop:
        orch = _orch(loop, _reminder_tool(calls))
        orch.always_allow = always_allow.AlwaysAllow(None)
        orch.always_allow.grant("create_reminder", "mail_received")
        ops = SimpleNamespace(reminder_fingerprint=lambda rid: "fp")
        orch.action_journal = ActionJournal(None, ops=ops)  # type: ignore[arg-type]
        orch.card_batcher = card_batch.CardBatcher(
            lambda e: None, window_s=180, timer_factory=lambda d, fn: _ManualTimer(d, fn))
        cards: list[Any] = []

        async def request_approval(**kw: Any) -> tuple[bool, None]:
            cards.append(kw)
            return False, None

        orch._request_approval = request_approval  # type: ignore[method-assign]
        wrapped = orch._wrap_tool_for_approval(orch.tools[0])
        _mail_turn(orch)
        import importlib
        sn = importlib.import_module("tools.show_notification")
        orig, sn.show_notification = sn.show_notification, lambda *a, **k: None
        try:
            result = asyncio.run_coroutine_threadsafe(
                asyncio.to_thread(wrapped.forward, title="Pay invoice"), loop).result(5)
        finally:
            sn.show_notification = orig
        _drain(loop)
        rows = orch.action_journal.rows()
    assert cards == [] and calls == [{"title": "Pay invoice"}]
    assert result["status"] == "ok"
    assert any(n.startswith("always_allow_run:") for n in orch._notices)
    assert rows and rows[0]["source"] == "always_allow" and rows[0]["why"] == "mail_received"


def _run_request(orch: Orchestrator, loop: Any, resp: dict[str, Any], **card: Any) -> Any:
    frames: list[Any] = []

    async def send(msg: Any) -> None:
        frames.append(msg)
        if getattr(msg, "type", "") == "approval_request":
            rid = msg.payload.request_id
            loop.call_soon(orch.submit_approval_response,
                           ApprovalResponsePayload(request_id=rid, **resp))

    orch._send = send  # type: ignore[method-assign]
    out = asyncio.run_coroutine_threadsafe(
        orch._request_approval(event_id="e1", **card), loop).result(5)
    return out, [f for f in frames if getattr(f, "type", "") == "approval_request"]


def test_click_grants_only_what_the_card_offered() -> None:
    with _LoopThread() as loop:
        orch = _orch(loop, _reminder_tool([]))
        orch.always_allow = always_allow.AlwaysAllow(None)
        orch.idle_seconds = lambda: 5.0
        _mail_turn(orch)
        (ok, _), reqs = _run_request(
            orch, loop, {"approved": True, "always_allow": True},
            tool_name="draft_email", tool_args={"kwargs": {}}, reasoning="r")
        assert ok and reqs[0].payload.always_allow_label is None
        assert orch.always_allow.list() == []
        (ok, _), reqs = _run_request(
            orch, loop, {"approved": True, "always_allow": True},
            tool_name="create_reminder", tool_args={"kwargs": {"title": "x"}}, reasoning="r")
        _drain(loop)
    assert reqs[0].payload.always_allow_label == "Always add reminders from email"
    assert orch.always_allow.allows("create_reminder", "mail_received")


def test_two_undos_switch_always_allow_off() -> None:
    store = always_allow.AlwaysAllow(None)
    store.grant("create_reminder", "mail_received")
    entry = {"source": "always_allow", "tool": "create_reminder", "why": "mail_received"}
    store.on_undo(entry, {"status": "undone"})
    assert store.allows("create_reminder", "mail_received")
    store.on_undo({**entry, "source": "approval"}, {"status": "undone"})
    store.on_undo(entry, {"status": "undone"})
    assert not store.allows("create_reminder", "mail_received")


# ---- #117 countdown -----------------------------------------------------


def test_countdown_only_for_undoable_cards_while_the_user_is_active(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_COUNTDOWN_SECONDS", raising=False)
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    orch.idle_seconds = lambda: 5.0
    assert orch._countdown_seconds(True) == 10
    assert orch._countdown_seconds(False) is None
    orch.idle_seconds = lambda: 600.0
    assert orch._countdown_seconds(True) is None
    orch.idle_seconds = lambda: None  # unreadable: nobody known to be there
    assert orch._countdown_seconds(True) is None
    orch.idle_seconds = lambda: 5.0
    orch._turn_from_phone = True
    assert orch._countdown_seconds(True) is None
    orch._turn_from_phone = False
    monkeypatch.setenv("AGENT_COUNTDOWN_SECONDS", "0")
    assert orch._countdown_seconds(True) is None


def test_card_carries_the_countdown() -> None:
    with _LoopThread() as loop:
        orch = _orch(loop, _reminder_tool([]))
        orch.idle_seconds = lambda: 1.0
        _mail_turn(orch)
        (ok, _), reqs = _run_request(
            orch, loop, {"approved": True, "auto_approved": True},
            tool_name="create_reminder", tool_args={"kwargs": {"title": "x"}}, reasoning="r")
        (_, _), deny = _run_request(
            orch, loop, {"approved": False},
            tool_name="draft_email", tool_args={"kwargs": {}}, reasoning="r")
        _drain(loop)
    assert ok and reqs[0].payload.auto_approve_seconds == 10
    assert deny[0].payload.auto_approve_seconds is None
