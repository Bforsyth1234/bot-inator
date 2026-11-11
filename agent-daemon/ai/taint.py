"""Injection shield: mark turns that read outside text (todo #81).

Text the agent reads (an email, a web page, a file, an iMessage, an
invite, an MCP result) can hold instructions. This module keeps, per turn,
a deterministic record of what outside text was read, so the orchestrator
can:

1. take away the no-card bypass from tools that write state
   (``remember_preference``, MCP tools the user marked "runs without
   asking") in a marked turn: :meth:`TurnTaint.requires_card`;
2. say on the card where each target-naming argument came from ("from
   you", "from the email", or written by the assistant):
   :meth:`TurnTaint.notes_for`.

Rules (todo #81 DECISIONS, binding):

* The mark is per turn and deterministic. A turn is marked when its event
  carries outside text (:data:`OUTSIDE_TEXT_EVENTS`, or a
  ``pattern_detected`` idea triggered by one, :func:`is_outside_trigger`)
  or when a tool that returns outside text ran in it
  (:data:`OUTSIDE_TEXT_TOOLS`, or any MCP tool). No model decides this.
* This never removes a card and never adds a bypass: ``requires_card`` can
  only turn a no-card tool into a carded one.
* The text kept for matching is a bounded copy, in memory only, dropped at
  the next :meth:`TurnTaint.begin_turn`. It is never logged, never
  serialised, and never part of ``repr``.

Wiring (the runner's): one :class:`TurnTaint` on the orchestrator,
``begin_turn`` where ``_turn_tool_calls`` / ``_turn_done`` are reset,
``mark_read`` after a tool returns in ``gated_forward``, ``requires_card``
on the bypass test, and ``notes_for`` on the approval request.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

# Events whose own metadata is outside text (decision 1a).
OUTSIDE_TEXT_EVENTS: frozenset[str] = frozenset({"imessage_received", "mail_received"})

# Built-in tools that return outside text (decision 1b), with the kind of
# text each returns. Every MCP tool marks too (#69 decision 5); callers
# say so with ``is_mcp=True``. ``read_front_window`` is #89's.
OUTSIDE_TEXT_TOOLS: Mapping[str, str] = {
    "read_email": "email",
    "read_active_tab_text": "web_page",
    "summarize_file": "file",
    "read_clipboard": "clipboard",
    "list_events": "calendar",
    "read_front_window": "window",
}

# Read-only tools that WRITE state: in a marked turn they get a card
# (decision 2). User-marked MCP tools are added per call.
WRITING_READ_ONLY_TOOLS: frozenset[str] = frozenset({"remember_preference"})

# ``pattern_detected`` triggers that come from an iMessage's text
# (``idea_sources._MESSAGE_KINDS``). Every ``mail_*`` trigger comes from a
# received email (invite, shipping, deadline, needs_reply, waiting_on).
MESSAGE_TRIGGERS: frozenset[str] = frozenset({"tracking_number", "plan_with_time"})
# Triggers built from calendar text someone else may have written (an
# invite's title, location, notes or video link): the look-ahead's
# ``lookahead_*`` offers (#77) and the meeting-link follow-up (runner, wave 15).
CALENDAR_TRIGGERS: frozenset[str] = frozenset({"meeting_with_video_link"})
CALENDAR_TRIGGER_PREFIX = "lookahead_"
# An idea's ``source_event_type`` (e.g. a #88 watch on mail) -> the kind of
# outside text it was built from.
SOURCE_EVENT_KINDS: Mapping[str, str] = {
    "mail_received": "email",
    "imessage_received": "message",
    "event_starting_soon": "calendar",
}

# How the card names each kind of outside text: "from <phrase>".
KIND_PHRASES: Mapping[str, str] = {
    "email": "the email",
    "message": "the message",
    "web_page": "the web page",
    "file": "the file",
    "clipboard": "the clipboard",
    "calendar": "the calendar",
    "window": "the window",
    "mcp": "an MCP tool",
    "selection": "the selected text",
}

FROM_YOU = "from you"
FROM_ASSISTANT = "written by the assistant"

# Argument names that name a target (decision 3), for any tool.
TARGET_ARGS: frozenset[str] = frozenset({
    "to", "cc", "bcc", "recipient", "recipients", "target_number", "phone",
    "number", "address", "email", "url", "link", "path", "file", "src",
    "dst", "dst_dir", "destination", "new_name", "shortcut", "tool_name",
})
# Extra target arguments of particular tools.
TOOL_TARGET_ARGS: Mapping[str, frozenset[str]] = {
    "run_shortcut": frozenset({"name"}),
    "append_note": frozenset({"title"}),
    "create_note": frozenset({"folder"}),
    "create_reminder": frozenset({"list_name"}),
    "create_event": frozenset({"calendar", "location"}),
    # Not a target, but a verbatim copy of the email's words is exactly
    # what an injected "remember that ..." looks like.
    "remember_preference": frozenset({"preference"}),
}
# Arguments that may hold several addresses ("a@x.com, b@y.com").
LIST_ARGS: frozenset[str] = frozenset({"to", "cc", "bcc", "recipients"})

# Bounds on what is kept for matching.
MAX_TEXT_CHARS = 20_000  # per read
MAX_READS = 16  # per turn; later reads still mark, their text is not kept
MAX_LABEL_CHARS = 80
_MAX_DEPTH = 6

_WS_RE = re.compile(r"\s+")
_SPLIT_RE = re.compile(r"[,;]")
_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{5,}\d")


def is_outside_trigger(trigger: Any) -> Optional[str]:
    """The kind of outside text a ``pattern_detected`` trigger came from,
    else ``None``."""
    t = str(trigger or "")
    if t.startswith("mail_"):
        return "email"
    if t in MESSAGE_TRIGGERS:
        return "message"
    if t in CALENDAR_TRIGGERS or t.startswith(CALENDAR_TRIGGER_PREFIX):
        return "calendar"
    return None


def marks_turn(tool_name: str, *, is_mcp: bool = False) -> bool:
    """True when running ``tool_name`` marks the turn."""
    return is_mcp or tool_name in OUTSIDE_TEXT_TOOLS


def _norm(text: str) -> str:
    return _WS_RE.sub(" ", str(text)).strip().casefold()


def _clean_value(value: str) -> str:
    v = _norm(value)
    if v.startswith("mailto:"):
        v = v[len("mailto:"):]
    v = v.strip("<>\"' ")
    if len(v) > 1 and v.endswith("/"):
        v = v.rstrip("/")
    return v


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _contains(haystack: str, needle: str) -> bool:
    """``needle`` occurs in ``haystack`` as a whole token: not glued to a
    letter, digit or address character (``bob.x@a.com`` does not contain
    ``x@a.com``; a sentence-ending ``.`` does not glue)."""
    if not needle:
        return False
    start = 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return False
        end = i + len(needle)
        before = haystack[i - 1] if i > 0 else " "
        after = haystack[end] if end < len(haystack) else " "
        after2 = haystack[end + 1] if end + 1 < len(haystack) else " "
        glued_before = needle[0].isalnum() and (before.isalnum() or before in "._-+@/")
        glued_after = needle[-1].isalnum() and (
            after.isalnum() or (after in "._-@/" and after2.isalnum())
        )
        if not glued_before and not glued_after:
            return True
        start = i + 1


_PHONE_VALUE_RE = re.compile(r"^\+?[\d\s().\-]+$")


def _phone_in(haystack: str, value: str) -> bool:
    """A phone number matches however it is punctuated (last 10 digits)."""
    if not _PHONE_VALUE_RE.match(value.strip()):
        return False
    want = _digits(value)
    if len(want) < 7:
        return False
    tail = want[-10:]
    return any(_digits(m.group(0)).endswith(tail) for m in _PHONE_RE.finditer(haystack))


def _found(haystack: str, value: str) -> bool:
    v = _clean_value(value)
    if not v:
        return False
    if _contains(haystack, v):
        return True
    return _phone_in(haystack, value)


def _text_of(value: Any, depth: int = 0) -> str:
    """Every string leaf of a tool result, one per line (keys skipped)."""
    if depth > _MAX_DEPTH or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if isinstance(value, Mapping):
        return "\n".join(t for t in (_text_of(v, depth + 1) for v in value.values()) if t)
    if isinstance(value, (list, tuple, set, frozenset)):
        return "\n".join(t for t in (_text_of(v, depth + 1) for v in value) if t)
    if isinstance(value, (int, float, bool)):
        return ""
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _short(text: Any) -> str:
    t = _WS_RE.sub(" ", str(text or "")).strip()
    return t if len(t) <= MAX_LABEL_CHARS else t[: MAX_LABEL_CHARS - 1] + "…"


@dataclass
class _Read:
    kind: str
    label: str
    text: str  # normalised, bounded; never logged

    def __repr__(self) -> str:  # never show the text
        return f"_Read(kind={self.kind!r}, label={self.label!r}, chars={len(self.text)})"


class TurnTaint:
    """What outside text the current turn read. Thread-safe: tools run in
    worker threads while the turn is reset on the event loop."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._reads: list[_Read] = []
        self._marked = False
        self._kind = ""
        self._user_text = ""

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"TurnTaint(kind={self._kind!r}, marked={self._marked}, "
                f"sources={self.sources()!r})"
            )

    # ---- turn lifecycle ---------------------------------------------------

    def begin_turn(
        self,
        kind: str = "",
        *,
        user_text: str = "",
        event_type: str = "",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Drop the last turn's mark and text; start this one. An event
        that carries outside text marks the turn here."""
        with self._lock:
            self._reads = []
            self._marked = False
            self._kind = kind
            self._user_text = _norm(user_text)[: MAX_TEXT_CHARS] if user_text else ""
            if event_type:
                self.mark_event(event_type, metadata)

    def reset(self) -> None:
        self.begin_turn()

    def mark_event(self, event_type: str, metadata: Optional[Mapping[str, Any]] = None) -> bool:
        """Mark the turn when ``event_type`` carries outside text. Returns
        whether it marked."""
        meta = metadata or {}
        sender = _short(meta.get("sender"))
        if event_type == "imessage_received":
            label = f"iMessage from {sender}" if sender else "an iMessage"
            self._add("message", label, [meta.get("text"), meta.get("sender")])
            return True
        if event_type == "mail_received":
            label = f"email from {sender}" if sender else "an email"
            self._add("email", label, [meta.get("subject"), meta.get("sender")])
            return True
        if event_type == "pattern_detected":
            kind = is_outside_trigger(meta.get("trigger"))
            if kind is None:
                kind = SOURCE_EVENT_KINDS.get(str(meta.get("source_event_type") or ""))
            if kind is None:
                return False
            noun = {"email": "email", "calendar": "calendar event"}.get(kind, "message")
            label = f"the {noun} behind this idea"
            parts = [meta.get("event_details"), meta.get("sender")]
            if kind == "calendar":
                parts.append(meta.get("arguments"))
            self._add(kind, label, parts)
            return True
        return False

    def mark_context(self, context: Optional[Mapping[str, Any]]) -> bool:
        """A quick-ask turn (#74) that carries selected or clipboard text:
        that text is someone else's words, not the user's request, so it
        marks the turn like any other read. Returns whether it marked."""
        ctx = context if isinstance(context, Mapping) else {}
        selection = ctx.get("selection")
        if not isinstance(selection, str) or not selection.strip():
            return False
        if ctx.get("selection_source") == "clipboard":
            self._add("clipboard", "text on the clipboard", [selection])
        else:
            self._add("selection", "the selected text", [selection])
        return True

    def mark_read(
        self,
        tool_name: str,
        result: Any,
        *,
        is_mcp: bool = False,
        server: Optional[str] = None,
    ) -> bool:
        """After a tool ran: mark the turn when it returns outside text and
        keep a bounded copy of the text. Returns whether it marked."""
        if not marks_turn(tool_name, is_mcp=is_mcp):
            return False
        if is_mcp:
            kind = "mcp"
            label = f"MCP tool {tool_name}" + (f" ({_short(server)})" if server else "")
        else:
            kind = OUTSIDE_TEXT_TOOLS[tool_name]
            label = KIND_PHRASES.get(kind, kind)
            sender = result.get("sender") if isinstance(result, Mapping) else None
            if tool_name == "read_email" and sender:
                label = f"email from {_short(sender)}"
            elif tool_name == "summarize_file" and isinstance(result, Mapping) and result.get("path"):
                label = f"file {_short(result.get('path'))}"
            elif tool_name == "read_active_tab_text" and isinstance(result, Mapping) and result.get("url"):
                label = f"web page {_short(result.get('url'))}"
        self._add(kind, label, [result])
        return True

    def _add(self, kind: str, label: str, parts: Iterable[Any]) -> None:
        text = _norm("\n".join(t for t in (_text_of(p) for p in parts) if t))
        with self._lock:
            self._marked = True
            if len(self._reads) < MAX_READS:
                self._reads.append(_Read(kind, label, text[:MAX_TEXT_CHARS]))

    # ---- queries ------------------------------------------------------------

    @property
    def kind(self) -> str:
        return self._kind

    def is_tainted(self) -> bool:
        with self._lock:
            return self._marked

    def sources(self) -> list[str]:
        """Labels of what was read this turn, in order, without repeats."""
        with self._lock:
            out: list[str] = []
            for r in self._reads:
                if r.label not in out:
                    out.append(r.label)
            return out

    def requires_card(self, tool_name: str, *, user_marked_mcp: bool = False) -> bool:
        """True when a tool that would run without a card must raise one:
        the turn is marked and the tool writes state (decision 2). Never
        False for a tool that has a card anyway; callers only use it to
        take a bypass away."""
        if not self.is_tainted():
            return False
        return user_marked_mcp or tool_name in WRITING_READ_ONLY_TOOLS

    def origin_of(self, value: Any) -> str:
        """``"from you"`` when the user typed ``value`` this turn, else
        ``"from <the outside text>"`` when it was read this turn, else
        ``"written by the assistant"``. Deterministic; no model call."""
        v = str(value if value is not None else "")
        with self._lock:
            if self._user_text and _found(self._user_text, v):
                return FROM_YOU
            for r in self._reads:
                if r.text and _found(r.text, v):
                    return "from " + KIND_PHRASES.get(r.kind, "what it read")
        return FROM_ASSISTANT

    def notes_for(
        self,
        tool_name: str,
        args: Sequence[Any] = (),
        kwargs: Optional[Mapping[str, Any]] = None,
        param_names: Optional[Sequence[str]] = None,
    ) -> list[str]:
        """The card's origin lines (decision 3). Empty in an unmarked turn,
        so unmarked cards are unchanged. In a marked turn: one line naming
        what was read, then one line per target-naming argument."""
        if not self.is_tainted():
            return []
        named: dict[str, Any] = {}
        for i, value in enumerate(args or ()):
            if param_names and i < len(param_names):
                named[param_names[i]] = value
        named.update({k: v for k, v in (kwargs or {}).items() if v is not None})
        targets = TARGET_ARGS | TOOL_TARGET_ARGS.get(tool_name, frozenset())
        lines: list[str] = []
        sources = self.sources()
        if sources:
            lines.append("Read this turn: " + "; ".join(sources))
        for arg, value in named.items():
            if arg not in targets or not isinstance(value, (str, int, float)):
                continue
            text = str(value).strip()
            if not text:
                continue
            parts = [p.strip() for p in _SPLIT_RE.split(text)] if arg in LIST_ARGS else [text]
            parts = [p for p in parts if p]
            for part in parts:
                head = f"{arg} {_short(part)}" if len(parts) > 1 else arg
                lines.append(f"{head}: {_say(self.origin_of(part))}")
        return lines


def _say(origin: str) -> str:
    return origin if origin == FROM_YOU else f"{origin}, not from you"
