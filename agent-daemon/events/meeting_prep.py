"""Meeting prep: one ``meeting_prep`` event a few minutes before a meeting (#71).

Flow:

* ``events.calendar_watcher`` publishes ``event_starting_soon`` once per
  meeting when its start is within ``calendar_watcher.LEAD_TIME`` (fixed 10
  minutes, polled every 60 s).
* :class:`MeetingPrepScheduler` sees that event through :meth:`observe` (the
  orchestrator calls ``observe(event)`` on every idea source for every
  event). If prep is on and the meeting qualifies it waits until
  ``start - minutes_before`` (no wait when that moment has passed), then
  gathers and publishes ``ContextEvent(event_type="meeting_prep",
  metadata=<payload>)``. The orchestrator words that in at most 6 lines
  (thought + notification), like the morning brief.

``minutes_before`` (setting, default 10) is clamped to 1..10 because the
watcher only announces a meeting 10 minutes ahead: a larger value could not
be honoured without changing the watcher's lead time. Smaller values wait.

Skipped (no event, nothing gathered): all-day events, meetings already
started, meetings whose attendees are known and include no one but the
user, and meetings whose attendees are unknown and that have no call link
(today the calendar layer does not report attendees; see
:func:`skip_reason`). When in doubt, nothing is surfaced (invariant 7d).

Gathered read-only (invariant 7a), each source wrapped so a failure is a
named "unavailable: <reason>" line, never dropped:

* attendees, location and call link from the event's metadata;
* the last few mail headers from or to those attendees (Mail's Envelope
  Index, read-only; sender + subject, never a body); each subject goes
  through ``ai.privacy.redact_text`` for the engines that read the event
  (invariant 7c, as #65);
* ``tools.find_files`` (Spotlight, read-only) on the title's content words;
* whether a note with the same title exists in Notes (title, folder and
  modification date only; checked only when Automation for Notes is already
  granted and Notes is running, so it never prompts or launches Notes).

Nothing here sends, writes, posts a notification or asks for approval.

Settings (``user_settings``, read live): ``AGENT_MEETING_PREP`` ("1"/"0",
default on) and ``AGENT_MEETING_PREP_MINUTES`` (1..10, default 10).
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

EVENT_TYPE = "meeting_prep"
TRIGGER_EVENT_TYPE = "event_starting_soon"
ENABLED_KEY = "AGENT_MEETING_PREP"
MINUTES_KEY = "AGENT_MEETING_PREP_MINUTES"
DEFAULT_MINUTES = 10
MIN_MINUTES = 1
# calendar_watcher.LEAD_TIME: the watcher announces a meeting this far ahead.
MAX_MINUTES = 10

MAIL_WINDOW = timedelta(days=30)
MAX_MAIL = 5
MAX_FILES = 5
MAX_ATTENDEES = 8
MAX_TITLE_WORDS = 3
FILES_PER_WORD = 5
READER_ROLES: tuple[str, ...] = ("main", "analysis")

SOURCES: tuple[tuple[str, str], ...] = (
    ("attendees", "Attendees"),
    ("mail", "Mail"),
    ("files", "Files"),
    ("note", "Notes"),
)

ATTENDEES_UNKNOWN = "the calendar doesn't report attendees yet"

# Words in a meeting title that say nothing about its content.
_STOP_WORDS = frozenset("""
a an and the of for to with on in at by from about re fw fwd vs via
meeting meet sync call chat catch up catchup check checkin check-in weekly
daily monthly quarterly biweekly standup stand-up stand 1:1 1on1 one on
review discussion intro session touch base quick follow followup follow-up
team zoom teams google hangout invite invitation updated update new
""".split())
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9&+'’.-]*")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _now() -> datetime:
    return datetime.now().astimezone()


# ---- settings ----------------------------------------------------------------

def _setting(key: str, default: str) -> str:
    """Shell export, then the settings file (read live), then .env, then default."""
    import user_settings

    if key not in user_settings.shell_exported:
        saved = user_settings.read().get(key)
        if saved:
            return saved
    return os.environ.get(key) or default


def parse_minutes(value: Any) -> Optional[int]:
    """``value`` as whole minutes in ``MIN_MINUTES..MAX_MINUTES``, else None."""
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        return None
    minutes = int(text)
    return minutes if MIN_MINUTES <= minutes <= MAX_MINUTES else None


def prep_enabled() -> bool:
    return _setting(ENABLED_KEY, "1").strip() != "0"


def minutes_before() -> int:
    return parse_minutes(_setting(MINUTES_KEY, str(DEFAULT_MINUTES))) or DEFAULT_MINUTES


# ---- event metadata ----------------------------------------------------------------

def _parse(value: Any) -> Optional[datetime]:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo is not None else stamp.astimezone()


def _address(text: str) -> str:
    match = _EMAIL_RE.search(text or "")
    return match.group(0).lower() if match else ""


def attendees_of(meta: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    """The event's attendees, or None when the calendar didn't say.

    Accepts ``meta["attendees"]`` as a list of ``{name, email, is_self}``
    dicts (``email`` may be a ``mailto:`` URL, as EventKit gives it) or of
    ``"Name <addr>"`` strings.
    """
    raw = meta.get("attendees")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    people: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            email = _address(str(item.get("email") or item.get("url") or ""))
            name = str(item.get("name") or "").strip()
            is_self = bool(item.get("is_self") or item.get("is_current_user"))
        else:
            text = str(item or "")
            email = _address(text)
            name = text.split("<", 1)[0].strip().strip('"') if "<" in text else ("" if email else text.strip())
            is_self = False
        if not (email or name):
            continue
        people.append({"name": name, "email": email, "is_self": is_self})
    return people


def others(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in people if not p["is_self"]]


def skip_reason(meta: dict[str, Any], now: Optional[datetime] = None) -> str:
    """Why this ``event_starting_soon`` gets no prep ("" when it does)."""
    if meta.get("all_day"):
        return "all-day event"
    start = _parse(meta.get("start"))
    if start is None:
        return "no start time"
    if start < (now or _now()):
        return "already started"
    people = attendees_of(meta)
    if people is None:
        # Unknown attendees: a call link says other people are expected;
        # without one it is most likely a solo block (7d: when in doubt, drop).
        return "" if meta.get("video_url") else "attendees unknown and no call link"
    if not others(people):
        return "no other attendee"
    return ""


def content_words(title: str) -> list[str]:
    """Up to ``MAX_TITLE_WORDS`` distinctive words of a title, longest first."""
    words: list[str] = []
    for raw in _WORD_RE.findall(title or ""):
        word = raw.strip(".'’-")
        low = word.lower()
        short = len(low) < 3 and not (len(low) == 2 and any(c.isdigit() for c in low) and any(c.isalpha() for c in low))
        if short or low in _STOP_WORDS or low.isdigit() or low in (w.lower() for w in words):
            continue
        words.append(word)
    return sorted(words, key=len, reverse=True)[:MAX_TITLE_WORDS]


# ---- default sources (read-only, never prompt) ---------------------------------------

SQL_MAIL_WITH = """
    SELECT m.ROWID, s.subject, a.address, {sender_name}, m.date_received, mb.url, {global_id}
    FROM messages m
    LEFT JOIN subjects s ON m.subject = s.ROWID
    LEFT JOIN addresses a ON m.sender = a.ROWID
    LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
    WHERE m.date_received >= ?
      AND (LOWER(a.address) IN ({marks}){to_clause})
    ORDER BY m.date_received DESC
    LIMIT 200
