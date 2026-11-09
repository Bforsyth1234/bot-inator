"""#60: Calendar through EventKit in the menu bar app, over the WebSocket.

A fake app connection stands in for MenuBarUI: its ``send`` coroutine runs
on a real event loop (in a background thread, like the daemon's) and
answers through ``AppCalendarBridge.submit_response``. The last test drives
the real ``/ws/stream`` route in ``main.py``. No real calendar access.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import TypeAdapter

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

import ai  # noqa: E402,F401  - import order: ai -> tools avoids a circular import
import importlib  # noqa: E402

from ai import permissions as P  # noqa: E402
from schemas.ws_messages import (  # noqa: E402
    CalendarRequest,
    CalendarRequestPayload,
    CalendarResponse,
    CalendarResponsePayload,
    CalendarStatus,
    CalendarStatusPayload,
    WSMessage,
)
from tools import calendar_access as ca  # noqa: E402

list_mod = importlib.import_module("tools.list_events")
create_mod = importlib.import_module("tools.create_event")

TZ = datetime.now().astimezone().tzinfo
_TS = "2026-11-09T12:00:00.000Z"
_ADAPTER: TypeAdapter = TypeAdapter(WSMessage)


def _dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


# ---- frames -----------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    CalendarRequest(seq=1, timestamp=_TS, payload=CalendarRequestPayload(
        request_id="cal_1", op="list",
        args={"start": "2026-11-10T00:00:00-08:00", "end": "2026-11-11T00:00:00-08:00"})),
    CalendarResponse(seq=2, timestamp=_TS, payload=CalendarResponsePayload(
        request_id="cal_1", ok=True, authorization="full_access",
        events=[{"id": "E1", "title": "Standup", "start": "2026-11-10T09:00:00-08:00",
                 "end": "2026-11-10T09:15:00-08:00", "calendar": "Work", "location": "",
                 "notes": "", "url": "", "all_day": False}])),
    CalendarResponse(seq=3, timestamp=_TS, payload=CalendarResponsePayload(
        request_id="cal_2", ok=False, error="denied", authorization="denied")),
    CalendarStatus(seq=4, timestamp=_TS, payload=CalendarStatusPayload(authorization="not_determined")),
])
def test_calendar_frames_round_trip(message) -> None:
    parsed = _ADAPTER.validate_python(json.loads(message.model_dump_json()))
    assert parsed == message and type(parsed) is type(message)


def test_swift_shaped_response_parses() -> None:
    """What CalendarBridge.swift encodes (nil optionals omitted)."""
    raw = ('{"type":"calendar_response","seq":1000001,"timestamp":"2026-11-09T12:00:00.000Z",'
           '"payload":{"request_id":"cal_9","ok":true,"event_id":"X","calendar":"Home",'
           '"authorization":"full_access"}}')
    msg = _ADAPTER.validate_json(raw)
    assert isinstance(msg, CalendarResponse) and msg.payload.event_id == "X"


# ---- fake app ---------------------------------------------------------------------


@pytest.fixture()
def loop():
    lp = asyncio.new_event_loop()
    thread = threading.Thread(target=lp.run_forever, daemon=True)
    thread.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    thread.join(timeout=5)
    lp.close()


@pytest.fixture()
def bridge(monkeypatch):
    b = ca.AppCalendarBridge()
    monkeypatch.setattr(ca, "app_bridge", b)
    return b


class FakeApp:
    """Records requests; ``reply(request_id, op, args)`` builds the answer
    (None: never answer)."""

    def __init__(self, bridge: ca.AppCalendarBridge, conn_id: str,
                 reply: Callable[[str, str, dict], Optional[dict]]) -> None:
        self.bridge, self.conn_id, self.reply = bridge, conn_id, reply
        self.requests: list[tuple[str, dict, int]] = []

    async def send(self, request_id: str, op: str, args: dict, timeout_seconds: int) -> None:
        self.requests.append((op, args, timeout_seconds))
        answer = self.reply(request_id, op, args)
        if answer is not None:
            asyncio.get_running_loop().call_later(
                0.01, self.bridge.submit_response, self.conn_id, {"request_id": request_id, **answer})


def _attach(bridge, loop, conn_id="ws_a", authorization="full_access", reply=None) -> FakeApp:
    app = FakeApp(bridge, conn_id, reply or (lambda rid, op, args: {"ok": True, "events": []}))
    bridge.connect(conn_id, app.send, loop)
    if authorization is not None:
        bridge.set_status(conn_id, authorization)
    return app


EVENTS = [
    {"id": "E2", "title": "Lunch", "start": "2026-11-10T12:00:00+00:00",
     "end": "2026-11-10T13:00:00+00:00", "calendar": "Home", "location": "Cafe",
     "notes": "n" * 900, "url": "", "all_day": False},
    {"id": "E1", "title": "Standup", "start": "2026-11-10T09:00:00+00:00",
     "end": "2026-11-10T09:15:00+00:00", "calendar": "Work", "location": "",
     "notes": "", "url": "https://zoom.us/j/1", "all_day": False},
]


def test_list_through_the_app(bridge, loop) -> None:
    app = _attach(bridge, loop, reply=lambda rid, op, args: {"ok": True, "events": EVENTS})
    events = ca.AppCalendarBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11), ["Work"])
    assert [e["id"] for e in events] == ["E1", "E2"]  # sorted by start
    assert events[0]["start"] == ca.iso(datetime.fromisoformat("2026-11-10T09:00:00+00:00"))
    assert len(events[1]["notes"]) == ca.NOTES_LIMIT
    op, args, timeout = app.requests[0]
    assert op == "list" and args["calendars"] == ["Work"] and timeout == ca.APP_TIMEOUT_S
    assert args["start"] == ca.iso(_dt(2026, 11, 10))


def test_create_through_the_app(bridge, loop) -> None:
    app = _attach(bridge, loop, reply=lambda rid, op, args: {
        "ok": True, "event_id": "NEW1", "calendar": "Home"})
    result = ca.AppCalendarBackend().create("Lunch with Sam", _dt(2026, 11, 10, 12), _dt(2026, 11, 10, 13))
    assert result == {"status": "ok", "event_id": "NEW1", "calendar": "Home"}
    op, args, _ = app.requests[0]
    assert op == "create" and args["title"] == "Lunch with Sam" and "calendar" not in args


def test_app_error_is_raised(bridge, loop) -> None:
    _attach(bridge, loop, reply=lambda rid, op, args: {
        "ok": False, "error": "Calendar access was denied", "authorization": "denied"})
    with pytest.raises(ca.CalendarAccessError, match="denied"):
        ca.AppCalendarBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11))
    assert bridge.authorization() == "denied"  # the response updates the status


def test_timeout(bridge, loop, monkeypatch) -> None:
    monkeypatch.setattr(ca, "APP_TIMEOUT_S", 0.2)
    _attach(bridge, loop, reply=lambda rid, op, args: None)
    start = time.monotonic()
    with pytest.raises(ca.CalendarAccessError, match="didn't answer"):
        ca.AppCalendarBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11))
    assert time.monotonic() - start < 3
    assert bridge._pending == {}


def test_first_request_waits_for_the_prompt(bridge, loop) -> None:
    app = _attach(bridge, loop, authorization="not_determined")
    ca.AppCalendarBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11))
    assert app.requests[0][2] == ca.APP_PROMPT_TIMEOUT_S


def test_not_connected_gives_the_open_the_app_error(bridge, loop, monkeypatch) -> None:
    monkeypatch.setenv(ca.ENV_BACKEND, "auto")
    ca.set_backend(None)
    backend = ca.get_backend()
    assert backend.name == "app" and backend.ready() is False
    with pytest.raises(ca.CalendarAccessError, match="open the bot-inator menu bar app"):
        backend.list(_dt(2026, 11, 10), _dt(2026, 11, 11))
    # A connection that never sent calendar_status (e.g. a test client) doesn't count.
    _attach(bridge, loop, authorization=None)
    assert not bridge.connected()
    out = list_mod.list_events("2026-11-10", "2026-11-11")
    assert out["status"] == "error" and "open the bot-inator menu bar app" in out["error"]
    out = create_mod.create_event("Lunch", "2026-11-10T12:00")
    assert out["status"] == "error" and "open the bot-inator menu bar app" in out["error"]


def test_tools_unchanged_above_the_app_backend(bridge, loop, monkeypatch) -> None:
    monkeypatch.setenv(ca.ENV_BACKEND, "app")
    ca.set_backend(None)
    _attach(bridge, loop, reply=lambda rid, op, args: (
        {"ok": True, "events": EVENTS} if op == "list"
        else {"ok": True, "event_id": "NEW1", "calendar": "Home"}))
    out = list_mod.list_events("2026-11-10", "2026-11-11")
    assert out["status"] == "ok" and out["count"] == 2 and out["events"][0]["title"] == "Standup"
    out = create_mod.create_event("Lunch", "2026-11-10T12:00")
    assert out["status"] == "ok" and out["event_id"] == "NEW1" and out["calendar"] == "Home"


def test_ready_only_with_full_access(bridge, loop) -> None:
    backend = ca.AppCalendarBackend()
    for auth, ready in (("not_determined", False), ("denied", False),
                        ("write_only", False), ("full_access", True)):
        _attach(bridge, loop, authorization=auth)
        assert backend.ready() is ready, auth
        bridge.disconnect("ws_a")
    assert backend.ready() is False


def test_requests_go_to_one_connection(bridge, loop) -> None:
    first = _attach(bridge, loop, conn_id="ws_a")
    second = _attach(bridge, loop, conn_id="ws_b")
    other = _attach(bridge, loop, conn_id="ws_c", authorization=None)  # not an app
    ca.AppCalendarBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11))
    assert len(second.requests) == 1 and first.requests == [] and other.requests == []
    bridge.disconnect("ws_b")
    ca.AppCalendarBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11))
    assert len(first.requests) == 1


def test_disconnect_fails_the_pending_request(bridge, loop) -> None:
    _attach(bridge, loop, reply=lambda rid, op, args: None)
    errors: list[BaseException] = []

    def call() -> None:
        try:
            ca.AppCalendarBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=call)
    worker.start()
    for _ in range(100):
        if bridge._pending:
            break
        time.sleep(0.01)
    bridge.disconnect("ws_a")
    worker.join(timeout=5)
    assert errors and "disconnected" in str(errors[0])


def test_refuses_to_block_the_event_loop(bridge, loop) -> None:
    _attach(bridge, loop)

    async def on_loop() -> Any:
        return ca.AppCalendarBackend().list(_dt(2026, 11, 10), _dt(2026, 11, 11))

    with pytest.raises(ca.CalendarAccessError, match="event loop"):
        asyncio.run_coroutine_threadsafe(on_loop(), loop).result(timeout=5)


def test_permissions_row_reads_the_app(bridge, loop, monkeypatch) -> None:
    ca.register_permission_hooks()
    try:
        row = P._row(P.get("calendars"))
        assert row["status"] == P.UNKNOWN and "menu bar app" in row["detail"]
        for auth, status in (("not_determined", P.NOT_ASKED), ("denied", P.DENIED),
                             ("full_access", P.GRANTED)):
            _attach(bridge, loop, authorization=auth,
                    reply=lambda rid, op, args: {"ok": True, "authorization": "full_access"})
            assert P._row(P.get("calendars"))["status"] == status
            bridge.disconnect("ws_a")
        app = _attach(bridge, loop, authorization="not_determined",
                      reply=lambda rid, op, args: {"ok": True, "authorization": "full_access"})
        out = P.request("calendars")
        assert app.requests[0][0] == "request_access"
        assert out["action"] == "prompted" and out["permission"]["status"] == P.GRANTED
    finally:
        P.set_external_check("calendars", None)
        P.set_external_request("calendars", None)


# ---- through main.py's /ws/stream ---------------------------------------------------


def test_ws_route_carries_request_and_response(monkeypatch) -> None:
    """The daemon sends calendar_request to the app's WS connection and
    routes its calendar_response back to the waiting tool thread."""
    monkeypatch.setattr(ca, "APP_TIMEOUT_S", 3.0)
    monkeypatch.setenv(ca.ENV_BACKEND, "app")
    ca.set_backend(None)
    import auth
    from fastapi.testclient import TestClient

    with patch("ai.orchestrator.Orchestrator._build_agent", return_value=None), patch(
        "ai.mlx_engine.MLXEngine.evaluate_event", new_callable=AsyncMock, return_value="",
    ), patch("ai.mlx_engine.MLXEngine.generate_chat_sync", return_value="NO_PATTERN"):
        from main import app

        with TestClient(app) as client:
            with client.websocket_connect("/ws/stream", headers=auth.auth_header()) as ws:
                assert ws.receive_json()["type"] == "status"
                ws.send_text(CalendarStatus(seq=1, timestamp=_TS, payload=CalendarStatusPayload(
                    authorization="full_access")).model_dump_json())
                for _ in range(200):
                    if ca.app_bridge.connected():
                        break
                    time.sleep(0.01)
                assert ca.get_backend().ready()

                result: dict[str, Any] = {}
                worker = threading.Thread(target=lambda: result.update(
                    list_mod.list_events("2026-11-10", "2026-11-11")))
                worker.start()
                frame = None
                for _ in range(20):
                    msg = ws.receive_json()
                    if msg["type"] == "calendar_request":
                        frame = msg
                        break
                assert frame is not None and frame["payload"]["op"] == "list"
                ws.send_text(CalendarResponse(seq=2, timestamp=_TS, payload=CalendarResponsePayload(
                    request_id=frame["payload"]["request_id"], ok=True, events=EVENTS[:1],
                    authorization="full_access")).model_dump_json())
                worker.join(timeout=10)
                assert result.get("status") == "ok", result
                assert [e["title"] for e in result["events"]] == ["Lunch"]
            # Closing the socket unregisters the app.
            for _ in range(200):
                if not ca.app_bridge.connected():
                    break
                time.sleep(0.01)
            assert not ca.app_bridge.connected()
