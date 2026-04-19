"""iMessage watcher: polls the SMS/iMessage chat database for inbound messages.

macOS stores iMessage + SMS conversations in ``~/Library/Messages/chat.db``.
That file is owned and actively written by the Messages app, so we open it
read-only via a SQLite URI with ``mode=ro``. SQLite still reads the WAL/SHM
side files for the latest committed state, but never acquires a write lock,
so polling the ``message`` table never blocks the Messages app.

.. note::

    Reading ``chat.db`` requires macOS **Full Disk Access** for the process
    that hosts the daemon (typically the terminal or IDE that launched
    ``uvicorn``). If FDA is not granted, ``sqlite3.connect`` will raise
    ``OperationalError: unable to open database file`` and this listener
    will log a warning and stay idle without crashing the daemon.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"
DEFAULT_POLL_INTERVAL = 10.0  # seconds


class IMessageWatcher:
    """Poll ``chat.db`` for new inbound iMessages.

    Runs a daemon thread that:

    1. Opens a read-only SQLite connection to ``chat.db``.
    2. Records the current ``MAX(ROWID)`` in ``message`` as the baseline so
       we ignore existing history.
    3. Every ``poll_interval`` seconds, queries for any inbound messages
       (``is_from_me = 0``) with ``ROWID`` greater than the last-seen value
       and pushes a :class:`ContextEvent` with ``event_type =
       "imessage_received"`` for each.
    """

    def __init__(
        self,
        bus: EventBus,
        db_path: Path | str = DEFAULT_DB_PATH,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._bus = bus
        self._db_path = Path(db_path).expanduser()
        self._poll_interval = poll_interval
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_rowid: int = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="IMessageWatcher", daemon=True
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
        if not self._db_path.exists():
            logger.warning(
                "iMessage chat.db not found at %s; watcher idle", self._db_path
            )
            return None
        uri = f"file:{self._db_path}?mode=ro"
        try:
            return sqlite3.connect(uri, uri=True, timeout=2.0)
        except sqlite3.Error as exc:
            logger.warning(
                "Could not open %s read-only (%s). The daemon process "
                "likely needs macOS Full Disk Access.",
                self._db_path, exc,
            )
            return None

    def _baseline_rowid(self, conn: sqlite3.Connection) -> int:
        try:
            row = conn.execute("SELECT IFNULL(MAX(ROWID), 0) FROM message").fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error:
            logger.exception("Failed to read baseline ROWID from chat.db")
            return 0

    def _run(self) -> None:
        conn = self._open()
        if conn is None:
            return
        try:
            self._last_rowid = self._baseline_rowid(conn)
            logger.info(
                "IMessageWatcher started (baseline ROWID=%d, db=%s)",
                self._last_rowid, self._db_path,
            )
            while not self._stop_event.is_set():
                try:
                    self._poll_once(conn)
                except sqlite3.Error:
                    logger.exception("iMessage poll failed; will retry")
                if self._stop_event.wait(self._poll_interval):
                    return
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def _poll_once(self, conn: sqlite3.Connection) -> None:
        cursor = conn.execute(
            """
            SELECT m.ROWID, m.text, h.id
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ? AND m.is_from_me = 0
            ORDER BY m.ROWID ASC
            """,
            (self._last_rowid,),
        )
        rows = cursor.fetchall()
        if not rows:
            max_row = conn.execute(
                "SELECT IFNULL(MAX(ROWID), 0) FROM message"
            ).fetchone()
            if max_row and int(max_row[0]) > self._last_rowid:
                self._last_rowid = int(max_row[0])
            return
        for rowid, text, sender in rows:
            self._last_rowid = max(self._last_rowid, int(rowid))
            if not text:
                continue
            event = ContextEvent(
                event_type="imessage_received",
                metadata={
                    "sender": sender or "unknown",
                    "text": text,
                    "rowid": int(rowid),
                },
            )
            try:
                self._bus.push_threadsafe(event)
            except Exception:  # pragma: no cover - loop not bound / shutting down
                logger.exception("Failed to push imessage_received event")
