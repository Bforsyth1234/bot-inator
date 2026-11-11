"""#110: the iPhone remote sends the screenshot itself, and never a raw
``final_answer`` JSON blob as the reply.

``screencapture`` and ``osascript`` are faked (``subprocess.run`` is
replaced): no screen is captured and no iMessage is sent.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import orchestrator as orch_mod
from ai.egress_log import KINDS, EgressLog
from ai.orchestrator import _make_groq_model_class, unwrap_answer_blob
from events import remote_channel as rc
from events.event_bus import ContextEvent
from events.remote_channel import RemoteChannel
from smolagents.models import ChatMessage, ChatMessageToolCall, ChatMessageToolCallFunction
from tests.test_wave16_wiring import _orch

ts = importlib.import_module("tools.take_screenshot")
las = importlib.import_module("tools.look_at_screen")
si = importlib.import_module("tools.send_imessage")

OWN = "+14155550100"
ANSWER = ("I captured a screenshot and saved it to your Desktop "
          "at/screenshot_2026-11-11_12-49-18.png.")
# The exact text the 2026-11-11 log showed the re-ask returning.
LOGGED = ('{\n  "name": "final_answer",\n  "arguments": {\n    "answer": "'
          + ANSWER + '"\n  }\n}')


# ---- 1. unwrap -----------------------------------------------------------------------

def test_the_logged_blob_becomes_the_plain_answer() -> None:
    assert LOGGED.startswith('{\n  "name": "final_answer",\n  "arguments": {\n    "answer": '
                             '"I captured a screenshot and saved it to your Desktop at/'
                             'screenshot_2026-11-11_12-49-18.png."')
    assert unwrap_answer_blob(LOGGED) == ANSWER


@pytest.mark.parametrize("blob", [
    json.dumps({"name": "answer", "arguments": {"answer": ANSWER}}),
    json.dumps({"name": "final_answer", "arguments": json.dumps({"answer": ANSWER})}),
    "```json\n" + LOGGED + "\n```",
    json.dumps({"name": "final_answer", "arguments": {"answer": LOGGED}}),  # nested
    json.dumps({"type": "function", "function": {"name": "final_answer",
                                                 "arguments": {"answer": ANSWER}}}),
])
def test_other_blob_shapes_unwrap(blob: str) -> None:
    assert unwrap_answer_blob(blob) == ANSWER


@pytest.mark.parametrize("text", [
    "You have two meetings tomorrow.",
    '{"name": "create_reminder", "arguments": {"title": "x"}}',  # another tool
    '{"answer": "no name"}',
    '{"name": "final_answer", "arguments": {}}',
    "{not json}",
    "",
])
def test_anything_else_is_unchanged(text: str) -> None:
    assert unwrap_answer_blob(text) == text


class _FakeBase:
    def __init__(self, reply: ChatMessage, tool_free: str = "") -> None:
        self.reply = reply
        self.tool_free = tool_free

    def generate(self, messages, stop_sequences=None, response_format=None,
                 tools_to_call_from=None, **kwargs):
        if tools_to_call_from is None:
            return ChatMessage(role="assistant", content=self.tool_free)
        return self.reply


HISTORY = [{"role": "user", "content": "screenshot"}, {"role": "tool-response", "content": "ok"}]


def test_empty_reply_reask_blob_is_not_wrapped_again() -> None:
    m = _make_groq_model_class(_FakeBase)(ChatMessage(role="assistant", content=""), LOGGED)
    out = m.generate(HISTORY, tools_to_call_from=[object()])
    assert out.tool_calls[0].function.name == "final_answer"
    assert out.tool_calls[0].function.arguments == {"answer": ANSWER}


def test_plain_text_blob_and_blob_inside_final_answer_unwrap() -> None:
    m = _make_groq_model_class(_FakeBase)(ChatMessage(role="assistant", content=LOGGED))
    out = m.generate([], tools_to_call_from=[object()])
    assert out.tool_calls[0].function.arguments == {"answer": ANSWER}
    call = ChatMessageToolCall(id="c1", type="function", function=ChatMessageToolCallFunction(
        name="final_answer", arguments={"answer": LOGGED}))
    m = _make_groq_model_class(_FakeBase)(ChatMessage(role="assistant", content="", tool_calls=[call]))
    out = m.generate([], tools_to_call_from=[object()])
    assert out.tool_calls[0].function.arguments == {"answer": ANSWER}


def test_turn_reply_is_unwrapped_before_it_reaches_the_phone() -> None:
    remote = mock.Mock()
    orch = _orch(remote_channel=remote)

    async def run_agent(*_a: Any, **_k: Any) -> str:
        return LOGGED

    orch._run_agent = run_agent  # type: ignore[method-assign]
    with mock.patch.object(orch_mod.activity_store, "record"):
        asyncio.run(orch._handle_event(ContextEvent(event_type="user_message", metadata={
            "event_id": "remote_7", "text": "screenshot", "source": rc.REMOTE_SOURCE})))
    remote.send_reply.assert_called_once_with("remote_7", ANSWER)


# ---- 2. the screenshot goes to the phone --------------------------------------------

def _png(path: Path, size: tuple[int, int]) -> None:
    from PIL import Image

    Image.new("RGB", size, "white").save(path)


@pytest.fixture()
def phone(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """Remote on, a capture folder in tmp, faked screencapture and osascript."""
    home = Path(os.path.realpath(tmp_path)) / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setattr(ts, "_home", lambda: home)
    monkeypatch.setattr(ts, "_now", lambda: datetime(2026, 11, 11, 12, 49, 18))
    monkeypatch.setattr(ts, "_screen_recording_granted", lambda: True)
    monkeypatch.setattr(rc, "_capture_dir_override", home / "Pictures" / rc.CAPTURE_DIR_NAME)
    monkeypatch.setenv(rc.ENABLED_KEY, "1")
    monkeypatch.setenv(rc.HANDLE_KEY, OWN)
    sensitive: list[str] = []

    def resolve(target: str) -> Any:
        if sensitive:
            raise las.Refused(sensitive[0])
        return None, "the whole screen"

    monkeypatch.setattr(las, "resolve", resolve)
    captures: list[list[str]] = []
    osascripts: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        if argv[0] == ts.SCREENCAPTURE:
            captures.append(list(argv))
            _png(Path(argv[-1]), (3024, 1964))
        else:
            assert argv[0] == "osascript"
            osascripts.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    # One ``subprocess`` module: both screencapture and osascript are faked.
    monkeypatch.setattr(subprocess, "run", fake_run)
    egress = EgressLog(db_path=tmp_path / "egress.db")
    deleted_later: list[Path] = []
    ch = RemoteChannel(None, tmp_path / "none.db", egress=egress)
    ch._delete_later = deleted_later.append  # type: ignore[method-assign]
    yield SimpleNamespace(home=home, ch=ch, captures=captures, osascripts=osascripts,
                          egress=egress, deleted_later=deleted_later, sensitive=sensitive)
    rc.end_phone_turn()


def _turn(ph: SimpleNamespace, event_id: str, source: Any, tool_call) -> Any:
    orch = _orch(remote_channel=ph.ch)
    results: list[Any] = []

    async def run_agent(*_a: Any, **_k: Any) -> str:
        results.append(await asyncio.to_thread(tool_call))
        return "Here is your screen."

    orch._run_agent = run_agent  # type: ignore[method-assign]
    meta = {"event_id": event_id, "text": "show me my screen"}
    if source:
        meta["source"] = source
        ph.ch._open_turns.add(event_id)
    with mock.patch.object(orch_mod.activity_store, "record"):
        asyncio.run(orch._handle_event(ContextEvent(event_type="user_message", metadata=meta)))
    return results[0]


def test_phone_turn_sends_text_then_the_screenshot(phone) -> None:
    from PIL import Image

    sizes: list[tuple[int, int]] = []
    real_send_file = phone.ch._file_sender

    def file_sender(handle: str, path: str) -> bool:
        with Image.open(path) as im:
            sizes.append(im.size)
        return real_send_file(handle, path)

    phone.ch._file_sender = file_sender
    result = _turn(phone, "remote_9", rc.REMOTE_SOURCE, ts.take_screenshot)
    assert result["status"] == "ok" and result["sent_to_phone"] is True
    assert "path" not in result  # the user didn't ask to save one
    assert list((phone.home / "Desktop").iterdir()) == []
    text_argv, file_argv = phone.osascripts
    assert text_argv[-2:] == [OWN, rc.PREFIX + "Here is your screen."]
    sent_path = Path(file_argv[-1])
    assert file_argv[-3:-1] == ["--", OWN]
    assert sent_path.parent == rc.phone_capture_dir()
    script = " ".join(file_argv[:-3])
    assert str(sent_path) not in script and OWN not in script and "argv" in script
    assert max(sizes[0]) <= 1600  # scaled like #107
    rows = phone.egress.rows()
    images = [r for r in rows if r["kind"] == rc.KIND_IMESSAGE_IMAGE]
    assert len(images) == 1 and images[0]["bytes_out"] == sent_path.stat().st_size > 0
    assert phone.deleted_later == [sent_path]
    assert rc.KIND_IMESSAGE_IMAGE in KINDS


def test_saved_copy_kept_only_when_a_path_was_given(phone) -> None:
    result = _turn(phone, "remote_10", rc.REMOTE_SOURCE,
                   lambda: ts.take_screenshot(str(phone.home / "Desktop" / "mine.png")))
    assert result["path"] == str(phone.home / "Desktop" / "mine.png")
    assert (phone.home / "Desktop" / "mine.png").exists()
    assert len(phone.osascripts) == 2  # text, then the scaled copy


def test_mac_turn_sends_nothing(phone) -> None:
    result = _turn(phone, "msg_1", None, ts.take_screenshot)
    assert result == {"status": "ok", "path": str(
        phone.home / "Desktop" / "Screenshot 2026-11-11 at 12.49.18.png")}
    assert phone.osascripts == []
    assert list(rc.phone_capture_dir().iterdir()) == []


def test_sensitive_window_is_refused_before_capturing(phone) -> None:
    phone.sensitive.append("a password field has focus; nothing was captured")
    rc.begin_phone_turn("remote_11")
    assert "refused" in ts.card_note()
    result = ts.take_screenshot()
    assert result["status"] == "error" and "password field" in result["error"]
    assert phone.captures == []


def test_card_says_it_goes_to_the_phone(phone) -> None:
    assert ts.card_note().startswith("Captures everything visible")
    rc.begin_phone_turn("remote_12")
    assert ts.card_note() == "Takes a screenshot and sends it to your phone."


def test_path_outside_the_capture_folder_is_refused(phone, tmp_path) -> None:
    rc.begin_phone_turn("remote_13")
    outside = phone.home / "Desktop" / "secret.png"
    _png(outside, (10, 10))
    assert rc.add_capture(outside) is False
    assert rc.add_capture(str(rc.phone_capture_dir() / ".." / "x.png")) is False
    link = rc.phone_capture_dir() / "link.png"
    link.symlink_to(outside)
    assert rc.add_capture(link) is False
    assert rc.take_captures("remote_13") == []
    # Even a direct send of an outside file never reaches osascript.
    assert phone.ch._send_file(outside) is False
    assert phone.osascripts == [] and outside.exists()


def test_no_capture_is_queued_outside_a_phone_turn(phone) -> None:
    inside = rc.new_capture_path()
    _png(inside, (10, 10))
    assert rc.add_capture(inside) is False


def test_attachment_echo_row_is_not_a_command(phone) -> None:
    row = rc._Row(1, rc.ATTACHMENT_CHAR, True, rc.IMESSAGE_SERVICE, 1)
    assert phone.ch._is_command(row, {1}) is False


def test_look_at_screen_keeps_its_capture_for_the_phone(phone) -> None:
    src = phone.home / "look.png"
    _png(src, (1600, 1000))
    assert las._keep_for_phone(src) is False  # not a phone turn
    rc.begin_phone_turn("remote_14")
    assert las._keep_for_phone(src) is True
    [queued] = rc.take_captures("remote_14")
    assert queued.parent == rc.phone_capture_dir() and queued.read_bytes() == src.read_bytes()


def test_file_argv_passes_the_path_unchanged_through_real_osascript() -> None:
    """A real osascript that only echoes its argument (no Messages)."""
    path = "/Users/x/Pictures/bot-inator phone/it’s \"a\" → shot.png"
    argv = si.osascript_file_argv(OWN, path)
    assert argv[-1] == path and path not in " ".join(argv[:-1])
    echo = ["osascript", "-e", "on run argv", "-e", "return item 2 of argv", "-e", "end run",
            "--", OWN, path]
    try:
        out = subprocess.run(echo, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        pytest.skip("no osascript")
    assert out.stdout.rstrip("\n") == path
