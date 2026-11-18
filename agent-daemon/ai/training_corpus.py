"""Opt-in training corpus (#118): the prompt and card behind each decision.

Off by default. While the user has opted in (``AGENT_TRAINING_CORPUS=1``),
every approval-card decision stores one row in ``~/.bot-inator/
training.db`` (SQLite, 0600, ``secure_delete`` on, never sent off the Mac):

* the prompt the model saw for the turn (``prompt``),
* the card it wrote: tool, arguments (draft text included), reasoning,
  summary and bundle actions (``card``),
* the verdict: ``approved`` / ``denied`` / ``edited`` (with the final
  arguments the user approved, ``final_args``) / ``timed_out``,
* the trigger: event type, trigger kind and the event's metadata
  (``payload``),
* later outcomes: ``undone`` (Undo within :data:`UNDO_SOON_SECONDS` of the
  decision) or ``tool_never_run`` (a generated tool that was never used).

Rows are joined to #79's ``OutcomeStore`` / ``DecisionLog`` by ``offer_id``
(and to #83's journal by ``event_id``), not copied from them.

Email and message text (decision 1) need a second toggle,
``AGENT_TRAINING_KEEP_MAIL_TEXT=1``. With it off, a row whose turn came from
an email or iMessage (a ``mail_received`` / ``imessage_received`` event, a
``pattern_detected`` idea built from one, or a turn that read mail or a
message, see :func:`is_mail_derived`) is stored STRIPPED:

* ``prompt`` is replaced by :data:`STRIPPED_PROMPT` (the prompt is built
  from the email or message, so none of it is kept);
* ``payload`` keeps only the structural keys in :data:`STRUCTURAL_KEYS`
  (ids, mailbox, trigger, source); subject, sender, body, text, snippet and
  every other field are dropped;
* any verbatim copy of a dropped payload string (12+ chars; addresses in
  :data:`ADDRESS_KEYS` excepted) inside the card or the final arguments is
  replaced by :data:`STRIPPED_QUOTE`.

The draft text the model wrote itself is kept: that is what the corpus is
for.

``tainted`` (decision 5) marks cards with origin notes (#81: an argument
came from outside text). Tainted rows are stored but left out of
:meth:`TrainingCorpus.training_rows`, :meth:`heldout_rows` and
:meth:`usable_counts` unless asked for.

Held-out split (#119 decision 5, #121 decision 2): :meth:`freeze_split`
picks ~10% of the eligible rows by a seeded hash of each row id and records
the choice in the DB, so every later call (and #120's gate) scores on the
same rows. Rows added after a freeze are training rows until a new split is
frozen (``refreeze=True``).

``path=None`` keeps the corpus in memory (tests); ``clock`` is injectable;
``enabled`` / ``keep_mail_text`` are callables (default: the live settings
readers below), so the store needs nothing from ``user_settings`` writes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Flag = Callable[[], bool]

DATA_DIR = Path.home() / ".bot-inator"
ENV_PATH = "AGENT_TRAINING_DB"

ENABLED_KEY = "AGENT_TRAINING_CORPUS"
KEEP_MAIL_TEXT_KEY = "AGENT_TRAINING_KEEP_MAIL_TEXT"

VERDICTS = ("approved", "denied", "edited", "timed_out")
# Verdicts a model can learn from (#119: approved/edited -> target,
# denied/undone -> negative). A timeout says nothing either way.
TRAINABLE_VERDICTS = ("approved", "denied", "edited")
OUTCOMES = ("undone", "tool_never_run")
UNDO_SOON_SECONDS = 24 * 3600.0

HELDOUT_FRACTION = 0.10
DEFAULT_SEED = "bot-inator-heldout-v1"

MAX_PROMPT_CHARS = 20_000
MAX_JSON_CHARS = 20_000
MAX_LIST = 500
MIN_QUOTE_CHARS = 12

STRIPPED_PROMPT = "[not kept: email or message text]"
STRIPPED_QUOTE = "[email or message text]"

MAIL_EVENTS = frozenset({"mail_received", "imessage_received"})
MAIL_KINDS = frozenset({"email", "message"})
# Payload keys kept on a stripped row: ids and labels, never text.
STRUCTURAL_KEYS = frozenset({
    "message_id", "mailbox", "date_received", "trigger", "source",
    "source_event_type", "tool_name", "builtin_tool", "idea_kind", "kind",
    "event_type", "app_name", "watch_id",
})
# Dropped from a stripped payload, but not scrubbed from the card: an
# address is not the email's text, and a reply's "to" must survive.
ADDRESS_KEYS = frozenset({"sender", "from", "to", "cc", "bcc", "recipient", "recipients", "handle"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rows (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    offer_id TEXT,
    request_id TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT '',
    tool_name TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    card TEXT NOT NULL DEFAULT '{}',
    payload TEXT NOT NULL DEFAULT '{}',
    verdict TEXT NOT NULL,
    final_args TEXT,
    tainted INTEGER NOT NULL DEFAULT 0,
    mail_stripped INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    outcome_at REAL
);
CREATE INDEX IF NOT EXISTS rows_created ON rows(created_at);
CREATE INDEX IF NOT EXISTS rows_offer ON rows(offer_id);
CREATE TABLE IF NOT EXISTS splits (
    split_id TEXT PRIMARY KEY,
    frozen_at REAL NOT NULL,
    seed TEXT NOT NULL,
    fraction REAL NOT NULL,
    current INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS heldout (
    split_id TEXT NOT NULL,
    row_id TEXT NOT NULL,
    PRIMARY KEY (split_id, row_id)
);
"""


