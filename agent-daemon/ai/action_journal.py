"""What the agent did, and how to take it back (#83).

One journal of every side-effecting tool call that ran (after its approval
card, or, from #78 on, under a standing rule), in ``~/.bot-inator/
actions.db`` (SQLite, 0600, kept :data:`RETENTION_SECONDS` = 30 days).

Each entry stores the tool, ids, paths and times only (never a title, note
text or email body), and, for a tool in :data:`UNDOABLE_TOOLS`, an *undo
spec*: what to reverse and how to tell it was not changed since.

* ``move_file``: the old and new path plus the moved file's size and
  mtime. Undo moves it back only while it is still at ``new_path`` with the
  same size and mtime and ``old_path`` is free.
* ``create_event`` / ``create_reminder`` / ``create_note``: the id the tool
  returned plus a short hash of the item's fields right after it was made
  (``tools.calendar_access.fingerprint``, ``tools.notes.note_fingerprint``).
  Undo deletes that id, and only while the hash still matches.

Anything else ("changed since", already gone) is refused with a reason and
nothing is touched. The delete operations take a journal entry, never an
argument from the model: there is no delete tool (LIVING_SPEC §1).

Undo is the user's click (or #78's notification button), so it needs no
approval card. Every attempt is logged (``undo_log`` table) and a
successful one calls ``OutcomeStore.record_undone(offer_id)`` (#79) when
the entry came from an idea offer, then every listener registered with
:meth:`ActionJournal.add_undo_listener` (#78: "a rule undone twice switches
itself off", see :meth:`ActionJournal.undo_count`).

``path=None`` keeps the journal in memory (tests). ``clock`` is injectable.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
UndoListener = Callable[[dict[str, Any], dict[str, Any]], None]

DATA_DIR = Path.home() / ".bot-inator"
RETENTION_SECONDS = 30 * 24 * 3600.0
MAX_LABEL = 160
MAX_WHY = 80
MAX_ROWS = 500

# Tools whose result the journal can reverse.
UNDOABLE_TOOLS: frozenset[str] = frozenset(
    {"move_file", "create_event", "create_reminder", "create_note"}
)

# Shown in History for a side-effecting tool that cannot be taken back.
_NOT_UNDOABLE = {
    "run_shortcut": "A shortcut can do anything, so it can't be undone.",
    "open_url": "Opening a page can't be undone.",
    "open_app": "Opening an app can't be undone.",
    "show_notification": "A notification can't be taken back.",
    "draft_email": "Not undoable yet: delete the draft in Mail.",
    "append_note": "Not undoable yet: remove the text in Notes.",
    "write_clipboard": "Not undoable yet.",
    "generate_custom_tool": "Remove the tool in Manage Tools instead.",
}
_GENERIC_NOT_UNDOABLE = "This action can't be undone."

UNDO_STATUSES = ("undone", "changed", "gone", "not_undoable", "already_undone",
                 "not_found", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    tool TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    why TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL DEFAULT '',
    offer_id TEXT,
    source TEXT NOT NULL DEFAULT 'approval',
    rule_id TEXT,
    undo_spec TEXT,
    not_undoable TEXT NOT NULL DEFAULT '',
    undone_at REAL,
    undo_status TEXT,
    undo_message TEXT
);
CREATE INDEX IF NOT EXISTS actions_created ON actions(created_at);
CREATE TABLE IF NOT EXISTS undo_log (
    entry_id TEXT NOT NULL,
    at REAL NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);
"""


def default_path() -> Path:
    return DATA_DIR / "actions.db"


def is_undoable(tool_name: str) -> bool:
    """True when an approved call of ``tool_name`` can be undone from the
    journal: the daemon sets ``reversible`` on its approval card (#83)."""
    return tool_name in UNDOABLE_TOOLS


def not_undoable_reason(tool_name: str) -> str:
    return "" if is_undoable(tool_name) else _NOT_UNDOABLE.get(tool_name, _GENERIC_NOT_UNDOABLE)


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _ok(result: Any) -> bool:
    """A call that did something: not an error, a skip or a denial."""
    if isinstance(result, Mapping):
        if result.get("skipped") or result.get("error") and result.get("status") != "ok":
            return False
        return str(result.get("status") or "ok").lower() not in {"error", "failed", "denied"}
    return result is not None


