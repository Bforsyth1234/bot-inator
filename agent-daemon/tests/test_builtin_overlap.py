"""#153: tool ideas a built-in already covers are refused before any card,
even when the user asked for a new tool; real new tools still get theirs."""
from __future__ import annotations

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tests.test_injected_shortcut import _FakeTool, _Turn, local_main  # noqa: F401
from tests.test_notices_privacy import _LoopThread, _drain
from tools import meta_tool_generator as mtg


@pytest.mark.parametrize("name, desc, builtin", [
    ("quit_application", "A tool to quit a specific macOS application by its bundle "
     "identifier or name.", "quit_app"),
    ("close_apps", "Close all running apps except Finder", "quit_app"),
    ("force_quit_frozen", "Force-quit the frontmost app when it hangs", "quit_app"),
    ("snap_window", "Take a screenshot of the front window", "take_screenshot"),
    ("screen_reader", "Read the text on the screen and summarize it", "look_at_screen"),
    ("inbox_digest", "List my 10 most recent unread emails", "list_recent_emails"),
    ("mail_summary", "Summarize the latest emails in my inbox", "list_recent_emails"),
    ("web_lookup", "Search the web for a query and return the top links", "web_search"),
    ("page_grabber", "Scrape a web page and return its text", "web_search"),
    ("phone_finder", "Look up a person's phone number", "find_contact"),
])
def test_ideas_a_builtin_covers_are_matched(name, desc, builtin) -> None:
    assert mtg.builtin_overlap(name, desc, "")[0] == builtin
    assert builtin in mtg.builtin_overlap_problem(name, desc, "")


@pytest.mark.parametrize("name, desc", [
    ("pomodoro_timer", "A 25 minute focus timer that notifies when done"),
    ("organize_screenshots", "Move screenshot files from the Desktop into a dated folder"),
    ("save_attachments", "Save attachments from emails into ~/Documents/bot-inator"),
    ("draft_reply", "Draft an email reply to the sender"),
    ("close_finder_tabs", "Close old Finder windows"),
    ("weather_now", "Call the Open-Meteo API for the current temperature"),
    ("focus_helper", "Start a focus session without quitting any apps"),
])
def test_real_new_tools_are_not_matched(name, desc) -> None:
    assert mtg.builtin_overlap(name, desc, "") == ("", "")
    assert mtg.builtin_overlap_problem(name, desc, "") == ""


def _idea_tool() -> _FakeTool:
    return _FakeTool("generate_custom_tool", {"tool_name": {}, "description": {}, "expected_logic": {}})


def test_quit_idea_is_refused_before_any_card_even_when_asked_for() -> None:
    tool = _idea_tool()
    with _LoopThread() as loop:
        turn = _Turn(loop, kind="chat", user_text="make a new tool that quits Spotify")
        result = turn.call(tool, tool_name="quit_application",
                           description="A tool to quit a specific macOS application by "
                           "its bundle identifier or name.",
                           expected_logic="Uses AppleScript to tell the app to quit.")
        _drain(loop)
    assert turn.cards == [] and tool.ran == []
    assert result["error"] == "builtin_covers_this"
    assert result["builtin"] == "quit_app"
    assert "quit_app" in result["message"]


def test_a_real_new_tool_idea_still_gets_its_card() -> None:
    tool = _idea_tool()
    with _LoopThread() as loop:
        turn = _Turn(loop, kind="chat", user_text="make a pomodoro timer tool")
        turn.call(tool, tool_name="pomodoro_timer",
                  description="A 25 minute focus timer that notifies when done",
                  expected_logic="Sleeps in a thread, then shows a notification.")
        _drain(loop)
    assert len(turn.cards) == 1
