"""Wave 5: Full Disk Access notice (#19), tools_changed (#29), and keeping
personal text out of cloud-engine prompts (#31)."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_integration import client  # noqa: F401  - pytest fixture

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import privacy
from ai.idea_sources import IdeaState, NextStepSuggester
from ai.mlx_engine import MLXEngine
from ai.open_help import OpenHelpSuggester, _describe as open_help_describe
from ai.orchestrator import Orchestrator
from ai.pattern_recognizer import PatternRecognizer
from config import settings
from events.event_bus import ContextEvent, EventBus
from events.imessage_watcher import IMessageWatcher
from events.mail_watcher import MailWatcher
from schemas.ws_messages import (
    NOTICE_EVENT_TYPE,
    Notice,
    ToolsChanged,
    full_disk_access_notice,
)

PH = privacy.PLACEHOLDER
SECRET_TEXT = "dinner tomorrow at 7pm? my door code is 4417"
SECRET_SUBJECT = "Invitation: Biopsy results review @ 3pm"
SECRET_BODY = "Your lab results are attached."


# ---- shared helpers ------------------------------------------------------


class _LoopThread:
    """An event loop on a background thread (like the daemon's)."""

    def __enter__(self) -> asyncio.AbstractEventLoop:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc: Any) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.loop.close()


def _drain(loop: asyncio.AbstractEventLoop) -> None:
    for _ in range(3):
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(2)


def _orchestrator(loop: asyncio.AbstractEventLoop, tmp_path: Path | None = None,
                  **kwargs: Any) -> tuple[Orchestrator, list[Any]]:
    bus = EventBus()
    bus.bind_loop(loop)
    orch = Orchestrator(engine=MLXEngine(), event_bus=bus, tools=[],
                        generated_tools_dir=tmp_path, **kwargs)
    frames: list[Any] = []

    async def sink(msg: Any) -> None:
        frames.append(msg)

    orch.add_subscriber(sink)
    return orch, frames


def _bus_events(bus: EventBus) -> list[ContextEvent]:
    out = []
    while bus.qsize():
        out.append(bus._queue.get_nowait())
    return out


# ---- #19 notice ------------------------------------------------------------


