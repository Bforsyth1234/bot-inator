"""Knowledge index source ``calendar`` (#138): the Mac's calendar events.

One :class:`~ai.knowledge_index.SourceItem` per event OCCURRENCE in a
window (default 730 days back, 365 ahead): recurring events come expanded,
as the calendar layer returns them. Text = title, time, calendar, location,
attendees, notes. ``item_id`` = event id + "@" + start, ``date`` = start,
``link`` empty (the layer has no URL that opens an event in Calendar; an
event's own ``url`` is usually a meeting link, often with a password in it,
so it is not indexed either).

READ-ONLY, NO NEW PERMISSION, NO NETWORK. Reads through
:mod:`tools.calendar_access` only: ``get_backend()``, ``ready()`` and
``list()``. Never ``create`` / ``delete``.

How an indexing pass reaches the calendar. ``KnowledgeIndex.run_pass`` runs
in a worker thread (``asyncio.to_thread``), which is exactly where
``calendar_access`` must be called from:

* default (``AGENT_CALENDAR_BACKEND`` unset/``auto``/``app``):
  :class:`AppCalendarBackend` sends ``calendar_request`` frames to the menu
  bar app over the WebSocket and blocks on the answer (15 s per request).
  Called on the event loop's own thread it raises; here that becomes the
  listing's ``error``.
* ``eventkit`` (in-process EventKit; the daemon's Python is normally denied)
  and ``applescript`` (Calendar.app; 14-day requests, no recurring
  expansion, no attendees, slow) are honoured if the user pinned them.

Like ``events/calendar_watcher.py`` the pass reads ONLY while
``backend.ready()`` (the app reported ``full_access``; EventKit granted;
Calendar.app running), so a background pass never shows the macOS Calendar
prompt (the app would show it on a ``list`` while ``not_determined``). The
window is read in slices of ``backend.max_range_days`` (31 for the app).

Not connected, no access, a slice that fails or times out: ``Listing`` with
``complete=False`` and the layer's own wording in ``error`` (no exception),
so nothing already indexed is pruned. Only a full, successful read is
``complete=True`` (events gone from it are pruned).

Limits inherited from the layer: notes are the first 500 characters
(``NOTES_LIMIT``, cut in the app); at most 50 attendees per event.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from ai.knowledge_index import Listing, SourceItem

NAME = "calendar"
TITLE = "Calendar"
DESCRIPTION = ("Your calendar events (title, time, place, attendees and notes) from two "
               "years back to one year ahead, read through the menu bar app; the index "
               "stays on this Mac.")
TAINT_LABEL = "indexed calendar event"

DEFAULT_OPTIONS: dict[str, Any] = {"days_back": 730, "days_ahead": 365}
MAX_DAYS_BACK = 3650
MAX_DAYS_AHEAD = 1825
# More occurrences than this in the window: index the first ones, never prune.
MAX_ITEMS = 20_000
SLICE_DAYS = 31

APP_NOT_CONNECTED = "Calendar isn't available: open the bot-inator menu bar app to use Calendar."
# The menu bar app's own wording (CalendarBridge.swift ensureAccess) per status.
_APP_AUTH_ERRORS = {
    "denied": ("Calendar access was denied for the bot-inator menu bar app. Allow it in "
               "System Settings > Privacy & Security > Calendars."),
    "restricted": ("Calendar access is restricted on this Mac (Screen Time or a device "
                   "profile), so the bot-inator menu bar app can't read calendars."),
    "write_only": ("The bot-inator menu bar app can only add events, not read them. Allow "
                   "full access in System Settings > Privacy & Security > Calendars."),
    "not_determined": ("Calendar access hasn't been asked yet: click Request on the "
                       "Calendars row of the Permissions screen."),
}


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def validate_options(options: Any) -> dict[str, Any]:
    """A cleaned copy of ``options`` (missing keys take the defaults).
    ValueError with a readable message on anything else."""
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("Calendar options must be an object.")
    unknown = sorted(str(k) for k in options if k not in DEFAULT_OPTIONS)
    if unknown:
        raise ValueError(f"Unknown calendar option: {', '.join(unknown)}.")
    out = dict(DEFAULT_OPTIONS)
    limits = {"days_back": MAX_DAYS_BACK, "days_ahead": MAX_DAYS_AHEAD}
    labels = {"days_back": "Days back", "days_ahead": "Days ahead"}
    for key, cap in limits.items():
        if key not in options:
            continue
        value = options[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{labels[key]} must be a whole number of days.")
        if not 0 <= value <= cap:
            raise ValueError(f"{labels[key]} must be between 0 and {cap}.")
        out[key] = value
    if out["days_back"] == 0 and out["days_ahead"] == 0:
        raise ValueError("The calendar window is empty: set days back or days ahead above 0.")
    return out


# ---------------------------------------------------------------------------
# Events -> items
# ---------------------------------------------------------------------------


def _parse(value: str) -> datetime:
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt.astimezone() if dt.tzinfo else dt.astimezone()


def _when(event: dict[str, Any], start: datetime) -> str:
    if event.get("all_day"):
        return start.strftime("%A %B %-d %Y (all day)")
    text = start.strftime("%A %B %-d %Y, %-I:%M %p")
    try:
        end = _parse(str(event.get("end") or ""))
    except ValueError:
        return text
    if end.date() == start.date():
        return text + end.strftime(" – %-I:%M %p")
    return text + end.strftime(" – %A %B %-d %Y, %-I:%M %p")


def _people(event: dict[str, Any]) -> list[str]:
    """Other people's names (address only when there is no name); the
    user's own entry left out. [] when unknown or nobody else."""
    out = []
    for p in event.get("attendees") or []:
        if not isinstance(p, dict) or p.get("is_self"):
            continue
        name = str(p.get("name") or "").strip()
        email = str(p.get("email") or "").strip()
        if name or email:
            out.append(name or email)
    return out


