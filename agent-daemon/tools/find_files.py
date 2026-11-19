"""Search local files with Spotlight (``mdfind``). Read-only.

#126: ``since`` / ``until`` filter on the day a file arrived. The date
clause is built only from dates this module parsed (:func:`parse_day`),
never from the caller's text. One ``mdfind`` call asks for files whose
``kMDItemDateAdded`` *or* creation date is in the range; one ``mdls`` call
then reads ``kMDItemDateAdded`` for those candidates, and each file is kept
on its date added when Spotlight has one, else on its creation date. Each
result says which was used (``date_used``).
"""

from __future__ import annotations

import os
import re
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


# Shared decision 5 (#43-#47): this tool belongs in READ_ONLY_TOOL_NAMES.
READ_ONLY = {"find_files"}

MAX_RESULTS = 50
# Candidates stat()ed before sorting; bounds the cost of very broad queries.
MAX_CANDIDATES = 1000
MDFIND_TIMEOUT_S = 10

_KIND_FILTERS: dict[str, str] = {
    "pdf": 'kMDItemContentTypeTree == "com.adobe.pdf"',
    "image": 'kMDItemContentTypeTree == "public.image"',
    "document": '(kMDItemContentTypeTree == "public.text"'
                ' || kMDItemContentTypeTree == "public.composite-content")',
    "folder": 'kMDItemContentType == "public.folder"',
}


