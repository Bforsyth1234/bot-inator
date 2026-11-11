"""Morning brief: one ``daily_brief`` event per day (#65).

Two parts:

* :func:`gather_brief` builds the brief's input deterministically from the
  read-only sources: today's calendar events (``tools.list_events``), mail
  received in the last 24 hours (Mail's Envelope Index, the same read-only
  path ``events.mail_watcher`` uses; newsletters, junk and sent mail left
  out) and reminders due today or overdue (``tools.reminders``). Each source
  is wrapped: a failure or an "unavailable" answer becomes a named line
  ("Calendar: unavailable: <reason>") instead of being dropped.
* :class:`DailyBriefScheduler` publishes
  ``ContextEvent(event_type="daily_brief", metadata=<gathered payload>)``
  once per day at the set local time (default 08:00). If the time was
  missed (Mac asleep, daemon down) it fires at the next check after it,
  the same day only. The last run is kept in
  ``~/.bot-inator/daily_brief.json`` so a restart doesn't repeat it.

Read-only (invariant 7a): nothing here writes to Mail, Calendar or
Reminders, posts a notification, asks for approval or triggers a macOS
permission prompt. A source whose access isn't granted yet is reported as
unavailable. The only file written is the scheduler's own state file.

Privacy (invariant 7c, #31/#52): the payload carries sender and subject
only, never a body, and each subject goes through ``ai.privacy.redact_text``
for the engines that read the event, so under a cloud engine it is the
placeholder unless the user allowed personal text.

Settings (``user_settings``, read on every check, so no restart needed):
``AGENT_DAILY_BRIEF`` ("1"/"0", default on) and ``AGENT_DAILY_BRIEF_TIME``
("HH:MM", default "08:00").
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

EVENT_TYPE = "daily_brief"
ENABLED_KEY = "AGENT_DAILY_BRIEF"
TIME_KEY = "AGENT_DAILY_BRIEF_TIME"
DEFAULT_TIME = "08:00"
DEFAULT_STATE_PATH = Path.home() / ".bot-inator" / "daily_brief.json"
DEFAULT_POLL_INTERVAL = 30.0  # seconds
# Wait before the first check so the menu bar app (which serves Calendar,
# #60) has connected when a missed brief is caught up at daemon start.
DEFAULT_STARTUP_DELAY = 45.0  # seconds

MAIL_WINDOW = timedelta(hours=24)
MAX_EVENTS = 20
MAX_MAIL = 15
MAX_REMINDERS = 20
# Engine roles that read the event: the analysis summary and the main turn.
READER_ROLES: tuple[str, ...] = ("main", "analysis")

SOURCES: tuple[tuple[str, str], ...] = (
    ("calendar", "Calendar"),
    ("mail", "Mail"),
    ("reminders", "Reminders"),
    ("waiting_on", "Waiting on"),
)

# Offers pushed after the scheduled brief (#73 nudges, #77 look-ahead), each
# a ``pattern_detected`` built-in idea that goes through the normal card.
MAX_NUDGES = 3
NUDGE_SECTION = "waiting_on"
NUDGE_COOLDOWN = 7 * 24 * 3600.0
LOOKAHEAD_SECTION = "lookahead"
LOOKAHEAD_COOLDOWN = 7 * 24 * 3600.0

SQL_RECENT_MAIL = """
    SELECT m.ROWID, s.subject, a.address, {sender_name}, m.date_received, mb.url,
           {global_id}, {list_id}, {unsubscribe}
    FROM messages m
    LEFT JOIN subjects s ON m.subject = s.ROWID
    LEFT JOIN addresses a ON m.sender = a.ROWID
    LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
    WHERE m.date_received >= ?
    ORDER BY m.date_received DESC
    LIMIT 500
