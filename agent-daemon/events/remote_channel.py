"""iPhone remote: ask and approve from your own Messages thread (#85).

The user texts their own iMessage handle from the phone. This channel polls
``chat.db`` for those rows, turns a ``yes <code>`` / ``no <code>`` reply into
the answer for exactly one approval card, and turns any other text into a
chat turn (``user_message``) whose answer is sent back to the same thread.

USER ANSWERS (2026-11-10) and DECISIONS (todo #85), binding:

1. OWN THREAD ONLY. A command is a row with ``is_from_me = 1`` on the
   iMessage service, in a chat whose only participant is the configured own
   handle. Received rows (``is_from_me = 0``), SMS, other handles and group
   chats are ignored, so a spoofed sender cannot reach it.
2. THE MODEL NEVER PICKS A RECIPIENT. :meth:`RemoteChannel._send` always
   sends to the configured handle, with the AppleScript template from
   ``tools/send_imessage.py`` (that tool stays unregistered).
3. APPROVE BY CODE. A mirrored card carries a 4-character code; ``yes K7Q2``
   resolves that request and no other. A bare ``yes`` does nothing. Codes are
   single use and end with the request (:meth:`forget`) or at their expiry.
4. NEVER FROM THE PHONE: code review (there is no API for it here),
   ``generate_custom_tool``, ``run_shortcut``, cards with #81 origin notes and
   cards with prepared actions (#76). They wait for the Mac.
5. Mirrored only while the Mac is idle (no input for N minutes) or when the
   turn came from the phone; such a request's timeout is
   :data:`REMOTE_TIMEOUT_S` (15 minutes). Ambient cards are not mirrored
   while Pause (#87) is on.
6. The text sent is the card's summary and arguments; at most
   :data:`MAX_PER_HOUR` messages an hour, all sends counted.
7. Any other text is a chat turn answered in the thread.

Egress (7c): the channel is OFF by default (``AGENT_REMOTE``). Every message
it sends is recorded in the privacy ledger (#82) with kind
:data:`KIND_IMESSAGE`.

The channel's own messages come back as ``is_from_me = 1`` rows in the same
thread. They are never read as commands: each sent body starts with
:data:`PREFIX` and is also remembered for ``ECHO_TTL_S``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
DEFAULT_POLL_INTERVAL = 5.0

# Settings keys (user_settings.ALLOWED_KEYS, runner). Read live.
ENABLED_KEY = "AGENT_REMOTE"              # "1"/"0", default "0" (7c opt-in)
HANDLE_KEY = "AGENT_REMOTE_HANDLE"        # own phone (E.164) or Apple ID email
IDLE_MINUTES_KEY = "AGENT_REMOTE_IDLE_MINUTES"  # whole minutes, 1..120
DEFAULT_IDLE_MINUTES = 10
MIN_IDLE_MINUTES = 1
MAX_IDLE_MINUTES = 120

REMOTE_TIMEOUT_S = 15 * 60.0
MAX_PER_HOUR = 20
CODE_LENGTH = 4
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
PREFIX = "bot-inator: "
REMOTE_SOURCE = "remote"  # metadata["source"] of a phone-originated user_message
IMESSAGE_SERVICE = "iMessage"

# Privacy ledger (#82). Runner: add to ``ai.egress_log.KINDS``.
KIND_IMESSAGE = "imessage"
EGRESS_HOST = "iMessage (Apple)"
EGRESS_TOOL = "remote_channel"
EGRESS_NOTE = "sent to your own Messages thread"

NEVER_FROM_PHONE: frozenset[str] = frozenset({"generate_custom_tool", "run_shortcut"})

MAX_ARG_CHARS = 200
MAX_BODY_CHARS = 1500
ECHO_TTL_S = 600.0

_REPLY_RE = re.compile(r"^\s*(yes|no)\b\s*([A-Za-z0-9]*)\s*[.!]*\s*$", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^\+?[\d\s().\-]{7,}$")


# ---- settings ---------------------------------------------------------------

def _setting(key: str, default: str) -> str:
    """Shell export, then the settings file (read live), then .env, then default."""
    import user_settings

    if key not in user_settings.shell_exported:
        saved = user_settings.read().get(key)
        if saved:
            return saved
    return os.environ.get(key) or default


def remote_enabled() -> bool:
    return _setting(ENABLED_KEY, "0").strip() == "1"


def normalize_handle(value: Any) -> str:
    """Email lower-cased; phone reduced to ``+`` and digits; else ``""``."""
    text = str(value or "").strip()
    if not text:
        return ""
    if _EMAIL_RE.match(text):
        return text.lower()
    if _PHONE_RE.match(text):
        digits = re.sub(r"\D", "", text)
        if len(digits) < 7:
            return ""
        return ("+" if text.startswith("+") else "") + digits
    return ""


def own_handle() -> str:
    return normalize_handle(_setting(HANDLE_KEY, ""))


def parse_idle_minutes(value: Any) -> Optional[int]:
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        return None
    minutes = int(text)
    return minutes if MIN_IDLE_MINUTES <= minutes <= MAX_IDLE_MINUTES else None


def idle_minutes() -> int:
    return parse_idle_minutes(_setting(IDLE_MINUTES_KEY, str(DEFAULT_IDLE_MINUTES))) or DEFAULT_IDLE_MINUTES


def same_handle(a: Any, b: Any) -> bool:
    """True when two handles name the same address (a missing country code
    on one phone number still matches on the last 10 digits)."""
    na, nb = normalize_handle(a), normalize_handle(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if "@" in na or "@" in nb:
        return False
    da, db = na.lstrip("+"), nb.lstrip("+")
    return len(da) >= 10 and len(db) >= 10 and da[-10:] == db[-10:]


def excluded_handle() -> Optional[str]:
    """The handle ``imessage_watcher`` leaves out of ``imessage_received``:
    the own thread, while the channel is on. ``None`` otherwise."""
    try:
        if not remote_enabled():
            return None
        return own_handle() or None
    except Exception:  # pragma: no cover - settings unreadable
        logger.exception("remote channel settings unreadable")
        return None


# ---- platform defaults ---------------------------------------------------------

def _seconds_since_any_input() -> float:
    """Seconds since the last keyboard/mouse input. 0 when unreadable (not idle)."""
    try:
        import Quartz  # type: ignore[import-not-found]

        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateCombinedSessionState, Quartz.kCGAnyInputEventType))
    except Exception:
        return 0.0


def applescript_send(handle: str, body: str) -> bool:
    """Send ``body`` to ``handle`` with ``tools/send_imessage``'s template."""
    from tools.send_imessage import _SCRIPT_TEMPLATE

    script = _SCRIPT_TEMPLATE.format(buddy=json.dumps(handle), message=json.dumps(body))
    try:
        subprocess.run(["osascript", "-e", script], check=True,
                       capture_output=True, text=True, timeout=10)
        return True
    except Exception as exc:
        logger.warning("remote channel: send failed: %s", getattr(exc, "stderr", None) or exc)
        return False


