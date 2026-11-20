"""Local knowledge index (#134 part 1): search over the user's own data.

Chat (part 2, a later wave) searches this index before answering
questions about the user's life. Retrieval only: nothing here trains a
model (LIVING_SPEC §1 "Knowledge index", decision 1).

Sources (decision 4/5), each its own opt-in, ALL OFF by default:

* ``mail``          Mail.app messages (#41 reader: Envelope Index + .emlx)
* ``folders``       text-like files in folders the user picks
* ``chats``         past Chat turns (``chat_log`` in memory.db, plus
                    :func:`index_chat_turn` for part 2)
* ``meeting_notes`` transcripts in ``~/.bot-inator/meetings/*.txt`` (#86)

iMessages are NOT a source (v1); a chosen folder may not be inside
``~/Library/Messages`` or ``~/Library/Mail``.

Storage: ``~/.bot-inator/knowledge.db`` (``AGENT_KNOWLEDGE_DB`` overrides),
SQLite, 0600, ``secure_delete`` on, a separate file from memory.db
(decision 2). Settings (toggles, cloud toggles, folders, last indexed) live
in ``~/.bot-inator/knowledge_index.json`` (``AGENT_KNOWLEDGE_SETTINGS_PATH``),
0600, written atomically. Neither file exists until a source is turned on.

Search is hybrid: SQLite FTS5 (bm25) + sqlite-vec over MiniLM embeddings,
fused by reciprocal rank. Without the vec0 extension or a local MiniLM
copy it is FTS5 only. The embedder never downloads anything: it reuses the
model ``ai.memory`` already loaded, else loads a copy already in the local
Hugging Face cache with ``local_files_only``; otherwise no vectors.

NOTHING IN THIS MODULE MAKES A NETWORK CALL (invariant 7c, decision 3).
Cloud use of snippets is part 2's job: :func:`cloud_allowed` says whether a
source's cloud toggle is on (off by default), and ``search(for_cloud=True)``
returns only such sources.

Off means gone (decision 8): turning a source off (or removing a folder)
deletes its rows; :meth:`KnowledgeIndex.forget_everything` deletes the db
file and turns every source off.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

SOURCES: tuple[str, ...] = ("mail", "folders", "chats", "meeting_notes")
SOURCE_LABELS = {
    "mail": "Mail",
    "folders": "Folders you choose",
    "chats": "Past chats",
    "meeting_notes": "Meeting notes",
}

DATA_DIR = Path.home() / ".bot-inator"
ENV_DB = "AGENT_KNOWLEDGE_DB"
ENV_SETTINGS = "AGENT_KNOWLEDGE_SETTINGS_PATH"

EMBED_DIM = 384
CHUNK_CHARS = 800
CHUNK_OVERLAP = 120
MAX_ITEM_CHARS = 200_000
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_FOLDER = 20_000
MAX_MAIL_ITEMS = 5_000
MAX_CHAT_ROWS = 1_000
PASS_BUDGET = 500          # changed items written per source per pass
RRF_K = 60
# A vector hit counts only this close (L2 on unit vectors; 1.2 = cosine
# 0.28). Without it the nearest chunk always "matches", related or not.
MAX_VEC_DISTANCE = 1.2
SNIPPET_CHARS = 700

# Background passes (#119 pattern): only on AC power or while the user is away.
BACKGROUND_INTERVAL = 15 * 60.0
IDLE_SECONDS = 300.0

Embedder = Callable[[str], list[float]]


def default_db_path() -> Path:
    override = os.environ.get(ENV_DB, "").strip()
    return Path(override).expanduser() if override else DATA_DIR / "knowledge.db"


def default_settings_path() -> Path:
    override = os.environ.get(ENV_SETTINGS, "").strip()
    return Path(override).expanduser() if override else DATA_DIR / "knowledge_index.json"


# ---------------------------------------------------------------------------
# Items a reader hands the indexer
# ---------------------------------------------------------------------------


@dataclass
class SourceItem:
    """One email / file / chat turn / note. ``version`` changes when the
    content does (mtime+size, a hash ...); an unchanged version is skipped
    without loading. ``load`` returns ``{"text", ...}`` and may also set
    ``title`` / ``link`` / ``date``; it is called only for changed items."""

    item_id: str
    version: str
    title: str = ""
    link: str = ""
    date: str = ""
    text: Optional[str] = None
    load: Optional[Callable[[], Mapping[str, str]]] = None

    def content(self) -> "SourceItem":
        if self.text is None and self.load is not None:
            got = dict(self.load() or {})
            self.text = str(got.get("text") or "")
            for key in ("title", "link", "date"):
                if got.get(key):
                    setattr(self, key, str(got[key]))
        self.text = (self.text or "")[:MAX_ITEM_CHARS]
        return self


@dataclass
class Listing:
    """What a reader returns. ``complete``: every item of the source is in
    ``items`` (so items missing from it were deleted at the source and may
    be pruned). ``error``: shown in status (e.g. no Full Disk Access)."""

    items: Iterable[SourceItem] = field(default_factory=list)
    complete: bool = True
    error: str = ""


Reader = Callable[[], Listing]


# ---------------------------------------------------------------------------
# Settings file (own file, not user_settings)
# ---------------------------------------------------------------------------


def _blank_settings() -> dict[str, Any]:
    return {
        "version": 1,
        "sources": {s: {"enabled": False, "cloud": False} for s in SOURCES},
        "folders": [],
        "last_indexed": {},
        "last_error": {},
        # #134 part 2: Chat turns that used retrieved snippets. Their reply
        # (which may quote another source) is never indexed as chat content.
        "reply_kept_out": [],
    }


class _Settings:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.data = self._read()

    def _read(self) -> dict[str, Any]:
        data = _blank_settings()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return data
        if not isinstance(raw, dict):
            return data
        for s in SOURCES:  # unknown sources (e.g. "imessage") are ignored
            got = (raw.get("sources") or {}).get(s) or {}
            data["sources"][s] = {"enabled": got.get("enabled") is True,
                                  "cloud": got.get("cloud") is True}
        data["folders"] = [str(f) for f in raw.get("folders") or [] if isinstance(f, str)]
        for key in ("last_indexed", "last_error"):
            got = raw.get(key) or {}
            data[key] = {s: got[s] for s in SOURCES if s in got}
        data["reply_kept_out"] = [str(e) for e in raw.get("reply_kept_out") or []
                                  if isinstance(e, str)][-MAX_CHAT_ROWS:]
        return data

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)

    def enabled(self, source: str) -> bool:
        return bool(self.data["sources"].get(source, {}).get("enabled"))

    def cloud(self, source: str) -> bool:
        return bool(self.data["sources"].get(source, {}).get("cloud"))


# ---------------------------------------------------------------------------
# Chunking, query words, embeddings
# ---------------------------------------------------------------------------


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split into ~``size``-char chunks, breaking at a paragraph, line or
    sentence end where possible, with ``overlap`` chars carried over."""
    text = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            window = text[start:end]
            for sep in ("\n\n", "\n", ". ", "? ", "! ", " "):
                cut = window.rfind(sep)
                if cut > size // 2:
                    end = start + cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


