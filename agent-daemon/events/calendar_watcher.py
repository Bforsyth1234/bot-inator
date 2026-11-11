"""Calendar watcher: one ``event_starting_soon`` per meeting about to start.

Polls the calendar backend (``tools.calendar_access``) every 60 s and pushes
``ContextEvent(event_type="event_starting_soon", metadata={title, start,
location, url, calendar, video_url, end, id[, attendees]})`` for each timed
event starting within the next 10 minutes. Each occurrence (event id + start) is announced once.
All-day events are skipped.

``video_url`` is the first Zoom / Google Meet / Microsoft Teams link found in
the event's url, location or notes ("" if none), so an idea source can offer
"open the meeting link".

``attendees`` (#80) is the backend's ``[{name, email, is_self}]`` list, for
``events.meeting_prep``; the key is absent when the backend can't tell (see
``tools.calendar_access``). Attendee names and addresses are personal data:
the new keys come after the original six so previews that show only the
first few metadata items don't pick them up.

The watcher never triggers a permission prompt: it polls only while the
backend reports ``ready()`` (EventKit already granted; for AppleScript,
Calendar.app already running) and stays idle otherwise.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60.0  # seconds
LEAD_TIME = timedelta(minutes=10)

VIDEO_LINK_RE = re.compile(
    r"https?://(?:[\w-]+\.)*(?:"
    r"zoom\.us/(?:j|my|w|s)/[^\s<>\"')]+"
    r"|meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}[^\s<>\"')]*"
    r"|teams\.microsoft\.com/l/meetup-join/[^\s<>\"')]+"
    r"|teams\.live\.com/meet/[^\s<>\"')]+"
    r")",
    re.IGNORECASE,
)


def find_video_link(event: dict[str, Any]) -> str:
    """First Zoom/Meet/Teams URL in the event's url, location or notes."""
    for field in ("url", "location", "notes"):
        match = VIDEO_LINK_RE.search(str(event.get(field) or ""))
        if match:
            return match.group(0)
    return ""


def _default_backend() -> Any:
    from tools import calendar_access  # lazy: tools imports the AI stack

    return calendar_access.get_backend()


class CalendarWatcher:
    def __init__(
        self,
        bus: EventBus,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        backend: Optional[Callable[[], Any]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self._bus = bus
        self._poll_interval = poll_interval
        self._backend = backend or _default_backend
        self._clock = clock
        self._announced: dict[tuple[str, str], datetime] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._warned = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="CalendarWatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)

    def _run(self) -> None:
        logger.info("CalendarWatcher started (poll every %.0fs)", self._poll_interval)
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                if not self._warned:
                    logger.warning("Calendar poll failed (%s); will keep retrying", exc)
                    self._warned = True
            if self._stop_event.wait(self._poll_interval):
                return

    def poll_once(self) -> list[ContextEvent]:
        """Check once; push and return the newly announced events."""
        backend = self._backend()
        if not backend.ready():
            return []
        now = self._clock()
        horizon = now + LEAD_TIME
        pushed: list[ContextEvent] = []
        for ev in backend.list(now, horizon):
            if ev.get("all_day"):
                continue
            start = datetime.fromisoformat(ev["start"])
            if not (now <= start <= horizon):
                continue  # already running, or overlaps only by its end
            key = (str(ev.get("id") or ev.get("title") or ""), ev["start"])
            if key in self._announced:
                continue
            self._announced[key] = start
            event = ContextEvent(
                event_type="event_starting_soon",
                metadata={
                    "title": ev.get("title", ""),
                    "start": ev["start"],
                    "location": ev.get("location", ""),
                    "url": ev.get("url", ""),
                    "calendar": ev.get("calendar", ""),
                    "video_url": find_video_link(ev),
                    "end": ev.get("end", ""),
                    "id": ev.get("id", ""),
                },
            )
            if ev.get("attendees") is not None:
                event.metadata["attendees"] = [dict(p) for p in ev["attendees"]]
            try:
                self._bus.push_threadsafe(event)
            except Exception:  # pragma: no cover - loop not bound / shutting down
                logger.exception("Failed to push event_starting_soon")
            pushed.append(event)
        self._prune(now)
        return pushed

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=1)
        for key in [k for k, start in self._announced.items() if start < cutoff]:
            del self._announced[key]
