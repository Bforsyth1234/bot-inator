"""Tests for the open-ended help source: generate, filter, learn."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.open_help import (
    GUIDING_QUESTION,
    OPEN_HELP_SYSTEM_PROMPT,
    IdeaFeedback,
    OpenHelpSuggester,
    parse_score,
    passes_checks,
)
from events.event_bus import ContextEvent, EventBus

IDEA = {
    "tool_name": "rename_screenshots_by_date",
    "description": "Rename new screenshots on the Desktop to their capture date.",
    "expected_logic": (
        "Input: folder (str, default ~/Desktop). For each file matching "
        "'Screenshot *.png', rename it to YYYY-MM-DD_HHMMSS.png. "
        "dry_run defaults to True and only lists the renames."
    ),
}


def _file(name: str) -> ContextEvent:
    return ContextEvent(
        event_type="file", metadata={"path": f"/Users/me/Desktop/{name}", "action": "created"}
    )


def _run(
    events: list[ContextEvent],
    *,
    reply: str = json.dumps(IDEA),
    score: str = '{"score": 9, "reason": "matches events"}',
    feedback: IdeaFeedback | None = None,
    **kwargs,
):
    published: list[ContextEvent] = []
    asks: list[str] = []
    judged: list[str] = []

    async def evaluate(text: str) -> str:
        asks.append(text)
        return reply

    async def judge(text: str) -> str:
        judged.append(text)
        return score

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    async def scenario():
        s = OpenHelpSuggester(
            event_bus=EventBus(), evaluate=evaluate, judge=judge,
            feedback=feedback, publish=publish, **kwargs,
        )
        for e in events:
            task = s.observe(e)
            if task:
                await task
        return s

    suggester = asyncio.run(scenario())
    return published, asks, judged, suggester


def test_prompt_asks_the_guiding_question() -> None:
    assert GUIDING_QUESTION in OPEN_HELP_SYSTEM_PROMPT


def test_publishes_judged_idea_after_min_events() -> None:
    events = [_file(f"Screenshot {i}.png") for i in range(3)]
    published, asks, judged, _ = _run(events)
    assert len(asks) == 1 and len(judged) == 1
    assert "Screenshot 2.png" in asks[0]
    meta = published[0].metadata
    assert published[0].event_type == "pattern_detected"
    assert meta["source"] == "open_help"
    assert meta["judge_score"] == 9


def test_waits_for_min_events() -> None:
    published, asks, _, _ = _run([_file("a.png"), _file("b.png")])
    assert asks == [] and published == []


def test_rate_limited_by_interval() -> None:
    events = [_file(f"{i}.png") for i in range(9)]
    _, asks, _, _ = _run(events, interval_seconds=600)
    assert len(asks) == 1
    _, asks, _, _ = _run(events, interval_seconds=0)
    assert len(asks) == 3


def test_ignores_agent_and_chat_events() -> None:
    events = [ContextEvent(event_type="pattern_detected", metadata=IDEA)] * 3
    events += [ContextEvent(event_type="user_message", metadata={"text": "hi"})] * 3
    _, asks, _, _ = _run(events)
    assert asks == []


def test_low_judge_score_is_dropped() -> None:
    events = [_file(f"{i}.png") for i in range(3)]
    published, _, judged, _ = _run(events, score='{"score": 4, "reason": "generic"}')
    assert judged and published == []


def test_unparseable_judge_reply_is_dropped() -> None:
    events = [_file(f"{i}.png") for i in range(3)]
    published, _, _, _ = _run(events, score="sounds great!")
    assert published == []


def test_no_suggestion_skips_judge() -> None:
    events = [_file(f"{i}.png") for i in range(3)]
    published, asks, judged, _ = _run(events, reply="NO_SUGGESTION")
    assert asks and judged == [] and published == []


def test_malformed_idea_skips_judge() -> None:
    bad = dict(IDEA, expected_logic="do it")
    events = [_file(f"{i}.png") for i in range(3)]
    published, _, judged, _ = _run(events, reply=json.dumps(bad))
    assert judged == [] and published == []


def test_same_idea_not_repeated() -> None:
    events = [_file(f"{i}.png") for i in range(6)]
    published, asks, _, _ = _run(events, interval_seconds=0)
    assert len(asks) == 2 and len(published) == 1


def test_rejected_idea_is_suppressed_and_history_reaches_judge() -> None:
    fb = IdeaFeedback()
    fb.record("rename_screenshots_by_date", IDEA["description"], approved=False)
    events = [_file(f"{i}.png") for i in range(3)]
    published, _, judged, _ = _run(events, feedback=fb)
    assert published == [] and judged == []

    fb = IdeaFeedback()
    fb.record("open_zoom_links", "Open Zoom links from iMessage", approved=True)
    published, _, judged, _ = _run(events, feedback=fb)
    assert len(published) == 1
    assert "approved: open_zoom_links" in judged[0]


def test_record_feedback_persists(tmp_path: Path) -> None:
    path = tmp_path / "fb.json"
    _, _, _, s = _run([], feedback=IdeaFeedback(path))
    s.record_feedback("tidy_downloads", "Sort Downloads by type", approved=False)
    reloaded = IdeaFeedback(path)
    assert reloaded.resembles_rejected("tidy_downloads", "")
    assert reloaded.resembles_rejected("sort_downloads", "Sort Downloads by file type")
    assert not reloaded.resembles_rejected("add_ics_invite", "Add calendar invite")


def test_parse_score() -> None:
    assert parse_score('{"score": 8, "reason": "x"}') == 8
    assert parse_score('Sure: {"score": 12}') == 10
    assert parse_score("score: 3") == 3
    assert parse_score("") is None


def test_passes_checks() -> None:
    assert passes_checks(IDEA)
    assert not passes_checks(dict(IDEA, tool_name="Bad Name"))
    assert not passes_checks(dict(IDEA, description=""))


def test_orchestrator_routes_feedback_to_sources() -> None:
    from ai.orchestrator import Orchestrator

    calls: list[tuple] = []

    class Source:
        def record_feedback(self, name, desc, approved):
            calls.append((name, desc, approved))

    orch = Orchestrator.__new__(Orchestrator)
    orch.idea_sources = [Source(), object()]
    orch._record_idea_feedback({"kwargs": {"tool_name": "t", "description": "d"}}, False)
    orch._record_idea_feedback({"args": ["u", "e"], "kwargs": {}}, True)
    orch._record_idea_feedback({"kwargs": {}}, True)
    assert calls == [("t", "d", False), ("u", "e", True)]


def test_pattern_prompt_has_open_help_intro() -> None:
    from ai.orchestrator import Orchestrator

    prompt = Orchestrator._build_pattern_prompt(
        ContextEvent(event_type="pattern_detected", metadata={**IDEA, "source": "open_help"})
    )
    assert "how it could help" in prompt