def _unreadable_file(path: Path) -> Path:
    path.write_bytes(b"not a database")
    path.chmod(0)
    return path


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_imessage_watcher_publishes_fda_notice_once(tmp_path) -> None:
    db = _unreadable_file(tmp_path / "chat.db")
    with _LoopThread() as loop:
        bus = EventBus()
        bus.bind_loop(loop)
        watcher = IMessageWatcher(bus, db_path=db)
        assert watcher._open() is None
        assert watcher._open() is None  # a second failure: no second notice
        _drain(loop)
        events = _bus_events(bus)
    db.chmod(0o600)
    assert [e.event_type for e in events] == [NOTICE_EVENT_TYPE]
    notice = events[0].metadata["notice"]
    assert notice["id"] == "full_disk_access:imessage_watcher"
    assert notice["action"]["kind"] == "open_settings"
    assert "Privacy_AllFiles" in notice["action"]["url"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_mail_watcher_publishes_fda_notice_once(tmp_path) -> None:
    root = tmp_path / "Mail"
    (root / "V10" / "MailData").mkdir(parents=True)
    root.chmod(0)  # what ~/Library/Mail looks like without Full Disk Access
    try:
        with _LoopThread() as loop:
            bus = EventBus()
            bus.bind_loop(loop)
            watcher = MailWatcher(bus, mail_root=root)
            assert watcher._open() is None
            assert watcher._open() is None
            _drain(loop)
            events = _bus_events(bus)
    finally:
        root.chmod(0o700)
    assert [e.metadata["notice"]["id"] for e in events] == ["full_disk_access:mail_watcher"]


def test_mail_watcher_missing_store_is_not_an_fda_notice(tmp_path) -> None:
    with _LoopThread() as loop:
        bus = EventBus()
        bus.bind_loop(loop)
        assert MailWatcher(bus, mail_root=tmp_path / "nope")._open() is None
        _drain(loop)
        assert _bus_events(bus) == []


def test_orchestrator_publishes_each_notice_once_and_replays(tmp_path) -> None:
    with _LoopThread() as loop:
        orch, frames = _orchestrator(loop)
        notice = full_disk_access_notice("imessage_watcher", "iMessages").model_dump()
        event = ContextEvent(event_type=NOTICE_EVENT_TYPE, metadata={"notice": notice})
        for _ in range(3):
            asyncio.run_coroutine_threadsafe(orch._handle_event(event), loop).result(2)
        _drain(loop)
    notices = [f for f in frames if isinstance(f, Notice)]
    assert [n.payload.id for n in notices] == ["full_disk_access:imessage_watcher"]
    # No thought, no observers: a notice is not an observation.
    assert len(frames) == 1
    replay = orch.notice_frames()
    assert [f.payload.id for f in replay] == ["full_disk_access:imessage_watcher"]
    assert replay[0].seq != notices[0].seq  # a fresh frame per replay


def test_notice_reaches_connected_and_later_clients(client) -> None:  # noqa: F811
    bus: EventBus = client.app.state.event_bus
    fda = full_disk_access_notice("mail_watcher", "Mail").model_dump()
    probe = {"id": "probe", "title": "probe", "body": "probe"}
    with client.websocket_connect("/ws/stream") as ws1:
        hello = ws1.receive_json()
        assert hello["type"] == "status"
        for _ in range(2):
            bus.push_threadsafe(ContextEvent(event_type=NOTICE_EVENT_TYPE,
                                             metadata={"notice": fda}))
        bus.push_threadsafe(ContextEvent(event_type=NOTICE_EVENT_TYPE,
                                         metadata={"notice": probe}))
        got = [ws1.receive_json() for _ in range(2)]
        # Pushed twice, broadcast once: the next frame is the probe.
        assert [(f["type"], f["payload"]["id"]) for f in got] == [
            ("notice", "full_disk_access:mail_watcher"), ("notice", "probe"),
        ]
    with client.websocket_connect("/ws/stream") as ws2:
        assert ws2.receive_json()["type"] == "status"
        replayed = [ws2.receive_json()["payload"]["id"] for _ in range(2)]
        assert replayed == ["full_disk_access:mail_watcher", "probe"]
    client.app.state.orchestrator._notices.clear()


def test_status_hello_carries_providers_and_listener_health(client, monkeypatch) -> None:  # noqa: F811
    class _Mac:
        def status(self) -> dict[str, Any]:
            return {"alive": True, "pid": 42, "restart_count": 3,
                    "last_error": None, "last_exit_code": None, "uptime_s": 1.0}

    monkeypatch.setattr(settings, "provider_drafting", "groq")
    monkeypatch.setattr(client.app.state, "mac_listener", _Mac(), raising=False)
    with client.websocket_connect("/ws/stream") as ws:
        payload = ws.receive_json()["payload"]
    assert payload["listener_health"]["nsworkspace"]["restart_count"] == 3
    assert payload["listener_health"]["nsworkspace"]["alive"] is True
    assert payload["engine_providers"] == {
        "main": "local", "analysis": "local", "drafting": "groq",
    }
    assert payload["cloud_personal_text"] is False


def test_status_hello_without_mac_listener_has_no_health(client) -> None:  # noqa: F811
    with client.websocket_connect("/ws/stream") as ws:
        payload = ws.receive_json()["payload"]
    assert payload["listener_health"] is None


# ---- #29 tools_changed ------------------------------------------------------


def _tool_source(name: str) -> str:
    return (
        f'"""{name}."""\n'
        "from smolagents import tool\n\n"
        "@tool\n"
        f"def {name}() -> dict:\n"
        '    """Return a sentinel."""\n'
        '    return {"status": "ok"}\n'
    )


def test_tools_changed_once_per_load_and_unload(tmp_path) -> None:
    for name in ("alpha_tool", "beta_tool"):
        (tmp_path / f"{name}.py").write_text(_tool_source(name))
    with patch.object(Orchestrator, "_build_agent", return_value=None), _LoopThread() as loop:
        orch, frames = _orchestrator(loop, tmp_path)
        orch.load_dynamic_tools()          # two tools, one frame
        _drain(loop)
        assert orch.unload_dynamic_tool("alpha_tool")
        assert not orch.unload_dynamic_tool("alpha_tool")  # no change, no frame
        _drain(loop)
        (tmp_path / "broken_tool.py").write_text("import not_a_module_xyz\n")
        # alpha's file is still there (unload leaves files alone): alpha
        # comes back, beta reloads, broken is quarantined: still one frame.
        orch.load_dynamic_tools()
        _drain(loop)
    changed = [f.payload for f in frames if isinstance(f, ToolsChanged)]
    assert [(c.reason, c.tool_names) for c in changed] == [
        ("loaded", ["alpha_tool", "beta_tool"]),
        ("unloaded", ["alpha_tool"]),
        ("quarantined", ["alpha_tool", "broken_tool"]),
    ]


def test_tools_changed_over_ws_after_disable_and_enable(client, tmp_path, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(settings, "generated_tools_dir", tmp_path)
    orch = client.app.state.orchestrator
    (tmp_path / "lamp_tool.py").write_text(_tool_source("lamp_tool"))
    orch.load_dynamic_tools(tmp_path)
    with client.websocket_connect("/ws/stream") as ws:
        assert ws.receive_json()["type"] == "status"
        assert client.post("/api/tools/lamp_tool/disable").status_code == 200
        frame = ws.receive_json()
        assert frame["type"] == "tools_changed"
        assert frame["payload"] == {"reason": "unloaded", "tool_names": ["lamp_tool"]}
        assert client.post("/api/tools/lamp_tool/enable").status_code == 200
        frame = ws.receive_json()
        assert (frame["type"], frame["payload"]["reason"]) == ("tools_changed", "loaded")
        # Deleting a disabled copy (nothing loaded) still tells clients.
        client.post("/api/tools/lamp_tool/disable")
        assert ws.receive_json()["payload"]["reason"] == "unloaded"
        assert client.delete("/api/tools/lamp_tool").status_code == 200
        frame = ws.receive_json()
        assert frame["payload"] == {"reason": "deleted", "tool_names": ["lamp_tool"]}


# ---- #31 personal text out of cloud prompts ---------------------------------


@pytest.fixture()
def providers(monkeypatch):
    """Set per-role providers; ``AGENT_CLOUD_PERSONAL_TEXT`` unset."""
    monkeypatch.delenv(privacy.OPT_IN_ENV, raising=False)
    monkeypatch.setattr(settings, "provider", "local")
    monkeypatch.setattr(settings, "pattern_detection_engine", "main")

    def set_(**roles: str) -> None:
        for role in ("main", "analysis", "drafting"):
            monkeypatch.setattr(settings, f"provider_{role}", roles.get(role, ""))

    set_()
    return set_


def _imessage() -> ContextEvent:
    return ContextEvent(event_type="imessage_received",
                        metadata={"sender": "+15551234567", "text": SECRET_TEXT, "rowid": 9})


def _mail() -> ContextEvent:
    return ContextEvent(event_type="mail_received", metadata={
        "sender": "Lab <lab@example.com>", "subject": SECRET_SUBJECT,
        "date": "2026-11-09T10:00:00+00:00", "mailbox": "INBOX", "message_id": 321,
    })


def test_imessage_prompt_redacted_only_for_cloud_main(providers, monkeypatch) -> None:
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    providers(analysis="groq", drafting="groq")      # main stays local
    assert SECRET_TEXT in orch._build_prompt(_imessage())
    providers(main="groq")
    prompt = orch._build_prompt(_imessage())
    assert SECRET_TEXT not in prompt and PH in prompt
    assert "+15551234567" in prompt                   # the sender stays
    monkeypatch.setenv(privacy.OPT_IN_ENV, "1")
    assert SECRET_TEXT in orch._build_prompt(_imessage())


def test_analysis_prompt_follows_the_analysis_engine(providers) -> None:
    engine = MLXEngine()
    analysis = MLXEngine()
    analysis.evaluate_event = AsyncMock(return_value="")  # type: ignore[method-assign]
    orch = Orchestrator(engine=engine, event_bus=EventBus(), tools=[],
                        analysis_engine=analysis)
    providers(main="groq")                            # analysis local
    asyncio.run(orch._analyse_event(_imessage()))
    assert SECRET_TEXT in analysis.evaluate_event.call_args.args[0]
    providers(analysis="groq")
    asyncio.run(orch._analyse_event(_imessage()))
    sent = analysis.evaluate_event.call_args.args[0]
    assert SECRET_TEXT not in sent and PH in sent


def _gated_read_email(orch: Orchestrator, loop: asyncio.AbstractEventLoop) -> Any:
    from smolagents import tool

    @tool
    def read_email(message_id: int) -> dict:
        """Read one email.

        Args:
            message_id: the id.
        """
        return {"status": "ok", "message_id": message_id, "sender": "lab@example.com",
                "subject": SECRET_SUBJECT, "date": "d", "body": SECRET_BODY}

    wrapped = orch._wrap_tool_for_approval(read_email)
    return asyncio.run_coroutine_threadsafe(
        asyncio.to_thread(wrapped.forward, 321), loop,
    ).result(5)


def test_read_email_result_redacted_for_cloud_main(providers, monkeypatch) -> None:
    with _LoopThread() as loop:
        orch, frames = _orchestrator(loop)
        providers(analysis="groq")                    # main local
        local = _gated_read_email(orch, loop)
        providers(main="groq")
        cloud = _gated_read_email(orch, loop)
        monkeypatch.setenv(privacy.OPT_IN_ENV, "1")
        opted_in = _gated_read_email(orch, loop)
        _drain(loop)
    assert (local["subject"], local["body"]) == (SECRET_SUBJECT, SECRET_BODY)
    assert (cloud["subject"], cloud["body"]) == (PH, PH)
    assert cloud["message_id"] == 321 and cloud["sender"] == "lab@example.com"
    assert (opted_in["subject"], opted_in["body"]) == (SECRET_SUBJECT, SECRET_BODY)
    # The tool_result thought the UI shows is what the engine got.
    shown = [f.payload.content for f in frames if f.payload.content.startswith("read_email")]
    assert [SECRET_BODY in c for c in shown] == [True, False, True]


@pytest.mark.parametrize("event,kind,secret,keeps", [
    (_mail(), "mail_invite", SECRET_SUBJECT, "message_id: 321"),
    (_imessage(), "plan_with_time", SECRET_TEXT, "An iMessage arrived"),
])
def test_builtin_follow_up_event_details(providers, event, kind, secret, keeps) -> None:
    assert NextStepSuggester.classify(event) == kind   # classify sees real text
    idea = NextStepSuggester.builtin_idea(event, kind)
    pattern = ContextEvent(event_type="pattern_detected", metadata=idea)
    providers(analysis="groq")                         # main local
    assert secret in Orchestrator._build_pattern_prompt(pattern)
    providers(main="groq")
    prompt = Orchestrator._build_pattern_prompt(pattern)
    assert secret not in prompt and PH in prompt and keeps in prompt
    # An idea without a redacted copy sends only the placeholder.
    bare = dict(idea)
    bare.pop("event_details_redacted")
    prompt = Orchestrator._build_pattern_prompt(
        ContextEvent(event_type="pattern_detected", metadata=bare))
    assert secret not in prompt


def _next_step_prompt(event: ContextEvent, tmp_path: Path) -> str:
    seen: list[str] = []

    async def evaluate(text: str) -> str:
        seen.append(text)
        return "NO_SUGGESTION"

    async def run() -> None:
        src = NextStepSuggester(event_bus=EventBus(), evaluate=evaluate,
                                state=IdeaState(tmp_path / f"s{time.time_ns()}.json"))
        task = src.observe(event)
        assert task is not None
        await task

    asyncio.run(run())
    return seen[0]


def test_idea_sources_follow_the_detection_engine(providers, tmp_path, monkeypatch) -> None:
    tracking = ContextEvent(event_type="imessage_received", metadata={
        "sender": "x", "text": "your ups package shipped, tracking 1Z999AA10123456784",
    })
    secret = "1Z999AA10123456784"

    def windows() -> tuple[str, str, str]:
        pr = PatternRecognizer(event_bus=EventBus(), evaluate=AsyncMock())
        pr.observe(_mail())
        oh = OpenHelpSuggester(event_bus=EventBus(), evaluate=AsyncMock(),
                               judge=AsyncMock(), state=IdeaState(tmp_path / "oh.json"),
                               min_events=99)
        oh.observe(_imessage())
        return (pr._window[0], open_help_describe(oh._window[0]),
                _next_step_prompt(tracking, tmp_path))

    providers(analysis="groq", drafting="groq")    # detection = main = local
    pr_text, oh_text, ns_text = windows()
    assert SECRET_SUBJECT[:30] in pr_text and SECRET_TEXT in oh_text and secret in ns_text

    providers(main="groq")                          # detection engine is cloud
    pr_text, oh_text, ns_text = windows()
    assert SECRET_SUBJECT[:30] not in pr_text and PH[:30] in pr_text
    assert SECRET_TEXT not in oh_text and PH in oh_text
    assert secret not in ns_text and PH in ns_text

    monkeypatch.setattr(settings, "pattern_detection_engine", "analysis")
    pr_text, oh_text, ns_text = windows()           # analysis engine is local
    assert SECRET_TEXT in oh_text and secret in ns_text

    monkeypatch.setenv(privacy.OPT_IN_ENV, "1")
    monkeypatch.setattr(settings, "pattern_detection_engine", "main")
    pr_text, oh_text, ns_text = windows()
    assert SECRET_TEXT in oh_text and secret in ns_text


def test_chat_text_window_titles_and_paths_are_not_redacted(providers) -> None:
    providers(main="groq", analysis="groq")
    chat = ContextEvent(event_type="user_message", metadata={"text": SECRET_TEXT})
    assert SECRET_TEXT in Orchestrator._build_chat_prompt(chat)
    file_event = ContextEvent(event_type="file", window_title="Q3 layoffs.key",
                              metadata={"path": "/Users/me/Downloads/payslip.pdf"})
    prompt = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])._build_prompt(file_event)
    assert "payslip.pdf" in prompt and "Q3 layoffs.key" in prompt
