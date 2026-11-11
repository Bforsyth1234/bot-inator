"""Shared access to the Mac's calendars: the menu bar app's EventKit (#60).

One interface, three backends (#42, #60):

* :class:`EventKitBackend` talks to Apple's EventKit through
  ``pyobjc-framework-EventKit``. It sees every calendar the Calendar app
  syncs (iCloud, Google, Exchange), expands recurring events, and keeps all
  data on the Mac. macOS asks for Calendar access once per responsible
  process (the terminal or IDE that started the daemon).
* :class:`AppleScriptBackend` drives Calendar.app with ``osascript``. It
  needs the Automation permission instead, is slow (seconds for a week),
  so ranges are capped at 14 days, and it does not expand recurring events.
  As in ``tools/notes.py`` the scripts are fixed constants and every user
  value reaches them as ``osascript`` arguments read with ``on run argv``.
* :class:`AppCalendarBackend` (#60, the default) asks the menu bar app,
  which owns EventKit and can show the normal Calendar prompt, over the
  daemon's WebSocket (``calendar_request`` / ``calendar_response``, see
  :class:`AppCalendarBridge`). The daemon's own Python is silently denied
  EventKit (no Info.plist usage string).

Backend selection: ``AGENT_CALENDAR_BACKEND`` = ``app``, ``eventkit``,
``applescript`` or ``auto`` (default). ``auto`` = ``app``: when the app
isn't connected calendar tools answer "open the bot-inator menu bar app to
use Calendar" (no slow AppleScript fallback). ``eventkit`` / ``applescript``
are explicit pins; ``python tools/calendar_access.py --check`` from
``agent-daemon`` diagnoses them.

Dates are ISO 8601 in local time. A naive datetime is local; output always
carries the local UTC offset. Events come back as ``{id, title, start, end,
calendar, location, notes (first 500 chars), url, all_day}``, plus
``attendees`` (#80) when the backend knows them: a list of ``{name, email,
is_self}`` (``email`` from the participant's ``mailto:`` URL, "" when it has
none; rooms and resources left out; at most ``ATTENDEES_LIMIT``). An event
with no other people has ``attendees: []``; the key is ABSENT when the
backend can't tell (the AppleScript backend, or a menu bar app older than
#80), so a reader can tell "nobody else" from "unknown".

Reminders (#45) live beside events: both backends also implement
``list_reminders`` / ``create_reminder`` / ``reminder_lists`` (EventKit with
``EKEntityTypeReminder``, a separate macOS permission; or Reminders.app via
AppleScript). Selection uses ``AGENT_REMINDERS_BACKEND``, falling back to
``AGENT_CALENDAR_BACKEND``; ``auto`` checks the *Reminders* authorization.
Reminders come back as ``{id, title, due, all_day, list, notes (first 500
chars), completed}``; ``due`` is ``""`` (none), ``YYYY-MM-DD`` (all-day) or
an ISO datetime with offset. Diagnostic: ``--check --reminders``.
"""

from __future__ import annotations

import asyncio
import builtins
import concurrent.futures
import itertools
import os
import subprocess
import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Protocol, Sequence

ENV_BACKEND = "AGENT_CALENDAR_BACKEND"
ENV_REMINDERS_BACKEND = "AGENT_REMINDERS_BACKEND"
NOTES_LIMIT = 500
ATTENDEES_LIMIT = 50
# osascript blocks on the first-use Automation prompt until it is answered.
_OSASCRIPT_TIMEOUT_S = 60
_ACCESS_TIMEOUT_S = 120
# EventKit's reminder fetch is asynchronous; never wait on it forever.
_FETCH_TIMEOUT_S = 30
# ASCII unit / record separators: never typed in event text.
_US = "\x1f"
_RS = "\x1e"


class CalendarAccessError(RuntimeError):
    """Calendar access is unavailable (permission, missing backend, ...)."""


# ---- dates -----------------------------------------------------------------


def parse_local(value: str) -> datetime:
    """Parse ISO 8601 (date or datetime) into an aware local datetime."""
    text = (value or "").strip()
    if not text:
        raise ValueError("empty date")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return _local(parsed)


def is_date_only(value: str) -> bool:
    text = (value or "").strip()
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _local(dt: datetime) -> datetime:
    """Aware datetime in the local zone (naive is taken as local)."""
    return dt.astimezone() if dt.tzinfo else dt.astimezone()


def iso(dt: datetime) -> str:
    return _local(dt).isoformat(timespec="seconds")


def email_from_url(url: Any) -> str:
    """The address in a participant URL (``mailto:a@b.c`` -> ``a@b.c``); "" otherwise."""
    from urllib.parse import unquote

    text = str(url or "").strip()
    if text.lower().startswith("mailto:"):
        text = unquote(text[len("mailto:"):]).split("?", 1)[0].strip()
    return text.lower() if "@" in text and " " not in text and ":" not in text else ""


def clean_attendees(raw: Any) -> Optional[builtins.list[dict[str, Any]]]:
    """Normalize attendees to ``[{name, email, is_self}]``; None stays None (unknown).

    Accepts dicts with ``email`` or a ``url`` (``mailto:``). Entries with
    neither a name nor an address are dropped; duplicates (same address) too.
    """
    if raw is None or not isinstance(raw, (builtins.list, tuple)):
        return None
    out: builtins.list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        email = email_from_url(item.get("email") or item.get("url") or "")
        name = str(item.get("name") or "").strip()
        if not (email or name):
            continue
        key = email or "name:" + name
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "email": email, "is_self": bool(item.get("is_self"))})
        if len(out) >= ATTENDEES_LIMIT:
            break
    return out


