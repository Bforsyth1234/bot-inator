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
    # #76: a document in Downloads is a prepared-offer kind; other PDFs aren't.
    (_file("statement.pdf"), "document_downloaded"),
    (_file("paper.pdf"), None),
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


# ---- reply drafts (#70) ------------------------------------------------------

YES = '{"needs_reply": true, "reason": "Sam asks for the report."}'
NO = '{"needs_reply": false, "reason": "FYI only."}'


def _reply_mail(subject: str = "Report", mid: int = 42, sender: str = "Sam <sam@other.org>",
                bulk: bool = False, mailbox: str = "INBOX") -> ContextEvent:
    return ContextEvent(event_type="mail_received", metadata={
        "sender": sender, "subject": subject, "date": "2026-11-10T09:00:00",
        "mailbox": mailbox, "message_id": mid, "bulk": bulk,
    })


def _fields(body: str = "Can you send me the report by Friday?", to: str = "Me <me@example.com>",
            cc: str = "", thread: str = "<t1@other.org>") -> dict:
    return {"to": to, "cc": cc, "body": body, "thread_id": thread, "delivered_to": []}


@pytest.fixture
def local_privacy(monkeypatch: pytest.MonkeyPatch) -> None:
    from ai import privacy

    monkeypatch.setattr(privacy, "should_redact", lambda role: False)


def _run_reply(events, fields: dict, judge_reply: str = YES, feedback=None, state=None):
    from ai.idea_sources import IdeaState

    published: list[ContextEvent] = []
    judged: list[str] = []
    model_prompts: list[str] = []

    async def judge(text: str) -> str:
        judged.append(text)
        return judge_reply

    async def evaluate(text: str) -> str:
        model_prompts.append(text)
        return PROPOSAL

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    async def scenario():
        s = NextStepSuggester(
            event_bus=EventBus(), evaluate=evaluate, publish=publish,
            state=state if state is not None else IdeaState(), feedback=feedback,
            reply_judge=judge, read_message=lambda mid: fields.get(mid),
            user_addresses=["me@example.com"],
        )
        for e in events:  # one at a time: a later mail sees earlier cooldowns
            task = s.observe(e)
            if task:
                await task

    asyncio.run(scenario())
    return published, judged, model_prompts


def test_direct_question_offers_one_reply_draft(local_privacy) -> None:
    published, judged, model = _run_reply([_reply_mail()], {42: _fields()})
    assert len(judged) == 1 and model == []
    assert "Can you send me the report by Friday?" in judged[0]
    assert len(published) == 1
    meta = published[0].metadata
    assert published[0].event_type == "pattern_detected"
    assert (meta["builtin_tool"], meta["trigger"], meta["source"]) == (
        "draft_email", "mail_needs_reply", "next_step")
    assert meta["tool_name"] == "draft_email_reply"
    assert meta["reply_to_message_id"] == 42
    assert "reply_to_message_id: 42" in meta["event_details"]
    assert "draft_email" in meta["description"] and "never say" in meta["description"]


@pytest.mark.parametrize("event,fields", [
    (_reply_mail(bulk=True), _fields()),                                   # newsletter
    (_reply_mail(sender="News <newsletter@shop.com>"), _fields()),         # automated sender
    (_reply_mail(), _fields(to="team@other.org", cc="me@example.com")),    # CC-only
    (_reply_mail(sender="Me <me@example.com>"), _fields()),                # the user's own mail
    (_reply_mail(), _fields(body="Here is the report. Thanks!")),          # no question
    (_reply_mail(mailbox="Junk"), _fields()),
])
def test_no_reply_offer_for_non_questions(local_privacy, event, fields) -> None:
    published, judged, _ = _run_reply([event], {42: fields})
    assert published == [] and judged == []


def test_judge_no_drops_the_offer(local_privacy) -> None:
    published, judged, _ = _run_reply([_reply_mail()], {42: _fields()}, judge_reply=NO)
    assert len(judged) == 1 and published == []


@pytest.mark.parametrize("reply", ["maybe", "", '{"needs_reply": "yes"}',
                                   '{"needs_reply": true} {"needs_reply": false}'])
def test_unclear_judge_reply_is_a_no(local_privacy, reply) -> None:
    published, _, _ = _run_reply([_reply_mail()], {42: _fields()}, judge_reply=reply)
    assert published == []