def event_text(event: dict[str, Any], start: datetime) -> str:
    lines = [str(event.get("title") or "").strip() or "(no title)",
             f"When: {_when(event, start)}"]
    for label, key in (("Calendar", "calendar"), ("Location", "location")):
        value = str(event.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    people = _people(event)
    if people:
        lines.append("Attendees: " + ", ".join(people))
    notes = str(event.get("notes") or "").strip()
    if notes:
        lines.append("Notes: " + notes)
    return "\n".join(lines)


def event_item(event: dict[str, Any]) -> Optional[SourceItem]:
    """One occurrence as an item; None when it has no usable start."""
    try:
        start = _parse(str(event.get("start") or ""))
    except ValueError:
        return None
    title = str(event.get("title") or "").strip() or "(no title)"
    start_iso = start.isoformat(timespec="seconds")
    event_id = str(event.get("id") or "").strip()
    if not event_id:  # no id from the layer: stable on title + calendar
        event_id = "noid:" + hashlib.sha256(
            f"{title}\x1f{event.get('calendar') or ''}".encode("utf-8")).hexdigest()[:16]
    text = event_text(event, start)
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return SourceItem(item_id=f"{event_id}@{start_iso}", version=version, title=title,
                      link="", date=start_iso, text=text)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def _not_ready_error(backend: Any) -> str:
    if getattr(backend, "name", "") == "app":
        bridge = getattr(backend, "_bridge", None)
        try:
            auth = bridge.authorization() if bridge is not None else None
        except Exception:
            auth = None
        if auth is None:
            return APP_NOT_CONNECTED
        return _APP_AUTH_ERRORS.get(str(auth), f"Calendar access is {auth} for the bot-inator "
                                    "menu bar app. Check the Calendars row of the Permissions screen.")
    if getattr(backend, "name", "") == "applescript":
        return "Calendar isn't available: Calendar.app isn't running (AppleScript backend)."
    return ("Calendar access isn't granted. Check the Calendars row of the Permissions "
            "screen.")


def _default_backend() -> Any:
    from tools import calendar_access  # lazy: tools imports the AI stack

    return calendar_access.get_backend()


def _now() -> datetime:
    return datetime.now().astimezone()


def listing(options: Optional[dict] = None, *, backend: Any = None,
            clock: Optional[Callable[[], datetime]] = None,
            max_items: int = MAX_ITEMS) -> Listing:
    """Every event occurrence starting in the window. Call it from a worker
    thread (the app backend blocks on the WebSocket). Test hooks:
    ``backend`` (an object with ``ready()``, ``list(start, end)`` and
    optionally ``max_range_days``), ``clock`` (aware ``datetime``)."""
    try:
        opts = validate_options(options or {})
    except ValueError as exc:
        return Listing([], complete=False, error=str(exc))
    try:
        cal = backend if backend is not None else _default_backend()
    except Exception as exc:  # e.g. a bad AGENT_CALENDAR_BACKEND
        return Listing([], complete=False, error=str(exc) or "Calendar isn't available.")
    try:
        ready = bool(cal.ready())
    except Exception:
        ready = False
    if not ready:
        return Listing([], complete=False, error=_not_ready_error(cal))

    now = (clock or _now)()
    window_start = now - timedelta(days=opts["days_back"])
    window_end = now + timedelta(days=opts["days_ahead"])
    try:
        step = int(getattr(cal, "max_range_days", SLICE_DAYS) or SLICE_DAYS)
    except (TypeError, ValueError):
        step = SLICE_DAYS
    step = timedelta(days=max(1, min(SLICE_DAYS, step)))

    items: dict[str, SourceItem] = {}
    complete, error = True, ""
    lo = window_start
    while lo < window_end:
        hi = min(lo + step, window_end)
        try:
            events = cal.list(lo, hi) or []
        except Exception as exc:  # not connected mid-pass, timeout, access revoked
            complete, error = False, str(exc) or "Calendar read failed."
            break
        for ev in events:
            if not isinstance(ev, dict):
                continue
            item = event_item(ev)
            if item is None:
                continue
            start = _parse(item.date)
            # Overlapping events come back from both slices: keep each
            # occurrence once, in the slice where it starts.
            if not (lo <= start < hi):
                continue
            items.setdefault(item.item_id, item)
        if len(items) >= max_items:
            complete = False
            break
        lo = hi
    ordered = sorted(items.values(), key=lambda i: i.date, reverse=True)[:max_items]
    return Listing(ordered, complete=complete, error=error)