def text_from_attributed_body(blob: Any) -> str:
    """Best-effort text from a ``message.attributedBody`` typedstream (newer
    macOS leaves ``message.text`` NULL for many rows)."""
    if not isinstance(blob, (bytes, bytearray)):
        return ""
    data = bytes(blob)
    try:
        start = data.index(b"NSString") + len(b"NSString")
        plus = data.index(b"+", start)
        pos = plus + 1
        n = data[pos]
        pos += 1
        if n == 0x81:
            n = int.from_bytes(data[pos:pos + 2], "little")
            pos += 2
        elif n == 0x82:
            n = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        return data[pos:pos + n].decode("utf-8", errors="replace")
    except (ValueError, IndexError):
        return ""


# ---- channel ------------------------------------------------------------------------

@dataclass
class _Pending:
    request_id: str
    code: str
    tool_name: str
    expires_at: float


@dataclass
class _Row:
    rowid: int
    text: str
    is_from_me: bool
    service: str
    chat_id: Optional[int]
    associated: int = 0


@dataclass
class _Echo:
    text: str
    at: float


class RemoteChannel:
    """Poll the own thread; mirror approval cards; answer phone turns.

    ``sender(handle, body) -> bool`` sends one iMessage (tests inject a
    fake; the default runs AppleScript). ``submit(request_id, approved) ->
    bool`` resolves the orchestrator's waiting future (see
    :meth:`attach_orchestrator`). ``idle_seconds()`` and ``paused()`` are
    injectable for tests.
    """

    def __init__(
        self,
        bus: Optional[EventBus],
        db_path: Path | str = DEFAULT_DB_PATH,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        *,
        sender: Optional[Callable[[str, str], bool]] = None,
        submit: Optional[Callable[[str, bool], bool]] = None,
        idle_seconds: Callable[[], float] = _seconds_since_any_input,
        paused: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.time,
        egress: Any = None,
        max_per_hour: int = MAX_PER_HOUR,
        timeout_s: float = REMOTE_TIMEOUT_S,
    ) -> None:
        self._bus = bus
        self._db_path = Path(db_path).expanduser()
        self._poll_interval = poll_interval
        self._sender = sender or applescript_send
        self._submit = submit
        self._idle_seconds = idle_seconds
        self._paused = paused
        self._clock = clock
        self._egress = egress
        self.max_per_hour = max_per_hour
        self.timeout_s = float(timeout_s)
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}   # code -> pending
        self._by_request: dict[str, str] = {}     # request_id -> code
        self._sent_at: deque[float] = deque()
        self._echoes: list[_Echo] = []
        self._open_turns: set[str] = set()
        self._last_rowid: Optional[int] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._columns: Optional[set[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ---- settings (live) -------------------------------------------------

    @staticmethod
    def enabled() -> bool:
        return remote_enabled() and bool(own_handle())

    @staticmethod
    def handle() -> str:
        return own_handle()

    def mac_idle(self) -> bool:
        try:
            return float(self._idle_seconds()) >= idle_minutes() * 60.0
        except Exception:
            return False

    # ---- wiring helpers --------------------------------------------------

    def attach_orchestrator(self, orchestrator: Any, loop: asyncio.AbstractEventLoop) -> None:
        """Resolve phone answers through ``orchestrator.submit_approval_response``
        on ``loop`` (the same future the Mac card waits on)."""
        from schemas.ws_messages import ApprovalResponsePayload

        def submit(request_id: str, approved: bool) -> bool:
            payload = ApprovalResponsePayload(
                request_id=request_id, approved=approved,
                user_note="answered from your phone",
            )

            async def _do() -> bool:
                return bool(orchestrator.submit_approval_response(payload))

            try:
                return asyncio.run_coroutine_threadsafe(_do(), loop).result(timeout=5)
            except Exception:
                logger.exception("remote channel: submit failed")
                return False

        self._submit = submit

    # ---- approvals ---------------------------------------------------------

    def eligible(
        self,
        tool_name: str,
        *,
        origin_notes: Optional[Sequence[str]] = None,
        actions: Optional[Sequence[Any]] = None,
        from_phone: bool = False,
    ) -> bool:
        """May this approval card be mirrored to the phone right now?"""
        if not self.enabled():
            return False
        if tool_name in NEVER_FROM_PHONE or origin_notes or actions:
            return False
        if from_phone:
            return True
        try:
            if self._paused():
                return False
        except Exception:
            return False
        return self.mac_idle()

    def mirror_approval(
        self,
        request_id: str,
        tool_name: str,
        tool_args: Mapping[str, Any],
        *,
        reasoning: str = "",
        summary: Optional[str] = None,
        origin_notes: Optional[Sequence[str]] = None,
        actions: Optional[Sequence[Any]] = None,
        from_phone: bool = False,
    ) -> Optional[str]:
        """Send the card to the phone; return its code, or ``None`` when it
        was not mirrored (ineligible, rate limit, send failure)."""
        if not self.eligible(tool_name, origin_notes=origin_notes, actions=actions,
                             from_phone=from_phone):
            return None
        with self._lock:
            self._expire_locked()
            code = self._new_code_locked()
            # Reserved before sending so a fast reply finds it.
            self._pending[code] = _Pending(request_id, code, tool_name,
                                           self._clock() + self.timeout_s)
            self._by_request[request_id] = code
        body = card_text(tool_name, tool_args, reasoning=reasoning, summary=summary,
                         code=code, minutes=int(self.timeout_s // 60))
        if not self._send(body):  # blocking (osascript): call off the event loop
            self.forget(request_id)
            return None
        return code

    def forget(self, request_id: str) -> None:
        """The request ended (answered anywhere, timed out): its code dies."""
        with self._lock:
            code = self._by_request.pop(request_id, None)
            if code is not None:
                self._pending.pop(code, None)

    def pending_codes(self) -> dict[str, str]:
        with self._lock:
            self._expire_locked()
            return {c: p.request_id for c, p in self._pending.items()}

    def _new_code_locked(self) -> str:
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in self._pending:
                return code

    def _expire_locked(self) -> None:
        now = self._clock()
        for code, p in list(self._pending.items()):
            if p.expires_at <= now:
                self._pending.pop(code, None)
                self._by_request.pop(p.request_id, None)

    # ---- phone turns ---------------------------------------------------------

    def send_reply(self, event_id: str, reply: str) -> bool:
        """Answer a turn that came from the phone. Only turns this channel
        started are answered, and each only once."""
        with self._lock:
            if event_id not in self._open_turns:
                return False
            self._open_turns.discard(event_id)
        if not self.enabled():
            return False
        text = (reply or "").strip() or "I couldn't find an answer to that."
        return self._send(text)

    # ---- sending -------------------------------------------------------------

    def _send(self, text: str) -> bool:
        """The only way out: to the configured handle, rate-limited, logged."""
        handle = own_handle()
        if not handle or not remote_enabled():
            return False
        body = text if text.startswith(PREFIX) else PREFIX + text
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS - 1] + "…"
        with self._lock:
            now = self._clock()
            while self._sent_at and now - self._sent_at[0] >= 3600.0:
                self._sent_at.popleft()
            if len(self._sent_at) >= self.max_per_hour:
                logger.info("remote channel: hourly limit (%d) reached; not sent",
                            self.max_per_hour)
                return False
            self._sent_at.append(now)
            self._echoes.append(_Echo(_norm_text(body[len(PREFIX):]), now))
        try:
            ok = bool(self._sender(handle, body))
        except Exception:
            logger.exception("remote channel: sender raised")
            ok = False
        if ok:
            self._record_egress(body)
        return ok

    def _record_egress(self, body: str) -> None:
        try:
            if self._egress is not None:
                record = self._egress.record
            else:
                from ai import egress_log

                record = egress_log.record
            record(KIND_IMESSAGE, EGRESS_HOST, None, None,
                   len(body.encode("utf-8")), 0, "sent", body,
                   tool=EGRESS_TOOL, note=EGRESS_NOTE)
        except Exception:
            logger.exception("remote channel: egress record failed")

    # ---- reading ------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="RemoteChannel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=self._poll_interval + 1.0)
        self._close()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except sqlite3.Error:
                logger.exception("remote channel poll failed; will retry")
                self._close()
            if self._stop_event.wait(self._poll_interval):
                return

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover
                pass

    def _connect(self) -> Optional[sqlite3.Connection]:
        if self._conn is not None:
            return self._conn
        try:
            if not self._db_path.exists():
                return None
            conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True, timeout=2.0,
                                   check_same_thread=False)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except (sqlite3.Error, OSError) as exc:
            # imessage_watcher already reports Full Disk Access (#19).
            logger.debug("remote channel: chat.db not readable: %s", exc)
            return None
        self._conn = conn
        self._columns = {r[1] for r in conn.execute("PRAGMA table_info(message)")}
        return conn

    def poll_once(self) -> int:
        """Read new rows; act on own-thread commands. Returns commands handled."""
        conn = self._connect()
        if conn is None:
            return 0
        max_rowid = int(conn.execute("SELECT IFNULL(MAX(ROWID), 0) FROM message").fetchone()[0])
        if self._last_rowid is None or not self.enabled():
            # Baseline (and stay at the head while off): history and anything
            # sent while the channel was off is never replayed.
            self._last_rowid = max_rowid
            return 0
        rows = self._rows_after(conn, self._last_rowid)
        self._last_rowid = max([max_rowid, self._last_rowid] + [r.rowid for r in rows])
        own_chats = self._own_chat_ids(conn)
        handled = 0
        for row in rows:
            if self._is_command(row, own_chats):
                self._handle_command(row)
                handled += 1
        return handled

    def _rows_after(self, conn: sqlite3.Connection, after: int) -> list[_Row]:
        cols = self._columns or set()
        attributed = "m.attributedBody" if "attributedBody" in cols else "NULL"
        assoc = "m.associated_message_type" if "associated_message_type" in cols else "0"
        cur = conn.execute(
            f"""
            SELECT m.ROWID, m.text, {attributed}, m.is_from_me, IFNULL(m.service, ''),
                   cmj.chat_id, IFNULL({assoc}, 0)
            FROM message m
            LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            WHERE m.ROWID > ?
            ORDER BY m.ROWID ASC
            """,
            (after,),
        )
        out = []
        for rowid, text, attr, from_me, service, chat_id, associated in cur.fetchall():
            body = text if text else text_from_attributed_body(attr)
            out.append(_Row(int(rowid), body or "", bool(from_me), str(service),
                            None if chat_id is None else int(chat_id), int(associated or 0)))
        return out

    def _own_chat_ids(self, conn: sqlite3.Connection) -> set[int]:
        """Chats whose ONLY participant is the own handle."""
        handle = own_handle()
        members: dict[int, list[str]] = {}
        for chat_id, hid in conn.execute(
            "SELECT chj.chat_id, h.id FROM chat_handle_join chj"
            " JOIN handle h ON h.ROWID = chj.handle_id"
        ).fetchall():
            members.setdefault(int(chat_id), []).append(str(hid or ""))
        return {cid for cid, ids in members.items()
                if len(ids) == 1 and same_handle(ids[0], handle)}

    def _is_command(self, row: _Row, own_chats: set[int]) -> bool:
        if not row.is_from_me or row.service != IMESSAGE_SERVICE:
            return False
        if row.chat_id is None or row.chat_id not in own_chats:
            return False
        if row.associated:  # tapbacks and other reactions
            return False
        text = row.text.strip()
        if not text:
            return False
        return not self._is_own_echo(text)

    def _is_own_echo(self, text: str) -> bool:
        prefixed = text.startswith(PREFIX.strip())
        norm = _norm_text(text[len(PREFIX.strip()):] if prefixed else text)
        with self._lock:
            now = self._clock()
            self._echoes = [e for e in self._echoes if now - e.at < ECHO_TTL_S]
            # Kept until ECHO_TTL_S: Messages may write more than one copy.
            if any(echo.text == norm for echo in self._echoes):
                return True
        return prefixed

    def _handle_command(self, row: _Row) -> None:
        text = row.text.strip()
        match = _REPLY_RE.match(text)
        if match:
            self._handle_reply(match.group(1).lower() == "yes", match.group(2).upper())
            return
        self._start_turn(row, text)

    def _handle_reply(self, approved: bool, code: str) -> None:
        if not code:
            self._send('Reply with the card\'s code, like "yes K7Q2".')
            return
        with self._lock:
            self._expire_locked()
            pending = self._pending.pop(code, None)
            if pending is not None:
                self._by_request.pop(pending.request_id, None)
        if pending is None:
            self._send(f"No open card has the code {code}.")
            return
        submit = self._submit
        ok = bool(submit(pending.request_id, approved)) if submit is not None else False
        if ok:
            self._send(f"{'Approved' if approved else 'Declined'} ({code}).")
        else:
            self._send(f"That card ({code}) was already answered.")

    def _start_turn(self, row: _Row, text: str) -> None:
        if self._bus is None:
            return
        event_id = f"remote_{row.rowid}"
        with self._lock:
            self._open_turns.add(event_id)
        try:
            self._bus.push_threadsafe(ContextEvent(
                event_type="user_message",
                metadata={"event_id": event_id, "text": text, "source": REMOTE_SOURCE},
            ))
        except Exception:
            logger.exception("remote channel: failed to push user_message")
            with self._lock:
                self._open_turns.discard(event_id)


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _short(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = _norm_text(text)
    return text if len(text) <= MAX_ARG_CHARS else text[:MAX_ARG_CHARS - 1] + "…"


def card_text(
    tool_name: str,
    tool_args: Mapping[str, Any],
    *,
    reasoning: str = "",
    summary: Optional[str] = None,
    code: str,
    minutes: int = int(REMOTE_TIMEOUT_S // 60),
) -> str:
    """The card as the Mac shows it: summary (or reasoning), tool, arguments."""
    lines = [f"{PREFIX}Approve? {summary or reasoning or tool_name}".rstrip()]
    lines.append(f"Action: {tool_name}")
    kwargs = tool_args.get("kwargs") if isinstance(tool_args, Mapping) else None
    args = tool_args.get("args") if isinstance(tool_args, Mapping) else None
    for i, value in enumerate(args or []):
        lines.append(f"  {i + 1}: {_short(value)}")
    for key, value in (kwargs or {}).items():
        lines.append(f"  {key}: {_short(value)}")
    lines.append(f'Reply "yes {code}" or "no {code}" within {minutes} min.')
    return "\n".join(lines)


__all__ = [
    "CODE_LENGTH", "DEFAULT_IDLE_MINUTES", "ENABLED_KEY", "HANDLE_KEY", "IDLE_MINUTES_KEY",
    "KIND_IMESSAGE", "MAX_IDLE_MINUTES", "MAX_PER_HOUR", "MIN_IDLE_MINUTES",
    "NEVER_FROM_PHONE", "PREFIX", "REMOTE_SOURCE", "REMOTE_TIMEOUT_S", "RemoteChannel",
    "applescript_send", "card_text", "excluded_handle", "idle_minutes", "normalize_handle",
    "own_handle", "parse_idle_minutes", "remote_enabled", "same_handle",
    "text_from_attributed_body",
]
