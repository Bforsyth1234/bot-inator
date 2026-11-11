"""Privacy ledger: a record of everything that left the Mac (#82).

A small SQLite table in its own file (``~/.bot-inator/egress.db``, 0600;
``AGENT_EGRESS_DB`` overrides; the constructor's ``db_path`` overrides
both) with one row per outbound request the daemon made or started.

DECISIONS (todo #82):

1. The ledger records what the daemon ITSELF sent, and says so. For an MCP
   server or a generated tool that runs with network access it records
   that the process ran with network access and the size of the
   arguments, not what that process sent (``host`` is ``None`` and
   ``note`` says why). :data:`LIMIT_NOTE` is the one line the UI shows.
2. Metadata by default: time, destination host, kind, engine role or tool
   name, model, bytes out and in (approximate: the JSON size of the
   request and reply), token counts when the reply has them, and
   ``personal_text`` (``"sent"`` / ``"replaced"``, from ``ai.privacy``).
   The request text is kept only while the "keep request text" switch
   (``AGENT_EGRESS_KEEP_TEXT``, "1"/"0", default off, read live) is on,
   and only for ``TEXT_RETENTION_DAYS`` (7) days.
3. Capped (``max_rows``) and pruned (``RETENTION_DAYS``) like
   ``activity.db`` (#75); cleared by :func:`clear` (the Settings button
   and ``clear_memory``).
4. Recording never fails or delays a request: every public writer catches
   everything and returns False.

Hook helpers (called by the runner at each egress site):

* :func:`record_completion` after a chat-completions call (GroqEngine).
* :func:`wrap_openai_client` around a client whose calls happen inside a
  library (smolagents' ``OpenAIServerModel`` in the agent loop).
* :func:`record_tool_run` for an MCP tool call or a network-enabled
  sandboxed tool.
* :func:`record_open_url` for ``open_url``.
* :func:`model_download` (context manager) around a model load that may
  download weights.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date as _date, datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DB_PATH_ENV = "AGENT_EGRESS_DB"
KEEP_TEXT_KEY = "AGENT_EGRESS_KEEP_TEXT"
DEFAULT_DB_PATH = Path.home() / ".bot-inator" / "egress.db"
RETENTION_DAYS = 90
TEXT_RETENTION_DAYS = 7
DEFAULT_MAX_ROWS = 20_000
PRUNE_EVERY = 100
MAX_TEXT_CHARS = 20_000

KIND_INFERENCE = "inference"
KIND_MCP_TOOL = "mcp_tool"
KIND_SANDBOXED_TOOL = "sandboxed_tool"
KIND_OPEN_URL = "open_url"
KIND_MODEL_DOWNLOAD = "model_download"
KINDS = frozenset({
    KIND_INFERENCE, KIND_MCP_TOOL, KIND_SANDBOXED_TOOL, KIND_OPEN_URL,
    KIND_MODEL_DOWNLOAD,
})
PERSONAL_SENT = "sent"
PERSONAL_REPLACED = "replaced"

NOTHING_TODAY = "Nothing left this Mac today"
LIMIT_NOTE = (
    "MCP servers and tools with network access run as their own processes: "
    "the ledger shows that they ran, not what they sent."
)
PROCESS_NOTE = "ran with network access; what it sent is not visible to bot-inator"
OPEN_URL_NOTE = "opened in your browser; the browser made the request"
HF_HOST = "huggingface.co"

COLUMNS = (
    "id", "ts", "date", "kind", "host", "role", "tool", "model",
    "bytes_out", "bytes_in", "tokens_out", "tokens_in", "personal_text",
    "note", "text",
)
_WRITE_COLUMNS = COLUMNS[1:]


def keep_text_enabled() -> bool:
    """Live read of the "keep request text" switch (default off)."""
    return os.environ.get(KEEP_TEXT_KEY, "0").strip() == "1"


def default_db_path() -> Path:
    override = os.environ.get(DB_PATH_ENV, "").strip()
    return Path(override).expanduser() if override else DEFAULT_DB_PATH


def host_of(url: Any) -> Optional[str]:
    """``"api.groq.com"`` for ``"https://api.groq.com/openai/v1"``."""
    if not isinstance(url, str) or not url:
        return None
    try:
        return urlsplit(url).hostname or None
    except ValueError:
        return None


# ---- sizes ------------------------------------------------------------------


def _jsonable(obj: Any, depth: int = 0) -> Any:
    if depth > 12:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    if isinstance(obj, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1) for v in obj]
    dump = getattr(obj, "model_dump", None)  # pydantic (openai responses)
    if callable(dump):
        try:
            return _jsonable(dump(), depth + 1)
        except Exception:
            pass
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        try:
            return {f.name: _jsonable(getattr(obj, f.name, None), depth + 1)
                    for f in dataclasses.fields(obj)}
        except Exception:
            pass
    attrs = getattr(obj, "__dict__", None)
    if isinstance(attrs, dict):
        return {k: _jsonable(v, depth + 1) for k, v in attrs.items()
                if not k.startswith("_") and not callable(v)}
    return str(obj)


def payload_text(obj: Any) -> str:
    """``obj`` as the JSON text that measures (and, if kept, records) it."""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(_jsonable(obj), ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def payload_size(obj: Any) -> int:
    """Approximate bytes on the wire: UTF-8 length of :func:`payload_text`."""
    if obj is None:
        return 0
    if isinstance(obj, bytes):
        return len(obj)
    return len(payload_text(obj).encode("utf-8"))


def personal_text_flag(role: Optional[str], payload: str = "") -> Optional[str]:
    """``"replaced"`` when ``ai.privacy`` kept personal text out of a prompt
    for ``role``, ``"sent"`` when the user's opt-in let it through."""
    try:
        from ai import privacy

        if payload and privacy.PLACEHOLDER in payload:
            return PERSONAL_REPLACED
        if role is None:
            return PERSONAL_SENT if privacy.personal_text_allowed() else PERSONAL_REPLACED
        return PERSONAL_REPLACED if privacy.should_redact(role) else PERSONAL_SENT
    except Exception:
        return None


