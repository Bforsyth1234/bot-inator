"""Wave 10 close: registrations the runner added (#63, #65, #67)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ai.orchestrator import Orchestrator  # before ``tools``: see note

import tools  # noqa: E402  (``import tools`` first hits an ai<->tools import cycle)
from events.event_bus import ContextEvent


def _names() -> set[str]:
    return {getattr(t, "name", getattr(t, "__name__", "")) for t in tools.AVAILABLE_TOOLS}


def test_draft_email_registered_and_gated() -> None:
    assert "draft_email" in _names()
    assert "draft_email" not in tools.READ_ONLY_TOOL_NAMES


def test_brief_me_registered_read_only() -> None:
    assert "brief_me" in _names()
    assert "brief_me" in tools.READ_ONLY_TOOL_NAMES


def test_daily_brief_route_is_mounted() -> None:
    """Included routers are one entry in ``app.routes`` (see the wave 8
    wiring test), so call the route through the real app and token."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    with patch("ai.orchestrator.Orchestrator._build_agent", return_value=None):
        import main
        from auth import auth_header

        with TestClient(main.app, headers=auth_header()) as client:
            body = client.get("/api/daily-brief")
            assert body.status_code == 200
            assert {"enabled", "time"} <= set(body.json())
        with TestClient(main.app) as anon:
            assert anon.get("/api/daily-brief").status_code == 401


def _event(text: str = "Calendar: 1 event\n09:00 Standup") -> ContextEvent:
    return ContextEvent(
        event_type="daily_brief",
        metadata={"text": text, "day": "Tuesday November 10",
                  "calendar": {"status": "ok", "items": [{}]},
                  "mail": {"status": "unavailable", "reason": "x"},
                  "reminders": {"status": "ok", "items": []}},
    )


def _fake_self(reply: str):
    thoughts: list[tuple[str, str]] = []
    notes: list[str] = []
    calls: list[int] = []

    async def emit(event_id, stage, content):
        thoughts.append((stage, content))

    async def run_agent(event_id, prompt, max_steps=2):
        calls.append(max_steps)
        return reply

    fake = SimpleNamespace(_emit_thought=emit, _run_agent=run_agent, _notify_brief=notes.append)
    return fake, thoughts, notes, calls


def test_daily_brief_turn_shows_brief_and_notifies() -> None:
    fake, thoughts, notes, calls = _fake_self("Standup at 9.")
    asyncio.run(Orchestrator._handle_daily_brief(fake, _event(), "evt_1"))
    assert ("complete", "Standup at 9.") in thoughts
    assert notes == ["Standup at 9."]
    assert calls == [2]


def test_daily_brief_falls_back_to_gathered_text() -> None:
    fake, thoughts, notes, _ = _fake_self("")
    asyncio.run(Orchestrator._handle_daily_brief(fake, _event("Calendar: nothing today"), "evt_2"))
    assert ("complete", "Calendar: nothing today") in thoughts
    assert notes == ["Calendar: nothing today"]


def test_describe_event_daily_brief() -> None:
    line = Orchestrator._describe_event(_event())
    assert "Morning brief" in line and "emails unavailable" in line


def test_chat_prompt_mentions_remember_preference_and_drafts() -> None:
    import inspect

    src = inspect.getsource(Orchestrator._build_chat_prompt)
    assert "remember_preference" in src
    assert "draft_email" in src and "cannot send mail" in src