def test_second_mail_in_thread_inside_cooldown_is_skipped(local_privacy) -> None:
    second = _reply_mail(subject="Re: Report", mid=43, sender="Kim <kim@other.org>")
    published, judged, _ = _run_reply(
        [_reply_mail(), second],
        {42: _fields(), 43: _fields(body="Also, could you add Q3?")},
    )
    assert [p.metadata["reply_to_message_id"] for p in published] == [42]
    assert len(judged) == 1


def test_same_sender_inside_cooldown_is_skipped(local_privacy) -> None:
    other_thread = _reply_mail(subject="Lunch?", mid=43)
    published, judged, _ = _run_reply(
        [_reply_mail(), other_thread],
        {42: _fields(), 43: _fields(body="Lunch tomorrow?", thread="<t2@other.org>")},
    )
    assert [p.metadata["reply_to_message_id"] for p in published] == [42]


def test_other_sender_other_thread_gets_its_own_offer(local_privacy) -> None:
    other = _reply_mail(subject="Lunch?", mid=43, sender="Kim <kim@other.org>")
    published, _, _ = _run_reply(
        [_reply_mail(), other],
        {42: _fields(), 43: _fields(body="Lunch tomorrow?", thread="<t2@other.org>")},
    )
    assert [p.metadata["reply_to_message_id"] for p in published] == [42, 43]


def test_thread_cooldown_survives_restart(local_privacy, tmp_path) -> None:
    from ai.idea_sources import IdeaState

    path = tmp_path / "idea_state.json"
    _run_reply([_reply_mail()], {42: _fields()}, state=IdeaState(path))
    later = _reply_mail(subject="Re: Report", mid=50, sender="Kim <kim@other.org>")
    published, judged, _ = _run_reply([later], {50: _fields()}, state=IdeaState(path))
    assert published == [] and judged == []


def test_declined_reply_idea_is_remembered(local_privacy) -> None:
    from ai.open_help import IdeaFeedback

    feedback = IdeaFeedback()
    published, _, _ = _run_reply([_reply_mail()], {42: _fields()}, feedback=feedback)
    meta = published[0].metadata
    feedback.record(meta["tool_name"], meta["description"], approved=False)
    other = _reply_mail(subject="Lunch?", mid=43, sender="Kim <kim@other.org>")
    published, judged, _ = _run_reply(
        [other], {43: _fields(thread="<t2@other.org>")}, feedback=feedback)
    assert published == [] and judged == []


def test_cloud_engine_with_personal_text_off_skips_and_says_why(monkeypatch) -> None:
    from ai import privacy
    from ai.idea_sources import NEEDS_REPLY_NOTICE_ID
    from schemas.ws_messages import NOTICE_EVENT_TYPE

    monkeypatch.setattr(privacy, "should_redact", lambda role: role == "main")
    published, judged, _ = _run_reply(
        [_reply_mail(), _reply_mail(mid=43, sender="Kim <kim@other.org>")],
        {42: _fields(), 43: _fields(thread="<t2@other.org>")},
    )
    assert judged == []
    assert [p.event_type for p in published] == [NOTICE_EVENT_TYPE]  # once
    notice = published[0].metadata["notice"]
    assert notice["id"] == NEEDS_REPLY_NOTICE_ID and notice["level"] == "info"
    assert "cloud" in notice["body"]


def test_deadline_mail_gets_the_reply_instead_of_the_reminder(local_privacy) -> None:
    event = _reply_mail(subject="Can you send me the report by Friday?")
    assert NextStepSuggester.classify(event) == "mail_deadline"
    published, _, _ = _run_reply([event], {42: _fields()})
    assert [p.metadata["builtin_tool"] for p in published] == ["draft_email"]


def test_deadline_mail_keeps_the_reminder_when_no_reply_is_needed(local_privacy) -> None:
    event = _reply_mail(subject="Please sign the lease by Friday")
    published, _, _ = _run_reply([event], {42: _fields()}, judge_reply=NO)
    assert [p.metadata["builtin_tool"] for p in published] == ["create_reminder"]


def test_reply_rule_is_off_without_a_judge(local_privacy) -> None:
    published, prompts = _run_next_step([_reply_mail()])
    assert published == [] and prompts == []


def test_unreadable_message_drops_the_offer(local_privacy) -> None:
    published, judged, _ = _run_reply([_reply_mail()], {})
    assert published == [] and judged == []
