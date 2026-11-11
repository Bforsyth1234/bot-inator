"""Right-moment delivery (#87): hold offers at bad times, cap them, real pause.

Nothing here calls a model. The orchestrator asks :meth:`Delivery.decide`
at the top of ``_handle_event`` (before the activity history, the idea
sources and the agent), so no model call is spent on an offer nobody will
see and the 120 s approval timer never runs while the user can't answer.

What is held (decision 1): only agent-initiated work, the event types in
:data:`AGENT_INITIATED` (``pattern_detected`` ideas and the ambient agent
turns ``mail_received`` / ``imessage_received`` / ``file``). A chat or
quick-ask turn (``user_message``) and anything carrying
:data:`BYPASS_KEY` (an offer the user opened from the Inbox) always runs.
Notices never reach this gate.

Busy (decision 3) is deterministic, see :class:`BusyGate`: a timed calendar
event in progress, a known screen-share process running, a key pressed in
the last 3 s, or Pause. A check that cannot be read counts as "not busy"
and its error is kept in :attr:`BusyGate.checks` for the Debug tab.

Held offers expire (decision 4, :func:`shelf_life`). An expired idea is
recorded in the :class:`~ai.idea_outcomes.OutcomeStore` as ``expired``
(never ``timed_out``); an expired ambient event is just dropped.

Daily cap (decision 5): once :meth:`Delivery.cards_today` reaches
``daily_cap`` (default 8), a ``pattern_detected`` idea goes to the Inbox
instead of a window. Ambient turns are not capped at the gate: they are not
offers yet (most end in "no action"), and their built-in follow-ups arrive
as ``pattern_detected`` ideas, which are.

Pause (decision 6): while paused, agent-initiated events are held (reason
``paused``) and every other non-user event is dropped: no turn, no offer, no
thought, no activity-history row. Listeners keep running, so Resume needs no
restart. A held event released after a pause is not written to the
activity history either (:attr:`Decision.record_activity`).

Never-watch apps (decision 7): :func:`is_never_watched` (bundle ids, or app
names for entries without a dot), seeded with password managers. The app
listener, the clipboard watcher and ``read_active_tab_text`` consult it.

Files (both 0600, atomic, in ``~/.bot-inator/``):

* ``delivery_settings.json`` (:class:`DeliverySettings`): ``daily_cap``,
  the user's extra never-watch entries and the seeds they removed.
  Override with ``AGENT_DELIVERY_SETTINGS_PATH``.
* ``held_offers.json`` (:class:`Delivery`): held offers, the Inbox, the
  pause deadline and today's counters. Override with
  ``AGENT_HELD_OFFERS_PATH``. Message text (``metadata["text"]``, i.e.
  iMessages) is never written: such events are held in memory only.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

Clock = Callable[[], float]

DATA_DIR = Path.home() / ".bot-inator"

AGENT_INITIATED: frozenset[str] = frozenset({
    "pattern_detected", "mail_received", "imessage_received", "file",
})
NEVER_HELD: frozenset[str] = frozenset({"user_message", "notice"})
BYPASS_KEY = "delivery_bypass"
HELD_SINCE_KEY = "held_since"
HELD_REASON_KEY = "held_reason"
PAUSED_FLAG = "held_in_pause"

DEFAULT_DAILY_CAP = 8
MAX_DAILY_CAP = 100
MAX_HELD = 50
MAX_INBOX = 30
MAX_NEVER_WATCH = 100
TYPING_IDLE_S = 3.0
CALENDAR_TTL_S = 60.0
SCREEN_SHARE_TTL_S = 5.0
RELEASE_INTERVAL_S = 10.0
RELEASE_PER_TICK = 1
PAUSE_DURATIONS = ("1h", "tomorrow", "indefinite")
TOMORROW_HOUR = 8

# Password managers and the system keychain (decision 7). Unknown ids are
# harmless; a missing one means that app's activations are reported.
NEVER_WATCH_SEED: tuple[str, ...] = (
    "com.1password.1password",
    "com.agilebits.onepassword7",
    "com.agilebits.onepassword-osx",
    "com.bitwarden.desktop",
    "com.lastpass.LastPass",
    "com.dashlane.dashlanephonefinal",
    "com.keepersecurity.passwordmanager",
    "org.keepassxc.keepassxc",
    "in.sinew.Enpass-Desktop",
    "com.nordsec.nordpass",
    "me.proton.pass.electron",
    "com.apple.keychainaccess",
    "com.apple.Passwords",
)

# Processes that run only while this Mac's screen is being shared.
# Browser-based sharing (Meet, Teams on the web) is not visible this way;
# the calendar check usually covers those meetings.
SCREEN_SHARE_PROCESSES: frozenset[str] = frozenset({
    "CptHost",          # Zoom, while sharing
    "screensharingd",   # macOS Screen Sharing / Remote Management session
    "ScreenSharingAgent",
})

# Shelf life (seconds) per idea kind (``trigger``) and per event type.
_HOUR = 3600.0
_DAY = 24 * _HOUR
KIND_SHELF_LIFE: dict[str, float] = {
    "meeting_with_video_link": 10 * 60.0,  # fallback when the start is unknown
    "mail_deadline": _DAY,
    "mail_invite": _DAY,
    "mail_needs_reply": _DAY,
    "mail_shipping": _DAY,
    "open_help": _HOUR,
}
EVENT_SHELF_LIFE: dict[str, float] = {
    "mail_received": _DAY,
    "imessage_received": 2 * _HOUR,
    "file": 2 * _HOUR,
}
DEFAULT_SHELF_LIFE = 4 * _HOUR
_START_KEYS = ("event_start", "meeting_start", "start", "starts_at")


def _atomic_write(path: Optional[Path], data: Mapping[str, Any]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, **data}, fh, indent=1, default=str)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError):
        logger.exception("failed to save %s", path)


def _read_json(path: Optional[Path]) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("%s unreadable; starting empty", path)
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Settings: daily cap + never-watch list
# ---------------------------------------------------------------------------


def default_settings_path() -> Path:
    override = os.environ.get("AGENT_DELIVERY_SETTINGS_PATH")
    return Path(override).expanduser() if override else DATA_DIR / "delivery_settings.json"


def default_held_path() -> Path:
    override = os.environ.get("AGENT_HELD_OFFERS_PATH")
    return Path(override).expanduser() if override else DATA_DIR / "held_offers.json"


def _clean_entry(value: Any) -> str:
    return " ".join(str(value or "").split())[:200]


class DeliverySettings:
    """``daily_cap`` and the never-watch list; reloads when the file changes."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._mtime: Optional[float] = None
        self.daily_cap = DEFAULT_DAILY_CAP
        self.extra: list[str] = []
        self.removed: list[str] = []
        self._load()

    def _load(self) -> None:
        data = _read_json(self.path)
        try:
            cap = int(data.get("daily_cap", DEFAULT_DAILY_CAP))
        except (TypeError, ValueError):
            cap = DEFAULT_DAILY_CAP
        self.daily_cap = max(0, min(cap, MAX_DAILY_CAP))
        self.extra = [_clean_entry(e) for e in data.get("never_watch_extra", []) if _clean_entry(e)]
        self.removed = [_clean_entry(e) for e in data.get("never_watch_removed", []) if _clean_entry(e)]
        try:
            self._mtime = self.path.stat().st_mtime if self.path is not None else None
        except OSError:
            self._mtime = None

    def _maybe_reload(self) -> None:
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._load()

    def never_watch(self) -> list[str]:
        with self._lock:
            self._maybe_reload()
            removed = {e.lower() for e in self.removed}
            out = [e for e in NEVER_WATCH_SEED if e.lower() not in removed]
            out += [e for e in self.extra if e.lower() not in {o.lower() for o in out}]
            return out

    def cap(self) -> int:
        with self._lock:
            self._maybe_reload()
            return self.daily_cap

    def update(self, *, daily_cap: Optional[int] = None,
               never_watch: Optional[Iterable[str]] = None) -> None:
        with self._lock:
            self._maybe_reload()
            if daily_cap is not None:
                self.daily_cap = max(0, min(int(daily_cap), MAX_DAILY_CAP))
            if never_watch is not None:
                wanted = []
                for e in never_watch:
                    e = _clean_entry(e)
                    if e and e.lower() not in {w.lower() for w in wanted}:
                        wanted.append(e)
                wanted = wanted[:MAX_NEVER_WATCH]
                low = {w.lower() for w in wanted}
                seeds = {s.lower() for s in NEVER_WATCH_SEED}
                self.extra = [w for w in wanted if w.lower() not in seeds]
                self.removed = [s for s in NEVER_WATCH_SEED if s.lower() not in low]
            _atomic_write(self.path, {"daily_cap": self.daily_cap,
                                      "never_watch_extra": self.extra,
                                      "never_watch_removed": self.removed})
            try:
                self._mtime = self.path.stat().st_mtime if self.path is not None else None
            except OSError:
                self._mtime = None

    def view(self) -> dict[str, Any]:
        return {"daily_cap": self.cap(), "never_watch": self.never_watch(),
                "never_watch_seed": list(NEVER_WATCH_SEED)}


