"""Keep personal text out of cloud-engine prompts (todo #31).

When the engine that will read a prompt runs on a cloud provider (Groq),
personal text is replaced by :data:`PLACEHOLDER` unless the user opted in
with the Settings toggle or ``AGENT_CLOUD_PERSONAL_TEXT=1`` (invariant 7c,
#52; see :func:`personal_text_allowed`).

"Personal text" here means words written by or to someone else that the
user never typed to the assistant:

* iMessage text (``imessage_received`` ``metadata.text``),
* email subjects (``mail_received`` ``metadata.subject``),
* email bodies and subjects returned by ``read_email``,
* the same text inside built-in follow-up ``event_details`` (#32),
* calendar attendees (``event_starting_soon`` ``metadata.attendees``, #80).

Not covered (decision scope, unchanged from before #31): sender handles and
addresses (the agent needs them to act), window titles, file paths, app
names, calendar titles, and the user's own chat messages to the assistant.

``role`` is an engine role (``"main"``, ``"analysis"``, ``"drafting"``) or
``"detection"`` for the engine the idea sources use
(``AGENT_PATTERN_ENGINE``). Settings are read on every call so a test or a
settings change takes effect without a restart of this module.
"""

from __future__ import annotations

import dataclasses
import os
import re
import threading
import time
from collections import deque
from typing import Any, Optional

from events.event_bus import ContextEvent

PLACEHOLDER = "[personal text kept on this Mac]"
OPT_IN_ENV = "AGENT_CLOUD_PERSONAL_TEXT"

# event_type -> metadata keys holding personal text.
PERSONAL_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "imessage_received": ("text",),
    "mail_received": ("subject",),
    # #80: calendar attendees (names and addresses of other people). The
    # open-help source dumps event metadata into the detection prompt.
    "event_starting_soon": ("attendees",),
    # The same people (and mail with them) in the gathered prep (#71/#80),
    # and the brief's new waiting-on / look-ahead sections (#73/#77), as the
    # idea sources' detection prompts would dump them.
    "meeting_prep": ("attendees", "mail"),
    "daily_brief": ("waiting_on", "lookahead"),
}
# ``read_email`` result keys holding personal text.
PERSONAL_EMAIL_FIELDS: tuple[str, ...] = ("subject", "body")


def personal_text_allowed() -> bool:
    """True when the user opted in to cloud engines seeing personal text.

    Read on every call, so a Settings change applies without a restart
    (#52). Precedence, highest first: a shell export of ``OPT_IN_ENV``
    (the Settings toggle is then locked), the ``cloud_personal_text``
    toggle saved in the settings file, the value from ``.env``, off.
    """
    import user_settings

    if OPT_IN_ENV not in user_settings.shell_exported:
        saved = user_settings.read().get(OPT_IN_ENV)
        if saved in ("0", "1"):
            return saved == "1"
    return os.environ.get(OPT_IN_ENV, "") == "1"


SCREENSHOT_KEPT = (
    "The screenshot was not sent: the vision model is a cloud one, and "
    "screenshots (which show mail, messages and more) leave this Mac only "
    "when the user allows personal text for cloud engines in Settings (or "
    f"{OPT_IN_ENV}=1). Switch vision to the local model in Settings, or "
    "turn that on."
)


def screenshot_may_leave() -> bool:
    """True when a screenshot may go to a cloud vision model (#107 decision 3).

    A screenshot is personal text and more, so it takes the same opt-in as
    personal text (#31/#52), read live. The caller also needs a cloud
    vision model chosen in Settings; this checks only the opt-in.
    """
    return personal_text_allowed()


def role_is_cloud(role: str) -> bool:
    from config import settings

    if role == "detection":
        return settings.detection_is_cloud()
    return settings.is_cloud(role)


def should_redact(role: Optional[str]) -> bool:
    """True when prompts for ``role`` must not carry personal text."""
    if role is None or personal_text_allowed():
        return False
    return role_is_cloud(role)


def redact_text(text: Any, role: Optional[str]) -> Any:
    if not text or not should_redact(role):
        return text
    return PLACEHOLDER


def redacted_event(event: ContextEvent) -> ContextEvent:
    """A copy of ``event`` with its personal fields replaced (always)."""
    fields = PERSONAL_EVENT_FIELDS.get(event.event_type)
    meta = event.metadata or {}
    if not fields or not any(meta.get(k) for k in fields):
        return event
    new_meta = dict(meta)
    for key in fields:
        if new_meta.get(key):
            new_meta[key] = PLACEHOLDER
    return dataclasses.replace(event, metadata=new_meta)


def redact_event(event: ContextEvent, role: Optional[str]) -> ContextEvent:
    """``event`` as the ``role`` engine may see it."""
    return redacted_event(event) if should_redact(role) else event


