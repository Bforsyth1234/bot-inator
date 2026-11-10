"""#64: a plain-language summary leads the idea card and the code-review card.

Fake engines only; the generated dir is a ``tmp_path``; nothing touches
``~/.bot-inator`` (see ``conftest.py``) or a real model.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
from ai import privacy, tool_summary
from ai.orchestrator import Orchestrator
from config import settings
from events.event_bus import ContextEvent, EventBus
from routes import tool_detail
from schemas.ws_messages import ApprovalResponsePayload, CodeApprovalResponsePayload
from tools import meta_tool_generator as mtg

GOOD = (
    "DOES: Renames new screenshots on your Desktop to the date they were taken.\n"
    "WHEN: It runs when a new screenshot appears.\n"
    "TOUCHES: Your Desktop folder."
)
NAME = "rename_screenshots"
DESC = "Rename new screenshots to their date."
LOGIC = "Watch ~/Desktop for Screenshot*.png and rename each to YYYY-MM-DD.png."

_SRC = '''from smolagents import tool
import subprocess
import urllib.request
from pathlib import Path


@tool
def rename_screenshots() -> dict:
    """Rename new screenshots to their date."""
    folder = Path("~/Desktop").expanduser()
    subprocess.run(["osascript", "-e", 'tell application "Finder" to activate'])
    (folder / "log.txt").write_text("done")
    return {"status": "ok"}
'''


class FakeEngine:
    """Records prompts; ``reply`` is a string, an exception, or a callable."""

    loaded = True

    def __init__(self, reply: Any = GOOD) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, int]] = []

    def generate_chat_sync(self, system: str, user: str, max_tokens: int) -> str:
        self.calls.append((system, user, max_tokens))
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply() if callable(self.reply) else self.reply


# ---- summary text ----------------------------------------------------------


def test_summary_generated_for_an_idea_with_a_fake_engine():
    engine = FakeEngine()
    summary = tool_summary.summarize_idea(engine, NAME, DESC, LOGIC)
    assert summary.source == "engine"
    assert summary.text() == (
        "Renames new screenshots on your Desktop to the date they were taken. "
        "It runs when a new screenshot appears. "
        "It would touch your Desktop folder. "
        "Each use asks for your approval first."
    )
    assert len(engine.calls) == 1  # one call
    system, user, _ = engine.calls[0]
    assert system == tool_summary.SUMMARY_SYSTEM_PROMPT
    assert DESC in user and LOGIC in user


@pytest.mark.parametrize("reply", [
    RuntimeError("engine down"),
    "",
    "   \n",
    "<think>The tool renames files, so I should say that.</think>",
    "<think>\nDOES: something long enough here",
    "Sure! Here is a summary of the tool you asked about.",
    "DOES: ok\nWHEN: now",
], ids=["raises", "empty", "blank", "only_think", "open_think", "no_labels", "too_short"])
def test_fallback_when_the_engine_fails_or_returns_junk(reply):
    summary = tool_summary.summarize_idea(FakeEngine(reply), NAME, DESC, LOGIC)
    assert summary.source == "fallback"
    assert summary.text() == (
        "Rename new screenshots to their date. "
        + tool_summary.FALLBACK_WHEN + " "
        + tool_summary.FALLBACK_TOUCHES + " "
        + tool_summary.ASKS_FIRST
    )


def test_think_block_is_stripped_before_parsing():
    summary = tool_summary.summarize_idea(
        FakeEngine("<think>\nDOES: a wrong line from the reasoning\n</think>\n" + GOOD),
        NAME, DESC, LOGIC,
    )
    assert summary.source == "engine"
    assert summary.does.startswith("Renames new screenshots")


def test_slow_engine_times_out_to_the_fallback():
    release = threading.Event()

    def slow() -> str:
        release.wait(10)
        return GOOD

    started = time.monotonic()
    try:
        summary = tool_summary.summarize_idea(FakeEngine(slow), NAME, DESC, LOGIC, timeout=0.2)
    finally:
        release.set()
    assert time.monotonic() - started < 3
    assert summary.source == "fallback"


def test_unloaded_engine_is_not_asked():
    engine = FakeEngine()
    engine.loaded = False  # asking would load a model and stall the card
    assert tool_summary.summarize_idea(engine, NAME, DESC, LOGIC).source == "fallback"
    assert engine.calls == []


def test_no_code_identifiers_or_markup_in_the_summary():
    summary = tool_summary.summarize_idea(
        FakeEngine(
            "**DOES:** `rename_screenshots` renames files via `os_rename()` for you.\n"
            "WHEN: When file_created fires on the Desktop.\n"
            "TOUCHES: ~/Desktop"
        ),
        NAME, DESC, LOGIC,
    )
    text = summary.text()
    assert summary.source == "engine"
    assert "`" not in text and "*" not in text and "_" not in text
    assert "this tool renames files" in text.lower()
    # No description: the name is spelled as words.
    assert tool_summary.fallback_summary("tidy_downloads", "").does == "A new tool: tidy downloads."


def test_touches_come_from_the_drafted_source():
    info = tool_summary.touches_from_source(_SRC)
    assert info["network"] == "imports urllib.request"
    assert info["apps"] == ["Finder"]
    assert info["paths"] == ["~/Desktop"]
    assert info["writes_files"] is True
    assert tool_summary.touches_sentence(_SRC) == (
        "Its code uses the internet; controls Finder; can change files in ~/Desktop."
    )
    quiet = "from smolagents import tool\n\n@tool\ndef add(a: int, b: int) -> int:\n    return a + b\n"
    assert tool_summary.touches_sentence(quiet) == (
        "Its code has no internet access and names no other apps or folders."
    )
    argv = 'import subprocess\nsubprocess.run(["open", "-a", "Safari", "https://x"])\n' \
           'subprocess.run(["/usr/bin/shortcuts", "run", "Tidy"])\n'
    assert tool_summary.touches_from_source(argv)["apps"] == ["Safari", "Shortcuts"]
    assert tool_summary.touches_from_source("def broken(:")["apps"] == []


# ---- privacy (#31/#51/#52) -------------------------------------------------

MESSAGE = "Hey! Are we still on for dinner at 7 with Sam? Bring the wine"
QUOTE = "dinner at 7 with Sam"


@pytest.fixture()
def providers(monkeypatch):
    monkeypatch.delenv(privacy.OPT_IN_ENV, raising=False)
    monkeypatch.setattr(settings, "provider", "local")
    privacy.clear_recent_personal()

    def set_(**roles: str) -> None:
        for role in ("main", "analysis", "drafting"):
            monkeypatch.setattr(settings, f"provider_{role}", roles.get(role, ""))

    set_()
    yield set_
    privacy.clear_recent_personal()


def _personal_idea_prompt() -> str:
    privacy.note_personal_event(ContextEvent(
        event_type="imessage_received", metadata={"text": MESSAGE, "sender": "+15550100"},
    ))
    engine = FakeEngine()
    tool_summary.summarize_idea(
        engine, "plan_reminder",
        f"Remind the user about {QUOTE} before it starts.",
        f"When a message like {QUOTE} arrives, create a reminder 1 hour before.",
    )
    return engine.calls[0][1]


def test_personal_text_not_in_the_summary_prompt_under_a_cloud_engine(providers):
    providers(analysis="groq")
    prompt = _personal_idea_prompt()
    assert QUOTE not in prompt
    assert "dinner" not in prompt and "Sam" not in prompt
    assert privacy.PLACEHOLDER in prompt
    assert "create a reminder 1 hour before" in prompt  # the rest survives


def test_local_engine_and_opt_in_keep_the_idea_text(providers, monkeypatch):
    assert QUOTE in _personal_idea_prompt()  # local analysis engine
    providers(analysis="groq")
    monkeypatch.setenv(privacy.OPT_IN_ENV, "1")
    assert QUOTE in _personal_idea_prompt()  # #52 opt-in


# ---- the two cards, provenance and the detail route ------------------------


@pytest.fixture
def flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real Orchestrator + the real ``generate_custom_tool`` with the
    drafting step faked and a client that approves both cards."""
    gen = tmp_path / "generated"
    gen.mkdir()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    bus = EventBus()
    bus.bind_loop(loop)
    engine = FakeEngine()
    with patch.object(Orchestrator, "_build_agent", return_value=None):
        orch = Orchestrator(
            engine=object(), analysis_engine=engine, event_bus=bus, tools=[],
            generated_tools_dir=gen, approval_timeout=5, code_approval_timeout=5,
        )
    orch._current_event_id = "evt_flow"
    orch._turn_kind = "chat"
    orch.load_dynamic_tools = lambda *a, **k: []  # type: ignore[method-assign]
    frames: list[Any] = []
    state = SimpleNamespace(edited_args=None, edited_code=None)

    async def client(message: Any) -> None:
        frames.append(message)
        if message.type == "approval_request":
            orch.submit_approval_response(ApprovalResponsePayload(
                request_id=message.payload.request_id, approved=True,
                edited_args=state.edited_args,
            ))
        elif message.type == "code_approval_request":
            orch.submit_code_approval_response(CodeApprovalResponsePayload(
                request_id=message.payload.request_id, approved=True,
                edited_code=state.edited_code,
            ))

    orch.add_subscriber(client)
    monkeypatch.setattr(mtg, "_ORCHESTRATOR", orch)
    monkeypatch.setattr(mtg, "_ENGINE", object())
    monkeypatch.setattr(mtg, "_DRAFT_ENGINE", None)
    monkeypatch.setattr(mtg, "_GENERATED_DIR", gen)
    monkeypatch.setattr(mtg, "_CURRENT_IDEA", None)
    monkeypatch.setattr(mtg, "_draft_until_clean", lambda *a: (_SRC, 1))
    monkeypatch.setattr(mtg, "_git_commit", lambda *a: (True, "ok"))
    wrapped = orch._wrap_tool_for_approval(mtg.generate_custom_tool)

    def run(**kwargs: Any) -> dict:
        call = {"tool_name": NAME, "description": DESC, "expected_logic": LOGIC, **kwargs}
        return wrapped.forward(**call)

    def payload(kind: str) -> Any:
        return next(f.payload for f in frames if f.type == kind)

    yield SimpleNamespace(
        gen=gen, orch=orch, engine=engine, run=run, payload=payload, state=state,
    )
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


