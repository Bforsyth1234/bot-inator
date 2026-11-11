"""The drafter is told what the tool sandbox can't do (todos #102, #103)."""

from __future__ import annotations

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools
from tools import meta_tool_generator as mtg


class _Recorder:
    def __init__(self) -> None:
        self.system = ""

    def generate_chat_sync(self, system: str, user: str, max_tokens: int = 0) -> str:
        self.system = system
        return '"""x."""\n'


def _drafting_system_prompt() -> str:
    engine = _Recorder()
    mtg._draft_with_engine(engine, "close_app", "Quit an app", "quit Spotify")
    return engine.system


def test_the_drafting_prompt_names_the_limits_and_the_builtins() -> None:
    prompt = _drafting_system_prompt()
    assert "Sandbox limits" in prompt
    for builtin in ("quit_app(app_name)", "take_screenshot(path)",
                    "list_recent_emails(count, mailbox)", "read_email(message_id)"):
        assert builtin in prompt, builtin
    assert "CANNOT quit or close apps" in prompt
    assert "CANNOT read Mail's messages" in prompt
    assert "screencapture" in prompt
    assert "CANNOT open URLs, files or apps except through `open`" in prompt


def test_the_new_builtins_are_listed_for_the_drafter() -> None:
    """The AVAILABLE TOOLS section lists them like every built-in."""
    section = mtg._format_available_tools_context()
    for name in ("quit_app", "take_screenshot", "list_recent_emails"):
        assert name in section


def test_generate_custom_tool_tells_the_agent_to_use_the_builtins() -> None:
    desc = tools.generate_custom_tool.description
    for name in ("quit_app", "take_screenshot", "list_recent_emails"):
        assert name in desc