_settings_lock = threading.Lock()
_default_settings: Optional[DeliverySettings] = None


def default_settings() -> DeliverySettings:
    global _default_settings
    with _settings_lock:
        if _default_settings is None:
            _default_settings = DeliverySettings(default_settings_path())
        return _default_settings


def set_default_settings(settings: Optional[DeliverySettings]) -> None:
    """Tests (and the runner) swap the process-wide settings object."""
    global _default_settings
    with _settings_lock:
        _default_settings = settings


def is_never_watched(bundle_id: Optional[str] = None, app_name: Optional[str] = None,
                     settings: Optional[DeliverySettings] = None) -> bool:
    """True when the app is on the never-watch list. Never raises.

    Entries with a dot are bundle ids; entries without one are app names.
    Both compare case-insensitively.
    """
    try:
        entries = (settings or default_settings()).never_watch()
    except Exception:
        logger.exception("never-watch list unreadable; using the seed")
        entries = list(NEVER_WATCH_SEED)
    bid = (bundle_id or "").strip().lower()
    name = (app_name or "").strip().lower()
    for entry in entries:
        low = entry.lower()
        if "." in low:
            if bid and bid == low:
                return True
        elif name and name == low:
            return True
    return False


# ---------------------------------------------------------------------------
# Busy sources
# ---------------------------------------------------------------------------


