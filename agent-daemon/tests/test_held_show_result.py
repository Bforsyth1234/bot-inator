"""#131 follow-up: Show on a held message must visibly do something.

A held iMessage was listed as ``imessage_received`` and Show re-ran a turn
that usually ended in "no action", so the click looked dead. The Held list
now names the sender and text, and a Show that produced no card says so."""
from __future__ import annotations

import asyncio

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
from ai import delivery as dl
from ai.orchestrator import Orchestrator
from events.event_bus import ContextEvent


def test_imessage_and_mail_titles_name_sender_and_content() -> None:
    assert dl._title("imessage_received", {"sender": "+15551234", "text": "  lunch\n at 1?"}) \
        == "Message from +15551234: lunch at 1?"
    assert dl._title("imessage_received", {"sender": "unknown", "text": "hi"}) == "Message: hi"
    assert dl._title("mail_received", {"sender": "Ann <a@x.com>", "subject": "Invoice"}) \
        == "Email from Ann <a@x.com>: Invoice"
    assert dl._title("mail_received", {}) == "Email"


def _orch(cards_in_turn: int) -> tuple[Orchestrator, list[dict]]:
    orch = Orchestrator.__new__(Orchestrator)
    notices: list[dict] = []
    orch.publish_notice = lambda n: notices.append(n) or True  # type: ignore[method-assign]

    async def inner(event):  # stands in for the whole turn
        orch._cards_sent = getattr(orch, "_cards_sent", 0) + cards_in_turn

    orch._handle_event_inner = inner  # type: ignore[method-assign]
    return orch, notices


def _msg(**meta) -> ContextEvent:
    return ContextEvent(event_type="imessage_received",
                        metadata={"sender": "Sam", "text": "running late", **meta})


def test_show_with_no_card_publishes_a_nothing_to_suggest_notice() -> None:
    orch, notices = _orch(cards_in_turn=0)
    asyncio.run(orch._handle_event(_msg(**{dl.BYPASS_KEY: "held"})))
    assert len(notices) == 1
    assert notices[0]["title"] == "Nothing to suggest"
    assert "Message from Sam: running late" in notices[0]["body"]


def test_show_that_sent_a_card_adds_no_notice() -> None:
    orch, notices = _orch(cards_in_turn=1)
    asyncio.run(orch._handle_event(_msg(**{dl.BYPASS_KEY: "held"})))
    assert notices == []


def test_ambient_event_not_opened_by_the_user_adds_no_notice() -> None:
    orch, notices = _orch(cards_in_turn=0)
    asyncio.run(orch._handle_event(_msg()))
    assert notices == []
