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

Some follow-ups need no new tool: a built-in tool (``create_event``,
``create_reminder``, ``open_url``) does the job directly. For those kinds
(:data:`BUILTIN_FOLLOW_UPS`) no model is called; the published idea carries
``metadata.builtin_tool`` and ``metadata.event_details`` so the agent calls
the built-in (which asks for approval) instead of generating a tool.

Cooldowns survive restarts through :class:`IdeaState`
(``~/.bot-inator/idea_state.json``), and ideas resembling one the user
declined are dropped via the shared ``IdeaFeedback`` store (todo #32).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path, PurePath
from typing import Any, Awaitable, Callable, Optional, Protocol

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
        "Read the .ics file and add the event with the built-in create_event "
        "tool (asks for approval), then move the file to the Trash."
    ),
    "tracking_number": (
        "Detect the carrier from the tracking number format and open its "
        "tracking page for that number."
    ),
    "plan_with_time": (
        "Add the plan with the built-in create_event tool (title, start and "
        "end parsed from the text); it asks the user for approval."
    ),
    "meeting_with_video_link": (
        "Open the meeting's video link (open_url) a minute before it starts."
    ),
    "mail_invite": (
        "Read the email with the built-in read_email tool, then add the "
        "meeting with the built-in create_event tool (title, start, end and "
        "location from the invite); it asks the user for approval."
    ),
    "mail_shipping": (
        "Read the email with the built-in read_email tool to find the "
        "tracking number or tracking link, then open the carrier's tracking "
        "page with the built-in open_url tool (asks for approval)."
    ),
    "mail_deadline": (
        "Read the email with the built-in read_email tool, then add a "
        "reminder with the built-in create_reminder tool (title from the "
        "request, due from the deadline); it asks the user for approval."
    ),
}

# Kinds a built-in tool handles directly: kind -> (idea name, built-in tool).
# The idea name is what the user's verdict is recorded under, so declining
# one of these suppresses the kind (see IdeaFeedback.resembles_rejected).
BUILTIN_FOLLOW_UPS: dict[str, tuple[str, str]] = {
    "calendar_invite_downloaded": ("add_ics_invite_to_calendar", "create_event"),
    "plan_with_time": ("add_message_plan_to_calendar", "create_event"),
    "meeting_with_video_link": ("open_meeting_video_link", "open_url"),
    "mail_invite": ("add_email_invite_to_calendar", "create_event"),
    "mail_shipping": ("open_email_tracking_page", "open_url"),
    "mail_deadline": ("remind_email_deadline", "create_reminder"),
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

# Mail is classified from headers only (the watcher never reads bodies,
# so an ``.ics`` attachment is invisible): subject words decide. Each kind
# is a positive pattern plus a veto, and the first kind that matches wins.
_DAY = (
    r"(?:today|tonight|tomorrow|eod|eow|end of (?:the )?(?:day|week|month)"
    r"|(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.? \d{1,2}(?:st|nd|rd|th)?"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
)
_MAIL_KINDS: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    ("mail_invite", re.compile(
        r"^(?:(?:updated|new) )?(?:invitation|invite)\s*:"
        r"|\bmeeting (?:request|invitation|invite)\b"
        r"|\b(?:invitation|invite)\b.{0,80}(?:@|\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b)",
        re.IGNORECASE,
    ), re.compile(
        r"^(?:accepted|declined|tentative(?:ly accepted)?|cancell?ed( event)?)\s*:"
        r"|\b(?:workspace|team on|collaborate|invite code|referral|invite (?:your )?friends"
        r"|join (?:me on|us on)|slack|github|figma|linkedin|webinar)\b",
        re.IGNORECASE,
    )),
    ("mail_shipping", re.compile(
        r"^shipped\s*:"
        r"|\b(?:has|have|was|been) shipped\b|\bout for delivery\b"
        r"|\btracking (?:number|info(?:rmation)?|details)\b|\bshipment\b"
        r"|\b(?:order|package|parcel)\b.{0,40}\bon (?:its|the) way\b",
        re.IGNORECASE,
    ), re.compile(
        r"\b(?:free shipping|ships free|\d+% off|sale|deal|coupon|promo|newsletter)\b",
        re.IGNORECASE,
    )),
    ("mail_deadline", re.compile(
        r"\b(?:please|action required|reminder|request|submit|sign|review|"
        r"complete|respond|rsvp|approve|send|pay|due)\b"
        r".{0,80}\b(?:by|due|before|deadline:?)\s+(?:on |this |next )?" + _DAY + r"\b"
        r"|\bdue\s+(?:on |this |next )?" + _DAY + r"\b",
        re.IGNORECASE,
    ), re.compile(
        r"\b(?:sale|offer|deal|discount|\d+% off|last chance|expires?|ends|"
        r"newsletter|unsubscribe|webinar|free)\b",
        re.IGNORECASE,
    )),
)
_SKIP_MAILBOXES = re.compile(r"junk|spam|trash|deleted|sent|draft|outbox", re.IGNORECASE)
_VIDEO_URL_RE = re.compile(r"^https://\S+$", re.IGNORECASE)