def _calendar_events_now(now: datetime) -> list[dict[str, Any]]:
    from tools import calendar_access  # lazy: tools imports the AI stack

    return calendar_access.list(now - timedelta(hours=12), now + timedelta(minutes=1))


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _running_processes() -> set[str]:
    out = subprocess.run(["ps", "-axco", "comm"], capture_output=True, text=True,
                         timeout=2.0, check=True).stdout
    return {line.strip() for line in out.splitlines()[1:] if line.strip()}


def _seconds_since_key() -> float:
    import Quartz  # type: ignore[import-not-found]

    return float(Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGEventKeyDown))


class BusyGate:
    """``busy()`` -> a reason (``paused``/``meeting``/``screen_sharing``/
    ``typing``) or None. Each source is injectable; a source that raises
    counts as not busy and its error is kept in :attr:`checks`."""

    def __init__(
        self,
        *,
        calendar: Callable[[datetime], list[dict[str, Any]]] = _calendar_events_now,
        processes: Callable[[], set[str]] = _running_processes,
        seconds_since_key: Callable[[], float] = _seconds_since_key,
        paused: Callable[[], bool] = lambda: False,
        clock: Clock = time.time,
        calendar_ttl: float = CALENDAR_TTL_S,
        typing_idle: float = TYPING_IDLE_S,
    ) -> None:
        self._calendar = calendar
        self._processes = processes
        self._seconds_since_key = seconds_since_key
        self._paused = paused
        self._clock = clock
        self._calendar_ttl = calendar_ttl
        self._typing_idle = typing_idle
        self._cal_cache: Optional[tuple[float, Any]] = None
        self._proc_cache: Optional[tuple[float, set[str]]] = None
        self._lock = threading.Lock()
        self.checks: dict[str, str] = {}

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc).astimezone()

    def in_meeting(self) -> bool:
        now_ts = self._clock()
        with self._lock:
            cached = self._cal_cache
            if cached is None or now_ts - cached[0] >= self._calendar_ttl:
                try:
                    events = list(self._calendar(self._now()) or [])
                except Exception as exc:
                    # A failure is cached too: a slow or missing calendar is
                    # not asked again on every event within the minute.
                    self._cal_cache = (now_ts, exc)
                    raise
                self._cal_cache = (now_ts, events)
            elif isinstance(cached[1], Exception):
                raise cached[1]
            else:
                events = cached[1]
        now = self._now()
        for ev in events:
            if not isinstance(ev, Mapping) or ev.get("all_day"):
                continue
            start, end = _parse_dt(ev.get("start")), _parse_dt(ev.get("end"))
            if start is not None and end is not None and start <= now < end:
                return True
        return False

    def screen_shared(self) -> bool:
        now_ts = self._clock()
        with self._lock:
            cached = self._proc_cache
            if cached is None or now_ts - cached[0] >= SCREEN_SHARE_TTL_S:
                procs = set(self._processes() or ())
                self._proc_cache = (now_ts, procs)
            else:
                procs = cached[1]
        return bool(procs & SCREEN_SHARE_PROCESSES)

    def typing(self) -> bool:
        return float(self._seconds_since_key()) < self._typing_idle

    def busy(self) -> Optional[str]:
        checks: list[tuple[str, Callable[[], bool], str]] = [
            ("pause", self._paused, "paused"),
            ("calendar", self.in_meeting, "meeting"),
            ("screen_share", self.screen_shared, "screen_sharing"),
            ("typing", self.typing, "typing"),
        ]
        reason: Optional[str] = None
        for name, check, label in checks:
            if reason is not None:
                self.checks.setdefault(name, "not checked")
                continue
            try:
                hit = bool(check())
            except Exception as exc:  # noqa: BLE001 - a failed check holds nothing
                self.checks[name] = f"unavailable: {type(exc).__name__}: {str(exc)[:120]}"
                continue
            self.checks[name] = "busy" if hit else "ok"
            if hit:
                reason = label
        return reason


