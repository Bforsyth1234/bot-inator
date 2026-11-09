"""sqlite-vec backed long-term memory for the agent.

The on-disk store lives at ``~/.bot-inator/memory.db`` by default and is
shared across the orchestrator (which injects recalled memories into the
prompt before each event) and the ``remember_preference`` tool (which
writes user-opted preferences into it).

Embeddings are produced by a pluggable callable. The default embedder
lazy-loads ``sentence-transformers`` (``all-MiniLM-L6-v2``, 384-dim) and
caches the model across calls. If ``sentence-transformers`` is not
installed, a deterministic hash-based fallback keeps the surface usable
for tests and environments without PyTorch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

EMBED_DIM = 384
Embedder = Callable[[str], list[float]]

DEFAULT_DB_PATH = Path.home() / ".bot-inator" / "memory.db"
DEFAULT_ST_MODEL = "all-MiniLM-L6-v2"


def _fallback_embed(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic hash-based embedding used when sentence-transformers is absent."""
    vec: list[float] = []
    i = 0
    while len(vec) < dim:
        digest = hashlib.sha256(f"{i}:{text}".encode()).digest()
        for j in range(0, len(digest), 4):
            if len(vec) >= dim:
                break
            val = struct.unpack("<I", digest[j : j + 4])[0] / 0xFFFFFFFF
            vec.append(val * 2.0 - 1.0)
        i += 1
    return vec


# ---------------------------------------------------------------------------
# Default embedder (sentence-transformers, lazy-loaded)
# ---------------------------------------------------------------------------

_st_model: Any = None
_st_lock = threading.Lock()


def _load_st_model(model_name: str = DEFAULT_ST_MODEL) -> Any:
    """Lazily instantiate and cache a SentenceTransformer model.

    Pinned to CPU by default: PyTorch's MPS backend dispatches Metal
    command buffers that collide with concurrent ``mlx-lm`` encoders on
    the same GPU, tripping ``A command encoder is already encoding to
    this command buffer`` aborts. ``all-MiniLM-L6-v2`` is ~22M params so
    CPU inference is a few tens of ms per encode — negligible next to an
    agent step. Override via ``AGENT_EMBED_DEVICE=mps`` if/when upstream
    resolves the contention.
    """
    global _st_model
    if _st_model is not None:
        return _st_model
    with _st_lock:
        if _st_model is not None:
            return _st_model
        from sentence_transformers import SentenceTransformer  # type: ignore

        device = os.environ.get("AGENT_EMBED_DEVICE", "cpu").strip() or "cpu"
        logger.info(
            "Loading sentence-transformers model %s on device=%s",
            model_name,
            device,
        )
        _st_model = SentenceTransformer(model_name, device=device)
    return _st_model


def _st_embed(text: str) -> list[float]:
    model = _load_st_model()
    vec = model.encode(text, normalize_embeddings=True)
    return [float(x) for x in vec]


def default_embedder() -> Embedder:
    """Return the sentence-transformers embedder, or the hash fallback.

    The decision is made once at call time: if ``sentence-transformers`` is
    importable we return ``_st_embed``; otherwise we return the hash-based
    scaffold and log a warning. Either way the returned callable is safe to
    use on every :meth:`Memory.save_memory` / :meth:`Memory.recall_memory`.
    """
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        logger.warning(
            "sentence-transformers not installed; falling back to hash "
            "embeddings. Install with `pip install sentence-transformers` "
            "to enable real semantic recall."
        )
        return lambda t: _fallback_embed(t, EMBED_DIM)
    return _st_embed


DEFAULT_MAX_ROWS = 5000
PREFERENCE_KIND = "preference"

# The "vector search disabled" warning fires once per process, not on every
# connect or recall (the 2026-11-09 live logs printed it on every recall).
_vec_warned = False
_vec_warned_lock = threading.Lock()


def _warn_vec_unavailable(reason: str) -> None:
    global _vec_warned
    with _vec_warned_lock:
        if _vec_warned:
            return
        _vec_warned = True
    logger.warning(
        "Vector memory search disabled (%s); recall uses the most recent "
        "memories instead. Fix: `uv pip install --python .venv/bin/python "
        "sqlite-vec` (or `pip install sqlite-vec`); if your Python's sqlite3 "
        "lacks enable_load_extension, also install `pysqlite3-binary`.",
        reason,
    )


def _try_load_vec(conn: Any) -> Optional[str]:
    """Load sqlite-vec into ``conn``. Returns ``None`` on success, else why not."""
    if not hasattr(conn, "enable_load_extension"):
        return "sqlite3 built without load_extension support"
    try:
        import sqlite_vec  # type: ignore
    except ImportError:
        return "sqlite-vec not installed"
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except Exception as exc:  # sqlite3/pysqlite3 OperationalError, AttributeError
        return f"could not load vec0: {exc}"
    return None


