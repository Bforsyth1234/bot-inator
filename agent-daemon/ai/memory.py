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
    """Lazily instantiate and cache a SentenceTransformer model."""
    global _st_model
    if _st_model is not None:
        return _st_model
    with _st_lock:
        if _st_model is not None:
            return _st_model
        from sentence_transformers import SentenceTransformer  # type: ignore

        logger.info("Loading sentence-transformers model %s", model_name)
        _st_model = SentenceTransformer(model_name)
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


class Memory:
    """SQLite + sqlite-vec backed embedding store."""

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        embedder: Optional[Embedder] = None,
        dim: int = EMBED_DIM,
    ) -> None:
        self.db_path = str(db_path)
        self.dim = dim
        self._embedder: Embedder = embedder or default_embedder()
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        parent = Path(self.db_path).expanduser().parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            conn.enable_load_extension(True)
        except AttributeError:
            logger.warning(
                "sqlite3 built without load_extension support; vector search disabled"
            )
        else:
            try:
                import sqlite_vec  # type: ignore

                sqlite_vec.load(conn)
            except ImportError:
                logger.warning("sqlite-vec not installed; vector search disabled")
            finally:
                conn.enable_load_extension(False)
        self._init_schema(conn)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _init_schema(self, conn: sqlite3.Connection) -> None:
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
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0("
                f"embedding float[{self.dim}])"
            )
        except sqlite3.OperationalError as exc:
            logger.warning("Could not create vec0 table: %s", exc)
        conn.commit()

    def store_embedding(
        self, text: str, metadata: Optional[dict[str, Any]] = None
    ) -> int:
        conn = self.connect()
        embedding = self._embedder(text)
        cur = conn.execute(
            "INSERT INTO memories (text, metadata) VALUES (?, ?)",
            (text, json.dumps(metadata or {})),
        )
        rowid = cur.lastrowid
        try:
            conn.execute(
                "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
                (rowid, self._serialize(embedding)),
            )
        except sqlite3.OperationalError as exc:
            logger.warning("vec0 insert failed: %s", exc)
        conn.commit()
        return int(rowid)

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        conn = self.connect()
        embedding = self._embedder(query)
        try:
            rows = conn.execute(
                """
                SELECT memories.id, memories.text, memories.metadata, distance
                FROM memory_vec
                JOIN memories ON memories.id = memory_vec.rowid
                WHERE embedding MATCH ?
                ORDER BY distance
                LIMIT ?
                """,
                (self._serialize(embedding), top_k),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("vec0 search unavailable (%s); falling back to recency", exc)
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
