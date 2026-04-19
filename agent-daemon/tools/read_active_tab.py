"""Query the currently active browser tab via AppleScript.

Supports Google Chrome and Safari. Tries each in turn and returns the
first one with an open window. Requires macOS Automation permission the
first time the daemon drives the browser.
"""

from __future__ import annotations

import subprocess
from typing import Any, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        """Fallback no-op decorator when smolagents is unavailable."""
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


_CHROME_SCRIPT = '''
if application "Google Chrome" is running then
    tell application "Google Chrome"
        if (count windows) > 0 then
            set theTab to active tab of front window
            return (URL of theTab) & linefeed & (title of theTab)
        end if
    end tell
end if
return ""
'''

_SAFARI_SCRIPT = '''
if application "Safari" is running then
    tell application "Safari"
        if (count windows) > 0 then
            set theTab to current tab of front window
            return (URL of theTab) & linefeed & (name of theTab)
        end if
    end tell
end if
return ""
'''


def _run_osascript(script: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _query(browser: str, script: str) -> Optional[dict[str, Any]]:
    output = _run_osascript(script)
    if not output:
        return None
    parts = output.split("\n", 1)
    if len(parts) < 2:
        return None
    url, title = parts[0].strip(), parts[1].strip()
    if not url:
        return None
    return {"url": url, "title": title, "browser": browser}


@tool
def read_active_tab() -> dict[str, Any]:
    """Return metadata about the frontmost browser's active tab.

    Tries Google Chrome first, then Safari. Returns an empty payload with
    an ``error`` field when no supported browser has an open window.

    Returns:
        A dict with the active tab's ``url``, ``title`` and ``browser``,
        or ``{"url": "", "title": "", "browser": "", "error": "..."}``.
    """
    for browser, script in (
        ("Google Chrome", _CHROME_SCRIPT),
        ("Safari", _SAFARI_SCRIPT),
    ):
        found = _query(browser, script)
        if found is not None:
            return found
    return {
        "url": "",
        "title": "",
        "browser": "",
        "error": "no supported browser with an open window",
    }
