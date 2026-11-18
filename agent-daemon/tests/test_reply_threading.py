"""#113: a reply draft opens as a reply in the thread, not a new email.

Live finding (wave 20): the agent read the email (message_id 449983) and
then called ``draft_email(to=<sender>, subject="Re: ...",
reply_to_message_id=0)``, which opened a NEW message. In a turn about one
email the orchestrator now fills in that email's id before the card. Mail /
AppleScript is always mocked: ``osascript`` never runs here.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import needs_reply, privacy
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from config import settings
from events.event_bus import ContextEvent, EventBus
from tests.test_notices_privacy import _LoopThread, _drain
from tools import email_draft

MID = 449983
SENDER = "Nishat Khan <nishat.khan@recruit.example>"
SENDER_ADDR = "nishat.khan@recruit.example"
SUBJECT = "26-02884::Agentic AI Engineer / Architect::Remote"


@pytest.fixture(autouse=True)
def local_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "provider", "local", raising=False)
    for role in ("main", "analysis", "drafting"):
        monkeypatch.setattr(settings, f"provider_{role}", "", raising=False)
    monkeypatch.delenv(privacy.OPT_IN_ENV, raising=False)


def _osascript(stdout: str = "OK") -> mock.MagicMock:
    return mock.MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")
    )


class _Turn:
    """A real Orchestrator + approval gate on a loop thread; cards recorded."""

    def __init__(self, loop: Any, *, event_type: str, meta: dict[str, Any] | None = None,
                 idea: dict[str, Any] | None = None) -> None:
        bus = EventBus()
        bus.bind_loop(loop)
        self.loop = loop
        self.orch = Orchestrator(engine=MLXEngine(), event_bus=bus, tools=[])
        self.orch._current_event_type = event_type
        self.orch._current_event_meta = dict(meta or {})
        self.orch._current_idea = idea
        self.orch._current_event_id = "evt_113"
        self.orch._turn_kind = "event"
        self.cards: list[dict[str, Any]] = []
        self.approve = True

        async def request_approval(**kwargs: Any) -> tuple[bool, None]:
            self.cards.append(kwargs)
            return self.approve, None

        self.orch._request_approval = request_approval  # type: ignore[method-assign]

    def call(self, tool: Any, **kwargs: Any) -> Any:
        wrapped = self.orch._wrap_tool_for_approval(tool)
        return asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(wrapped.forward, **kwargs), self.loop
        ).result(5)


def _fake_read_tool(name: str, result: dict[str, Any]) -> Any:
    return SimpleNamespace(name=name, inputs={}, forward=lambda **_: result)


def _mail_meta() -> dict[str, Any]:
    return {"sender": SENDER, "subject": SUBJECT, "mailbox": "INBOX", "message_id": MID}


# ── the fill-in before the card ─────────────────────────────────────────

def test_logged_call_in_a_mail_turn_becomes_a_reply_in_the_thread() -> None:
    run = _osascript()
    with _LoopThread() as loop, mock.patch.object(email_draft.subprocess, "run", run):
        turn = _Turn(loop, event_type="mail_received", meta=_mail_meta())
        result = turn.call(email_draft.draft_email, to=SENDER_ADDR, subject="Re: " + SUBJECT,
                           body="Thanks, I'm interested.", reply_to_message_id=0)
        _drain(loop)
    assert len(turn.cards) == 1
    card = turn.cards[0]
    assert card["tool_name"] == "draft_email"
    assert card["tool_args"]["kwargs"]["reply_to_message_id"] == MID
    # The recipient is unchanged.
    assert card["tool_args"]["kwargs"]["to"] == SENDER_ADDR
    assert "Reply in the thread with Nishat Khan" in card["reasoning"]
    # The card says what happens if Mail can't find the email (DECISION 4).
    assert "new message to " + SENDER_ADDR in card["reasoning"]
    # After approval Mail's reply runs, with Mail's own subject.
    cmd = run.call_args.args[0]
    assert cmd[2] == email_draft._REPLY_SCRIPT
    assert cmd[3:] == ["--", str(MID), SENDER_ADDR, "", "", "Thanks, I'm interested."]
    assert result["status"] == "ok" and result["reply_to_message_id"] == MID


def test_read_email_result_makes_a_chat_turn_about_one_email() -> None:
    run = _osascript()
    read = _fake_read_tool("read_email", {
        "status": "ok", "message_id": MID, "sender": SENDER, "subject": SUBJECT,
        "body": "Are you open to a call?",
    })
    with _LoopThread() as loop, mock.patch.object(email_draft.subprocess, "run", run):
        turn = _Turn(loop, event_type="user_message", meta={"text": "reply to Nishat"})
        turn.call(read, message_id=MID)
        turn.call(email_draft.draft_email, to=SENDER, subject="Re: " + SUBJECT, body="Yes.")
        _drain(loop)
    assert [c["tool_name"] for c in turn.cards] == ["draft_email"]
    assert turn.cards[0]["tool_args"]["kwargs"]["reply_to_message_id"] == MID


def test_no_recipient_but_re_subject_fills_in() -> None:
    with _LoopThread() as loop, mock.patch.object(email_draft.subprocess, "run", _osascript()):
        turn = _Turn(loop, event_type="mail_received", meta=_mail_meta())
        turn.call(email_draft.draft_email, to="", subject="RE: re: " + SUBJECT, body="Yes.")
        _drain(loop)
    assert turn.cards[0]["tool_args"]["kwargs"]["reply_to_message_id"] == MID


def test_draft_to_someone_else_stays_a_new_message() -> None:
    run = _osascript()
    with _LoopThread() as loop, mock.patch.object(email_draft.subprocess, "run", run):
        turn = _Turn(loop, event_type="mail_received", meta=_mail_meta())
        turn.call(email_draft.draft_email, to="boss@work.example",
                  subject="Re: " + SUBJECT, body="FYI", reply_to_message_id=0)
        _drain(loop)
    card = turn.cards[0]
    assert card["tool_args"]["kwargs"]["reply_to_message_id"] == 0
    assert "Reply in the thread" not in card["reasoning"]
    cmd = run.call_args.args[0]
    assert cmd[2] == email_draft._NEW_SCRIPT
    assert cmd[3] == "--" and cmd[4] == "boss@work.example"


def test_no_recipient_and_other_subject_is_left_alone() -> None:
    with _LoopThread() as loop, mock.patch.object(email_draft.subprocess, "run", _osascript()):
        turn = _Turn(loop, event_type="mail_received", meta=_mail_meta())
        turn.call(email_draft.draft_email, to="", subject="Re: something else", body="x")
        _drain(loop)
    assert turn.cards[0]["tool_args"]["kwargs"].get("reply_to_message_id", 0) == 0


def test_several_listed_emails_are_not_one_email() -> None:
    listed = _fake_read_tool("list_recent_emails", {"status": "ok", "count": 2, "emails": [
        {"message_id": MID, "sender": SENDER, "subject": SUBJECT},
        {"message_id": 5, "sender": SENDER, "subject": "Another role"},
    ]})
    with _LoopThread() as loop, mock.patch.object(email_draft.subprocess, "run", _osascript()):
        turn = _Turn(loop, event_type="user_message", meta={"text": "reply to Nishat"})
        turn.call(listed)
        turn.call(email_draft.draft_email, to=SENDER_ADDR, subject="Re: x", body="Yes.")
        _drain(loop)
    assert turn.cards[0]["tool_args"]["kwargs"].get("reply_to_message_id", 0) == 0


def test_an_id_the_model_set_is_kept() -> None:
    with _LoopThread() as loop, mock.patch.object(email_draft.subprocess, "run", _osascript()):
        turn = _Turn(loop, event_type="mail_received", meta=_mail_meta())
        turn.call(email_draft.draft_email, to=SENDER_ADDR, subject="", body="Yes.",
                  reply_to_message_id=1234)
        _drain(loop)
    assert turn.cards[0]["tool_args"]["kwargs"]["reply_to_message_id"] == 1234
    assert "Reply in the thread with" not in turn.cards[0]["reasoning"]


def test_a_denied_card_drafts_nothing() -> None:
    run = _osascript()
    with _LoopThread() as loop, mock.patch.object(email_draft.subprocess, "run", run):
        turn = _Turn(loop, event_type="mail_received", meta=_mail_meta())
        turn.approve = False
        result = turn.call(email_draft.draft_email, to=SENDER_ADDR,
                           subject="Re: " + SUBJECT, body="x")
        _drain(loop)
    assert result == {"skipped": True, "reason": "user_denied"}
    run.assert_not_called()


# ── subject handling and the failed lookup (tools/email_draft.py) ──────

def test_typed_subject_on_a_reply_is_ignored_never_re_re() -> None:
    run = _osascript()
    with mock.patch.object(email_draft.subprocess, "run", run):
        result = email_draft.draft_email(to=SENDER_ADDR, subject="Re: " + SUBJECT,
                                         body="b", reply_to_message_id=MID)
    argv = run.call_args.args[0]
    assert argv[3:] == ["--", str(MID), SENDER_ADDR, "", "", "b"]
    assert not any("Re: Re:" in part for part in argv)
    assert result["subject"] == "" and result["ignored_subject"] == "Re: " + SUBJECT


def test_new_message_keeps_its_subject() -> None:
    run = _osascript()
    with mock.patch.object(email_draft.subprocess, "run", run):
        email_draft.draft_email(to=SENDER_ADDR, subject="Hello", body="b")
    assert run.call_args.args[0][3:] == ["--", SENDER_ADDR, "", "Hello", "b"]


def test_failed_lookup_falls_back_to_a_new_message_and_says_so() -> None:
    run = mock.MagicMock(side_effect=[
        subprocess.CompletedProcess([], 0, stdout="NOT_FOUND", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="OK", stderr=""),
    ])
    with mock.patch.object(email_draft.subprocess, "run", run):
        result = email_draft.draft_email(to=SENDER_ADDR, subject="Re: " + SUBJECT,
                                         body="b", reply_to_message_id=MID)
    first, second = (c.args[0] for c in run.call_args_list)
    assert first[2] == email_draft._REPLY_SCRIPT
    assert second[2] == email_draft._NEW_SCRIPT
    assert second[3:] == ["--", SENDER_ADDR, "", "Re: " + SUBJECT, "b"]
    assert result["status"] == "ok" and result["fallback"] == "new_message"
    assert result["reply_not_found"] == MID and str(MID) in result["note"]
    assert "NEW message" in result["note"] and result["delivered"] is False


def test_failed_lookup_without_a_recipient_drafts_nothing() -> None:
    run = _osascript("NOT_FOUND")
    with mock.patch.object(email_draft.subprocess, "run", run):
        result = email_draft.draft_email(to="", subject="", body="b", reply_to_message_id=MID)
    run.assert_called_once()
    assert result["status"] == "error" and str(MID) in result["error"]


def test_card_note_states_the_fallback() -> None:
    note = email_draft.card_note(to=SENDER_ADDR, subject="", body="b", reply_to_message_id=MID)
    assert "thread" in note and "new message to " + SENDER_ADDR in note
    assert "nothing is drafted" in email_draft.card_note(to="", reply_to_message_id=MID)
    assert "new" in email_draft.card_note(to=SENDER_ADDR).lower()
    assert email_draft.draft_email.card_note is email_draft.card_note


# ── prompts (DECISION 2) ────────────────────────────────────────────────

def test_mail_prompt_says_to_pass_the_id_and_not_type_re() -> None:
    event = ContextEvent(event_type="mail_received", metadata=_mail_meta())
    prompt = Orchestrator._build_mail_prompt(event)
    assert f"reply_to_message_id={MID}" in prompt
    assert "don't type Re: yourself" in prompt


def test_reply_follow_up_prompt_says_the_same() -> None:
    from ai.idea_sources import NextStepSuggester

    event = ContextEvent(event_type="mail_received", metadata=_mail_meta())
    idea = NextStepSuggester.reply_idea(event)
    prompt = Orchestrator._build_pattern_prompt(
        ContextEvent(event_type="pattern_detected", metadata=idea)
    )
    assert needs_reply.reply_instruction(MID) in prompt
    assert "don't type Re: yourself" in prompt
