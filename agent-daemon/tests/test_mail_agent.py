"""New email reaches the tool-calling agent, which reads it and may offer a
reminder or calendar event. Junk/sent mail and mail a built-in follow-up
already covers are skipped so the user never gets two cards for one email."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import privacy
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator, _mail_skips_agent
from config import settings
from events.event_bus import ContextEvent, EventBus


def _mail(subject: str, mailbox: str = "INBOX") -> ContextEvent:
    return ContextEvent(event_type="mail_received", metadata={
        "sender": "Me <me@example.com>", "subject": subject,
        "date": "2026-11-09T15:30:00+00:00", "mailbox": mailbox, "message_id": 449710,
    })


def _orch() -> Orchestrator:
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    orch._run_agent = AsyncMock(return_value="no action")  # type: ignore[method-assign]
    orch._analyse_event = AsyncMock(return_value="")  # type: ignore[method-assign]
    orch._recall_memories = AsyncMock(return_value=[])  # type: ignore[method-assign]
    return orch


@pytest.fixture
def local_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "provider", "local", raising=False)
    for role in ("main", "analysis", "drafting"):
        monkeypatch.setattr(settings, f"provider_{role}", "", raising=False)
    monkeypatch.delenv(privacy.OPT_IN_ENV, raising=False)


def test_plain_reminder_email_runs_the_agent(local_main) -> None:
    orch = _orch()
    asyncio.run(orch._handle_event(_mail("Reminder take the dog on a walk")))
    orch._run_agent.assert_awaited_once()
    prompt = orch._run_agent.call_args.args[1]
    assert "Reminder take the dog on a walk" in prompt
    assert "read_email" in prompt and "message_id=449710" in prompt
    assert "create_reminder" in prompt
    assert orch._run_agent.call_args.kwargs["max_steps"] == 3


@pytest.mark.parametrize("mailbox", ["Junk", "Sent Messages", "Drafts", "Trash"])
def test_junk_and_sent_mail_skip_the_agent(local_main, mailbox: str) -> None:
    orch = _orch()
    asyncio.run(orch._handle_event(_mail("Reminder take the dog on a walk", mailbox)))
    orch._run_agent.assert_not_awaited()


def test_mail_covered_by_builtin_follow_up_skips_the_agent(local_main) -> None:
    # "by tonight" makes it a mail_deadline: NextStepSuggester offers it.
    event = _mail("Reminder take the dog on a walk by tonight")
    assert _mail_skips_agent(event)
    orch = _orch()
    asyncio.run(orch._handle_event(event))
    orch._run_agent.assert_not_awaited()


def test_mail_prompt_redacts_subject_for_cloud_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(privacy, "should_redact", lambda role: role == "main")
    monkeypatch.setattr(
        privacy, "redact_event",
        lambda event, role: privacy.redacted_event(event),
    )
    prompt = Orchestrator._build_mail_prompt(_mail("Pick up the biopsy results"))
    assert "biopsy" not in prompt
    assert f'Subject: "{privacy.PLACEHOLDER}"' in prompt
    assert "message_id=449710" in prompt


# ---- reply drafts (#70) ------------------------------------------------------

def _reply_idea_event() -> ContextEvent:
    from ai.idea_sources import NextStepSuggester

    mail = ContextEvent(event_type="mail_received", metadata={
        "sender": "Sam <sam@other.org>", "subject": "Can you send me the report?",
        "date": "2026-11-10T09:00:00", "mailbox": "INBOX", "message_id": 42,
    })
    return ContextEvent(event_type="pattern_detected",
                        metadata=NextStepSuggester.reply_idea(mail))


def test_reply_idea_prompt_calls_draft_email_with_the_message_id(local_main) -> None:
    prompt = Orchestrator._build_pattern_prompt(_reply_idea_event())
    assert "`draft_email`" in prompt and "read_email" in prompt
    assert "reply_to_message_id: 42" in prompt
    assert "Can you send me the report?" in prompt
    assert "generate_custom_tool` tool exactly once" not in prompt


def test_reply_idea_prompt_redacts_subject_for_cloud_main(monkeypatch) -> None:
    monkeypatch.setattr(privacy, "should_redact", lambda role: role == "main")
    prompt = Orchestrator._build_pattern_prompt(_reply_idea_event())
    assert "send me the report" not in prompt
    assert "reply_to_message_id: 42" in prompt


def test_read_mail_fields_reads_headers_and_thread(monkeypatch, tmp_path) -> None:
    from ai.idea_sources import read_mail_fields

    raw = (
        "From: Sam <sam@other.org>\r\nTo: Me <me@example.com>\r\nCc: kim@other.org\r\n"
        "Delivered-To: me@example.com\r\nSubject: Re: Report\r\n"
        "Message-ID: <m2@other.org>\r\nIn-Reply-To: <m1@other.org>\r\n"
        "References: <m0@other.org> <m1@other.org>\r\n\r\n"
        "Can you send it?\r\n\r\nOn Mon, Me wrote:\r\n> here\r\n"
    ).encode()
    folder = tmp_path / "V10" / "acct" / "INBOX.mbox" / "Messages"
    folder.mkdir(parents=True)
    (folder / "42.emlx").write_bytes(f"{len(raw)}\n".encode() + raw + b"<?xml?><plist/>")
    monkeypatch.setenv("AGENT_MAIL_ROOT", str(tmp_path))
    fields = read_mail_fields(42)
    assert fields["to"] == "Me <me@example.com>" and fields["cc"] == "kim@other.org"
    assert fields["thread_id"] == "<m0@other.org>"
    assert fields["delivered_to"] == ["me@example.com"]
    assert fields["body"].startswith("Can you send it?")
    assert read_mail_fields(99) is None