def make_event(
    *, id: str = "", title: str = "", start: datetime, end: datetime,
    calendar: str = "", location: str = "", notes: str = "", url: str = "",
    all_day: bool = False, attendees: Any = None,
) -> dict[str, Any]:
    event = {
        "id": id or "",
        "title": title or "",
        "start": iso(start),
        "end": iso(end),
        "calendar": calendar or "",
        "location": location or "",
        "notes": (notes or "")[:NOTES_LIMIT],
        "url": url or "",
        "all_day": bool(all_day),
    }
    people = clean_attendees(attendees)
    if people is not None:  # absent = the backend can't tell (#80)
        event["attendees"] = people
    return event


def make_reminder(
    *, id: str = "", title: str = "", due: Optional[datetime] = None,
    all_day: bool = False, list: str = "", notes: str = "", completed: bool = False,
) -> dict[str, Any]:
    if due is None:
        due_text = ""
    elif all_day:
        due_text = due.date().isoformat() if isinstance(due, datetime) else due.isoformat()
    else:
        due_text = iso(due)
    return {
        "id": id or "",
        "title": title or "",
        "due": due_text,
        "all_day": bool(all_day and due is not None),
        "list": list or "",
        "notes": (notes or "")[:NOTES_LIMIT],
        "completed": bool(completed),
    }


def sort_reminders(items: "builtins.list[dict[str, Any]]") -> "builtins.list[dict[str, Any]]":
    """Due first (soonest first), undated last, then by title."""
    return sorted(items, key=lambda r: (r["due"] == "", r["due"], r["title"].lower()))


# ---- interface ---------------------------------------------------------------


