"""Observe recent events and propose new tools the agent could author.

The recognizer is fed by :class:`~ai.orchestrator.Orchestrator` — one event
in, one optional ``pattern_detected`` event back on the bus — so the main
observation loop remains the sole consumer of ``EventBus.consume()``.

Detection cadence is deliberately conservative: we only ask the analysis
engine to evaluate the rolling window once every :attr:`trigger_every`
events or after :attr:`trigger_interval` seconds, whichever comes first,
and we cool down for :attr:`cooldown_seconds` after every hit so the user
is not flooded with duplicate suggestions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Optional

from events.event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_DETECTION_PROMPT = (
    "You are watching a rolling window of recent macOS events from the "
    "user's machine. If — and only if — you see the user repeating a "
    "concrete, automatable workflow at least three times, propose a new "
    "tool that would automate it. Otherwise reply with the literal "
    "string NO_PATTERN.\n\n"
    "When you do propose a tool, reply with one line of JSON and nothing "
    "else, of the form:\n"
    '{"tool_name": "<snake_case>", "description": "<one sentence>", '
    '"expected_logic": "<short paragraph>"}\n\n'
    "Recent events:\n"
)


class PatternRecognizer:
    """Rolling-window heuristic that suggests new tools to draft.

    The recognizer does *not* touch the event bus directly — the
    orchestrator calls :meth:`observe` for every event it handles. When a
    detection fires, the recognizer publishes a synthetic
    ``ContextEvent(event_type="pattern_detected", metadata=...)`` via the
    supplied ``publish`` coroutine so the orchestrator's normal loop picks
    it up on the next iteration.
    """

    # Maximum recent suggestions to track for de-duplication (prevents ping-pong)
    _MAX_RECENT_SUGGESTIONS = 10
    # Per-tool cooldown after suggestion (prevents rapid re-suggestion)
    _PER_TOOL_COOLDOWN_SECONDS = 600.0  # 10 minutes

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Callable[[str], Awaitable[str]],
        window_size: int = 50,
        trigger_every: int = 10,
        trigger_interval: float = 60.0,
        cooldown_seconds: float = 300.0,
        publish: Optional[Callable[[ContextEvent], Awaitable[None]]] = None,
    ) -> None:
        self.event_bus = event_bus
        self._evaluate = evaluate
        self._window: Deque[str] = deque(maxlen=window_size)
        self.trigger_every = trigger_every
        self.trigger_interval = trigger_interval
        self.cooldown_seconds = cooldown_seconds
        self._publish = publish or self._default_publish
        self._since_last_eval: int = 0
        self._last_eval_at: float = 0.0
        self._cooldown_until: float = 0.0
        self._last_suggestion: Optional[str] = None
        # Track per-tool cooldowns to prevent re-suggesting recently suggested tools
        self._tool_cooldowns: dict[str, float] = {}
        # Recent suggestions deque to detect ping-pong patterns
        self._recent_suggestions: Deque[str] = deque(maxlen=self._MAX_RECENT_SUGGESTIONS)
        self._eval_lock = asyncio.Lock()

    # ---- public API --------------------------------------------------

    def observe(self, event: ContextEvent) -> Optional[asyncio.Task[None]]:
        """Record ``event`` and, if thresholds are met, schedule detection.

        Returns the created background :class:`asyncio.Task` for tests to
        await, or ``None`` when no detection fires this call.
        """
        # Agent-internal events never re-enter the window.
        if event.event_type == "pattern_detected":
            return None
        self._window.append(self._describe(event))
        self._since_last_eval += 1
        now = time.time()
        if now < self._cooldown_until:
            return None
        enough_events = self._since_last_eval >= self.trigger_every
        enough_time = (
            self._last_eval_at > 0
            and (now - self._last_eval_at) >= self.trigger_interval
        )
        if not (enough_events or enough_time):
            return None
        return asyncio.create_task(self._run_detection(), name="pattern-detect")

    # ---- internals ---------------------------------------------------

    async def _run_detection(self) -> None:
        async with self._eval_lock:
            self._since_last_eval = 0
            self._last_eval_at = time.time()
            prompt = _DETECTION_PROMPT + "\n".join(
                f"- {line}" for line in list(self._window)
            )
            try:
                raw = await self._evaluate(prompt)
            except Exception:
                logger.exception("pattern detection evaluate failed")
                return
            suggestion = self._parse(raw or "")
            if suggestion is None:
                return
            tool_name = suggestion["tool_name"]

            # Guard 1: Skip if identical to the last suggestion
            if tool_name == self._last_suggestion:
                logger.debug("Skipping duplicate suggestion: %s", tool_name)
                return

            # Guard 2: Skip if this tool is in per-tool cooldown (recently suggested)
            now = time.time()
            tool_cooldown_until = self._tool_cooldowns.get(tool_name, 0.0)
            if now < tool_cooldown_until:
                logger.debug(
                    "Skipping %s: still in per-tool cooldown for %.1fs",
                    tool_name,
                    tool_cooldown_until - now,
                )
                return

            # Guard 3: Detect ping-pong patterns (same tool suggested multiple times recently)
            recent_count = sum(1 for t in self._recent_suggestions if t == tool_name)
            if recent_count >= 2:
                logger.warning(
                    "Suppressing %s: suggested %d times recently (ping-pong guard)",
                    tool_name,
                    recent_count,
                )
                # Apply extended cooldown to break the cycle
                self._tool_cooldowns[tool_name] = now + self._PER_TOOL_COOLDOWN_SECONDS * 2
                return

            # Update tracking state
            self._last_suggestion = tool_name
            self._recent_suggestions.append(tool_name)
            self._tool_cooldowns[tool_name] = now + self._PER_TOOL_COOLDOWN_SECONDS
            self._cooldown_until = now + self.cooldown_seconds

            # Prune expired cooldowns to avoid memory growth
            self._tool_cooldowns = {
                k: v for k, v in self._tool_cooldowns.items() if v > now
            }
            event = ContextEvent(
                event_type="pattern_detected", metadata=suggestion
            )
            try:
                await self._publish(event)
            except Exception:
                logger.exception("failed to publish pattern_detected event")

    async def _default_publish(self, event: ContextEvent) -> None:
        await self.event_bus.push(event)

    @staticmethod
    def _parse(raw: str) -> Optional[dict[str, Any]]:
        text = raw.strip()
        if not text or "NO_PATTERN" in text.upper():
            return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        tool_name = str(data.get("tool_name") or "").strip()
        description = str(data.get("description") or "").strip()
        expected_logic = str(data.get("expected_logic") or "").strip()
        if not _IDENTIFIER_RE.match(tool_name):
            return None
        if not description or not expected_logic:
            return None
        return {
            "tool_name": tool_name,
            "description": description,
            "expected_logic": expected_logic,
        }

    @staticmethod
    def _describe(event: ContextEvent) -> str:
        parts = [event.event_type]
        if event.app_name:
            parts.append(f"app={event.app_name}")
        if event.window_title:
            parts.append(f"window={event.window_title}")
        if event.metadata:
            meta_preview = ", ".join(
                f"{k}={str(v)[:40]}" for k, v in list(event.metadata.items())[:4]
            )
            parts.append(meta_preview)
        return " | ".join(parts)