IDEA_TEXT = (
    "Renames new screenshots on your Desktop to the date they were taken. "
    "It runs when a new screenshot appears. "
    "It would touch your Desktop folder. "
    "Each use asks for your approval first."
)
REVIEW_TEXT = (
    "Renames new screenshots on your Desktop to the date they were taken. "
    "It runs when a new screenshot appears. "
    "Its code uses the internet; controls Finder; can change files in ~/Desktop. "
    "Each use asks for your approval first."
)


def test_summary_in_the_idea_approval_payload(flow):
    assert flow.run()["status"] == "ok"
    idea = flow.payload("approval_request")
    assert idea.tool_name == "generate_custom_tool"
    assert idea.summary == IDEA_TEXT
    # The technical spec is still on the card, below the summary.
    assert idea.tool_args["kwargs"]["expected_logic"] == LOGIC
    assert json.loads(idea.model_dump_json())["summary"] == IDEA_TEXT
    assert len(flow.engine.calls) == 1  # one engine call for both cards


def test_summary_in_the_code_review_payload_uses_the_source_for_touches(flow):
    assert flow.run()["status"] == "ok"
    review = flow.payload("code_approval_request")
    assert review.code == _SRC
    assert review.summary == REVIEW_TEXT
    assert review.description == DESC  # unchanged, shown below