class CalendarBackend(Protocol):
    name: str
    max_range_days: int

    def ready(self) -> bool:
        """True when reading needs no permission prompt (watcher gate)."""

    def list(
        self, start: datetime, end: datetime, calendars: Optional[Sequence[str]] = None,
    ) -> builtins.list[dict[str, Any]]:
        """Events overlapping ``[start, end)``, sorted by start."""

    def create(
        self, title: str, start: datetime, end: datetime, calendar: Optional[str] = None,
        location: Optional[str] = None, notes: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create an event; return ``{status, event_id, calendar}``."""


class RemindersBackend(Protocol):
    name: str

    def reminder_lists(self) -> builtins.list[str]:
        """Names of the reminder lists."""

    def list_reminders(
        self, lists: Optional[Sequence[str]] = None, include_completed: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        """Reminders in ``lists`` (all lists when empty)."""

    def create_reminder(
        self, title: str, due: Optional[datetime] = None, all_day: bool = False,
        list_name: Optional[str] = None, notes: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a reminder; return ``{status, reminder_id, list}``."""


# ---- EventKit ----------------------------------------------------------------

# EKAuthorizationStatus values (EventKit headers).
_STATUS_NAMES = {
    0: "not_determined", 1: "restricted", 2: "denied",
    3: "full_access", 4: "write_only",
}


def _eventkit():
    try:
        import EventKit  # type: ignore
        import Foundation  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on install
        raise CalendarAccessError(
            "pyobjc-framework-EventKit is not installed"
        ) from exc
    return EventKit, Foundation


def eventkit_available() -> bool:
    try:
        _eventkit()
    except CalendarAccessError:
        return False
    return True


class EventKitBackend:
    name = "eventkit"
    max_range_days = 31

    def __init__(self) -> None:
        self._store: Any = None
        self._lock = threading.Lock()

    # access ---------------------------------------------------------------

    def status(self, entity: str = "event") -> str:
        """Authorization status name. Reading it never prompts."""
        ek, _ = _eventkit()
        code = int(ek.EKEventStore.authorizationStatusForEntityType_(self._entity(entity)))
        return _STATUS_NAMES.get(code, f"unknown({code})")

    @staticmethod
    def _entity(entity: str) -> int:
        ek, _ = _eventkit()
        return ek.EKEntityTypeReminder if entity == "reminder" else ek.EKEntityTypeEvent

    def ready(self) -> bool:
        try:
            return self.status() == "full_access"
        except CalendarAccessError:
            return False

    def _get_store(self) -> Any:
        ek, _ = _eventkit()
        with self._lock:
            if self._store is None:
                self._store = ek.EKEventStore.alloc().init()
            return self._store

    def request_access(self, entity: str = "event") -> bool:
        """Ask macOS for full access (prompts once if not yet determined)."""
        store = self._get_store()
        done = threading.Event()
        result: dict[str, Any] = {"granted": False, "error": None}

        def completion(granted: bool, error: Any) -> None:
            result["granted"] = bool(granted)
            result["error"] = error
            done.set()

        if entity == "reminder" and hasattr(store, "requestFullAccessToRemindersWithCompletion_"):
            store.requestFullAccessToRemindersWithCompletion_(completion)
        elif entity == "event" and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(completion)
        else:  # macOS 13 and older
            store.requestAccessToEntityType_completion_(self._entity(entity), completion)
        if not done.wait(_ACCESS_TIMEOUT_S):
            raise CalendarAccessError("timed out waiting for the Calendar permission prompt")
        if result["granted"]:
            store.reset()  # pick up calendars now visible
        return result["granted"]

    def _ensure_access(self, entity: str = "event", *, write: bool = False) -> Any:
        status = self.status(entity)
        if status == "not_determined":
            self.request_access(entity)
            status = self.status(entity)
        ok = {"full_access"} | ({"write_only"} if write else set())
        if status not in ok:
            if entity == "reminder":
                raise CalendarAccessError(
                    f"Reminders access is {status} for this process. Allow it in System "
                    "Settings > Privacy & Security > Reminders (for the app that runs "
                    f"the daemon), or set {ENV_REMINDERS_BACKEND}=applescript."
                )
            raise CalendarAccessError(
                f"Calendar access is {status} for this process. Allow it in System "
                "Settings > Privacy & Security > Calendars (for the app that runs "
                "the daemon), or set AGENT_CALENDAR_BACKEND=applescript."
            )
        return self._get_store()

    # helpers ----------------------------------------------------------------

    @staticmethod
    def _nsdate(dt: datetime) -> Any:
        _, foundation = _eventkit()
        return foundation.NSDate.dateWithTimeIntervalSince1970_(_local(dt).timestamp())

    @staticmethod
    def _from_nsdate(value: Any) -> datetime:
        return datetime.fromtimestamp(float(value.timeIntervalSince1970())).astimezone()

    def _calendar_named(self, store: Any, name: str) -> Any:
        ek, _ = _eventkit()
        for cal in store.calendarsForEntityType_(ek.EKEntityTypeEvent) or []:
            if str(cal.title()) == name:
                return cal
        return None

    # EKParticipantType: rooms and resources are not people to prep for.
    _NOT_PEOPLE = (2, 3)  # EKParticipantTypeResource, EKParticipantTypeRoom

    @classmethod
    def _participant(cls, p: Any) -> Optional[dict[str, Any]]:
        try:
            if int(p.participantType()) in cls._NOT_PEOPLE:
                return None
        except Exception:
            pass
        url = p.URL()
        return {
            "name": str(p.name() or ""),
            "email": str(url.absoluteString()) if url is not None else "",
            "is_self": bool(p.isCurrentUser()),
        }

    @classmethod
    def _attendees(cls, ev: Any) -> builtins.list[dict[str, Any]]:
        """``EKEvent.attendees`` (+ the organizer when not listed); [] for a solo event."""
        people = [cls._participant(p) for p in (ev.attendees() or [])]
        organizer = ev.organizer() if hasattr(ev, "organizer") else None
        if organizer is not None and people:
            people.append(cls._participant(organizer))  # dropped by clean_attendees if a duplicate
        return [p for p in people if p is not None]

    def _to_dict(self, ev: Any) -> dict[str, Any]:
        url = ev.URL()
        cal = ev.calendar()
        return make_event(
            id=str(ev.eventIdentifier() or ""),
            title=str(ev.title() or ""),
            start=self._from_nsdate(ev.startDate()),
            end=self._from_nsdate(ev.endDate()),
            calendar=str(cal.title()) if cal is not None else "",
            location=str(ev.location() or ""),
            notes=str(ev.notes() or ""),
            url=str(url.absoluteString()) if url is not None else "",
            all_day=bool(ev.isAllDay()),
            attendees=self._attendees(ev),
        )

    # interface --------------------------------------------------------------

    def list(self, start, end, calendars=None):
        store = self._ensure_access("event")
        cals = None
        if calendars:
            cals = [c for c in (self._calendar_named(store, n) for n in calendars) if c is not None]
            if not cals:
                return []
        pred = store.predicateForEventsWithStartDate_endDate_calendars_(
            self._nsdate(start), self._nsdate(end), cals,
        )
        events = [self._to_dict(ev) for ev in (store.eventsMatchingPredicate_(pred) or [])]
        return sorted(events, key=lambda e: e["start"])

    def create(self, title, start, end, calendar=None, location=None, notes=None):
        ek, _ = _eventkit()
        store = self._ensure_access("event", write=True)
        if calendar:
            cal = self._calendar_named(store, calendar)
            if cal is None:
                raise CalendarAccessError(f"No calendar named {calendar!r}.")
        else:
            cal = store.defaultCalendarForNewEvents()
            if cal is None:
                raise CalendarAccessError("No default calendar for new events.")
        if not cal.allowsContentModifications():
            raise CalendarAccessError(f"Calendar {str(cal.title())!r} is read-only.")
        ev = ek.EKEvent.eventWithEventStore_(store)
        ev.setTitle_(title)
        ev.setStartDate_(self._nsdate(start))
        ev.setEndDate_(self._nsdate(end))
        ev.setCalendar_(cal)
        if location:
            ev.setLocation_(location)
        if notes:
            ev.setNotes_(notes)
        ok, err = store.saveEvent_span_commit_error_(ev, ek.EKSpanThisEvent, True, None)
        if not ok:
            raise CalendarAccessError(f"EventKit could not save the event: {err}")
        return {"status": "ok", "event_id": str(ev.eventIdentifier() or ""),
                "calendar": str(cal.title())}

    # reminders (#45) ----------------------------------------------------------

    def _reminder_calendars(self, store: Any) -> builtins.list[Any]:
        ek, _ = _eventkit()
        return builtins.list(store.calendarsForEntityType_(ek.EKEntityTypeReminder) or [])

    def reminder_lists(self) -> builtins.list[str]:
        store = self._ensure_access("reminder")
        return [str(c.title()) for c in self._reminder_calendars(store)]

    @staticmethod
    def _fetch_reminders(store: Any, predicate: Any, timeout: Optional[float] = None) -> builtins.list[Any]:
        """Run the asynchronous fetch and wait for it, at most ``timeout`` s."""
        done = threading.Event()
        box: dict[str, Any] = {"items": []}

        def completion(items: Any) -> None:
            box["items"] = builtins.list(items or [])
            done.set()

        ident = store.fetchRemindersMatchingPredicate_completion_(predicate, completion)
        if not done.wait(_FETCH_TIMEOUT_S if timeout is None else timeout):
            try:
                store.cancelFetchRequest_(ident)
            except Exception:
                pass
            raise CalendarAccessError("timed out waiting for EventKit to fetch reminders")
        return box["items"]

    @staticmethod
    def _due_components(due: datetime, all_day: bool) -> Any:
        _, foundation = _eventkit()
        d = _local(due)
        comps = foundation.NSDateComponents.alloc().init()
        comps.setCalendar_(foundation.NSCalendar.currentCalendar())
        comps.setYear_(d.year)
        comps.setMonth_(d.month)
        comps.setDay_(d.day)
        if not all_day:
            comps.setTimeZone_(foundation.NSTimeZone.localTimeZone())
            comps.setHour_(d.hour)
            comps.setMinute_(d.minute)
            comps.setSecond_(d.second)
        return comps

    def _due_of(self, rem: Any) -> tuple[Optional[datetime], bool]:
        _, foundation = _eventkit()
        comps = rem.dueDateComponents()
        if comps is None:
            return None, False
        undefined = foundation.NSDateComponentUndefined
        if int(comps.hour()) == undefined:
            return datetime(int(comps.year()), int(comps.month()), int(comps.day())).astimezone(), True
        cal = comps.calendar() or foundation.NSCalendar.currentCalendar()
        nsdate = cal.dateFromComponents_(comps)
        if nsdate is None:
            return None, False
        return self._from_nsdate(nsdate), False

    def _reminder_to_dict(self, rem: Any) -> dict[str, Any]:
        due, all_day = self._due_of(rem)
        cal = rem.calendar()
        return make_reminder(
            id=str(rem.calendarItemIdentifier() or ""),
            title=str(rem.title() or ""),
            due=due, all_day=all_day,
            list=str(cal.title()) if cal is not None else "",
            notes=str(rem.notes() or ""),
            completed=bool(rem.isCompleted()),
        )

    def list_reminders(self, lists=None, include_completed=False):
        store = self._ensure_access("reminder")
        cals = None
        if lists:
            wanted = set(lists)
            cals = [c for c in self._reminder_calendars(store) if str(c.title()) in wanted]
            if not cals:
                raise CalendarAccessError(f"No reminders list named {', '.join(lists)!r}.")
        if include_completed:
            pred = store.predicateForRemindersInCalendars_(cals)
        else:
            pred = store.predicateForIncompleteRemindersWithDueDateStarting_ending_calendars_(
                None, None, cals,
            )
        items = [self._reminder_to_dict(r) for r in self._fetch_reminders(store, pred)]
        return sort_reminders(items)

    def create_reminder(self, title, due=None, all_day=False, list_name=None, notes=None):
        ek, _ = _eventkit()
        store = self._ensure_access("reminder", write=True)
        if list_name:
            cal = next((c for c in self._reminder_calendars(store)
                        if str(c.title()) == list_name), None)
            if cal is None:
                raise CalendarAccessError(f"No reminders list named {list_name!r}.")
        else:
            cal = store.defaultCalendarForNewReminders()
            if cal is None:
                raise CalendarAccessError("No default list for new reminders.")
        if not cal.allowsContentModifications():
            raise CalendarAccessError(f"Reminders list {str(cal.title())!r} is read-only.")
        rem = ek.EKReminder.reminderWithEventStore_(store)
        rem.setTitle_(title)
        rem.setCalendar_(cal)
        if notes:
            rem.setNotes_(notes)
        if due is not None:
            rem.setDueDateComponents_(self._due_components(due, all_day))
            if not all_day:  # a timed reminder should actually alert at that time
                rem.addAlarm_(ek.EKAlarm.alarmWithAbsoluteDate_(self._nsdate(due)))
        ok, err = store.saveReminder_commit_error_(rem, True, None)
        if not ok:
            raise CalendarAccessError(f"EventKit could not save the reminder: {err}")
        return {"status": "ok", "reminder_id": str(rem.calendarItemIdentifier() or ""),
                "list": str(cal.title())}


# ---- AppleScript ---------------------------------------------------------------

# Shared handlers. Dates cross the boundary as "Y M D h m s" numbers (local
# time) so parsing never depends on the user's date-format locale.
_AS_HELPERS = '''
on mkDate(parts)
    set AppleScript's text item delimiters to " "
    set p to text items of parts
    set AppleScript's text item delimiters to ""
    set d to current date
    set day of d to 1
    set year of d to (item 1 of p) as integer
    set month of d to (item 2 of p) as integer
    set day of d to (item 3 of p) as integer
    set time of d to ((item 4 of p) as integer) * 3600 + ((item 5 of p) as integer) * 60 + ((item 6 of p) as integer)
    return d
end mkDate

on pad(n)
    set s to (n as integer) as text
    if (count of s) < 2 then set s to "0" & s
    return s
end pad

on isoOf(d)
    set t to time of d
    return ((year of d) as text) & "-" & my pad(month of d as integer) & "-" & my pad(day of d) & "T" & my pad(t div 3600) & ":" & my pad((t mod 3600) div 60) & ":" & my pad(t mod 60)
end isoOf

on txt(v)
    if v is missing value then return ""
    return v as text
end txt
'''

# argv: 1 = start parts, 2 = end parts, 3 = calendar names joined by US ("" = all).
_LIST_SCRIPT = _AS_HELPERS + '''
on run argv
    set US to character id 31
    set RS to character id 30
    set startD to my mkDate(item 1 of argv)
    set endD to my mkDate(item 2 of argv)
    set wanted to {}
    if (item 3 of argv) is not "" then
        set AppleScript's text item delimiters to US
        set wanted to text items of (item 3 of argv)
        set AppleScript's text item delimiters to ""
    end if
    set out to {}
    tell application "Calendar"
        repeat with c in calendars
            set cName to name of c as text
            if wanted is {} or wanted contains cName then
                repeat with e in (every event of c whose start date < endD and end date > startD)
                    set rec to {my txt(uid of e), my txt(summary of e), my isoOf(start date of e), my isoOf(end date of e), cName, my txt(location of e), my txt(description of e), my txt(url of e), (allday event of e) as text}
                    set AppleScript's text item delimiters to US
                    set end of out to rec as text
                    set AppleScript's text item delimiters to ""
                end repeat
            end if
        end repeat
    end tell
    set AppleScript's text item delimiters to RS
    set s to out as text
    set AppleScript's text item delimiters to ""
    return s
end run
'''

# argv: 1 = title, 2 = start parts, 3 = end parts, 4 = calendar ("" = first
# writable), 5 = location, 6 = notes.
_CREATE_SCRIPT = _AS_HELPERS + '''
on run argv
    set t to item 1 of argv
    set startD to my mkDate(item 2 of argv)
    set endD to my mkDate(item 3 of argv)
    set calName to item 4 of argv
    tell application "Calendar"
        if calName is "" then
            set cs to (every calendar whose writable is true)
            if (count of cs) is 0 then return "NO_CALENDAR"
            set c to item 1 of cs
        else
            if not (exists calendar calName) then return "NO_CALENDAR"
            set c to calendar calName
        end if
        set e to make new event at end of events of c with properties {summary:t, start date:startD, end date:endD}
        if (item 5 of argv) is not "" then set location of e to (item 5 of argv)
        if (item 6 of argv) is not "" then set description of e to (item 6 of argv)
        return "OK" & (character id 31) & (uid of e) & (character id 31) & (name of c)
    end tell
end run
'''


# Reminders.app (#45). argv: 1 = list name ("" = all), 2 = "true" to include
# completed. Fields: id, name, due date, allday due date, list, body, completed.
_REMINDERS_LIST_SCRIPT = _AS_HELPERS + '''
on dOf(v)
    if v is missing value then return ""
    return my isoOf(v)
end dOf

on run argv
    set US to character id 31
    set RS to character id 30
    set wanted to item 1 of argv
    set withDone to (item 2 of argv) is "true"
    set out to {}
    tell application "Reminders"
        if wanted is not "" and not (exists list wanted) then return "NO_LIST"
        repeat with l in lists
            set lName to name of l as text
            if wanted is "" or lName is wanted then
                if withDone then
                    set rs to every reminder of l
                else
                    set rs to (every reminder of l whose completed is false)
                end if
                repeat with r in rs
                    set rec to {my txt(id of r), my txt(name of r), my dOf(due date of r), my dOf(allday due date of r), lName, my txt(body of r), (completed of r) as text}
                    set AppleScript's text item delimiters to US
                    set end of out to rec as text
                    set AppleScript's text item delimiters to ""
                end repeat
            end if
        end repeat
    end tell
    set AppleScript's text item delimiters to RS
    set s to out as text
    set AppleScript's text item delimiters to ""
    return s
end run
'''

# argv: 1 = title, 2 = list ("" = default list), 3 = notes, 4 = due parts
# ("" = no due date), 5 = "true" for an all-day due date.
_REMINDERS_CREATE_SCRIPT = _AS_HELPERS + '''
on run argv
    set t to item 1 of argv
    set lName to item 2 of argv
    tell application "Reminders"
        if lName is "" then
            set l to default list
        else
            if not (exists list lName) then return "NO_LIST"
            set l to list lName
        end if
        set r to make new reminder at end of reminders of l with properties {name:t}
        if (item 3 of argv) is not "" then set body of r to (item 3 of argv)
        if (item 4 of argv) is not "" then
            set d to my mkDate(item 4 of argv)
            if (item 5 of argv) is "true" then
                set allday due date of r to d
            else
                set due date of r to d
                set remind me date of r to d
            end if
        end if
        return "OK" & (character id 31) & (id of r) & (character id 31) & (name of l)
    end tell
end run
'''

_REMINDER_LISTS_SCRIPT = '''
on run argv
    tell application "Reminders"
        set AppleScript's text item delimiters to (character id 30)
        set s to (name of every list) as text
        set AppleScript's text item delimiters to ""
        return s
    end tell
end run
'''


def _parts(dt: datetime) -> str:
    d = _local(dt)
    return f"{d.year} {d.month} {d.day} {d.hour} {d.minute} {d.second}"


def _run_osascript(script: str, *args: str) -> str:
    proc = subprocess.run(
        ["osascript", "-e", script, "--", *args],
        check=True, capture_output=True, text=True, timeout=_OSASCRIPT_TIMEOUT_S,
    )
    return (proc.stdout or "").rstrip("\n")


def _osascript_error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or str(exc)).strip()
    return str(exc)