class FeedbackStore(Protocol):
    """The part of ``ai.open_help.IdeaFeedback`` the sources consult."""

    def resembles_rejected(self, tool_name: str, description: str) -> bool: ...


class IdeaState:
    """Idea-source cooldowns persisted as JSON (todo #32).

    One instance is shared by every source (two instances on one file
    would overwrite each other). Each source keeps a section of
    ``key -> expiry`` (epoch seconds). Expired entries are pruned on load
    and on every save. ``path=None`` keeps state in memory only (tests).
    """

    VERSION = 1
    MAX_PER_SECTION = 1000

    def __init__(self, path: Optional[Path] = None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self.sections: dict[str, dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            raw = data.get("cooldowns", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            logger.warning("idea state at %s unreadable; starting empty", self.path)
            return
        for name, entries in (raw.items() if isinstance(raw, dict) else ()):
            if isinstance(entries, dict):
                self.sections[str(name)] = {
                    str(k): float(v) for k, v in entries.items()
                    if isinstance(v, (int, float))
                }
        self._prune()

    def _prune(self) -> None:
        now = self._clock()
        for name, entries in self.sections.items():
            live = sorted(((v, k) for k, v in entries.items() if v > now), reverse=True)
            entries.clear()
            entries.update((k, v) for v, k in live[: self.MAX_PER_SECTION])

    def _save(self) -> None:
        self._prune()
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"version": self.VERSION, "cooldowns": self.sections}, indent=1,
            ))
            tmp.replace(self.path)
        except OSError:
            logger.exception("failed to save idea state to %s", self.path)

    def active(self, section: str, key: str) -> bool:
        return self._clock() < self.sections.get(section, {}).get(key, 0.0)

    def start(self, section: str, key: str, seconds: float) -> None:
        self.sections.setdefault(section, {})[key] = self._clock() + seconds
        self._save()


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
    """Shared plumbing: persisted per-key cooldowns, feedback, publishing."""

    source_name = "unknown"

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        cooldown_seconds: float,
        publish: Optional[Publish] = None,
        state: Optional[IdeaState] = None,
        feedback: Optional[FeedbackStore] = None,
    ) -> None:
        self.event_bus = event_bus
        self._evaluate = evaluate
        self.cooldown_seconds = cooldown_seconds
        self._publish = publish or self.event_bus.push
        self.state = state if state is not None else IdeaState()
        # Shared IdeaFeedback store; only consulted here; the source that
        # records verdicts is open-help (or friction), never these.
        self.feedback = feedback

    def _cooling(self, key: str) -> bool:
        return self.state.active(self.source_name, key)

    def _start_cooldown(self, key: str) -> None:
        self.state.start(self.source_name, key, self.cooldown_seconds)

    def _rejected(self, idea: dict[str, Any]) -> bool:
        if self.feedback is None:
            return False
        name = str(idea.get("tool_name") or "")
        try:
            hit = self.feedback.resembles_rejected(name, str(idea.get("description") or ""))
        except Exception:
            logger.exception("%s feedback lookup failed", self.source_name)
            return False
        if hit:
            logger.info("%s dropped %r: resembles a declined idea", self.source_name, name)
        return hit

    async def _publish_idea(self, idea: dict[str, Any]) -> None:
        try:
            await self._publish(ContextEvent(event_type="pattern_detected", metadata=idea))
        except Exception:
            logger.exception("%s failed to publish suggestion", self.source_name)

    async def _suggest(self, user_text: str, trigger: str) -> None:
        try:
            raw = await self._evaluate(user_text)
        except Exception:
            logger.exception("%s evaluate failed", self.source_name)
            return
        suggestion = parse_suggestion(raw)
        if suggestion is None or self._rejected(suggestion):
            return
        suggestion["source"] = self.source_name
        suggestion["trigger"] = trigger
        await self._publish_idea(suggestion)


