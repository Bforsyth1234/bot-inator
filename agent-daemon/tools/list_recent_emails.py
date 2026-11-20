"""List the newest emails in the Mac's Mail app (todo #103).

A generated tool cannot do this: Mail refuses message data to any process
under ``sandbox-exec`` (``count messages of inbox`` fails with -10004 even
with a profile of only ``(allow default)`` and Automation → Mail granted).
So the daemon reads Mail's ``Envelope Index`` itself, read-only, the way
``events.mail_watcher`` does (same schema block and helpers; no AppleScript).

Read-only (in ``READ_ONLY_TOOL_NAMES``): sender, subject, date, mailbox and
``message_id`` of the newest ``count`` received messages (cap
:data:`MAX_COUNT`), skipping Sent / Drafts / Outbox like the watcher and
also Junk / Trash, and the same message seen in several Gmail mailboxes.
Bodies stay with ``read_email(message_id)``.

Dates are LOCAL time with the offset (#144). ``since`` / ``until`` (an ISO
date-time, a bare ``YYYY-MM-DD`` or a bare ``HH:MM`` for today; local when
no offset is given) filter in SQL on ``date_received``; with a range the
result's ``count`` is every matching mail, not only the ones listed.

Privacy (#31/#52): subjects are personal text. Under a cloud main engine
the orchestrator passes the result through :func:`redact_result` (each
item exactly like a ``read_email`` result) unless the user allowed it.

Needs Full Disk Access for the daemon, like the watcher, and says so.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn

from events import mail_watcher as mw

from .read_email import mail_root

READ_ONLY: frozenset[str] = frozenset({"list_recent_emails"})

DEFAULT_COUNT = 10
MAX_COUNT = 50
_BATCH = 200
_MAX_SCANNED = 5000
# With a time range every row in it is scanned (to count them all); a range
# bigger than this reports ``count_complete: False``.
_MAX_SCANNED_RANGE = 50_000

FDA_ERROR = (
    "Can't read Mail's message index: the daemon needs Full Disk Access "
    "(Settings → Permissions → Full Disk Access)."
)

# Junk and trash, on top of the watcher's own-mail mailboxes.
_SKIPPED_MAILBOX_RE = re.compile(
    r"^(junk|junk e-?mail|junk mail|spam|bulk mail|trash|deleted messages|"
    r"deleted items|deleted|bin|papierkorb|corbeille)$",
    re.IGNORECASE,
)

_SQL = """
    SELECT m.ROWID, s.subject, a.address, {sender_name}, m.date_received, mb.url,
           {global_id}, {deleted}
    FROM messages m
    LEFT JOIN subjects s ON m.subject = s.ROWID
    LEFT JOIN addresses a ON m.sender = a.ROWID
    LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
    {where}
    ORDER BY m.date_received DESC, m.ROWID DESC
    LIMIT ? OFFSET ?