"""


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


def parse_time(value: Any) -> Optional[dtime]:
    """``"HH:MM"`` (24 h) as a time, or None when it isn't one."""
    parts = str(value or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() and 1 <= len(p) <= 2 for p in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        return None
    return dtime(hour, minute)


def brief_enabled() -> bool:
    return _setting(ENABLED_KEY, "1").strip() != "0"


def brief_time() -> dtime:
    return parse_time(_setting(TIME_KEY, DEFAULT_TIME)) or parse_time(DEFAULT_TIME)  # type: ignore[return-value]


def brief_time_text() -> str:
    return brief_time().strftime("%H:%M")


# ---- default sources (read-only, never prompt) ---------------------------------

def _default_list_events(start: str = "", end: str = "") -> dict[str, Any]:
    from tools import calendar_access  # lazy: tools imports the AI stack
    from tools.list_events import list_events

    backend = calendar_access.get_backend()
    if not backend.ready():
        # Asking now could show a macOS prompt or launch Calendar.app.
        if getattr(backend, "name", "") == "app" and not calendar_access.app_bridge.connected():
            return {"status": "error", "error": calendar_access.APP_NOT_CONNECTED}
        return {"status": "error",
                "error": "Calendar access isn't granted yet (Settings > Permissions)."}
    return list_events(start=start, end=end)


def _default_list_reminders(list_name: str = "", include_completed: bool = False) -> dict[str, Any]:
    from tools import calendar_access
    from tools.reminders import list_reminders

    backend = calendar_access.get_reminder_backend()
    status_of = getattr(backend, "status", None)
    if callable(status_of):  # EventKit: asking while not granted would prompt
        status = status_of("reminder")
        if status != "full_access":
            return {"status": "error",
                    "error": f"Reminders access is {status} (Settings > Permissions)."}
    elif getattr(backend, "name", "") == "applescript":
        from ai import permissions

        check = permissions.check_automation("com.apple.reminders", "Reminders")
        if check.get("status") != permissions.GRANTED:
            return {"status": "error",
                    "error": "Reminders automation isn't allowed yet (Settings > Permissions)."}
    return list_reminders(list_name=list_name, include_completed=include_completed)


def recent_mail(since: datetime, mail_root: Path | str | None = None) -> dict[str, Any]:
    """Mail received since ``since``: headers only, newest first.

    Read-only query of Mail's Envelope Index. Leaves out sent/draft/junk/
    trash mailboxes, mailing-list mail and Gmail label duplicates.
    """
    from ai.idea_sources import _SKIP_MAILBOXES
    from tools.read_email import mail_root as default_mail_root

    from . import mail_watcher as mw

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
        optional = [f"m.{n}" if n in message_cols else "NULL" for n in mw.OPTIONAL_MESSAGE_COLUMNS]
        sql = SQL_RECENT_MAIL.format(
            sender_name=f"a.{col}" if has_name else "NULL",
            global_id=optional[0], list_id=optional[1], unsubscribe=optional[2],
        )
        rows = conn.execute(sql, (since.timestamp(),)).fetchall()
    except sqlite3.Error as exc:
        return {"status": "error", "error": f"could not read the Mail index ({exc})."}
    finally:
        conn.close()
    seen: set[Any] = set()
    messages: list[dict[str, Any]] = []
    for rowid, subject, address, name, received, url, global_id, list_id, unsub in rows:
        if mw.is_own_mailbox(url) or mw.is_bulk(list_id, unsub):
            continue
        mailbox = mw.mailbox_name(url)
        if _SKIP_MAILBOXES.search(mailbox):
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
            "mailbox": mailbox,
        })
    return {"status": "ok", "count": len(messages), "messages": messages}


# ---- gathering -----------------------------------------------------------------

def _call_source(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Optional[dict[str, Any]], str]:
    """``(result, "")`` on success, else ``(None, reason)``. Never raises."""
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # a source must never sink the whole brief
        return None, str(exc) or exc.__class__.__name__
    if not isinstance(result, dict):
        return None, "unexpected answer"
    if result.get("status") != "ok":
        return None, str(result.get("error") or result.get("status") or "no answer")
    return result, ""


def _clock_text(value: datetime) -> str:
    return value.strftime("%-I:%M %p")


def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _event_item(event: dict[str, Any]) -> dict[str, Any]:
    start, end = _parse(event.get("start")), _parse(event.get("end"))
    if event.get("all_day") or start is None:
        when = "All day" if event.get("all_day") else str(event.get("when") or "")
    else:
        when = _clock_text(start) + (f" – {_clock_text(end)}" if end else "")
    return {
        "when": when,
        "title": str(event.get("title") or "(untitled)"),
        "location": str(event.get("location") or ""),
        "all_day": bool(event.get("all_day")),
    }