# ---------------------------------------------------------------------------
# Held offers
# ---------------------------------------------------------------------------


def _event_to_dict(event: Any) -> dict[str, Any]:
    ts = getattr(event, "timestamp", None)
    return {
        "event_type": event.event_type,
        "app_name": getattr(event, "app_name", None),
        "window_title": getattr(event, "window_title", None),
        "timestamp": ts.isoformat() if isinstance(ts, datetime) else None,
        "metadata": dict(getattr(event, "metadata", None) or {}),
    }


def _event_from_dict(data: Mapping[str, Any]) -> Any:
    from events.event_bus import ContextEvent

    kwargs: dict[str, Any] = {
        "event_type": str(data.get("event_type") or ""),
        "app_name": data.get("app_name"),
        "window_title": data.get("window_title"),
        "metadata": dict(data.get("metadata") or {}),
    }
    ts = _parse_dt(data.get("timestamp"))
    if ts is not None:
        kwargs["timestamp"] = ts
    return ContextEvent(**kwargs)


def _idea_kind(event_type: str, meta: Mapping[str, Any]) -> str:
    if event_type == "pattern_detected":
        return str(meta.get("trigger") or "")
    return event_type


def shelf_life(event_type: str, metadata: Mapping[str, Any], held_since: float) -> float:
    """The time (epoch seconds) a held offer stops being worth showing.

    A meeting link lasts until the meeting starts (``event_start`` / ``start``
    when the idea carries it, else 10 minutes: the calendar watcher announces
    meetings 10 minutes ahead); mail follow-ups a day; a time routine until
    the end of its weekday-hour slot; other kinds per the tables above.
    """
    meta = metadata or {}
    kind = _idea_kind(event_type, meta)
    if kind == "meeting_with_video_link":
        for key in _START_KEYS:
            start = _parse_dt(meta.get(key))
            if start is not None:
                return start.timestamp()
    if kind == "time_routine":
        slot = datetime.fromtimestamp(held_since).astimezone()
        end = slot.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return end.timestamp()
    if event_type == "pattern_detected":
        return held_since + KIND_SHELF_LIFE.get(kind, DEFAULT_SHELF_LIFE)
    return held_since + EVENT_SHELF_LIFE.get(event_type, DEFAULT_SHELF_LIFE)


