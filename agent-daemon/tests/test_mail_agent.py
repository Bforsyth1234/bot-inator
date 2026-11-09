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
