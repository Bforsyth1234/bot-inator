"""Meeting follow-through: local notes, action items as one card (#86).

Flow (the menu bar app records; the daemon never can):

1. ``event_starting_soon`` (calendar watcher, #80 shape with ``id``, ``end``,
   ``attendees``) reaches :meth:`MeetingNotes.observe`. When notes are on
   (``AGENT_MEETING_NOTES``, default OFF) and the meeting qualifies (the
   #71 rules: not all-day, other people expected), a timer sends the app one
   ``meeting_record_offer`` frame at the meeting-prep moment. It is an OFFER:
   the app shows "Take notes for <title>?" and starts recording only when
   the user clicks (there is no server→client "start" frame at all). The
   app's menu item can also start a recording with no offer.
2. The app reports ``meeting_record_state`` (``idle`` on connect, then
   ``started`` / ``stopped`` / ``failed`` / ``declined``, with its
   Microphone and Speech Recognition authorization, which feed the #59
   Permissions rows). On ``started`` the daemon arms a safety timer that
   sends ``meeting_record_stop`` at the event's end + :data:`STOP_GRACE`
   (5 minutes); the app enforces the same ``stop_at`` on its own.
3. The app sends ``meeting_audio_ready`` with either ``transcript_path``
   (it transcribed on the device, SFSpeechRecognizer with
   ``requiresOnDeviceRecognition``; the default, engine ``apple``) or
   ``audio_path`` (engine ``whisper``: :func:`transcribe_audio` runs
   ``mlx_whisper`` here, when installed). Audio is deleted once the
   transcript exists (:func:`delete_audio`; only files inside
   :func:`audio_dir`, never a path a frame names elsewhere). #108: when
   Whisper fails here, the audio is KEPT (same folder, 0600) with the app's
   #105 ``<id>.failed.json`` marker (:func:`keep_failed`), so the app's
   24-hour purge applies, and the app is told with a ``meeting_record_stop``
   frame carrying ``transcription: "failed"``; the panel's Retry sends the
   same ``meeting_audio_ready`` again. A later success (``transcription:
   "done"``) deletes the audio and the marker and publishes the one card.
   Any other failure (an ``error`` from the app, a bad id) still deletes.
4. :meth:`MeetingNotes.process` stores the transcript (0600, in
   :func:`notes_dir`), extracts decisions, action items and a follow-up
   with local rules (:func:`extract`), optionally words a short summary with
   a LOCAL engine (never a cloud one, whatever ``AGENT_PROVIDER`` is and
   whatever the personal-text opt-in says), and publishes ONE
   ``pattern_detected`` idea carrying a #76 prepared offer: the decisions as
   text, then tick-box actions (``create_reminder`` per action item that is
   the user's, ``create_event`` for a follow-up, ``draft_email`` to the
   attendees, ``create_note`` with the notes). Nothing runs before the card
   is approved (7a); the email is a draft only (#63).

Owner of an action item (no speaker identification in v1): a known name
said in the sentence ("Brooks will send ...", "Alice, can you ..."), or the
first person ("I'll ...") on the user's own channel: the app labels
microphone lines ``[me]`` and system-audio lines ``[others]``; unlabelled
lines (a single mixed stream) count as the user's for "I'll".
"""

from __future__ import annotations

import asyncio
import calendar as _calendar
import concurrent.futures
import hashlib
import json
import logging
import os
import pwd
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

Evaluate = Callable[[str], Awaitable[str]]
Sender = Callable[[str, dict[str, Any]], Awaitable[None]]

TRIGGER_EVENT_TYPE = "event_starting_soon"
ENABLED_KEY = "AGENT_MEETING_NOTES"
ENGINE_KEY = "AGENT_MEETING_NOTES_ENGINE"
ENGINES: tuple[str, ...] = ("apple", "whisper")
DEFAULT_ENGINE = "apple"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"

STOP_GRACE = timedelta(minutes=5)
MANUAL_MAX = timedelta(hours=2)
SOURCE_NAME = "meeting_notes"
IDEA_NAME = "meeting_follow_through"
OFFER_KIND = "meeting_notes"

FRAME_OFFER = "meeting_record_offer"
FRAME_STOP = "meeting_record_stop"
STATES: tuple[str, ...] = ("idle", "started", "stopped", "failed", "declined")
AUTH_STATUSES: tuple[str, ...] = ("not_determined", "restricted", "denied", "authorized")

AUDIO_SUFFIXES = frozenset({".caf", ".m4a", ".wav", ".aac"})
MAX_ITEMS = 12
MAX_TITLE = 120
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _now() -> datetime:
    return datetime.now().astimezone()


# ---- settings (read live) -----------------------------------------------------

def _setting(key: str, default: str) -> str:
    import user_settings

    if key not in user_settings.shell_exported:
        saved = user_settings.read().get(key)
        if saved:
            return saved
    return os.environ.get(key) or default


def notes_enabled() -> bool:
    """Whether meetings get a "Take notes?" offer. Off unless turned on."""
    return _setting(ENABLED_KEY, "0").strip() == "1"


def whisper_available() -> bool:
    try:
        import mlx_whisper  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


def engine() -> str:
    """The transcription engine to use now. ``whisper`` falls back to
    ``apple`` (on-device, in the app) when ``mlx_whisper`` isn't installed."""
    chosen = _setting(ENGINE_KEY, DEFAULT_ENGINE).strip().lower()
    if chosen not in ENGINES:
        chosen = DEFAULT_ENGINE
    if chosen == "whisper" and not whisper_available():
        return "apple"
    return chosen


