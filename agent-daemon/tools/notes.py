"""Create and append to notes in the native macOS Notes app (AppleScript).

User text never becomes part of the AppleScript source. The scripts below are
fixed constants; the title, body and folder reach them as ``osascript``
arguments (``osascript -e <script> -- arg1 arg2 ...``) and are read with
``on run argv``, so quotes or ``end tell`` in the text are plain data.

Notes.app stores a note body as HTML. Text is therefore HTML-escaped
(``html.escape`` with ``quote=True``: ``& < > " '``) and each line wrapped in
``<div>...</div>`` (blank lines become ``<div><br></div>``, as Notes writes
them), so the text shows up literally instead of being read as markup.

Both tools change user data and are approval-gated: neither name belongs in
``tools.READ_ONLY_TOOL_NAMES``.
"""

from __future__ import annotations

import html
import subprocess
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


# Tool names in this module and which side of the approval gate they sit on.
GATED: frozenset[str] = frozenset({"create_note", "append_note"})
READ_ONLY: frozenset[str] = frozenset()

# The first-use Automation prompt blocks osascript until the user answers.
_TIMEOUT_S = 30

# argv: 1 = body HTML (its first line is the title), 2 = folder name or "".
_CREATE_SCRIPT = '''
on run argv
    set noteBody to item 1 of argv
    set folderName to item 2 of argv
    tell application "Notes"
        if folderName is "" then
            make new note with properties {body:noteBody}
        else
            if not (exists folder folderName) then return "NO_FOLDER"
            make new note at folder folderName with properties {body:noteBody}
        end if
    end tell
    return "OK"
end run
'''

# argv: 1 = exact note title, 2 = HTML to append.
_APPEND_SCRIPT = '''
on run argv
    set noteTitle to item 1 of argv
    set extraHtml to item 2 of argv
    tell application "Notes"
        set found to {}
        repeat with n in (every note whose name is noteTitle)
            considering case
                if (name of n as text) is noteTitle then set end of found to contents of n
            end considering
        end repeat
        if (count of found) is 0 then return "NOT_FOUND"
        if (count of found) > 1 then return "MULTIPLE"
        set theNote to item 1 of found
        set body of theNote to (body of theNote) & extraHtml
    end tell
    return "OK"
end run
'''


def _to_html(text: str) -> str:
    """Escape ``text`` and wrap each line in a ``<div>`` the way Notes does."""
    lines = text.splitlines() or [""]
    return "".join(
        f"<div>{html.escape(line, quote=True)}</div>" if line else "<div><br></div>"
        for line in lines
    )


def _run(script: str, *args: str) -> str:
    """Run a fixed AppleScript with ``args`` as argv; return trimmed stdout."""
    proc = subprocess.run(
        ["osascript", "-e", script, "--", *args],
        check=True, capture_output=True, text=True, timeout=_TIMEOUT_S,
    )
    return (proc.stdout or "").strip()


def _error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or str(exc)).strip()
    return str(exc)


@tool
def create_note(title: str, body: str, folder: str = "") -> dict[str, Any]:
    """Create a new note in the macOS Notes app.

    Args:
        title: Title of the note (its first line in Notes).
        body: Plain-text content of the note; line breaks are kept.
        folder: Optional name of an existing Notes folder to put the note in.
            Empty means the default folder. A folder that does not exist is
            an error, not created.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``title``, and, on
        failure, an ``error`` string.
    """
    note_html = f"<div><h1>{html.escape(title, quote=True)}</h1></div>" + _to_html(body)
    try:
        out = _run(_CREATE_SCRIPT, note_html, folder)
    except Exception as exc:
        return {"status": "error", "title": title, "error": _error(exc)}
    if out == "NO_FOLDER":
        return {"status": "error", "title": title, "error": f"No Notes folder named {folder!r}."}
    return {"status": "ok", "title": title}


@tool
def append_note(title: str, text: str) -> dict[str, Any]:
    """Append text to the end of an existing note in the macOS Notes app.

    Args:
        title: Exact title of the note to append to (case-sensitive). If no
            note has this title, nothing is created and an error is returned.
        text: Plain-text content to add at the end; line breaks are kept.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``title``, and, on
        failure, an ``error`` string.
    """
    try:
        out = _run(_APPEND_SCRIPT, title, _to_html(text))
    except Exception as exc:
        return {"status": "error", "title": title, "error": _error(exc)}
    if out == "NOT_FOUND":
        return {"status": "error", "title": title, "error": f"No note titled {title!r}."}
    if out == "MULTIPLE":
        return {"status": "error", "title": title,
                "error": f"More than one note is titled {title!r}; rename one first."}
    return {"status": "ok", "title": title}