def _title(event_type: str, meta: Mapping[str, Any]) -> str:
    for key in ("card_text", "description", "tool_name"):
        text = " ".join(str(meta.get(key) or "").split())
        if text:
            return text[:160]
    return event_type


@dataclass
class HeldOffer:
    id: str
    event: dict[str, Any]
    reason: str
    held_since: float
    expires_at: float
    persist: bool = True

    @property
    def event_type(self) -> str:
        return str(self.event.get("event_type") or "")

    @property
    def meta(self) -> dict[str, Any]:
        return self.event.get("metadata") or {}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "event": self.event, "reason": self.reason,
                "held_since": self.held_since, "expires_at": self.expires_at}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Optional["HeldOffer"]:
        try:
            return cls(id=str(data["id"]), event=dict(data["event"]),
                       reason=str(data.get("reason") or ""),
                       held_since=float(data["held_since"]),
                       expires_at=float(data["expires_at"]))
        except (KeyError, TypeError, ValueError):
            return None

    def summary(self) -> dict[str, Any]:
        meta = self.meta
        return {
            "id": self.id,
            "event_type": self.event_type,
            "source": str(meta.get("source") or ""),
            "kind": _idea_kind(self.event_type, meta),
            "title": _title(self.event_type, meta),
            "tool_name": str(meta.get("builtin_tool") or meta.get("tool_name") or ""),
            "reason": self.reason,
            "held_since": _iso(self.held_since),
            "expires_at": _iso(self.expires_at),
        }


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None or not math.isfinite(ts):
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


@dataclass
class Decision:
    """What :meth:`Delivery.decide` did with an event.

    ``action``: ``run`` (handle as usual), ``hold`` (queued until not busy),
    ``inbox`` (over the daily cap) or ``drop`` (paused, not agent-initiated).
    ``record_activity`` is False while paused and for an event that was
    held by a pause (#75 must not see paused time).
    """

    action: str
    reason: str = ""
    record_activity: bool = True
    offer_id: Optional[str] = None

    @property
    def message(self) -> str:
        if self.action == "hold":
            return f"Held until you're free ({self.reason.replace('_', ' ')})."
        if self.action == "inbox":
            return "Today's suggestion limit is reached; saved to the Inbox."
        return ""