def settings_view() -> dict[str, Any]:
    """``GET /api/meeting-notes`` body."""
    import user_settings

    chosen = _setting(ENGINE_KEY, DEFAULT_ENGINE).strip().lower()
    return {
        "enabled": notes_enabled(),
        "engine": chosen if chosen in ENGINES else DEFAULT_ENGINE,
        "engine_in_use": engine(),
        "engines": list(ENGINES),
        "whisper_available": whisper_available(),
        "notes_dir": str(notes_dir(create=False)),
        "locked_fields": [f for f, k in (("enabled", ENABLED_KEY), ("engine", ENGINE_KEY))
                          if k in user_settings.shell_exported],
    }


def update_settings(enabled: Optional[bool] = None, engine_name: Optional[str] = None) -> dict[str, Any]:
    """``PUT /api/meeting-notes``. Raises ValueError for an unknown engine."""
    import user_settings

    updates: dict[str, str] = {}
    if enabled is not None:
        updates[ENABLED_KEY] = "1" if enabled else "0"
    if engine_name is not None:
        name = str(engine_name).strip().lower()
        if name not in ENGINES:
            raise ValueError(f"engine must be one of {', '.join(ENGINES)}")
        updates[ENGINE_KEY] = name
    if updates:
        user_settings.write(updates)
    return settings_view()


# ---- files (local, 0600) ------------------------------------------------------

def notes_dir(create: bool = True) -> Path:
    path = Path.home() / ".bot-inator" / "meetings"
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def audio_dir(create: bool = True) -> Path:
    """Where the app writes recordings (``~/.bot-inator/meetings/audio``)."""
    path = notes_dir(create) / "audio"
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def delete_audio(path: Any) -> bool:
    """Delete a recording. Only a regular audio file inside :func:`audio_dir`
    is touched; anything else a frame names is left alone (False)."""
    if not path:
        return False
    p = Path(str(path)).expanduser()
    if p.suffix.lower() not in AUDIO_SUFFIXES or p.is_symlink():
        return False
    if not _inside(p, audio_dir(create=False)):
        logger.warning("meeting notes: refused to delete %s (outside the audio folder)", p)
        return False
    try:
        p.unlink()
    except FileNotFoundError:
        return False
    return True


def write_transcript(meeting_id: str, text: str) -> Path:
    path = notes_dir() / f"{meeting_id}.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)
    return path


def read_transcript(path: Any) -> str:
    p = Path(str(path)).expanduser()
    if p.suffix.lower() != ".txt" or p.is_symlink() or not _inside(p, notes_dir(create=False)):
        raise ValueError("transcript must be a .txt file in ~/.bot-inator/meetings")
    os.chmod(p, 0o600)
    return p.read_text("utf-8", errors="replace")


class TranscriptionError(RuntimeError):
    pass


def _whisper_transcribe(path: str) -> str:
    try:
        import mlx_whisper  # type: ignore
    except Exception as exc:
        raise TranscriptionError("mlx-whisper is not installed; use the on-device engine") from exc
    out = mlx_whisper.transcribe(path, path_or_hf_repo=WHISPER_MODEL)
    return str((out or {}).get("text") or "")


def _transcribe_file(path: Any, transcriber: Optional[Callable[[str], str]] = None) -> str:
    p = Path(str(path)).expanduser()
    if p.suffix.lower() not in AUDIO_SUFFIXES or not _inside(p, audio_dir(create=False)):
        raise TranscriptionError("audio must be in ~/.bot-inator/meetings/audio")
    return (transcriber or _whisper_transcribe)(str(p))


def transcribe_audio(path: Any, transcriber: Optional[Callable[[str], str]] = None) -> str:
    """Local transcription of one recording. The audio is deleted once it is
    transcribed; on a failure it is left for the caller (#108: kept for a
    Retry by :meth:`MeetingNotes.process`)."""
    text = _transcribe_file(path, transcriber)
    delete_audio(path)
    return text


# ---- #108 a failed Whisper transcription keeps its audio ----------------------

KEEP_FAILED_FOR = timedelta(hours=24)
# The app's marker is Swift's JSONEncoder output for `FailedRecording`, whose
# Date is seconds since 2001-01-01 UTC (Foundation's reference date).
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def marker_path(meeting_id: str) -> Path:
    """The #105 marker the app's purge honours: ``<id>.failed.json``."""
    return audio_dir(create=False) / f"{meeting_id}.failed.json"


def _keepable(path: Any) -> Optional[Path]:
    if not path:
        return None
    p = Path(str(path)).expanduser()
    if (p.suffix.lower() not in AUDIO_SUFFIXES or p.is_symlink() or not p.is_file()
            or not _inside(p, audio_dir(create=False))):
        return None
    return p


def read_marker(meeting_id: str) -> Optional[dict[str, Any]]:
    if not _ID_RE.match(meeting_id or ""):
        return None
    path = marker_path(meeting_id)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("meetingId") != meeting_id:
        return None
    return data


