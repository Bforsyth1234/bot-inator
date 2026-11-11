"""Take a screenshot of the whole screen to a PNG (todo #102).

A generated tool cannot do this: the tool sandbox only lets it run the
executables in ``tools.tool_sandbox.ALLOWED_EXECUTABLES``, and
``/usr/sbin/screencapture`` is deliberately not one (any generated tool
could then capture the screen silently, and a network-allowed one could
send it off the Mac). So the daemon runs it, after the user approves the
card (rule 7a: approval-gated, not in ``READ_ONLY_TOOL_NAMES``).

``take_screenshot`` runs ``/usr/sbin/screencapture -x`` (no sound) to
``~/Desktop/Screenshot <date> at <time>.png`` by default. A given path must
be under the home folder and outside the places the tool sandbox denies
writes to (``~/Library``, ``~/.ssh``, ``~/.bot-inator``, shell start-up
files, the bot-inator repo). It never overwrites a file.

Capturing other apps' windows needs Screen Recording for the daemon's
responsible process (Settings → Permissions → Screen Recording); without it
macOS gives only the desktop, and the result says so.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn

from .quit_app import in_tool_sandbox

GATED: frozenset[str] = frozenset({"take_screenshot"})

SCREENCAPTURE = "/usr/sbin/screencapture"
TIMEOUT_SECONDS = 20.0
PERMISSION_HINT = (
    "Grant Screen Recording to the daemon in Settings → Permissions → "
    "Screen Recording, then try again."
)
SANDBOX_NOTE = (
    "take_screenshot can't run inside a generated tool (the tool sandbox "
    "does not allow screencapture). Call the take_screenshot tool directly instead."
)

# Tests replace these.
_now: Callable[[], datetime] = datetime.now


def _home() -> Path:
    return Path(os.path.realpath(Path.home()))


def default_path() -> Path:
    stamp = _now().strftime("%Y-%m-%d at %H.%M.%S")
    return _home() / "Desktop" / f"Screenshot {stamp}.png"


def _write_denied(home: Path) -> tuple[list[Path], list[Path]]:
    """``(denied subtrees, denied files)``: the sandbox's own write-deny list."""
    from . import tool_sandbox as ts

    daemon = Path(os.path.realpath(ts._DAEMON_ROOT))
    trees = [home / d for d in ts._WRITE_DENIED_IN_HOME]
    trees.append(ts._protected_repo_dir(home))
    trees.append(daemon / "tools" / "generated")
    files = [home / f for f in ts._SHELL_STARTUP_FILES]
    return trees, files


def _fold(path: Path) -> str:
    # The Mac's disk is case-insensitive: ~/library is ~/Library.
    return os.path.normpath(str(path)).casefold()


def _within(path: Path, tree: Path) -> bool:
    p, t = _fold(path), _fold(tree)
    return p == t or p.startswith(t.rstrip("/") + "/")


def check_path(path: str) -> tuple[Optional[Path], str]:
    """``(target, "")`` for an allowed screenshot path, else ``(None, reason)``."""
    raw = (path or "").strip()
    if not raw:
        return default_path(), ""
    if "\x00" in raw:
        return None, "path contains a NUL byte"
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return None, "path must be absolute (or start with ~)"
    if not expanded.suffix:
        expanded = expanded.with_suffix(".png")
    if expanded.suffix.lower() != ".png":
        return None, "path must end in .png"
    parent = Path(os.path.realpath(expanded.parent))
    target = parent / expanded.name
    home = _home()
    if not _within(parent, home):
        return None, "path must be inside your home folder"
    trees, files = _write_denied(home)
    for tree in trees:
        if _within(target, tree):
            return None, f"path is in a protected folder ({tree})"
    if any(_fold(target) == _fold(f) for f in files):
        return None, "path is a protected file"
    if not parent.is_dir():
        return None, f"folder {parent} does not exist"
    if target.exists() or target.is_symlink():
        return None, f"{target.name} already exists (it is never overwritten)"
    return target, ""


def _screen_recording_granted() -> Optional[bool]:
    """``CGPreflightScreenCaptureAccess`` (never prompts); ``None`` if unknown."""
    try:
        import Quartz  # type: ignore

        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def card_note(path: str = "", *args: Any, **kwargs: Any) -> str:
    """One line for the approval card."""
    path = path or str(kwargs.get("path") or "")
    target, why = check_path(path)
    where = str(target) if target is not None else f"{path!r} (refused: {why})"
    return f"Captures everything visible on screen and saves it to {where}."


@tool
def take_screenshot(path: str = "") -> dict[str, Any]:
    """Take a screenshot of everything visible on screen and save it as a PNG.

    Use this whenever the user asks for a screenshot or screen capture. Do
    not write a new tool for it: a generated tool cannot capture the screen.

    Args:
        path: Optional absolute path (``~`` allowed) for the PNG, inside the
            home folder and not in ~/Library. Empty saves to
            ``~/Desktop/Screenshot <date> at <time>.png``. Never overwrites.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``path`` of the saved PNG
        on success (and ``note`` when Screen Recording is not granted), or
        ``error``.
    """
    if in_tool_sandbox():
        return {"status": "error", "error": SANDBOX_NOTE}
    target, why = check_path(path)
    if target is None:
        return {"status": "error", "error": f"refused: {why}"}
    granted = _screen_recording_granted()
    try:
        proc = subprocess.run(
            [SCREENCAPTURE, "-x", str(target)],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return {"status": "error", "error": f"screencapture failed: {exc}"}
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    if proc.returncode != 0 or size == 0:
        detail = (proc.stderr or "").strip() or f"screencapture exited {proc.returncode}"
        return {"status": "error",
                "error": f"no screenshot was saved ({detail}). {PERMISSION_HINT}"}
    result: dict[str, Any] = {"status": "ok", "path": str(target)}
    if granted is False:
        result["note"] = (
            "Screen Recording isn't granted, so the image may show only the "
            "desktop, without other apps' windows. " + PERMISSION_HINT
        )
    return result


# Read by the approval gate (``Orchestrator._wrap_tool_for_approval``).
take_screenshot.card_note = card_note  # type: ignore[attr-defined]
