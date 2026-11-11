"""Local activity history for routine detection (#75).

A small SQLite table in its own file (``~/.bot-inator/activity.db``, 0600)
that remembers *what kind* of thing happened and when, across days, so
``ai.routines`` can find repeats. It never stores content.

Only these fields are kept (the allow-list, ``STORED_FIELDS``):

* ``ts`` (UTC epoch), local ``date``, ``weekday`` (0 = Monday), ``hour``
* ``event_type``
* ``app``: app name (``app_activated`` / ``clipboard_changed``)
* ``launched``: 1 when an ``app_activated`` is a new process (new pid)
  rather than a switch back to an app already running
* ``ext``, ``folder``, ``from_folder``, ``action``: for ``file`` events the
  file's extension and parent folder (``~``-relative), never its name
* ``bulk``: for ``mail_received``, 1 = bulk/list mail, 0 = from a person
* ``kind``: for ``clipboard_changed``, the pasteboard kind
  (text/url/image/file), never the clipboard content

Message text, senders, subjects, window titles, URLs, calendar titles and
clipboard content are never read from the event. Rows older than
``RETENTION_DAYS`` are pruned and the table is capped at ``max_rows``.

Switch: ``AGENT_ACTIVITY_HISTORY`` ("1"/"0", default on), read live.
Off means :meth:`ActivityStore.record` stores nothing.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENABLED_KEY = "AGENT_ACTIVITY_HISTORY"
DB_PATH_ENV = "AGENT_ACTIVITY_DB"
DEFAULT_DB_PATH = Path.home() / ".bot-inator" / "activity.db"
RETENTION_DAYS = 90
DEFAULT_MAX_ROWS = 50_000
PRUNE_EVERY = 200  # records between automatic prunes

# Event types worth remembering. Anything else (notices, daily_brief,
# internal events) is ignored.
RECORDED_TYPES = frozenset({
    "app_activated",
    "file",
    "mail_received",
    "clipboard_changed",
    "event_starting_soon",
    "imessage_received",
})
# File actions kept: arrivals and moves. "modified" floods and says nothing
# a routine can act on.
FILE_ACTIONS = frozenset({"created", "renamed", "moved"})
CLIPBOARD_KINDS = frozenset({"text", "url", "image", "file"})

STORED_FIELDS = (
    "ts", "date", "weekday", "hour", "event_type", "app", "launched",
    "ext", "folder", "from_folder", "action", "bulk", "kind",
)

_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_MAX_APP = 100
_MAX_FOLDER = 300


def history_enabled() -> bool:
    """Live read of the Settings switch (default on)."""
    return os.environ.get(ENABLED_KEY, "1").strip() != "0"


def default_db_path() -> Path:
    override = os.environ.get(DB_PATH_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


def _short_folder(path: Path) -> str:
    """Parent folder with the home directory written as ``~``."""
    folder = str(path)
    home = str(Path.home())
    if folder == home:
        folder = "~"
    elif folder.startswith(home + os.sep):
        folder = "~" + folder[len(home):]
    return folder[:_MAX_FOLDER]


def _ext(path: Path) -> str:
    ext = path.suffix.lower()
    return ext if _EXT_RE.match(ext) else ""


def _app(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_MAX_APP]


class ActivityStore:
    """Allow-listed, local activity history. Thread-safe."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        enabled: Optional[bool] = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        retention_days: int = RETENTION_DAYS,
        tz: Optional[tzinfo] = None,
        prune_every: int = PRUNE_EVERY,
    ) -> None:
        self.db_path = Path(db_path).expanduser() if db_path else default_db_path()
        # None = follow the live Settings switch; True/False = fixed.
        self._enabled = enabled
        self.max_rows = max(1, int(max_rows))
        self.retention_days = max(1, int(retention_days))
        self.tz = tz  # None = the system's local time zone
        self.prune_every = max(1, int(prune_every))
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._since_prune = 0

    # -- switch -------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return history_enabled() if self._enabled is None else self._enabled

    @enabled.setter
    def enabled(self, value: Optional[bool]) -> None:
        self._enabled = value

    # -- connection ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            parent = self.db_path.parent
            if not parent.exists():
                parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.db_path.exists():
                fd = os.open(self.db_path, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
            os.chmod(self.db_path, 0o600)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS activity ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL, date TEXT NOT NULL,"
                " weekday INTEGER NOT NULL, hour INTEGER NOT NULL,"
                " event_type TEXT NOT NULL, app TEXT, launched INTEGER,"
                " ext TEXT, folder TEXT, from_folder TEXT, action TEXT,"
                " bulk INTEGER, kind TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS activity_ts ON activity(ts)")
            # Last pid seen per app, so a switch back to a running app isn't
            # mistaken for a launch, including across daemon restarts.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS app_pids ("
                " app TEXT PRIMARY KEY, pid INTEGER NOT NULL)"
            )
            conn.commit()
            self._conn = conn
            self._prune_locked(conn, time.time())
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- writing ------------------------------------------------------------

    def _local(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(self.tz) if self.tz is not None else ts.astimezone()

    def _fields(self, event: Any, conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        """Pick the allow-listed fields out of ``event``; None = don't store."""
        etype = getattr(event, "event_type", None)
        if etype not in RECORDED_TYPES:
            return None
        meta = getattr(event, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        row: dict[str, Any] = {"event_type": etype}
        if etype == "app_activated":
            app = _app(getattr(event, "app_name", None))
            if app is None:
                return None
            row["app"] = app
            pid = meta.get("pid")
            launched = 1
            if isinstance(pid, int):
                prev = conn.execute(
                    "SELECT pid FROM app_pids WHERE app = ?", (app,)
                ).fetchone()
                launched = 0 if prev is not None and prev[0] == pid else 1
                conn.execute(
                    "INSERT OR REPLACE INTO app_pids (app, pid) VALUES (?, ?)",
                    (app, pid),
                )
            row["launched"] = launched
        elif etype == "file":
            action = meta.get("action")
            path = meta.get("path")
            if action not in FILE_ACTIONS or not isinstance(path, str) or not path:
                return None
            p = Path(path)
            row["action"] = "renamed" if action == "moved" else action
            row["ext"] = _ext(p)
            row["folder"] = _short_folder(p.parent)
            src = meta.get("from")
            if isinstance(src, str) and src:
                row["from_folder"] = _short_folder(Path(src).parent)
        elif etype == "mail_received":
            bulk = meta.get("bulk")
            row["bulk"] = None if bulk is None else (1 if bulk else 0)
        elif etype == "clipboard_changed":
            row["app"] = _app(getattr(event, "app_name", None) or meta.get("app"))
            kind = meta.get("kind")
            row["kind"] = kind if kind in CLIPBOARD_KINDS else None
        # event_starting_soon / imessage_received: the type and time only.
        return row

    def record(self, event: Any) -> bool:
        """Store the allow-listed fields of ``event``. Never raises.

        Returns True when a row was written.
        """
        if not self.enabled:
            return False
        try:
            with self._lock:
                conn = self.connect()
                row = self._fields(event, conn)
                if row is None:
                    conn.commit()
                    return False
                ts = getattr(event, "timestamp", None)
                if not isinstance(ts, datetime):
                    ts = datetime.now(timezone.utc)
                local = self._local(ts)
                row.update(
                    ts=local.timestamp(),
                    date=local.date().isoformat(),
                    weekday=local.weekday(),
                    hour=local.hour,
                )
                cols = [c for c in STORED_FIELDS if c in row]
                conn.execute(
                    f"INSERT INTO activity ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    [row[c] for c in cols],
                )
                conn.commit()
                self._since_prune += 1
                if self._since_prune >= self.prune_every:
                    self._prune_locked(conn, time.time())
                return True
        except Exception:
            logger.exception("activity record failed")
            return False

    # -- retention ----------------------------------------------------------

    def _prune_locked(self, conn: sqlite3.Connection, now: float) -> int:
        self._since_prune = 0
        cutoff = now - self.retention_days * 86400
        removed = conn.execute("DELETE FROM activity WHERE ts < ?", (cutoff,)).rowcount
        count = conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        if count > self.max_rows:
            removed += conn.execute(
                "DELETE FROM activity WHERE id IN "
                "(SELECT id FROM activity ORDER BY ts, id LIMIT ?)",
                (count - self.max_rows,),
            ).rowcount
        conn.commit()
        return removed

    def prune(self, now: Optional[datetime] = None) -> int:
        """Drop rows past retention, then the oldest over the cap."""
        ref = (now or datetime.now(timezone.utc)).timestamp()
        with self._lock:
            return self._prune_locked(self.connect(), ref)

    def clear(self) -> int:
        """Delete all history (the "Clear activity history" button)."""
        with self._lock:
            conn = self.connect()
            n = conn.execute("DELETE FROM activity").rowcount
            conn.execute("DELETE FROM app_pids")
            conn.commit()
            return n

    # -- reading ------------------------------------------------------------

    def rows(self, since: Optional[datetime] = None) -> list[dict[str, Any]]:
        """All rows (oldest first) as dicts of ``STORED_FIELDS``."""
        sql = f"SELECT {', '.join(STORED_FIELDS)} FROM activity"
        args: tuple[Any, ...] = ()
        if since is not None:
            sql += " WHERE ts >= ?"
            args = (since.timestamp(),)
        sql += " ORDER BY ts, id"
        with self._lock:
            cur = self.connect().execute(sql, args)
            return [dict(zip(STORED_FIELDS, r)) for r in cur.fetchall()]

    def count(self) -> int:
        with self._lock:
            return self.connect().execute("SELECT COUNT(*) FROM activity").fetchone()[0]

    def stats(self) -> dict[str, Any]:
        """Small summary for a Settings/list route: count, span, by type."""
        with self._lock:
            conn = self.connect()
            n, first, last = conn.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM activity"
            ).fetchone()
            by_type = dict(conn.execute(
                "SELECT event_type, COUNT(*) FROM activity GROUP BY event_type"
            ).fetchall())

        def iso(v: Optional[float]) -> Optional[str]:
            return None if v is None else datetime.fromtimestamp(v, timezone.utc).isoformat()

        return {
            "enabled": self.enabled,
            "count": n,
            "first_seen": iso(first),
            "last_seen": iso(last),
            "by_type": by_type,
            "retention_days": self.retention_days,
            "max_rows": self.max_rows,
        }


_default: Optional[ActivityStore] = None
_default_lock = threading.Lock()


def default_store() -> ActivityStore:
    """The process-wide store at ``default_db_path()``."""
    global _default
    with _default_lock:
        if _default is None:
            _default = ActivityStore()
        return _default


def record(event: Any) -> bool:
    """Record ``event`` in the default store (no-op when the switch is off)."""
    return default_store().record(event)


def clear() -> int:
    """Clear the default store."""
    return default_store().clear()


__all__ = [
    "ActivityStore", "ENABLED_KEY", "DB_PATH_ENV", "RETENTION_DAYS",
    "DEFAULT_MAX_ROWS", "STORED_FIELDS", "history_enabled", "default_db_path",
    "default_store", "record", "clear",
]
