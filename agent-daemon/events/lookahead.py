"""Look-ahead gap check: what is missing for the next few days (#77).

Run once a day with the morning brief (#65). :func:`check_gaps` reads the
next ``days`` (default 3) of calendar events, recent mail headers and the
user's reminders, and runs four small deterministic detectors:

* ``unopened_file`` (a): a meeting whose notes / URL link a document the
  user has not opened. Needs a file-open history; the #75 activity store
  records file *creates and renames* by folder and extension only (never
  names, never opens), so by default this detector is skipped and named.
  A history can be injected as ``file_opened(ref)``.
* ``travel`` (b): a flight / train / hotel event with no confirmation mail
  for that leg in the last 60 days of mail headers, or an outbound flight
  that stays overnight with no hotel booking in mail or on the calendar.
* ``deadline`` (c): a mail matching the #32 ``mail_deadline`` rule whose
  date falls in the window and has no matching reminder or event on or
  before it.
* ``schedule`` (d): two timed events that overlap, or back-to-back events
  (gap <= 15 min) in two different physical places.

Each gap is one line with its evidence and one offer: a built-in idea
(``pattern_detected`` metadata with ``builtin_tool`` = ``create_reminder``,
``open_url`` or ``summarize_file``), so acting on it goes through the
normal approval card. No gap, no line. When a detector cannot tell, it says
nothing (invariant 7d). A source that is unavailable skips every detector
that needs it, and the skip is named.

Read-only and local (7a / 7c): nothing here writes, notifies or asks for
approval. Mail is headers only (sender + subject); every subject shown goes
through ``ai.privacy.redact_text`` for the engines that read the brief, as
#65 does. Event titles are shown as in the brief.
"""

from __future__ import annotations

import calendar as _calendar
import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from .event_bus import ContextEvent

SOURCE_NAME = "lookahead"
DEFAULT_DAYS = 3
TRAVEL_MAIL_WINDOW = timedelta(days=60)
DEADLINE_MAIL_WINDOW = timedelta(days=14)
# Flights before the window: an upcoming flight after one of these is
# probably the way home, so no hotel is expected.
TRAVEL_LOOKBACK_DAYS = 14
BACK_TO_BACK = timedelta(minutes=15)
MAX_GAPS = 6

DETECTORS: tuple[tuple[str, str], ...] = (
    ("unopened_file", "unopened files"),
    ("travel", "travel bookings"),
    ("deadline", "mail deadlines"),
    ("schedule", "clashes"),
)

NO_FILE_HISTORY = (
    "no file-open history (activity history records file creates and "
    "renames by folder and extension, not opens)"
)

_STOPWORDS = frozenset(
    "the and for with from your you our this that will please action required "
    "reminder request submit sign review complete respond rsvp approve send pay "
    "due before deadline today tonight tomorrow next week month end day re fwd fw "
    "monday tuesday wednesday thursday friday saturday sunday eod eow "
    "january february march april june july august september october november december "
    "meeting call sync".split()
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'&-]+")

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DEADLINE_DATE_RE: Optional[re.Pattern[str]] = None


def _deadline_date_re() -> re.Pattern[str]:
    global _DEADLINE_DATE_RE
    if _DEADLINE_DATE_RE is None:
        from ai.idea_sources import _DAY  # the #32 rule's own day words

        _DEADLINE_DATE_RE = re.compile(
            r"\b(?:by|due|before|deadline:?)\s+(on |this |next )?(" + _DAY + r")\b",
            re.IGNORECASE,
        )
    return _DEADLINE_DATE_RE


# ---- small helpers ---------------------------------------------------------------

def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _local(value: Any, now: datetime) -> Optional[datetime]:
    stamp = _parse(value)
    if stamp is None:
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=now.tzinfo)
    return stamp.astimezone(now.tzinfo)


def _day_text(day: date) -> str:
    return day.strftime("%A %B %-d")


def _clock(value: datetime) -> str:
    return value.strftime("%a %-I:%M %p")


def content_words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= 4 and w not in _STOPWORDS}