# ---------------------------------------------------------------------------
# Settings (read live, like events.remote_channel)
# ---------------------------------------------------------------------------


def default_path() -> Path:
    override = os.environ.get(ENV_PATH, "").strip()
    return Path(override).expanduser() if override else DATA_DIR / "training.db"


def _setting(key: str, default: str) -> str:
    """Shell export, then the settings file (read live), then env, then default."""
    try:
        import user_settings

        if key not in user_settings.shell_exported:
            saved = user_settings.read().get(key)
            if saved:
                return saved
    except Exception:  # pragma: no cover - settings file unreadable
        logger.debug("user_settings unreadable", exc_info=True)
    return os.environ.get(key) or default


def opted_in() -> bool:
    """The main opt-in (decision 1). Off by default."""
    return _setting(ENABLED_KEY, "0").strip() == "1"


def keep_mail_text() -> bool:
    """The second, separate toggle for email and message text. Off by default."""
    return _setting(KEEP_MAIL_TEXT_KEY, "0").strip() == "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def outside_kinds_of(shield: Any) -> list[str]:
    """The kinds of outside text a ``taint.TurnTaint`` read this turn
    (``email``, ``message``, ``web_page`` ...). Empty for None."""
    if shield is None:
        return []
    kinds = getattr(shield, "kinds", None)
    if callable(kinds):
        try:
            return [str(k) for k in kinds()]
        except Exception:
            return []
    lock = getattr(shield, "_lock", None)
    reads = getattr(shield, "_reads", None) or []
    try:
        if lock is not None:
            with lock:
                return [str(getattr(r, "kind", "")) for r in list(reads)]
        return [str(getattr(r, "kind", "")) for r in list(reads)]
    except Exception:
        return []


def is_mail_derived(
    event_type: str,
    event_meta: Optional[Mapping[str, Any]] = None,
    outside_kinds: Iterable[str] = (),
) -> bool:
    """True when the turn's text came from an email or a message."""
    if event_type in MAIL_EVENTS:
        return True
    if any(str(k) in MAIL_KINDS for k in outside_kinds or ()):
        return True
    meta = event_meta or {}
    if event_type == "pattern_detected":
        from ai import taint  # noqa: WPS433 - light module, lazy on purpose

        kind = taint.is_outside_trigger(meta.get("trigger"))
        if kind is None:
            kind = taint.SOURCE_EVENT_KINDS.get(str(meta.get("source_event_type") or ""))
        if kind in MAIL_KINDS:
            return True
    return str(meta.get("source_event_type") or "") in MAIL_EVENTS


def _jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_PROMPT_CHARS]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v, depth + 1) for k, v in list(value.items())[:MAX_LIST]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1) for v in list(value)[:MAX_LIST]]
    return str(value)[:MAX_PROMPT_CHARS]


def _dump(value: Any) -> str:
    text = json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True)
    if len(text) > MAX_JSON_CHARS:
        text = json.dumps({"truncated": text[:MAX_JSON_CHARS]}, ensure_ascii=False)
    return text


