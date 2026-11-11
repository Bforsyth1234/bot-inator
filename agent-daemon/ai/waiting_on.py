""""Waiting on" tracker: sent mail that asked something and got no reply (#73).

Once a day (with the morning brief, #65) :func:`waiting_on` looks at the
user's sent mail from the last :data:`SENT_WINDOW` (14 days) in Mail's
Envelope Index, read-only (``file:…?mode=ro``, as ``events.mail_watcher``):

1. Sent messages: rows in a Sent mailbox (Gmail label duplicates dropped by
   ``global_message_id``). Per thread only the user's *latest* sent message
   counts: a later "thanks!" means the user is not waiting.
2. Excluded: mailing-list mail (``list_id_hash`` / ``unsubscribe_type``),
   mail to no-reply / notification addresses only, mail to more than
   :data:`MAX_RECIPIENTS` people, and mail that asks nothing
   (``ai.needs_reply.asks_something`` on the subject, then on the first
   lines of the local ``.emlx`` body; a "Re:"/"Fwd:" subject is inherited,
   so a reply is judged on its body alone).
3. Answered: a later message in the same thread whose sender is one of the
   sent message's To/Cc recipients. Thread match is deterministic: the
   ``conversation_id`` column, or the subject without "Re:" prefixes when a
   row has no conversation id.
4. Waiting: unanswered for at least ``wait_days`` (default 3) ->
   ``"waiting on <name>: <subject>"``.

Privacy (invariant 7c, #31/#52): the body is read only here, locally, by
the deterministic check; it never enters the result. Items carry the
recipient's name/address and the subject, and the subject goes through
``ai.privacy.redact_text`` for the engines that read the brief, so under a
cloud engine it is the placeholder unless the user allowed personal text.

Follow-ups are offers, never automatic (invariant 7a): :func:`nudge_idea`
builds a built-in ``draft_email`` idea with ``reply_to_message_id``; the
tool only opens an unsent draft, behind an approval card.

Nothing here writes, sends, notifies or asks for a permission.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ai.needs_reply import (
    asks_something,
    extract_addresses,
    is_automated_sender,
    strip_subject_prefix,
)

logger = logging.getLogger(__name__)

SENT_WINDOW = timedelta(days=14)
DEFAULT_WAIT_DAYS = 3
WAIT_DAYS_KEY = "AGENT_WAITING_ON_DAYS"
MAX_ITEMS = 10
# Sent messages whose body may be read in one run (newest first).
MAX_BODY_READS = 60
# More recipients than this is a broadcast, not a question to someone.
MAX_RECIPIENTS = 8
READER_ROLES: tuple[str, ...] = ("main", "analysis")
LABEL = "Waiting on"
EMPTY_TEXT = "nothing unanswered"

IDEA_NAME = "draft_email_nudge"
IDEA_TRIGGER = "mail_waiting_on"
IDEA_SOURCE = "waiting_on"

# Last path component of a Sent mailbox (not Drafts / Outbox: unsent).
_SENT_MAILBOX_RE = re.compile(
    r"^(sent|sent messages|sent mail|sent items|gesendet|envoyés|enviados|inviati)$",
    re.IGNORECASE,
)
# Mailboxes whose messages are not a reply that reached the user.
_NOT_A_REPLY_MAILBOX_RE = re.compile(r"draft|outbox|send ?later", re.IGNORECASE)

SQL_SENT = """
    SELECT m.ROWID, s.subject, a.address, {date_sent}, m.date_received,
           {conversation}, {global_id}, {list_id}, {unsubscribe}
    FROM messages m
    LEFT JOIN subjects s ON m.subject = s.ROWID
    LEFT JOIN addresses a ON m.sender = a.ROWID
    WHERE m.mailbox IN ({boxes}) AND m.date_received >= ?
    ORDER BY m.date_received DESC
    LIMIT 2000
"""
SQL_RECIPIENTS = """
    SELECT r.message, r.type, ra.address, {name}
    FROM recipients r
    JOIN addresses ra ON r.address = ra.ROWID
    WHERE r.message IN ({marks})
    ORDER BY r.message, r.type, r.position
"""
SQL_FROM = """
    SELECT m.ROWID, s.subject, LOWER(a.address), m.date_received, mb.url, {conversation}
    FROM messages m
    LEFT JOIN subjects s ON m.subject = s.ROWID
    LEFT JOIN addresses a ON m.sender = a.ROWID
    LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
    WHERE m.date_received >= ? AND LOWER(a.address) IN ({marks})
