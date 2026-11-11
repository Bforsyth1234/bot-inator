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

In a turn the user started from the phone (#110, the iPhone remote) the
capture is also sent to the phone after the reply: it is taken scaled to
about 1600 px (``ai.vision_engine.MAX_IMAGE_SIDE``, like #107) into the
remote channel's capture folder, which only the daemon writes to; the
Desktop copy is kept only when the user asked for one (a ``path`` was
given). Sensitive windows are refused first, as in #107 decision 4
(``look_at_screen.resolve("screen")``). The card says
:data:`PHONE_CARD_NOTE`.

Capturing other apps' windows needs Screen Recording for the daemon's
responsible process (Settings → Permissions → Screen Recording); without it
macOS gives only the desktop, and the result says so.
"""

from __future__ import annotations

import os
import shutil
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

PHONE_CARD_NOTE = "Takes a screenshot and sends it to your phone"
PHONE_RESULT_NOTE = (
    "The screenshot will be sent to the user's phone right after your reply; "
    "tell them it is on its way. It was not saved on the Mac."
)
PHONE_SAVED_NOTE = (
    "The screenshot will be sent to the user's phone right after your reply, "
    "and it was also saved on the Mac."
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


def _phone_turn() -> bool:
    try:
        from events import remote_channel

        return remote_channel.phone_turn_active()
    except Exception:
        return False


def _sensitive_refusal() -> str:
    """Why the screen may not be captured for the phone (#107 decision 4), or ""."""
    import importlib

    # ``tools.look_at_screen`` the module (the package rebinds the name to the tool).
    look_at_screen = importlib.import_module("tools.look_at_screen")
    try:
        look_at_screen.resolve("screen")
    except look_at_screen.Refused as exc:
        return str(exc)
    return ""


def _scale(path: Path) -> None:
    from ai import vision_engine

    vision_engine.prepare_image(path)


def card_note(path: str = "", *args: Any, **kwargs: Any) -> str:
    """One line for the approval card."""
    path = path or str(kwargs.get("path") or "")
    if _phone_turn():
        refused = _sensitive_refusal()
        if refused:
            return f"Would take a screenshot for your phone, but it is refused: {refused}."
        note = PHONE_CARD_NOTE
        if path.strip():
            target, why = check_path(path)
            where = str(target) if target is not None else f"{path!r} (refused: {why})"
            note += f", and saves it to {where}"
        return note + "."
    target, why = check_path(path)
    where = str(target) if target is not None else f"{path!r} (refused: {why})"
    return f"Captures everything visible on screen and saves it to {where}."


@tool
def take_screenshot(path: str = "") -> dict[str, Any]:
    """Take a screenshot of everything visible on screen and save it as a PNG.

    Use this whenever the user asks for a screenshot or screen capture. Do
    not write a new tool for it: a generated tool cannot capture the screen.
    When the user asks from their phone, the screenshot is sent to the phone.

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
    if _phone_turn():
        return _for_phone(path)
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


def _capture(target: Path) -> str:
    """Run screencapture into ``target``; ``""`` on success, else the error."""
    try:
        proc = subprocess.run(
            [SCREENCAPTURE, "-x", str(target)],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return f"screencapture failed: {exc}"
    try:
        size = target.stat().st_size
    except OSError:
        size = 0
    if proc.returncode != 0 or size == 0:
        detail = (proc.stderr or "").strip() or f"screencapture exited {proc.returncode}"
        return f"no screenshot was saved ({detail}). {PERMISSION_HINT}"
    return ""


def _for_phone(path: str) -> dict[str, Any]:
    """#110: capture for the phone. Scaled, into the capture folder, queued
    for the reply; a Desktop copy only when a path was given."""
    from events import remote_channel

    refused = _sensitive_refusal()
    if refused:
        return {"status": "error", "error": f"refused: {refused}"}
    saved: Optional[Path] = None
    if path.strip():
        saved, why = check_path(path)
        if saved is None:
            return {"status": "error", "error": f"refused: {why}"}
    granted = _screen_recording_granted()
    phone_path = remote_channel.new_capture_path()
    error = _capture(saved or phone_path)
    if error:
        return {"status": "error", "error": error}
    try:
        if saved is not None:
            shutil.copyfile(saved, phone_path)
        _scale(phone_path)
    except Exception as exc:
        remote_channel._delete(phone_path)
        return {"status": "error", "error": f"the screenshot could not be prepared: {exc}"}
    if not remote_channel.add_capture(phone_path):
        remote_channel._delete(phone_path)
        return {"status": "error", "error": "the screenshot could not be queued for the phone"}
    result: dict[str, Any] = {"status": "ok", "sent_to_phone": True}
    if saved is not None:
        result["path"] = str(saved)
        result["note"] = PHONE_SAVED_NOTE
    else:
        result["note"] = PHONE_RESULT_NOTE
    if granted is False:
        result["note"] += (
            " Screen Recording isn't granted, so the image may show only the "
            "desktop. " + PERMISSION_HINT
        )
    return result


# Read by the approval gate (``Orchestrator._wrap_tool_for_approval``).
take_screenshot.card_note = card_note  # type: ignore[attr-defined]
