"""Move a file into a destination directory, optionally under a new name."""

from __future__ import annotations

import shutil
from pathlib import Path, PurePath
from typing import Any, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


def target_name(src_name: str, new_name: Optional[str]) -> tuple[Optional[str], str]:
    """``(name, "")`` for the moved file's name, else ``(None, error)``.

    ``new_name`` must be a plain file name: no path separators, no leading
    dot, not ``.``/``..``. Without an extension it keeps the source's.
    Empty or None keeps the source's name.
    """
    name = (new_name or "").strip()
    if not name:
        return src_name, ""
    if "/" in name or "\\" in name or ":" in name or "\x00" in name:
        return None, "new_name must be a plain file name (no path separators)"
    if name.startswith(".") or name in {".", ".."}:
        return None, "new_name must not start with a dot"
    if not PurePath(name).suffix:
        name += PurePath(src_name).suffix
    return name, ""


@tool
def move_file(src: str, dst_dir: str, new_name: Optional[str] = None) -> dict[str, Any]:
    """Move a file into an existing destination directory, optionally renaming it.

    Args:
        src: Absolute path of the file to move.
        dst_dir: Absolute path of the target directory. Must already exist.
        new_name: Optional new file name (a plain name, no folders). Without
            an extension the file keeps its own. Omit to keep the current name.

    Returns:
        A dict with ``old_path``, ``new_path`` and ``status``.
    """
    src_p = Path(src).expanduser()
    dst_p = Path(dst_dir).expanduser()

    if not src_p.is_file():
        return {"old_path": str(src_p), "status": "error", "error": "src is not a file"}
    if not dst_p.is_dir():
        return {"old_path": str(src_p), "status": "error", "error": "dst_dir is not a directory"}
    name, error = target_name(src_p.name, new_name)
    if name is None:
        return {"old_path": str(src_p), "status": "error", "error": error}

    target = dst_p / name
    if target.exists() and target.resolve() != src_p.resolve():
        # Never overwrite: the user approved a move, not a replacement.
        return {"old_path": str(src_p), "status": "error",
                "error": f"a file named {name!r} already exists in dst_dir"}
    try:
        shutil.move(str(src_p), str(target))
    except Exception as exc:
        return {"old_path": str(src_p), "status": "error", "error": str(exc)}
    return {"old_path": str(src_p), "new_path": str(target), "status": "ok"}