class AppleScriptBackend:
    """Calendar.app over ``osascript``.

    Events carry no ``attendees`` key (#80: unknown, not "nobody"). Calendar's
    scripting dictionary lists attendees but not which one is the user, so
    the user's own address would come back as a guest and a mail lookup on
    it would match every message; reading them would also cost one more
    Apple Event per event on an already slow backend.
    """

    name = "applescript"
    max_range_days = 14

    def ready(self) -> bool:
        """Only query while Calendar.app is already running.

        ``tell application "Calendar"`` launches the app; a background poll
        must not do that every minute.
        """
        try:
            return subprocess.run(
                ["pgrep", "-xq", "Calendar"], timeout=5, check=False,
            ).returncode == 0
        except Exception:
            return False

    def list(self, start, end, calendars=None):
        try:
            out = _run_osascript(
                _LIST_SCRIPT, _parts(start), _parts(end), _US.join(calendars or []),
            )
        except Exception as exc:
            raise CalendarAccessError(_osascript_error(exc)) from exc
        events = []
        for record in filter(None, out.split(_RS)):
            f = record.split(_US)
            if len(f) < 9:
                continue
            events.append(make_event(
                id=f[0], title=f[1], start=parse_local(f[2]), end=parse_local(f[3]),
                calendar=f[4], location=f[5], notes=f[6], url=f[7],
                all_day=f[8].strip() == "true",
            ))
        return sorted(events, key=lambda e: e["start"])

    def create(self, title, start, end, calendar=None, location=None, notes=None):
        try:
            out = _run_osascript(
                _CREATE_SCRIPT, title, _parts(start), _parts(end),
                calendar or "", location or "", notes or "",
            )
        except Exception as exc:
            raise CalendarAccessError(_osascript_error(exc)) from exc
        if out == "NO_CALENDAR":
            raise CalendarAccessError(
                f"No calendar named {calendar!r}." if calendar else "No writable calendar."
            )
        f = out.split(_US)
        if f[0] != "OK":
            raise CalendarAccessError(f"Unexpected reply from Calendar: {out!r}")
        return {"status": "ok", "event_id": f[1] if len(f) > 1 else "",
                "calendar": f[2] if len(f) > 2 else (calendar or "")}

    # reminders (#45) ----------------------------------------------------------

    def reminder_lists(self):
        try:
            out = _run_osascript(_REMINDER_LISTS_SCRIPT)
        except Exception as exc:
            raise CalendarAccessError(_osascript_error(exc)) from exc
        return [n for n in out.split(_RS) if n]

    def list_reminders(self, lists=None, include_completed=False):
        names = builtins.list(lists or []) or [""]
        items: builtins.list[dict[str, Any]] = []
        for name in names:
            try:
                out = _run_osascript(
                    _REMINDERS_LIST_SCRIPT, name, "true" if include_completed else "false",
                )
            except Exception as exc:
                raise CalendarAccessError(_osascript_error(exc)) from exc
            if out == "NO_LIST":
                raise CalendarAccessError(f"No reminders list named {name!r}.")
            for record in filter(None, out.split(_RS)):
                f = record.split(_US)
                if len(f) < 7:
                    continue
                due_s, allday_s = f[2].strip(), f[3].strip()
                due: Optional[datetime] = None
                all_day = False
                if allday_s and (not due_s or due_s.endswith("T00:00:00")):
                    due, all_day = parse_local(allday_s), True
                elif due_s:
                    due = parse_local(due_s)
                items.append(make_reminder(
                    id=f[0], title=f[1], due=due, all_day=all_day, list=f[4],
                    notes=f[5], completed=f[6].strip() == "true",
                ))
        return sort_reminders(items)

    def create_reminder(self, title, due=None, all_day=False, list_name=None, notes=None):
        try:
            out = _run_osascript(
                _REMINDERS_CREATE_SCRIPT, title, list_name or "", notes or "",
                _parts(due) if due is not None else "", "true" if all_day else "false",
            )
        except Exception as exc:
            raise CalendarAccessError(_osascript_error(exc)) from exc
        if out == "NO_LIST":
            raise CalendarAccessError(f"No reminders list named {list_name!r}.")
        f = out.split(_US)
        if f[0] != "OK":
            raise CalendarAccessError(f"Unexpected reply from Reminders: {out!r}")
        return {"status": "ok", "reminder_id": f[1] if len(f) > 1 else "",
                "list": f[2] if len(f) > 2 else (list_name or "")}


