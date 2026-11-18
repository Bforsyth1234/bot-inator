"""Wave 25 runner wiring (#123): delete ideas refused before any card;
the safety scorer reads ``allow_if``."""
from __future__ import annotations

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tests.test_injected_shortcut import _FakeTool, _Turn, local_main  # noqa: F401
from tests.test_notices_privacy import _LoopThread, _drain


def _idea_tool() -> _FakeTool:
    return _FakeTool("generate_custom_tool", {"tool_name": {}, "description": {}, "expected_logic": {}})


def test_reminder_delete_idea_is_refused_before_any_card() -> None:
    tool = _idea_tool()
    with _LoopThread() as loop:
        turn = _Turn(loop, kind="chat", user_text="Clear all my reminders")
        result = turn.call(tool, tool_name="clear_reminders",
                           description="Delete all reminders from the Reminders app",
                           expected_logic="Loops over every reminder and deletes it.")
        _drain(loop)
    assert turn.cards == [] and tool.ran == []
    assert result["error"] == "delete_not_recoverable"


def test_trash_file_idea_still_gets_its_card() -> None:
    tool = _idea_tool()
    with _LoopThread() as loop:
        turn = _Turn(loop, kind="chat", user_text="make a tool that trashes old dmg files")
        turn.call(tool, tool_name="trash_old_dmgs",
                  description="Move .dmg files older than 30 days in Downloads to the Trash",
                  expected_logic="Finds them and calls move_to_trash.")
        _drain(loop)
    assert len(turn.cards) == 1
