"""Send an iMessage via the native macOS Messages app (AppleScript)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


_SCRIPT_TEMPLATE = '''
tell application "Messages"
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy {buddy} of targetService
    send {message} to targetBuddy
end tell
'''


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
    script = _SCRIPT_TEMPLATE.format(
        buddy=json.dumps(target_number),
        message=json.dumps(message),
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True, capture_output=True, text=True, timeout=10,
        )
    except subprocess.CalledProcessError as exc:
        return {
            "status": "error",
            "target": target_number,
            "error": (exc.stderr or str(exc)).strip(),
        }
    except Exception as exc:
        return {"status": "error", "target": target_number, "error": str(exc)}
    return {"status": "ok", "target": target_number}