def _short(reason: str, limit: int = 160) -> str:
    reason = " ".join(str(reason).split())
    return reason if len(reason) <= limit else reason[: limit - 1] + "\u2026"


def _key(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Optional[dict[str, Any]], str]:
    """``(answer, "")`` on success, else ``(None, reason)``. Never raises."""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        return None, str(exc) or exc.__class__.__name__
    if not isinstance(result, dict):
        return None, "unexpected answer"
    if result.get("status") != "ok":
        return None, str(result.get("error") or result.get("status") or "no answer")
    return result, ""


def make_offer(trigger: str, name: str, tool: str, follow_up: str,
               details: str, details_redacted: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """A built-in idea, shaped like ``NextStepSuggester.builtin_idea``.

    Published as ``ContextEvent("pattern_detected", metadata=offer)`` the
    orchestrator asks the main engine to call ``tool``, which raises the
    normal approval card. ``event_details_redacted`` is sent instead when
    the main engine is a cloud one (#31). The follow-up text never holds a
    mail subject.
    """
    return {
        "tool_name": name,
        "description": follow_up,
        "expected_logic": follow_up,
        "builtin_tool": tool,
        "arguments": arguments,
        "event_details": details,
        "event_details_redacted": details_redacted,
        "source": SOURCE_NAME,
        "trigger": f"lookahead_{trigger}",
    }


def _gap(detector: str, line: str, evidence: dict[str, Any], offer: dict[str, Any], key: str) -> dict[str, Any]:
    return {"detector": detector, "line": line, "evidence": evidence, "offer": offer, "key": f"{detector}:{key}"}


# ---- normalised sources ------------------------------------------------------------

def _timed_events(events: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    out = []
    for ev in events:
        start, end = _local(ev.get("start"), now), _local(ev.get("end"), now)
        if start is None:
            continue
        out.append({**ev, "_start": start, "_end": end or start, "title": str(ev.get("title") or "")})
    return sorted(out, key=lambda e: e["_start"])


def _mail_items(messages: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        received = _local(m.get("date"), now)
        if received is None:
            continue
        out.append({**m, "_received": received,
                    "subject": " ".join(str(m.get("subject") or "").split()),
                    "sender": str(m.get("sender") or "unknown")})
    return out


# ---- (a) meeting with an unopened linked file ---------------------------------------

_DOC_URL_RE = re.compile(
    r"https://(?:docs\.google\.com|drive\.google\.com|[\w-]+\.sharepoint\.com|onedrive\.live\.com"
    r"|(?:www\.)?dropbox\.com|(?:www\.)?notion\.so|(?:www\.)?figma\.com|app\.box\.com|(?:www\.)?icloud\.com/(?:pages|numbers|keynote|iclouddrive))"
    r"/[^\s<>\"')]+",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?:file://)?((?:/Users/|~/)[^\s<>\"']+\.[A-Za-z0-9]{1,5})\b")


def linked_files(event: dict[str, Any]) -> list[str]:
    """Document links and local file paths in an event's notes and URL."""
    text = f"{event.get('url') or ''}\n{event.get('notes') or ''}"
    found = [m.group(0).rstrip(".,;") for m in _DOC_URL_RE.finditer(text)]
    found += [m.group(1).rstrip(".,;") for m in _PATH_RE.finditer(text)]
    return list(dict.fromkeys(found))


def detect_unopened_files(events: list[dict[str, Any]], file_opened: Callable[[str], Any],
                          now: datetime, window_end: datetime) -> list[dict[str, Any]]:
    gaps = []
    for ev in events:
        if not (now <= ev["_start"] < window_end) or ev.get("all_day"):
            continue
        for ref in linked_files(ev):
            answer, _ = _call(file_opened, ref)
            # Unknown is not "unopened": say nothing (7d).
            if answer is None or answer.get("opened") is not False:
                continue
            web = ref.startswith("https://")
            shown = ref if web else ref.rsplit("/", 1)[-1]
            line = f"{ev['title']} ({_clock(ev['_start'])}): linked file not opened yet: {shown}"
            if web:
                tool, args, follow = "open_url", {"url": ref}, "Open the meeting's linked document with the built-in open_url tool (asks for approval)."
            else:
                tool, args, follow = "summarize_file", {"path": ref}, "Show a preview of the meeting's linked file with the built-in summarize_file tool."
            details = f"Meeting: {ev['title']}\nStart: {ev['_start'].isoformat()}\nLinked file: {ref}"
            offer = make_offer("unopened_file", "open_meeting_file", tool, follow, details, details, args)
            gaps.append(_gap("unopened_file", line, {"title": ev["title"], "start": ev["_start"].isoformat(), "file": ref},
                             offer, _key(ev["title"], ev["_start"].isoformat(), ref)))
    return gaps


# ---- (b) travel without confirmations ------------------------------------------------

_AIRLINES = (
    r"united|delta|american airlines|southwest|jetblue|alaska air(?:lines)?|frontier|spirit airlines"
    r"|air canada|westjet|british airways|lufthansa|air france|klm|iberia|emirates|qatar|ryanair|easyjet"
    r"|aer lingus|virgin atlantic|qantas|singapore airlines|swiss|turkish airlines|hawaiian airlines"
)
_FLIGHT_CODES = r"(?:UA|DL|AA|WN|B6|AS|F9|NK|AC|WS|BA|LH|AF|KL|IB|EK|QR|FR|U2|EI|VS|QF|SQ|LX|TK|HA)\s?\d{1,4}"
_LEG_WORDS: dict[str, re.Pattern[str]] = {
    "flight": re.compile(r"\b(?:flight|fly(?:ing)? to|boarding pass|e-?ticket|" + _AIRLINES + r")\b"
                         r"|\b" + _FLIGHT_CODES + r"\b", re.IGNORECASE),
    "train": re.compile(r"\b(?:train|amtrak|eurostar|trainline|sncf|deutsche bahn|via rail|acela|rail ticket)\b",
                        re.IGNORECASE),
    "hotel": re.compile(r"\b(?:hotel|airbnb|motel|hostel|resort|vrbo|marriott|hilton|hyatt|ihg|holiday inn"
                        r"|sheraton|westin|booking\.com|hotels\.com|stay at)\b", re.IGNORECASE),
}
_CONFIRM_RE = re.compile(
    r"\b(?:confirm(?:ed|ation)?|itinerary|booking|booked|reservation|reserved|e-?ticket|ticket|"
    r"boarding pass|check-?in|receipt|trip)\b",
    re.IGNORECASE,
)
# Agencies sell every leg: a confirmation from one counts for any leg.
_AGENCY_RE = re.compile(r"\b(?:expedia|priceline|kayak|orbitz|travelocity|tripit|hopper|google travel)\b", re.IGNORECASE)
_LEG_NAMES = {"flight": "flight", "train": "train", "hotel": "hotel"}


def legs_of(text: str) -> set[str]:
    return {leg for leg, rx in _LEG_WORDS.items() if rx.search(text)}


def _confirmation_legs(mail: list[dict[str, Any]], since: datetime) -> set[str]:
    legs: set[str] = set()
    for m in mail:
        if m["_received"] < since:
            continue
        text = f"{m['subject']} {m['sender']}"
        if not _CONFIRM_RE.search(m["subject"]):
            continue
        found = legs_of(text)
        if not found and _AGENCY_RE.search(text):
            found = set(_LEG_WORDS)
        legs |= found
    return legs


def detect_travel(events: list[dict[str, Any]], mail: list[dict[str, Any]],
                  now: datetime, window_end: datetime) -> list[dict[str, Any]]:
    confirmed = _confirmation_legs(mail, now - TRAVEL_MAIL_WINDOW)
    trips = [(ev, legs_of(f"{ev['title']} {ev.get('location') or ''}")) for ev in events]
    trips = [(ev, legs) for ev, legs in trips if legs]
    upcoming = [(ev, legs) for ev, legs in trips if now <= ev["_start"] < window_end]
    gaps: list[dict[str, Any]] = []

    def offer_for(ev: dict[str, Any], leg: str, what: str) -> dict[str, Any]:
        title = f"Check {leg} booking for {ev['title']}"
        due = ev["_start"].date().isoformat()
        details = (f"Upcoming travel event: {ev['title']}\nStart: {ev['_start'].isoformat()}\n"
                   f"Gap: {what}\nSuggested reminder: title={title!r}, due={(now.date()).isoformat()}")
        follow = ("Add a reminder to check the travel booking with the built-in "
                  "create_reminder tool (asks for approval).")
        return make_offer("travel", f"remind_{leg}_booking", "create_reminder", follow, details, details,
                          {"title": title, "due": now.date().isoformat(), "notes": f"Trip date {due}"})

    # 1. a leg on the calendar with no confirmation mail for it.
    for ev, legs in upcoming:
        for leg in sorted(legs - confirmed):
            what = f"no {leg} confirmation in mail from the last 60 days"
            gaps.append(_gap("travel", f"{ev['title']} ({_clock(ev['_start'])}): {what}",
                             {"title": ev["title"], "start": ev["_start"].isoformat(), "leg": leg},
                             offer_for(ev, leg, what), _key("leg", ev["title"], ev["_start"].isoformat(), leg)))
    # 2. an outbound flight that stays overnight with no hotel anywhere.
    flights = [ev for ev, legs in trips if "flight" in legs]
    hotel_events = [ev for ev, legs in trips if "hotel" in legs]
    if "hotel" not in confirmed and not hotel_events:
        for ev, legs in upcoming:
            if "flight" not in legs or "hotel" in legs:
                continue
            before = [f for f in flights if f is not ev and ev["_start"] - timedelta(days=TRAVEL_LOOKBACK_DAYS) <= f["_start"] < ev["_start"]]
            if before:
                continue  # probably the way home
            same_day_back = [e for e, l in trips if e is not ev and (l & {"flight", "train"})
                             and e["_start"] > ev["_start"] and e["_start"].date() == ev["_start"].date()]
            if same_day_back:
                continue  # day trip
            what = "no hotel booking in mail from the last 60 days or on the calendar"
            gaps.append(_gap("travel", f"{ev['title']} ({_clock(ev['_start'])}): {what}",
                             {"title": ev["title"], "start": ev["_start"].isoformat(), "leg": "hotel"},
                             offer_for(ev, "hotel", what), _key("hotel", ev["title"], ev["_start"].isoformat())))
            break  # one trip, one hotel line
    return gaps


# ---- (c) mail deadline with no reminder ----------------------------------------------

def _is_deadline_mail(m: dict[str, Any]) -> bool:
    from ai.idea_sources import NextStepSuggester  # the #32 rule, unchanged

    event = ContextEvent(event_type="mail_received",
                         metadata={"subject": m["subject"], "mailbox": m.get("mailbox") or "",
                                   "sender": m["sender"], "message_id": m.get("message_id")})
    return NextStepSuggester.classify(event) == "mail_deadline"


def _month_index(word: str) -> Optional[int]:
    w = word.lower().rstrip(".")
    for i, abbr in enumerate(_MONTHS):
        full = _calendar.month_name[i + 1].lower()
        if w.startswith(abbr) and (full.startswith(w) or w == "sept"):
            return i + 1
    return None


def deadline_date(subject: str, received: date) -> Optional[date]:
    """The date a deadline subject names, read against the day it arrived.

    ``None`` when there is no date or it is ambiguous ("next Friday").
    """
    match = _deadline_date_re().search(subject)
    if match is None:
        return None
    if (match.group(1) or "").strip().lower() == "next":
        return None
    text = match.group(2).lower().strip().rstrip(".")
    if text in {"today", "tonight", "eod", "end of day", "end of the day"}:
        return received
    if text == "tomorrow":
        return received + timedelta(days=1)
    if text in {"eow", "end of week", "end of the week"}:
        if received.weekday() > 4:
            return None
        return received + timedelta(days=4 - received.weekday())
    if text in {"end of month", "end of the month"}:
        return received.replace(day=_calendar.monthrange(received.year, received.month)[1])
    for i, abbr in enumerate(_WEEKDAYS):
        if text.startswith(abbr) and re.fullmatch(r"[a-z]+", text):
            return received + timedelta(days=(i - received.weekday()) % 7)
    md = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text)
    if md:
        month, day, year = int(md.group(1)), int(md.group(2)), md.group(3)
        y = (2000 + int(year) if len(year) == 2 else int(year)) if year else received.year
        return _safe_date(y, month, day, received, explicit_year=bool(year))
    parts = text.split()
    if len(parts) == 2:
        month = _month_index(parts[0])
        day = re.match(r"\d{1,2}", parts[1])
        if month and day:
            return _safe_date(received.year, month, int(day.group(0)), received, explicit_year=False)
    return None


def _safe_date(year: int, month: int, day: int, received: date, explicit_year: bool) -> Optional[date]:
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    if not explicit_year and found < received - timedelta(days=30):
        try:
            found = date(year + 1, month, day)
        except ValueError:
            return None
    return found


def _covered(words: set[str], due: date, reminders: list[dict[str, Any]], events: list[dict[str, Any]]) -> bool:
    """A reminder (dated on or before ``due``, or undated) or an event on or before ``due`` shares a word."""
    for r in reminders:
        if not content_words(str(r.get("title") or "")) & words:
            continue
        raw = str(r.get("due") or "")
        if not raw:
            return True  # the user has it written down; unsure means quiet
        try:
            if date.fromisoformat(raw[:10]) <= due:
                return True
        except ValueError:
            return True
    for ev in events:
        if ev["_start"].date() <= due and content_words(ev["title"]) & words:
            return True
    return False


def detect_deadlines(mail: list[dict[str, Any]], reminders: list[dict[str, Any]], events: list[dict[str, Any]],
                     now: datetime, last_day: date, redact: Callable[[str], str]) -> list[dict[str, Any]]:
    gaps = []
    seen: set[tuple[str, date]] = set()
    for m in sorted(mail, key=lambda x: x["_received"], reverse=True):
        if m["_received"] < now - DEADLINE_MAIL_WINDOW or not m["subject"]:
            continue
        if not _is_deadline_mail(m):
            continue
        due = deadline_date(m["subject"], m["_received"].date())
        if due is None or not (now.date() <= due <= last_day):
            continue
        words = content_words(m["subject"])
        if not words or _covered(words, due, reminders, events):
            continue  # nothing to match on, or already handled
        if (m["subject"].lower(), due) in seen:
            continue
        seen.add((m["subject"].lower(), due))
        shown = redact(m["subject"])
        sender = m["sender"].split(" <")[0]
        line = f"No reminder for “{shown}” (from {sender}, due {_day_text(due)}) [id {m.get('message_id')}]"
        follow = ("Read the email with the built-in read_email tool, then add a reminder "
                  "with the built-in create_reminder tool (title from the request, due "
                  "from the deadline); it asks the user for approval.")

        def details(subject: str) -> str:
            return (f"An email asks for something by a date with no reminder set.\nFrom: {m['sender']}\n"
                    f"Subject: {subject[:200]}\nDue: {due.isoformat()}\nmessage_id: {m.get('message_id')}")

        from ai.privacy import PLACEHOLDER

        offer = make_offer("deadline", "remind_email_deadline", "create_reminder", follow,
                           details(m["subject"]), details(PLACEHOLDER),
                           {"due": due.isoformat(), "message_id": m.get("message_id")})
        gaps.append(_gap("deadline", line, {"message_id": m.get("message_id"), "sender": m["sender"],
                                            "subject": shown, "due": due.isoformat()},
                         offer, _key(m.get("message_id"), due.isoformat())))
    return gaps


# ---- (d) overlaps and back-to-back in different places -------------------------------

_VIRTUAL_RE = re.compile(r"https?://|zoom|meet\.google|google meet|teams|webex|facetime|phone|online|virtual|remote|tbd|slack|call",
                         re.IGNORECASE)


def _place(ev: dict[str, Any]) -> str:
    loc = " ".join(str(ev.get("location") or "").split())
    return "" if not loc or _VIRTUAL_RE.search(loc) else loc.lower()


def detect_schedule(events: list[dict[str, Any]], now: datetime, window_end: datetime) -> list[dict[str, Any]]:
    timed = [e for e in events if not e.get("all_day") and e["_end"] > e["_start"]
             and e["_end"] > now and e["_start"] < window_end]
    unique: list[dict[str, Any]] = []
    for e in timed:  # the same meeting on two calendars is not a clash
        if any(u["title"].lower() == e["title"].lower() and u["_start"] == e["_start"] for u in unique):
            continue
        unique.append(e)
    gaps = []
    for i, a in enumerate(unique):
        for b in unique[i + 1:]:
            if b["_start"] >= a["_end"] + BACK_TO_BACK:
                break  # sorted by start: no later b is closer
            if b["_start"] < a["_end"]:
                kind = "overlap"
                line = f"{a['title']} ({_clock(a['_start'])}) overlaps {b['title']} ({_clock(b['_start'])})"
            else:
                pa, pb = _place(a), _place(b)
                if not pa or not pb or pa == pb:
                    continue  # unknown or same place: say nothing
                kind = "back_to_back"
                mins = int((b["_start"] - a["_end"]).total_seconds() // 60)
                line = (f"{a['title']} ends {_clock(a['_end'])} at {a.get('location')}, {b['title']} starts "
                        f"{mins} min later at {b.get('location')}")
            title = f"Sort out {a['title']} vs {b['title']}"
            details = (f"Calendar {kind.replace('_', ' ')}: {a['title']} {a['_start'].isoformat()}-{a['_end'].isoformat()} "
                       f"({a.get('location') or 'no place'}) and {b['title']} {b['_start'].isoformat()}-"
                       f"{b['_end'].isoformat()} ({b.get('location') or 'no place'}).\n"
                       f"Suggested reminder: title={title!r}, due={now.date().isoformat()}")
            follow = ("Add a reminder to sort out the calendar clash with the built-in "
                      "create_reminder tool (asks for approval).")
            offer = make_offer("schedule", "remind_calendar_clash", "create_reminder", follow, details, details,
                               {"title": title, "due": now.date().isoformat()})
            gaps.append(_gap("schedule", line, {"kind": kind, "first": a["title"], "second": b["title"],
                                                "first_start": a["_start"].isoformat(), "second_start": b["_start"].isoformat()},
                             offer, _key(kind, a["title"], a["_start"].isoformat(), b["title"], b["_start"].isoformat())))
    return gaps


# ---- default sources ----------------------------------------------------------------

def _default_list_events(start: str = "", end: str = "") -> dict[str, Any]:
    from .daily_brief import _default_list_events as brief_events  # never prompts

    return brief_events(start=start, end=end)


def _default_list_reminders(list_name: str = "", include_completed: bool = False) -> dict[str, Any]:
    from .daily_brief import _default_list_reminders as brief_reminders  # never prompts

    return brief_reminders(list_name=list_name, include_completed=include_completed)


def _default_recent_mail(since: datetime) -> dict[str, Any]:
    from .daily_brief import recent_mail  # headers only, read-only

    return recent_mail(since)


def _default_redact(subject: str) -> str:
    from .daily_brief import _redact_subject

    return _redact_subject(subject)


# ---- entry point ----------------------------------------------------------------------

def format_gaps(gaps: list[dict[str, Any]], skipped: list[dict[str, Any]], days: int) -> str:
    """The brief section: gap lines, then one line naming skipped checks. "" when neither."""
    lines = []
    if gaps:
        lines.append(f"Look-ahead, next {days} days ({len(gaps)}):")
        lines += [f"- {g['line']}" for g in gaps]
    if skipped:
        lines.append("Look-ahead skipped: " + "; ".join(f"{s['label']} ({s['reason']})" for s in skipped))
    return "\n".join(lines)


def check_gaps(
    now: Optional[datetime] = None,
    *,
    days: int = DEFAULT_DAYS,
    list_events: Optional[Callable[..., Any]] = None,
    recent_mail: Optional[Callable[..., Any]] = None,
    list_reminders: Optional[Callable[..., Any]] = None,
    file_opened: Optional[Callable[[str], Any]] = None,
    redact_subject: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    """Find what is missing for the next ``days`` days. Never raises.

    Sources (injectable; tests pass fakes): ``list_events(start=, end=)``
    and ``list_reminders(list_name=, include_completed=)`` answer like the
    tools; ``recent_mail(since)`` like ``daily_brief.recent_mail``;
    ``file_opened(ref)`` answers ``{"status": "ok", "opened": bool}`` and
    defaults to none (detector ``unopened_file`` skipped and named).

    Returns ``{"status": "ok", "days", "gaps": [{detector, line, evidence,
    offer, key}], "skipped": [{detector, label, reason}], "text"}``; each
    ``offer`` is ``pattern_detected`` metadata for a built-in tool.
    """
    now = now or datetime.now().astimezone()
    days = max(1, int(days))
    redact = redact_subject or _default_redact
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end = today + timedelta(days=days)
    last_day = (window_end - timedelta(days=1)).date()

    ev_answer, ev_reason = _call(
        list_events or _default_list_events,
        start=(today - timedelta(days=TRAVEL_LOOKBACK_DAYS)).date().isoformat(), end=last_day.isoformat(),
    )
    mail_answer, mail_reason = _call(recent_mail or _default_recent_mail, now - TRAVEL_MAIL_WINDOW)
    # Open reminders only: listing completed ones through AppleScript took
    # over 60 s on a real account (smoke run), and the brief reads the same.
    rem_answer, rem_reason = _call(list_reminders or _default_list_reminders, list_name="", include_completed=False)
    if rem_answer is not None and rem_answer.get("truncated"):
        rem_answer, rem_reason = None, "too many reminders to check them all"

    events = _timed_events(ev_answer.get("events") or [], now) if ev_answer else []
    mail = _mail_items(mail_answer.get("messages") or [], now) if mail_answer else []
    reminders = list(rem_answer.get("reminders") or []) if rem_answer else []

    gaps: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    labels = dict(DETECTORS)

    def run(detector: str, needs: list[tuple[Optional[dict[str, Any]], str, str]], fn: Callable[[], list]) -> None:
        missing = [f"{name} unavailable: {_short(reason)}" for answer, name, reason in needs if answer is None]
        if missing:
            skipped.append({"detector": detector, "label": labels[detector], "reason": "; ".join(missing)})
            return
        try:
            gaps.extend(fn())
        except Exception as exc:  # one detector must never sink the others
            skipped.append({"detector": detector, "label": labels[detector], "reason": f"failed ({exc})"})

    cal = (ev_answer, "calendar", ev_reason)
    if file_opened is None:
        skipped.append({"detector": "unopened_file", "label": labels["unopened_file"], "reason": NO_FILE_HISTORY})
    else:
        run("unopened_file", [cal], lambda: detect_unopened_files(events, file_opened, now, window_end))
    run("travel", [cal, (mail_answer, "mail", mail_reason)], lambda: detect_travel(events, mail, now, window_end))
    run("deadline", [(mail_answer, "mail", mail_reason), (rem_answer, "reminders", rem_reason), cal],
        lambda: detect_deadlines(mail, reminders, events, now, last_day, redact))
    run("schedule", [cal], lambda: detect_schedule(events, now, window_end))

    gaps = gaps[:MAX_GAPS]
    return {"status": "ok", "days": days, "gaps": gaps, "skipped": skipped,
            "text": format_gaps(gaps, skipped, days)}


def offer_events(result: dict[str, Any]) -> list[ContextEvent]:
    """One ``pattern_detected`` event per gap offer, for the orchestrator's normal card flow."""
    return [ContextEvent(event_type="pattern_detected", metadata={**g["offer"], "lookahead_key": g["key"]})
            for g in result.get("gaps") or []]