# ---- menu bar app (#60) ----------------------------------------------------------
#
# The daemon's Python can't get the Calendar prompt (no Info.plist usage
# string), so the menu bar app owns EventKit. ``main.py`` registers each WS
# connection with :data:`app_bridge`; the app advertises its EventKit status
# with a ``calendar_status`` frame (read without prompting) and answers
# ``calendar_request`` frames with ``calendar_response``.

APP_NOT_CONNECTED = (
    "Calendar isn't available: open the bot-inator menu bar app to use Calendar."
)
APP_TIMEOUT_S = 15.0
# Before the user has answered the macOS prompt the first request waits on it.
APP_PROMPT_TIMEOUT_S = 120.0

# send(request_id, op, args, timeout_seconds), a coroutine run on the loop.
AppSend = Callable[[str, str, dict, int], Awaitable[None]]


class _AppConnection:
    def __init__(self, conn_id: str, send: AppSend, loop: asyncio.AbstractEventLoop) -> None:
        self.conn_id = conn_id
        self.send = send
        self.loop = loop
        self.authorization: Optional[str] = None  # None: not a calendar-capable client
        self.advertised_at = 0


class AppCalendarBridge:
    """Request/response to the menu bar app over the WebSocket.

    Works from any thread except the event loop's own (tools and the watcher
    run in worker threads): the frame is sent with
    ``run_coroutine_threadsafe`` and the caller blocks on a future keyed by
    ``request_id`` until :meth:`submit_response` resolves it.

    With several connections, requests go to ONE: the one that most
    recently sent ``calendar_status``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, _AppConnection] = {}
        self._pending: dict[str, tuple[str, concurrent.futures.Future]] = {}
        self._counter = itertools.count(1)

    # connection lifecycle (called on the loop by main.py) --------------------

    def connect(self, conn_id: str, send: AppSend, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._conns[conn_id] = _AppConnection(conn_id, send, loop)

    def disconnect(self, conn_id: str) -> None:
        with self._lock:
            self._conns.pop(conn_id, None)
            dropped = [rid for rid, (cid, _) in self._pending.items() if cid == conn_id]
            futures = [self._pending.pop(rid)[1] for rid in dropped]
        for fut in futures:
            if not fut.done():
                fut.set_exception(CalendarAccessError(
                    "The menu bar app disconnected before answering. " + APP_NOT_CONNECTED))

    def set_status(self, conn_id: str, authorization: str) -> None:
        with self._lock:
            conn = self._conns.get(conn_id)
            if conn is None:
                return
            conn.authorization = authorization
            conn.advertised_at = next(self._counter)

    # state (never prompts) -----------------------------------------------------

    def _pick(self) -> Optional[_AppConnection]:
        with self._lock:
            capable = [c for c in self._conns.values() if c.authorization is not None]
        return max(capable, key=lambda c: c.advertised_at) if capable else None

    def connected(self) -> bool:
        return self._pick() is not None

    def authorization(self) -> Optional[str]:
        conn = self._pick()
        return conn.authorization if conn else None

    # request / response ----------------------------------------------------------

    def submit_response(self, conn_id: str, payload: dict[str, Any]) -> bool:
        """Resolve a pending request (main.py, on ``calendar_response``)."""
        request_id = str(payload.get("request_id") or "")
        with self._lock:
            entry = self._pending.pop(request_id, None)
            auth = payload.get("authorization")
            conn = self._conns.get(conn_id)
            if auth and conn is not None:
                conn.authorization = auth
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(payload)
        return True

    def request(self, op: str, args: dict[str, Any], timeout: Optional[float] = None) -> dict[str, Any]:
        conn = self._pick()
        if conn is None:
            raise CalendarAccessError(APP_NOT_CONNECTED)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is conn.loop:
            raise CalendarAccessError(
                "calendar_access was called on the event loop; run it in a thread")
        if timeout is None:
            prompt = op == "request_access" or conn.authorization == "not_determined"
            timeout = APP_PROMPT_TIMEOUT_S if prompt else APP_TIMEOUT_S
        request_id = f"cal_{uuid.uuid4().hex[:12]}"
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending[request_id] = (conn.conn_id, fut)
        payload: Any = None
        try:
            sent = asyncio.run_coroutine_threadsafe(
                conn.send(request_id, op, args, int(timeout)), conn.loop)
            sent.result(timeout=APP_TIMEOUT_S)
            payload = fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise CalendarAccessError(
                f"The menu bar app didn't answer the calendar request within {timeout:.0f} s."
            ) from None
        except CalendarAccessError:
            raise
        except Exception as exc:
            raise CalendarAccessError(f"Couldn't reach the menu bar app: {exc}") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if not payload.get("ok"):
            raise CalendarAccessError(str(payload.get("error") or "the menu bar app's calendar request failed"))
        return payload


app_bridge = AppCalendarBridge()


def _app_permission_check() -> dict[str, str]:
    """Permissions row ``calendars`` (#59): what the app reported. Never prompts."""
    auth = app_bridge.authorization()
    if auth is None:
        return {"status": "unknown", "detail": "open the bot-inator menu bar app to check"}
    status = {"full_access": "granted", "not_determined": "not_asked",
              "denied": "denied", "restricted": "denied",
              "write_only": "denied"}.get(auth, "unknown")
    detail = "" if status == "granted" else f"EventKit status in the menu bar app: {auth}"
    return {"status": status, "detail": detail}


def _app_permission_request() -> dict[str, Any]:
    """Permissions "Request" click: the app shows the macOS Calendar prompt."""
    auth = app_bridge.authorization()
    if auth is None:
        return {"action": "none", "detail": "open the bot-inator menu bar app, then click Request"}
    if auth in {"denied", "restricted", "write_only"}:
        return {"action": "none", "detail": (
            f"Calendar access is {auth} for the menu bar app; change it in System Settings "
            "> Privacy & Security > Calendars")}
    try:
        reply = app_bridge.request("request_access", {})
    except CalendarAccessError as exc:
        return {"action": "none", "detail": str(exc)}
    return {"action": "prompted", "detail": str(reply.get("authorization") or "")}


def register_permission_hooks() -> None:
    """Point the Permissions ``calendars`` row at the app (#59 hook)."""
    from ai import permissions  # lazy: tools must not import the AI stack at import time

    permissions.set_external_check("calendars", _app_permission_check)
    permissions.set_external_request("calendars", _app_permission_request)


class AppCalendarBackend:
    """Calendar through the menu bar app's EventKit (#60)."""

    name = "app"
    max_range_days = 31

    def __init__(self, bridge: Optional[AppCalendarBridge] = None) -> None:
        self._bridge = bridge or app_bridge

    def ready(self) -> bool:
        return self._bridge.authorization() == "full_access"

    def list(self, start, end, calendars=None):
        args: dict[str, Any] = {"start": iso(start), "end": iso(end)}
        if calendars:
            args["calendars"] = builtins.list(calendars)
        reply = self._bridge.request("list", args)
        events = []
        for ev in reply.get("events") or []:
            events.append(make_event(
                id=str(ev.get("id") or ""), title=str(ev.get("title") or ""),
                start=parse_local(str(ev["start"])), end=parse_local(str(ev["end"])),
                calendar=str(ev.get("calendar") or ""), location=str(ev.get("location") or ""),
                notes=str(ev.get("notes") or ""), url=str(ev.get("url") or ""),
                all_day=bool(ev.get("all_day")),
                # Absent from apps older than #80: stays absent (unknown).
                attendees=ev.get("attendees"),
            ))
        return sorted(events, key=lambda e: e["start"])

    def create(self, title, start, end, calendar=None, location=None, notes=None):
        args: dict[str, Any] = {"title": title, "start": iso(start), "end": iso(end)}
        for key, value in (("calendar", calendar), ("location", location), ("notes", notes)):
            if value:
                args[key] = value
        reply = self._bridge.request("create", args)
        return {"status": "ok", "event_id": str(reply.get("event_id") or ""),
                "calendar": str(reply.get("calendar") or calendar or "")}


# ---- selection -------------------------------------------------------------------

_override: Optional[CalendarBackend] = None
_eventkit_backend: Optional[EventKitBackend] = None


def set_backend(backend: Optional[CalendarBackend]) -> None:
    """Force a backend (tests); ``None`` returns to env-based selection."""
    global _override
    _override = backend


def get_backend() -> CalendarBackend:
    if _override is not None:
        return _override
    choice = (os.environ.get(ENV_BACKEND) or "auto").strip().lower()
    if choice == "applescript":
        return AppleScriptBackend()
    if choice in {"app", "auto"}:
        # #60: the menu bar app owns EventKit. No AppleScript fallback under
        # auto (it timed out live); when the app isn't connected the backend
        # answers with "open the bot-inator menu bar app".
        return AppCalendarBackend()
    if choice != "eventkit":
        raise CalendarAccessError(
            f"{ENV_BACKEND}={choice!r}; use app, eventkit, applescript or auto"
        )
    global _eventkit_backend
    if not eventkit_available():
        raise CalendarAccessError("pyobjc-framework-EventKit is not installed")
    if _eventkit_backend is None:
        _eventkit_backend = EventKitBackend()
    return _eventkit_backend


_reminder_override: Optional[RemindersBackend] = None


def set_reminder_backend(backend: Optional[RemindersBackend]) -> None:
    """Force a reminders backend (tests); ``None`` returns to env selection."""
    global _reminder_override
    _reminder_override = backend


def get_reminder_backend() -> RemindersBackend:
    """Like :func:`get_backend`, but for reminders: ``AGENT_REMINDERS_BACKEND``
    (falling back to ``AGENT_CALENDAR_BACKEND``), and ``auto`` looks at the
    Reminders authorization, which macOS tracks separately from Calendar."""
    if _reminder_override is not None:
        return _reminder_override
    raw = os.environ.get(ENV_REMINDERS_BACKEND) or os.environ.get(ENV_BACKEND) or "auto"
    choice = raw.strip().lower()
    if choice == "app":  # the app serves events only (#60); reminders stay as before
        choice = "auto"
    if choice == "applescript":
        return AppleScriptBackend()
    if choice not in {"eventkit", "auto"}:
        raise CalendarAccessError(
            f"{ENV_REMINDERS_BACKEND}={choice!r}; use eventkit, applescript or auto"
        )
    global _eventkit_backend
    if eventkit_available():
        if _eventkit_backend is None:
            _eventkit_backend = EventKitBackend()
        if choice == "eventkit":
            return _eventkit_backend
        if _eventkit_backend.status("reminder") not in {"denied", "restricted"}:
            return _eventkit_backend
    elif choice == "eventkit":
        raise CalendarAccessError("pyobjc-framework-EventKit is not installed")
    return AppleScriptBackend()


def list_reminders(lists=None, include_completed=False):
    return get_reminder_backend().list_reminders(lists, include_completed)


def create_reminder(title, due=None, all_day=False, list_name=None, notes=None):
    return get_reminder_backend().create_reminder(title, due, all_day, list_name, notes)


def list(start, end, calendars=None):  # noqa: A001 - interface name from #42
    return get_backend().list(start, end, calendars)


def create(title, start, end, calendar=None, location=None, notes=None):
    return get_backend().create(title, start, end, calendar, location, notes)


# ---- permission diagnostic -------------------------------------------------------


def _check(use_applescript: bool) -> int:  # pragma: no cover - live check
    print(f"{ENV_BACKEND}={os.environ.get(ENV_BACKEND) or '(unset: auto)'}")
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)
    backend: CalendarBackend
    if use_applescript:
        backend = AppleScriptBackend()
        print("Backend: applescript (Calendar.app; may show an Automation prompt)")
    else:
        if not eventkit_available():
            print("EventKit: NOT importable. Install pyobjc-framework-EventKit, "
                  "or rerun with --applescript.")
            return 2
        ek = EventKitBackend()
        before = ek.status()
        print(f"EventKit status before: {before}")
        if before == "not_determined":
            print("Requesting access (a macOS prompt should appear now)...")
            granted = ek.request_access()
            print(f"Granted: {granted}")
        after = ek.status()
        print(f"EventKit status after: {after}")
        if after != "full_access":
            print("RESULT: EventKit has no read access from this process. If no "
                  "prompt appeared, it was silently denied: try "
                  "`--applescript`, then pin AGENT_CALENDAR_BACKEND=applescript.")
            return 1
        backend = ek
    try:
        events = backend.list(today, tomorrow)
    except CalendarAccessError as exc:
        print(f"RESULT: listing failed: {exc}")
        return 1
    print(f"Today's events ({len(events)}):")
    for ev in events:
        when = "all day" if ev["all_day"] else f"{ev['start'][11:16]}-{ev['end'][11:16]}"
        print(f"  {when}  {ev['title']}  [{ev['calendar']}]")
    print(f"RESULT: {backend.name} works. Pin it with "
          f"{ENV_BACKEND}={backend.name} in agent-daemon/.env. Rerun this after a "
          "daemon restart: the status should read full_access with no prompt.")
    return 0


