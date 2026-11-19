"""A timeline of the recent past (#126). Read-only.

Merges two local stores for one day, or a range of at most
:data:`MAX_DAYS` days:

* ``ai.activity_store.ActivityStore.rows``: what *kind* of thing happened
  and when (an app, a file's extension and folder, mail arriving). Never a
  file name: the store does not have one (#75). Names come from
  ``find_files(since=..., until=...)``.
* ``ai.action_journal.ActionJournal.rows``: what the agent did, with the
  label the journal wrote (for a move: the file's name and new folder) and
  whether it was undone.

At most :data:`MAX_LINES` lines come back. An hour with more than
:data:`HOUR_DETAIL` activity rows is summed into one line; if that is not
enough every hour is summed, then every day. What the agent did is never
summed.

With activity history off (``AGENT_ACTIVITY_HISTORY=0``) the result says so
and holds the journal part only.

Cloud engines (#126 decision 5): :func:`redact_result` shortens journal
paths to folder + extension, the level ``activity.db`` stores. The caller
that puts tool results back into a prompt applies it (where
``redact_email_result`` is applied in ``ai/orchestrator.py``).

The stores are injected with :func:`set_sources`. Without it the activity
store is ``ai.activity_store.default_store()`` and there is no journal (the
daemon's one is built in ``main.py``; this module never opens a second
connection to ``actions.db``).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Mapping, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn

from tools.find_files import parse_day

# This tool belongs in READ_ONLY_TOOL_NAMES (rule 7a: it changes nothing).
READ_ONLY = {"recall_activity"}

MAX_DAYS = 14
MAX_LINES = 100
# Activity rows shown one by one within an hour; more are summed.
HOUR_DETAIL = 6
# Distinct things named in a summed line.
SUMMARY_TOP = 5
# ``ActionJournal.rows`` returns at most this many (its MAX_ROWS).
JOURNAL_LIMIT = 500
JOURNAL_KEEPS_DAYS = 30

HISTORY_OFF_NOTE = (
    "Activity history is off (AGENT_ACTIVITY_HISTORY=0), so this holds only "
    "what the agent did. Apps, files and mail are not in it."
)
NO_JOURNAL_NOTE = "The action journal is not available, so what the agent did is not in this."
REDACTED_NOTE = (
    "File names in what the agent did were kept on this Mac because this "
    "engine runs in the cloud: only the folder and the file's extension are "
    "shown (the user can allow sending them in Settings, or with "
    "AGENT_CLOUD_PERSONAL_TEXT=1)."
)

# ``kind`` values -> activity event types. "actions" = the journal only.
KINDS: dict[str, frozenset[str]] = {
    "files": frozenset({"file"}),
    "apps": frozenset({"app_activated"}),
    "mail": frozenset({"mail_received"}),
    "messages": frozenset({"imessage_received"}),
    "clipboard": frozenset({"clipboard_changed"}),
    "calendar": frozenset({"event_starting_soon"}),
    "actions": frozenset(),
}

_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")

_activity: Any = None
_journal: Any = None
_tz: Optional[tzinfo] = None


def set_sources(*, activity: Any = None, journal: Any = None,
                tz: Optional[tzinfo] = None) -> None:
    """Inject the stores (the daemon's journal; fixtures in tests).

    ``activity`` needs ``rows(since)`` and ``enabled``; ``journal`` needs
    ``rows(limit)``. ``None`` restores the default for that source. ``tz``
    is the zone days and hours are read in (None = the system's).
    """
    global _activity, _journal, _tz
    _activity, _journal, _tz = activity, journal, tz


def _default_activity() -> Any:
    from ai import activity_store

    return activity_store.default_store()


# ---- path shortening (decision 5) -------------------------------------------


def short_folder(folder: str) -> str:
    """``folder`` with the home directory written as ``~``."""
    home = str(Path.home())
    if folder == home:
        return "~"
    if folder.startswith(home + os.sep):
        return "~" + folder[len(home):]
    return folder


def shorten_path(path: str) -> str:
    """A path as ``activity.db`` would keep it: folder + extension, no name.

    ``/Users/me/Downloads/acme-invoice.pdf`` -> ``~/Downloads/*.pdf``.
    """
    p = Path(str(path))
    ext = p.suffix if _EXT_RE.match(p.suffix) else ""
    return f"{short_folder(str(p.parent))}/*{ext.lower()}"


def shorten_label(tool_name: str, label: str) -> str:
    """A journal label without the file name.

    The journal writes a move as ``Moved <name> to <folder>``. A file name
    cannot hold ``/`` and the folder is absolute, so the first `` to /`` is
    the separator. A label that does not fit (clipped, or a move that did
    not report its paths) becomes ``Moved a file``: nothing is guessed.
    Labels of other tools hold no path and are returned unchanged.
    """
    if tool_name != "move_file":
        return label
    prefix, sep = "Moved ", " to /"
    cut = label.find(sep)
    if not label.startswith(prefix) or cut < len(prefix):
        return "Moved a file"
    name, folder = label[len(prefix):cut], label[cut + len(" to "):]
    ext = Path(name).suffix
    ext = ext.lower() if _EXT_RE.match(ext) else ""
    what = f"a {ext} file" if ext else "a file"
    return f"Moved {what} to {short_folder(folder)}"


def redact_result(result: Any, role: Optional[str]) -> Any:
    """A ``recall_activity`` result as the ``role`` engine may see it.

    Under a cloud engine without the personal-text opt-in
    (``ai.privacy.should_redact``) every line from the journal loses its
    file name and its ``why`` text; activity lines never held a name.
    """
    from ai import privacy

    if not isinstance(result, dict) or not privacy.should_redact(role):
        return result
    return shortened_result(result)


def shortened_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """``result`` with journal lines shortened (pure; no settings read)."""
    out = dict(result)
    lines = []
    changed = False
    for line in result.get("lines") or []:
        if isinstance(line, dict) and line.get("source") == "agent":
            line = dict(line)
            text = shorten_label(str(line.get("tool") or ""), str(line.get("label") or ""))
            line["label"] = text
            line["text"] = text + (" (undone)" if line.get("undone") else "")
            line.pop("why", None)
            changed = True
        lines.append(line)
    out["lines"] = lines
    if changed:
        out["notes"] = [*(result.get("notes") or []), REDACTED_NOTE]
    return out


# ---- describing rows ---------------------------------------------------------


def describe_activity(row: Mapping[str, Any]) -> str:
    """One stored activity row in words (allow-listed fields only)."""
    etype = row.get("event_type")
    if etype == "app_activated":
        verb = "opened" if row.get("launched") else "switched to"
        return f"{verb} {row.get('app') or 'an app'}"
    if etype == "file":
        what = f"{row.get('ext')} file" if row.get("ext") else "file"
        folder = row.get("folder") or "a folder"
        if row.get("action") == "renamed":
            src = row.get("from_folder")
            if src and src != folder:
                return f"{what} moved from {src} to {folder}"
            return f"{what} renamed in {folder}"
        return f"new {what} in {folder}"
    if etype == "mail_received":
        bulk = row.get("bulk")
        tail = "" if bulk is None else (" (bulk)" if bulk else " (from a person)")
        return f"mail arrived{tail}"
    if etype == "clipboard_changed":
        kind = row.get("kind") or "something"
        app = row.get("app")
        return f"copied {kind}" + (f" in {app}" if app else "")
    if etype == "event_starting_soon":
        return "a calendar event was about to start"
    if etype == "imessage_received":
        return "an iMessage arrived"
    return str(etype or "something happened")


def _summary(texts: list[str]) -> str:
    counts = Counter(texts)
    top = counts.most_common(SUMMARY_TOP)
    parts = [f"{n}x {text}" for text, n in top]
    rest = len(texts) - sum(n for _, n in top)
    if rest:
        parts.append(f"{rest} other")
    return f"{len(texts)} things: " + "; ".join(parts)


# ---- the timeline ------------------------------------------------------------


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message, "count": 0, "lines": []}


def _local(ts: float, tz: Optional[tzinfo]) -> datetime:
    return datetime.fromtimestamp(ts, tz) if tz is not None else datetime.fromtimestamp(ts)


def _midnight(day: date, tz: Optional[tzinfo]) -> datetime:
    naive = datetime(day.year, day.month, day.day)
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def _activity_lines(rows: list[tuple[datetime, str]], level: str) -> list[dict[str, Any]]:
    """Activity as lines. ``level``: "detail" (busy hours summed), "hour"
    (every hour summed) or "day" (every day summed)."""
    groups: dict[tuple[str, int], list[tuple[datetime, str]]] = {}
    for when, text in rows:
        key = (when.strftime("%Y-%m-%d"), -1 if level == "day" else when.hour)
        groups.setdefault(key, []).append((when, text))
    lines: list[dict[str, Any]] = []
    for (day, hour), items in sorted(groups.items()):
        summed = level != "detail" or len(items) > HOUR_DETAIL
        if not summed:
            lines += [{"time": when.strftime("%Y-%m-%d %H:%M"), "source": "activity",
                       "text": text, "_sort": when.timestamp()} for when, text in items]
            continue
        label = day if hour < 0 else f"{day} {hour:02d}:00-{hour:02d}:59"
        lines.append({"time": label, "source": "activity", "summed": len(items),
                      "text": _summary([text for _, text in items]),
                      "_sort": items[0][0].timestamp()})
    return lines


def build_timeline(day: str, until: str = "", kind: str = "", *, activity: Any = None,
                   journal: Any = None, tz: Optional[tzinfo] = None,
                   today: Optional[date] = None) -> dict[str, Any]:
    """The timeline for ``day`` .. ``until`` from the two given stores.

    ``activity`` None = no activity source; ``journal`` None = no journal.
    Never raises for bad input or a failing store: bad input is an error
    result, a failing store is a note.
    """
    try:
        first = parse_day(day, today)
        last = parse_day(until, today) if str(until or "").strip() else first
    except (ValueError, OverflowError) as exc:
        return _error(str(exc))
    if last < first:
        return _error("until is before day")
    days = (last - first).days + 1
    if days > MAX_DAYS:
        return _error(f"the range is {days} days; at most {MAX_DAYS} days at a time")
    kind = str(kind or "").strip().lower()
    if kind and kind not in KINDS:
        return _error(f"unknown kind {kind!r}; use one of {sorted(KINDS)}")
    if tz is None:
        tz = getattr(activity, "tz", None)
    start = _midnight(first, tz)
    end = _midnight(last + timedelta(days=1), tz)
    lo, hi = start.timestamp(), end.timestamp()
    notes: list[str] = []

    # What happened (activity.db): kinds and times, never a file name.
    history_on = activity is not None and bool(getattr(activity, "enabled", True))
    seen: list[tuple[datetime, str]] = []
    if not history_on:
        notes.append(HISTORY_OFF_NOTE)
    elif kind != "actions":
        try:
            rows = activity.rows(start)
        except Exception as exc:
            rows = []
            notes.append(f"Activity history could not be read: {str(exc)[:120]}")
        wanted = KINDS.get(kind)
        for row in rows:
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or not lo <= ts < hi:
                continue
            if wanted is not None and row.get("event_type") not in wanted:
                continue
            seen.append((_local(ts, tz), describe_activity(row)))

    # What the agent did (actions.db), newest 500 at most.
    did: list[dict[str, Any]] = []
    if journal is None:
        notes.append(NO_JOURNAL_NOTE)
    elif kind in ("", "actions", "files"):
        try:
            entries = journal.rows(JOURNAL_LIMIT)
        except Exception as exc:
            entries = []
            notes.append(f"The action journal could not be read: {str(exc)[:120]}")
        stamps = [e.get("created_at") for e in entries
                  if isinstance(e.get("created_at"), (int, float))]
        if len(entries) >= JOURNAL_LIMIT and stamps and min(stamps) > lo:
            notes.append(f"The journal returned only its newest {JOURNAL_LIMIT} "
                         "actions; earlier ones in this range are missing.")
        for entry in entries:
            ts = entry.get("created_at")
            if not isinstance(ts, (int, float)) or not lo <= ts < hi:
                continue
            if kind == "files" and entry.get("tool") != "move_file":
                continue
            undone = entry.get("undone_at") is not None
            label = str(entry.get("label") or entry.get("tool") or "did something")
            line = {"time": _local(ts, tz).strftime("%Y-%m-%d %H:%M"), "source": "agent",
                    "text": label + (" (undone)" if undone else ""), "label": label,
                    "tool": entry.get("tool"), "undone": undone, "_sort": float(ts)}
            if entry.get("why"):
                line["why"] = entry["why"]
            did.append(line)
        ref = today or (datetime.now(tz).date() if tz is not None else date.today())
        if (ref - first).days > JOURNAL_KEEPS_DAYS:
            notes.append(f"The journal keeps {JOURNAL_KEEPS_DAYS} days; older actions are gone.")

    truncated = False
    if len(did) > MAX_LINES:
        notes.append(f"{len(did) - MAX_LINES} earlier actions were left out "
                     f"(at most {MAX_LINES} lines).")
        did = sorted(did, key=lambda l: l["_sort"])[-MAX_LINES:]
        truncated = True
    lines: list[dict[str, Any]] = []
    for level in ("detail", "hour", "day"):
        lines = _activity_lines(seen, level)
        if len(lines) + len(did) <= MAX_LINES:
            break
    else:
        room = MAX_LINES - len(did)
        if len(lines) > room:
            notes.append("Some activity was left out to make room for what the agent did.")
            lines = lines[len(lines) - room:] if room > 0 else []
            truncated = True
    lines = sorted(lines + did, key=lambda l: l["_sort"])
    for line in lines:
        del line["_sort"]
    return {
        "status": "ok",
        "from": first.isoformat(),
        "to": last.isoformat(),
        "history": "on" if history_on else "off",
        "count": len(lines),
        "events": len(seen),
        "actions": len(did),
        "truncated": truncated,
        "notes": notes,
        "lines": lines,
    }


@tool
def recall_activity(day: str, until: str = "", kind: str = "") -> dict[str, Any]:
    """Recall what happened on this Mac on a past day: a timeline of activity and of what the agent did.

    Activity lines say what kind of thing happened and when (an app opened,
    a .pdf arrived in ~/Downloads, mail arrived) but never a file's name:
    use ``find_files(since=..., until=...)`` for names. Lines with source
    "agent" are actions the agent took (for example a file it moved, with
    its new folder) and say whether the action was undone. Example:
    ``recall_activity("2026-11-10", kind="files")``.

    Args:
        day: The day to recall, "YYYY-MM-DD" (local time), or "today" / "yesterday".
        until: Optional last day of a range, "YYYY-MM-DD", inclusive. At most 14 days. Empty means just ``day``.
        kind: Optional filter: "files", "apps", "mail", "messages", "clipboard", "calendar" or "actions" (only what the agent did). Empty means everything.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``from``, ``to``,
        ``history`` ("on", or "off" when activity history is switched off:
        then only the agent's actions are listed), ``notes`` and ``lines``
        (oldest first, at most 100), each ``{time, source, text}`` with
        ``source`` "activity" or "agent". A busy hour or day is one line
        with ``summed`` = how many things it stands for. Agent lines also
        have ``tool`` and ``undone``.
    """
    activity = _activity
    if activity is None:
        try:
            activity = _default_activity()
        except Exception:
            activity = None
    return build_timeline(day, until, kind, activity=activity, journal=_journal, tz=_tz)


__all__ = [
    "READ_ONLY", "MAX_DAYS", "MAX_LINES", "HOUR_DETAIL", "KINDS",
    "recall_activity", "build_timeline", "set_sources", "redact_result",
    "shortened_result", "shorten_label", "shorten_path", "short_folder",
    "describe_activity",
]
