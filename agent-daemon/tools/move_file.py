"""Move a file into a destination directory."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


@tool
def move_file(src: str, dst_dir: str) -> dict[str, Any]:
    """Move a file into an existing destination directory.

    Args:
        src: Absolute path of the file to move.
        dst_dir: Absolute path of the target directory. Must already exist.

    Returns:
        A dict with ``old_path``, ``new_path`` and ``status``.
    """
    src_p = Path(src).expanduser()
    dst_p = Path(dst_dir).expanduser()

    if not src_p.is_file():
        return {"old_path": str(src_p), "status": "error", "error": "src is not a file"}
    if not dst_p.is_dir():
        return {"old_path": str(src_p), "status": "error", "error": "dst_dir is not a directory"}

    target = dst_p / src_p.name
    try:
        shutil.move(str(src_p), str(target))
    except Exception as exc:
        return {"old_path": str(src_p), "status": "error", "error": str(exc)}
    return {"old_path": str(src_p), "new_path": str(target), "status": "ok"}
