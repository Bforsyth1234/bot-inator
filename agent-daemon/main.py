"""FastAPI entry point for the agent daemon.

Exposes:
- GET  /health      — liveness probe
- WS   /ws/stream   — bidirectional protocol for the SwiftUI MenuBar app
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from ai.memory import Memory
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from config import settings
from events.event_bus import EventBus
from events.file_watcher import FileWatcher
from events.mac_listeners import MacAppListener
from schemas.ws_messages import (
    ApprovalResponse,
    Command,
    Status,
    StatusPayload,
    WSMessage,
)
from tools.read_active_tab import read_active_tab

logger = logging.getLogger(__name__)

_WS_ADAPTER: TypeAdapter = TypeAdapter(WSMessage)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _listeners_enabled() -> bool:
    return os.environ.get("AGENT_DISABLE_LISTENERS") != "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.seq = 0

    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())

    engine = MLXEngine(settings.model_path)
    memory = Memory()

    listeners: list[Any] = []
    listener_names: list[str] = []
    if _listeners_enabled():
        try:
            mac = MacAppListener(bus)
            mac.start()
            listeners.append(mac)
            listener_names.append("nsworkspace")
        except Exception:
            logger.exception("Failed to start MacAppListener")
        try:
            watcher = FileWatcher(bus, settings.watch_dirs)
            watcher.start()
            listeners.append(watcher)
            listener_names.append("file_watcher")
        except Exception:
            logger.exception("Failed to start FileWatcher")

    orchestrator = Orchestrator(
        engine=engine,
        event_bus=bus,
        memory=memory,
        tools=[read_active_tab],
    )
    await orchestrator.start()

    app.state.event_bus = bus
    app.state.engine = engine
    app.state.memory = memory
    app.state.orchestrator = orchestrator
    app.state.listeners = listeners
    app.state.listener_names = listener_names

    try:
        yield
    finally:
        await orchestrator.stop()
        for listener in listeners:
            try:
                listener.stop()
            except Exception:
                logger.exception("Error stopping listener")
        try:
            memory.close()
        except Exception:
            logger.exception("Error closing memory")


app = FastAPI(title="agent-daemon", version="0.1.0", lifespan=lifespan)


def _next_seq() -> int:
    app.state.seq += 1
    return app.state.seq


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


async def _handle_command(app: FastAPI, msg: Command) -> None:
    action = msg.payload.action
    logger.info("Received command: %s", action)
    if action == "pause_listeners":
        for listener in app.state.listeners:
            try:
                listener.stop()
            except Exception:
                logger.exception("Failed to pause listener")
        app.state.listeners = []
        app.state.listener_names = []
    elif action == "resume_listeners":
        logger.info("resume_listeners requested; no-op in Phase 1")
    elif action == "reload_model":
        try:
            await app.state.engine.load()
        except Exception:
            logger.exception("Failed to reload model")
    elif action == "clear_memory":
        try:
            app.state.memory.close()
        except Exception:
            logger.exception("Failed to clear memory")


@app.websocket("/ws/stream")
async def ws_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    orchestrator: Orchestrator = app.state.orchestrator

    async def _ws_send(message: Any) -> None:
        payload = (
            message.model_dump_json()
            if hasattr(message, "model_dump_json")
            else str(message)
        )
        await websocket.send_text(payload)

    orchestrator.add_subscriber(_ws_send)

    hello = Status(
        seq=_next_seq(),
        timestamp=_now_iso(),
        payload=StatusPayload(
            state="ready",
            model_loaded=settings.model_path,
            listeners_active=list(app.state.listener_names),
        ),
    )
    await websocket.send_text(hello.model_dump_json())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = _WS_ADAPTER.validate_json(raw)
            except ValidationError:
                logger.warning("Invalid WS message dropped: %s", raw[:200])
                continue
            if isinstance(msg, ApprovalResponse):
                orchestrator.submit_approval_response(msg.payload)
            elif isinstance(msg, Command):
                await _handle_command(app, msg)
            else:
                logger.debug("Ignoring inbound %s", type(msg).__name__)
    except WebSocketDisconnect:
        return
    finally:
        orchestrator.remove_subscriber(_ws_send)