def _reminder_item(reminder: dict[str, Any], now: datetime) -> Optional[dict[str, Any]]:
    """The reminder if it is due today or overdue, else None."""
    due = str(reminder.get("due") or "")
    if not due or reminder.get("completed"):
        return None
    try:
        due_day = date.fromisoformat(due[:10])
    except ValueError:
        return None
    today = now.date()
    if due_day > today:
        return None
    overdue = due_day < today
    when = "today"
    timed = None if len(due) <= 10 else _parse(due)
    if timed is not None:
        when = f"today {_clock_text(timed)}"
        if not overdue:
            if timed.tzinfo is None:
                timed = timed.replace(tzinfo=now.tzinfo)
            overdue = timed < now
    if due_day < today:
        when = f"since {due_day.strftime('%A %B %-d')}"
    return {
        "title": str(reminder.get("title") or "(untitled)"),
        "due": due,
        "when": when,
        "overdue": overdue,
        "list": str(reminder.get("list") or ""),
    }


def _redact_subject(subject: str) -> str:
    from ai import privacy

    for role in READER_ROLES:
        subject = privacy.redact_text(subject, role)
    return subject


def _section(label: str, noun: str, source: dict[str, Any], render: Callable[[dict[str, Any]], str]) -> list[str]:
    if source["status"] != "ok":
        return [f"{label}: unavailable: {source['reason']}"]
    items = source["items"]
    if not items:
        return [f"{label}: {noun}"]
    lines = [f"{label} ({len(items)}{'+' if source.get('truncated') else ''}):"]
    lines += [f"- {render(item)}" for item in items]
    return lines


def format_brief(payload: dict[str, Any]) -> str:
    """The plain-text brief for a :func:`gather_brief` payload."""
    lines = [f"Brief for {payload['day']}"]
    lines += _section(
        "Calendar", "nothing today", payload["calendar"],
        lambda e: f"{e['when']}: {e['title']}" + (f" ({e['location']})" if e["location"] else ""),
    )
    lines += _section(
        "Mail", "nothing new in the last 24 hours", payload["mail"],
        lambda m: f"{m['sender']}: {m['subject'] or '(no subject)'} [id {m['message_id']}]",
    )
    lines += _section(
        "Reminders", "nothing due", payload["reminders"],
        lambda r: f"{r['title']} ({'overdue, ' if r['overdue'] else 'due '}{r['when']})",
    )
    if payload.get("waiting_on") is not None:
        from ai import waiting_on as wo

        lines += wo.format_section(payload["waiting_on"])
    look = (payload.get("lookahead") or {}).get("text")
    if look:
        lines += str(look).splitlines()
    return "\n".join(lines)


