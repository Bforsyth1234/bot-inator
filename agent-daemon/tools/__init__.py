"""Agent tools package.

Exports the list of available tools the orchestrator registers with smolagents.
"""

from __future__ import annotations

from .brief import brief_me
from .create_event import create_event
from .email_draft import draft_email
from .find_files import find_files
from .list_events import list_events
# #103: Mail's newest messages from the Envelope Index (read-only).
from .list_recent_emails import list_recent_emails
# #107: one capture, asked of a vision model (approval-gated).
from .look_at_screen import look_at_screen
from .meta_tool_generator import generate_custom_tool, repair_generated_tool
from .move_file import move_file
from .notes import append_note, create_note
from .open_url import open_url
# #102: built-ins a generated tool can't do in the sandbox (approval-gated).
from .quit_app import quit_app
from .read_active_tab import read_active_tab, read_active_tab_text
from .read_clipboard import read_clipboard
from .read_email import read_email
# #89: bound under another name so ``tools.read_front_window`` stays the
# module (``from tools import read_front_window as screen_text`` in main.py,
# the orchestrator and the tests needs its ``bridge`` and helpers).
from .read_front_window import read_front_window as _read_front_window_tool
from .reminders import create_reminder, list_reminders
from .remember_preference import remember_preference
from .send_imessage import send_imessage
from .shortcuts import list_shortcuts, run_shortcut
from .show_notification import show_notification
from .summarize_file import summarize_file
from .take_screenshot import take_screenshot
from .windows import list_windows, open_app
from .write_clipboard import write_clipboard

AVAILABLE_TOOLS = [
    read_active_tab,
    read_active_tab_text,
    # #89: the front window's text through the app's Accessibility bridge.
    _read_front_window_tool,
    open_url,
    show_notification,
    read_clipboard,
    write_clipboard,
    summarize_file,
    move_file,
    remember_preference,
    find_files,
    list_shortcuts,
    run_shortcut,
    create_note,
    append_note,
    list_windows,
    open_app,
    # #102: daemon-side, approval-gated (the sandbox blocks both).
    quit_app,
    take_screenshot,
    # #107: approval-gated; captures once, asks the vision model, deletes.
    look_at_screen,
    read_email,
    # #103: read-only; Mail refuses message data to sandboxed tools.
    list_recent_emails,
    draft_email,
    brief_me,
    list_events,
    create_event,
    list_reminders,
    create_reminder,
    generate_custom_tool,
    # #90: drafts a fix for a failing generated tool; NOT read-only (the
    # repair idea card, then the code-review diff card).
    repair_generated_tool,
]

# Tools whose side-effects are limited to reading local state (no file
# writes, no network, no notifications). The orchestrator invokes these
# without requesting user approval; see ``Orchestrator._wrap_tool_for_approval``.
# ``remember_preference`` writes only to the user's own local memory DB, so
# it also bypasses the approval gate.
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({
    "read_active_tab",
    "read_active_tab_text",
    # #89: reads only, only in a turn the user started (chat / quick ask).
    "read_front_window",
    "read_clipboard",
    "summarize_file",
    "remember_preference",
    "find_files",
    "list_shortcuts",
    "list_windows",
    "read_email",
    # #103: reads Mail's index only (subjects redacted for a cloud engine).
    "list_recent_emails",
    "list_events",
    "list_reminders",
    "brief_me",
})

__all__ = [
    "read_active_tab",
    "open_url",
    "show_notification",
    "read_clipboard",
    "write_clipboard",
    "summarize_file",
    "move_file",
    "send_imessage",
    "remember_preference",
    "find_files",
    "list_shortcuts",
    "run_shortcut",
    "create_note",
    "append_note",
    "list_windows",
    "open_app",
    "quit_app",
    "take_screenshot",
    "look_at_screen",
    "read_email",
    "list_recent_emails",
    "draft_email",
    "brief_me",
    "list_events",
    "create_event",
    "list_reminders",
    "create_reminder",
    "generate_custom_tool",
    "repair_generated_tool",
    "AVAILABLE_TOOLS",
    "READ_ONLY_TOOL_NAMES",
]