def test_engine_failure_never_blocks_either_card(flow):
    flow.engine.reply = RuntimeError("engine down")
    assert flow.run()["status"] == "ok"
    idea = flow.payload("approval_request").summary
    review = flow.payload("code_approval_request").summary
    assert idea.startswith("Rename new screenshots to their date. ")
    assert idea.endswith(tool_summary.ASKS_FIRST)
    assert "Its code uses the internet; controls Finder" in review


def test_other_tools_carry_no_summary(flow):
    from smolagents import tool

    @tool
    def poke(target: str) -> str:
        """Poke a thing.

        Args:
            target: What to poke.
        """
        return target

    assert flow.orch._wrap_tool_for_approval(poke).forward(target="a") == "a"
    assert flow.payload("approval_request").summary is None
    assert flow.engine.calls == []


def test_summary_saved_in_provenance_and_returned_by_the_detail_route(flow):
    assert flow.run()["status"] == "ok"
    prov = json.loads(mtg.provenance_path(flow.gen, NAME).read_text())
    assert prov["summary"] == REVIEW_TEXT

    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(tool_detail.router)
    app.state.orchestrator = SimpleNamespace(tools=[], _dynamic_tool_names=set())
    app.state.generated_tools_dir = flow.gen
    client = TestClient(app)
    client.headers.update(auth.auth_header())
    body = client.get(f"/api/tools/{NAME}").json()
    assert body["plain_summary"] == REVIEW_TEXT
    assert body["summary"] == "Rename new screenshots to their date."  # docstring, as before

    # A tool installed before #64 (no "summary" in its provenance) and a
    # built-in both answer null.
    prov.pop("summary")
    mtg.provenance_path(flow.gen, NAME).write_text(json.dumps(prov))
    assert client.get(f"/api/tools/{NAME}").json()["plain_summary"] is None
    app.state.orchestrator = None
    assert client.get("/api/tools/read_clipboard").json()["plain_summary"] is None


def test_edited_idea_and_edited_code_are_summarized_as_installed(flow):
    edited_src = _SRC.replace("import urllib.request\n", "").replace("Finder", "Preview")
    flow.state.edited_args = {"kwargs": {
        "tool_name": NAME, "description": "Rename screenshots my way.", "expected_logic": LOGIC,
    }}
    flow.state.edited_code = edited_src
    with patch.object(mtg, "check_draft", return_value=[]):
        assert flow.run()["status"] == "ok"
    review = flow.payload("code_approval_request").summary
    # The engine summarized the old description: rebuilt from the edit.
    assert review.startswith("Rename screenshots my way. ")
    prov = json.loads(mtg.provenance_path(flow.gen, NAME).read_text())
    assert "has no internet access; controls Preview" in prov["summary"]
    assert prov["user_edited"] is True


def test_summary_is_an_optional_ws_field():
    """Frames without ``summary`` (other tools, older peers) still validate."""
    from schemas.ws_messages import ApprovalRequestPayload, CodeApprovalRequestPayload

    idea = ApprovalRequestPayload.model_validate({
        "request_id": "r", "event_id": "e", "tool_name": "open_url", "reasoning": "x",
    })
    code = CodeApprovalRequestPayload.model_validate({
        "request_id": "r", "event_id": "e", "tool_name": "t", "description": "d", "code": "c",
    })
    assert idea.summary is None and code.summary is None
