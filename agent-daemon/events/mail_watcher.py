"""Mail watcher: polls Mail.app's ``Envelope Index`` for newly received mail.

Mail.app keeps a SQLite index of every message it has downloaded at
``~/Library/Mail/V<n>/MailData/Envelope Index``. Like ``chat.db`` for the
iMessage watcher, the file is owned and written by another app, so it is
opened read-only through a ``file:…?mode=ro`` URI and never locked.

Events carry headers only (sender, subject, date, mailbox, message id).
Bodies are read on demand by ``tools.read_email`` (todo #41, decision 3).

The index layout is undocumented and changes between macOS versions, so
everything schema-specific lives in the ``SCHEMA`` block below. If a
required table or column is missing the watcher logs the mismatch once
and idles. To record the real layout, run (with Full Disk Access)::

    cd agent-daemon && .venv/bin/python -m events.mail_watcher --schema

.. note::

    Reading ``~/Library/Mail`` requires macOS **Full Disk Access** for the
    process hosting the daemon. Without it the watcher logs a warning and
    stays idle without crashing the daemon.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

DEFAULT_MAIL_ROOT = Path.home() / "Library" / "Mail"
INDEX_RELATIVE_PATH = Path("MailData") / "Envelope Index"
DEFAULT_POLL_INTERVAL = 10.0  # seconds
MAX_ROWS_PER_POLL = 200

# ---- SCHEMA (adjust here after the live check) ----------------------------
# Required tables -> columns. ``ROWID`` is implicit on every table.
REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "messages": ("subject", "sender", "date_received", "mailbox"),
    "subjects": ("subject",),
    "addresses": ("address",),
    "mailboxes": ("url",),
}
# Optional columns: selected when present, NULL otherwise.
OPTIONAL_SENDER_NAME = ("addresses", "comment")  # display name

SQL_MAX_ROWID = "SELECT IFNULL(MAX(ROWID), 0) FROM messages"
SQL_NEW_MESSAGES = """
    SELECT m.ROWID, s.subject, a.address, {sender_name}, m.date_received, mb.url
    FROM messages m
    LEFT JOIN subjects s ON m.subject = s.ROWID
    LEFT JOIN addresses a ON m.sender = a.ROWID
    LEFT JOIN mailboxes mb ON m.mailbox = mb.ROWID
    WHERE m.ROWID > ?
    ORDER BY m.ROWID ASC
    LIMIT {limit}
"""

# A mailbox whose last path component matches this is the user's own mail.
_OWN_MAILBOX_RE = re.compile(
    r"^(sent|sent messages|sent mail|sent items|gesendet|envoyés|drafts)$",
    re.IGNORECASE,
)
# ---------------------------------------------------------------------------


def find_index(mail_root: Path | str = DEFAULT_MAIL_ROOT) -> Optional[Path]:
    """Return the ``Envelope Index`` in the newest ``V<n>`` dir, or None."""
    root = Path(mail_root).expanduser()
    try:
        versions = [
            p for p in root.glob("V*")
            if p.is_dir() and p.name[1:].isdigit()
            and (p / INDEX_RELATIVE_PATH).exists()
        ]
    except OSError:  # no Full Disk Access -> PermissionError
        return None
    if not versions:
        return None
    newest = max(versions, key=lambda p: int(p.name[1:]))
    return newest / INDEX_RELATIVE_PATH


def table_columns(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Map every table in the database to its column names."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    return {
        t: [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
        for t in tables
    }


def schema_problems(columns: dict[str, list[str]]) -> list[str]:
    """List missing required tables/columns (empty when the schema fits)."""
    problems: list[str] = []
    for table, cols in REQUIRED_COLUMNS.items():
        if table not in columns:
            problems.append(f"missing table {table!r}")
            continue
        present = {c.lower() for c in columns[table]}
        problems += [
            f"missing column {table}.{c}" for c in cols if c.lower() not in present
        ]
    return problems


def build_new_messages_sql(columns: dict[str, list[str]]) -> str:
    table, col = OPTIONAL_SENDER_NAME
    has_name = col in {c.lower() for c in columns.get(table, [])}
    return SQL_NEW_MESSAGES.format(
        sender_name=f"a.{col}" if has_name else "NULL",
        limit=MAX_ROWS_PER_POLL,
    )


def mailbox_name(url: Optional[str]) -> str:
    """``imap://UUID/%5BGmail%5D/Sent%20Mail`` -> ``[Gmail]/Sent Mail``."""
    if not url:
        return ""
    path = unquote(urlsplit(url).path).strip("/")
    return path or unquote(url)


def is_own_mailbox(url: Optional[str]) -> bool:
    """True for Sent / Drafts mailboxes (the user's own outgoing mail)."""
    name = mailbox_name(url)
    last = name.rsplit("/", 1)[-1].strip()
    return bool(_OWN_MAILBOX_RE.match(last))


def _iso_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _sender(address: Optional[str], name: Optional[str]) -> str:
    if address and name:
        return f"{name} <{address}>"
    return address or name or "unknown"