"""


def _now() -> datetime:
    return datetime.now().astimezone()


def wait_days() -> int:
    """``AGENT_WAITING_ON_DAYS`` (1..14), else :data:`DEFAULT_WAIT_DAYS`."""
    try:
        import user_settings

        value = None
        if WAIT_DAYS_KEY not in user_settings.shell_exported:
            value = user_settings.read().get(WAIT_DAYS_KEY)
        value = value or os.environ.get(WAIT_DAYS_KEY)
        days = int(str(value).strip()) if value else DEFAULT_WAIT_DAYS
    except Exception:
        return DEFAULT_WAIT_DAYS
    return days if 1 <= days <= SENT_WINDOW.days else DEFAULT_WAIT_DAYS


def is_sent_mailbox(url: Optional[str]) -> bool:
    from events import mail_watcher as mw

    last = mw.mailbox_name(url).rsplit("/", 1)[-1].strip()
    return bool(_SENT_MAILBOX_RE.match(last))


def thread_key(conversation_id: Any, subject: str) -> tuple[str, str]:
    """``("c", id)`` when the row has a conversation id, else the subject."""
    if conversation_id not in (None, 0, "0", ""):
        return ("c", str(conversation_id))
    return ("s", strip_subject_prefix(subject or "").lower())


def _own_subject(subject: str) -> str:
    """The subject when the user wrote it; ``""`` for a reply or forward.

    "Re: Lunch?" inherits the other person's question mark, so a reply only
    asks something when its own first lines do.
    """
    subject = " ".join((subject or "").split())
    return subject if strip_subject_prefix(subject) == subject else ""


def _redact_subject(subject: str) -> str:
    from ai import privacy

    for role in READER_ROLES:
        subject = privacy.redact_text(subject, role)
    return subject


def _ts(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def default_read_bodies(message_ids: Iterable[int], root: Optional[Path] = None) -> dict[int, str]:
    """Bodies of local ``.emlx`` files, one walk of the Mail folder for all ids.

    Local and read-only (the files ``tools.read_email`` reads). Ids whose
    file is missing or unreadable are left out.
    """
    from tools.read_email import mail_root, message_body, parse_emlx

    wanted = {f"{int(i)}.emlx": int(i) for i in message_ids}
    wanted.update({f"{i}.partial.emlx": i for i in list(wanted.values())})
    if not wanted:
        return {}
    root = Path(root) if root else mail_root()
    paths: dict[int, Path] = {}
    try:
        versions = sorted(
            (p for p in root.iterdir() if p.is_dir() and p.name.startswith("V") and p.name[1:].isdigit()),
            key=lambda p: int(p.name[1:]), reverse=True,
        )
    except OSError:
        return {}
    for version in versions:
        for dirpath, dirnames, filenames in os.walk(version):
            if dirpath == str(version):
                dirnames[:] = [d for d in dirnames if d != "MailData"]
            if os.path.basename(dirpath) != "Messages":
                continue
            for name in filenames:
                mid = wanted.get(name)
                if mid is not None and (mid not in paths or not name.endswith(".partial.emlx")):
                    paths[mid] = Path(dirpath) / name
        if paths:
            break
    bodies: dict[int, str] = {}
    for mid, path in paths.items():
        try:
            bodies[mid] = message_body(parse_emlx(path.read_bytes()))
        except Exception:
            logger.info("waiting_on: could not read message %s", mid, exc_info=True)
    return bodies


def _error(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def _open_index(mail_root: Path | str | None, db_path: Path | str | None) -> tuple[Optional[sqlite3.Connection], str]:
    from events import mail_watcher as mw
    from tools.read_email import mail_root as default_mail_root

    if db_path:
        path: Optional[Path] = Path(db_path).expanduser()
    else:
        root = Path(mail_root).expanduser() if mail_root else default_mail_root()
        path = mw.find_index(root)
        if path is None:
            if mw.root_unreadable(root):
                return None, "the daemon needs Full Disk Access to read Mail (Settings > Permissions)."
            return None, "no Mail index found (is the Mail app set up?)."
    if path is None or not path.exists():
        return None, "no Mail index found (is the Mail app set up?)."
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        return None, f"could not open the Mail index ({exc})."
    return conn, ""


def _query(conn: sqlite3.Connection, since: float) -> tuple[list[dict[str, Any]], dict[int, list[tuple[int, str, str]]], list[tuple]]:
    """Sent rows, their recipients, and later mail from those recipients."""
    from events import mail_watcher as mw

    columns = mw.table_columns(conn)
    problems = mw.schema_problems(columns)
    if "recipients" not in columns or not {"message", "address"} <= {c.lower() for c in columns["recipients"]}:
        problems.append("missing table 'recipients'")
    if problems:
        raise LookupError("the Mail index layout changed (" + "; ".join(problems) + ").")
    message_cols = {c.lower() for c in columns.get("messages", [])}

    def opt(name: str) -> str:
        return f"m.{name}" if name in message_cols else "NULL"

    boxes = [rid for rid, url in conn.execute("SELECT ROWID, url FROM mailboxes") if is_sent_mailbox(url)]
    if not boxes:
        return [], {}, []
    sql = SQL_SENT.format(
        date_sent=opt("date_sent"), conversation=opt("conversation_id"),
        global_id=opt("global_message_id"), list_id=opt("list_id_hash"),
        unsubscribe=opt("unsubscribe_type"), boxes=",".join("?" for _ in boxes),
    )
    sent: list[dict[str, Any]] = []
    seen_global: set[Any] = set()
    for rowid, subject, sender, date_sent, received, conv, gid, list_id, unsub in conn.execute(sql, [*boxes, since]):
        if gid not in (None, 0):
            if gid in seen_global:
                continue
            seen_global.add(gid)
        sent.append({
            "message_id": int(rowid), "subject": subject or "", "sender": (sender or "").lower(),
            "time": _ts(date_sent) or _ts(received) or 0.0, "conversation_id": conv,
            "bulk": mw.is_bulk(list_id, unsub),
        })
    if not sent:
        return [], {}, []
    name_col = "ra.comment" if "comment" in {c.lower() for c in columns.get("addresses", [])} else "NULL"
    recipients: dict[int, list[tuple[int, str, str]]] = {}
    ids = [s["message_id"] for s in sent]
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        sql = SQL_RECIPIENTS.format(name=name_col, marks=",".join("?" for _ in chunk))
        for message, rtype, address, name in conn.execute(sql, chunk):
            if address:
                recipients.setdefault(int(message), []).append((int(rtype or 0), address.lower(), name or ""))
    addrs = sorted({a for rs in recipients.values() for _, a, _ in rs})
    later: list[tuple] = []
    for start in range(0, len(addrs), 500):
        chunk = addrs[start:start + 500]
        sql = SQL_FROM.format(conversation=opt("conversation_id"), marks=",".join("?" for _ in chunk))
        later += conn.execute(sql, [since, *chunk]).fetchall()
    return sent, recipients, later


def _display(recips: list[tuple[int, str, str]]) -> tuple[str, str]:
    """The first To recipient's name (or address), with "+N" for the rest."""
    to = [r for r in recips if r[0] == 0] or recips
    _, address, name = to[0]
    label = name.strip() or address
    if len(to) > 1:
        label += f" +{len(to) - 1}"
    return label, address