def gather_brief(
    now: Optional[datetime] = None,
    *,
    list_events: Optional[Callable[..., Any]] = None,
    recent_mail: Optional[Callable[..., Any]] = None,
    list_reminders: Optional[Callable[..., Any]] = None,
    redact_subject: Optional[Callable[[str], str]] = None,
    waiting_on: Optional[Callable[..., Any]] = None,
    check_gaps: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Gather today's brief from the read-only sources. Never raises.

    Two more sections: ``waiting_on`` (#73, sent mail with no reply;
    ``waiting_on(now, redact_subject=)`` answers like
    ``ai.waiting_on.waiting_on``) and ``lookahead`` (#77, gaps in the next
    days; ``check_gaps`` like ``events.lookahead.check_gaps``, given this
    brief's own calendar / mail / reminder sources).

    The sources are injectable (tests pass fakes): ``list_events(start=,
    end=)`` and ``list_reminders(list_name=, include_completed=)`` answer
    like the tools of the same name; ``recent_mail(since)`` answers
    ``{"status": "ok", "messages": [{message_id, sender, subject, date,
    mailbox}]}``. Each of ``calendar`` / ``mail`` / ``reminders`` in the
    result is ``{"status": "ok", "items": [...]}`` or ``{"status":
    "unavailable", "reason": "..."}``; ``text`` is the whole brief.
    """
    now = now or _now()
    list_events = list_events or _default_list_events
    list_reminders = list_reminders or _default_list_reminders
    fetch_mail = recent_mail or globals()["recent_mail"]
    redact = redact_subject or _redact_subject
    # The brief and the look-ahead both list open reminders: ask once
    # (AppleScript can be slow).
    list_reminders = _once(list_reminders)

    def source(result: Optional[dict[str, Any]], reason: str, items: Callable[[dict[str, Any]], list], cap: int) -> dict[str, Any]:
        if result is None:
            return {"status": "unavailable", "reason": reason}
        try:
            found = items(result)
        except Exception as exc:
            return {"status": "unavailable", "reason": f"unreadable answer ({exc})"}
        return {"status": "ok", "items": found[:cap], "truncated": len(found) > cap}

    calendar = source(
        *_call_source(list_events, start=now.date().isoformat(), end=""),
        lambda r: [_event_item(e) for e in r.get("events") or []], MAX_EVENTS,
    )
    mail = source(
        *_call_source(fetch_mail, now - MAIL_WINDOW),
        lambda r: [
            {
                "message_id": m.get("message_id"),
                "sender": str(m.get("sender") or "unknown"),
                # Sender + subject only; bodies never enter the brief.
                "subject": redact(str(m.get("subject") or "")),
                "date": str(m.get("date") or ""),
            }
            for m in r.get("messages") or []
        ], MAX_MAIL,
    )
    reminders = source(
        *_call_source(list_reminders, list_name="", include_completed=False),
        lambda r: [i for i in (_reminder_item(x, now) for x in r.get("reminders") or []) if i],
        MAX_REMINDERS,
    )
    waiting = _waiting_section(waiting_on, now, redact)
    sources = {"calendar": calendar, "mail": mail, "reminders": reminders, "waiting_on": waiting}
    payload: dict[str, Any] = {
        "date": now.date().isoformat(),
        "day": now.strftime("%A %B %-d"),
        **sources,
        "unavailable": [label for key, label in SOURCES if sources[key]["status"] != "ok"],
    }
    try:
        if check_gaps is None:
            from . import lookahead

            check_gaps = lookahead.check_gaps
        payload["lookahead"] = check_gaps(
            now, list_events=list_events, recent_mail=fetch_mail,
            list_reminders=list_reminders, redact_subject=redact,
        )
    except Exception as exc:  # the look-ahead must never sink the brief
        logger.exception("look-ahead failed")
        payload["lookahead"] = {"status": "unavailable", "gaps": [], "skipped": [],
                                "text": f"Look-ahead: unavailable: {exc}"}
    payload["text"] = format_brief(payload)
    return payload


def _once(fn: Callable[..., Any]) -> Callable[..., Any]:
    """``fn`` with its answers kept per arguments for one gather."""
    seen: dict[str, Any] = {}

    def call(*args: Any, **kwargs: Any) -> Any:
        key = repr((args, sorted(kwargs.items())))
        if key not in seen:
            seen[key] = fn(*args, **kwargs)
        return seen[key]

    return call


def _waiting_section(fn: Optional[Callable[..., Any]], now: datetime, redact: Callable[[str], str]) -> dict[str, Any]:
    """The #73 "Waiting on" source: ``{"status": "ok", "items"}`` or unavailable."""
    try:
        if fn is None:
            from ai import waiting_on as wo

            fn = wo.waiting_on
        result = fn(now, redact_subject=redact)
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc) or exc.__class__.__name__}
    if not isinstance(result, dict):
        return {"status": "unavailable", "reason": "unexpected answer"}
    if result.get("status") != "ok":
        return {"status": "unavailable", "reason": str(result.get("reason") or result.get("error") or "no answer")}
    return result


def build_brief_text(now: Optional[datetime] = None, **sources: Any) -> str:
    """Today's brief as plain text (the on-demand path)."""
    return gather_brief(now, **sources)["text"]


def brief_me() -> dict[str, Any]:
    """Get today's brief: today's meetings, mail from the last 24 hours and reminders due or overdue.

    Use when the user asks for their brief, their day, or "brief me".
    Read-only. Summarise the returned ``brief`` for the user; keep any
    "unavailable" line.

    Returns:
        A dict with ``status`` ("ok") and ``brief`` (plain text).
    """
    return {"status": "ok", "brief": build_brief_text()}


