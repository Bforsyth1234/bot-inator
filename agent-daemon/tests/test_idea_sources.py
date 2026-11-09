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
    from ai.idea_sources import FOLLOW_UPS, _FILE_KINDS, _MESSAGE_KINDS

    kinds = set(_FILE_KINDS.values()) | {k for k, _ in _MESSAGE_KINDS}
    assert kinds <= set(FOLLOW_UPS)