def _usage(response: Any) -> tuple[Optional[int], Optional[int]]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None, None

    def pick(name: str) -> Optional[int]:
        v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    return pick("prompt_tokens"), pick("completion_tokens")


# ---- the store ----------------------------------------------------------------


class EgressLog:
    """Local, capped ledger of outbound requests. Thread-safe; writers never raise."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        keep_text: Optional[bool] = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        retention_days: int = RETENTION_DAYS,
        text_retention_days: int = TEXT_RETENTION_DAYS,
        tz: Optional[tzinfo] = None,
        prune_every: int = PRUNE_EVERY,
    ) -> None:
        self.db_path = Path(db_path).expanduser() if db_path else default_db_path()
        self._keep_text = keep_text  # None = follow the live switch
        self.max_rows = max(1, int(max_rows))
        self.retention_days = max(1, int(retention_days))
        self.text_retention_days = max(1, int(text_retention_days))
        self.tz = tz
        self.prune_every = max(1, int(prune_every))
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._since_prune = 0

    @property
    def keep_text(self) -> bool:
        return keep_text_enabled() if self._keep_text is None else self._keep_text

    @keep_text.setter
    def keep_text(self, value: Optional[bool]) -> None:
        self._keep_text = value

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
                "CREATE TABLE IF NOT EXISTS egress ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL,"
                " host TEXT, role TEXT, tool TEXT, model TEXT,"
                " bytes_out INTEGER NOT NULL DEFAULT 0,"
                " bytes_in INTEGER NOT NULL DEFAULT 0,"
                " tokens_out INTEGER, tokens_in INTEGER,"
                " personal_text TEXT, note TEXT, text TEXT)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS egress_ts ON egress(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS egress_date ON egress(date)")
            conn.commit()
            self._conn = conn
            self._prune_locked(conn, time.time())
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _local(self, ts: float) -> datetime:
        dt = datetime.fromtimestamp(ts, timezone.utc)
        return dt.astimezone(self.tz) if self.tz is not None else dt.astimezone()

    # -- writing ------------------------------------------------------------

    def record(
        self,
        kind: str,
        host: Optional[str] = None,
        role: Optional[str] = None,
        model: Optional[str] = None,
        bytes_out: int = 0,
        bytes_in: int = 0,
        personal_text: Optional[str] = None,
        text: Optional[str] = None,
        *,
        tool: Optional[str] = None,
        note: Optional[str] = None,
        tokens_out: Optional[int] = None,
        tokens_in: Optional[int] = None,
        ts: Optional[float] = None,
    ) -> bool:
        """Add one row. ``text`` is dropped unless keep-text is on. Never raises."""
        try:
            stamp = time.time() if ts is None else float(ts)
            kept = None
            if text and self.keep_text:
                kept = str(text)[:MAX_TEXT_CHARS]
            row = {
                "ts": stamp,
                "date": self._local(stamp).date().isoformat(),
                "kind": str(kind),
                "host": host,
                "role": role,
                "tool": tool,
                "model": model,
                "bytes_out": max(0, int(bytes_out or 0)),
                "bytes_in": max(0, int(bytes_in or 0)),
                "tokens_out": tokens_out,
                "tokens_in": tokens_in,
                "personal_text": personal_text,
                "note": note,
                "text": kept,
            }
            with self._lock:
                conn = self.connect()
                conn.execute(
                    f"INSERT INTO egress ({', '.join(_WRITE_COLUMNS)}) "
                    f"VALUES ({', '.join('?' for _ in _WRITE_COLUMNS)})",
                    [row[c] for c in _WRITE_COLUMNS],
                )
                conn.commit()
                self._since_prune += 1
                if self._since_prune >= self.prune_every:
                    self._prune_locked(conn, time.time())
            return True
        except Exception:
            logger.exception("egress record failed")
            return False

    # -- retention ----------------------------------------------------------

    def _prune_locked(self, conn: sqlite3.Connection, now: float) -> int:
        self._since_prune = 0
        removed = conn.execute(
            "DELETE FROM egress WHERE ts < ?", (now - self.retention_days * 86400,)
        ).rowcount
        conn.execute(
            "UPDATE egress SET text = NULL WHERE text IS NOT NULL AND ts < ?",
            (now - self.text_retention_days * 86400,),
        )
        count = conn.execute("SELECT COUNT(*) FROM egress").fetchone()[0]
        if count > self.max_rows:
            removed += conn.execute(
                "DELETE FROM egress WHERE id IN "
                "(SELECT id FROM egress ORDER BY ts, id LIMIT ?)",
                (count - self.max_rows,),
            ).rowcount
        conn.commit()
        return removed

    def prune(self, now: Optional[datetime] = None) -> int:
        """Drop rows past retention and text past text retention, then cap."""
        ref = (now or datetime.now(timezone.utc)).timestamp()
        with self._lock:
            return self._prune_locked(self.connect(), ref)

    def forget_text(self) -> int:
        """Erase every kept request text (keep-text switched off)."""
        with self._lock:
            conn = self.connect()
            n = conn.execute(
                "UPDATE egress SET text = NULL WHERE text IS NOT NULL"
            ).rowcount
            conn.commit()
            return n

    def clear(self) -> int:
        """Delete every row (the Clear button, ``clear_memory``)."""
        with self._lock:
            conn = self.connect()
            n = conn.execute("DELETE FROM egress").rowcount
            conn.commit()
            return n

    # -- reading ------------------------------------------------------------

    def rows(
        self, since: Optional[datetime] = None, limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Rows newest first, each with an ISO ``time``."""
        sql = f"SELECT {', '.join(COLUMNS)} FROM egress"
        args: list[Any] = []
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            sql += " WHERE ts >= ?"
            args.append(since.timestamp())
        sql += " ORDER BY ts DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(max(0, int(limit)))
        with self._lock:
            cur = self.connect().execute(sql, args)
            out = [dict(zip(COLUMNS, r)) for r in cur.fetchall()]
        for row in out:
            row["time"] = self._local(row["ts"]).isoformat(timespec="seconds")
        return out

    def count(self) -> int:
        with self._lock:
            return self.connect().execute("SELECT COUNT(*) FROM egress").fetchone()[0]

    def stats(self, day: Optional[_date | str] = None) -> dict[str, Any]:
        """Totals for one local day (default today)."""
        if day is None:
            day = self._local(time.time()).date()
        key = day if isinstance(day, str) else day.isoformat()
        with self._lock:
            conn = self.connect()
            n, out_b, in_b = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes_out), 0), COALESCE(SUM(bytes_in), 0)"
                " FROM egress WHERE date = ?", (key,),
            ).fetchone()
            by_kind = dict(conn.execute(
                "SELECT kind, COUNT(*) FROM egress WHERE date = ? GROUP BY kind", (key,),
            ).fetchall())
            by_host = dict(conn.execute(
                "SELECT COALESCE(host, ''), COUNT(*) FROM egress WHERE date = ?"
                " GROUP BY host", (key,),
            ).fetchall())
            sent = conn.execute(
                "SELECT COUNT(*) FROM egress WHERE date = ? AND personal_text = ?",
                (key, PERSONAL_SENT),
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM egress").fetchone()[0]
        return {
            "day": key,
            "count": n,
            "bytes_out": out_b,
            "bytes_in": in_b,
            "by_kind": by_kind,
            "by_host": by_host,
            "personal_text_sent": sent,
            "summary": NOTHING_TODAY if n == 0 else _summary(n, out_b, in_b),
            "total_rows": total,
            "keep_text": self.keep_text,
            "retention_days": self.retention_days,
            "text_retention_days": self.text_retention_days,
            "max_rows": self.max_rows,
        }


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"  # pragma: no cover


def _summary(n: int, out_b: int, in_b: int) -> str:
    noun = "request" if n == 1 else "requests"
    return f"{n} {noun} left this Mac today ({_human_bytes(out_b)} out, {_human_bytes(in_b)} in)"


# ---- process-wide default ------------------------------------------------------

_default: Optional[EgressLog] = None
_default_lock = threading.Lock()


def default_log() -> EgressLog:
    """The process-wide ledger at ``default_db_path()``."""
    global _default
    with _default_lock:
        if _default is None:
            _default = EgressLog()
        return _default


def set_default_log(log: Optional[EgressLog]) -> None:
    """Replace the process-wide ledger (tests)."""
    global _default
    with _default_lock:
        _default = log


def record(kind: str, host: Optional[str] = None, role: Optional[str] = None,
           model: Optional[str] = None, bytes_out: int = 0, bytes_in: int = 0,
           personal_text: Optional[str] = None, text: Optional[str] = None,
           **extra: Any) -> bool:
    """Record in the default ledger. Never raises."""
    try:
        return default_log().record(kind, host, role, model, bytes_out, bytes_in,
                                    personal_text, text, **extra)
    except Exception:
        logger.exception("egress record failed")
        return False


def rows(since: Optional[datetime] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
    return default_log().rows(since, limit)


def stats(day: Optional[_date | str] = None) -> dict[str, Any]:
    return default_log().stats(day)


def clear() -> int:
    return default_log().clear()


# ---- hook helpers ------------------------------------------------------------


def record_completion(
    *, role: Optional[str], model: Optional[str], api_base: Optional[str],
    request: Any, response: Any = None, error: Optional[BaseException] = None,
    log: Optional[EgressLog] = None,
) -> bool:
    """One chat-completions call to a cloud engine. Never raises.

    ``request`` is what was sent (the ``create`` kwargs, or the messages),
    ``response`` the reply (``None`` when the call raised: pass ``error``).
    """
    try:
        text = payload_text(request)
        tokens_out, tokens_in = _usage(response)
        note = None if error is None else f"failed: {type(error).__name__}"
        target = log or default_log()
        return target.record(
            KIND_INFERENCE, host_of(api_base), role, model,
            len(text.encode("utf-8")), payload_size(response),
            personal_text_flag(role, text), text,
            note=note, tokens_out=tokens_out, tokens_in=tokens_in,
        )
    except Exception:
        logger.exception("egress record failed")
        return False


class _LoggedCompletions:
    def __init__(self, inner: Any, owner: "_LoggedOpenAIClient") -> None:
        self._inner = inner
        self._owner = owner

    def create(self, *args: Any, **kwargs: Any) -> Any:
        owner = self._owner
        model = kwargs.get("model") or owner._model
        try:
            response = self._inner.create(*args, **kwargs)
        except BaseException as exc:
            record_completion(role=owner._role, model=model, api_base=owner._api_base,
                              request=kwargs, error=exc, log=owner._log)
            raise
        if kwargs.get("stream"):
            # A stream's size is unknown until it is consumed; record what went out.
            record_completion(role=owner._role, model=model, api_base=owner._api_base,
                              request=kwargs, log=owner._log)
        else:
            record_completion(role=owner._role, model=model, api_base=owner._api_base,
                              request=kwargs, response=response, log=owner._log)
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _LoggedChat:
    def __init__(self, inner: Any, owner: "_LoggedOpenAIClient") -> None:
        self._inner = inner
        self.completions = _LoggedCompletions(inner.completions, owner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _LoggedOpenAIClient:
    """Delegating proxy: ``chat.completions.create`` calls are recorded."""

    egress_logged = True

    def __init__(self, inner: Any, role: Optional[str], api_base: Optional[str],
                 model: Optional[str], log: Optional[EgressLog]) -> None:
        self._inner = inner
        self._role = role
        self._api_base = api_base or str(getattr(inner, "base_url", "") or "") or None
        self._model = model
        self._log = log
        self.chat = _LoggedChat(inner.chat, self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def wrap_openai_client(client: Any, *, role: Optional[str], api_base: Optional[str] = None,
                       model: Optional[str] = None, log: Optional[EgressLog] = None) -> Any:
    """``client`` with every ``chat.completions.create`` recorded.

    Returns ``client`` unchanged when it is ``None``, already wrapped, or has
    no ``chat.completions`` (wrapping never breaks the caller).
    """
    try:
        if client is None or getattr(client, "egress_logged", False):
            return client
        if getattr(getattr(client, "chat", None), "completions", None) is None:
            return client
        return _LoggedOpenAIClient(client, role, api_base, model, log)
    except Exception:
        logger.exception("egress wrap failed")
        return client


def record_tool_run(
    kind: str, tool: str, arguments: Any = None, *, note: Optional[str] = None,
    host: Optional[str] = None, log: Optional[EgressLog] = None,
) -> bool:
    """An MCP tool call or a network-enabled sandboxed tool run (decision 1)."""
    try:
        text = payload_text(arguments) if arguments is not None else None
        return (log or default_log()).record(
            kind, host, None, None, payload_size(arguments), 0, None, text,
            tool=tool, note=note or PROCESS_NOTE,
        )
    except Exception:
        logger.exception("egress record failed")
        return False


def record_open_url(url: str, *, log: Optional[EgressLog] = None) -> bool:
    """``open_url`` handed ``url`` to the browser. Only the host is kept by default."""
    try:
        return (log or default_log()).record(
            KIND_OPEN_URL, host_of(url), None, None, payload_size(url), 0, None, url,
            tool="open_url", note=OPEN_URL_NOTE,
        )
    except Exception:
        logger.exception("egress record failed")
        return False


def _hf_cache_dir() -> Path:
    explicit = os.environ.get("HF_HUB_CACHE") or os.environ.get("HUGGINGFACE_HUB_CACHE")
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _repo_bytes(model_name: str, cache_dir: Path) -> int:
    blobs = cache_dir / ("models--" + model_name.replace("/", "--")) / "blobs"
    total = 0
    try:
        for entry in blobs.iterdir():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        return 0
    return total


@contextlib.contextmanager
def model_download(
    model_name: str, *, role: Optional[str] = None,
    cache_dir: Optional[Path] = None, log: Optional[EgressLog] = None,
) -> Iterator[None]:
    """Wrap a model load: records a row when the Hugging Face cache grew.

    A local path, or ``HF_HUB_OFFLINE=1``, records nothing. Errors in the
    measurement are swallowed; errors in the load propagate unchanged.
    """
    measure = False
    before = 0
    cache = None
    try:
        local = bool(model_name) and Path(model_name).expanduser().exists()
        offline = os.environ.get("HF_HUB_OFFLINE", "").strip() == "1"
        if model_name and not local and not offline:
            cache = cache_dir or _hf_cache_dir()
            before = _repo_bytes(model_name, cache)
            measure = True
    except Exception:
        measure = False
    try:
        yield
    finally:
        if measure and cache is not None:
            try:
                grown = _repo_bytes(model_name, cache) - before
                if grown > 0:
                    (log or default_log()).record(
                        KIND_MODEL_DOWNLOAD, HF_HOST, role, model_name, 0, grown,
                        None, None, note="model weights downloaded",
                    )
            except Exception:
                logger.exception("egress record failed")


__all__ = [
    "COLUMNS", "DB_PATH_ENV", "DEFAULT_MAX_ROWS", "EgressLog", "KEEP_TEXT_KEY",
    "KINDS", "KIND_INFERENCE", "KIND_MCP_TOOL", "KIND_MODEL_DOWNLOAD",
    "KIND_OPEN_URL", "KIND_SANDBOXED_TOOL", "LIMIT_NOTE", "NOTHING_TODAY",
    "PERSONAL_REPLACED", "PERSONAL_SENT", "RETENTION_DAYS", "TEXT_RETENTION_DAYS",
    "clear", "default_db_path", "default_log", "host_of", "keep_text_enabled",
    "model_download", "payload_size", "personal_text_flag", "record",
    "record_completion", "record_open_url", "record_tool_run", "rows",
    "set_default_log", "stats", "wrap_openai_client",
]
