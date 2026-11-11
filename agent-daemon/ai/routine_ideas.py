"""Routine candidates as tool ideas (#75, source ``routine``).

Once a day the activity history is mined (:func:`ai.routines.find_candidates`,
deterministic, no model call). Each candidate goes through the same idea
pipeline every other source uses: a ``pattern_detected`` event whose
metadata says ``source="routine"``, so the orchestrator's pattern turn, the
approval card, the declined-idea memory and #79's outcome store all apply.

* A ``time_routine`` (same apps launched in the same weekday-hour slot) is a
  built-in idea (``builtin_tool`` = ``open_app`` / ``run_shortcut``) and is
  offered only during its slot, so "open Slack" is asked on a Monday around
  09:00, not at a random hour.
* A ``trigger_action`` (file lands -> moved, mail -> app...) needs a trigger
  the built-ins don't have, so it is a ``generate_custom_tool`` idea (code
  review and dry run follow, invariant 7b).

Filters (invariant 7d): the mining thresholds themselves, the shared
``IdeaFeedback`` (an idea resembling a declined one is dropped), and a
cooldown keyed on the candidate's ``id`` (one offer per routine per
:data:`COOLDOWN_SECONDS`). At most one idea per tick.

Privacy (invariant 7c): everything a prompt can see (``description``,
``expected_logic``, ``event_details``, ``event_details_redacted``) is built
from ``candidate["summary"]`` only (app names, folder, extension, counts).
The evidence line for the card (``card_text``) is read by the approval card
alone, never by an engine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from ai import activity_store, routines
from ai.idea_sources import FeedbackStore, IdeaState, Publish, _CooldownSource
from events.event_bus import EventBus

logger = logging.getLogger(__name__)

MINE_INTERVAL_SECONDS = 24 * 3600.0
TICK_SECONDS = 600.0
COOLDOWN_SECONDS = 30 * 24 * 3600.0
BUILTIN_TOOLS = frozenset({"open_app", "run_shortcut"})


async def _no_model(_: str) -> str:  # the source never asks a model
    return ""


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "_".join(words)[:40].strip("_") or "helper"


class RoutineIdeaSource(_CooldownSource):
    """Daily routine mining that publishes at most one idea per tick."""

    source_name = "routine"

    def __init__(
        self,
        *,
        event_bus: EventBus,
        state: Optional[IdeaState] = None,
        feedback: Optional[FeedbackStore] = None,
        publish: Optional[Publish] = None,
        find_candidates: Callable[..., list[dict[str, Any]]] = routines.find_candidates,
        enabled: Callable[[], bool] = activity_store.history_enabled,
        clock: Callable[[], datetime] = datetime.now,
        shortcuts: Optional[Callable[[], Iterable[str]]] = None,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        tick_seconds: float = TICK_SECONDS,
    ) -> None:
        super().__init__(
            event_bus=event_bus, evaluate=_no_model,
            cooldown_seconds=cooldown_seconds, publish=publish,
            state=state, feedback=feedback,
        )
        self._find = find_candidates
        self._enabled = enabled
        self._clock = clock
        self._shortcuts = shortcuts
        self.tick_seconds = tick_seconds
        self._candidates: list[dict[str, Any]] = []
        self._mined_at: Optional[datetime] = None
        self._task: Optional[asyncio.Task[None]] = None

    # ---- the idea -------------------------------------------------------

    @staticmethod
    def due(candidate: dict[str, Any], now: datetime) -> bool:
        """A time routine is offered only in its weekday-hour slot."""
        if candidate.get("kind") != "time_routine":
            return True
        trig = candidate.get("trigger") or {}
        return (
            routines.WEEKDAYS[now.weekday()] in (trig.get("weekdays") or [])
            and now.hour == int(trig.get("hour", -1))
        )

    @staticmethod
    def idea_for(candidate: dict[str, Any]) -> dict[str, Any]:
        """The ``pattern_detected`` metadata for one candidate."""
        summary = str(candidate.get("summary") or "")
        card_text = (
            f"Routine: {candidate.get('title', '')} "
            f"({candidate.get('evidence_text', '')})"
        )
        idea: dict[str, Any] = {
            "source": "routine",
            "trigger": str(candidate.get("kind") or "routine"),
            "routine_id": str(candidate.get("id") or ""),
            "description": summary,
            "expected_logic": summary,
            "card_text": card_text,
        }
        tool = str(candidate.get("tool") or "")
        if candidate.get("kind") == "time_routine" and tool in BUILTIN_TOOLS:
            args = json.dumps(candidate.get("tool_args") or {}, sort_keys=True)
            idea.update({
                "tool_name": tool,
                "builtin_tool": tool,
                # Both copies are built from the summary only; the local one
                # adds the arguments, which are app / Shortcut names the
                # summary already carries.
                "event_details": f"{summary}\nArguments: {args}",
                "event_details_redacted": summary,
            })
        else:
            idea["tool_name"] = "routine_" + _slug(str(candidate.get("title") or ""))
        return idea

    # ---- one pass ---------------------------------------------------------

    def _shortcut_names(self) -> list[str]:
        if self._shortcuts is None:
            return []
        try:
            return list(self._shortcuts())
        except Exception:
            logger.info("routine ideas: shortcuts unavailable", exc_info=True)
            return []

    def _mine(self, now: datetime) -> None:
        if (
            self._mined_at is not None
            and (now - self._mined_at).total_seconds() < MINE_INTERVAL_SECONDS
        ):
            return
        self._mined_at = now
        try:
            self._candidates = list(self._find(shortcuts=self._shortcut_names()))
        except Exception:
            logger.exception("routine mining failed")
            self._candidates = []

    async def tick(self, now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
        """Mine (at most daily) and publish the first due, fresh candidate."""
        if not self._enabled():
            return None
        now = now or self._clock()
        await asyncio.to_thread(self._mine, now)
        for cand in self._candidates:
            key = str(cand.get("id") or "")
            if not key or self._cooling(key) or not self.due(cand, now):
                continue
            idea = self.idea_for(cand)
            self._start_cooldown(key)
            if self._rejected(idea):
                continue
            await self._publish_idea(idea)
            return idea
        return None

    # ---- lifecycle ----------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("routine idea tick failed")
            await asyncio.sleep(self.tick_seconds)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="routine-ideas")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
