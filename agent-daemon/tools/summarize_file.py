"""Return metadata and a text preview of a local file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".html", ".css", ".sh", ".swift", ".go", ".rs", ".toml", ".ini",
    ".csv", ".tsv", ".xml",
}


@tool
def summarize_file(path: str, max_chars: int = 2000) -> dict[str, Any]:
    """Return metadata and a short text preview of a local text file.

    For recognised text suffixes the file is read as UTF-8 (errors
    replaced) and the first ``max_chars`` characters are returned along
    with line/word counts. Binary files are detected by suffix and only
    their size is returned.

    Args:
        path: Absolute path to the file.
        max_chars: Maximum number of preview characters to include.

    Returns:
        A dict with ``path``, ``size_bytes``, ``is_text``,
        ``line_count``, ``word_count``, ``preview`` and ``status``.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        return {"path": str(p), "status": "error", "error": "not a file"}

    size = p.stat().st_size
    is_text = p.suffix.lower() in _TEXT_SUFFIXES

    if not is_text:
        return {
            "path": str(p),
            "size_bytes": size,
            "is_text": False,
            "line_count": 0,
            "word_count": 0,
            "preview": "",
            "status": "ok",
        }

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"path": str(p), "status": "error", "error": str(exc)}

    preview = content[:max_chars]
    return {
        "path": str(p),
        "size_bytes": size,
        "is_text": True,
        "line_count": content.count("\n") + (1 if content and not content.endswith("\n") else 0),
        "word_count": len(content.split()),
        "preview": preview,
        "status": "ok",
    }
