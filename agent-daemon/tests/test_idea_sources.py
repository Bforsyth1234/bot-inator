"""Tests for Phase 5.2 idea sources: next-step and chat-gap suggestions."""

from __future__ import annotations

import asyncio
import json

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.idea_sources import ChatGapDetector, NextStepSuggester, parse_suggestion
from events.event_bus import ContextEvent, EventBus

PROPOSAL = json.dumps({
    "tool_name": "install_dmg",
    "description": "Mount a downloaded .dmg and copy the app to /Applications.",
    "expected_logic": "Input: path (str).\n1. hdiutil attach.\n2. Copy .app.",
})


def _file(name: str, action: str = "created") -> ContextEvent:
    return ContextEvent(event_type="file", metadata={"path": f"/Users/me/Downloads/{name}", "action": action})


def _msg(text: str) -> ContextEvent:
    return ContextEvent(event_type="imessage_received", metadata={"sender": "+1555", "text": text})


def _mail(subject: str, mailbox: str = "INBOX", mid: int = 42) -> ContextEvent:
    return ContextEvent(event_type="mail_received", metadata={
        "sender": "Sam <sam@example.com>", "subject": subject,
        "date": "2026-11-09T10:00:00", "mailbox": mailbox, "message_id": mid,
    })


def _meeting(video_url: str = "https://zoom.us/j/123456") -> ContextEvent:
    return ContextEvent(event_type="event_starting_soon", metadata={
        "title": "Standup", "start": "2026-11-09T10:00:00", "location": "",
        "url": "", "calendar": "Work", "video_url": video_url,
    })


@pytest.mark.parametrize("event,kind", [
    (_file("Zoom.dmg"), "installer_downloaded"),
    (_file("Zoom.dmg", action="renamed"), "installer_downloaded"),
    (_file("Archive.ZIP"), "archive_downloaded"),
    (_file("statement.pdf"), None),
    (_file("Screenshot 2026-11-08 at 10.00.00.png"), None),
    (_file("Zoom.dmg.crdownload"), None),
    (_file("Zoom.dmg", action="modified"), None),
    (_file(".DS_Store"), None),
    (_file("notes.txt"), None),
    (_msg("Your UPS package 1Z999AA10123456784 ships today"), "tracking_number"),
    (_msg("FedEx shipped, tracking 123456789012"), "tracking_number"),
    (_msg("call me at 415555121234"), None),
    (_msg("Your verification code is 482913"), None),
    (_msg("dinner tomorrow at 7pm?"), "plan_with_time"),
    (_msg("look https://example.org/x"), None),
    (_msg("ok sounds good"), None),
    (ContextEvent(event_type="app_activated", app_name="Safari"), None),
    (_file("invite.ics"), "calendar_invite_downloaded"),
    # calendar: video link
    (_meeting(), "meeting_with_video_link"),
    (_meeting(""), None),
    (_meeting("zoom.us/j/1"), None),
    (_meeting("javascript:alert(1)"), None),
    # mail: invites
    (_mail("Invitation: Sync @ Tue Nov 10, 2026 2pm - 3pm (PST)"), "mail_invite"),
    (_mail("Updated invitation: Design review @ Wed Nov 11"), "mail_invite"),
    (_mail("Meeting request: Q4 planning"), "mail_invite"),
    (_mail("You have an invite for coffee chat at 3pm"), "mail_invite"),
    (_mail("Accepted: Sync @ Tue Nov 10, 2026 2pm"), None),
    (_mail("Declined: Invitation: Sync @ Tue Nov 10"), None),
    (_mail("Sam invited you to join the Acme workspace on Slack"), None),
    (_mail("Invitation to collaborate on design.fig"), None),
    (_mail("Your invite code is inside"), None),
    (_mail("Invite your friends and get $10"), None),
    (_mail("Invitation: Sync @ Tue", mailbox="Junk"), None),
    # mail: shipping
    (_mail("Your Amazon.com order #112-334 has shipped"), "mail_shipping"),
    (_mail("Shipped: \u201cUSB-C cable\u201d"), "mail_shipping"),
    (_mail("Your package is out for delivery"), "mail_shipping"),
    (_mail("Your order is on its way!"), "mail_shipping"),
    (_mail("Tracking number for your shipment"), "mail_shipping"),
    (_mail("Free shipping on all orders this weekend"), None),
    (_mail("Spring is on its way: 30% off sale"), None),
    (_mail("Shipping policy update"), None),
    (_mail("Your order has shipped - 20% off your next order"), None),
    # mail: request with a deadline
    (_mail("Please review the contract by Friday"), "mail_deadline"),
    (_mail("Action required: submit your timesheet by 11/14"), "mail_deadline"),
    (_mail("Expense report due tomorrow"), "mail_deadline"),
    (_mail("RSVP by Nov 20 for the offsite"), "mail_deadline"),
    (_mail("Sale ends Friday!"), None),
    (_mail("Last chance: 50% off, order by Sunday"), None),
    (_mail("Lunch Friday?"), None),
    (_mail("Weekly newsletter"), None),
    (_mail("Please review the attached doc"), None),
    (_mail("Re: review"), None),
    (_mail(""), None),
    (_mail("Please submit by Friday", mailbox="Sent Messages"), None),
])
def test_next_step_classify(event: ContextEvent, kind) -> None:
    assert NextStepSuggester.classify(event) == kind


