"""Open-ended help source: ask, filter, learn (LIVING_SPEC §1).

The other idea sources only fire on repetition or a fixed list of event
kinds. :class:`OpenHelpSuggester` asks the guiding question on every
window of activity: *"How could I possibly help the user here?"*

A small model asked that question over-proposes, so the answer never
reaches the user directly. Three stages:

1. **Generate**: :data:`OPEN_HELP_SYSTEM_PROMPT` turns recent events into
   at most one candidate tool idea.
2. **Filter**: deterministic checks, then a separate judge call
   (:data:`IDEA_JUDGE_SYSTEM_PROMPT`) scores the idea 0-10. Only ideas at
   or above ``min_score`` are published.
3. **Learn**: :class:`IdeaFeedback` records which ideas the user approved
   or rejected. Ideas close to a rejected one are dropped, and recent
   verdicts are shown to the judge so the bar fits this user.

Approved ideas publish the shared ``pattern_detected`` event with
``source="open_help"``, so the approve-idea -> draft -> review-code flow is
unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from ai.idea_sources import (
    NO_SUGGESTION,
    Evaluate,
    Publish,
    _PROPOSAL_FORMAT,
    parse_suggestion,
)
from events.event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

GUIDING_QUESTION = "How could I possibly help the user here?"

OPEN_HELP_SYSTEM_PROMPT = (
    "You are a proactive macOS assistant watching recent activity on the "
    f"user's Mac. Ask yourself: {GUIDING_QUESTION}\n\n"
    "Think of the single most useful small tool that would save the user "
    "time or effort, given what they are doing right now. Good ideas "
    "remove a step the user is clearly about to do, or will do again. "
    "The tool must work for any future case of the same kind (take the "
    "path, text or number as an input), and must never delete or send "
    "anything without a dry_run option that defaults to True.\n\n"
    "Most activity needs no tool. If nothing would clearly help, say so.\n\n"
    + _PROPOSAL_FORMAT
)

IDEA_JUDGE_SYSTEM_PROMPT = (
    "You review a proposed helper tool for a macOS user before it is "
    "shown to them. Be strict: the user only wants ideas that are clearly "
    "useful. Score the idea from 0 to 10:\n"
    "- 9-10: removes a step the events show the user doing or about to do;\n"
    "- 6-8: plausibly useful but not shown by the events;\n"
    "- 0-5: generic, unrelated to the events, already trivial in macOS, "
    "or not doable with the Python standard library and macOS commands.\n"
    "Ideas similar to ones the user rejected score low. Ideas similar to "
    "ones they approved may score higher.\n\n"
    "Reply with exactly one JSON object and nothing else:\n"
    '{"score": <integer 0-10>, "reason": "<one sentence>"}'
)

_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_MIN_LOGIC_CHARS = 60
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and the to of for in on with from by or is it its this that "
    "file files user tool".split()
)


def _tokens(*texts: str) -> set[str]:
    words: set[str] = set()
    for text in texts:
        words.update(_WORD_RE.findall((text or "").lower().replace("_", " ")))
    return words - _STOPWORDS


def _similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class IdeaFeedback:
    """Approve/reject history for tool ideas, persisted as JSON.

    ``path=None`` keeps history in memory only (tests).
    """

    MAX_ENTRIES = 200
    REJECT_SIMILARITY = 0.5

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            logger.warning("idea feedback at %s unreadable; starting empty", self.path)
            return
        if isinstance(data, list):
            self.entries = [e for e in data if isinstance(e, dict)][-self.MAX_ENTRIES:]

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entries, indent=1))
            tmp.replace(self.path)
        except OSError:
            logger.exception("failed to save idea feedback to %s", self.path)

    def record(self, tool_name: str, description: str, approved: bool) -> None:
        self.entries.append({
            "tool_name": str(tool_name or ""),
            "description": str(description or "")[:300],
            "approved": bool(approved),
            "at": time.time(),
        })
        self.entries = self.entries[-self.MAX_ENTRIES:]
        self._save()

    def resembles_rejected(self, tool_name: str, description: str) -> bool:
        idea = _tokens(tool_name, description)
        for entry in self.entries:
            if entry.get("approved"):
                continue
            if entry.get("tool_name") == tool_name:
                return True
            past = _tokens(entry.get("tool_name", ""), entry.get("description", ""))
            if _similarity(idea, past) >= self.REJECT_SIMILARITY:
                return True
        return False

    def summary(self, limit: int = 8) -> str:
        """Recent verdicts as judge-prompt lines (newest last)."""
        lines = []
        for entry in self.entries[-limit:]:
            verdict = "approved" if entry.get("approved") else "rejected"
            lines.append(
                f"- {verdict}: {entry.get('tool_name')} — {entry.get('description')}"
            )
        return "\n".join(lines)


def parse_score(raw: str) -> Optional[int]:
    """Pull an integer 0-10 score out of a judge reply; ``None`` if absent."""
    text = (raw or "").strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            score = json.loads(match.group(0)).get("score")
            if isinstance(score, (int, float)):
                return max(0, min(10, int(score)))
        except (ValueError, AttributeError):
            pass
    match = re.search(r'"?score"?\s*[:=]\s*(\d{1,2})', text, re.IGNORECASE)
    if match:
        return max(0, min(10, int(match.group(1))))
    return None


def passes_checks(idea: dict[str, Any]) -> bool:
    """Deterministic filter: well-formed name and a usable spec."""
    name = str(idea.get("tool_name") or "")
    logic = str(idea.get("expected_logic") or "")
    desc = str(idea.get("description") or "")
    return bool(_SNAKE_RE.match(name)) and len(logic) >= _MIN_LOGIC_CHARS and bool(desc)


def _describe(event: ContextEvent) -> str:
    meta = event.metadata or {}
    if event.event_type == "file":
        return f"file {meta.get('action', '?')}: {meta.get('path', '?')}"
    if event.event_type == "imessage_received":
        return f"iMessage received: {str(meta.get('text') or '')[:200]!r}"
    if event.event_type == "app_activated":
        title = f" — {event.window_title}" if event.window_title else ""
        return f"switched to {event.app_name or '?'}{title}"
    return f"{event.event_type}: {json.dumps(meta, default=str)[:200]}"


class OpenHelpSuggester:
    """Ask the guiding question over recent events; publish filtered ideas."""

    source_name = "open_help"
    # Events produced by the agent itself, or handled by chat.
    _SKIP_TYPES = frozenset({"pattern_detected", "user_message"})

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        judge: Evaluate,
        feedback: Optional[IdeaFeedback] = None,
        interval_seconds: float = 600.0,
        window_size: int = 20,
        min_events: int = 3,
        min_score: int = 7,
        idea_cooldown_seconds: float = 7 * 24 * 3600.0,
        publish: Optional[Publish] = None,
    ) -> None:
        self.event_bus = event_bus
        self._evaluate = evaluate
        self._judge = judge
        self.feedback = feedback or IdeaFeedback()
        self.interval_seconds = interval_seconds
        self.min_events = min_events
        self.min_score = min_score
        self.idea_cooldown_seconds = idea_cooldown_seconds
        self._publish = publish or self.event_bus.push
        self._window: deque[ContextEvent] = deque(maxlen=window_size)
        self._new_since_ask = 0
        self._next_ask_at = 0.0
        self._recent_ideas: dict[str, float] = {}
        self._task: Optional[asyncio.Task[None]] = None

    # ---- generate ------------------------------------------------------

    def observe(self, event: ContextEvent) -> Optional[asyncio.Task[None]]:
        """Buffer ``event``; ask the guiding question when due."""
        if event.event_type in self._SKIP_TYPES:
            return None
        self._window.append(event)
        self._new_since_ask += 1
        now = time.time()
        if self._new_since_ask < self.min_events or now < self._next_ask_at:
            return None
        if self._task is not None and not self._task.done():
            return None
        self._next_ask_at = now + self.interval_seconds
        self._new_since_ask = 0
        prompt = "Recent events (oldest first):\n" + "\n".join(
            f"- {_describe(e)}" for e in self._window
        )
        self._task = asyncio.create_task(self._run(prompt), name="open-help")
        return self._task

    async def _run(self, events_text: str) -> None:
        try:
            raw = await self._evaluate(events_text)
        except Exception:
            logger.exception("open_help evaluate failed")
            return
        idea = parse_suggestion(raw)
        if idea is None:
            return
        if not await self._accept(idea, events_text):
            return
        idea["source"] = self.source_name
        idea["trigger"] = "open_help"
        self._recent_ideas[idea["tool_name"]] = time.time() + self.idea_cooldown_seconds
        try:
            await self._publish(ContextEvent(event_type="pattern_detected", metadata=idea))
        except Exception:
            logger.exception("open_help failed to publish suggestion")

    # ---- filter --------------------------------------------------------

    async def _accept(self, idea: dict[str, Any], events_text: str) -> bool:
        name = str(idea.get("tool_name") or "")
        desc = str(idea.get("description") or "")
        if not passes_checks(idea):
            logger.info("open_help dropped malformed idea %r", name)
            return False
        if time.time() < self._recent_ideas.get(name, 0.0):
            return False
        if self.feedback.resembles_rejected(name, desc):
            logger.info("open_help dropped %r: resembles a rejected idea", name)
            return False
        history = self.feedback.summary()
        judge_prompt = (
            f"{events_text}\n\nProposed tool: {name}\n"
            f"Description: {desc}\n"
            f"Expected logic: {str(idea.get('expected_logic'))[:800]}"
            + (f"\n\nThe user's past verdicts:\n{history}" if history else "")
        )
        try:
            score = parse_score(await self._judge(judge_prompt))
        except Exception:
            logger.exception("open_help judge failed")
            return False
        if score is None or score < self.min_score:
            logger.info("open_help dropped %r: judge score %s", name, score)
            return False
        idea["judge_score"] = score
        return True

    # ---- learn ---------------------------------------------------------

    def record_feedback(self, tool_name: str, description: str, approved: bool) -> None:
        """Called by the orchestrator when the user rules on a tool idea."""
        self.feedback.record(tool_name, description, approved)


__all__ = [
    "GUIDING_QUESTION",
    "IDEA_JUDGE_SYSTEM_PROMPT",
    "NO_SUGGESTION",
    "OPEN_HELP_SYSTEM_PROMPT",
    "IdeaFeedback",
    "OpenHelpSuggester",
    "parse_score",
    "passes_checks",
]
