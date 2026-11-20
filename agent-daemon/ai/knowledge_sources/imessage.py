"""Knowledge-index source: iMessages (#137), a self-contained reader.

Reads ``~/Library/Messages/chat.db`` READ-ONLY (``mode=ro`` URI, no copy, as
``events/imessage_watcher.py`` does) and hands the #134 indexer one
:class:`SourceItem` per conversation per local day. The integrator registers
it (``SOURCES``, routes, Settings, taint); this module only reads.

DECISIONS (todo #137), as built:

1. Every conversation once the source is on, minus ``options["exclude"]``
   (chat guids, chat identifiers or handles). An excluded conversation
   yields no items; a successful read is always ``complete=True``, so the
   next pass prunes its rows (the integrator may also delete them at once
   with :func:`conversation_of`).
3. ``item_id`` = ``<chat guid>#<YYYY-MM-DD>``; messages in order, each line
   ``HH:MM <sender>: <text>`` (``Me`` for the user's own; others by handle,
   chat.db holds no contact names and #127 forbids listing Contacts); no link.
4. Text only: attachment-only rows (U+FFFC), tapbacks/reactions
   (``associated_message_type``), group events (``item_type``), unsent
   (``date_retracted``) and deleted messages (no longer in
   ``chat_message_join``) are skipped.
5. Newest day first, at most :data:`MAX_ITEMS` items. The cap drops the
   OLDEST days, and because the listing is still complete those fall out of
   the index (the index keeps the newest days; mail instead never prunes
   when capped, but then exclusion could never prune either).
6. The iPhone remote's own thread (#85) is ALWAYS left out, whatever the
   options say: a chat with at most one participant that is the configured
   own handle (``AGENT_REMOTE_HANDLE``, read whether or not the remote is on),
   or one where the user's own rows carry the app's ``bot-inator:`` prefix.

``version`` is a hash of the day's rendered text, so it changes only when a
message of that day is added, edited or removed.

No network, no writes. Missing Full Disk Access gives
``Listing([], complete=False, error=...)`` with the app's own FDA wording.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Mapping, Optional

from ai.knowledge_index import Listing, SourceItem

logger = logging.getLogger(__name__)

NAME = "imessages"
TITLE = "Messages"
DESCRIPTION = (
    "Your iMessage and SMS conversations, one item per conversation per day; "
    "these are mostly other people's words, and the index stays on this Mac."
)
TAINT_LABEL = "indexed message"
DEFAULT_OPTIONS: dict[str, Any] = {"exclude": []}

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
MAX_ITEMS = 5_000
MAX_EXCLUDE = 2_000
MAX_EXCLUDE_CHARS = 300
MAX_LINE_CHARS = 4_000
APPLE_EPOCH = 978_307_200          # 2001-01-01T00:00:00Z in Unix seconds
ATTACHMENT_CHAR = "￼"
OWN_PREFIX = "bot-inator:"         # events.remote_channel.PREFIX, stripped
GROUP_STYLE = 43                   # chat.style: 43 group, 45 one-to-one
ID_SEP = "#"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


def validate_options(options: Any) -> dict[str, Any]:
    """A cleaned copy of ``options``; ValueError (user-readable) on junk."""
    if options is None:
        return {"exclude": []}
    if not isinstance(options, Mapping):
        raise ValueError("Messages options must be an object.")
    unknown = sorted(str(k) for k in options if k != "exclude")
    if unknown:
        raise ValueError(f"Unknown Messages option: {', '.join(unknown)}.")
    raw = options.get("exclude", [])
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("The Messages exclude list must be a list of conversations.")
    if len(raw) > MAX_EXCLUDE:
        raise ValueError(f"The Messages exclude list can hold at most {MAX_EXCLUDE} conversations.")
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError("Each excluded conversation must be text (a conversation id or handle).")
        entry = entry.strip()
        if not entry:
            raise ValueError("An excluded conversation can't be empty.")
        if len(entry) > MAX_EXCLUDE_CHARS or _CONTROL_RE.search(entry):
            raise ValueError("An excluded conversation id is too long or has control characters.")
        if entry not in out:
            out.append(entry)
    return {"exclude": out}


def conversation_of(item_id: str) -> str:
    """The chat guid an ``item_id`` belongs to (to delete an excluded
    conversation's rows at once)."""
    return str(item_id).rpartition(ID_SEP)[0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fda_error() -> str:
    try:
        from schemas.ws_messages import full_disk_access_notice

        return full_disk_access_notice("knowledge_imessages", "iMessages").body
    except Exception:  # pragma: no cover - schemas import failure
        return ("iMessages can't be read because the app running the daemon doesn't have "
                "Full Disk Access. Turn it on in System Settings → Privacy & Security → "
                "Full Disk Access, then restart the daemon.")


def _default_own_handle() -> str:
    try:
        from events.remote_channel import own_handle

        return own_handle()
    except Exception:
        logger.debug("own handle unreadable", exc_info=True)
        return ""


def _same_handle(a: Any, b: Any) -> bool:
    from events.remote_channel import same_handle

    return same_handle(a, b)


def _decode(text: Any, attributed: Any) -> str:
    if text:
        return str(text)
    from events.remote_channel import text_from_attributed_body

    return text_from_attributed_body(attributed)


def _unix(apple_date: Any) -> Optional[float]:
    try:
        value = float(apple_date or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 1e11:  # nanoseconds since macOS 10.13; seconds before
        value /= 1e9
    return value + APPLE_EPOCH


def _open(db_path: Path) -> tuple[Optional[sqlite3.Connection], str]:
    try:
        os.stat(db_path)
    except FileNotFoundError:
        return None, (f"no Messages database at {db_path} (Messages not set up, "
                      "or the daemon lacks Full Disk Access)")
    except OSError:
        return None, _fda_error()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return None, f"{_fda_error()} ({exc})"
    return conn, ""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


class _Chat:
    __slots__ = ("rowid", "guid", "identifier", "display_name", "style", "handles")

    def __init__(self, rowid: int, guid: str, identifier: str, display_name: str, style: int):
        self.rowid = rowid
        self.guid = guid
        self.identifier = identifier
        self.display_name = display_name
        self.style = style
        self.handles: list[str] = []

    @property
    def is_group(self) -> bool:
        return self.style == GROUP_STYLE or len(self.handles) > 1

    def title(self) -> str:
        if self.display_name:
            return self.display_name
        if self.is_group:
            shown = self.handles[:4]
            more = len(self.handles) - len(shown)
            return ", ".join(shown) + (f" and {more} more" if more > 0 else "") or "Group chat"
        return (self.handles[0] if self.handles else self.identifier) or "Conversation"

    def matches(self, entry: str) -> bool:
        low = entry.lower()
        if low in (self.guid.lower(), self.identifier.lower()):
            return True
        if not self.is_group:
            for h in [self.identifier, *self.handles]:
                if h and _same_handle(h, entry):
                    return True
        return False


def _chats(conn: sqlite3.Connection) -> dict[int, _Chat]:
    cols = _columns(conn, "chat")
    name = "IFNULL(display_name, '')" if "display_name" in cols else "''"
    style = "IFNULL(style, 0)" if "style" in cols else "0"
    chats: dict[int, _Chat] = {}
    for rowid, guid, ident, disp, sty in conn.execute(
        f"SELECT ROWID, IFNULL(guid, ''), IFNULL(chat_identifier, ''), {name}, {style} FROM chat"
    ):
        chats[int(rowid)] = _Chat(int(rowid), str(guid) or f"chat-{rowid}", str(ident),
                                  str(disp), int(sty or 0))
    for chat_id, hid in conn.execute(
        "SELECT chj.chat_id, h.id FROM chat_handle_join chj"
        " JOIN handle h ON h.ROWID = chj.handle_id ORDER BY chj.chat_id, h.ROWID"
    ):
        chat = chats.get(int(chat_id))
        if chat is not None and hid and str(hid) not in chat.handles:
            chat.handles.append(str(hid))
    return chats


def _own_thread_ids(conn: sqlite3.Connection, chats: dict[int, _Chat], own: str) -> set[int]:
    """#85: chats that are the app talking to the user. Always excluded."""
    out: set[int] = set()
    for cid, chat in chats.items():
        if len(chat.handles) > 1 or chat.style == GROUP_STYLE:
            continue
        if own and any(_same_handle(h, own) for h in [chat.identifier, *chat.handles] if h):
            out.add(cid)
    # The app's own sends start with its prefix: that thread is never indexed,
    # even before a handle is configured.
    for (cid,) in conn.execute(
        "SELECT DISTINCT cmj.chat_id FROM message m"
        " JOIN chat_message_join cmj ON cmj.message_id = m.ROWID"
        " WHERE m.is_from_me = 1 AND m.text LIKE ?",
        (OWN_PREFIX + "%",),
    ):
        chat = chats.get(int(cid))
        if chat is not None and len(chat.handles) <= 1 and chat.style != GROUP_STYLE:
            out.add(int(cid))
    return out


def _excluded_ids(chats: dict[int, _Chat], exclude: list[str]) -> set[int]:
    return {cid for cid, chat in chats.items() if any(chat.matches(e) for e in exclude)}


def _local_tz(tz: Optional[tzinfo]) -> tzinfo:
    return tz if tz is not None else (datetime.now().astimezone().tzinfo or timezone.utc)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def listing(options: Optional[dict] = None, *, db_path: Optional[Path] = None,
            own_handle: Optional[str] = None, tz: Optional[tzinfo] = None,
            max_items: int = MAX_ITEMS, **_ignored: Any) -> Listing:
    """Newest-first items, one per conversation per local day.

    Overrides (tests): ``db_path`` (default ``~/Library/Messages/chat.db``),
    ``own_handle`` (default the #85 setting), ``tz`` (default local),
    ``max_items``."""
    try:
        opts = validate_options(options if options is not None else DEFAULT_OPTIONS)
    except ValueError as exc:
        return Listing([], complete=False, error=str(exc))
    path = Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
    conn, error = _open(path)
    if conn is None:
        return Listing([], complete=False, error=error)
    own = _default_own_handle() if own_handle is None else own_handle
    zone = _local_tz(tz)
    try:
        chats = _chats(conn)
        skip = _own_thread_ids(conn, chats, own) | _excluded_ids(chats, opts["exclude"])
        days = _read_days(conn, chats, skip, zone, max_items)
    except sqlite3.Error as exc:
        return Listing([], complete=False, error=f"could not read Messages' database: {exc}")
    finally:
        conn.close()
    items = []
    for (cid, day), lines in days.items():
        chat = chats[cid]
        name = chat.title()
        last_ts = lines[-1][0]
        header = f"{'Group chat' if chat.is_group else 'Messages with'} {name} on {day}"
        text = header + "\n\n" + "\n".join(line for _, line in lines)
        version = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        items.append(SourceItem(
            item_id=f"{chat.guid}{ID_SEP}{day}", version=version,
            title=f"{name} · {day}",
            date=datetime.fromtimestamp(last_ts, tz=zone).isoformat(),
            load=(lambda t=text: {"text": t}),
        ))
    # A successful read is the whole source (minus exclusions and the oldest
    # days past the cap): complete, so excluded conversations get pruned.
    return Listing(items, complete=True)


def _read_days(conn: sqlite3.Connection, chats: dict[int, _Chat], skip: set[int],
               zone: tzinfo, max_items: int) -> dict[tuple[int, str], list[tuple[float, str]]]:
    cols = _columns(conn, "message")
    attributed = "m.attributedBody" if "attributedBody" in cols else "NULL"
    assoc = "IFNULL(m.associated_message_type, 0)" if "associated_message_type" in cols else "0"
    item_type = "IFNULL(m.item_type, 0)" if "item_type" in cols else "0"
    retracted = "IFNULL(m.date_retracted, 0)" if "date_retracted" in cols else "0"
    cur = conn.execute(
        f"""
        SELECT m.ROWID, m.text, {attributed}, m.is_from_me, m.date, h.id, cmj.chat_id
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE {assoc} = 0 AND {item_type} = 0 AND {retracted} = 0
        ORDER BY m.date DESC, m.ROWID DESC
        """
    )
    days: dict[tuple[int, str], list[tuple[float, str]]] = {}
    boundary: Optional[str] = None  # set once the cap is reached
    for _rowid, text, attr, from_me, date, sender, chat_id in cur:
        if chat_id is None or int(chat_id) in skip or int(chat_id) not in chats:
            continue
        ts = _unix(date)
        if ts is None:
            continue
        body = _decode(text, attr).replace(ATTACHMENT_CHAR, "").strip()
        if not body:
            continue
        when = datetime.fromtimestamp(ts, tz=zone)
        day = when.date().isoformat()
        if boundary is not None and day < boundary:
            break  # older than every kept day: nothing more can join one
        key = (int(chat_id), day)
        if key not in days:
            if len(days) >= max_items:
                boundary = boundary or min(d for _, d in days)
                continue
            days[key] = []
        who = "Me" if from_me else (str(sender) if sender else "Unknown")
        days[key].append((ts, f"{when:%H:%M} {who}: {body[:MAX_LINE_CHARS]}"))
    for lines in days.values():
        lines.reverse()  # read newest-first; a day reads in order
    return days


# ---------------------------------------------------------------------------
# Settings pickers
# ---------------------------------------------------------------------------


def choices(options: Optional[dict] = None, *, db_path: Optional[Path] = None,
            own_handle: Optional[str] = None, **_ignored: Any) -> dict[str, Any]:
    """What Settings needs for the exclude-list picker::

        {"conversations": [{"id", "title", "handles", "is_group",
                            "last_message", "excluded"}], "error": ""}

    Newest conversation first; ``id`` is the chat guid to put in
    ``options["exclude"]``. The #85 own thread is never listed."""
    try:
        opts = validate_options(options if options is not None else DEFAULT_OPTIONS)
    except ValueError:
        opts = {"exclude": []}
    path = Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
    conn, error = _open(path)
    if conn is None:
        return {"conversations": [], "error": error}
    own = _default_own_handle() if own_handle is None else own_handle
    try:
        chats = _chats(conn)
        own_ids = _own_thread_ids(conn, chats, own)
        excluded = _excluded_ids(chats, opts["exclude"])
        last = {int(cid): d for cid, d in conn.execute(
            "SELECT cmj.chat_id, MAX(m.date) FROM chat_message_join cmj"
            " JOIN message m ON m.ROWID = cmj.message_id GROUP BY cmj.chat_id")}
    except sqlite3.Error as exc:
        return {"conversations": [], "error": f"could not read Messages' database: {exc}"}
    finally:
        conn.close()
    rows = []
    for cid, chat in chats.items():
        if cid in own_ids:
            continue
        ts = _unix(last.get(cid))
        rows.append({
            "id": chat.guid, "title": chat.title(), "handles": list(chat.handles),
            "is_group": chat.is_group,
            "last_message": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else "",
            "excluded": cid in excluded,
        })
    rows.sort(key=lambda r: r["last_message"], reverse=True)
    return {"conversations": rows, "error": ""}


__all__ = ["NAME", "TITLE", "DESCRIPTION", "TAINT_LABEL", "DEFAULT_OPTIONS",
           "validate_options", "listing", "choices", "conversation_of"]