def _run_next_step(events: list[ContextEvent], reply: str = PROPOSAL):
    published: list[ContextEvent] = []
    prompts: list[str] = []

    async def evaluate(text: str) -> str:
        prompts.append(text)
        return reply

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    async def scenario():
        s = NextStepSuggester(event_bus=EventBus(), evaluate=evaluate, publish=publish)
        tasks = [t for t in (s.observe(e) for e in events) if t]
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    return published, prompts


def test_next_step_publishes_tagged_suggestion() -> None:
    published, prompts = _run_next_step([_file("Zoom.dmg")])
    assert len(published) == 1
    meta = published[0].metadata
    assert published[0].event_type == "pattern_detected"
    assert meta["tool_name"] == "install_dmg"
    assert meta["source"] == "next_step"
    assert meta["trigger"] == "installer_downloaded"
    assert "Zoom.dmg" in prompts[0]
    assert "Follow-up to automate: Install the app" in prompts[0]


def test_next_step_cools_down_per_kind() -> None:
    published, prompts = _run_next_step(
        [_file("a.zip"), _file("b.zip"), _file("c.zip"), _file("Zoom.dmg")]
    )
    assert len(prompts) == 2  # one zip + one dmg
    assert len(published) == 2


def test_next_step_respects_no_suggestion() -> None:
    published, prompts = _run_next_step([_file("Zoom.dmg")], reply="NO_SUGGESTION")
    assert prompts and published == []


def test_parse_suggestion_handles_noise() -> None:
    assert parse_suggestion("NO_SUGGESTION") is None
    assert parse_suggestion("") is None
    assert parse_suggestion("sure! " + PROPOSAL)["tool_name"] == "install_dmg"


@pytest.mark.parametrize("reply,gap", [
    ("I can't access your calendar.", True),
    ("I don't have a tool for that yet.", True),
    ("I'm unable to check the weather.", True),
    ("None of my tools can read Mail.", True),
    ("Done! I moved the file.", False),
    ("Python is a programming language.", False),
])
def test_chat_gap_is_gap(reply: str, gap: bool) -> None:
    assert ChatGapDetector.is_gap(reply) is gap


def _run_chat(turns: list[tuple[str, str, frozenset]]):
    published: list[ContextEvent] = []
    prompts: list[str] = []

    async def evaluate(text: str) -> str:
        prompts.append(text)
        return PROPOSAL

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    async def scenario():
        d = ChatGapDetector(event_bus=EventBus(), evaluate=evaluate, publish=publish)
        tasks = [t for t in (d.observe_chat(*turn) for turn in turns) if t]
        await asyncio.gather(*tasks)

    asyncio.run(scenario())
    return published, prompts


def test_chat_gap_suggests_once_per_request() -> None:
    turn = ("what's on my calendar today?", "I can't access your calendar.", frozenset())
    same_words = ("today what's on my calendar", "I cannot see it.", frozenset())
    published, prompts = _run_chat([turn, same_words])
    assert len(prompts) == 1
    assert published[0].metadata["source"] == "chat_gap"
    assert "calendar" in prompts[0]


def test_chat_gap_skips_when_tool_generation_already_tried() -> None:
    turn = ("time me 5 minutes", "I can't do that yet.", frozenset({"generate_custom_tool"}))
    published, prompts = _run_chat([turn])
    assert prompts == [] and published == []