class UndoOps:
    """The operations an undo runs. Tests replace single methods."""

    # files -------------------------------------------------------------
    def file_stat(self, path: str) -> Optional[tuple[int, int]]:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (int(st.st_size), int(st.st_mtime_ns))

    def path_taken(self, path: str) -> bool:
        return os.path.lexists(path)

    def dir_exists(self, path: str) -> bool:
        return os.path.isdir(path)

    def move(self, src: str, dst: str) -> None:
        shutil.move(src, dst)

    # calendar / reminders (lazy: tools must not load at journal import) -
    def event_fingerprint(self, event_id: str) -> Optional[str]:
        from tools import calendar_access as ca
        return ca.fingerprint(ca.get_event(event_id), "event")

    def delete_event(self, event_id: str) -> None:
        from tools import calendar_access as ca
        ca.delete_event(event_id)

    def reminder_fingerprint(self, reminder_id: str) -> Optional[str]:
        from tools import calendar_access as ca
        return ca.fingerprint(ca.get_reminder(reminder_id), "reminder")

    def delete_reminder(self, reminder_id: str) -> None:
        from tools import calendar_access as ca
        ca.delete_reminder(reminder_id)

    # notes ---------------------------------------------------------------
    def note_fingerprint(self, note_id: str) -> Optional[str]:
        from tools import notes
        return notes.note_fingerprint(note_id)

    def delete_note(self, note_id: str) -> bool:
        from tools import notes
        return notes.delete_note(note_id)


# kind -> (fingerprint op, delete op, id key, noun)
_ITEM_KINDS = {
    "event": ("event_fingerprint", "delete_event", "event_id", "event"),
    "reminder": ("reminder_fingerprint", "delete_reminder", "reminder_id", "reminder"),
    "note": ("note_fingerprint", "delete_note", "note_id", "note"),
}
_TOOL_KIND = {"create_event": "event", "create_reminder": "reminder", "create_note": "note"}