def _escape(text: str) -> str:
    """Escape a value for a double-quoted Spotlight query string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MDLS_TIMEOUT_S = 10
MDLS_CHUNK = 200
_NULL_MARKER = "(null)"
# Matches every file: the clause used when there are no words to match.
_ANY_FILE = 'kMDItemFSName == "*"'


def parse_day(text: Any, today: Optional[date] = None) -> date:
    """``YYYY-MM-DD`` (or "today" / "yesterday") as a date; else ValueError."""
    raw = str(text or "").strip().lower()
    if raw in ("today", "yesterday"):
        base = today or date.today()
        return base if raw == "today" else base - timedelta(days=1)
    if not _DAY_RE.match(raw):
        raise ValueError(f"not a date (use YYYY-MM-DD): {str(text)[:40]!r}")
    return date.fromisoformat(raw)  # ValueError for e.g. 2026-13-40


def day_start(day: date) -> datetime:
    """Local midnight at the start of ``day``, as an aware datetime."""
    return datetime(day.year, day.month, day.day).astimezone()


def _time_iso(moment: datetime) -> str:
    """A Spotlight ``$time.iso(...)`` literal; digits come from a datetime."""
    return moment.astimezone(timezone.utc).strftime("$time.iso(%Y-%m-%dT%H:%M:%SZ)")


def _date_clause(start: Optional[datetime], end: Optional[datetime]) -> str:
    """Files added, or created, in ``[start, end)``. Empty when both are None."""
    def one(attr: str) -> str:
        parts = []
        if start is not None:
            parts.append(f"{attr} >= {_time_iso(start)}")
        if end is not None:
            parts.append(f"{attr} < {_time_iso(end)}")
        return "(" + " && ".join(parts) + ")"

    if start is None and end is None:
        return ""
    return f"({one('kMDItemDateAdded')} || {one('kMDItemFSCreationDate')})"


def _build_query(query: str, kind: str, start: Optional[datetime] = None,
                 end: Optional[datetime] = None) -> str:
    parts: list[str] = []
    if query:
        q = _escape(query)
        parts.append(f'(kMDItemDisplayName == "*{q}*"cd || kMDItemTextContent == "{q}"cd)')
    if kind:
        parts.append(_KIND_FILTERS[kind])
    clause = _date_clause(start, end)
    if clause:
        parts.append(clause)
    return " && ".join(parts) if parts else _ANY_FILE


def _birthtime(st: os.stat_result) -> float:
    return float(getattr(st, "st_birthtime", st.st_mtime))


def _parse_mdls_date(value: str) -> Optional[float]:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except ValueError:
        return None


def _dates_added(paths: list[str]) -> dict[str, float]:
    """``kMDItemDateAdded`` per path (Unix time), for the paths that have one.

    Best effort: a failed or odd ``mdls`` reply leaves those paths out, and
    the caller falls back to the creation date for them.
    """
    out: dict[str, float] = {}
    for i in range(0, len(paths), MDLS_CHUNK):
        chunk = paths[i:i + MDLS_CHUNK]
        cmd = ["mdls", "-name", "kMDItemDateAdded", "-raw",
               "-nullMarker", _NULL_MARKER, *chunk]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=MDLS_TIMEOUT_S)
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        values = proc.stdout.split("\0")
        if values and values[-1] == "":
            values.pop()
        if len(values) != len(chunk):
            continue  # cannot pair values with paths: use creation dates
        for path, value in zip(chunk, values):
            ts = _parse_mdls_date(value)
            if ts is not None:
                out[path] = ts
    return out


def _apply_dates(results: list[dict[str, Any]], start: Optional[datetime],
                 end: Optional[datetime]) -> list[dict[str, Any]]:
    """Keep the results whose arrival date is in ``[start, end)``.

    The arrival date is the date added when Spotlight has one, else the
    creation date. Adds ``date`` and ``date_used`` to each kept result.
    """
    added = _dates_added([r["path"] for r in results])
    lo = start.timestamp() if start is not None else float("-inf")
    hi = end.timestamp() if end is not None else float("inf")
    kept: list[dict[str, Any]] = []
    for r in results:
        created = r.pop("_created")
        if r["path"] in added:
            when, used = added[r["path"]], "added"
        else:
            when, used = created, "created"
        if lo <= when < hi:
            kept.append({**r, "date": when, "date_used": used})
    return kept


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@tool
def find_files(query: str = "", folder: str = "", kind: str = "", limit: int = 20,
               since: str = "", until: str = "") -> dict[str, Any]:
    """Find local files by name, content or arrival date using macOS Spotlight.

    Results are sorted newest first. Files under ``~/Library`` are left
    out unless ``folder`` is inside ``~/Library``. Examples:
    ``find_files("invoice", kind="pdf", limit=5)``;
    ``find_files(folder="~/Downloads", kind="pdf", since="2026-11-10", until="2026-11-10")``
    for the PDFs that arrived in Downloads on that day.

    Args:
        query: Words to match against file names and contents. May be empty when a date or a folder is given.
        folder: Optional folder to search within (e.g. "~/Documents"). Empty searches everywhere.
        kind: Optional type filter: "pdf", "image", "document" or "folder". Empty means any type.
        limit: Maximum number of results to return (1-50, default 20).
        since: Optional first day, "YYYY-MM-DD" (local time): only files that arrived on or after it.
        until: Optional last day, "YYYY-MM-DD", inclusive: only files that arrived on or before it.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``count`` and ``results``,
        a list of ``{path, name, modified, size}`` where ``modified`` is a
        Unix timestamp and ``size`` is in bytes. With ``since`` or ``until``
        each result also has ``date`` (Unix timestamp of arrival) and
        ``date_used`` ("added" = the date it was added to its folder,
        "created" = its creation date, used when no date added is known),
        and the results are sorted by ``date``.
    """
    query = (query or "").strip()
    since = str(since or "").strip()
    until = str(until or "").strip()
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    try:
        if since:
            start = day_start(parse_day(since))
        if until:
            end = day_start(parse_day(until) + timedelta(days=1))
    except (ValueError, OverflowError) as exc:
        return {"status": "error", "error": str(exc), "count": 0, "results": []}
    if start is not None and end is not None and start >= end:
        return {"status": "error", "error": "since is after until",
                "count": 0, "results": []}
    dated = start is not None or end is not None
    if not query and not dated and not str(folder or "").strip():
        return {"status": "error",
                "error": "query is empty (give words, a folder, or since/until)",
                "count": 0, "results": []}
    kind = (kind or "").strip().lower()
    if kind and kind not in _KIND_FILTERS:
        return {
            "status": "error",
            "error": f"unknown kind {kind!r}; use one of {sorted(_KIND_FILTERS)}",
            "count": 0,
            "results": [],
        }
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, MAX_RESULTS))

    cmd = ["mdfind"]
    folder_path = ""
    if folder:
        folder_path = str(Path(folder).expanduser().resolve())
        if not os.path.isdir(folder_path):
            return {"status": "error", "error": f"not a folder: {folder_path}",
                    "count": 0, "results": []}
        cmd += ["-onlyin", folder_path]
    cmd.append(_build_query(query, kind, start, end))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=MDFIND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "mdfind timed out", "count": 0, "results": []}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "count": 0, "results": []}
    if proc.returncode != 0:
        return {"status": "error", "error": (proc.stderr or "mdfind failed").strip(),
                "count": 0, "results": []}

    library = str(Path.home() / "Library")
    keep_library = bool(folder_path) and _is_under(folder_path, library)

    results: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path or (not keep_library and _is_under(path, library)):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue  # vanished since Spotlight indexed it
        row = {"path": path, "name": os.path.basename(path),
               "modified": st.st_mtime, "size": st.st_size}
        if dated:
            row["_created"] = _birthtime(st)
        results.append(row)
        if len(results) >= MAX_CANDIDATES:
            break

    if dated:
        results = _apply_dates(results, start, end)
        results.sort(key=lambda r: r["date"], reverse=True)
    else:
        results.sort(key=lambda r: r["modified"], reverse=True)
    results = results[:limit]
    return {"status": "ok", "count": len(results), "results": results}
