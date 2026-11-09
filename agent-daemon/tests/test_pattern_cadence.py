"""Adaptive detection cadence (todo #25).

Each model call that returns NO_PATTERN doubles the time-based interval
(cap 10 min); a match resets it; repeat-gate skips don't count. The clock
is injected so nothing sleeps.
"""

from __future__ import annotations

import asyncio
import logging

from ai.pattern_recognizer import PatternRecognizer
from events.event_bus import ContextEvent, EventBus

MATCH = (
    '{"tool_name": "file_invoices", "description": "File invoices.", '
    '"expected_logic": "Move PDFs from ~/Downloads to ~/Invoices."}'
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Harness:
    """Recognizer + fake evaluator whose reply the test controls."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.reply = "NO_PATTERN"
        self.calls = 0
        self.published: list[ContextEvent] = []

        async def evaluate(_prompt: str) -> str:
            self.calls += 1
            return self.reply

        async def publish(event: ContextEvent) -> None:
            self.published.append(event)

        self.rec = PatternRecognizer(
            event_bus=EventBus(), evaluate=evaluate, publish=publish,
            clock=self.clock, cooldown_seconds=0.0,
        )

    async def feed(self, event: ContextEvent | None = None) -> bool:
        """Observe one event; await detection if it fired. True if it did."""
        task = self.rec.observe(event or ContextEvent(event_type="file", app_name="Finder"))
        if task is None:
            return False
        await task
        return True

    async def fire_by_count(self) -> None:
        """At the base interval, ten repeating events trigger a call."""
        for _ in range(self.rec.trigger_every):
            await self.feed()

    async def fire_by_time(self) -> bool:
        """Advance the clock by the current interval and observe one event."""
        self.clock.now += self.rec.current_interval
        return await self.feed()


def test_interval_doubles_per_no_pattern_and_caps_at_ten_minutes() -> None:
    async def scenario() -> list[float]:
        h = Harness()
        assert h.rec.current_interval == 60.0
        await h.fire_by_count()
        seen = [h.rec.current_interval]
        for _ in range(6):
            assert await h.fire_by_time()
            seen.append(h.rec.current_interval)
        assert h.calls == 7
        return seen

    assert asyncio.run(scenario()) == [120.0, 240.0, 480.0, 600.0, 600.0, 600.0, 600.0]


def test_match_resets_interval_to_base() -> None:
    async def scenario() -> None:
        h = Harness()
        await h.fire_by_count()
        assert await h.fire_by_time()
        assert h.rec.current_interval == 240.0 and h.rec.backed_off
        h.reply = MATCH
        assert await h.fire_by_time()
        assert len(h.published) == 1
        assert h.rec.current_interval == 60.0 and not h.rec.backed_off

    asyncio.run(scenario())


def test_repeat_gate_skips_do_not_back_off() -> None:
    async def scenario() -> None:
        h = Harness()
        apps = ["Safari", "Slack", "Xcode", "Spotify", "Notes",
                "Terminal", "Calendar", "Mail", "Messages", "Figma"]
        for app in apps:
            assert not await h.feed(ContextEvent(event_type="app_activated", app_name=app))
        # Time trigger reached, still no repetition: another skip.
        h.clock.now += 3600
        assert not await h.feed(ContextEvent(event_type="app_activated", app_name="Maps"))
        assert h.calls == 0
        assert h.rec.current_interval == 60.0

    asyncio.run(scenario())


def test_backoff_holds_count_trigger_until_interval_elapses() -> None:
    """Busy user, nothing found: the count trigger must not bypass backoff.

    Non-vacuity: with the count trigger left as it was (``enough_events``
    not gated on ``backed_off``), the call fires on the 10th event, ~10 s
    after the NO_PATTERN, and this test fails.
    """
    async def scenario() -> None:
        h = Harness()
        await h.fire_by_count()
        assert h.calls == 1 and h.rec.current_interval == 120.0
        start = h.clock.now
        fired_at = None
        for _ in range(200):  # one event per second
            h.clock.now += 1
            if await h.feed():
                fired_at = h.clock.now - start
                break
        assert h.calls == 2
        assert fired_at == 120.0

    asyncio.run(scenario())


def test_interval_change_logged_once_per_change(caplog) -> None:
    async def scenario() -> None:
        h = Harness()
        await h.fire_by_count()          # 60 -> 120
        for _ in range(4):               # 240, 480, 600, 600 (no change)
            await h.fire_by_time()
        h.reply = MATCH
        await h.fire_by_time()           # 600 -> 60

    with caplog.at_level(logging.INFO, logger="ai.pattern_recognizer"):
        asyncio.run(scenario())
    lines = [r.getMessage() for r in caplog.records if "interval" in r.getMessage()]
    assert lines == [
        "pattern detection interval 60s -> 120s (no pattern)",
        "pattern detection interval 120s -> 240s (no pattern)",
        "pattern detection interval 240s -> 480s (no pattern)",
        "pattern detection interval 480s -> 600s (no pattern)",
        "pattern detection interval 600s -> 60s (pattern found)",
    ]
