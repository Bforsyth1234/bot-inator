"""FastAPI entry point for the agent daemon.

Exposes:
- GET  /health      — liveness probe
- WS   /ws/stream   — bidirectional protocol for the SwiftUI MenuBar app
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from ai.memory import Memory, set_default_memory
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from ai.pattern_recognizer import PatternRecognizer
from config import settings
from events.event_bus import EventBus
from events.file_watcher import FileWatcher
from events.imessage_watcher import IMessageWatcher
from events.mac_listeners import MacAppListener
from schemas.ws_messages import (
    ApprovalResponse,
    CodeApprovalResponse,
    Command,
    Status,
    StatusPayload,
    WSMessage,
)
from tools import AVAILABLE_TOOLS
from tools.meta_tool_generator import set_meta_tool_context

logging.basicConfig(
    level=os.environ.get("AGENT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
    analysis_engine = MLXEngine(settings.analysis_model_path)
    memory = Memory()
    set_default_memory(memory)

    # Eager-load both engines in parallel so the first event is not
    # blocked by a multi-GB model download + Metal compile.
    if os.environ.get("AGENT_EAGER_LOAD", "1") != "0":
        logger.info(
            "Eager-loading MLX models: main=%s analysis=%s",
            settings.model_path,
            settings.analysis_model_path,
        )
        results = await asyncio.gather(
            engine.load(),
            analysis_engine.load(),
            return_exceptions=True,
        )
        for name, result in zip(("main", "analysis"), results):
            if isinstance(result, Exception):
                logger.exception(
                    "Failed to eager-load %s engine; will fall back to "
                    "lazy load on first use",
                    name,
                    exc_info=result,
                )

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
        try:
            imessage = IMessageWatcher(bus)
            imessage.start()
            listeners.append(imessage)
            listener_names.append("imessage_watcher")
        except Exception:
            logger.exception("Failed to start IMessageWatcher")

    pattern_recognizer = PatternRecognizer(
        event_bus=bus,
        evaluate=analysis_engine.evaluate_event,
    )
    orchestrator = Orchestrator(
        engine=engine,
        event_bus=bus,
        memory=memory,
        tools=list(AVAILABLE_TOOLS),
        analysis_engine=analysis_engine,
        generated_tools_dir=settings.generated_tools_dir,
        pattern_recognizer=pattern_recognizer,
    )
    set_meta_tool_context(
        orchestrator=orchestrator,
        engine=engine,
        generated_dir=settings.generated_tools_dir,
    )
    try:
        orchestrator.load_dynamic_tools()
    except Exception:
        logger.exception("load_dynamic_tools at startup failed")
    await orchestrator.start()

    app.state.event_bus = bus
    app.state.engine = engine
    app.state.analysis_engine = analysis_engine
    app.state.memory = memory
    app.state.orchestrator = orchestrator
    app.state.pattern_recognizer = pattern_recognizer
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
        set_default_memory(None)
        try:
            import tools.meta_tool_generator as _mg  # noqa: WPS433
            _mg._ORCHESTRATOR = None  # type: ignore[attr-defined]
            _mg._ENGINE = None  # type: ignore[attr-defined]
            _mg._GENERATED_DIR = None  # type: ignore[attr-defined]
        except Exception:
            logger.exception("Error clearing meta-tool context")


app = FastAPI(title="agent-daemon", version="0.1.0", lifespan=lifespan)


def _next_seq() -> int:
    app.state.seq += 1
    return app.state.seq


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _description_for_tool(tool_obj: Any) -> str:
    """Pull a one-paragraph description from a live smolagents Tool."""
    for attr in ("description", "__doc__"):
        value = getattr(tool_obj, attr, None)
        if value:
            return value.strip().splitlines()[0][:240]
    return ""


def _description_from_source(path: Path) -> str:
    """Return the first line of the first function docstring in ``path``.

    Used for generated modules that aren't currently loaded (e.g. they
    failed to import or are pending a restart), so the Tool Manager list
    stays useful even when the agent can't introspect them live.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            if doc:
                return doc.strip().splitlines()[0][:240]
    return ""


@app.get("/api/tools")
async def list_tools() -> list[dict[str, Any]]:
    """Return every tool the agent can see — built-ins + generated."""
    orchestrator: Orchestrator = app.state.orchestrator
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for tool_obj in orchestrator.tools:
        name = getattr(tool_obj, "name", getattr(tool_obj, "__name__", ""))
        if not name or name in seen:
            continue
        seen.add(name)
        is_generated = name in getattr(orchestrator, "_dynamic_tool_names", set())
        rows.append({
            "name": name,
            "description": _description_for_tool(tool_obj),
            "is_generated": is_generated,
        })

    generated_dir = settings.generated_tools_dir
    if generated_dir.exists():
        for path in sorted(generated_dir.iterdir()):
            if path.name.startswith(".") or path.is_dir() or path.suffix != ".py":
                continue
            name = path.stem
            if name in seen:
                continue
            seen.add(name)
            rows.append({
                "name": name,
                "description": _description_from_source(path),
                "is_generated": True,
            })
    return rows


@app.delete("/api/tools/{tool_name}")
async def delete_tool(tool_name: str) -> dict[str, Any]:
    """Uninstall an agent-authored tool. Refuses to touch built-ins."""
    if not _IDENTIFIER_RE.match(tool_name):
        raise HTTPException(status_code=400, detail="invalid tool name")
    orchestrator: Orchestrator = app.state.orchestrator
    if tool_name in orchestrator._builtin_tool_names:
        raise HTTPException(
            status_code=400, detail="cannot delete a built-in tool"
        )
    generated_dir = settings.generated_tools_dir
    path = generated_dir / f"{tool_name}.py"
    if not path.exists():
        raise HTTPException(status_code=404, detail="tool not found")
    unloaded = orchestrator.unload_dynamic_tool(tool_name)
    try:
        path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # Best-effort nested-repo commit of the removal.
    try:
        from tools.meta_tool_generator import _git_commit  # noqa: WPS433
        _git_commit(
            generated_dir,
            f"{tool_name}.py",
            f"Removed generated tool: {tool_name}",
        )
    except Exception:
        logger.exception("git commit of removal failed")
    return {"status": "ok", "tool_name": tool_name, "unloaded": unloaded}


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
    elif action == "reload_dynamic_tools":
        try:
            app.state.orchestrator.load_dynamic_tools()
        except Exception:
            logger.exception("Failed to reload dynamic tools")


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
            elif isinstance(msg, CodeApprovalResponse):
                orchestrator.submit_code_approval_response(msg.payload)
            elif isinstance(msg, Command):
                await _handle_command(app, msg)
            else:
                logger.debug("Ignoring inbound %s", type(msg).__name__)
    except WebSocketDisconnect:
        return
    finally:
        orchestrator.remove_subscriber(_ws_send)