def _check_reminders(use_applescript: bool) -> int:  # pragma: no cover - live check
    print(f"{ENV_REMINDERS_BACKEND}={os.environ.get(ENV_REMINDERS_BACKEND) or '(unset)'}  "
          f"{ENV_BACKEND}={os.environ.get(ENV_BACKEND) or '(unset)'}")
    backend: RemindersBackend
    if use_applescript:
        backend = AppleScriptBackend()
        print("Backend: applescript (Reminders.app; may show an Automation prompt)")
    else:
        if not eventkit_available():
            print("EventKit: NOT importable. Install pyobjc-framework-EventKit, "
                  "or rerun with --applescript.")
            return 2
        ek = EventKitBackend()
        before = ek.status("reminder")
        print(f"EventKit Reminders status before: {before}")
        if before == "not_determined":
            print("Requesting Reminders access (a macOS prompt should appear now)...")
            print(f"Granted: {ek.request_access('reminder')}")
        after = ek.status("reminder")
        print(f"EventKit Reminders status after: {after}")
        if after != "full_access":
            print("RESULT: EventKit has no Reminders access from this process. If no "
                  "prompt appeared, it was silently denied: try `--reminders "
                  f"--applescript`, then pin {ENV_REMINDERS_BACKEND}=applescript.")
            return 1
        backend = ek
    try:
        names = backend.reminder_lists()
        open_items = backend.list_reminders()
    except CalendarAccessError as exc:
        print(f"RESULT: listing failed: {exc}")
        return 1
    print(f"Reminder lists ({len(names)}): {', '.join(names)}")
    print(f"Open reminders: {len(open_items)}")
    for r in open_items[:10]:
        print(f"  {r['due'] or '(no due date)'}  {r['title']}  [{r['list']}]")
    print(f"RESULT: {backend.name} works for reminders. Pin it with "
          f"{ENV_REMINDERS_BACKEND}={backend.name} in agent-daemon/.env.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Calendar permission check (#42)")
    parser.add_argument("--check", action="store_true", help="request access, list today")
    parser.add_argument("--applescript", action="store_true", help="check the AppleScript backend")
    parser.add_argument("--reminders", action="store_true",
                        help="with --check: request Reminders access and list reminder lists (#45)")
    ns = parser.parse_args()
    if not ns.check:
        parser.print_help()
        sys.exit(0)
    sys.exit(_check_reminders(ns.applescript) if ns.reminders else _check(ns.applescript))
