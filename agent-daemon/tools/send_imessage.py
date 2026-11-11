"""Send an iMessage via the native macOS Messages app (AppleScript)."""

from __future__ import annotations

import subprocess
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


# The script never contains data: handle and body arrive as ``argv`` (#106).
# Building AppleScript source from data broke on any non-ASCII text
# (json.dumps writes \uXXXX, which AppleScript can't parse: -2741) and let
# a message's text inject AppleScript.
_SCRIPT_LINES: tuple[str, ...] = (
    "on run argv",
    'tell application "Messages"',
    "set targetService to 1st service whose service type = iMessage",
    "set targetBuddy to buddy (item 1 of argv) of targetService",
    "send (item 2 of argv) to targetBuddy",
    "end tell",
    "end run",
)


def osascript_argv(handle: str, body: str) -> list[str]:
    """The ``osascript`` command line that sends ``body`` to ``handle``."""
    argv = ["osascript"]
    for line in _SCRIPT_LINES:
        argv += ["-e", line]
    return argv + ["--", str(handle), str(body)]


def osascript_send(handle: str, body: str, timeout: float = 10) -> None:
    """Send ``body`` to ``handle`` through Messages. Raises on failure."""
    subprocess.run(osascript_argv(handle, body), check=True,
                   capture_output=True, text=True, timeout=timeout)


@tool
def send_imessage(target_number: str, message: str) -> dict[str, Any]:
    """Send an iMessage to the given phone number or Apple ID.

    Args:
        target_number: Phone number in E.164 form (``"+14155551212"``) or
            an Apple ID email address for the recipient.
        message: Plain-text message body to send.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``target`` echoing the
        recipient, and, on failure, an ``error`` string.
    """
    try:
        osascript_send(target_number, message)
    except subprocess.CalledProcessError as exc:
        return {
            "status": "error",
            "target": target_number,
            "error": (exc.stderr or str(exc)).strip(),
        }
    except Exception as exc:
        return {"status": "error", "target": target_number, "error": str(exc)}
    return {"status": "ok", "target": target_number}