def _load(text: Optional[str]) -> Any:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _strings(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(value, str):
        if len(value.strip()) >= MIN_QUOTE_CHARS:
            out.append(value.strip())
    elif isinstance(value, Mapping):
        for v in value.values():
            _strings(v, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _strings(v, out, depth + 1)


def _scrub(value: Any, quotes: list[str], depth: int = 0) -> Any:
    """Replace verbatim copies of ``quotes`` inside string leaves."""
    if depth > 8 or not quotes:
        return value
    if isinstance(value, str):
        for q in quotes:
            if q in value:
                value = value.replace(q, STRIPPED_QUOTE)
        return value
    if isinstance(value, Mapping):
        return {k: _scrub(v, quotes, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, quotes, depth + 1) for v in value]
    return value


def _structural(meta: Mapping[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in meta.items()
        if k in STRUCTURAL_KEYS and (v is None or isinstance(v, (bool, int, float, str)))
    }


def _effective_kwargs(tool_args: Mapping[str, Any], edited_args: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    from ai import standing_rules  # noqa: WPS433 - same rule the decision log uses

    return standing_rules.effective_kwargs(tool_args, edited_args)


def _heldout_rank(seed: str, row_id: str) -> str:
    return hashlib.sha256(f"{seed}:{row_id}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TrainingCorpus:
    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        enabled: Optional[Flag] = None,
        keep_mail_text: Optional[Flag] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.enabled: Flag = enabled or opted_in
        self.keep_mail_text: Flag = keep_mail_text or globals()["keep_mail_text"]
        self.clock: Clock = clock or time.time
        self._lock = threading.RLock()
        self._db: Optional[sqlite3.Connection] = None
        # Nothing touches the disk until the first row is written (or the
        # file already exists): an opted-out user gets no training.db.
        if self.path is None or self.path.exists():
            self._conn()

    # ---- storage ---------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is not None:
                return self._db
            if self.path is None:
                db = sqlite3.connect(":memory:", check_same_thread=False)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if not self.path.exists():
                    os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
                os.chmod(self.path, 0o600)
                db = sqlite3.connect(str(self.path), check_same_thread=False)
            db.row_factory = sqlite3.Row
            # Deleted rows are overwritten, not left in free pages.
            db.execute("PRAGMA secure_delete = ON")
            db.executescript(_SCHEMA)
            db.commit()
            self._db = db
            return db

    def _exists(self) -> bool:
        return self._db is not None or (self.path is not None and self.path.exists())

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ---- record ----------------------------------------------------------

    def record(
        self,
        *,
        tool_name: str,
        tool_args: Optional[Mapping[str, Any]],
        verdict: str,
        prompt: str = "",
        edited_args: Optional[Mapping[str, Any]] = None,
        offer_id: Optional[str] = None,
        request_id: str = "",
        event_id: str = "",
        event_type: str = "",
        trigger: str = "",
        event_meta: Optional[Mapping[str, Any]] = None,
        reasoning: str = "",
        summary: str = "",
        actions: Optional[list[Mapping[str, Any]]] = None,
        tainted: bool = False,
        outside_kinds: Iterable[str] = (),
    ) -> Optional[dict[str, Any]]:
        """Store one card decision; ``None`` (and nothing written) when the
        user has not opted in.

        ``verdict`` is ``approved`` / ``denied`` / ``timed_out``; an
        approval whose final arguments differ from the proposal is stored
        as ``edited``. ``tainted``: the card had origin notes (#81).
        """
        try:
            if not self.enabled():
                return None
        except Exception:
            logger.exception("training corpus opt-in check failed; not recording")
            return None
        if verdict == "declined":
            verdict = "denied"
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")

        args = dict(tool_args or {})
        original = dict(args.get("kwargs") or {})
        final: Optional[dict[str, Any]] = None
        if verdict in ("approved", "edited"):
            final = _effective_kwargs(args, edited_args) if edited_args else original
            verdict = "edited" if final != original else "approved"
        card: dict[str, Any] = {
            "tool": tool_name,
            "args": original,
            "reasoning": reasoning or "",
            "summary": summary or "",
        }
        if actions:
            card["actions"] = list(actions)
        meta = dict(event_meta or {})
        kinds = list(outside_kinds or ())

        stripped = False
        try:
            keep_mail = bool(self.keep_mail_text())
        except Exception:
            keep_mail = False
        if not keep_mail and is_mail_derived(event_type, meta, kinds):
            stripped = True
            quotes: list[str] = []
            _strings({k: v for k, v in meta.items()
                      if k not in STRUCTURAL_KEYS and k not in ADDRESS_KEYS}, quotes)
            quotes.sort(key=len, reverse=True)
            prompt = STRIPPED_PROMPT
            meta = _structural(meta)
            card = _scrub(card, quotes)
            final = _scrub(final, quotes) if final is not None else None

        row_id = f"tr_{uuid.uuid4().hex[:16]}"
        now = float(self.clock())
        values = (
            row_id, now, offer_id or None, str(request_id or ""), str(event_id or ""),
            str(event_type or ""), str(trigger or ""), str(tool_name or ""),
            str(prompt or "")[:MAX_PROMPT_CHARS], _dump(card), _dump(meta), verdict,
            _dump(final) if final is not None else None, 1 if tainted else 0,
            1 if stripped else 0,
        )
        with self._lock:
            db = self._conn()
            db.execute(
                "INSERT INTO rows (id, created_at, offer_id, request_id, event_id, event_type,"
                " trigger, tool_name, prompt, card, payload, verdict, final_args, tainted,"
                " mail_stripped) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            db.commit()
        return self.get(row_id)

    # ---- outcomes ----------------------------------------------------------

    def _mark(self, outcome: str, offer_id: Optional[str], event_id: Optional[str],
              within: Optional[float]) -> int:
        if not (offer_id or event_id) or not self._exists():
            return 0
        now = float(self.clock())
        clauses, params = [], []
        if offer_id:
            clauses.append("offer_id = ?")
            params.append(offer_id)
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        sql = (f"UPDATE rows SET outcome = ?, outcome_at = ? WHERE ({' OR '.join(clauses)})"
               " AND verdict IN ('approved', 'edited')")
        args: list[Any] = [outcome, now, *params]
        if within is not None:
            sql += " AND created_at >= ?"
            args.append(now - within)
        with self._lock:
            cur = self._conn().execute(sql, args)
            self._db.commit()  # type: ignore[union-attr]
            return cur.rowcount

    def mark_undone(self, offer_id: Optional[str] = None, *, event_id: Optional[str] = None) -> int:
        """The approved action was undone soon after (within
        :data:`UNDO_SOON_SECONDS` of the decision): a negative for #119.
        Matches by offer id (#79) or the journal entry's event id (#83).
        Returns the number of rows marked."""
        return self._mark("undone", offer_id, event_id, UNDO_SOON_SECONDS)

    def mark_tool_never_run(self, offer_id: str) -> int:
        """The tool this approved offer installed was never run (#79)."""
        return self._mark("tool_never_run", offer_id, None, None)

    def undo_listener(self, entry: Mapping[str, Any], _result: Any = None) -> None:
        """``ActionJournal.add_undo_listener`` hook (#83)."""
        try:
            self.mark_undone(entry.get("offer_id") or None, event_id=entry.get("event_id") or None)
        except Exception:
            logger.exception("training corpus: mark_undone failed")

    # ---- reading -----------------------------------------------------------

    @staticmethod
    def _row(r: sqlite3.Row, heldout: Optional[set[str]] = None) -> dict[str, Any]:
        out = {k: r[k] for k in r.keys()}
        out["card"] = _load(out.get("card")) or {}
        out["payload"] = _load(out.get("payload")) or {}
        out["final_args"] = _load(out.get("final_args"))
        out["tainted"] = bool(out.get("tainted"))
        out["mail_stripped"] = bool(out.get("mail_stripped"))
        out["negative"] = out["verdict"] == "denied" or out.get("outcome") == "undone"
        if heldout is not None:
            out["heldout"] = out["id"] in heldout
        return out

    def get(self, row_id: str) -> Optional[dict[str, Any]]:
        if not self._exists():
            return None
        with self._lock:
            r = self._conn().execute("SELECT * FROM rows WHERE id = ?", (row_id,)).fetchone()
            return self._row(r, self._heldout_ids()) if r else None

    def count(self) -> int:
        if not self._exists():
            return 0
        with self._lock:
            return int(self._conn().execute("SELECT COUNT(*) FROM rows").fetchone()[0])

    def rows(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Every row (tainted too), newest first, for Settings."""
        if not self._exists():
            return []
        limit = max(1, min(int(limit), MAX_LIST))
        offset = max(0, int(offset))
        with self._lock:
            held = self._heldout_ids()
            cur = self._conn().execute(
                "SELECT * FROM rows ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            return [self._row(r, held) for r in cur]

    def _eligible(self, include_tainted: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM rows WHERE verdict IN ('approved', 'edited', 'denied')"
        if not include_tainted:
            sql += " AND tainted = 0"
        sql += " ORDER BY created_at, rowid"
        return list(self._conn().execute(sql))

    def usable_counts(self) -> dict[str, int]:
        """#119's threshold numbers: ``total`` untainted trainable rows and
        ``negatives`` among them (denied, or approved then undone)."""
        if not self._exists():
            return {"total": 0, "negatives": 0, "tainted": 0, "all": 0, "heldout": 0}
        with self._lock:
            db = self._conn()
            total, negatives = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN verdict = 'denied' OR outcome = 'undone'"
                " THEN 1 ELSE 0 END), 0) FROM rows WHERE tainted = 0"
                " AND verdict IN ('approved', 'edited', 'denied')"
            ).fetchone()
            tainted = db.execute("SELECT COUNT(*) FROM rows WHERE tainted = 1").fetchone()[0]
            everything = db.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
            return {
                "total": int(total), "negatives": int(negatives), "tainted": int(tainted),
                "all": int(everything), "heldout": len(self._heldout_ids()),
            }

    # ---- held-out split ------------------------------------------------------

    def _current_split(self) -> Optional[sqlite3.Row]:
        return self._conn().execute(
            "SELECT * FROM splits WHERE current = 1 ORDER BY frozen_at DESC LIMIT 1"
        ).fetchone()

    def _heldout_ids(self) -> set[str]:
        split = self._current_split()
        if split is None:
            return set()
        return {r[0] for r in self._conn().execute(
            "SELECT h.row_id FROM heldout h JOIN rows r ON r.id = h.row_id WHERE h.split_id = ?",
            (split["split_id"],),
        )}

    def split_info(self) -> Optional[dict[str, Any]]:
        if not self._exists():
            return None
        with self._lock:
            split = self._current_split()
            if split is None:
                return None
            return {"split_id": split["split_id"], "frozen_at": split["frozen_at"],
                    "seed": split["seed"], "fraction": split["fraction"],
                    "size": len(self._heldout_ids())}

    def freeze_split(
        self,
        *,
        fraction: float = HELDOUT_FRACTION,
        seed: str = DEFAULT_SEED,
        refreeze: bool = False,
    ) -> dict[str, Any]:
        """Freeze ~``fraction`` of the eligible (untainted, trainable) rows as
        held-out. Deterministic: rows are ranked by ``sha256(seed:id)`` and
        the lowest ``ceil(n * fraction)`` are held out. Recorded in the DB:
        a second call returns the same split unless ``refreeze=True``."""
        if not 0 < fraction < 1:
            raise ValueError("fraction must be between 0 and 1")
        with self._lock:
            db = self._conn()
            split = self._current_split()
            if split is not None and not refreeze:
                return self.split_info() or {}
            ids = [r["id"] for r in self._eligible()]
            size = math.ceil(len(ids) * fraction) if ids else 0
            chosen = sorted(ids, key=lambda i: _heldout_rank(seed, i))[:size]
            split_id = f"split_{uuid.uuid4().hex[:12]}"
            db.execute("UPDATE splits SET current = 0")
            db.execute(
                "INSERT INTO splits (split_id, frozen_at, seed, fraction, current) VALUES (?,?,?,?,1)",
                (split_id, float(self.clock()), seed, float(fraction)),
            )
            db.executemany("INSERT INTO heldout (split_id, row_id) VALUES (?, ?)",
                           [(split_id, i) for i in chosen])
            db.commit()
            return self.split_info() or {}

    def training_rows(self, *, include_tainted: bool = False) -> list[dict[str, Any]]:
        """Eligible rows not in the frozen held-out set, oldest first."""
        if not self._exists():
            return []
        with self._lock:
            held = self._heldout_ids()
            return [self._row(r, held) for r in self._eligible(include_tainted) if r["id"] not in held]

    def heldout_rows(self, *, include_tainted: bool = False) -> list[dict[str, Any]]:
        """The frozen held-out rows still stored, oldest first ([] before a freeze)."""
        if not self._exists():
            return []
        with self._lock:
            held = self._heldout_ids()
            return [self._row(r, held) for r in self._eligible(include_tainted) if r["id"] in held]

    # ---- deleting ------------------------------------------------------------

    def delete(self, row_id: str) -> bool:
        if not self._exists():
            return False
        with self._lock:
            db = self._conn()
            cur = db.execute("DELETE FROM rows WHERE id = ?", (row_id,))
            db.execute("DELETE FROM heldout WHERE row_id = ?", (row_id,))
            db.commit()
            return cur.rowcount > 0

    def delete_all(self) -> int:
        """Every row, the held-out split with them. Returns rows deleted."""
        if not self._exists():
            return 0
        with self._lock:
            db = self._conn()
            n = int(db.execute("SELECT COUNT(*) FROM rows").fetchone()[0])
            db.execute("DELETE FROM rows")
            db.execute("DELETE FROM heldout")
            db.execute("DELETE FROM splits")
            db.commit()
            db.execute("VACUUM")
            return n

    def opt_out(self, *, delete_stored: bool = True) -> int:
        """The user turned the opt-in off; "delete what's stored" defaults
        to yes (decision 3). Deletes everything, held-out included."""
        return self.delete_all() if delete_stored else 0
