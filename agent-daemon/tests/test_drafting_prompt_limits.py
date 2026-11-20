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
                    "list_recent_emails(count, mailbox, since, until)", "read_email(message_id)"):
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


def test_the_drafting_prompt_names_every_folder_the_sandbox_closes() -> None:
    """#114: read from the profile's own lists, so the two stay in step."""
    from tools import tool_sandbox

    prompt = _drafting_system_prompt()
    closed = set(tool_sandbox._READ_DENIED_IN_HOME) | set(tool_sandbox._WRITE_DENIED_IN_HOME)
    assert closed >= {".bot-inator", ".ssh", "Library"}
    for name in closed:
        assert f"`~/{name}`" in prompt, name
    assert "CANNOT read or write `~/.bot-inator`" in prompt
    assert "shell start-up files" in prompt
    assert f"under `{tool_sandbox.TOOL_FILES_DIR}/`" in prompt
    assert "even when `expected_logic` names one of the refused folders" in prompt
    # After the sentence about "one of these" (the built-in tasks), which
    # must not read as covering the folders: its remedy is another one.
    limits = prompt[prompt.index("Sandbox limits"):prompt.index("User-visible output rules")]
    assert limits.index("If the requested tool needs one of these anyway") < limits.index("* Folders:")
    assert limits.rstrip().splitlines()[-1].startswith("* Folders:")


def test_generate_custom_tool_tells_the_agent_where_a_tool_keeps_its_files() -> None:
    """#114: the 2026-11-17 spec named ``~/.bot-inator``; the agent wrote it."""
    from tools import tool_sandbox

    desc = tools.generate_custom_tool.description
    for folder in ("~/.bot-inator", "~/.ssh", "~/Library"):
        assert folder in desc, folder
    assert f"{tool_sandbox.TOOL_FILES_DIR}/" in desc
    assert "When the tool needs a file of its own" in desc


def test_the_critic_does_not_send_the_file_back() -> None:
    """#114: the critic compares the code with the spec word for word, and
    the spec may name the folder the drafter was told to stay out of."""
    from tools import draft_critic, tool_sandbox

    prompt = draft_critic.CRITIC_SYSTEM_PROMPT
    skip = prompt[prompt.index("Do NOT report"):prompt.index("Every problem must")]
    assert f"{tool_sandbox.TOOL_FILES_DIR}/" in skip
    for folder in ("~/.bot-inator", "~/.ssh", "~/Library"):
        assert folder in skip, folder