class MailWatcher:
    """Poll the Envelope Index for new received mail.

    1. Opens the newest ``V*/MailData/Envelope Index`` read-only.
    2. Checks the schema; on a mismatch logs it once and idles.
    3. Records ``MAX(ROWID)`` of ``messages`` as the baseline.
    4. Every ``poll_interval`` seconds pushes a ``mail_received``
       :class:`ContextEvent` per new row not in a Sent/Drafts mailbox.
    """

    def __init__(
        self,
        bus: EventBus,
        mail_root: Path | str = DEFAULT_MAIL_ROOT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        db_path: Path | str | None = None,
    ) -> None:
        self._bus = bus
        self._mail_root = Path(mail_root).expanduser()
        self._db_path = Path(db_path).expanduser() if db_path else None
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_rowid: int = 0
        self._sql: str = ""
        self._poll_error_logged = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="MailWatcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=self._poll_interval + 1.0)

    # ---- internal --------------------------------------------------

    def _open(self) -> Optional[sqlite3.Connection]:
        path = self._db_path or find_index(self._mail_root)
        if path is None or not path.exists():
            logger.warning(
                "Mail Envelope Index not found under %s; watcher idle "
                "(Mail.app unused, or the daemon lacks Full Disk Access)",
                self._db_path or self._mail_root,
            )
            return None
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            logger.warning(
                "Could not open %s read-only (%s). The daemon process "
                "likely needs macOS Full Disk Access.", path, exc,
            )
            return None
        self._db_path = path
        return conn

    def _prepare(self, conn: sqlite3.Connection) -> bool:
        columns = table_columns(conn)
        problems = schema_problems(columns)
        if problems:
            logger.warning(
                "Mail Envelope Index schema mismatch at %s: %s. MailWatcher "
                "idle. Run `python -m events.mail_watcher --schema` and "
                "update SCHEMA in events/mail_watcher.py.",
                self._db_path, "; ".join(problems),
            )
            return False
        self._sql = build_new_messages_sql(columns)
        row = conn.execute(SQL_MAX_ROWID).fetchone()
        self._last_rowid = int(row[0]) if row else 0
        return True

    def _run(self) -> None:
        conn = self._open()
        if conn is None:
            return
        try:
            try:
                if not self._prepare(conn):
                    return
            except sqlite3.Error:
                logger.exception("Failed to read Mail Envelope Index; watcher idle")
                return
            logger.info(
                "MailWatcher started (baseline ROWID=%d, db=%s)",
                self._last_rowid, self._db_path,
            )
            while not self._stop_event.is_set():
                try:
                    self._poll_once(conn)
                except sqlite3.Error as exc:
                    if not self._poll_error_logged:
                        logger.warning("Mail poll failed (%s); will retry", exc)
                        self._poll_error_logged = True
                if self._stop_event.wait(self._poll_interval):
                    return
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def _poll_once(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(self._sql, (self._last_rowid,)).fetchall()
        for rowid, subject, address, name, date_received, url in rows:
            self._last_rowid = max(self._last_rowid, int(rowid))
            if is_own_mailbox(url):
                continue
            event = ContextEvent(
                event_type="mail_received",
                metadata={
                    "sender": _sender(address, name),
                    "subject": subject or "",
                    "date": _iso_date(date_received),
                    "mailbox": mailbox_name(url),
                    "message_id": int(rowid),
                },
            )
            try:
                self._bus.push_threadsafe(event)
            except Exception:  # pragma: no cover - loop not bound / shutting down
                logger.exception("Failed to push mail_received event")


def print_schema(mail_root: Path | str = DEFAULT_MAIL_ROOT, out=sys.stdout) -> int:
    """Print the index's tables/columns and mailbox URLs (no message content)."""
    path = find_index(mail_root)
    if path is None:
        print(f"No V*/{INDEX_RELATIVE_PATH} under {mail_root} "
              "(missing, or no Full Disk Access).", file=out)
        return 1
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        columns = table_columns(conn)
    except sqlite3.Error as exc:
        print(f"Could not open {path}: {exc}", file=out)
        return 1
    print(f"Index: {path}", file=out)
    for table, cols in columns.items():
        print(f"  {table}: {', '.join(cols)}", file=out)
    problems = schema_problems(columns)
    print("Schema check: " + ("OK" if not problems else "; ".join(problems)), file=out)
    if "mailboxes" in columns and "url" in columns["mailboxes"]:
        print("Mailboxes (own = skipped as sent/drafts):", file=out)
        for (url,) in conn.execute("SELECT url FROM mailboxes ORDER BY url"):
            flag = "own" if is_own_mailbox(url) else "   "
            print(f"  [{flag}] {mailbox_name(url)}", file=out)
    conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - manual diagnostic
    if "--schema" in sys.argv[1:]:
        sys.exit(print_schema())
    print("usage: python -m events.mail_watcher --schema")
    sys.exit(2)