def _marker_failed_at(data: Optional[Mapping[str, Any]]) -> Optional[datetime]:
    value = (data or {}).get("failedAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return _APPLE_EPOCH + timedelta(seconds=float(value))


def delete_marker(meeting_id: str) -> bool:
    if not _ID_RE.match(meeting_id or ""):
        return False
    path = marker_path(meeting_id)
    if not (path.is_symlink() or path.exists()):
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def keep_failed(meeting_id: str, payload: Mapping[str, Any], error: str,
                now: datetime) -> Optional[dict[str, Any]]:
    """Keep a recording whose transcription failed: its audio (only regular
    files inside :func:`audio_dir`) goes 0600 and a #105 marker is written
    (0600), with ``failedAt`` from an earlier marker so the 24 hours run from
    the first failure. None (nothing kept: delete it) when no file qualifies
    or the first failure is already 24 hours old."""
    if not _ID_RE.match(meeting_id or ""):
        return None
    mic = _keepable(payload.get("audio_path"))
    other = _keepable(payload.get("system_audio_path"))
    if mic is None and other is None:
        return None
    now = now if now.tzinfo else now.astimezone()
    failed_at = _marker_failed_at(read_marker(meeting_id)) or now
    if now - failed_at >= KEEP_FAILED_FOR:
        return None
    for p in (mic, other):
        if p is not None:
            os.chmod(p, 0o600)
    record = {
        "meetingId": meeting_id,
        "title": str(payload.get("title") or "Meeting")[:MAX_TITLE],
        "startedAt": str(payload.get("started_at") or ""),
        "endedAt": str(payload.get("ended_at") or ""),
        "stopReason": payload.get("stop_reason") or None,
        "error": error,
        "failedAt": (failed_at - _APPLE_EPOCH).total_seconds(),
        "micPath": str(mic) if mic else None,
        "systemPath": str(other) if other else None,
        "engine": "whisper",
    }
    path = marker_path(meeting_id)
    if path.is_symlink():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.chmod(path, 0o600)
    return {**record, "failed_at": failed_at, "expires_at": failed_at + KEEP_FAILED_FOR}


# ---- people -------------------------------------------------------------------

def _attendees(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    from .meeting_prep import attendees_of

    return attendees_of(dict(meta)) or []


def _system_full_name() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0].strip()
    except Exception:
        return ""


def _name_forms(name: str) -> set[str]:
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or "@" in name:
        return set()
    forms = {name.lower()}
    first = name.split(" ")[0]
    if len(first) >= 2:
        forms.add(first.lower())
    return forms


def my_names(attendees: Sequence[Mapping[str, Any]], full_name: Optional[str] = None) -> set[str]:
    """Lower-case names that mean the user: the ``is_self`` attendee and the
    account's full name, each with its first name."""
    names: set[str] = set()
    for person in attendees:
        if person.get("is_self"):
            names |= _name_forms(str(person.get("name") or ""))
    names |= _name_forms(_system_full_name() if full_name is None else full_name)
    return names


# ---- extraction (local rules) -------------------------------------------------

@dataclass
class Utterance:
    source: str  # "me" | "others" | "unknown"
    text: str


@dataclass
class ActionItem:
    task: str
    owner: str
    owner_is_user: bool
    due: str = ""
    sentence: str = ""


@dataclass
class FollowUp:
    title: str
    start: str
    end: str


@dataclass
class Summary:
    decisions: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    follow_up: Optional[FollowUp] = None
    summary: str = ""
    summary_note: str = ""

    @property
    def mine(self) -> list[ActionItem]:
        return [a for a in self.action_items if a.owner_is_user]


_TAG_RE = re.compile(r"^\s*\[(me|others|unknown)\]\s*", re.I)
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def parse_transcript(text: str) -> list[Utterance]:
    """Split a transcript into sentences, keeping each line's channel tag."""
    out: list[Utterance] = []
    for line in (text or "").splitlines():
        m = _TAG_RE.match(line)
        source = m.group(1).lower() if m else "unknown"
        body = line[m.end():] if m else line
        for sentence in _SENT_RE.split(body.strip()):
            sentence = sentence.strip()
            if sentence:
                out.append(Utterance(source, sentence))
    return out


_WEEKDAYS = {d.lower(): i for i, d in enumerate(_calendar.day_name)}
_WD = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_MON = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
_MONTH_NUM = {name: i for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_PREP = r"(?:by|before|on|until|till|due|for)\s+"
_DUE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("today", re.compile(r"\b(?:" + _PREP + r")?(?:today|tonight|end of (?:the )?day|eod)\b", re.I)),
    ("tomorrow", re.compile(r"\b(?:" + _PREP + r")?tomorrow\b", re.I)),
    ("eow", re.compile(r"\b(?:" + _PREP + r")?(?:the )?end of (?:the |this )?week\b", re.I)),
    ("eom", re.compile(r"\b(?:" + _PREP + r")?(?:the )?end of (?:the |this )?month\b", re.I)),
    ("nextweek", re.compile(r"\b(?:" + _PREP + r")?next week\b", re.I)),
    ("monthday", re.compile(r"\b(?:" + _PREP + r")?" + _MON + r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)),
    ("dayofmonth", re.compile(r"\b(?:" + _PREP + r")?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\s+of\s+" + _MON + r"\b", re.I)),
    ("weekday", re.compile(r"\b(?:" + _PREP + r")?(?:this\s+|(next)\s+)?" + _WD + r"\b", re.I)),
)


def _month_day(month: int, day: int, on: date) -> Optional[date]:
    for year in (on.year, on.year + 1):
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d >= on:
            return d
    return None


def find_due(text: str, on: date) -> tuple[str, str]:
    """``(iso_date, matched_text)`` of the first spoken due date in ``text``,
    relative to the meeting day ``on``; ``("", "")`` when none."""
    best: Optional[tuple[int, date, str]] = None
    for kind, pattern in _DUE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        d: Optional[date] = None
        if kind == "today":
            d = on
        elif kind == "tomorrow":
            d = on + timedelta(days=1)
        elif kind == "eow":
            d = on + timedelta(days=(4 - on.weekday()) % 7)
        elif kind == "eom":
            d = date(on.year, on.month, _calendar.monthrange(on.year, on.month)[1])
        elif kind == "nextweek":
            d = on + timedelta(days=7 - on.weekday())
        elif kind == "monthday":
            d = _month_day(_MONTH_NUM[m.group(1).lower()[:3]], int(m.group(2)), on)
        elif kind == "dayofmonth":
            d = _month_day(_MONTH_NUM[m.group(2).lower()[:3]], int(m.group(1)), on)
        elif kind == "weekday":
            target = _WEEKDAYS[m.group(2).lower()]
            ahead = (target - on.weekday()) % 7 or 7
            d = on + timedelta(days=ahead)
            if m.group(1) and d.isocalendar()[1] == on.isocalendar()[1]:
                d += timedelta(days=7)
        if d is not None and (best is None or m.start() < best[0]):
            best = (m.start(), d, m.group(0))
    return (best[1].isoformat(), best[2]) if best else ("", "")


_FIRST_PERSON = re.compile(
    r"\b(?:I'll|I will|I'm going to|I am going to|I'm gonna|I need to|I have to|"
    r"I can take|let me)\s+(?P<task>.+)", re.I)
_ACTION_LABEL = re.compile(r"\baction items?(?: for (?P<name>[\w .'-]+?))?\s*[:,-]\s*(?P<task>.+)", re.I)
_DECISION = re.compile(
    r"\b(?:we(?:'ve| have)? decided|we(?:'ve| have)? agreed|we are going with|we're going with|"
    r"let's go with|we'll go with|the decision is|decision:|agreed:|decided:)\b", re.I)
_FOLLOW_UP = re.compile(
    r"\b(?:follow[- ]?up (?:meeting|call)|meet again|reconvene|regroup|sync again|"
    r"catch up again|check in again|next meeting)\b", re.I)
_TIME_RE = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", re.I)
_NOT_TASKS = re.compile(r"^(?:be\b|say\b|just say\b|admit\b|guess\b|think\b|try\b$|tell you\b|"
                        r"see\b|have a look\b|let you know\b$)", re.I)


def _clean_task(task: str, due_text: str) -> str:
    if due_text:
        task = task.replace(due_text, " ")
    task = re.sub(r"\s+", " ", task).strip(" ,.;:!?-")
    task = re.sub(r"\b(?:okay|ok|alright|right|then)$", "", task, flags=re.I).strip(" ,.")
    if not task:
        return ""
    return (task[0].upper() + task[1:])[:MAX_TITLE]


def _named(sentence: str, names: Iterable[str]) -> Optional[tuple[str, str]]:
    """``(name, task)`` when a known name is given the task in ``sentence``."""
    alts = sorted({n for n in names if n}, key=len, reverse=True)
    if not alts:
        return None
    nm = "(?P<name>" + "|".join(re.escape(n) for n in alts) + ")"
    patterns = (
        r"\b" + nm + r"\b,?\s+(?:will|'ll|is going to|is gonna|to|owns|is taking)\s+(?P<task>.+)",
        r"\b" + nm + r"\b,?\s+(?:can|could|would) you(?: please)?\s+(?P<task>.+)",
        r"\b(?:can|could|would) you(?: please)?\s+(?P<task>.+?),?\s+" + nm + r"\b\W*$",
    )
    for pattern in patterns:
        m = re.search(pattern, sentence, re.I)
        if m:
            return m.group("name"), m.group("task")
    return None


def _display(name: str, people: Sequence[Mapping[str, Any]]) -> str:
    low = name.lower()
    for person in people:
        full = str(person.get("name") or "")
        if low in _name_forms(full):
            return full
    return name[:1].upper() + name[1:]


def extract(transcript: str, on: date, mine: set[str],
            attendees: Sequence[Mapping[str, Any]] = (), title: str = "") -> Summary:
    """Decisions, action items (owner, due) and a follow-up, by local rules."""
    people_names: set[str] = set(mine)
    for person in attendees:
        people_names |= _name_forms(str(person.get("name") or ""))
    out = Summary()
    seen: set[str] = set()
    for utt in parse_transcript(transcript):
        s = utt.text
        if _DECISION.search(s) and len(out.decisions) < MAX_ITEMS:
            out.decisions.append(_clean_task(s, ""))
        if out.follow_up is None and _FOLLOW_UP.search(s):
            due, _ = find_due(s, on)
            if due:
                hour, minute = 10, 0
                tm = _TIME_RE.search(s)
                if tm:
                    hour, minute = int(tm.group(1)) % 24, int(tm.group(2) or 0)
                    ampm = (tm.group(3) or "").lower().replace(".", "")
                    if ampm == "pm" and hour < 12:
                        hour += 12
                    elif ampm == "am" and hour == 12:
                        hour = 0
                    elif not ampm and 1 <= hour <= 7:
                        hour += 12  # "at 3" in a meeting means the afternoon
                start = datetime.combine(date.fromisoformat(due), time(hour % 24, minute % 60))
                out.follow_up = FollowUp(
                    title=("Follow-up: " + title).strip()[:MAX_TITLE] if title else "Follow-up meeting",
                    start=start.isoformat(timespec="minutes"),
                    end=(start + timedelta(minutes=30)).isoformat(timespec="minutes"))
                continue
        item: Optional[ActionItem] = None
        named = _named(s, people_names)
        label = _ACTION_LABEL.search(s)
        first = _FIRST_PERSON.search(s)
        if named:
            name, task = named
            item = ActionItem(task, _display(name, attendees), name.lower() in mine, sentence=s)
        elif label:
            name = (label.group("name") or "").strip()
            is_me = name.lower() in mine or (not name and utt.source != "others")
            if name and name.lower() not in people_names:
                is_me = False
            item = ActionItem(label.group("task"), _display(name, attendees) if name else
                              ("you" if is_me else "someone"), is_me, sentence=s)
        elif first and not _NOT_TASKS.search(first.group("task")):
            # No speaker identification: "I'll" is the user's on the user's
            # own channel (or a single mixed stream), someone else's otherwise.
            is_me = utt.source != "others"
            item = ActionItem(first.group("task"), "you" if is_me else "someone on the call",
                              is_me, sentence=s)
        if item is None:
            continue
        item.due, due_text = find_due(item.task, on)
        item.task = _clean_task(item.task, due_text)
        key = item.task.lower()
        if len(item.task.split()) < 2 or key in seen or len(out.action_items) >= MAX_ITEMS:
            continue
        seen.add(key)
        out.action_items.append(item)
    return out


def format_notes(title: str, when: str, s: Summary) -> str:
    lines = [f"{title} ({when})" if when else title]
    if s.summary:
        lines += ["", s.summary]
    if s.decisions:
        lines += ["", "Decided:"] + [f"• {d}" for d in s.decisions]
    if s.action_items:
        lines += ["", "Action items:"]
        for a in s.action_items:
            due = f" (by {a.due})" if a.due else ""
            lines.append(f"• {a.owner}: {a.task}{due}")
    if s.follow_up:
        lines += ["", f"Follow-up: {s.follow_up.start.replace('T', ' ')}"]
    if s.summary_note:
        lines += ["", s.summary_note]
    return "\n".join(lines)


# ---- optional summary by a LOCAL engine ----------------------------------------

SUMMARY_PROMPT = (
    "Summarize this meeting transcript in at most three plain sentences. Reply "
    'with one JSON object only: {{"summary": "...", "decisions": ["..."]}}.\n\n'
    "Transcript:\n{transcript}"
)
CLOUD_SKIPPED = "Summary by local rules only: no local engine is set up and the transcript never goes to a cloud engine."


def _is_cloud(role: str) -> bool:
    from ai import privacy

    try:
        return privacy.role_is_cloud(role)
    except Exception:
        logger.exception("meeting notes: engine lookup failed; treating %s as cloud", role)
        return True


def local_evaluator(evaluators: Sequence[tuple[str, Evaluate]],
                    is_cloud: Callable[[str], bool] = _is_cloud) -> Optional[Evaluate]:
    """The first evaluator whose engine role is local, else None. The
    personal-text opt-in does NOT apply: a transcript never goes to a cloud
    engine (#86 decision 3)."""
    for role, evaluate in evaluators:
        if not is_cloud(role):
            return evaluate
    return None


def _parse_json(raw: str) -> Optional[dict[str, Any]]:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---- the card -------------------------------------------------------------------

def _emails(attendees: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(p["email"]) for p in attendees if p.get("email") and not p.get("is_self")]


def build_offer(meeting: Mapping[str, Any], s: Summary, notes_text: str) -> Any:
    """One #76 prepared offer: the decisions as text, then the actions."""
    from ai.prepared_offers import OfferAction, PreparedOffer

    title = str(meeting.get("title") or "your meeting")[:MAX_TITLE]
    day = str(meeting.get("start") or "")[:10]
    actions: list[Any] = []
    for item in s.mine:
        args: dict[str, Any] = {"title": item.task, "notes": f"From {title}" + (f" on {day}" if day else "")}
        if item.due:
            args["due"] = item.due
        label = f"Reminder: {item.task}" + (f" (due {item.due})" if item.due else "")
        actions.append(OfferAction("create_reminder", args, True, label))
    if s.follow_up:
        actions.append(OfferAction("create_event", {
            "title": s.follow_up.title, "start": s.follow_up.start, "end": s.follow_up.end,
            "notes": f"Follow-up to {title}"}, True,
            f"Add follow-up on {s.follow_up.start.replace('T', ' ')}"))
    to = _emails(_attendees(meeting))
    if to:
        actions.append(OfferAction("draft_email", {
            "to": ", ".join(to), "subject": f"Notes: {title}", "body": notes_text}, True,
            f"Draft the notes to {len(to)} attendee{'s' if len(to) != 1 else ''} (not sent)"))
    actions.append(OfferAction("create_note", {
        "title": f"{title} notes {day}".strip(), "body": notes_text}, True, "Save the notes in Notes"))
    lines = [f"Notes from {title}."]
    if s.decisions:
        lines.append("Decided: " + "; ".join(s.decisions[:5]))
    if s.mine:
        lines.append("You owe: " + "; ".join(
            a.task + (f" (by {a.due})" if a.due else "") for a in s.mine[:5]))
    elif not s.decisions:
        lines.append("No decisions or action items for you were heard.")
    return PreparedOffer(OFFER_KIND, "\n".join(lines), actions, {
        "meeting_id": meeting.get("meeting_id", ""), "title": title,
        "decisions": len(s.decisions), "action_items": len(s.mine),
    })


def make_idea(meeting: Mapping[str, Any], offer: Any) -> dict[str, Any]:
    """``pattern_detected`` metadata. No transcript text outside the offer:
    the details and the description name the meeting only."""
    from ai import privacy
    from ai.prepared_offers import PREPARED_KEY

    title = str(meeting.get("title") or "your meeting")
    details = f"Meeting notes for {title} are ready as one card."
    return {
        "tool_name": IDEA_NAME,
        "description": f"Follow up on {title}",
        "expected_logic": "Show the prepared meeting follow-through card; run only the ticked actions.",
        "event_details": details,
        "event_details_redacted": privacy.PLACEHOLDER,
        "source": SOURCE_NAME,
        "trigger": OFFER_KIND,
        "meeting_id": str(meeting.get("meeting_id") or ""),
        "source_event_type": "meeting_audio_ready",
        PREPARED_KEY: offer.to_dict(),
    }


# ---- link to the menu bar app --------------------------------------------------

class RecorderLink:
    """Frames to the app that most recently sent ``meeting_record_state``.
    ``send`` works from any thread; it never waits for the app."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, tuple[Sender, asyncio.AbstractEventLoop]] = {}
        self._current: Optional[str] = None

    def connect(self, conn_id: str, send: Sender, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._conns[conn_id] = (send, loop)

    def disconnect(self, conn_id: str) -> None:
        with self._lock:
            self._conns.pop(conn_id, None)
            if self._current == conn_id:
                self._current = None

    def advertise(self, conn_id: str) -> None:
        with self._lock:
            if conn_id in self._conns:
                self._current = conn_id

    def connected(self) -> bool:
        with self._lock:
            return self._current is not None

    def send(self, frame_type: str, payload: dict[str, Any]) -> bool:
        with self._lock:
            conn = self._conns.get(self._current or "")
        if conn is None:
            return False
        sender, loop = conn
        try:
            fut = asyncio.run_coroutine_threadsafe(sender(frame_type, dict(payload)), loop)
        except RuntimeError:
            return False
        fut.add_done_callback(_log_send_error)
        return True


def _log_send_error(fut: concurrent.futures.Future) -> None:
    if not fut.cancelled() and fut.exception() is not None:
        logger.warning("meeting notes: frame not sent: %s", fut.exception())


# ---- the scheduler / processor ------------------------------------------------

def _parse(value: Any) -> Optional[datetime]:
    from .meeting_prep import _parse as parse

    return parse(value)


def meeting_id_for(meta: Mapping[str, Any]) -> str:
    raw = f"{meta.get('id') or meta.get('title') or ''}|{meta.get('start') or ''}"
    return "m" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def stop_at(meta: Mapping[str, Any], now: datetime) -> datetime:
    """When a recording of this meeting stops: its end + 5 minutes; a
    meeting without an end (or a menu-item recording) stops after
    :data:`MANUAL_MAX`."""
    end = _parse(meta.get("end"))
    if end is None or end <= now:
        return now + MANUAL_MAX if end is None else end + STOP_GRACE
    return end + STOP_GRACE


def _offer_lead_minutes() -> int:
    from . import meeting_prep

    return meeting_prep.minutes_before() if meeting_prep.prep_enabled() else 0


def _auth(value: Any) -> str:
    text = str(value or "")
    return text if text in AUTH_STATUSES else ""


_PERM_STATUS = {"authorized": "granted", "denied": "denied", "restricted": "denied",
                "not_determined": "not_asked"}


class MeetingNotes:
    """Offers to take notes (never starts), arms the stop timer, and turns a
    finished recording into one prepared-offer card."""

    def __init__(
        self,
        bus: Optional[EventBus] = None,
        link: Optional[RecorderLink] = None,
        clock: Callable[[], datetime] = _now,
        timer: Optional[Callable[[float, Callable[[], None]], Any]] = None,
        enabled: Callable[[], bool] = notes_enabled,
        lead_minutes: Callable[[], int] = _offer_lead_minutes,
        engine_name: Callable[[], str] = engine,
        evaluators: Sequence[tuple[str, Evaluate]] = (),
        is_cloud: Callable[[str], bool] = _is_cloud,
        transcriber: Optional[Callable[[str], str]] = None,
        full_name: Optional[str] = None,
        publish: Optional[Callable[[ContextEvent], Awaitable[None]]] = None,
    ) -> None:
        self._bus = bus
        self.link = link or RecorderLink()
        self._clock = clock
        self._timer = timer or self._thread_timer
        self._enabled = enabled
        self._lead = lead_minutes
        self._engine = engine_name
        self._evaluators = list(evaluators)
        self._is_cloud = is_cloud
        self._transcriber = transcriber
        self._full_name = full_name
        self._publish = publish or (bus.push if bus is not None else None)
        self._meetings: dict[str, dict[str, Any]] = {}
        self._offered: set[str] = set()
        self._stop_timers: dict[str, Any] = {}
        self._pending: list[Any] = []
        self._recording: Optional[str] = None
        self._lock = threading.Lock()
        self._stopped = False

    @staticmethod
    def _thread_timer(delay: float, fn: Callable[[], None]) -> Any:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.name = "MeetingNotes"
        t.start()
        return t

    def start(self) -> None:
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True
        with self._lock:
            handles = self._pending + list(self._stop_timers.values())
            self._pending, self._stop_timers = [], {}
        for h in handles:
            cancel = getattr(h, "cancel", None)
            if cancel is not None:
                cancel()

    @property
    def recording(self) -> Optional[str]:
        return self._recording

    # 1. offer ---------------------------------------------------------------

    def observe(self, event: ContextEvent) -> Optional[float]:
        """Schedule one offer for an ``event_starting_soon``; the delay used."""
        from .meeting_prep import skip_reason

        if self._stopped or event.event_type != TRIGGER_EVENT_TYPE or not self._enabled():
            return None
        meta = dict(event.metadata or {})
        now = self._clock()
        if skip_reason(meta, now):
            return None
        mid = meeting_id_for(meta)
        start = _parse(meta.get("start"))
        assert start is not None  # skip_reason checked it
        delay = max(0.0, (start - timedelta(minutes=max(0, int(self._lead()))) - now).total_seconds())
        with self._lock:
            if mid in self._offered:
                return None
            self._offered.add(mid)
            self._meetings[mid] = {**meta, "meeting_id": mid}
            self._pending.append(self._timer(delay, lambda: self.send_offer(mid)))
        return delay

    def offer_payload(self, mid: str) -> Optional[dict[str, Any]]:
        meta = self._meetings.get(mid)
        if meta is None:
            return None
        now = self._clock()
        return {
            "meeting_id": mid,
            "title": str(meta.get("title") or "Meeting")[:MAX_TITLE],
            "start": str(meta.get("start") or ""),
            "end": str(meta.get("end") or ""),
            "stop_at": stop_at(meta, now).isoformat(timespec="seconds"),
            "engine": self._engine(),
            "attendee_count": len([p for p in _attendees(meta) if not p.get("is_self")]),
        }

    def send_offer(self, mid: str) -> bool:
        """Send the "Take notes?" offer. An offer cannot start a recording:
        the app starts one only on the user's click."""
        if self._stopped or not self._enabled():
            return False
        payload = self.offer_payload(mid)
        if payload is None:
            return False
        sent = self.link.send(FRAME_OFFER, payload)
        if not sent:
            logger.info("meeting notes: no menu bar app connected; no offer for %s", payload["title"])
        return sent

    # 2. state from the app --------------------------------------------------

    def on_record_state(self, conn_id: str, payload: Mapping[str, Any]) -> None:
        """The app's ``meeting_record_state`` frame."""
        self.link.advertise(conn_id)
        self._report_permissions(payload)
        state = str(payload.get("state") or "")
        mid = str(payload.get("meeting_id") or "")
        if state not in STATES or (mid and not _ID_RE.match(mid)):
            return
        if state == "started" and mid:
            now = self._clock()
            meta = self._meetings.get(mid)
            if meta is not None:
                when = stop_at(meta, now)
            else:
                # Menu-item recording: the app's own stop_at, capped.
                asked = _parse(payload.get("stop_at"))
                cap = now + MANUAL_MAX
                when = min(asked, cap) if asked else cap
                self._meetings[mid] = {"meeting_id": mid, "title": str(payload.get("title") or "Meeting"),
                                       "start": now.isoformat(timespec="seconds")}
            self._recording = mid
            delay = max(0.0, (when - now).total_seconds())
            with self._lock:
                old = self._stop_timers.pop(mid, None)
                self._stop_timers[mid] = self._timer(delay, lambda: self._stop_recording(mid))
            if old is not None and hasattr(old, "cancel"):
                old.cancel()
        elif state in ("stopped", "failed", "declined") and mid:
            with self._lock:
                handle = self._stop_timers.pop(mid, None)
            if handle is not None and hasattr(handle, "cancel"):
                handle.cancel()
            if self._recording == mid:
                self._recording = None

    def _stop_recording(self, mid: str) -> bool:
        with self._lock:
            self._stop_timers.pop(mid, None)
        return self.link.send(FRAME_STOP, {"meeting_id": mid, "reason": "meeting_ended"})

    @staticmethod
    def _report_permissions(payload: Mapping[str, Any]) -> None:
        from ai import permissions

        for key, perm_id in (("microphone", "microphone"), ("speech_recognition", "speech_recognition")):
            status = _auth(payload.get(key))
            if status:
                mapped = _PERM_STATUS[status]
                permissions.set_external_check(
                    perm_id, lambda m=mapped, s=status: permissions._result(m, f"menu bar app: {s}"))

    # 3. audio / transcript -> one card ---------------------------------------

    async def process(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Handle ``meeting_audio_ready``. Returns ``{status, reason?, kept?}``;
        ``status`` is ``offered``, ``empty`` or ``error``. The audio is
        deleted on every path except a failed Whisper transcription here
        (#108), where it is kept for the app's Retry (``kept: True``)."""
        audio = payload.get("audio_path")
        system_audio = payload.get("system_audio_path")
        mid = str(payload.get("meeting_id") or "")
        whisper = bool(audio or system_audio) and not payload.get("transcript_path")
        kept: Optional[dict[str, Any]] = None
        try:
            if not _ID_RE.match(mid):
                return {"status": "error", "reason": "bad meeting id"}
            if payload.get("error"):
                return {"status": "error", "reason": str(payload["error"])[:300]}
            if payload.get("transcript_path"):
                text = await asyncio.to_thread(read_transcript, payload["transcript_path"])
            elif whisper:
                try:
                    text = await self._transcribe_channels(audio, system_audio)
                except Exception as exc:
                    logger.exception("meeting notes: transcription failed")
                    reason = str(exc)[:300] or type(exc).__name__
                    kept = await asyncio.to_thread(keep_failed, mid, payload, reason, self._clock())
                    self._tell_failed(mid, payload, reason, kept)
                    return {"status": "error", "reason": reason, "kept": kept is not None}
            else:
                return {"status": "error", "reason": "no audio or transcript"}
        except Exception as exc:
            logger.exception("meeting notes: transcription failed")
            return {"status": "error", "reason": str(exc)[:300]}
        finally:
            if kept is None:
                await asyncio.to_thread(delete_audio, audio)
                await asyncio.to_thread(delete_audio, system_audio)
                await asyncio.to_thread(delete_marker, mid)
            if self._recording == mid:
                self._recording = None
        result = await self._finish(mid, payload, text)
        if whisper:
            # #108: the app forgets a kept failure (its Retry worked).
            self.link.send(FRAME_STOP, {
                "meeting_id": mid, "reason": "transcription_done", "transcription": "done",
                "title": str(payload.get("title") or "Meeting")[:MAX_TITLE],
                "message": str(result.get("reason") or ""),
            })
        return result

    async def _transcribe_channels(self, audio: Any, system_audio: Any) -> str:
        """Microphone = the user's channel, system audio = the others'. Both
        are transcribed before either is deleted, so a failure on the second
        keeps both for the Retry (#108)."""
        said: list[tuple[str, str]] = []
        for tag, path in (("me", audio), ("others", system_audio)):
            if path:
                said.append((tag, await asyncio.to_thread(_transcribe_file, path, self._transcriber)))
        return "\n".join(f"[{tag}] {text.strip()}" for tag, text in said if text.strip())

    def _tell_failed(self, mid: str, payload: Mapping[str, Any], reason: str,
                     kept: Optional[Mapping[str, Any]]) -> bool:
        """#108: tell the app; its #105 panel offers Retry / Delete / Later."""
        frame: dict[str, Any] = {
            "meeting_id": mid, "reason": "transcription_failed", "transcription": "failed",
            "title": str(payload.get("title") or "Meeting")[:MAX_TITLE],
            "error": reason, "kept": kept is not None,
            "started_at": str(payload.get("started_at") or ""),
            "ended_at": str(payload.get("ended_at") or ""),
            "stop_reason": payload.get("stop_reason") or None,
        }
        if kept is not None:
            frame.update({
                "audio_path": kept["micPath"], "system_audio_path": kept["systemPath"],
                "failed_at": kept["failed_at"].isoformat(timespec="seconds"),
                "expires_at": kept["expires_at"].isoformat(timespec="seconds"),
            })
        sent = self.link.send(FRAME_STOP, frame)
        if not sent:
            logger.info("meeting notes: no menu bar app connected to hear the failure of %s", mid)
        return sent

    async def _finish(self, mid: str, payload: Mapping[str, Any], text: str) -> dict[str, Any]:
        if not text.strip():
            return {"status": "empty", "reason": "nothing was transcribed"}
        path = await asyncio.to_thread(write_transcript, mid, text)
        meeting = dict(self._meetings.get(mid) or {"meeting_id": mid,
                                                   "title": str(payload.get("title") or "Meeting"),
                                                   "start": str(payload.get("started_at") or "")})
        event = await self.summarize_to_event(meeting, text, path)
        if event is None:
            return {"status": "empty", "reason": "no follow-through found"}
        if self._publish is not None:
            await self._publish(event)
        return {"status": "offered", "transcript_path": str(path)}

    async def summarize(self, meeting: Mapping[str, Any], text: str) -> Summary:
        start = _parse(meeting.get("start")) or self._clock()
        people = _attendees(meeting)
        summary = extract(text, start.date(), my_names(people, self._full_name), people,
                          str(meeting.get("title") or ""))
        evaluate = local_evaluator(self._evaluators, self._is_cloud)
        if evaluate is None:
            summary.summary_note = CLOUD_SKIPPED if self._evaluators else ""
            return summary
        try:
            got = _parse_json(await evaluate(SUMMARY_PROMPT.format(transcript=text[:12000])))
        except Exception:
            logger.exception("meeting notes: local summary failed; rules only")
            got = None
        if got:
            summary.summary = str(got.get("summary") or "").strip()[:800]
            extra = [str(d).strip()[:MAX_TITLE] for d in got.get("decisions") or [] if str(d).strip()]
            if not summary.decisions:
                summary.decisions = extra[:MAX_ITEMS]
        return summary

    async def summarize_to_event(self, meeting: Mapping[str, Any], text: str,
                                 path: Optional[Path] = None) -> Optional[ContextEvent]:
        s = await self.summarize(meeting, text)
        when = str(meeting.get("start") or "")[:16].replace("T", " ")
        notes = format_notes(str(meeting.get("title") or "Meeting"), when, s)
        if path is not None:
            notes += f"\n\nTranscript (on this Mac): {path}"
        offer = build_offer(meeting, s, notes)
        if not offer.actions:
            return None
        return ContextEvent(event_type="pattern_detected", metadata=make_idea(meeting, offer))


__all__ = [
    "ActionItem", "ENABLED_KEY", "ENGINE_KEY", "ENGINES", "FRAME_OFFER", "FRAME_STOP",
    "FollowUp", "MANUAL_MAX", "MeetingNotes", "RecorderLink", "STOP_GRACE", "Summary",
    "KEEP_FAILED_FOR", "audio_dir", "build_offer", "delete_audio", "delete_marker", "engine", "extract", "find_due", "keep_failed",
    "local_evaluator", "make_idea", "marker_path", "meeting_id_for", "my_names", "notes_dir", "notes_enabled",
    "parse_transcript", "read_marker", "read_transcript", "settings_view", "stop_at", "transcribe_audio",
    "update_settings", "write_transcript",
]