# ---- what the orchestrator says about the event --------------------------------

def describe(event: ContextEvent) -> str:
    """One line for the ``event_received`` thought."""
    meta = event.metadata or {}

    def count(key: str, noun: str) -> str:
        src = meta.get(key) or {}
        if src.get("status") != "ok":
            return f"{noun} unavailable"
        return f"{len(src.get('items') or [])} {noun}"

    text = (
        f"Morning brief for {meta.get('day', 'today')}: "
        f"{count('calendar', 'events')}, {count('mail', 'emails')}, "
        f"{count('reminders', 'reminders due')}"
    )
    waiting = meta.get("waiting_on")
    if isinstance(waiting, dict) and waiting.get("status") == "ok":
        text += f", {len(waiting.get('items') or [])} waiting"
    gaps = (meta.get("lookahead") or {}).get("gaps") if isinstance(meta.get("lookahead"), dict) else None
    if gaps:
        text += f", {len(gaps)} look-ahead gaps"
    return text


def build_prompt(event: ContextEvent) -> str:
    """The main-engine prompt for a ``daily_brief`` event."""
    text = str((event.metadata or {}).get("text") or "")
    return (
        "You are an on-device macOS assistant. Write the user's morning "
        "brief from the data below. The data is already gathered: do NOT "
        "call any tool, and do not ask for approval. Reply with the brief "
        "as your final answer.\n\n"
        "Rules:\n"
        "- At most 8 short lines, plain text, most urgent first.\n"
        "- Meetings: time and title, in order.\n"
        "- Mail: name only the messages that look like they need a reply "
        "from the user (a person asking something); give sender and "
        "subject. Skip receipts and notifications. Never guess what a "
        "message says beyond its subject.\n"
        "- Reminders: overdue first.\n"
        "- Waiting on: who the user is waiting on, with the subject.\n"
        "- Look-ahead: keep each Look-ahead line as written (they are "
        "checked facts), including a \"Look-ahead skipped\" line.\n"
        "- If a line says a source is unavailable, keep that line with its "
        "reason. Never drop it and never invent items.\n"
        "- If there is nothing at all, say so in one line.\n\n"
        f"Data:\n{text}"
    )


# ---- scheduler -----------------------------------------------------------------

def _read_last_run(path: Path) -> Optional[datetime]:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("last_run")
        stamp = datetime.fromisoformat(str(value))
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return stamp if stamp.tzinfo is not None else stamp.astimezone()