"""
SQL_TO_CLAUSE = """
      OR m.ROWID IN (SELECT r.message FROM recipients r
                     JOIN addresses ra ON r.address = ra.ROWID
                     WHERE LOWER(ra.address) IN ({marks}))"""
_SKIP_MAIL_RE = re.compile(r"junk|spam|trash|deleted|draft|outbox|send ?later", re.IGNORECASE)


def mail_with(addresses: list[str], since: datetime, mail_root: Path | str | None = None) -> dict[str, Any]:
    """Mail headers from or to ``addresses`` since ``since``, newest first.

    Read-only query of Mail's Envelope Index (the path ``events.daily_brief``
    uses). "To" matches use the ``recipients`` table when the index has one.
    Sent mail is kept (it is mail *to* an attendee); drafts, junk and trash
    are left out, as are Gmail label duplicates.
    """
    from tools.read_email import mail_root as default_mail_root

    from . import mail_watcher as mw

    addresses = sorted({a.lower() for a in addresses if a})
    if not addresses:
        return {"status": "error", "error": "no attendee email addresses"}
    root = Path(mail_root).expanduser() if mail_root else default_mail_root()
    path = mw.find_index(root)
    if path is None:
        if mw.root_unreadable(root):
            return {"status": "error",
                    "error": "the daemon needs Full Disk Access to read Mail (Settings > Permissions)."}
        return {"status": "error", "error": "no Mail index found (is the Mail app set up?)."}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        return {"status": "error", "error": f"could not open the Mail index ({exc})."}
    try:
        columns = mw.table_columns(conn)
        problems = mw.schema_problems(columns)
        if problems:
            return {"status": "error",
                    "error": "the Mail index layout changed (" + "; ".join(problems) + ")."}
        table, col = mw.OPTIONAL_SENDER_NAME
        has_name = col in {c.lower() for c in columns.get(table, [])}
        message_cols = {c.lower() for c in columns.get("messages", [])}
        recipient_cols = {c.lower() for c in columns.get("recipients", [])}
        marks = ",".join("?" for _ in addresses)
        has_to = {"message", "address"} <= recipient_cols
        sql = SQL_MAIL_WITH.format(
            sender_name=f"a.{col}" if has_name else "NULL",
            global_id="m.global_message_id" if "global_message_id" in message_cols else "NULL",
            marks=marks,
            to_clause=SQL_TO_CLAUSE.format(marks=marks) if has_to else "",
        )
        params: list[Any] = [since.timestamp(), *addresses] + (addresses if has_to else [])
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        return {"status": "error", "error": f"could not read the Mail index ({exc})."}
    finally:
        conn.close()
    seen: set[Any] = set()
    messages: list[dict[str, Any]] = []
    for rowid, subject, address, name, received, url, global_id in rows:
        mailbox = mw.mailbox_name(url)
        if _SKIP_MAIL_RE.search(mailbox):
            continue
        if global_id not in (None, 0):
            if global_id in seen:
                continue
            seen.add(global_id)
        messages.append({
            "message_id": int(rowid),
            "sender": mw._sender(address, name),
            "subject": subject or "",
            "date": mw._iso_date(received),
            "sent": mw.is_own_mailbox(url),
        })
    return {"status": "ok", "count": len(messages), "messages": messages}


def _default_find_files(query: str, limit: int = FILES_PER_WORD) -> dict[str, Any]:
    from tools.find_files import find_files  # read-only (READ_ONLY_TOOL_NAMES)

    return find_files(query=query, limit=limit)


# argv: 1 = exact note title. Prints "count<US>folder<US>modified" (read-only).
_NOTE_SCRIPT = '''
on run argv
    set noteTitle to item 1 of argv
    tell application "Notes"
        set found to {}
        repeat with n in (every note whose name is noteTitle)
            considering case
                if (name of n as text) is noteTitle then set end of found to contents of n
            end considering
        end repeat
        if (count of found) is 0 then return "0"
        set theNote to item 1 of found
        set folderName to ""
        try
            set folderName to name of container of theNote
        end try
        set modifiedText to ""
        try
            set modifiedText to (modification date of theNote) as «class isot» as string
        end try
        return ((count of found) as text) & (ASCII character 31) & folderName & (ASCII character 31) & modifiedText
    end tell
end run
'''


def _default_find_note(title: str) -> dict[str, Any]:
    """Whether Notes has a note titled ``title``. Never prompts or launches Notes."""
    import subprocess

    from ai import permissions

    check = permissions.check_automation("com.apple.Notes", "Notes")
    if check.get("status") != permissions.GRANTED:
        detail = check.get("detail") or check.get("status") or "not allowed"
        return {"status": "error",
                "error": f"Notes automation isn't available ({detail}; Settings > Permissions)."}
    try:
        proc = subprocess.run(
            ["osascript", "-e", _NOTE_SCRIPT, "--", title],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        return {"status": "error", "error": (exc.stderr or str(exc)).strip()}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    fields = (proc.stdout or "").strip().split("\x1f")
    count = int(fields[0]) if fields and fields[0].isdigit() else 0
    if not count:
        return {"status": "ok", "notes": []}
    return {"status": "ok", "notes": [{
        "title": title,
        "folder": fields[1] if len(fields) > 1 else "",
        "modified": fields[2] if len(fields) > 2 else "",
        "count": count,
    }]}


# ---- gathering -----------------------------------------------------------------------

def _call_source(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Optional[dict[str, Any]], str]:
    """``(result, "")`` on success, else ``(None, reason)``. Never raises."""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # a source must never sink the whole card
        return None, str(exc) or exc.__class__.__name__
    if not isinstance(result, dict):
        return None, "unexpected answer"
    if result.get("status") != "ok":
        return None, str(result.get("error") or result.get("status") or "no answer")
    return result, ""


def _redact_subject(subject: str) -> str:
    from ai import privacy

    for role in READER_ROLES:
        subject = privacy.redact_text(subject, role)
    return subject


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def _ok(items: list[Any], cap: int, **extra: Any) -> dict[str, Any]:
    return {"status": "ok", "items": items[:cap], "truncated": len(items) > cap, **extra}


def _person_text(person: dict[str, Any]) -> str:
    name, email = person.get("name") or "", person.get("email") or ""
    if name and email:
        return f"{name} <{email}>"
    return name or email


def _gather_files(title: str, find_files: Callable[..., Any]) -> dict[str, Any]:
    words = content_words(title)
    if not words:
        return _ok([], MAX_FILES, words=[])
    found: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for word in words:
        result, reason = _call_source(find_files, word, limit=FILES_PER_WORD)
        if result is None:
            failures.append(reason)
            continue
        for item in result.get("results") or []:
            path = str(item.get("path") or "")
            if path and path not in found:
                found[path] = {"path": path, "name": str(item.get("name") or Path(path).name),
                               "modified": item.get("modified") or 0, "matched": word}
    if failures and not found:
        return _unavailable(failures[0])
    items = sorted(found.values(), key=lambda f: f["modified"] or 0, reverse=True)
    return _ok(items, MAX_FILES, words=words)


def _when(start: datetime, now: datetime) -> tuple[str, int]:
    minutes = max(0, int(round((start - now).total_seconds() / 60)))
    return start.strftime("%-I:%M %p"), minutes


def _section(label: str, noun: str, source: dict[str, Any], render: Callable[[Any], str]) -> list[str]:
    if source["status"] != "ok":
        return [f"{label}: unavailable: {source['reason']}"]
    items = source["items"]
    if not items:
        return [f"{label}: {noun}"]
    lines = [f"{label} ({len(items)}{'+' if source.get('truncated') else ''}):"]
    lines += [f"- {render(item)}" for item in items]
    return lines


def format_prep(payload: dict[str, Any]) -> str:
    """The plain-text card for a :func:`gather_prep` payload."""
    lines = [f"Meeting prep: {payload['title']} at {payload['when']} (in {payload['minutes_until']} min)"]
    where = []
    if payload["location"]:
        where.append(f"Where: {payload['location']}")
    if payload["video_url"]:
        where.append(f"Call link: {payload['video_url']}")
    lines.append(" · ".join(where) if where else "Where: no location or call link")
    attendees = payload["attendees"]
    if attendees["status"] == "ok":
        lines.append(f"Attendees ({len(attendees['items'])}): " + ", ".join(attendees["items"]))
    else:
        lines.append(f"Attendees: unavailable: {attendees['reason']}")
    lines += _section(
        "Recent mail with them", "none in the last 30 days", payload["mail"],
        lambda m: f"{'you (sent)' if m['sent'] else m['sender']}: "
                  f"{m['subject'] or '(no subject)'} ({m['date'][:10]}) [id {m['message_id']}]",
    )
    files_noun = ("no content words in the title" if not payload["files"].get("words")
                  else "none match " + ", ".join(payload["files"].get("words") or []))
    lines += _section("Files", files_noun, payload["files"], lambda f: f"{f['name']} ({f['path']})")
    note = payload["note"]
    if note["status"] != "ok":
        lines.append(f"Notes: unavailable: {note['reason']}")
    elif not note["items"]:
        lines.append("Notes: no note with this title")
    else:
        n = note["items"][0]
        where_text = f" in {n['folder']}" if n.get("folder") else ""
        when_text = f", modified {n['modified'][:10]}" if n.get("modified") else ""
        lines.append(f"Notes: a note titled \"{n['title']}\"{where_text}{when_text}")
    return "\n".join(lines)


def gather_prep(
    meta: dict[str, Any],
    now: Optional[datetime] = None,
    *,
    mail_with: Optional[Callable[..., Any]] = None,
    find_files: Optional[Callable[..., Any]] = None,
    find_note: Optional[Callable[..., Any]] = None,
    redact_subject: Optional[Callable[[str], str]] = None,
) -> Optional[dict[str, Any]]:
    """Gather the prep card for an ``event_starting_soon`` payload. Never raises.

    Returns None when the meeting gets no prep (:func:`skip_reason`). The
    sources are injectable (tests pass fakes): ``mail_with(addresses,
    since)`` answers ``{"status": "ok", "messages": [{message_id, sender,
    subject, date, sent}]}``; ``find_files(query, limit=)`` answers like the
    tool; ``find_note(title)`` answers ``{"status": "ok", "notes": [{title,
    folder, modified}]}``. Each of ``attendees`` / ``mail`` / ``files`` /
    ``note`` in the result is ``{"status": "ok", "items": [...]}`` or
    ``{"status": "unavailable", "reason": "..."}``; ``text`` is the card.
    """
    now = now or _now()
    if skip_reason(meta, now):
        return None
    fetch_mail = mail_with or globals()["mail_with"]
    find_files = find_files or _default_find_files
    find_note = find_note or _default_find_note
    redact = redact_subject or _redact_subject
    start = _parse(meta.get("start"))
    assert start is not None  # skip_reason checked it
    title = str(meta.get("title") or "(untitled)")

    people = attendees_of(meta)
    if people is None:
        attendees = _unavailable(ATTENDEES_UNKNOWN)
        mail = _unavailable("no attendees to look up (" + ATTENDEES_UNKNOWN + ")")
    else:
        guests = others(people)
        attendees = _ok([_person_text(p) for p in guests], MAX_ATTENDEES)
        addresses = [p["email"] for p in guests if p["email"]]
        if not addresses:
            mail = _unavailable("no attendee has an email address")
        else:
            result, reason = _call_source(fetch_mail, addresses, now - MAIL_WINDOW)
            if result is None:
                mail = _unavailable(reason)
            else:
                try:
                    mail = _ok([
                        {
                            "message_id": m.get("message_id"),
                            "sender": str(m.get("sender") or "unknown"),
                            # Sender + subject only; bodies never enter the card.
                            "subject": redact(str(m.get("subject") or "")),
                            "date": str(m.get("date") or ""),
                            "sent": bool(m.get("sent")),
                        }
                        for m in result.get("messages") or []
                    ], MAX_MAIL)
                except Exception as exc:
                    mail = _unavailable(f"unreadable answer ({exc})")

    try:
        files = _gather_files(title, find_files)
    except Exception as exc:
        files = _unavailable(str(exc) or exc.__class__.__name__)

    result, reason = _call_source(find_note, title)
    if result is None:
        note = _unavailable(reason)
    else:
        note = _ok([n for n in result.get("notes") or [] if isinstance(n, dict)], 1)

    when, minutes_until = _when(start, now)
    sources = {"attendees": attendees, "mail": mail, "files": files, "note": note}
    payload: dict[str, Any] = {
        "title": title,
        "start": start.isoformat(),
        "when": when,
        "minutes_until": minutes_until,
        "location": str(meta.get("location") or ""),
        "video_url": str(meta.get("video_url") or ""),
        "calendar": str(meta.get("calendar") or ""),
        **sources,
        "unavailable": [label for key, label in SOURCES if sources[key]["status"] != "ok"],
    }
    payload["text"] = format_prep(payload)
    return payload


# ---- what the orchestrator says about the event ----------------------------------------

def describe(event: ContextEvent) -> str:
    """One line for the ``event_received`` thought."""
    meta = event.metadata or {}

    def count(key: str, noun: str) -> str:
        src = meta.get(key) or {}
        if src.get("status") != "ok":
            return f"{noun} unavailable"
        return f"{len(src.get('items') or [])} {noun}"

    return (
        f"Meeting prep for {meta.get('title', '(untitled)')} at {meta.get('when', '?')}: "
        f"{count('attendees', 'attendees')}, {count('mail', 'emails')}, {count('files', 'files')}"
    )


def build_prompt(event: ContextEvent) -> str:
    """The main-engine prompt for a ``meeting_prep`` event."""
    text = str((event.metadata or {}).get("text") or "")
    return (
        "You are an on-device macOS assistant. A meeting starts soon. Write "
        "a short prep card for the user from the data below. The data is "
        "already gathered: do NOT call any tool, and do not ask for "
        "approval. Reply with the card as your final answer.\n\n"
        "Rules:\n"
        "- At most 6 short lines, plain text.\n"
        "- Line 1: the meeting title and time, then where (location or call link).\n"
        "- Who is in it.\n"
        "- The recent mail with them that matters most: sender and subject. "
        "Never guess what a message says beyond its subject.\n"
        "- Files and the note, by name, if any.\n"
        "- If a line says a source is unavailable, keep that source and its "
        "reason in a few words. Never drop it and never invent items.\n\n"
        f"Data:\n{text}"
    )


def make_event(payload: dict[str, Any]) -> ContextEvent:
    return ContextEvent(event_type=EVENT_TYPE, metadata=dict(payload))


# ---- scheduler -------------------------------------------------------------------------

class MeetingPrepScheduler:
    """Turn ``event_starting_soon`` into one ``meeting_prep`` event per meeting.

    :meth:`observe` is the idea-source hook the orchestrator calls for every
    event; it only schedules a timer (never blocks the event loop). The timer
    fires at ``start - minutes_before`` (at once when that has passed),
    re-checks the setting, gathers on its own thread and pushes the event.
    Each meeting (title + start) is prepped once.
    """

    def __init__(
        self,
        bus: EventBus,
        clock: Callable[[], datetime] = _now,
        gather: Optional[Callable[[dict[str, Any], datetime], Optional[dict[str, Any]]]] = None,
        enabled: Callable[[], bool] = prep_enabled,
        lead_minutes: Callable[[], int] = minutes_before,
        timer: Callable[[float, Callable[[], None]], Any] | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._gather = gather or gather_prep
        self._enabled = enabled
        self._lead_minutes = lead_minutes
        self._timer = timer or self._thread_timer
        self._seen: dict[tuple[str, str], datetime] = {}
        self._pending: list[Any] = []
        self._lock = threading.Lock()
        self._stopped = False

    @staticmethod
    def _thread_timer(delay: float, fn: Callable[[], None]) -> Any:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.name = "MeetingPrep"
        t.start()
        return t

    # listener interface (main.py keeps listeners and stops them at shutdown)
    def start(self) -> None:
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True
        with self._lock:
            pending, self._pending = self._pending, []
        for t in pending:
            cancel = getattr(t, "cancel", None)
            if cancel is not None:
                cancel()

    def delay_for(self, meta: dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
        """Seconds to wait before gathering, or None when there is no prep."""
        now = now or self._clock()
        if not self._enabled() or skip_reason(meta, now):
            return None
        start = _parse(meta.get("start"))
        assert start is not None
        lead = max(MIN_MINUTES, min(MAX_MINUTES, int(self._lead_minutes())))
        return max(0.0, (start - timedelta(minutes=lead) - now).total_seconds())

    def observe(self, event: ContextEvent) -> Optional[float]:
        """Schedule prep for an ``event_starting_soon``; return the delay used."""
        if self._stopped or event.event_type != TRIGGER_EVENT_TYPE:
            return None
        meta = dict(event.metadata or {})
        now = self._clock()
        delay = self.delay_for(meta, now)
        if delay is None:
            return None
        key = (str(meta.get("title") or ""), str(meta.get("start") or ""))
        with self._lock:
            if key in self._seen:
                return None
            self._seen[key] = _parse(meta.get("start")) or now
            self._prune(now)
            handle = self._timer(delay, lambda: self._fire(meta))
            self._pending.append(handle)
        return delay

    def _fire(self, meta: dict[str, Any]) -> Optional[ContextEvent]:
        if self._stopped or not self._enabled():
            return None
        try:
            payload = self._gather(meta, self._clock())
        except Exception:
            logger.exception("meeting prep gather failed")
            return None
        if payload is None:
            return None
        event = make_event(payload)
        try:
            self._bus.push_threadsafe(event)
        except Exception:  # pragma: no cover - loop not bound / shutting down
            logger.exception("Failed to push meeting_prep")
            return None
        return event

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=1)
        for key in [k for k, start in self._seen.items() if start < cutoff]:
            del self._seen[key]
        self._pending = [t for t in self._pending if getattr(t, "is_alive", lambda: True)()]
