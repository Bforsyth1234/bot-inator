"""Always allow: the user's own "don't ask me again" for one tool on one
kind of trigger (#116, user decision 2026-11-18, amendment to 7a).

The user turns it on by clicking **Always allow** on an approval card. The
app shows that button only on a card whose tool History can undo
(:data:`ELIGIBLE_TOOLS`, the same set as ``ai.action_journal
.UNDOABLE_TOOLS``). After that, a call of the same tool in a turn with the
same trigger kind (``ai.idea_outcomes.trigger_kind_of``: ``mail_received``,
``user_message``, ...) runs without a card. Unlike a standing rule (#78)
the arguments still come from the model; what keeps it safe is the
narrow tool set: every one of them is undone in one click, every run is
journaled (``source="always_allow"``) and announced with a notice that
carries Undo plus a macOS notification, and the user can see and revoke
each entry in Settings → Always allowed.

A model can never add an entry: only ``Orchestrator._request_approval``
(on the user's click) and ``routes.always_allow`` (delete) touch the store.
An entry undone twice switches itself off, like a standing rule.

Stored in ``~/.bot-inator/always_allow.json`` (0600) or
``$AGENT_ALWAYS_ALLOW_PATH``. ``path=None`` keeps it in memory (tests).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PATH_ENV = "AGENT_ALWAYS_ALLOW_PATH"
ELIGIBLE_TOOLS: frozenset[str] = frozenset(
    {"move_file", "create_event", "create_reminder", "create_note"}
)
UNDOS_TO_DISABLE = 2
MAX_ENTRIES = 30
JOURNAL_SOURCE = "always_allow"

_TOOL_WORDS = {
    "create_reminder": "add reminders",
    "create_event": "add calendar events",
    "create_note": "add notes",
    "move_file": "move files",
}
_TRIGGER_WORDS = {
    "mail_received": "from email",
    "mail_needs_reply": "from email",
    "user_message": "when you ask in chat",
    "file": "for new files",
    "imessage_received": "from messages",
}


def key_of(tool: str, trigger: str) -> str:
    return f"{tool}|{trigger}"


def describe(tool: str, trigger: str) -> str:
    """"Always add reminders from email" (button and Settings row)."""
    what = _TOOL_WORDS.get(tool, tool)
    when = _TRIGGER_WORDS.get(trigger, f"for {trigger.replace('_', ' ')}" if trigger else "")
    return f"Always {what} {when}".strip()


def eligible(tool: str, trigger: str) -> bool:
    return tool in ELIGIBLE_TOOLS and bool(trigger)


def default_path() -> Path:
    env = os.environ.get(PATH_ENV)
    return Path(env).expanduser() if env else Path.home() / ".bot-inator" / "always_allow.json"


class AlwaysAllow:
    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # ---- storage --------------------------------------------------------

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            entries = data.get("entries") or {}
            self._entries = {
                str(k): dict(v) for k, v in entries.items()
                if isinstance(v, dict) and v.get("tool") in ELIGIBLE_TOOLS
            }
        except Exception:
            logger.exception("always_allow store unreadable; starting empty")
            self._entries = {}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), prefix=".always_allow.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"version": 1, "entries": self._entries}, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- queries --------------------------------------------------------

    def allows(self, tool: str, trigger: str) -> bool:
        with self._lock:
            entry = self._entries.get(key_of(tool, trigger))
            return bool(entry and entry.get("enabled", True) and tool in ELIGIBLE_TOOLS)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"key": k, **v, "words": describe(v["tool"], v["trigger"])}
                for k, v in sorted(self._entries.items(), key=lambda kv: kv[1].get("added_at", 0))
            ]

    # ---- changes (user only) -------------------------------------------

    def grant(self, tool: str, trigger: str) -> bool:
        """The user clicked Always allow. False when not eligible / full."""
        if not eligible(tool, trigger):
            return False
        with self._lock:
            key = key_of(tool, trigger)
            if key not in self._entries and len(self._entries) >= MAX_ENTRIES:
                return False
            self._entries[key] = {
                "tool": tool, "trigger": trigger, "enabled": True,
                "added_at": time.time(), "runs": int(self._entries.get(key, {}).get("runs", 0)),
                "undos": 0,
            }
            self._save()
        return True

    def revoke(self, key: str) -> bool:
        with self._lock:
            if self._entries.pop(key, None) is None:
                return False
            self._save()
        return True

    def note_run(self, tool: str, trigger: str) -> None:
        with self._lock:
            entry = self._entries.get(key_of(tool, trigger))
            if entry is None:
                return
            entry["runs"] = int(entry.get("runs", 0)) + 1
            entry["last_run_at"] = time.time()
            self._save()

    def note_undo(self, tool: str, trigger: str) -> bool:
        """An automatic run was undone; True when that switched it off."""
        with self._lock:
            entry = self._entries.get(key_of(tool, trigger))
            if entry is None:
                return False
            entry["undos"] = int(entry.get("undos", 0)) + 1
            off = entry["undos"] >= UNDOS_TO_DISABLE
            if off:
                entry["enabled"] = False
            self._save()
            return off


    def on_undo(self, entry: dict[str, Any], result: dict[str, Any]) -> None:
        """``ActionJournal.add_undo_listener(store.on_undo)``: an undone
        automatic run counts; at :data:`UNDOS_TO_DISABLE` it switches off."""
        if (entry or {}).get("source") != JOURNAL_SOURCE or (result or {}).get("status") != "undone":
            return
        if self.note_undo(str(entry.get("tool") or ""), str(entry.get("why") or "")):
            logger.info("always allow %s|%s switched off after undos", entry.get("tool"), entry.get("why"))


def run_notice(tool: str, trigger: str, entry: Optional[dict[str, Any]]) -> dict[str, Any]:
    """``NoticePayload`` dict for one run without a card, with Undo."""
    entry = entry or {}
    notice: dict[str, Any] = {
        "id": f"always_allow_run:{entry.get('id') or key_of(tool, trigger)}",
        "level": "info",
        "title": "Done without asking",
        "body": (f"{entry.get('label') or tool} ({describe(tool, trigger).lower()}). "
                 "Undo it here or in History; turn it off in Settings → Always allowed."),
    }
    if entry.get("id") and entry.get("can_undo", True):
        notice["action"] = {"kind": "undo", "entry_id": str(entry["id"])}
    return notice


_default: Optional[AlwaysAllow] = None


def default_store() -> AlwaysAllow:
    global _default
    if _default is None:
        _default = AlwaysAllow(default_path())
    return _default


__all__ = [
    "AlwaysAllow", "ELIGIBLE_TOOLS", "JOURNAL_SOURCE", "UNDOS_TO_DISABLE", "default_store",
    "describe", "eligible", "key_of", "run_notice",
]
