"""Tool-idea sources beyond repetition (Phase 5.2).

Repetition (:mod:`ai.pattern_recognizer`) only finds what the user already
does by hand, after three rounds. The sources here suggest a tool *before*
the user thinks to ask:

* :class:`NextStepSuggester` (source 1): one high-signal event with an
  obvious, repeatable follow-up (a finished ``.dmg`` download, an
  iMessage with a tracking number).
* :class:`ChatGapDetector` (source 2): the agent could not do what the
  user asked in chat.

Both publish the same ``pattern_detected`` event the recognizer uses, so
the approve-idea → draft → review-code flow is shared. ``metadata.source``
says which source produced the idea. Deterministic filters run first so
the model is only called for events worth a suggestion.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import PurePath
from typing import Any, Awaitable, Callable, Optional

from ai.pattern_recognizer import PatternRecognizer
from events.event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

Evaluate = Callable[[str], Awaitable[str]]
Publish = Callable[[ContextEvent], Awaitable[None]]

NO_SUGGESTION = "NO_SUGGESTION"

_PROPOSAL_FORMAT = (
    "When you propose a tool, reply with exactly one JSON object and "
    "nothing else (no markdown, no commentary):\n"
    '{"tool_name": "<snake_case>", "description": "<one sentence>", '
    '"expected_logic": "<paragraph>"}\n\n'
    "expected_logic must be specific enough for a programmer to implement "
    "with the Python standard library and macOS commands: name the "
    "inputs and their types, the exact steps, the output, and what to do "
    "when an input is missing. Otherwise reply with the literal string "
    f"{NO_SUGGESTION} and nothing else."
)

NEXT_STEP_SYSTEM_PROMPT = (
    "You are a proactive macOS assistant. One event just happened on the "
    "user's Mac, and it has a known follow-up that is worth turning into "
    "a reusable one-click tool. Write the tool proposal for that "
    "follow-up. The tool must work for any future event of the same "
    "kind (take the file path, text or number as an input), not just "
    "this one, and must never delete or send anything without a "
    "dry_run option that defaults to True.\n\n"
    "If the given follow-up does not make sense for this specific event, "
    "do not propose.\n\n"
    + _PROPOSAL_FORMAT
)

# Event kinds with a known-good follow-up. Only these trigger a
# suggestion: given an open-ended "what next?", an 8B model proposes a
# tool for every event (Phase 5.2 test: "add a screenshot to the
# calendar"), so the follow-up comes from this list and the model only
# writes the spec for it.
FOLLOW_UPS: dict[str, str] = {
    "installer_downloaded": (
        "Install the app from the downloaded .dmg/.pkg (mount, copy the "
        ".app to /Applications, unmount), then move the installer to the Trash."
    ),
    "archive_downloaded": (
        "Unzip the archive next to itself, open the extracted folder in "
        "Finder, then move the .zip to the Trash."
    ),
    "calendar_invite_downloaded": (
        "Add the .ics invite to the Calendar app, then move the file to the Trash."
    ),
    "tracking_number": (
        "Detect the carrier from the tracking number format and open its "
        "tracking page for that number."
    ),
    "plan_with_time": (
        "Create a Calendar event from the message (title, date and time "
        "parsed from the text), asking for confirmation first."
    ),
}

CHAT_GAP_SYSTEM_PROMPT = (
    "You review a chat turn where a macOS assistant could not do what the "
    "user asked. Decide whether a small local tool would let it succeed "
    "next time. Propose a tool only when the request is an action or a "
    "lookup on this Mac (files, apps, calendar, clipboard, system state, "
    "a public web API) that the Python standard library or macOS "
    "commands (osascript, shortcuts, mdfind, open) can do. Questions "
    "that only need knowledge, opinions or chit-chat do not need a tool.\n\n"
    + _PROPOSAL_FORMAT
)

# File extensions mapped to a trigger kind in :data:`FOLLOW_UPS`.
_FILE_KINDS: dict[str, str] = {
    ".dmg": "installer_downloaded",
    ".pkg": "installer_downloaded",
    ".zip": "archive_downloaded",
    ".ics": "calendar_invite_downloaded",
}
# Temporary names browsers use while a download is in flight.
_TEMP_SUFFIXES = (".crdownload", ".download", ".part", ".tmp")

# Message content mapped to a trigger kind in :data:`FOLLOW_UPS`.
# Order matters: the first match wins.
_MESSAGE_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tracking_number", re.compile(
        r"\b(1Z[0-9A-Z]{16}|9[0-9]{21}|TBA[0-9]{12})\b"
        r"|\b(?:tracking|track|fedex|usps|ups|shipped|delivery)\b.{0,60}\b[0-9]{12,15}\b",
        re.IGNORECASE,
    )),
    ("plan_with_time", re.compile(
        r"\b(meet|dinner|lunch|call|tomorrow|tonight|on (mon|tue|wed|thu|fri|sat|sun)\w*)\b"
        r".{0,40}\b(\d{1,2}(:\d{2})?\s?(am|pm)|noon)\b",
        re.IGNORECASE,
    )),
)

# Phrases that mean the agent could not do what was asked.
_GAP_RE = re.compile(
    r"\b("
    r"I(?: cannot| can['’]t|(?: am|['’]m) (?:unable|not able) to)"
    r"|I don['’]t have (?:a |the |any )?(?:tool|ability|access|way)"
    r"|(?:no|none of my) (?:available |existing )?tools?"
    r"|not (?:currently )?(?:able|possible) to"
    r"|beyond my (?:current )?capabilities"
    r")",
    re.IGNORECASE,
)


def parse_suggestion(raw: str) -> Optional[dict[str, Any]]:
    """Parse a proposal reply; ``None`` for NO_SUGGESTION or junk."""
    text = (raw or "").strip()
    if not text or NO_SUGGESTION in text.upper():
        return None
    return PatternRecognizer._parse(text)


class _CooldownSource:
    """Shared plumbing: per-key cooldowns, background tasks, publishing."""

    source_name = "unknown"

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        cooldown_seconds: float,
        publish: Optional[Publish] = None,
    ) -> None:
        self.event_bus = event_bus
        self._evaluate = evaluate
        self.cooldown_seconds = cooldown_seconds
        self._publish = publish or self.event_bus.push
        self._cooldowns: dict[str, float] = {}

    def _cooling(self, key: str) -> bool:
        return time.time() < self._cooldowns.get(key, 0.0)

    def _start_cooldown(self, key: str) -> None:
        now = time.time()
        self._cooldowns[key] = now + self.cooldown_seconds
        self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}

    async def _suggest(self, user_text: str, trigger: str) -> None:
        try:
            raw = await self._evaluate(user_text)
        except Exception:
            logger.exception("%s evaluate failed", self.source_name)
            return
        suggestion = parse_suggestion(raw)
        if suggestion is None:
            return
        suggestion["source"] = self.source_name
        suggestion["trigger"] = trigger
        try:
            await self._publish(
                ContextEvent(event_type="pattern_detected", metadata=suggestion)
            )
        except Exception:
            logger.exception("%s failed to publish suggestion", self.source_name)


class NextStepSuggester(_CooldownSource):
    """Suggest a tool for a single event with an obvious follow-up."""

    source_name = "next_step"

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        cooldown_seconds: float = 24 * 3600.0,
        publish: Optional[Publish] = None,
    ) -> None:
        super().__init__(
            event_bus=event_bus, evaluate=evaluate,
            cooldown_seconds=cooldown_seconds, publish=publish,
        )

    @staticmethod
    def classify(event: ContextEvent) -> Optional[str]:
        """Return a trigger kind for high-signal events, else ``None``."""
        meta = event.metadata or {}
        if event.event_type == "file":
            if meta.get("action") not in {"created", "renamed"}:
                return None
            name = PurePath(str(meta.get("path") or "")).name
            if not name or name.startswith(".") or name.lower().endswith(_TEMP_SUFFIXES):
                return None
            return _FILE_KINDS.get(PurePath(name).suffix.lower())
        if event.event_type == "imessage_received":
            text = str(meta.get("text") or "")
            for kind, pattern in _MESSAGE_KINDS:
                if pattern.search(text):
                    return kind
        return None

    @staticmethod
    def describe(event: ContextEvent, kind: str) -> str:
        meta = event.metadata or {}
        follow_up = FOLLOW_UPS.get(kind, "")
        if event.event_type == "file":
            path = str(meta.get("path") or "")
            what = (
                f"A file appeared in a watched folder: {PurePath(path).name}\n"
                f"Folder: {PurePath(path).parent}"
            )
        else:
            text = str(meta.get("text") or "")[:300]
            what = f"An iMessage arrived. Text: {text!r}"
        return f"Event kind: {kind}\n{what}\nFollow-up to automate: {follow_up}"

    def observe(self, event: ContextEvent) -> Optional[asyncio.Task[None]]:
        """Schedule a suggestion for qualifying events; returns the task."""
        if event.event_type == "pattern_detected":
            return None
        kind = self.classify(event)
        if kind is None or self._cooling(kind):
            return None
        # Cool down per kind before the model call so a burst of PDFs
        # produces one suggestion, not one per file.
        self._start_cooldown(kind)
        return asyncio.create_task(
            self._suggest(self.describe(event, kind), kind), name="next-step"
        )


class ChatGapDetector(_CooldownSource):
    """Suggest a tool when the agent could not do what the user asked."""

    source_name = "chat_gap"

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        cooldown_seconds: float = 3600.0,
        publish: Optional[Publish] = None,
    ) -> None:
        super().__init__(
            event_bus=event_bus, evaluate=evaluate,
            cooldown_seconds=cooldown_seconds, publish=publish,
        )

    @staticmethod
    def is_gap(reply: str) -> bool:
        return bool(_GAP_RE.search(reply or ""))

    @staticmethod
    def _key(user_text: str) -> str:
        words = re.findall(r"[a-z0-9]+", user_text.lower())
        return " ".join(sorted(set(words)))[:200]

    def observe_chat(
        self, user_text: str, reply: str, tools_called: frozenset[str] = frozenset(),
    ) -> Optional[asyncio.Task[None]]:
        """Schedule a suggestion when ``reply`` admits it could not help."""
        if not user_text.strip() or "generate_custom_tool" in tools_called:
            return None
        if not self.is_gap(reply):
            return None
        key = self._key(user_text)
        if self._cooling(key):
            return None
        self._start_cooldown(key)
        prompt = (
            f"User asked: {user_text[:500]!r}\n"
            f"Assistant replied: {reply[:500]!r}"
        )
        return asyncio.create_task(
            self._suggest(prompt, "chat_gap"), name="chat-gap"
        )
