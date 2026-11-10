"""#51: idea text a local engine wrote must not quote personal text into a
cloud main engine's pattern prompt."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import privacy
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from config import settings
from events.event_bus import ContextEvent, EventBus

PH = privacy.PLACEHOLDER
MESSAGE = "Hey! Are we still on for dinner at 7 with Sam? Bring the wine"
QUOTE = "dinner at 7 with Sam"


@pytest.fixture()
def providers(monkeypatch):
    monkeypatch.delenv(privacy.OPT_IN_ENV, raising=False)
    monkeypatch.setattr(settings, "provider", "local")
    monkeypatch.setattr(settings, "pattern_detection_engine", "analysis")
    privacy.clear_recent_personal()

    def set_(**roles: str) -> None:
        for role in ("main", "analysis", "drafting"):
            monkeypatch.setattr(settings, f"provider_{role}", roles.get(role, ""))

    set_()
    yield set_
    privacy.clear_recent_personal()


def _idea() -> ContextEvent:
    # What a local detection engine might write after seeing the iMessage.
    return ContextEvent(event_type="pattern_detected", metadata={
        "tool_name": "dinner_reminder",
        "description": f"Remind the user about {QUOTE} before it starts.",
        "expected_logic": (
            f"When a message like '{QUOTE}' arrives, create a reminder 1 hour "
            "before. Files go to ~/Documents/Plans."
        ),
        "source": "repetition",
    })


def _run_events(orch: Orchestrator, events: list[ContextEvent]) -> list[str]:
    prompts: list[str] = []

    async def run_agent(event_id: str, prompt: str, max_steps: Any = None) -> str:
        prompts.append(prompt)
        return "no action"

    async def emit(*_a: Any, **_k: Any) -> None:
        return None

    async def analyse(_event: ContextEvent) -> str:
        return ""

    orch._run_agent = run_agent  # type: ignore[method-assign]
    orch._emit_thought = emit  # type: ignore[method-assign]
    orch._analyse_event = analyse  # type: ignore[method-assign]

    async def scenario() -> None:
        for event in events:
            await orch._handle_event(event)

    asyncio.run(scenario())
    return prompts


def _imessage() -> ContextEvent:
    return ContextEvent(event_type="imessage_received",
                        metadata={"sender": "+15551234567", "text": MESSAGE})


def test_cloud_main_prompt_gets_placeholder_for_quoted_imessage(providers) -> None:
    providers(main="groq")  # main cloud, detection (analysis) local
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    prompts = _run_events(orch, [_imessage(), _idea()])
    pattern_prompt = prompts[-1]
    assert QUOTE not in pattern_prompt
    assert "dinner at 7" not in pattern_prompt
    assert PH in pattern_prompt
    # The rest of the idea survives: the name and a path that is not personal.
    assert "dinner_reminder" in pattern_prompt
    assert "~/Documents/Plans" in pattern_prompt


def test_local_main_and_opt_in_keep_the_idea_text(providers, monkeypatch) -> None:
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    prompts = _run_events(orch, [_imessage(), _idea()])
    assert QUOTE in prompts[-1]          # main is local: nothing to scrub
    providers(main="groq")
    monkeypatch.setenv(privacy.OPT_IN_ENV, "1")
    assert QUOTE in Orchestrator._build_pattern_prompt(_idea())


def test_scrub_is_deterministic_and_word_bounded(providers) -> None:
    personal = [MESSAGE, "Invoice 4471 overdue from Acme Corp"]
    text = (
        "Track the Invoice 4471 overdue reminders and move *.pdf files; "
        'a "short" quote and "see you at the station" stay or go by length. '
        "Sam likes dinner."  # two personal words in a row: kept
    )
    out = privacy.scrub_personal_text(text, personal)
    assert out == privacy.scrub_personal_text(text, personal)
    assert "Invoice 4471 overdue" not in out and PH in out
    assert "*.pdf" in out and '"short"' in out
    assert "see you at the station" not in out   # quoted, 5 words
    assert "Sam likes dinner." in out


def test_mail_subject_quote_is_scrubbed(providers) -> None:
    providers(main="groq")
    subject = "Your biopsy results from Dr. Patel are ready"
    mail = ContextEvent(event_type="mail_received", metadata={
        "sender": "lab@example.com", "subject": subject, "message_id": 3,
    })
    idea = ContextEvent(event_type="pattern_detected", metadata={
        "tool_name": "lab_results", "description": "Open lab mail",
        "expected_logic": "Find mails like: biopsy results from Dr. Patel",
    })
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    prompt = _run_events(orch, [mail, idea])[-1]
    assert "biopsy results from Dr" not in prompt and PH in prompt


def test_non_vacuous_without_the_scrub(providers, monkeypatch) -> None:
    """The first test fails if the pattern prompt skips the scrub."""
    providers(main="groq")
    monkeypatch.setattr(privacy, "scrub_idea_text", lambda text, role="main": text)
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    assert QUOTE in _run_events(orch, [_imessage(), _idea()])[-1]


def test_single_quoted_quote_does_not_leak_its_last_word(providers) -> None:
    """#68: ``'dinner at 7 with Sam'`` used to become ``'[placeholder] Sam'``."""
    for q in ("'", "’"):
        out = privacy.scrub_personal_text(f"Remind about {q}{QUOTE}{q} tonight", [MESSAGE])
        assert "Sam" not in out and "dinner" not in out, out
        assert PH in out
    # A possessive inside personal text still matches.
    out = privacy.scrub_personal_text("note: Sam's dinner at 7 is on", ["Sam's dinner at 7"])
    assert "dinner" not in out and "Sam" not in out, out
