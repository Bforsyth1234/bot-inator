"""No app-switcher ideas from the friction and pattern sources (todo #66).

The open-help half of #66 is tested in ``test_open_help.py``. The same
leak existed in the two other sources that see ``app_activated`` events:
friction's ping-pong trigger asked for a tool for "Chrome <-> Claude" and
accepted any idea that named one of the apps, and the pattern recognizer
published whatever the model proposed for a window of app switches.
"""

from __future__ import annotations

import asyncio
import json

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.friction import FRICTION_JUDGE_SYSTEM_PROMPT, FRICTION_SYSTEM_PROMPT, FrictionDetector
from ai.open_help import IdeaFeedback
from ai.pattern_recognizer import DETECTION_SYSTEM_PROMPT, PatternRecognizer
from events.event_bus import ContextEvent, EventBus

TOGGLE = {
    "tool_name": "toggle_chrome_claude",
    "description": "Switches focus between Google Chrome and Claude with a single hotkey.",
    "expected_logic": (
        "Input: none. Read the frontmost app with osascript; if it is Google "
        "Chrome activate Claude, otherwise activate Google Chrome."
    ),
}
MOVER = {
    "tool_name": "chrome_page_to_claude",
    "description": "Send the text of the open Google Chrome page to Claude as a new chat.",
    "expected_logic": (
        "Input: dry_run (bool, default True). Read the front Chrome tab's text "
        "with osascript and paste it into a new Claude chat; dry_run returns the text only."
    ),
}


def _act(app: str) -> ContextEvent:
    return ContextEvent(event_type="app_activated", app_name=app)


def _friction(idea: dict, events: list[ContextEvent]):
    judged: list[str] = []
    published: list[ContextEvent] = []
    now = [1000.0]

    async def evaluate(_: str) -> str:
        return json.dumps(idea)

    async def judge(prompt: str) -> str:
        judged.append(prompt)
        return json.dumps({"score": 10, "reason": "fake"})

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    async def scenario() -> FrictionDetector:
        det = FrictionDetector(
            event_bus=EventBus(), evaluate=evaluate, judge=judge,
            feedback=IdeaFeedback(), publish=publish, clock=lambda: now[0],
        )
        for ev in events:
            now[0] += 5.0
            task = det.observe(ev)
            if task:
                await task
        return det

    return asyncio.run(scenario()), judged, published


PING_PONG = [_act("Google Chrome"), _act("Claude")] * 6


def test_friction_ping_pong_drops_app_toggle_before_judge() -> None:
    det, judged, published = _friction(TOGGLE, PING_PONG)
    assert det.last_verdict["tool_name"] == "toggle_chrome_claude"  # the model was asked
    assert det.last_verdict["stage"] == "app_switcher"
    assert judged == [] and published == []


def test_friction_ping_pong_still_publishes_a_data_moving_idea() -> None:
    det, judged, published = _friction(MOVER, PING_PONG)
    assert len(judged) == 1 and len(published) == 1
    assert published[0].metadata["tool_name"] == "chrome_page_to_claude"
    assert det.last_verdict["stage"] == "passed"


def test_friction_prompts_rule_out_app_switchers() -> None:
    assert "only switches, toggles or focuses the two apps" in FRICTION_SYSTEM_PROMPT
    assert "always scores 0-3" in FRICTION_JUDGE_SYSTEM_PROMPT


def _recognize(reply: dict):
    published: list[ContextEvent] = []
    calls: list[str] = []

    async def evaluate(prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(reply)

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    async def scenario() -> PatternRecognizer:
        rec = PatternRecognizer(
            event_bus=EventBus(), evaluate=evaluate, publish=publish,
            trigger_every=6, trigger_interval=60.0, cooldown_seconds=0.0,
        )
        for ev in [_act("Google Chrome"), _act("Claude")] * 3:
            task = rec.observe(ev)
            if task:
                await task
        return rec

    return asyncio.run(scenario()), calls, published


def test_pattern_recognizer_drops_app_switcher_and_backs_off() -> None:
    rec, calls, published = _recognize(TOGGLE)
    assert len(calls) == 1 and published == []
    # Treated like NO_PATTERN: the next call waits longer.
    assert rec.current_interval == 120.0
    # Recognised only through the app names in the window.
    rec, _, published = _recognize(dict(TOGGLE, tool_name="chrome_claude_cycle", description="x"))
    assert published == []


def test_pattern_recognizer_still_publishes_other_ideas() -> None:
    rec, calls, published = _recognize(MOVER)
    assert len(calls) == 1 and len(published) == 1
    assert rec.current_interval == 60.0
    assert "never propose a tool that switches" in DETECTION_SYSTEM_PROMPT