def redact_email_result(result: Any, role: Optional[str]) -> Any:
    """A ``read_email`` result as the ``role`` engine may see it."""
    if not isinstance(result, dict) or not should_redact(role):
        return result
    out = dict(result)
    for key in PERSONAL_EMAIL_FIELDS:
        if out.get(key):
            out[key] = PLACEHOLDER
    if any(k in result for k in PERSONAL_EMAIL_FIELDS):
        out["note"] = (
            "Subject and body were kept on this Mac because this engine "
            "runs in the cloud (the user can allow sending them in "
            f"Settings, or with {OPT_IN_ENV}=1)."
        )
    return out


# ---- idea text written by a local engine (#51) -----------------------------
#
# A local detection engine sees raw iMessage text and mail subjects, and the
# idea it writes (``description``, ``expected_logic``) can quote them. When
# the main engine is a cloud one, ``scrub_idea_text`` removes those quotes
# before the pattern prompt is built. Deterministic, two rules:
#
# 1. any run of >= ``SCRUB_MIN_WORDS`` consecutive words that also appears,
#    in order, in personal text seen in the last ``RECENT_PERSONAL_SECONDS``
#    (the window the idea was drawn from) is replaced by the placeholder;
# 2. any quoted string ("...", “...”, '...') of >= ``SCRUB_MIN_WORDS``
#    words is replaced too, since a quote that long is likely someone
#    else's words even when it came from outside the remembered window.

SCRUB_MIN_WORDS = 3
RECENT_PERSONAL_SECONDS = 3600.0
_RECENT_PERSONAL_MAX = 200
_recent_personal: "deque[tuple[float, str]]" = deque(maxlen=_RECENT_PERSONAL_MAX)
_recent_lock = threading.Lock()

_WORD_RE = re.compile(r"[\w][\w'’]*")
_QUOTED_RE = re.compile(
    r'"[^"\n]{3,400}"|“[^”\n]{3,400}”|(?<!\w)\'[^\'\n]{3,400}\'(?!\w)'
)


def note_personal_event(event: ContextEvent, now: Optional[float] = None) -> None:
    """Remember the personal text of ``event`` for :func:`scrub_idea_text`."""
    fields = PERSONAL_EVENT_FIELDS.get(event.event_type)
    if not fields:
        return
    meta = event.metadata or {}
    stamp = time.time() if now is None else now
    with _recent_lock:
        for key in fields:
            text = meta.get(key)
            if isinstance(text, str) and text.strip() and text != PLACEHOLDER:
                _recent_personal.append((stamp, text))


def recent_personal_texts(now: Optional[float] = None) -> list[str]:
    cutoff = (time.time() if now is None else now) - RECENT_PERSONAL_SECONDS
    with _recent_lock:
        return [text for stamp, text in _recent_personal if stamp >= cutoff]


def clear_recent_personal() -> None:
    """Forget remembered personal text (tests)."""
    with _recent_lock:
        _recent_personal.clear()


def _words(text: str) -> list[tuple[str, int, int]]:
    """Lower-cased words with their spans. A trailing apostrophe is not
    part of the word: otherwise the last word of a single-quoted quote
    (``'… with Sam'`` → ``sam'``) never matches the personal text (#68)."""
    out: list[tuple[str, int, int]] = []
    for m in _WORD_RE.finditer(text):
        word = m.group(0).rstrip("'’")
        out.append((word.lower(), m.start(), m.start() + len(word)))
    return out


def _personal_ngrams(personal: list[str], n: int) -> set[tuple[str, ...]]:
    grams: set[tuple[str, ...]] = set()
    for text in personal:
        words = [w for w, _, _ in _words(text)]
        for i in range(len(words) - n + 1):
            grams.add(tuple(words[i:i + n]))
    return grams


def scrub_personal_text(text: str, personal: list[str]) -> str:
    """``text`` with quotes of ``personal`` and long quoted strings replaced."""
    if not text:
        return text
    n = SCRUB_MIN_WORDS
    grams = _personal_ngrams(personal, n)
    tokens = _words(text)
    covered = [False] * len(tokens)
    for i in range(len(tokens) - n + 1):
        if tuple(w for w, _, _ in tokens[i:i + n]) in grams:
            for j in range(i, i + n):
                covered[j] = True
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(tokens):
        if not covered[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(tokens) and covered[j + 1]:
            j += 1
        spans.append((tokens[i][1], tokens[j][2]))
        i = j + 1
    for start, end in reversed(spans):
        text = text[:start] + PLACEHOLDER + text[end:]

    def quoted(m: "re.Match[str]") -> str:
        inner = m.group(0)[1:-1]
        if PLACEHOLDER in inner or len(_WORD_RE.findall(inner)) < n:
            return m.group(0)
        return m.group(0)[0] + PLACEHOLDER + m.group(0)[-1]

    return _QUOTED_RE.sub(quoted, text)


def scrub_idea_text(text: Any, role: Optional[str] = "main") -> Any:
    """Idea text as the ``role`` engine may see it (#51)."""
    if not isinstance(text, str) or not text or not should_redact(role):
        return text
    return scrub_personal_text(text, recent_personal_texts())
