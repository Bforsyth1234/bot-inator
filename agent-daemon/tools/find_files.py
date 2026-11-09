"""Search local files with Spotlight (``mdfind``). Read-only."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

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


def _build_query(query: str, kind: str) -> str:
    q = _escape(query)
    expr = f'(kMDItemDisplayName == "*{q}*"cd || kMDItemTextContent == "{q}"cd)'
    if kind:
        expr = f"{expr} && {_KIND_FILTERS[kind]}"
    return expr


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@tool
def find_files(query: str, folder: str = "", kind: str = "", limit: int = 20) -> dict[str, Any]:
    """Find local files by name or content using macOS Spotlight.

    Results are sorted newest first. Files under ``~/Library`` are left
    out unless ``folder`` is inside ``~/Library``. Example:
    ``find_files("invoice", kind="pdf", limit=5)``.

    Args:
        query: Words to match against file names and contents.
        folder: Optional folder to search within (e.g. "~/Documents"). Empty searches everywhere.
        kind: Optional type filter: "pdf", "image", "document" or "folder". Empty means any type.
        limit: Maximum number of results to return (1-50, default 20).

    Returns:
        A dict with ``status`` ("ok" or "error"), ``count`` and ``results``,
        a list of ``{path, name, modified, size}`` where ``modified`` is a
        Unix timestamp and ``size`` is in bytes.
    """
    query = (query or "").strip()
    if not query:
        return {"status": "error", "error": "query is empty", "count": 0, "results": []}
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
    cmd.append(_build_query(query, kind))

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
        results.append({"path": path, "name": os.path.basename(path),
                        "modified": st.st_mtime, "size": st.st_size})
        if len(results) >= MAX_CANDIDATES:
            break

    results.sort(key=lambda r: r["modified"], reverse=True)
    results = results[:limit]
    return {"status": "ok", "count": len(results), "results": results}
