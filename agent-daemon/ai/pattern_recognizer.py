"""Observe recent events and propose new tools the agent could author.

The recognizer is fed by :class:`~ai.orchestrator.Orchestrator` — one event
in, one optional ``pattern_detected`` event back on the bus — so the main
observation loop remains the sole consumer of ``EventBus.consume()``.

Detection cadence is deliberately conservative: we only ask the analysis
engine to evaluate the rolling window once every :attr:`trigger_every`
events or after :attr:`trigger_interval` seconds, whichever comes first,
and we cool down for :attr:`cooldown_seconds` after every hit so the user
is not flooded with duplicate suggestions.

Adaptive cadence (todo #25): every model call that finds nothing
(``NO_PATTERN``) doubles :attr:`current_interval`, capped at
:attr:`max_trigger_interval` (10 min); a match resets it to
:attr:`trigger_interval`. While backed off, the event-count trigger is
held: a call needs ``current_interval`` seconds since the last one,
because with a busy user the count trigger alone fires every few seconds
and the backed-off interval would otherwise never be reached. Repeat-gate
skips make no model call and leave the cadence unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Optional

from ai.mlx_engine import THINK_BLOCK_RE as _THINK_BLOCK_RE
from ai.privacy import redact_event
from events.event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
# The ``app=<name>`` field :meth:`PatternRecognizer._describe` writes.
_APP_FIELD_RE = re.compile(r"\bapp=([^|,]+)")


# Token budget for a detection reply. A full JSON proposal with a
# paragraph of ``expected_logic`` needs ~150-300 tokens; the old 128-token
# cap inherited from ``evaluate_event`` truncated it mid-object.
DETECTION_MAX_TOKENS = 512

# System prompt for pattern detection. Must be sent as the *system* role:
# routing detection through ``evaluate_event`` wrapped it in the
# event-summary system prompt ("one or two sentences, do not propose
# actions"), which directly contradicted these instructions.
DETECTION_SYSTEM_PROMPT = (
    "You watch a rolling window of recent macOS events from the user's "
    "machine and decide whether a new automation tool is warranted.\n\n"
    "Propose a tool if — and only if — the user repeats the same "
    "concrete, automatable workflow at least three times in the window "
    "(same apps, same kind of file, same sequence). Switching between "
    "apps is not a workflow: never propose a tool that switches, toggles, "
    "lists or counts apps. Otherwise reply with "
    "the literal string NO_PATTERN and nothing else.\n\n"
    "When you propose a tool, reply with exactly one JSON object and "
    "nothing else (no markdown, no commentary):\n"
    '{"tool_name": "<snake_case>", "description": "<one sentence>", '
    '"expected_logic": "<paragraph>"}\n\n'
    "expected_logic must be specific enough for a programmer to implement "
    "without seeing the events: name the inputs and their types, the "
    "exact steps (which app, file path pattern, or macOS command), the "
    "output, and what to do when an input is missing."
)

_DETECTION_USER_PREFIX = "Recent events (oldest first):\n"


def make_detection_evaluator(
    engine: Any,
    max_tokens: int = DETECTION_MAX_TOKENS,
    system_prompt: str = DETECTION_SYSTEM_PROMPT,
) -> Callable[[str], Awaitable[str]]:
    """Build the ``evaluate`` callable :class:`PatternRecognizer` expects.

    ``engine`` is any object exposing ``generate_chat_sync(system, user,
    max_tokens)`` (both :class:`~ai.mlx_engine.MLXEngine` and
    :class:`~ai.groq_engine.GroqEngine` do). The call runs in a worker
    thread so blocking inference never stalls the event loop.
    """

    async def evaluate(user_text: str) -> str:
        if not getattr(engine, "loaded", True):
            await engine.load()
        raw = await asyncio.to_thread(
            engine.generate_chat_sync,
            system_prompt,
            user_text,
            max_tokens,
        )
        return _THINK_BLOCK_RE.sub("", raw or "").strip()

    return evaluate


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
        min_repeats: int = 3,
        max_trigger_interval: float = 600.0,
        clock: Optional[Callable[[], float]] = None,
        publish: Optional[Callable[[ContextEvent], Awaitable[None]]] = None,
    ) -> None:
        self.event_bus = event_bus
        self._evaluate = evaluate
        self._window: Deque[str] = deque(maxlen=window_size)
        # Coarse per-event signatures, parallel to ``_window``, for the
        # repeat gate in :meth:`_has_repetition`.
        self._signatures: Deque[str] = deque(maxlen=window_size)
        self.min_repeats = min_repeats
        self.trigger_every = trigger_every
        self.trigger_interval = trigger_interval
        self.cooldown_seconds = cooldown_seconds
        # Never cap below the base interval (tests use a 3600 s base).
        self.max_trigger_interval = max(max_trigger_interval, trigger_interval)
        # Time-based trigger in effect now; doubles on NO_PATTERN.
        self.current_interval: float = trigger_interval
        self._clock: Callable[[], float] = clock or time.time
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
        # The window goes to the detection engine: no personal text
        # when that engine is a cloud one (#31).
        self._window.append(self._describe(redact_event(event, "detection")))
        self._signatures.append(self._signature(event))
        self._since_last_eval += 1
        now = self._clock()
        if now < self._cooldown_until:
            return None
        enough_time = (
            self._last_eval_at > 0
            and (now - self._last_eval_at) >= self.current_interval
        )
        # Backed off: only the (longer) interval may trigger a call.
        enough_events = (
            not self.backed_off
            and self._since_last_eval >= self.trigger_every
        )
        if not (enough_events or enough_time):
            return None
        if not self._has_repetition():
            # Nothing repeats often enough to be a workflow. Skip the LLM
            # call entirely: models given a window of unrelated events
            # still invent a "pattern" (e.g. a daily_rituals tool for ten
            # random app switches). Reset the trigger so we re-check after
            # the next batch rather than on every event.
            self._since_last_eval = 0
            self._last_eval_at = now
            return None
        return asyncio.create_task(self._run_detection(), name="pattern-detect")

    @property
    def backed_off(self) -> bool:
        """True while NO_PATTERN replies have stretched the interval."""
        return self.current_interval > self.trigger_interval

    # ---- internals ---------------------------------------------------

    def _set_interval(self, value: float, reason: str) -> None:
        if value == self.current_interval:
            return
        logger.info(
            "pattern detection interval %.0fs -> %.0fs (%s)",
            self.current_interval, value, reason,
        )
        self.current_interval = value

    async def _run_detection(self) -> None:
        async with self._eval_lock:
            self._since_last_eval = 0
            self._last_eval_at = self._clock()
            prompt = _DETECTION_USER_PREFIX + "\n".join(
                f"- {line}" for line in list(self._window)
            )
            try:
                raw = await self._evaluate(prompt)
            except Exception:
                logger.exception("pattern detection evaluate failed")
                return
            suggestion = self._parse(raw or "")
            if suggestion is None:
                # NO_PATTERN (or an unusable reply): back off.
                self._set_interval(
                    min(self.current_interval * 2, self.max_trigger_interval),
                    "no pattern",
                )
                return
            # An app switcher is not a pattern (todo #66). Imported here:
            # ai.open_help imports ai.idea_sources, which imports this module.
            from ai.open_help import is_app_switcher

            apps = {m.strip() for line in self._window for m in _APP_FIELD_RE.findall(line)}
            if is_app_switcher(suggestion, apps):
                logger.info(
                    "pattern detection dropped %r: it only switches, lists or counts apps",
                    suggestion["tool_name"],
                )
                self._set_interval(
                    min(self.current_interval * 2, self.max_trigger_interval),
                    "app switcher",
                )
                return
            self._set_interval(self.trigger_interval, "pattern found")
            tool_name = suggestion["tool_name"]

            # Guard 1: Skip if identical to the last suggestion
            if tool_name == self._last_suggestion:
                logger.debug("Skipping duplicate suggestion: %s", tool_name)
                return

            # Guard 2: Skip if this tool is in per-tool cooldown (recently suggested)
            now = self._clock()
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
            # strict=False: models often put raw newlines inside the
            # expected_logic string (numbered steps), which strict JSON
            # rejects as control characters.
            data = json.loads(match.group(0), strict=False)
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

    def _has_repetition(self) -> bool:
        """True when some event signature occurs ``min_repeats``+ times."""
        counts: dict[str, int] = {}
        for sig in self._signatures:
            counts[sig] = counts.get(sig, 0) + 1
            if counts[sig] >= self.min_repeats:
                return True
        return False

    @staticmethod
    def _signature(event: ContextEvent) -> str:
        """Coarse key for "the same kind of step": type, app, file action.

        Window titles and paths are left out on purpose so the three
        rounds of a workflow (``inv1041.pdf``, ``inv1042.pdf``, …) map to
        the same signature.
        """
        action = ""
        if event.metadata:
            action = str(event.metadata.get("action") or "")
        return f"{event.event_type}|{event.app_name or ''}|{action}"

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