def _write_last_run(path: Path, stamp: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".daily-brief-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"last_run": stamp.isoformat()}, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def make_event(payload: dict[str, Any], trigger: str = "schedule") -> ContextEvent:
    return ContextEvent(event_type=EVENT_TYPE, metadata={**payload, "trigger": trigger})


class DailyBriefScheduler:
    """Publish one ``daily_brief`` event per day at the set local time.

    Fires when ``last run < today's brief time <= now``: once per day, not
    again after a restart, and at the next check when the time was missed
    (same day only; yesterday's missed brief is not replayed). Moving the
    time to later the same day arms it again.
    """

    def __init__(
        self,
        bus: EventBus,
        state_path: Path | str | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        clock: Callable[[], datetime] = _now,
        gather: Optional[Callable[[datetime], dict[str, Any]]] = None,
        enabled: Callable[[], bool] = brief_enabled,
        time_of_day: Callable[[], dtime] = brief_time,
        startup_delay: float = DEFAULT_STARTUP_DELAY,
        idea_state: Any = None,
        feedback: Any = None,
    ) -> None:
        self._bus = bus
        # Cooldowns (``ai.idea_sources.IdeaState``) and declined ideas
        # (``IdeaFeedback``) for the offers after the brief; main.py attaches
        # the shared ones (:meth:`attach_ideas`).
        self.idea_state = idea_state
        self.feedback = feedback
        self._state_path = Path(state_path).expanduser() if state_path else DEFAULT_STATE_PATH
        self._poll_interval = poll_interval
        self._clock = clock
        self._gather = gather or gather_brief
        self._enabled = enabled
        self._time_of_day = time_of_day
        self._startup_delay = startup_delay
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._warned = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="DailyBriefScheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=5.0)

    def _run(self) -> None:
        logger.info("DailyBriefScheduler started (check every %.0fs)", self._poll_interval)
        if self._stop_event.wait(self._startup_delay):
            return
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:
                if not self._warned:
                    logger.warning("Daily brief check failed (%s); will keep retrying", exc)
                    self._warned = True
            if self._stop_event.wait(self._poll_interval):
                return

    def is_due(self, now: Optional[datetime] = None) -> bool:
        now = now or self._clock()
        if not self._enabled():
            return False
        due = datetime.combine(now.date(), self._time_of_day(), tzinfo=now.tzinfo)
        if now < due:
            return False
        last = _read_last_run(self._state_path)
        return last is None or last < due

    def check_once(self) -> Optional[ContextEvent]:
        """Publish today's brief if it is due; return the event, else None."""
        now = self._clock()
        if not self.is_due(now):
            return None
        event = make_event(self._gather(now), trigger="schedule")
        # Push first: if the bus isn't ready the brief is retried, not lost.
        self._bus.push_threadsafe(event)
        _write_last_run(self._state_path, now)
        # Offers follow the scheduled brief only, never "Brief me now".
        try:
            self.push_offers(event.metadata or {})
        except Exception:
            logger.exception("daily brief offers failed")
        return event

    def attach_ideas(self, idea_state: Any, feedback: Any = None) -> None:
        self.idea_state = idea_state
        self.feedback = feedback

    def _state(self) -> Any:
        if self.idea_state is None:
            from ai.idea_sources import IdeaState

            self.idea_state = IdeaState(None)  # in memory: no file of our own
        return self.idea_state

    def _declined(self, idea: dict[str, Any]) -> bool:
        check = getattr(self.feedback, "resembles_rejected", None)
        if check is None:
            return False
        try:
            return bool(check(str(idea.get("tool_name") or ""), str(idea.get("description") or "")))
        except Exception:
            logger.exception("daily brief: feedback lookup failed")
            return False

    def offers_for(self, payload: dict[str, Any]) -> list[ContextEvent]:
        """The offer events for a brief payload, with their cooldowns started.

        #73: a "draft a nudge?" idea per waiting item, at most
        :data:`MAX_NUDGES`, one per message id per :data:`NUDGE_COOLDOWN`.
        #77: each look-ahead gap's offer, one per gap key per
        :data:`LOOKAHEAD_COOLDOWN`, dropped when it resembles a declined idea.
        """
        from ai import waiting_on as wo

        from . import lookahead

        state = self._state()
        out: list[ContextEvent] = []
        waiting = payload.get("waiting_on") or {}
        if waiting.get("status") == "ok":
            nudges = 0
            for item in waiting.get("items") or []:
                if nudges >= MAX_NUDGES:
                    break
                key = str(item.get("message_id") or "")
                if not key or state.active(NUDGE_SECTION, key):
                    continue
                state.start(NUDGE_SECTION, key, NUDGE_COOLDOWN)
                out.append(ContextEvent(event_type="pattern_detected", metadata=wo.nudge_idea(item)))
                nudges += 1
        look = payload.get("lookahead") or {}
        for offer in lookahead.offer_events(look) if isinstance(look, dict) else []:
            meta = offer.metadata or {}
            key = str(meta.get("lookahead_key") or "")
            if not key or state.active(LOOKAHEAD_SECTION, key) or self._declined(meta):
                continue
            state.start(LOOKAHEAD_SECTION, key, LOOKAHEAD_COOLDOWN)
            out.append(offer)
        return out

    def push_offers(self, payload: dict[str, Any]) -> list[ContextEvent]:
        offers = self.offers_for(payload)
        for offer in offers:
            self._bus.push_threadsafe(offer)
        return offers

    def run_now(self) -> ContextEvent:
        """Publish a brief now ("Brief me now"). Doesn't count as today's run."""
        event = make_event(self._gather(self._clock()), trigger="manual")
        self._bus.push_threadsafe(event)
        return event