"""


def is_skipped_mailbox(url: Optional[str]) -> bool:
    """Sent / Drafts / Outbox (the watcher's rule), Junk and Trash."""
    if mw.is_own_mailbox(url):
        return True
    last = mw.mailbox_name(url).rsplit("/", 1)[-1].strip()
    return bool(_SKIPPED_MAILBOX_RE.match(last))


def _mailbox_matches(url: Optional[str], wanted: str) -> bool:
    name = mw.mailbox_name(url).lower()
    return wanted in (name, name.rsplit("/", 1)[-1].strip())


def _build_sql(columns: dict[str, list[str]], where: str = "") -> str:
    """The listing query; ``where`` (a ``WHERE …`` clause with ``?``
    placeholders that come before LIMIT / OFFSET) is "" for the knowledge
    index's full scan, which calls this as ``_build_sql(columns)``."""
    table, col = mw.OPTIONAL_SENDER_NAME
    has_name = col in {c.lower() for c in columns.get(table, [])}
    message_cols = {c.lower() for c in columns.get("messages", [])}
    return _SQL.format(
        sender_name=f"a.{col}" if has_name else "NULL",
        global_id="m.global_message_id" if "global_message_id" in message_cols else "NULL",
        deleted="m.deleted" if "deleted" in message_cols else "NULL",
        where=where,
    )


# ---- time range (#144) -----------------------------------------------------

_BARE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BARE_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def _localize(naive: datetime, tz: Optional[tzinfo]) -> datetime:
    """A wall-clock time in the local zone (``tz``, else the system's)."""
    if tz is not None:
        return naive.replace(tzinfo=tz)
    return naive.astimezone()  # naive -> treated as system local time


def parse_local_time(value: Any, *, end_of_day: bool = False, tz: Optional[tzinfo] = None,
                     now: Optional[datetime] = None) -> datetime:
    """``since`` / ``until`` text as an aware datetime. No offset = local
    time. ``YYYY-MM-DD`` is that day's midnight (or the next midnight with
    ``end_of_day``, so ``until="2026-11-20"`` includes the whole day);
    ``HH:MM`` is that time today. Raises ValueError."""
    text = str(value).strip()
    if not text:
        raise ValueError("empty")
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    if _BARE_DATE_RE.match(text):
        day = date.fromisoformat(text) + timedelta(days=1 if end_of_day else 0)
        return _localize(datetime.combine(day, time()), tz)
    if _BARE_TIME_RE.match(text):
        today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
        hh, mm, *ss = (int(p) for p in text.split(":"))
        return _localize(datetime.combine(today, time(hh, mm, *(ss or [0]))), tz)
    stamp = datetime.fromisoformat(text)
    return stamp if stamp.tzinfo is not None else _localize(stamp, tz)


def _bad_time(name: str, value: Any, tz: Optional[tzinfo], now: Optional[datetime]) -> str:
    today = (now or datetime.now(timezone.utc)).astimezone(tz).date().isoformat()
    return (f"invalid {name} {value!r}: use a local date-time like {today}T12:00, "
            f"a time like 12:00 (today), or a date like {today}")


def _open(root: Path) -> tuple[Optional[sqlite3.Connection], str]:
    path = mw.find_index(root)
    if path is None:
        if mw.root_unreadable(root):
            return None, FDA_ERROR
        return None, (f"no Mail message index under {root} (Mail not set up, "
                      "or the daemon lacks Full Disk Access)")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return None, f"{FDA_ERROR} ({exc})"
    return conn, ""


def list_recent_emails_from(root: Path, count: Any = DEFAULT_COUNT, mailbox: str = "",
                            since: Any = "", until: Any = "", *, tz: Optional[tzinfo] = None,
                            now: Optional[datetime] = None) -> dict[str, Any]:
    try:
        n = int(count)
    except (TypeError, ValueError):
        return {"status": "error", "error": f"invalid count {count!r}"}
    n = max(1, min(MAX_COUNT, n))
    wanted = (mailbox or "").strip().lower()
    bounds: dict[str, Optional[datetime]] = {"since": None, "until": None}
    for name, value in (("since", since), ("until", until)):
        if value in (None, "") or not str(value).strip():
            continue
        try:
            bounds[name] = parse_local_time(value, end_of_day=name == "until", tz=tz, now=now)
        except (ValueError, TypeError, OverflowError):
            return {"status": "error", "error": _bad_time(name, value, tz, now)}
    start, stop = bounds["since"], bounds["until"]
    ranged = start is not None or stop is not None
    if start is not None and stop is not None and stop <= start:
        return {"status": "error",
                "error": f"until ({stop.astimezone(tz).isoformat()}) must be after "
                         f"since ({start.astimezone(tz).isoformat()})"}
    clauses, params = [], []
    if start is not None:
        clauses.append("m.date_received >= ?")
        params.append(start.timestamp())
    if stop is not None:
        clauses.append("m.date_received < ?")
        params.append(stop.timestamp())
    conn, error = _open(Path(root).expanduser())
    if conn is None:
        return {"status": "error", "error": error}
    emails: list[dict[str, Any]] = []
    seen_global: set[Any] = set()
    matched = 0
    scan_cap = _MAX_SCANNED_RANGE if ranged else _MAX_SCANNED
    offset = 0
    try:
        columns = mw.table_columns(conn)
        problems = mw.schema_problems(columns)
        if problems:
            return {"status": "error",
                    "error": "Mail's index has an unexpected layout: " + "; ".join(problems)}
        sql = _build_sql(columns, ("WHERE " + " AND ".join(clauses)) if clauses else "")
        # Without a range: stop once ``n`` are listed. With one: read the
        # whole range so ``count`` is every match.
        while (ranged or len(emails) < n) and offset < scan_cap:
            rows = conn.execute(sql, (*params, _BATCH, offset)).fetchall()
            if not rows:
                break
            offset += len(rows)
            for rowid, subject, address, name, date_received, url, global_id, deleted in rows:
                if deleted not in (None, 0, "0") or is_skipped_mailbox(url):
                    continue
                if wanted and not _mailbox_matches(url, wanted):
                    continue
                if global_id not in (None, 0):
                    if global_id in seen_global:
                        continue  # the same mail in another Gmail mailbox
                    seen_global.add(global_id)
                matched += 1
                if len(emails) < n:
                    emails.append({
                        "message_id": int(rowid),
                        "sender": mw._sender(address, name),
                        "subject": subject or "",
                        "date": mw._iso_date(date_received, tz),
                        "mailbox": mw.mailbox_name(url),
                    })
                elif not ranged:
                    break
    except sqlite3.Error as exc:
        return {"status": "error", "error": f"could not read Mail's index: {exc}"}
    finally:
        conn.close()
    if not ranged:
        return {"status": "ok", "count": len(emails), "emails": emails}
    result: dict[str, Any] = {
        "status": "ok", "count": matched, "listed": len(emails),
        "truncated": matched > len(emails),
        "since": start.astimezone(tz).isoformat() if start is not None else "",
        "until": stop.astimezone(tz).isoformat() if stop is not None else "",
        "emails": emails,
    }
    if offset >= scan_cap:
        result["count_complete"] = False
        result["note"] = (f"The range holds more than {scan_cap} messages; count is only "
                          "those read. Use a shorter range.")
    elif result["truncated"]:
        result["note"] = (f"{matched} emails match; only the newest {len(emails)} are "
                          f"listed. Answer 'how many' with count ({matched}).")
    return result


def redact_result(result: Any, role: Optional[str]) -> Any:
    """``result`` as the ``role`` engine may see it: each email redacted
    exactly like a ``read_email`` result (``ai.privacy``)."""
    from ai import privacy

    if not isinstance(result, dict) or not isinstance(result.get("emails"), list):
        return result
    if not privacy.should_redact(role):
        return result
    out = dict(result)
    out["emails"] = [privacy.redact_email_result(e, role) for e in result["emails"]]
    for item in out["emails"]:
        if isinstance(item, dict):
            item.pop("note", None)
    out["note"] = (
        "Subjects were kept on this Mac because this engine runs in the cloud "
        f"(the user can allow sending them in Settings, or with {privacy.OPT_IN_ENV}=1)."
    )
    return out


@tool
def list_recent_emails(count: int = DEFAULT_COUNT, mailbox: str = "", since: str = "",
                       until: str = "") -> dict[str, Any]:
    """List or count emails received in the Mac's Mail app (sender, subject, local date, mailbox, message_id); use since for "how many since …".

    Use this for "what are my latest emails" and, with ``since``, for
    "how many emails since 12 pm" / "what came in this morning". Do not
    count the list yourself: with ``since`` or ``until``, ``count`` in the
    result is every matching email even when only some are listed
    (``truncated``). Dates are the user's LOCAL time with the offset, e.g.
    2026-11-20T12:16:17-05:00. Read-only. Do not write a new tool for it: a
    generated tool cannot read Mail. For a message's body, call read_email
    with its message_id.

    Args:
        count: How many emails to list, newest first (1 to 50, default 10).
        mailbox: Optional mailbox name to limit to, e.g. "INBOX" (case does
            not matter). Empty means every mailbox except Sent, Drafts,
            Junk and Trash.
        since: Optional start, LOCAL time unless it has an offset: a
            date-time like "2026-11-20T12:00", a time like "12:00" (today)
            or a date like "2026-11-20" (from midnight). Use it for "how
            many since …" questions.
        until: Optional end (exclusive), same forms; a bare date includes
            that whole day.

    Returns:
        A dict with ``status`` ("ok" or "error"); on success ``count`` and
        ``emails`` (a list of ``{"message_id", "sender", "subject", "date",
        "mailbox"}``, newest first). With ``since`` / ``until`` also
        ``listed``, ``truncated``, the resolved ``since`` / ``until`` and,
        when truncated, a ``note``. On failure ``error``.
    """
    return list_recent_emails_from(mail_root(), count, mailbox, since, until)