_STOPWORDS = frozenset(
    "a an and are as at be by did do does for from had has have he her his how i in is "
    "it its me my of on or our she so that the their them they this to was we were what "
    "when where which who why will with you your about said say".split()
)


def fts_query(query: str) -> str:
    """A safe FTS5 MATCH string: quoted words joined by OR (no operators
    from the user's text reach FTS5)."""
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 1]
    keep = [w for w in words if w not in _STOPWORDS] or words
    seen: dict[str, None] = {}
    for w in keep[:24]:
        seen.setdefault(w, None)
    return " OR ".join(f'"{w}"' for w in seen)


def _hf_cache_dirs() -> list[Path]:
    dirs = []
    for key in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(key):
            dirs.append(Path(os.environ[key]).expanduser())
    if os.environ.get("HF_HOME"):
        dirs.append(Path(os.environ["HF_HOME"]).expanduser() / "hub")
    dirs.append(Path.home() / ".cache" / "huggingface" / "hub")
    return dirs


def local_minilm_dir(model: str = "sentence-transformers/all-MiniLM-L6-v2") -> Optional[Path]:
    """A complete local snapshot of MiniLM in the Hugging Face cache, or
    None. Reads the disk only; never asks the Hub."""
    folder = "models--" + model.replace("/", "--")
    for cache in _hf_cache_dirs():
        snaps = cache / folder / "snapshots"
        try:
            candidates = sorted(snaps.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        for snap in candidates:
            if (snap / "modules.json").exists() and (
                (snap / "model.safetensors").exists() or (snap / "pytorch_model.bin").exists()
            ):
                return snap
    return None


_local_model: Any = None
_local_model_lock = threading.Lock()


def local_embedder() -> Optional[Embedder]:
    """MiniLM without any download: ``ai.memory``'s model when it is already
    loaded, else a copy already on disk (``local_files_only``). None when
    neither exists, or sentence-transformers is not installed (the index is
    then FTS5 only; the hash fallback in ai.memory has no meaning to search)."""
    try:
        from ai import memory

        if getattr(memory, "_st_model", None) is not None:
            return memory._st_embed
    except Exception:  # pragma: no cover - memory import failure
        logger.debug("ai.memory unavailable", exc_info=True)
    snap = local_minilm_dir()
    if snap is None:
        return None
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return None

    def _embed(text: str) -> list[float]:
        global _local_model
        with _local_model_lock:
            if _local_model is None:
                from sentence_transformers import SentenceTransformer

                device = os.environ.get("AGENT_EMBED_DEVICE", "cpu").strip() or "cpu"
                _local_model = SentenceTransformer(str(snap), device=device, local_files_only=True)
            model = _local_model
        return [float(x) for x in model.encode(text, normalize_embeddings=True)]

    return _embed


def _serialize(vec: Iterable[float]) -> bytes:
    data = list(vec)
    return struct.pack(f"{len(data)}f", *data)


def _try_load_vec(conn: Any) -> Optional[str]:
    """Load sqlite-vec (ai.memory's loader). None on success, else why not."""
    from ai.memory import _try_load_vec as load

    return load(conn)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    date TEXT NOT NULL DEFAULT '',
    indexed_at REAL NOT NULL,
    PRIMARY KEY (source, item_id)
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    item_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    text TEXT NOT NULL,
    has_vec INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunks_item ON chunks(source, item_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, body, tokenize = 'porter unicode61'
);
"""


class SourceError(ValueError):
    """Unknown source, or a request the source's state does not allow."""


class KnowledgeIndex:
    """The index. Thread-safe: one connection, every access under ``_lock``
    (the background pass runs in a worker thread, search on another)."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        settings_path: Optional[Path] = None,
        *,
        embedder: Any = "auto",
        readers: Optional[Mapping[str, Reader]] = None,
        mail_root: Optional[Path] = None,
        notes_dir: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.settings = _Settings(Path(settings_path) if settings_path is not None
                                  else default_settings_path())
        self._embedder_arg = embedder
        self._embedder: Optional[Embedder] = None
        self._embedder_resolved = False
        self.clock = clock
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        self.vec_loaded = False
        self.vec_reason = ""
        self._mail_root = mail_root
        self._notes_dir = notes_dir
        self.readers: dict[str, Reader] = {
            "mail": lambda: mail_listing(self._mail_root),
            "folders": lambda: folder_listing(self.folders()),
            "chats": lambda: chat_listing(kept_out=self.settings.data.get("reply_kept_out", ())),
            "meeting_notes": lambda: notes_listing(self._notes_dir),
        }
        self.readers.update(readers or {})
        self.running = False
        self.last_pass: dict[str, Any] = {}

    # ---- storage ---------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is not None:
                return self._db
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if not self.db_path.exists():
                os.close(os.open(self.db_path, os.O_CREAT | os.O_WRONLY, 0o600))
            os.chmod(self.db_path, 0o600)
            db = sqlite3.connect(str(self.db_path), check_same_thread=False)
            db.execute("PRAGMA secure_delete = ON")
            db.executescript(_SCHEMA)
            try:  # FTS5 overwrites deleted terms too (SQLite 3.42+)
                db.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('secure-delete', 1)")
            except sqlite3.Error:
                pass
            reason = _try_load_vec(db)
            self.vec_loaded = reason is None
            self.vec_reason = reason or ""
            if self.vec_loaded:
                try:
                    db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0("
                               f"embedding float[{EMBED_DIM}])")
                except sqlite3.Error as exc:
                    self.vec_loaded, self.vec_reason = False, f"vec0 table: {exc}"
            if not self.vec_loaded:
                logger.info("knowledge index: keyword search only (%s)", self.vec_reason)
            db.commit()
            self._db = db
            return db

    def _exists(self) -> bool:
        return self._db is not None or self.db_path.exists()

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def embedder(self) -> Optional[Embedder]:
        if not self._embedder_resolved:
            arg = self._embedder_arg
            self._embedder = local_embedder() if arg == "auto" else arg
            self._embedder_resolved = True
        return self._embedder

    def vector_enabled(self) -> bool:
        """Vectors in use: vec0 loaded and a local embedder present."""
        if self._exists():
            self._conn()
        return self.vec_loaded and self.embedder() is not None

    def _embed(self, text: str) -> Optional[bytes]:
        emb = self.embedder()
        if emb is None or not self.vec_loaded:
            return None
        try:
            vec = emb(text)
        except Exception:
            logger.exception("knowledge index: embedding failed; keyword only for this chunk")
            return None
        if len(vec) != EMBED_DIM:
            logger.warning("knowledge index: embedder gave %d dims, want %d", len(vec), EMBED_DIM)
            return None
        return _serialize(vec)

    # ---- settings --------------------------------------------------------

    @staticmethod
    def _check(source: str) -> None:
        if source not in SOURCES:
            raise SourceError(f"unknown source {source!r}; sources are {', '.join(SOURCES)}")

    def enabled(self, source: str) -> bool:
        return source in SOURCES and self.settings.enabled(source)

    def cloud_allowed(self, source: str) -> bool:
        """True only when the source is on AND its cloud toggle is on."""
        return self.enabled(source) and self.settings.cloud(source)

    def set_source_enabled(self, source: str, on: bool) -> int:
        """Turn a source on or off. Off deletes its rows and its cloud
        toggle; returns the number of items deleted."""
        self._check(source)
        with self._lock:
            entry = self.settings.data["sources"][source]
            entry["enabled"] = bool(on)
            deleted = 0
            if not on:
                entry["cloud"] = False
                self.settings.data["last_indexed"].pop(source, None)
                self.settings.data["last_error"].pop(source, None)
                deleted = self._delete_where("source = ?", (source,))
            self.settings.save()
        return deleted

    def set_cloud_allowed(self, source: str, on: bool) -> None:
        self._check(source)
        with self._lock:
            if on and not self.settings.enabled(source):
                raise SourceError(f"turn {source} on before allowing it in the cloud")
            self.settings.data["sources"][source]["cloud"] = bool(on)
            self.settings.save()

    def folders(self) -> list[str]:
        return list(self.settings.data["folders"])

    def set_folders(self, folders: Iterable[str]) -> int:
        """Replace the folder list (each an existing directory, not inside
        Mail, Messages or the app's own data). Folders removed lose their
        rows; returns the number of items deleted."""
        clean: list[str] = []
        for raw in folders:
            path = check_folder(raw)
            if path not in clean:
                clean.append(path)
        with self._lock:
            removed = [f for f in self.folders() if f not in clean]
            self.settings.data["folders"] = clean
            deleted = 0
            for folder in removed:
                prefix = folder.rstrip(os.sep) + os.sep
                deleted += self._delete_where(
                    "source = 'folders' AND substr(item_id, 1, ?) = ?", (len(prefix), prefix))
            self.settings.save()
        return deleted

    # ---- deletion ----------------------------------------------------------

    def _delete_where(self, where: str, params: tuple) -> int:
        """Delete items (and their chunks, FTS rows and vectors) matching
        ``where`` on the items table. Returns items deleted."""
        with self._lock:
            if not self._exists():
                return 0
            db = self._conn()
            keys = db.execute(f"SELECT source, item_id FROM items WHERE {where}", params).fetchall()
            for source, item_id in keys:
                self._delete_item(db, source, item_id)
            db.commit()
            return len(keys)

    def _delete_item(self, db: sqlite3.Connection, source: str, item_id: str) -> None:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM chunks WHERE source = ? AND item_id = ?", (source, item_id))]
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            marks = ",".join("?" * len(part))
            db.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({marks})", part)
            if self.vec_loaded:
                db.execute(f"DELETE FROM chunk_vec WHERE rowid IN ({marks})", part)
            db.execute(f"DELETE FROM chunks WHERE id IN ({marks})", part)
        db.execute("DELETE FROM items WHERE source = ? AND item_id = ?", (source, item_id))

    def forget_everything(self) -> int:
        """Delete the whole index (the db file) and turn every source off,
        folders and cloud toggles included. Returns items deleted."""
        with self._lock:
            count = 0
            if self._exists():
                try:
                    count = int(self._conn().execute("SELECT COUNT(*) FROM items").fetchone()[0])
                except sqlite3.Error:
                    count = 0
            self.close()
            for suffix in ("", "-wal", "-shm", "-journal"):
                try:
                    Path(str(self.db_path) + suffix).unlink()
                except FileNotFoundError:
                    pass
            kept_out = list(self.settings.data.get("reply_kept_out") or [])
            self.settings.data = _blank_settings()
            # Ids only (no content): a later "chats" on must still leave
            # those replies out (#134 part 2).
            self.settings.data["reply_kept_out"] = kept_out
            self.settings.save()
        return count

    # ---- indexing ----------------------------------------------------------

    def _version_of(self, source: str, item_id: str) -> Optional[str]:
        with self._lock:
            if not self._exists():
                return None
            row = self._conn().execute(
                "SELECT version FROM items WHERE source = ? AND item_id = ?", (source, item_id)
            ).fetchone()
        return row[0] if row else None

    def _write_item(self, source: str, item: SourceItem) -> bool:
        """(Re)write one loaded item. False when the source went off meanwhile."""
        chunks = chunk_text(item.text or "")
        # Embed outside the lock (slow); write under it.
        vecs = [self._embed(f"{item.title}\n{c}") for c in chunks] if chunks else []
        with self._lock:
            if not self.settings.enabled(source):
                return False
            db = self._conn()
            self._delete_item(db, source, item.item_id)
            db.execute(
                "INSERT INTO items (source, item_id, version, title, link, date, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, item.item_id, item.version, item.title[:300], item.link, item.date,
                 self.clock()),
            )
            for ord_, (text, vec) in enumerate(zip(chunks, vecs)):
                cur = db.execute(
                    "INSERT INTO chunks (source, item_id, ord, text, has_vec) VALUES (?, ?, ?, ?, ?)",
                    (source, item.item_id, ord_, text, 1 if vec is not None and self.vec_loaded else 0),
                )
                rowid = cur.lastrowid
                db.execute("INSERT INTO chunks_fts (rowid, title, body) VALUES (?, ?, ?)",
                           (rowid, item.title, text))
                if vec is not None and self.vec_loaded:
                    db.execute("INSERT INTO chunk_vec (rowid, embedding) VALUES (?, ?)", (rowid, vec))
            db.commit()
        return True

    def index_source(
        self,
        source: str,
        *,
        budget: int = PASS_BUDGET,
        keep_going: Optional[Callable[[], bool]] = None,
    ) -> dict[str, Any]:
        """One incremental pass over a source: changed items are (re)indexed,
        unchanged ones skipped without loading; on a complete listing,
        items gone at the source are pruned. Nothing happens while the
        source is off."""
        self._check(source)
        result = {"source": source, "indexed": 0, "skipped": 0, "removed": 0,
                  "failed": 0, "error": "", "finished": False}
        if not self.enabled(source):
            result["error"] = "off"
            return result
        try:
            listing = self.readers[source]()
        except Exception as exc:
            logger.exception("knowledge index: %s reader failed", source)
            listing = Listing([], complete=False, error=f"could not read: {exc}")
        seen: set[str] = set()
        finished = True
        for item in listing.items:
            if not self.enabled(source):
                finished = False
                break
            seen.add(item.item_id)
            if self._version_of(source, item.item_id) == item.version:
                result["skipped"] += 1
                continue
            if result["indexed"] >= budget or (keep_going is not None and not keep_going()):
                finished = False
                break
            try:
                item.content()
            except Exception:
                logger.warning("knowledge index: could not load %s item", source, exc_info=True)
                result["failed"] += 1
                continue
            if not self._write_item(source, item):
                finished = False
                break
            result["indexed"] += 1
        if finished and listing.complete and not listing.error and self.enabled(source):
            with self._lock:
                if self._exists():
                    db = self._conn()
                    stale = [r[0] for r in db.execute(
                        "SELECT item_id FROM items WHERE source = ?", (source,)) if r[0] not in seen]
                    for item_id in stale:
                        self._delete_item(db, source, item_id)
                    db.commit()
                    result["removed"] = len(stale)
        result["finished"] = finished
        result["error"] = listing.error
        with self._lock:
            if self.enabled(source):
                if finished:
                    self.settings.data["last_indexed"][source] = self.clock()
                if listing.error:
                    self.settings.data["last_error"][source] = listing.error
                else:
                    self.settings.data["last_error"].pop(source, None)
                self.settings.save()
        return result

    def backfill_vectors(self, limit: int = 2000) -> int:
        """Embed chunks stored while no embedder was available."""
        if not self._exists() or not self.vector_enabled():
            return 0
        with self._lock:
            rows = self._conn().execute(
                "SELECT c.id, i.title, c.text FROM chunks c JOIN items i "
                "ON i.source = c.source AND i.item_id = c.item_id WHERE c.has_vec = 0 LIMIT ?",
                (limit,)).fetchall()
        done = 0
        for rowid, title, text in rows:
            vec = self._embed(f"{title}\n{text}")
            if vec is None:
                break
            with self._lock:
                db = self._conn()
                if db.execute("SELECT 1 FROM chunks WHERE id = ?", (rowid,)).fetchone() is None:
                    continue
                db.execute("DELETE FROM chunk_vec WHERE rowid = ?", (rowid,))
                db.execute("INSERT INTO chunk_vec (rowid, embedding) VALUES (?, ?)", (rowid, vec))
                db.execute("UPDATE chunks SET has_vec = 1 WHERE id = ?", (rowid,))
                db.commit()
            done += 1
        return done

    def run_pass(self, keep_going: Optional[Callable[[], bool]] = None) -> dict[str, Any]:
        """Index every source that is on. ``keep_going`` (background passes)
        is checked before each changed item; a False stops the pass."""
        self.running = True
        started = self.clock()
        try:
            results = {s: self.index_source(s, keep_going=keep_going)
                       for s in SOURCES if self.enabled(s)}
            backfilled = self.backfill_vectors() if results else 0
        finally:
            self.running = False
        self.last_pass = {"started_at": started, "finished_at": self.clock(),
                          "sources": results, "backfilled": backfilled}
        return self.last_pass

    def index_chat_turn(self, event_id: str, user_text: str, reply_text: str = "",
                        *, date: str = "", keep_reply: bool = True) -> bool:
        """Index one Chat turn now (part 2 calls this after a reply). No-op
        (False) while the ``chats`` source is off. Same item id and version
        as :func:`chat_listing`, so the background pass skips it.

        ``keep_reply=False`` (a turn that used retrieved snippets): the reply
        is never indexed, now or by a later pass, since it may quote another
        source whose own toggles must hold. Remembered (ids only) even while
        ``chats`` is off, so turning it on later keeps the reply out too."""
        eid = str(event_id or "").strip()
        if not eid:
            return False
        if not keep_reply:
            with self._lock:
                kept = self.settings.data.setdefault("reply_kept_out", [])
                if eid not in kept:
                    kept.append(eid)
                    del kept[:-MAX_CHAT_ROWS]
                    if self.settings.path.exists() or any(self.enabled(s) for s in SOURCES):
                        self.settings.save()
        if not self.enabled("chats"):
            return False
        item = chat_item(eid, [("user", user_text or ""), ("assistant", reply_text or "")],
                         date=date or _now_iso(), keep_out_reply=not keep_reply)
        if keep_reply and self._version_of("chats", item.item_id) == item.version:
            return True
        return self._write_item("chats", item.content())

    # ---- search --------------------------------------------------------------

    def search(
        self,
        query: str,
        top_k: int = 5,
        *,
        sources: Optional[Iterable[str]] = None,
        for_cloud: bool = False,
    ) -> list[dict[str, Any]]:
        """Top-``top_k`` snippets for ``query``, best first, one per item.

        Only sources that are on; ``sources`` narrows further; ``for_cloud``
        keeps only sources whose cloud toggle is on. Each snippet:
        ``{source, item_id, title, link, date, text, score, match}`` where
        ``match`` is ``keyword`` / ``meaning`` / ``both``."""
        allowed = [s for s in SOURCES if self.enabled(s)]
        if sources is not None:
            wanted = set(sources)
            allowed = [s for s in allowed if s in wanted]
        if for_cloud:
            allowed = [s for s in allowed if self.cloud_allowed(s)]
        top_k = max(1, min(int(top_k), 50))
        if not allowed or not (query or "").strip() or not self._exists():
            return []
        pool = top_k * 8
        marks = ",".join("?" * len(allowed))
        ranks: dict[int, dict[str, int]] = {}
        match = fts_query(query)
        qvec = self._embed(query) if self.vector_enabled() else None
        with self._lock:
            db = self._conn()
            if match:
                try:
                    rows = db.execute(
                        f"SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                        f"WHERE chunks_fts MATCH ? AND c.source IN ({marks}) "
                        f"ORDER BY bm25(chunks_fts, 2.0, 1.0) LIMIT ?",
                        (match, *allowed, pool)).fetchall()
                except sqlite3.Error:
                    logger.warning("knowledge index: keyword search failed", exc_info=True)
                    rows = []
                for rank, (cid,) in enumerate(rows):
                    ranks.setdefault(cid, {})["keyword"] = rank
            if qvec is not None:
                try:
                    rows = db.execute(
                        "SELECT rowid FROM chunk_vec WHERE embedding MATCH ? AND k = ? "
                        "AND distance <= ? ORDER BY distance",
                        (qvec, pool * 2, MAX_VEC_DISTANCE)).fetchall()
                except sqlite3.Error:
                    logger.warning("knowledge index: vector search failed", exc_info=True)
                    rows = []
                ids = [r[0] for r in rows]
                if ids:
                    ok = {r[0] for r in db.execute(
                        f"SELECT id FROM chunks WHERE id IN ({','.join('?' * len(ids))}) "
                        f"AND source IN ({marks})", (*ids, *allowed))}
                    rank = 0
                    for cid in ids:
                        if cid in ok:
                            ranks.setdefault(cid, {})["meaning"] = rank
                            rank += 1
            scored = sorted(
                ((sum(1.0 / (RRF_K + r + 1) for r in kinds.values()), cid, kinds)
                 for cid, kinds in ranks.items()),
                key=lambda t: (-t[0], t[1]))
            out: list[dict[str, Any]] = []
            seen_items: set[tuple[str, str]] = set()
            for score, cid, kinds in scored:
                row = db.execute(
                    "SELECT c.source, c.item_id, c.text, i.title, i.link, i.date FROM chunks c "
                    "JOIN items i ON i.source = c.source AND i.item_id = c.item_id WHERE c.id = ?",
                    (cid,)).fetchone()
                if row is None or (row[0], row[1]) in seen_items:
                    continue
                seen_items.add((row[0], row[1]))
                out.append({
                    "source": row[0], "item_id": row[1], "title": row[3], "link": row[4],
                    "date": row[5], "text": row[2][:SNIPPET_CHARS], "score": round(score, 6),
                    "match": "both" if len(kinds) == 2 else next(iter(kinds)),
                })
                if len(out) >= top_k:
                    break
        return out

    # ---- status ----------------------------------------------------------------

    def counts(self) -> dict[str, dict[str, int]]:
        out = {s: {"items": 0, "chunks": 0} for s in SOURCES}
        if not self._exists():
            return out
        with self._lock:
            db = self._conn()
            for source, n in db.execute("SELECT source, COUNT(*) FROM items GROUP BY source"):
                if source in out:
                    out[source]["items"] = int(n)
            for source, n in db.execute("SELECT source, COUNT(*) FROM chunks GROUP BY source"):
                if source in out:
                    out[source]["chunks"] = int(n)
        return out

    def status(self) -> dict[str, Any]:
        counts = self.counts()
        data = self.settings.data
        size = 0
        if self.db_path.exists():
            try:
                size = self.db_path.stat().st_size
            except OSError:
                size = 0
        return {
            "sources": [
                {
                    "source": s, "label": SOURCE_LABELS[s],
                    "enabled": self.enabled(s), "cloud": self.cloud_allowed(s),
                    "items": counts[s]["items"], "chunks": counts[s]["chunks"],
                    "last_indexed": data["last_indexed"].get(s),
                    "error": data["last_error"].get(s, ""),
                }
                for s in SOURCES
            ],
            "folders": self.folders(),
            "running": self.running,
            "vector": self.vector_enabled() if self._exists() else (
                self.embedder() is not None),
            "db_bytes": size,
            "last_pass": self.last_pass,
        }


# ---------------------------------------------------------------------------
# Readers (read-only; tests pass fakes)
# ---------------------------------------------------------------------------

TEXT_EXTS = frozenset({".txt", ".md", ".markdown", ".text", ".csv", ".tsv", ".json", ".yaml",
                       ".yml", ".log", ".org", ".rst", ".tex", ".xml"})
HTML_EXTS = frozenset({".html", ".htm"})
TEXTUTIL_EXTS = frozenset({".rtf", ".doc", ".docx", ".odt", ".webarchive"})
PDF_EXTS = frozenset({".pdf"})
FILE_EXTS = TEXT_EXTS | HTML_EXTS | TEXTUTIL_EXTS | PDF_EXTS


def _forbidden_roots() -> list[Path]:
    home = Path.home()
    return [home / "Library" / "Messages", home / "Library" / "Mail", DATA_DIR,
            default_db_path().parent]


def check_folder(raw: Any) -> str:
    """Normalise a chosen folder or raise :class:`SourceError`. Refused:
    not a directory, ``/`` or the home folder itself, and anything inside
    Messages (iMessages are not a v1 source), Mail (it has its own toggle)
    or the app's own data."""
    if not isinstance(raw, str) or not raw.strip():
        raise SourceError("folder must be a path")
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise SourceError(f"folder must be an absolute path: {raw}")
    try:
        path = path.resolve()
    except OSError as exc:
        raise SourceError(f"can't open {raw}: {exc}") from exc
    if not path.is_dir():
        raise SourceError(f"not a folder: {raw}")
    if path == Path("/") or path == Path.home().resolve():
        raise SourceError("pick a narrower folder than the whole disk or home folder")
    for root in _forbidden_roots():
        try:
            root = root.resolve()
        except OSError:
            continue
        if path == root or root in path.parents or path in root.parents:
            raise SourceError(f"{path} can't be indexed (it holds Mail, Messages or the app's own data)")
    return str(path)


def _file_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return path.read_text("utf-8", errors="replace")
    if ext in HTML_EXTS:
        from tools.read_email import html_to_text

        return html_to_text(path.read_text("utf-8", errors="replace"))
    if ext in TEXTUTIL_EXTS:  # macOS's own converter, local only
        out = subprocess.run(["/usr/bin/textutil", "-convert", "txt", "-stdout", str(path)],
                             capture_output=True, timeout=30)
        return out.stdout.decode("utf-8", errors="replace") if out.returncode == 0 else ""
    if ext in PDF_EXTS:  # PDFKit (pyobjc-framework-Quartz), local only
        from Foundation import NSURL  # type: ignore
        from Quartz import PDFDocument  # type: ignore

        doc = PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(str(path)))
        return str(doc.string() or "") if doc is not None else ""
    return ""


def folder_listing(folders: Iterable[str]) -> Listing:
    """Text-like files under each folder (hidden files and folders, symlinks
    and files over :data:`MAX_FILE_BYTES` skipped)."""
    items: list[SourceItem] = []
    errors: list[str] = []
    complete = True
    for folder in folders:
        root = Path(folder)
        if not root.is_dir():
            errors.append(f"{folder} is missing")
            complete = False
            continue
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith(".") or Path(name).suffix.lower() not in FILE_EXTS:
                    continue
                path = Path(dirpath) / name
                try:
                    if path.is_symlink():
                        continue
                    st = path.stat()
                except OSError:
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue
                count += 1
                if count > MAX_FILES_PER_FOLDER:
                    complete = False
                    break
                items.append(SourceItem(
                    item_id=str(path), version=f"{st.st_mtime_ns}:{st.st_size}",
                    title=name, link=path.as_uri(),
                    date=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    load=(lambda p=path: {"text": _file_text(p)}),
                ))
            if count > MAX_FILES_PER_FOLDER:
                break
    return Listing(items, complete=complete, error="; ".join(errors))


def _emlx_paths(root: Path) -> dict[int, Path]:
    """``{ROWID: path}`` for every ``<ROWID>.emlx`` / ``.partial.emlx``
    under ``root/V*`` (one walk; full files beat partial ones)."""
    found: dict[int, Path] = {}
    try:
        versions = [p for p in root.iterdir() if p.is_dir() and p.name.startswith("V")
                    and p.name[1:].isdigit()]
    except OSError:
        return found
    for version in versions:
        for dirpath, dirnames, filenames in os.walk(version):
            if dirpath == str(version):
                dirnames[:] = [d for d in dirnames if d != "MailData"]
            if os.path.basename(dirpath) != "Messages":
                continue
            for name in filenames:
                m = re.match(r"^(\d+)(\.partial)?\.emlx$", name)
                if not m:
                    continue
                rowid = int(m.group(1))
                if rowid not in found or (m.group(2) is None and found[rowid].name.endswith(".partial.emlx")):
                    found[rowid] = Path(dirpath) / name
    return found


def _load_email(path: Path, sender: str, subject: str, date: str) -> dict[str, str]:
    from tools.read_email import message_body, parse_emlx

    msg = parse_emlx(path.read_bytes())
    body = message_body(msg)
    header_id = str(msg.get("Message-ID", "") or "").strip().strip("<>")
    link = f"message://%3C{quote(header_id, safe='@.-_+')}%3E" if header_id else ""
    text = f"From: {sender}\nSubject: {subject}\nDate: {date}\n\n{body}"
    return {"text": text, "link": link}


def mail_listing(mail_root: Optional[Path] = None, max_items: int = MAX_MAIL_ITEMS) -> Listing:
    """The newest ``max_items`` received emails (Sent, Drafts, Junk, Trash,
    deleted mail and Gmail duplicates skipped, as in #103). ``item_id`` is
    the Mail ROWID that ``read_email(message_id)`` takes; ``link`` is a
    ``message://`` URL that opens the mail in Mail. Needs Full Disk Access."""
    from events import mail_watcher as mw
    from tools import list_recent_emails as lre
    from tools.read_email import mail_root as default_root

    root = Path(mail_root).expanduser() if mail_root is not None else default_root()
    conn, error = lre._open(root)
    if conn is None:
        return Listing([], complete=False, error=error)
    entries: list[tuple] = []
    complete = True
    try:
        columns = mw.table_columns(conn)
        problems = mw.schema_problems(columns)
        if problems:
            return Listing([], complete=False, error="Mail's index has an unexpected layout")
        sql = lre._build_sql(columns)
        seen_global: set[Any] = set()
        offset = 0
        while True:
            rows = conn.execute(sql, (500, offset)).fetchall()
            if not rows:
                break
            offset += len(rows)
            for rowid, subject, address, name, date_received, url, global_id, deleted in rows:
                if deleted not in (None, 0, "0") or lre.is_skipped_mailbox(url):
                    continue
                if global_id not in (None, 0):
                    if global_id in seen_global:
                        continue
                    seen_global.add(global_id)
                entries.append((int(rowid), subject or "", mw._sender(address, name),
                                mw._iso_date(date_received)))
            if len(entries) >= max_items:
                complete = False
                entries = entries[:max_items]
                break
    except sqlite3.Error as exc:
        return Listing([], complete=False, error=f"could not read Mail's index: {exc}")
    finally:
        conn.close()
    paths = _emlx_paths(root)
    items = []
    for rowid, subject, sender, date in entries:
        path = paths.get(rowid)
        if path is None:  # not downloaded: nothing to index yet
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        items.append(SourceItem(
            item_id=str(rowid), version=f"{path.name}:{size}", title=subject or "(no subject)",
            date=date,
            load=(lambda p=path, s=sender, t=subject, d=date: _load_email(p, s, t, d)),
        ))
    return Listing(items, complete=complete)


def chat_item(event_id: str, turns: Iterable[tuple[str, str]], date: str = "",
              *, keep_out_reply: bool = False) -> SourceItem:
    """One Chat turn (the user's message and the reply) as an item.

    ``keep_out_reply`` (#134 part 2, a turn that used retrieved snippets):
    the indexed text is the user's lines only; ``version`` is still over the
    whole turn, so :func:`chat_listing` and :meth:`KnowledgeIndex.index_chat_turn`
    agree and the background pass skips it."""
    lines, first_user, user_lines = [], "", []
    for role, text in turns:
        text = (text or "").strip()
        if not text:
            continue
        if role == "user" and not first_user:
            first_user = text
        lines.append(f"{'You' if role == 'user' else 'Assistant'}: {text}")
        if role == "user":
            user_lines.append(lines[-1])
    body = "\n".join(lines)
    title = "Chat: " + (first_user[:60] + ("…" if len(first_user) > 60 else "") if first_user else event_id)
    version = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    text = "\n".join(user_lines) if keep_out_reply else body
    return SourceItem(item_id=event_id, version=version, title=title, date=date, text=text)


def chat_listing(memory: Any = None, limit: int = MAX_CHAT_ROWS,
                 kept_out: Iterable[str] = ()) -> Listing:
    """Past Chat turns from memory.db's ``chat_log`` (read-only), grouped by
    ``event_id``. Turns in ``kept_out`` are indexed without their reply."""
    if memory is None:
        from ai.memory import get_default_memory

        memory = get_default_memory()
    rows = memory.get_chat_log(limit)
    groups: dict[str, list[tuple[str, str]]] = {}
    dates: dict[str, str] = {}
    for row in rows:
        eid = str(row.get("event_id") or "")
        if not eid:
            continue
        groups.setdefault(eid, []).append((str(row.get("role") or ""), str(row.get("text") or "")))
        dates.setdefault(eid, str(row.get("created_at") or ""))
    skip = set(kept_out or ())
    items = [chat_item(eid, turns, dates.get(eid, ""), keep_out_reply=eid in skip)
             for eid, turns in groups.items()]
    # A capped log may start mid-history: never prune from it.
    return Listing(items, complete=len(rows) < limit)


def notes_listing(notes_dir: Optional[Path] = None) -> Listing:
    """Meeting transcripts (``<meeting id>.txt``, #86). ``item_id`` is the
    meeting id; ``link`` the transcript's file URL."""
    if notes_dir is None:
        from events.meeting_notes import notes_dir as default_dir

        notes_dir = default_dir(create=False)
    root = Path(notes_dir)
    if not root.is_dir():
        return Listing([], complete=True)
    items = []
    for path in sorted(root.glob("*.txt")):
        try:
            if path.is_symlink():
                continue
            st = path.stat()
        except OSError:
            continue
        when = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        items.append(SourceItem(
            item_id=path.stem, version=f"{st.st_mtime_ns}:{st.st_size}",
            title=f"Meeting notes {when.date().isoformat()}", link=path.as_uri(),
            date=when.isoformat(),
            load=(lambda p=path: {"text": p.read_text("utf-8", errors="replace")}),
        ))
    return Listing(items, complete=True)


# ---------------------------------------------------------------------------
# Background indexer: AC power or idle only (#119 guards); "Index now" any time
# ---------------------------------------------------------------------------


def background_blocked(power: Callable[[], Optional[str]], idle: Callable[[], Optional[float]],
                       idle_seconds: float = IDLE_SECONDS) -> Optional[str]:
    """None when a background pass may run: on AC power, or the user has
    been away ``idle_seconds``. Else the reason (shown in status)."""
    try:
        if power() == "ac":
            return None
    except Exception:
        pass
    try:
        away = idle()
    except Exception:
        away = None
    if away is not None and away >= idle_seconds:
        return None
    return "waiting for AC power or until you're away"


class KnowledgeIndexer:
    """Runs :meth:`KnowledgeIndex.run_pass` in a worker thread every
    ``interval`` seconds while any source is on and the guard allows;
    :meth:`index_now` runs one pass at once, guard or not."""

    def __init__(self, index: KnowledgeIndex, *, interval: float = BACKGROUND_INTERVAL,
                 power: Optional[Callable[[], Optional[str]]] = None,
                 idle: Optional[Callable[[], Optional[float]]] = None) -> None:
        from ai import personal_training as pt  # stdlib-only module; same guards as #119

        self.index = index
        self.interval = interval
        self.power = power or pt.power_source
        self.idle = idle or pt.input_idle_seconds
        self.blocked_reason: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._pass: Optional[asyncio.Task] = None
        self._stop = False

    def _guard(self) -> bool:
        self.blocked_reason = background_blocked(self.power, self.idle)
        return self.blocked_reason is None

    async def start(self) -> None:
        if self._task is None:
            self._stop = False
            self._task = asyncio.create_task(self._loop(), name="knowledge-indexer")

    async def stop(self) -> None:
        self._stop = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._pass is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._pass), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while not self._stop:
            if any(self.index.enabled(s) for s in SOURCES) and self._guard():
                await self._run(background=True)
            await asyncio.sleep(self.interval)

    async def _run(self, background: bool) -> dict[str, Any]:
        keep_going = (lambda: not self._stop and self._guard()) if background else (lambda: not self._stop)
        self._pass = asyncio.ensure_future(asyncio.to_thread(self.index.run_pass, keep_going))
        try:
            return await self._pass
        finally:
            self._pass = None

    @property
    def running(self) -> bool:
        return self._pass is not None

    async def index_now(self) -> bool:
        """Start a pass now (no guard). False if one is already running."""
        if self._pass is not None:
            return False
        asyncio.ensure_future(self._run(background=False))
        await asyncio.sleep(0)
        return True

    def status(self) -> dict[str, Any]:
        return {"running": self.running, "blocked_reason": self.blocked_reason,
                "background": self._task is not None}


# ---------------------------------------------------------------------------
# Process-wide default (part 2 / main.py)
# ---------------------------------------------------------------------------

_default: Optional[KnowledgeIndex] = None
_default_lock = threading.Lock()


def set_default_index(index: Optional[KnowledgeIndex]) -> None:
    global _default
    with _default_lock:
        _default = index


def get_default_index() -> KnowledgeIndex:
    global _default
    with _default_lock:
        if _default is None:
            _default = KnowledgeIndex()
        return _default


def search(query: str, top_k: int = 5, *, sources: Optional[Iterable[str]] = None,
           for_cloud: bool = False) -> list[dict[str, Any]]:
    """Module-level :meth:`KnowledgeIndex.search` on the default index."""
    return get_default_index().search(query, top_k, sources=sources, for_cloud=for_cloud)


def cloud_allowed(source: str) -> bool:
    return get_default_index().cloud_allowed(source)


def index_chat_turn(event_id: str, user_text: str, reply_text: str = "", *, date: str = "",
                    keep_reply: bool = True) -> bool:
    return get_default_index().index_chat_turn(event_id, user_text, reply_text, date=date,
                                               keep_reply=keep_reply)