def _local_day(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d")


class Delivery:
    """The gate, the held queue, the Inbox, Pause and the daily count."""

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        settings: Optional[DeliverySettings] = None,
        gate: Optional[BusyGate] = None,
        outcomes: Optional[Any] = None,
        clock: Clock = time.time,
        on_change: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.settings = settings if settings is not None else default_settings()
        self.clock = clock
        self.gate = gate if gate is not None else BusyGate(clock=clock)
        self.gate._paused = lambda: self.paused  # pause is one of the busy checks
        self.outcomes = outcomes
        self.on_change = on_change
        self._lock = threading.RLock()
        self.held: list[HeldOffer] = []
        self.inbox: list[HeldOffer] = []
        self.paused_until: Optional[float] = None  # math.inf = until resumed
        self._day = ""
        self._ambient_cards = 0
        self._user_opened = 0
        self.last_reason: str = ""
        self._load()

    # ---- persistence --------------------------------------------------

    def _load(self) -> None:
        data = _read_json(self.path)
        self.held = [h for h in (HeldOffer.from_dict(d) for d in data.get("held", [])) if h][-MAX_HELD:]
        self.inbox = [h for h in (HeldOffer.from_dict(d) for d in data.get("inbox", [])) if h][-MAX_INBOX:]
        pu = data.get("paused_until")
        if pu == "indefinite":
            self.paused_until = math.inf
        elif isinstance(pu, (int, float)):
            self.paused_until = float(pu)
        counters = data.get("counters") or {}
        self._day = str(counters.get("day") or "")
        self._ambient_cards = int(counters.get("ambient_cards") or 0)
        self._user_opened = int(counters.get("user_opened") or 0)

    def _save(self) -> None:
        pu: Any = self.paused_until
        if pu is not None and math.isinf(pu):
            pu = "indefinite"
        _atomic_write(self.path, {
            "held": [h.to_dict() for h in self.held if h.persist],
            "inbox": [h.to_dict() for h in self.inbox if h.persist],
            "paused_until": pu,
            "counters": {"day": self._day, "ambient_cards": self._ambient_cards,
                         "user_opened": self._user_opened},
        })

    def _changed(self) -> None:
        self._save()
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                logger.exception("delivery on_change failed")

    # ---- pause ----------------------------------------------------------

    @property
    def paused(self) -> bool:
        pu = self.paused_until
        return pu is not None and self.clock() < pu

    def pause(self, duration: Optional[str] = "indefinite", *,
              minutes: Optional[int] = None) -> Optional[float]:
        """Pause for ``1h`` / ``tomorrow`` (08:00 local; before 05:00 that is
        today) / ``indefinite``, or ``minutes``. Returns the deadline."""
        now = self.clock()
        if minutes is not None:
            until = now + max(1, int(minutes)) * 60.0
        elif duration in (None, "indefinite"):
            until = math.inf
        elif duration == "1h":
            until = now + _HOUR
        elif duration == "tomorrow":
            local = datetime.fromtimestamp(now).astimezone()
            day = local if local.hour < 5 else local + timedelta(days=1)
            until = day.replace(hour=TOMORROW_HOUR, minute=0, second=0, microsecond=0).timestamp()
        else:
            raise ValueError(f"duration must be one of {PAUSE_DURATIONS}")
        with self._lock:
            self.paused_until = until
            self._changed()
        return until

    def resume(self) -> None:
        with self._lock:
            self.paused_until = None
            self._changed()

    # ---- daily count ----------------------------------------------------

    def _roll_day(self) -> None:
        today = _local_day(self.clock())
        if today != self._day:
            self._day, self._ambient_cards, self._user_opened = today, 0, 0

    def note_ambient_card(self) -> None:
        """An ambient turn (mail / message / file) sent an approval card."""
        with self._lock:
            self._roll_day()
            self._ambient_cards += 1
            self._save()

    def cards_today(self) -> int:
        """Agent-initiated cards sent today: idea offers in the OutcomeStore
        (not chat, not expired) plus ambient-turn cards, minus offers the user
        opened from the Inbox (the cap never counts those)."""
        with self._lock:
            self._roll_day()
            midnight = datetime.fromtimestamp(self.clock()).astimezone().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            ideas = 0
            store = self.outcomes
            if store is not None:
                try:
                    lock = getattr(store, "_lock", None) or threading.Lock()
                    with lock:
                        rows = list(getattr(store, "offers", []) or [])
                    ideas = sum(
                        1 for o in rows
                        if float(o.get("shown_at") or 0) >= midnight
                        and o.get("source") != "chat"
                        and o.get("verdict") != "expired"
                    )
                except Exception:
                    logger.exception("outcome count unreadable; counting 0")
            return max(0, ideas + self._ambient_cards - self._user_opened)

    # ---- the gate -------------------------------------------------------

    def decide(self, event: Any) -> Decision:
        """Run, hold, inbox or drop ``event``; queues it when not ``run``.

        Blocking (the calendar check); call it with ``asyncio.to_thread``.
        """
        etype = getattr(event, "event_type", "")
        meta = event.metadata if isinstance(getattr(event, "metadata", None), dict) else {}
        if etype in NEVER_HELD or meta.get(BYPASS_KEY):
            return Decision("run")
        held_by_pause = bool(meta.get(PAUSED_FLAG))
        gated = etype in AGENT_INITIATED
        if self.paused:
            if not gated:
                return Decision("drop", "paused", record_activity=False)
            return self._hold(event, "paused")
        if not gated:
            return Decision("run")
        reason = self.gate.busy()
        self.last_reason = reason or ""
        if reason is not None:
            return self._hold(event, reason)
        if etype == "pattern_detected" and self.cards_today() >= self.settings.cap():
            return self._to_inbox(event, record_activity=not held_by_pause)
        return Decision("run", record_activity=not held_by_pause)

    def _entry(self, event: Any, reason: str) -> Optional[HeldOffer]:
        meta = dict(event.metadata or {})
        try:
            since = float(meta.get(HELD_SINCE_KEY))
        except (TypeError, ValueError):
            since = self.clock()
        entry = HeldOffer(
            id=f"held_{uuid.uuid4().hex[:12]}", event=_event_to_dict(event),
            reason=reason, held_since=since,
            expires_at=shelf_life(event.event_type, meta, since),
            persist="text" not in meta,
        )
        if reason == "paused" or meta.get(PAUSED_FLAG):
            # Happened while paused: the activity history never records it.
            entry.event["metadata"][PAUSED_FLAG] = True
        if entry.expires_at <= self.clock():
            self._record_expired(entry)
            return None
        return entry

    def _hold(self, event: Any, reason: str) -> Decision:
        with self._lock:
            entry = self._entry(event, reason)
            if entry is None:
                return Decision("drop", "expired", record_activity=False)
            self.held.append(entry)
            while len(self.held) > MAX_HELD:
                self._record_expired(self.held.pop(0))
            self._changed()
        return Decision("hold", reason, record_activity=False, offer_id=entry.id)

    def _to_inbox(self, event: Any, record_activity: bool = True) -> Decision:
        with self._lock:
            entry = self._entry(event, "daily_cap")
            if entry is None:
                return Decision("drop", "expired", record_activity=False)
            self.inbox.append(entry)
            while len(self.inbox) > MAX_INBOX:
                self._record_expired(self.inbox.pop(0))
            self._changed()
        return Decision("inbox", "daily_cap", record_activity=record_activity, offer_id=entry.id)

    # ---- expiry + release ----------------------------------------------

    def _record_expired(self, entry: HeldOffer) -> None:
        """An idea that expired unseen is ``expired`` in #79 (never timed_out)."""
        if entry.event_type != "pattern_detected" or self.outcomes is None:
            return
        meta = entry.meta
        tool = str(meta.get("builtin_tool") or meta.get("tool_name") or "")
        try:
            self.outcomes.record_offer(
                meta.get("source") or "", tool, "expired",
                idea_kind=str(meta.get("trigger") or ""),
                one_shot=bool(meta.get("builtin_tool")),
            )
        except ValueError:
            logger.warning("OutcomeStore has no 'expired' verdict yet; not recorded")
        except Exception:
            logger.exception("recording an expired offer failed")

    def expire(self) -> int:
        """Drop held and Inbox offers past their shelf life; returns how many."""
        now = self.clock()
        with self._lock:
            gone = [h for h in self.held + self.inbox if h.expires_at <= now]
            if not gone:
                return 0
            ids = {h.id for h in gone}
            self.held = [h for h in self.held if h.id not in ids]
            self.inbox = [h for h in self.inbox if h.id not in ids]
            for entry in gone:
                self._record_expired(entry)
            self._changed()
        return len(gone)

    def release_due(self, limit: int = RELEASE_PER_TICK) -> list[Any]:
        """Expire, then, when not busy, pop up to ``limit`` held events
        (oldest first) to put back on the bus with ``held_since`` set."""
        self.expire()
        with self._lock:
            if not self.held:
                return []
        if self.paused or self.gate.busy() is not None:
            return []
        out: list[Any] = []
        with self._lock:
            while self.held and len(out) < limit:
                entry = self.held.pop(0)
                event = _event_from_dict(entry.event)
                event.metadata[HELD_SINCE_KEY] = entry.held_since
                event.metadata[HELD_REASON_KEY] = entry.reason
                out.append(event)
            if out:
                self._changed()
        return out

    async def run_release_loop(self, bus: Any, interval: float = RELEASE_INTERVAL_S) -> None:
        """Forever: release held events onto ``bus`` when the user is free."""
        while True:
            try:
                for event in await asyncio.to_thread(self.release_due):
                    await bus.push(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("delivery release tick failed")
            await asyncio.sleep(interval)

    # ---- inbox ----------------------------------------------------------

    def open_inbox(self, offer_id: str) -> Optional[Any]:
        """Take an Inbox offer out; the event to push, marked user-caused."""
        with self._lock:
            for i, entry in enumerate(self.inbox):
                if entry.id == offer_id:
                    self.inbox.pop(i)
                    self._roll_day()
                    self._user_opened += 1
                    self._changed()
                    event = _event_from_dict(entry.event)
                    event.metadata[BYPASS_KEY] = "inbox"
                    return event
        return None

    def dismiss_inbox(self, offer_id: str) -> bool:
        with self._lock:
            for i, entry in enumerate(self.inbox):
                if entry.id == offer_id:
                    self.inbox.pop(i)
                    self._changed()
                    return True
        return False

    # ---- views ----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            held = [h.summary() for h in self.held]
            inbox = [h.summary() for h in self.inbox]
            pu = self.paused_until if self.paused else None
        reasons: dict[str, int] = {}
        for h in held:
            reasons[h["reason"]] = reasons.get(h["reason"], 0) + 1
        return {
            "paused": pu is not None,
            "paused_until": "indefinite" if pu is not None and math.isinf(pu) else _iso(pu),
            "held_count": len(held),
            "held_reasons": reasons,
            "held": held,
            "inbox_count": len(inbox),
            "inbox": inbox,
            "cards_today": self.cards_today(),
            "daily_cap": self.settings.cap(),
            "checks": dict(self.gate.checks),
        }

    def debug_line(self) -> str:
        """``held: N (reason, ...)`` for the Debug tab."""
        s = self.status()
        if not s["held_count"]:
            return "held: 0"
        why = ", ".join(f"{k} {v}" for k, v in sorted(s["held_reasons"].items()))
        return f"held: {s['held_count']} ({why})"


__all__ = [
    "AGENT_INITIATED", "BYPASS_KEY", "BusyGate", "Decision", "Delivery",
    "DeliverySettings", "HELD_REASON_KEY", "PAUSED_FLAG", "HELD_SINCE_KEY", "HeldOffer",
    "NEVER_WATCH_SEED", "PAUSE_DURATIONS", "default_held_path",
    "default_settings", "default_settings_path", "is_never_watched",
    "set_default_settings", "shelf_life",
]
