"""Shared access to the Mac's calendars: EventKit first, AppleScript fallback.

One interface, two backends (#42):

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

Backend selection: ``AGENT_CALENDAR_BACKEND`` = ``eventkit``, ``applescript``
or ``auto`` (default). ``auto`` uses EventKit unless it cannot be imported
or access was denied/restricted, then AppleScript. Run the permission check
(``python tools/calendar_access.py --check`` from ``agent-daemon``) and pin
whichever backend it reports working.

Dates are ISO 8601 in local time. A naive datetime is local; output always
carries the local UTC offset. Events come back as ``{id, title, start, end,
calendar, location, notes (first 500 chars), url, all_day}``.

Extending for reminders (#45): add reminder methods next to the event ones
and reuse :meth:`EventKitBackend._ensure_access` with
``EKEntityTypeReminder``; nothing here assumes events are the only entity.
"""

from __future__ import annotations

import builtins
import os
import subprocess
import threading
from datetime import date, datetime, timedelta
from typing import Any, Optional, Protocol, Sequence

ENV_BACKEND = "AGENT_CALENDAR_BACKEND"
NOTES_LIMIT = 500
# osascript blocks on the first-use Automation prompt until it is answered.
_OSASCRIPT_TIMEOUT_S = 60
_ACCESS_TIMEOUT_S = 120
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


def make_event(
    *, id: str = "", title: str = "", start: datetime, end: datetime,
    calendar: str = "", location: str = "", notes: str = "", url: str = "",
    all_day: bool = False,
) -> dict[str, Any]:
    return {
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
    if choice not in {"eventkit", "auto"}:
        raise CalendarAccessError(
            f"{ENV_BACKEND}={choice!r}; use eventkit, applescript or auto"
        )
    global _eventkit_backend
    if eventkit_available():
        if _eventkit_backend is None:
            _eventkit_backend = EventKitBackend()
        if choice == "eventkit":
            return _eventkit_backend
        if _eventkit_backend.status() not in {"denied", "restricted"}:
            return _eventkit_backend
    elif choice == "eventkit":
        raise CalendarAccessError("pyobjc-framework-EventKit is not installed")
    return AppleScriptBackend()


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


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Calendar permission check (#42)")
    parser.add_argument("--check", action="store_true", help="request access, list today")
    parser.add_argument("--applescript", action="store_true", help="check the AppleScript backend")
    ns = parser.parse_args()
    if not ns.check:
        parser.print_help()
        sys.exit(0)
    sys.exit(_check(ns.applescript))
