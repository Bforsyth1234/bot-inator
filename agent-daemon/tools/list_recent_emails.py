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

Privacy (#31/#52): subjects are personal text. Under a cloud main engine
the orchestrator passes the result through :func:`redact_result` (each
item exactly like a ``read_email`` result) unless the user allowed it.

Needs Full Disk Access for the daemon, like the watcher, and says so.
"""

from __future__ import annotations

import re
import sqlite3
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


def _build_sql(columns: dict[str, list[str]]) -> str:
    table, col = mw.OPTIONAL_SENDER_NAME
    has_name = col in {c.lower() for c in columns.get(table, [])}
    message_cols = {c.lower() for c in columns.get("messages", [])}
    return _SQL.format(
        sender_name=f"a.{col}" if has_name else "NULL",
        global_id="m.global_message_id" if "global_message_id" in message_cols else "NULL",
        deleted="m.deleted" if "deleted" in message_cols else "NULL",
    )


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


def list_recent_emails_from(root: Path, count: Any = DEFAULT_COUNT, mailbox: str = "") -> dict[str, Any]:
    try:
        n = int(count)
    except (TypeError, ValueError):
        return {"status": "error", "error": f"invalid count {count!r}"}
    n = max(1, min(MAX_COUNT, n))
    wanted = (mailbox or "").strip().lower()
    conn, error = _open(Path(root).expanduser())
    if conn is None:
        return {"status": "error", "error": error}
    emails: list[dict[str, Any]] = []
    seen_global: set[Any] = set()
    try:
        columns = mw.table_columns(conn)
        problems = mw.schema_problems(columns)
        if problems:
            return {"status": "error",
                    "error": "Mail's index has an unexpected layout: " + "; ".join(problems)}
        sql = _build_sql(columns)
        offset = 0
        while len(emails) < n and offset < _MAX_SCANNED:
            rows = conn.execute(sql, (_BATCH, offset)).fetchall()
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
                emails.append({
                    "message_id": int(rowid),
                    "sender": mw._sender(address, name),
                    "subject": subject or "",
                    "date": mw._iso_date(date_received),
                    "mailbox": mw.mailbox_name(url),
                })
                if len(emails) >= n:
                    break
    except sqlite3.Error as exc:
        return {"status": "error", "error": f"could not read Mail's index: {exc}"}
    finally:
        conn.close()
    return {"status": "ok", "count": len(emails), "emails": emails}


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
def list_recent_emails(count: int = DEFAULT_COUNT, mailbox: str = "") -> dict[str, Any]:
    """List the newest emails received in the Mac's Mail app: sender, subject, date, mailbox and message_id.

    Use this for "what are my latest emails" and similar. Read-only. Do not
    write a new tool for it: a generated tool cannot read Mail. For a
    message's body, call read_email with its message_id.

    Args:
        count: How many emails, newest first (1 to 50, default 10).
        mailbox: Optional mailbox name to limit to, e.g. "INBOX" (case does
            not matter). Empty means every mailbox except Sent, Drafts,
            Junk and Trash.

    Returns:
        A dict with ``status`` ("ok" or "error"); on success ``count`` and
        ``emails`` (a list of ``{"message_id", "sender", "subject", "date",
        "mailbox"}``, newest first); on failure ``error``.
    """
    return list_recent_emails_from(mail_root(), count, mailbox)
