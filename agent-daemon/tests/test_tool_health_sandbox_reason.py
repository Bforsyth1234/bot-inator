"""#102 / #103: a not-repairable sandbox failure's reason reaches the reply,
and Apple Event -10004 from a sandboxed tool is the sandbox, not a permission."""
from __future__ import annotations

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import tool_health as th
from ai.idea_sources import IdeaState
from tools.tool_sandbox import ToolSandboxError


@pytest.fixture
def log() -> th.ToolHealthLog:
    return th.ToolHealthLog(path=None, state=IdeaState(path=None))


def _orch(log: th.ToolHealthLog) -> object:
    return type("O", (), {"tool_health": log, "generated_tools_dir": None})()


@pytest.mark.parametrize("message", [
    # The live failures: quit (Spotify) and Mail's message list, Automation granted.
    "RuntimeError: execution error: Spotify got an error: A privilege violation occurred. (-10004)",
    "CalledProcessError: osascript: Mail got an error: (-10004)",
])
def test_apple_event_minus_10004_is_the_sandbox_not_a_permission(message: str) -> None:
    ok, cat, reason = th.classify_failure(message, "RuntimeError")
    assert (ok, cat) == (False, th.CATEGORY_SANDBOX)
    assert reason == th.REASON_SANDBOX_APPLE_EVENT
    assert "Automation" in reason and "quit_app" in reason and "list_recent_emails" in reason


def test_a_denied_executable_names_take_screenshot() -> None:
    ok, cat, reason = th.classify_failure(
        "PermissionError: [Errno 1] Operation not permitted: '/usr/sbin/screencapture'",
        "PermissionError")
    assert (ok, cat) == (False, th.CATEGORY_SANDBOX)
    assert "take_screenshot" in reason


def test_a_real_automation_denial_is_still_a_permission() -> None:
    _, cat, _ = th.classify_failure("Not authorized to send Apple events to Notes. (-1743)")
    assert cat == th.CATEGORY_PERMISSION


def test_record_call_puts_the_reason_in_a_failed_result(log: th.ToolHealthLog) -> None:
    result = {"status": "error", "error": "execution error: A privilege violation occurred. (-10004)"}
    th.record_call(_orch(log), "close_app", result=result, args=["Spotify"])
    note = result[th.USER_NOTE_KEY]
    assert note.startswith("Tell the user in one line why close_app can't work:")
    assert th.REASON_SANDBOX_APPLE_EVENT in note and "\n" not in note
    # Even without a health log the reply gets it.
    bare = {"status": "error", "error": "PermissionError: [Errno 1] Operation not permitted"}
    th.record_call(object(), "capture_screenshot", result=bare)
    assert th.USER_NOTE_KEY in bare


def test_record_call_appends_the_reason_to_a_raised_error(log: th.ToolHealthLog) -> None:
    exc = ToolSandboxError(
        "PermissionError: [Errno 1] Operation not permitted: '/usr/sbin/screencapture'")
    th.record_call(_orch(log), "capture_screenshot", exc=exc)
    assert "take_screenshot" in str(exc)
    assert exc.child_error.startswith("PermissionError")  # the log keeps the raw line
    th.record_call(_orch(log), "capture_screenshot", exc=exc)
    assert str(exc).count("Tell the user") == 1


def test_repairable_and_ok_results_get_no_note(log: th.ToolHealthLog) -> None:
    repairable = {"status": "error", "error": "AttributeError: x"}
    ok = {"status": "ok"}
    th.record_call(_orch(log), "t", result=repairable)
    th.record_call(_orch(log), "t", result=ok)
    assert th.USER_NOTE_KEY not in repairable and th.USER_NOTE_KEY not in ok