def test_chat_gap_ignores_successful_replies() -> None:
    published, prompts = _run_chat([("move it", "Done, moved.", frozenset())])
    assert prompts == [] and published == []


def test_pattern_prompt_intro_depends_on_source() -> None:
    from ai.orchestrator import Orchestrator

    base = {"tool_name": "install_dmg", "description": "d", "expected_logic": "l"}
    for source, phrase in [
        ("next_step", "obvious follow-up"),
        ("chat_gap", "no existing tool could do"),
        ("repetition", "repeatable workflow"),
    ]:
        prompt = Orchestrator._build_pattern_prompt(
            ContextEvent(event_type="pattern_detected", metadata={**base, "source": source})
        )
        assert phrase in prompt
    legacy = Orchestrator._build_pattern_prompt(
        ContextEvent(event_type="pattern_detected", metadata=base)
    )
    assert "repeatable workflow" in legacy


def test_file_watcher_reports_finished_download_rename() -> None:
    from events.file_watcher import _FileChangeHandler
    from watchdog.events import FileMovedEvent

    pushed: list[ContextEvent] = []

    class FakeBus:
        def push_threadsafe(self, ev: ContextEvent) -> None:
            pushed.append(ev)

    handler = _FileChangeHandler(FakeBus())  # type: ignore[arg-type]
    handler.on_moved(FileMovedEvent("/d/Zoom.dmg.crdownload", "/d/Zoom.dmg"))
    assert pushed[0].metadata == {
        "path": "/d/Zoom.dmg", "action": "renamed", "from": "/d/Zoom.dmg.crdownload",
    }
    assert NextStepSuggester.classify(pushed[0]) == "installer_downloaded"


def test_every_trigger_kind_has_a_follow_up() -> None:
    from ai.idea_sources import BUILTIN_FOLLOW_UPS, FOLLOW_UPS, _FILE_KINDS, _MAIL_KINDS, _MESSAGE_KINDS

    kinds = (
        set(_FILE_KINDS.values()) | {k for k, _ in _MESSAGE_KINDS}
        | {k for k, _, _ in _MAIL_KINDS} | {"meeting_with_video_link"}
    )
    assert kinds <= set(FOLLOW_UPS)
    assert set(BUILTIN_FOLLOW_UPS) <= set(FOLLOW_UPS)
    for kind, (_, tool) in BUILTIN_FOLLOW_UPS.items():
        assert tool in FOLLOW_UPS[kind], kind


@pytest.mark.parametrize("event,kind,tool,detail", [
    (_mail("Invitation: Sync @ Tue Nov 10, 2026 2pm", mid=9), "mail_invite", "create_event", "message_id: 9"),
    (_mail("Your order has shipped"), "mail_shipping", "open_url", "Subject: Your order has shipped"),
    (_mail("Please sign the lease by Friday"), "mail_deadline", "create_reminder", "message_id: 42"),
    (_meeting(), "meeting_with_video_link", "open_url", "https://zoom.us/j/123456"),
    (_msg("dinner tomorrow at 7pm?"), "plan_with_time", "create_event", "dinner tomorrow"),
    (_file("invite.ics"), "calendar_invite_downloaded", "create_event", "/Users/me/Downloads/invite.ics"),
])
def test_builtin_kinds_publish_without_model(event, kind, tool, detail) -> None:
    published, prompts = _run_next_step([event])
    assert prompts == []
    meta = published[0].metadata
    assert published[0].event_type == "pattern_detected"
    assert (meta["trigger"], meta["builtin_tool"], meta["source"]) == (kind, tool, "next_step")
    assert detail in meta["event_details"]
    assert meta["tool_name"] and meta["description"] and meta["expected_logic"]


def test_builtin_kinds_cool_down_per_event_not_per_kind() -> None:
    published, prompts = _run_next_step([
        _mail("Invitation: A @ Tue 2pm", mid=1),
        _mail("Invitation: A @ Tue 2pm", mid=1),  # same email again
        _mail("Invitation: B @ Wed 3pm", mid=2),
    ])
    assert prompts == []
    assert [p.metadata["event_details"].rsplit(" ", 1)[1] for p in published] == ["1", "2"]


def test_tool_kinds_still_call_the_model() -> None:
    published, prompts = _run_next_step([_file("Zoom.dmg")])
    assert len(prompts) == 1 and "builtin_tool" not in published[0].metadata