class Memory:
    """SQLite + sqlite-vec backed embedding store.

    One connection is shared by the event loop thread and the
    ``asyncio.to_thread`` workers the orchestrator uses, so every
    connection access (including ``close``) holds ``self._lock``. Without
    it, ``close()`` at lifespan teardown could free the connection while a
    worker was still inside ``search``, a use-after-free that segfaulted.

    Retention: at most ``max_rows`` memories are kept. When a save goes
    over the cap, the oldest rows whose metadata ``kind`` isn't
    ``"preference"`` go first; preferences are evicted (oldest first) only
    when nothing else is left.
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        embedder: Optional[Embedder] = None,
        dim: int = EMBED_DIM,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self.db_path = str(db_path)
        self.dim = dim
        self.max_rows = max(1, int(max_rows))
        self._embedder: Embedder = embedder or default_embedder()
        self._conn: Any = None
        self._db_error: tuple[type[BaseException], ...] = (sqlite3.Error,)
        self.vec_enabled = False
        self._lock = threading.RLock()

    def _open(self) -> Any:
        """Open a connection with vec0 loaded if possible.

        Tries the stdlib ``sqlite3`` first, then ``pysqlite3`` (from
        ``pysqlite3-binary``) when the stdlib build can't load extensions.
        """
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        reason = _try_load_vec(conn)
        if reason is None:
            self._db_error = (sqlite3.Error,)
            self.vec_enabled = True
            return conn
        try:
            import pysqlite3.dbapi2 as pysqlite3  # type: ignore
        except ImportError:
            pass
        else:
            alt = pysqlite3.connect(self.db_path, check_same_thread=False)
            alt_reason = _try_load_vec(alt)
            if alt_reason is None:
                conn.close()
                self._db_error = (sqlite3.Error, pysqlite3.Error)
                self.vec_enabled = True
                logger.info("Using pysqlite3 for memory (stdlib: %s)", reason)
                return alt
            alt.close()
            reason = f"{reason}; pysqlite3: {alt_reason}"
        self._db_error = (sqlite3.Error,)
        self.vec_enabled = False
        _warn_vec_unavailable(reason)
        return conn

    def connect(self) -> Any:
        with self._lock:
            if self._conn is not None:
                return self._conn
            parent = Path(self.db_path).expanduser().parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
            conn = self._open()
            self._init_schema(conn)
            self._conn = conn
            self._backfill_vectors(conn)
            return conn

    def _backfill_vectors(self, conn: Any) -> None:
        """Embed rows saved while vec0 was unavailable so they become searchable."""
        if not self.vec_enabled:
            return
        try:
            missing = conn.execute(
                "SELECT id, text FROM memories WHERE id NOT IN "
                "(SELECT rowid FROM memory_vec)"
            ).fetchall()
            for rowid, text in missing:
                conn.execute(
                    "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                    (rowid, self._serialize(self._embedder(text))),
                )
            conn.commit()
        except Exception:
            logger.exception("vec0 backfill failed")
            return
        if missing:
            logger.info("Backfilled vectors for %d memories", len(missing))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _init_schema(self, conn: Any) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_log_event_id "
            "ON chat_log(event_id)"
        )
        if self.vec_enabled:
            try:
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0("
                    f"embedding float[{self.dim}])"
                )
            except self._db_error as exc:
                self.vec_enabled = False
                _warn_vec_unavailable(f"could not create vec0 table: {exc}")
        conn.commit()

    # ---- chat transcript log ---------------------------------------------

    def log_chat(self, event_id: str, role: str, text: str) -> int:
        """Append one chat turn to the persistent transcript.

        ``role`` is typically ``"user"`` or ``"assistant"``. Returns the
        inserted row id. Callers must not rely on per-turn ordering beyond
        insertion order; the query API sorts by ``id`` ascending.
        """
        with self._lock:
            conn = self.connect()
            cur = conn.execute(
                "INSERT INTO chat_log (event_id, role, text) VALUES (?, ?, ?)",
                (event_id, role, text),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_chat_log(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` turns in insertion order.

        The rows come back oldest-first so the UI can render them straight
        into a scrolling log without reversing. ``limit`` is clamped to
        ``[1, 1000]`` to keep the payload bounded.
        """
        limit = max(1, min(int(limit), 1000))
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                """
                SELECT id, event_id, role, text, created_at
                FROM chat_log
                WHERE id > (
                    SELECT COALESCE(MAX(id), 0) - ? FROM chat_log
                )
                ORDER BY id ASC
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "event_id": r[1],
                "role": r[2],
                "text": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    def store_embedding(
        self, text: str, metadata: Optional[dict[str, Any]] = None
    ) -> int:
        embedding = self._embedder(text)
        with self._lock:
            conn = self.connect()
            cur = conn.execute(
                "INSERT INTO memories (text, metadata) VALUES (?, ?)",
                (text, json.dumps(metadata or {})),
            )
            rowid = cur.lastrowid
            if self.vec_enabled:
                try:
                    conn.execute(
                        "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                        (rowid, self._serialize(embedding)),
                    )
                except self._db_error as exc:
                    logger.warning("vec0 insert failed: %s", exc)
            self._evict(conn)
            conn.commit()
            return int(rowid)

    def _evict(self, conn: Any) -> int:
        """Delete rows over ``max_rows``: oldest non-preference first."""
        over = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] - self.max_rows
        if over <= 0:
            return 0
        ids = [
            r[0]
            for r in conn.execute(
                """
                SELECT id FROM memories
                ORDER BY
                    CASE WHEN json_valid(metadata)
                         AND json_extract(metadata, '$.kind') = ?
                    THEN 1 ELSE 0 END,
                    id ASC
                LIMIT ?
                """,
                (PREFERENCE_KIND, over),
            ).fetchall()
        ]
        self._delete_ids(conn, ids)
        logger.info("Memory over cap (%d); evicted %d rows", self.max_rows, len(ids))
        return len(ids)

    def _delete_ids(self, conn: Any, ids: list[int]) -> None:
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM memories WHERE id IN ({marks})", chunk)
            if self.vec_enabled:
                conn.execute(f"DELETE FROM memory_vec WHERE rowid IN ({marks})", chunk)

    def count(self) -> int:
        with self._lock:
            return int(self.connect().execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def clear_memory(self) -> int:
        """Delete every stored memory (not the chat log). Returns rows removed."""
        with self._lock:
            conn = self.connect()
            n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conn.execute("DELETE FROM memories")
            if self.vec_enabled:
                conn.execute("DELETE FROM memory_vec")
            conn.commit()
        logger.info("Cleared %d memories", n)
        return int(n)

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        embedding = self._embedder(query)
        with self._lock:
            conn = self.connect()
            rows = None
            if self.vec_enabled:
                try:
                    rows = conn.execute(
                        """
                        SELECT memories.id, memories.text, memories.metadata, distance
                        FROM memory_vec
                        JOIN memories ON memories.id = memory_vec.rowid
                        WHERE embedding MATCH ? AND k = ?
                        ORDER BY distance
                        """,
                        (self._serialize(embedding), top_k),
                    ).fetchall()
                except self._db_error as exc:
                    self.vec_enabled = False
                    _warn_vec_unavailable(f"vec0 search failed: {exc}")
            if rows is None:
                rows = conn.execute(
                    "SELECT id, text, metadata, 0.0 FROM memories ORDER BY id DESC LIMIT ?",
                    (top_k,),
                ).fetchall()
        return [
            {
                "id": r[0],
                "text": r[1],
                "metadata": json.loads(r[2] or "{}"),
                "distance": r[3],
            }
            for r in rows
        ]


    # ---- high-level API ---------------------------------------------------

    def save_memory(
        self, text: str, metadata: Optional[dict[str, Any]] = None
    ) -> int:
        """Embed ``text`` and persist it; returns the row id."""
        return self.store_embedding(text, metadata=metadata)

    def recall_memory(self, query: str, top_k: int = 3) -> list[str]:
        """Return the text of the top-``k`` memories most similar to ``query``."""
        try:
            rows = self.search(query, top_k=top_k)
        except Exception:
            logger.exception("recall_memory failed for query=%r", query[:80])
            return []
        return [r["text"] for r in rows]

    @staticmethod
    def _serialize(vec: Iterable[float]) -> bytes:
        data = list(vec)
        return struct.pack(f"{len(data)}f", *data)


# ---------------------------------------------------------------------------
# Process-wide singleton plumbing
# ---------------------------------------------------------------------------

_default_memory: Optional[Memory] = None
_default_memory_lock = threading.Lock()


def set_default_memory(memory: Optional[Memory]) -> None:
    """Install (or clear) the process-wide default :class:`Memory` instance.

    ``main.py`` sets this during lifespan startup so that tools like
    ``remember_preference`` can access the same store as the orchestrator.
    Tests use it to inject an isolated in-memory database.
    """
    global _default_memory
    with _default_memory_lock:
        _default_memory = memory


def get_default_memory() -> Memory:
    """Return the shared :class:`Memory` instance, creating one if needed."""
    global _default_memory
    if _default_memory is not None:
        return _default_memory
    with _default_memory_lock:
        if _default_memory is None:
            _default_memory = Memory()
        return _default_memory


def save_memory(text: str, metadata: Optional[dict[str, Any]] = None) -> int:
    """Module-level convenience wrapper around the default memory."""
    return get_default_memory().save_memory(text, metadata=metadata)


def recall_memory(query: str, top_k: int = 3) -> list[str]:
    """Module-level convenience wrapper around the default memory."""
    return get_default_memory().recall_memory(query, top_k=top_k)


def clear_memory() -> int:
    """Module-level convenience wrapper around the default memory."""
    return get_default_memory().clear_memory()