class ActionJournal:
    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        outcomes: Any = None,
        clock: Optional[Clock] = None,
        ops: Optional[UndoOps] = None,
        retention_seconds: float = RETENTION_SECONDS,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self.outcomes = outcomes  # ai.idea_outcomes.OutcomeStore (#79), optional
        self.clock: Clock = clock or time.time
        self.ops = ops or UndoOps()
        self.retention_seconds = float(retention_seconds)
        self._lock = threading.RLock()
        self._listeners: list[UndoListener] = []
        self._db = self._open()
        self.prune()

    # ---- storage ---------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        if self.path is None:
            db = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
            os.chmod(self.path, 0o600)
            db = sqlite3.connect(str(self.path), check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        db.commit()
        return db

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def prune(self) -> int:
        """Drop entries (and their undo log) older than the retention."""
        cutoff = float(self.clock()) - self.retention_seconds
        with self._lock:
            old = [r["id"] for r in self._db.execute(
                "SELECT id FROM actions WHERE created_at < ?", (cutoff,))]
            self._db.executemany("DELETE FROM undo_log WHERE entry_id = ?", [(i,) for i in old])
            self._db.execute("DELETE FROM actions WHERE created_at < ?", (cutoff,))
            self._db.commit()
        return len(old)

    # ---- record ----------------------------------------------------------

    def record(
        self,
        tool: str,
        args: Any,
        result: Any,
        *,
        event_id: str = "",
        offer_id: Optional[str] = None,
        why: str = "",
        source: str = "approval",
        rule_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Journal one side-effecting call that ran; its entry, or None.

        Call it after the tool returned, for every non-read-only call (the
        approval path and #78's rule runs). Errors, skips and denials are not
        recorded. ``args`` (the call's kwargs, or the card's ``{"args",
        "kwargs"}`` shape) is used only to label the entry; nothing from it
        is stored except paths. Runs ops (e.g. a calendar read for the
        fingerprint), so call it off the event loop.
        """
        if not tool or not _ok(result):
            return None
        res = result if isinstance(result, Mapping) else {}
        spec: Optional[dict[str, Any]] = None
        label = _clip(tool, MAX_LABEL)
        reason = not_undoable_reason(tool)
        try:
            if tool == "move_file":
                spec, label, reason = self._file_spec(res)
            elif tool in _TOOL_KIND:
                spec, label, reason = self._item_spec(_TOOL_KIND[tool], res)
        except Exception as exc:  # never let journaling break the tool call
            logger.exception("action_journal: building the undo spec for %s failed", tool)
            spec, reason = None, f"Couldn't prepare the undo: {_clip(exc, 120)}"
        entry_id = f"act_{uuid.uuid4().hex[:12]}"
        row = (
            entry_id, float(self.clock()), _clip(tool, 80), label, _clip(why, MAX_WHY),
            _clip(event_id, 80), _clip(offer_id, 80) or None,
            "rule" if source == "rule" else "approval", _clip(rule_id, 80) or None,
            json.dumps(spec) if spec is not None else None, reason if spec is None else "",
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO actions (id, created_at, tool, label, why, event_id, offer_id,"
                " source, rule_id, undo_spec, not_undoable) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            self._db.commit()
        self.prune()
        return self.get(entry_id)

    def _file_spec(self, res: Mapping[str, Any]) -> tuple[Optional[dict], str, str]:
        old, new = str(res.get("old_path") or ""), str(res.get("new_path") or "")
        if not old or not new:
            return None, "Moved a file", "The move didn't report both paths."
        label = _clip(f"Moved {Path(new).name} to {Path(new).parent}", MAX_LABEL)
        stat = self.ops.file_stat(new)
        if stat is None:
            return None, label, f"{new} wasn't there right after the move."
        spec = {"kind": "file", "old_path": old, "new_path": new,
                "size": stat[0], "mtime_ns": stat[1]}
        return spec, label, ""

    def _item_spec(self, kind: str, res: Mapping[str, Any]) -> tuple[Optional[dict], str, str]:
        fp_op, _, id_key, noun = _ITEM_KINDS[kind]
        label = {"event": "Added a calendar event", "reminder": "Added a reminder",
                 "note": "Created a note"}[kind]
        where = res.get("calendar") or res.get("list")
        if where:
            label = _clip(f"{label} in {where}", MAX_LABEL)
        item_id = str(res.get(id_key) or "")
        if not item_id:
            return None, label, f"The {noun} came back without an id, so it can't be undone."
        try:
            fingerprint = getattr(self.ops, fp_op)(item_id)
        except Exception as exc:
            logger.warning("action_journal: reading the new %s failed: %s", noun, exc)
            fingerprint = None
        # A missing fingerprint keeps the spec: undo then refuses with a
        # reason ("couldn't confirm it is unchanged") instead of deleting.
        return {"kind": kind, "item_id": item_id, "fingerprint": fingerprint}, label, ""

    # ---- read --------------------------------------------------------------

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        spec = json.loads(row["undo_spec"]) if row["undo_spec"] else None
        status = row["undo_status"]
        can_undo = spec is not None and row["undone_at"] is None and status != "gone"
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "tool": row["tool"],
            "label": row["label"],
            "why": row["why"],
            "event_id": row["event_id"],
            "offer_id": row["offer_id"],
            "source": row["source"],
            "rule_id": row["rule_id"],
            "undoable": spec is not None,
            "can_undo": can_undo,
            "not_undoable_reason": row["not_undoable"],
            "undone_at": row["undone_at"],
            "undo_status": status,
            "undo_message": row["undo_message"],
        }

    def get(self, entry_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM actions WHERE id = ?", (entry_id,)).fetchone()
        return self._public(row) if row is not None else None

    def rows(self, limit: int = 200) -> list[dict[str, Any]]:
        """Newest first, at most ``limit`` (capped at :data:`MAX_ROWS`)."""
        limit = max(1, min(int(limit), MAX_ROWS))
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM actions ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._public(r) for r in rows]

    def undo_log(self, entry_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at, status, message FROM undo_log WHERE entry_id = ? ORDER BY at, rowid",
                (entry_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def undo_count(self, rule_id: str) -> int:
        """How many of this standing rule's runs were undone (#78)."""
        with self._lock:
            (n,) = self._db.execute(
                "SELECT COUNT(*) FROM actions WHERE rule_id = ? AND undo_status = 'undone'",
                (rule_id,),
            ).fetchone()
        return int(n)

    def add_undo_listener(self, listener: UndoListener) -> None:
        """``listener(entry, result)`` after every successful undo (#78)."""
        self._listeners.append(listener)

    # ---- undo --------------------------------------------------------------

    def undo(self, entry_id: str) -> dict[str, Any]:
        """Reverse one journaled action. The user's click; no card.

        Returns ``{status, message, entry}`` with ``status`` one of
        :data:`UNDO_STATUSES`: ``undone`` (reversed), ``changed`` (edited or
        moved since: nothing touched), ``gone`` (already deleted),
        ``not_undoable``, ``already_undone``, ``not_found`` or ``error``.
        Runs ops (file system, calendar bridge), so call it off the loop.
        """
        with self._lock:
            row = self._db.execute("SELECT * FROM actions WHERE id = ?", (entry_id,)).fetchone()
            if row is None:
                return {"status": "not_found", "message": "No such action in History.", "entry": None}
            if row["undone_at"] is not None:
                return self._finish(row, "already_undone", "Already undone.", log_only=True)
            if not row["undo_spec"]:
                return self._finish(row, "not_undoable",
                                    row["not_undoable"] or _GENERIC_NOT_UNDOABLE, log_only=True)
            spec = json.loads(row["undo_spec"])
            try:
                status, message = self._reverse(spec)
            except Exception as exc:
                logger.exception("action_journal: undo %s failed", entry_id)
                status, message = "error", f"Undo failed: {_clip(exc, 200)}"
            out = self._finish(row, status, message)
        if status == "undone":
            self._after_undo(out)
        return out

    def _finish(self, row: sqlite3.Row, status: str, message: str, *, log_only: bool = False) -> dict[str, Any]:
        now = float(self.clock())
        self._db.execute("INSERT INTO undo_log (entry_id, at, status, message) VALUES (?,?,?,?)",
                         (row["id"], now, status, message))
        if not log_only:
            self._db.execute(
                "UPDATE actions SET undo_status = ?, undo_message = ?, undone_at = ? WHERE id = ?",
                (status, message, now if status == "undone" else None, row["id"]),
            )
        self._db.commit()
        logger.info("undo %s (%s): %s %s", row["id"], row["tool"], status, message)
        return {"status": status, "message": message, "entry": self.get(row["id"])}

    def _after_undo(self, out: dict[str, Any]) -> None:
        entry = out.get("entry") or {}
        offer_id = entry.get("offer_id")
        if self.outcomes is not None and offer_id:
            try:
                self.outcomes.record_undone(offer_id)
            except Exception:
                logger.exception("idea_outcomes.record_undone failed")
        for listener in list(self._listeners):
            try:
                listener(entry, out)
            except Exception:
                logger.exception("action_journal undo listener failed")

    def _reverse(self, spec: Mapping[str, Any]) -> tuple[str, str]:
        kind = spec.get("kind")
        if kind == "file":
            return self._reverse_file(spec)
        if kind in _ITEM_KINDS:
            return self._reverse_item(kind, spec)
        return "not_undoable", f"Unknown undo kind {kind!r}."

    def _reverse_file(self, spec: Mapping[str, Any]) -> tuple[str, str]:
        old, new = str(spec["old_path"]), str(spec["new_path"])
        stat = self.ops.file_stat(new)
        if stat is None:
            return "changed", f"Changed since, not undone: the file is no longer at {new}."
        if stat != (int(spec["size"]), int(spec["mtime_ns"])):
            return "changed", f"Changed since, not undone: {Path(new).name} was edited after the move."
        if self.ops.path_taken(old):
            return "changed", f"Changed since, not undone: {old} is taken by another file."
        if not self.ops.dir_exists(str(Path(old).parent)):
            return "changed", f"Changed since, not undone: the folder {Path(old).parent} is gone."
        self.ops.move(new, old)
        return "undone", f"Moved {Path(old).name} back to {Path(old).parent}."

    def _reverse_item(self, kind: str, spec: Mapping[str, Any]) -> tuple[str, str]:
        fp_op, delete_op, _, noun = _ITEM_KINDS[kind]
        item_id = str(spec.get("item_id") or "")
        expected = spec.get("fingerprint")
        if not item_id:
            return "not_undoable", f"The {noun} has no id."
        current = getattr(self.ops, fp_op)(item_id)
        if current is None:
            return "gone", f"The {noun} is already gone; nothing to undo."
        if not expected:
            return "changed", f"Couldn't confirm the {noun} is unchanged, so it was not deleted."
        if current != expected:
            return "changed", f"Changed since, not undone: the {noun} was edited."
        deleted = getattr(self.ops, delete_op)(item_id)
        if deleted is False:
            return "gone", f"The {noun} is already gone; nothing to undo."
        return "undone", f"Deleted the {noun}."