def waiting_on(
    now: Optional[datetime] = None,
    *,
    days: Optional[int] = None,
    mail_root: Path | str | None = None,
    db_path: Path | str | None = None,
    read_bodies: Optional[Callable[[list[int]], dict[int, str]]] = None,
    redact_subject: Optional[Callable[[str], str]] = None,
    limit: int = MAX_ITEMS,
) -> dict[str, Any]:
    """Sent mail that asked something and has had no reply for ``days``.

    Never raises. Returns the brief's source shape::

        {"status": "ok", "items": [{"message_id", "name", "address", "to",
          "subject", "sent", "days", "line"}], "truncated": bool,
          "skipped": {reason: count}}

    or ``{"status": "unavailable", "reason": "..."}``. ``subject`` is
    already redacted for the reading engines; ``line`` is
    ``"waiting on <name>: <subject>"``. Oldest wait first.
    """
    now = now or _now()
    wait = days if days is not None else wait_days()
    redact = redact_subject or _redact_subject
    since = (now - SENT_WINDOW).timestamp()
    cutoff = (now - timedelta(days=wait)).timestamp()

    conn, reason = _open_index(mail_root, db_path)
    if conn is None:
        return _error(reason)
    try:
        sent, recipients, later = _query(conn, since)
    except LookupError as exc:
        return _error(str(exc))
    except sqlite3.Error as exc:
        return _error(f"could not read the Mail index ({exc}).")
    finally:
        conn.close()

    user = {s["sender"] for s in sent if s["sender"]}
    # Only the user's latest sent message per thread is a candidate.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for msg in sent:
        key = thread_key(msg["conversation_id"], msg["subject"])
        if key not in latest or msg["time"] > latest[key]["time"]:
            latest[key] = msg
    replies: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for _rowid, subject, sender, received, url, conv in later:
        if sender in user or _NOT_A_REPLY_MAILBOX_RE.search(url or "") or is_sent_mailbox(url):
            continue
        for key in {thread_key(conv, subject or ""), thread_key(None, subject or "")}:
            replies.setdefault(key, []).append((sender, _ts(received) or 0.0))

    skipped: dict[str, int] = {}
    pending: list[tuple[dict[str, Any], list[tuple[int, str, str]]]] = []

    def skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    for key, msg in latest.items():
        recips = [r for r in recipients.get(msg["message_id"], []) if r[1] not in user]
        if msg["time"] < since:
            continue
        if msg["bulk"]:
            skip("bulk")
            continue
        if not recips:
            skip("no_recipient")
            continue
        if len(recips) > MAX_RECIPIENTS:
            skip("too_many_recipients")
            continue
        if all(is_automated_sender(a) for _, a, _ in recips):
            skip("automated")
            continue
        if msg["time"] > cutoff:
            skip("too_recent")
            continue
        addrs = {a for _, a, _ in recips}
        # A reply row without a conversation id still matches by subject.
        keys = {key, thread_key(None, msg["subject"])}
        if any(s in addrs and t >= msg["time"] for k in keys for s, t in replies.get(k, [])):
            skip("answered")
            continue
        pending.append((msg, recips))

    pending.sort(key=lambda p: p[0]["time"])
    need_body = [m["message_id"] for m, _ in pending if not asks_something(_own_subject(m["subject"]))]
    need_body = need_body[:MAX_BODY_READS]
    bodies: dict[int, str] = {}
    if need_body:
        try:
            bodies = (read_bodies or (lambda ids: default_read_bodies(ids, Path(mail_root) if mail_root else None)))(need_body)
        except Exception:
            logger.info("waiting_on: body read failed", exc_info=True)
    items: list[dict[str, Any]] = []
    for msg, recips in pending:
        mid = msg["message_id"]
        if not asks_something(_own_subject(msg["subject"]), bodies.get(mid, "")):
            skip("no_question")  # no body found counts as no question (7d)
            continue
        name, address = _display(recips)
        subject = redact(strip_subject_prefix(msg["subject"])) or "(no subject)"
        waited = int((now.timestamp() - msg["time"]) // 86400)
        items.append({
            "message_id": mid,
            "name": name,
            "address": address,
            "to": [a for t, a, _ in recips if t == 0] or [address],
            "subject": subject,
            "sent": datetime.fromtimestamp(msg["time"], tz=timezone.utc).isoformat(),
            "days": waited,
            "line": f"waiting on {name}: {subject}",
        })
    return {"status": "ok", "items": items[:limit], "truncated": len(items) > limit, "skipped": skipped}


def render_item(item: dict[str, Any]) -> str:
    """One brief line: ``waiting on Sam: Budget (4 days) [id 12]``."""
    return f"{item['line']} ({item['days']} days) [id {item['message_id']}]"


def format_section(source: dict[str, Any]) -> list[str]:
    """The brief's lines for a :func:`waiting_on` result (as ``_section``)."""
    if source.get("status") != "ok":
        return [f"{LABEL}: unavailable: {source.get('reason') or 'no answer'}"]
    items = source.get("items") or []
    if not items:
        return [f"{LABEL}: {EMPTY_TEXT}"]
    lines = [f"{LABEL} ({len(items)}{'+' if source.get('truncated') else ''}):"]
    return lines + [f"- {render_item(i)}" for i in items]


def nudge_idea(item: dict[str, Any]) -> dict[str, Any]:
    """A "Draft a nudge?" built-in ``draft_email`` idea for one waiting item.

    The ``pattern_detected`` metadata shape of
    ``NextStepSuggester.reply_idea``: the agent calls the built-in
    ``draft_email`` (approval-gated; it only opens an unsent draft) with
    ``reply_to_message_id`` = the user's own sent message. Nothing is sent.
    """
    from ai import privacy

    mid = int(item.get("message_id") or 0)
    to = ", ".join(item.get("to") or [item.get("address") or ""])
    follow_up = (
        "Offer a short, friendly nudge. Call the built-in draft_email tool with "
        f"reply_to_message_id set to {mid} (the user's own unanswered email), to "
        f"set to {to}, subject empty, and a two-line body in the user's voice "
        "asking whether they had a chance to look. Do not invent facts. "
        "draft_email only opens an unsent draft for the user to review; never "
        "say the email was sent."
    )

    def details(subject: str) -> str:
        return (
            f"Unanswered email the user sent {item.get('days', '?')} days ago\n"
            f"To: {item.get('name', '')} <{item.get('address', '')}>\n"
            f"Subject: {subject}\nreply_to_message_id: {mid}"
        )

    shown = details(str(item.get("subject") or ""))
    return {
        "tool_name": IDEA_NAME,
        "description": f"Draft a nudge to {item.get('name', '')}?",
        "expected_logic": f"{follow_up}\n{shown}",
        "builtin_tool": "draft_email",
        "event_details": shown,
        "event_details_redacted": details(privacy.PLACEHOLDER),
        "source": IDEA_SOURCE,
        "trigger": IDEA_TRIGGER,
        "reply_to_message_id": mid,
    }


__all__ = [
    "DEFAULT_WAIT_DAYS",
    "SENT_WINDOW",
    "format_section",
    "is_sent_mailbox",
    "nudge_idea",
    "render_item",
    "thread_key",
    "wait_days",
    "waiting_on",
]
