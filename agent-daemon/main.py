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
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, TypeAdapter, ValidationError

import user_settings

from ai.memory import Memory, set_default_memory
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from ai.pattern_recognizer import PatternRecognizer, make_detection_evaluator
from ai.idea_sources import (
    CHAT_GAP_SYSTEM_PROMPT,
    NEXT_STEP_SYSTEM_PROMPT,
    ChatGapDetector,
    NextStepSuggester,
)
from ai.open_help import (
    IDEA_JUDGE_SYSTEM_PROMPT,
    OPEN_HELP_SYSTEM_PROMPT,
    IdeaFeedback,
    OpenHelpSuggester,
)
# GroqEngine is imported lazily inside ``lifespan`` so the daemon still
# boots cleanly when ``settings.provider == "local"`` and the optional
# Groq dependency tree is missing.
from config import GROQ_DEFAULT_MODELS, settings
from events.event_bus import ContextEvent, EventBus
from events.file_watcher import FileWatcher
from events.imessage_watcher import IMessageWatcher
from events.mac_listeners import MacAppListener
from schemas.ws_messages import (
    ApprovalResponse,
    CodeApprovalResponse,
    Command,
    Status,
    StatusPayload,
    UserMessage,
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


def _build_engines() -> tuple[Any, Any, Any]:
    """Construct the (main, analysis, drafting) engine triplet.

    Provider is read from :data:`settings.provider`. ``"groq"`` builds
    three :class:`~ai.groq_engine.GroqEngine` instances pointed at the
    configured Groq endpoint; anything else (default ``"local"``) keeps
    the original MLX-on-Apple-Silicon stack. Each engine is isolated so
    the tool-calling agent, the lightweight analysis loop, and the
    meta-tool drafter can run on different model ids without sharing
    state. Unknown provider names fall back to ``"local"`` with a warning
    so a typo in ``AGENT_PROVIDER`` cannot brick startup.
    """
    provider = (settings.provider or "local").lower()
    if provider == "groq":
        from ai.groq_engine import GroqEngine  # noqa: WPS433
        return (
            GroqEngine(settings.groq_main_model,
                       api_key=settings.groq_api_key,
                       api_base=settings.groq_api_base),
            GroqEngine(settings.groq_analysis_model,
                       api_key=settings.groq_api_key,
                       api_base=settings.groq_api_base),
            GroqEngine(settings.groq_drafting_model,
                       api_key=settings.groq_api_key,
                       api_base=settings.groq_api_base),
        )
    if provider != "local":
        logger.warning(
            "Unknown AGENT_PROVIDER=%r; falling back to local MLX", provider
        )
    return (
        MLXEngine(settings.model_path),
        MLXEngine(settings.analysis_model_path),
        MLXEngine(settings.drafting_model_path),
    )


def _select_detection_engine(
    choice: str, engine: Any, analysis_engine: Any, drafting_engine: Any
) -> Any:
    """Pick the engine that runs pattern detection.

    ``choice`` comes from :data:`settings.pattern_detection_engine`.
    Unknown values fall back to the main engine with a warning so a typo
    in ``AGENT_PATTERN_ENGINE`` cannot brick startup.
    """
    engines = {
        "main": engine,
        "analysis": analysis_engine,
        "drafting": drafting_engine,
    }
    key = (choice or "main").lower()
    if key not in engines:
        logger.warning(
            "Unknown AGENT_PATTERN_ENGINE=%r; using main engine", choice
        )
        key = "main"
    return engines[key]


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.seq = 0

    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())

    engine, analysis_engine, drafting_engine = _build_engines()
    memory = Memory()
    set_default_memory(memory)

    # Eager-load the engines sequentially. Parallel ``asyncio.gather``
    # used to race ``huggingface_hub.snapshot_download``'s internal
    # ``tqdm.contrib.concurrent.ensure_lock`` context manager — the
    # second exiter hits ``AttributeError: type object 'tqdm' has no
    # attribute '_lock'`` (or, in the other interleaving, deadlocks on
    # the same RLock). Serializing the loads sidesteps both outcomes and
    # costs nothing on warm-cache startups since each mmap is near-instant.
    # For the Groq provider eager-loading is essentially free — it just
    # constructs the OpenAI client and validates the API key — but we
    # still skip it under ``AGENT_EAGER_LOAD=0`` for symmetry with tests.
    if os.environ.get("AGENT_EAGER_LOAD", "1") != "0":
        logger.info(
            "Eager-loading %s models: main=%s analysis=%s drafting=%s",
            settings.provider,
            engine.model_name,
            analysis_engine.model_name,
            drafting_engine.model_name,
        )
        for name, eng in (
            ("main", engine),
            ("analysis", analysis_engine),
            ("drafting", drafting_engine),
        ):
            try:
                await eng.load()
            except Exception:
                logger.exception(
                    "Failed to eager-load %s engine; will fall back to "
                    "lazy load on first use",
                    name,
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

    detection_engine = _select_detection_engine(
        settings.pattern_detection_engine,
        engine, analysis_engine, drafting_engine,
    )
    pattern_recognizer = PatternRecognizer(
        event_bus=bus,
        evaluate=make_detection_evaluator(detection_engine),
    )
    idea_sources = [
        NextStepSuggester(
            event_bus=bus,
            evaluate=make_detection_evaluator(
                detection_engine, system_prompt=NEXT_STEP_SYSTEM_PROMPT,
            ),
        ),
        ChatGapDetector(
            event_bus=bus,
            evaluate=make_detection_evaluator(
                detection_engine, system_prompt=CHAT_GAP_SYSTEM_PROMPT,
            ),
        ),
    ]
    if settings.open_help_enabled:
        idea_sources.append(OpenHelpSuggester(
            event_bus=bus,
            evaluate=make_detection_evaluator(
                detection_engine, system_prompt=OPEN_HELP_SYSTEM_PROMPT,
            ),
            judge=make_detection_evaluator(
                detection_engine, max_tokens=120,
                system_prompt=IDEA_JUDGE_SYSTEM_PROMPT,
            ),
            feedback=IdeaFeedback(settings.idea_feedback_path),
            interval_seconds=settings.open_help_interval_seconds,
            min_score=settings.open_help_min_score,
        ))
    orchestrator = Orchestrator(
        engine=engine,
        event_bus=bus,
        memory=memory,
        tools=list(AVAILABLE_TOOLS),
        analysis_engine=analysis_engine,
        generated_tools_dir=settings.generated_tools_dir,
        pattern_recognizer=pattern_recognizer,
        idea_sources=idea_sources,
    )
    set_meta_tool_context(
        orchestrator=orchestrator,
        engine=engine,
        generated_dir=settings.generated_tools_dir,
        drafting_engine=drafting_engine,
    )
    try:
        orchestrator.load_dynamic_tools()
    except Exception:
        logger.exception("load_dynamic_tools at startup failed")
    await orchestrator.start()

    app.state.event_bus = bus
    app.state.engine = engine
    app.state.analysis_engine = analysis_engine
    app.state.drafting_engine = drafting_engine
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
            _mg._DRAFT_ENGINE = None  # type: ignore[attr-defined]
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


@app.get("/api/chats")
async def list_chat_log(limit: int = 200) -> list[dict[str, Any]]:
    """Return the tail of the persisted chat transcript.

    Used by the chat window to re-hydrate prior turns on first open or
    after a daemon restart. Each row is ``{id, event_id, role, text,
    created_at}`` in insertion order (oldest-first).
    """
    memory: Memory = app.state.memory
    try:
        return await asyncio.to_thread(memory.get_chat_log, limit)
    except Exception as exc:
        logger.exception("get_chat_log failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class SettingsUpdate(BaseModel):
    """Body for ``PUT /api/settings``. Omitted fields are left unchanged.

    ``groq_api_key``: omit or ``null`` keeps the saved key, ``""`` clears it.
    """

    provider: Optional[str] = None
    groq_api_key: Optional[str] = None
    groq_main_model: Optional[str] = None
    groq_analysis_model: Optional[str] = None
    groq_drafting_model: Optional[str] = None


# (request field, env/settings-file key, attribute on ``settings``)
_SETTING_FIELDS = (
    ("provider", "AGENT_PROVIDER", "provider"),
    ("groq_api_key", "GROQ_API_KEY", "groq_api_key"),
    ("groq_main_model", "GROQ_MAIN_MODEL", "groq_main_model"),
    ("groq_analysis_model", "GROQ_ANALYSIS_MODEL", "groq_analysis_model"),
    ("groq_drafting_model", "GROQ_DRAFTING_MODEL", "groq_drafting_model"),
)


def _key_hint(key: str) -> str:
    return f"…{key[-4:]}" if len(key) >= 8 else ""


def _settings_view() -> dict[str, Any]:
    """What the Settings window shows. Never returns the full API key.

    ``saved`` values apply on the next daemon start; ``running`` is what
    this process is using now. ``restart_required`` is true when they differ.
    """
    saved_file = user_settings.read()
    saved: dict[str, str] = {}
    running: dict[str, str] = {}
    for field_name, env_key, attr in _SETTING_FIELDS:
        running[field_name] = str(getattr(settings, attr) or "")
        if env_key in user_settings.shell_exported:
            # A shell export beats the file, so a restart won't change it.
            saved[field_name] = running[field_name]
        else:
            saved[field_name] = saved_file.get(env_key) or running[field_name]
    return {
        "provider": saved["provider"].lower(),
        "running_provider": running["provider"].lower(),
        "groq_api_key_set": bool(saved["groq_api_key"]),
        "groq_api_key_hint": _key_hint(saved["groq_api_key"]),
        "groq_main_model": saved["groq_main_model"],
        "groq_analysis_model": saved["groq_analysis_model"],
        "groq_drafting_model": saved["groq_drafting_model"],
        "restart_required": saved != running,
        "can_restart": _can_self_restart(),
        # Fields the UI can't change because they're exported in the shell.
        "locked_fields": [
            field_name for field_name, env_key, _ in _SETTING_FIELDS
            if env_key in user_settings.shell_exported
        ],
    }


def _can_self_restart() -> bool:
    # Under ``uvicorn --reload`` this process is a multiprocessing child;
    # re-exec'ing its argv would not bring the server back.
    import multiprocessing  # noqa: WPS433
    return multiprocessing.parent_process() is None


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return _settings_view()


@app.put("/api/settings")
async def put_settings(body: SettingsUpdate) -> dict[str, Any]:
    """Save provider settings from the UI. Takes effect after a restart."""
    updates: dict[str, str] = {}
    if body.provider is not None:
        provider = body.provider.strip().lower()
        if provider not in user_settings.PROVIDERS:
            raise HTTPException(status_code=400, detail="unknown provider")
        updates["AGENT_PROVIDER"] = provider
    if body.groq_api_key is not None:
        updates["GROQ_API_KEY"] = body.groq_api_key.strip()
    for field_name, env_key, _ in _SETTING_FIELDS[2:]:
        value = getattr(body, field_name)
        if value is not None:
            value = value.strip()
            # Don't pin the built-in default: storing "" lets a future
            # default change reach this user.
            if value == GROQ_DEFAULT_MODELS[env_key]:
                value = ""
            updates[env_key] = value

    # Refuse to save Groq without a key, or the next start would fail.
    current = user_settings.read()
    next_provider = updates.get(
        "AGENT_PROVIDER", current.get("AGENT_PROVIDER", settings.provider)
    )
    next_key = updates.get(
        "GROQ_API_KEY", current.get("GROQ_API_KEY", settings.groq_api_key)
    )
    if next_provider == "groq" and not next_key:
        raise HTTPException(
            status_code=400, detail="a Groq API key is required for Groq"
        )
    try:
        await asyncio.to_thread(user_settings.write, updates)
    except (OSError, ValueError) as exc:
        logger.exception("Saving settings failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _settings_view()


@app.post("/api/restart")
async def restart_daemon() -> dict[str, str]:
    """Re-exec the daemon so saved settings take effect.

    Replies first, then replaces this process with the same command line
    after a short delay so the HTTP response gets out.
    """
    if not _can_self_restart():
        raise HTTPException(
            status_code=409,
            detail="running under --reload; restart the daemon manually",
        )

    def _reexec() -> None:
        logger.info("Restarting daemon to apply settings")
        try:
            app.state.memory.close()
        except Exception:
            logger.exception("Error closing memory before restart")
        user_settings.clear_inherited_environ()
        # ``orig_argv`` is the full original command line, interpreter
        # included, e.g. ``[python, .venv/bin/uvicorn, main:app]``.
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])

    asyncio.get_running_loop().call_later(0.5, _reexec)
    return {"status": "restarting"}


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

    main_engine = app.state.engine
    hello = Status(
        seq=_next_seq(),
        timestamp=_now_iso(),
        payload=StatusPayload(
            state="ready",
            model_loaded=getattr(main_engine, "model_name", settings.model_path),
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
            elif isinstance(msg, UserMessage):
                bus: EventBus = app.state.event_bus
                await bus.push(
                    ContextEvent(
                        event_type="user_message",
                        metadata={
                            "event_id": msg.payload.message_id,
                            "text": msg.payload.text,
                        },
                    )
                )
            else:
                logger.debug("Ignoring inbound %s", type(msg).__name__)
    except WebSocketDisconnect:
        return
    finally:
        orchestrator.remove_subscriber(_ws_send)