class NextStepSuggester(_CooldownSource):
    """Suggest a tool (or a built-in action) for an event with an obvious follow-up."""

    source_name = "next_step"

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evaluate: Evaluate,
        cooldown_seconds: float = 24 * 3600.0,
        publish: Optional[Publish] = None,
        state: Optional[IdeaState] = None,
        feedback: Optional[FeedbackStore] = None,
    ) -> None:
        super().__init__(
            event_bus=event_bus, evaluate=evaluate,
            cooldown_seconds=cooldown_seconds, publish=publish,
            state=state, feedback=feedback,
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
        if event.event_type == "event_starting_soon":
            if _VIDEO_URL_RE.match(str(meta.get("video_url") or "").strip()):
                return "meeting_with_video_link"
            return None
        if event.event_type == "mail_received":
            if _SKIP_MAILBOXES.search(str(meta.get("mailbox") or "")):
                return None
            subject = " ".join(str(meta.get("subject") or "").split())
            if not subject:
                return None
            for kind, pattern, veto in _MAIL_KINDS:
                if pattern.search(subject):
                    return None if veto.search(subject) else kind
        return None

    @staticmethod
    def details(event: ContextEvent) -> str:
        """The event's facts, as the agent needs them to act."""
        meta = event.metadata or {}
        if event.event_type == "file":
            path = str(meta.get("path") or "")
            return (
                f"A file appeared in a watched folder: {PurePath(path).name}\n"
                f"Path: {path}"
            )
        if event.event_type == "mail_received":
            return (
                "An email arrived.\n"
                f"From: {meta.get('sender', '')}\n"
                f"Subject: {str(meta.get('subject') or '')[:200]}\n"
                f"Date: {meta.get('date', '')}\n"
                f"message_id: {meta.get('message_id', '')}"
            )
        if event.event_type == "event_starting_soon":
            return (
                "A calendar event is about to start.\n"
                f"Title: {meta.get('title', '')}\n"
                f"Start: {meta.get('start', '')}\n"
                f"Video link: {meta.get('video_url', '')}"
            )
        text = str(meta.get("text") or "")[:300]
        return f"An iMessage arrived. Text: {text!r}"

    @classmethod
    def describe(cls, event: ContextEvent, kind: str) -> str:
        follow_up = FOLLOW_UPS.get(kind, "")
        return f"Event kind: {kind}\n{cls.details(event)}\nFollow-up to automate: {follow_up}"

    @staticmethod
    def _instance_key(event: ContextEvent, kind: str) -> str:
        """Cooldown key for a built-in kind: one per concrete event."""
        meta = event.metadata or {}
        if event.event_type == "mail_received":
            ident = str(meta.get("message_id") or meta.get("subject") or "")
        elif event.event_type == "event_starting_soon":
            ident = f"{meta.get('title', '')}|{meta.get('start', '')}"
        else:
            ident = str(meta.get("path") or meta.get("text") or "")
        digest = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]
        return f"{kind}:{digest}"

    @classmethod
    def builtin_idea(cls, event: ContextEvent, kind: str) -> dict[str, Any]:
        """The ``pattern_detected`` metadata for a built-in follow-up."""
        name, tool = BUILTIN_FOLLOW_UPS[kind]
        follow_up = FOLLOW_UPS[kind]
        details = cls.details(event)
        return {
            "tool_name": name,
            "description": follow_up,
            "expected_logic": f"{follow_up}\n{details}",
            "builtin_tool": tool,
            "event_details": details,
            "source": cls.source_name,
            "trigger": kind,
        }

    def observe(self, event: ContextEvent) -> Optional[asyncio.Task[None]]:
        """Schedule a suggestion for qualifying events; returns the task."""
        if event.event_type == "pattern_detected":
            return None
        kind = self.classify(event)
        if kind is None:
            return None
        if kind in BUILTIN_FOLLOW_UPS:
            # A specific action on a specific event: cool down per event,
            # not per kind, so a second invite the same day is not lost.
            key = self._instance_key(event, kind)
            if self._cooling(key):
                return None
            idea = self.builtin_idea(event, kind)
            if self._rejected(idea):
                return None
            self._start_cooldown(key)
            return asyncio.create_task(self._publish_idea(idea), name="next-step")
        if self._cooling(kind):
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
        state: Optional[IdeaState] = None,
        feedback: Optional[FeedbackStore] = None,
    ) -> None:
        super().__init__(
            event_bus=event_bus, evaluate=evaluate,
            cooldown_seconds=cooldown_seconds, publish=publish,
            state=state, feedback=feedback,
        )

    @staticmethod
    def is_gap(reply: str) -> bool:
        return bool(_GAP_RE.search(reply or ""))

    @staticmethod
    def _key(user_text: str) -> str:
        # Hashed: the key is persisted, and the chat text need not be.
        words = re.findall(r"[a-z0-9]+", user_text.lower())
        joined = " ".join(sorted(set(words)))[:200]
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

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
