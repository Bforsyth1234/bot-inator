"""FastAPI entry point for the agent daemon.

Exposes:
- GET  /health      — liveness probe (no token needed)
- WS   /ws/stream   — bidirectional protocol for the SwiftUI MenuBar app
- /api/*            — tools, chats, settings, restart

Every route except ``GET /health``, and the WS handshake, needs
``Authorization: Bearer <~/.bot-inator/token>``; any request carrying a
browser ``Origin`` header is refused first (#27, see ``auth.py``).
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
from auth import AuthMiddleware, get_token

from ai.memory import Memory, set_default_memory
from ai.privacy import personal_text_allowed
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from ai.pattern_recognizer import PatternRecognizer, make_detection_evaluator
from ai.idea_sources import (
    CHAT_GAP_SYSTEM_PROMPT,
    NEXT_STEP_SYSTEM_PROMPT,
    ChatGapDetector,
    IdeaState,
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
from ai.friction import (
    FRICTION_JUDGE_SYSTEM_PROMPT,
    FRICTION_SYSTEM_PROMPT,
    FrictionDetector,
)
from ai.setup_scan import (
    SETUP_JUDGE_SYSTEM_PROMPT,
    SETUP_SCAN_SYSTEM_PROMPT,
    SetupScanner,
)
from events.calendar_watcher import CalendarWatcher
from events.clipboard_watcher import ClipboardWatcher
from events.event_bus import ContextEvent, EventBus
from events.file_watcher import FileWatcher
from events.imessage_watcher import IMessageWatcher
from events.mac_listeners import MacAppListener
from events.mail_watcher import MailWatcher
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

    Each engine's provider is :meth:`settings.engine_provider` for its
    role: ``AGENT_PROVIDER_<ROLE>`` when set, else ``AGENT_PROVIDER``
    (#26), so e.g. local main + analysis with Groq drafting is allowed.
    ``"groq"`` builds a :class:`~ai.groq_engine.GroqEngine` on that role's
    Groq model; ``"local"`` an :class:`MLXEngine` on its MLX model. Each
    engine is isolated so they can run on different model ids without
    sharing state. Unknown provider names resolve to ``"local"`` with a
    warning (see :func:`config.normalize_provider`) so a typo can neither
    brick startup nor send data to the cloud.
    """
    local_models = {
        "main": settings.model_path,
        "analysis": settings.analysis_model_path,
        "drafting": settings.drafting_model_path,
    }
    groq_models = {
        "main": settings.groq_main_model,
        "analysis": settings.groq_analysis_model,
        "drafting": settings.groq_drafting_model,
    }
    engines: list[Any] = []
    for role in ("main", "analysis", "drafting"):
        if settings.engine_provider(role) == "groq":
            from ai.groq_engine import GroqEngine  # noqa: WPS433
            engines.append(GroqEngine(
                groq_models[role],
                api_key=settings.groq_api_key,
                api_base=settings.groq_api_base,
            ))
        else:
            engines.append(MLXEngine(local_models[role]))
    return engines[0], engines[1], engines[2]


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
    # Create ~/.bot-inator/token on first start so clients can read it
    # (#27). Never log the value.
    get_token()

    bus = EventBus()
    bus.bind_loop(asyncio.get_running_loop())

    engine, analysis_engine, drafting_engine = _build_engines()
    # ``AGENT_MEMORY_DB`` lets tests keep the real ~/.bot-inator/memory.db
    # untouched (tests/conftest.py sets it to a temp file).
    memory_db = os.environ.get("AGENT_MEMORY_DB")
    memory = Memory(db_path=memory_db) if memory_db else Memory()
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
            settings.engine_providers(),
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
    app.state.mac_listener = None
    if _listeners_enabled():
        try:
            mac = MacAppListener(bus)
            mac.start()
            listeners.append(mac)
            app.state.mac_listener = mac
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
        for name, factory in (
            ("mail_watcher", MailWatcher),
            ("calendar_watcher", CalendarWatcher),
            ("clipboard_watcher", ClipboardWatcher),
        ):
            try:
                listener = factory(bus)
                listener.start()
                listeners.append(listener)
                listener_names.append(name)
            except Exception:
                logger.exception("Failed to start %s", name)

    detection_engine = _select_detection_engine(
        settings.pattern_detection_engine,
        engine, analysis_engine, drafting_engine,
    )
    pattern_recognizer = PatternRecognizer(
        event_bus=bus,
        evaluate=make_detection_evaluator(detection_engine),
    )
    # One feedback store and one cooldown store for every idea source: two
    # instances on the same file would overwrite each other.
    idea_feedback = IdeaFeedback(settings.idea_feedback_path)
    idea_state = IdeaState(settings.idea_feedback_path.with_name("idea_state.json"))
    idea_sources = [
        NextStepSuggester(
            event_bus=bus,
            evaluate=make_detection_evaluator(
                detection_engine, system_prompt=NEXT_STEP_SYSTEM_PROMPT,
            ),
            state=idea_state,
            feedback=idea_feedback,
        ),
        ChatGapDetector(
            event_bus=bus,
            evaluate=make_detection_evaluator(
                detection_engine, system_prompt=CHAT_GAP_SYSTEM_PROMPT,
            ),
            state=idea_state,
            feedback=idea_feedback,
        ),
    ]
    # One feedback store for every judged source: two IdeaFeedback
    # instances on the same file would overwrite each other's verdicts.
    # Exactly one source records verdicts (the orchestrator calls every
    # source's record_feedback).
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
            feedback=idea_feedback,
            state=idea_state,
            interval_seconds=settings.open_help_interval_seconds,
            min_score=settings.open_help_min_score,
        ))
    idea_sources.append(FrictionDetector(
        event_bus=bus,
        evaluate=make_detection_evaluator(
            detection_engine, system_prompt=FRICTION_SYSTEM_PROMPT,
        ),
        judge=make_detection_evaluator(
            detection_engine, max_tokens=120,
            system_prompt=FRICTION_JUDGE_SYSTEM_PROMPT,
        ),
        feedback=idea_feedback,
        records_feedback=not settings.open_help_enabled,
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

    setup_scanner: Optional[SetupScanner] = None
    if _listeners_enabled() and os.environ.get("AGENT_SETUP_SCAN", "1") != "0":
        setup_scanner = SetupScanner(
            event_bus=bus,
            # The scan's signals go to the detection engine (#26).
            cloud=settings.detection_is_cloud(),
            feedback=idea_feedback,
            evaluate=make_detection_evaluator(
                detection_engine, system_prompt=SETUP_SCAN_SYSTEM_PROMPT,
            ),
            judge=make_detection_evaluator(
                detection_engine, max_tokens=120,
                system_prompt=SETUP_JUDGE_SYSTEM_PROMPT,
            ),
            calendar_reader=_calendar_reader_if_granted,
        )
        setup_scanner.start()

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
        if setup_scanner is not None:
            try:
                await setup_scanner.stop()
            except Exception:
                logger.exception("Error stopping setup scanner")
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
# Origin check, then bearer token, on every HTTP route and the WS (#27).
app.add_middleware(AuthMiddleware)


def _next_seq() -> int:
    app.state.seq += 1
    return app.state.seq


async def _calendar_reader_if_granted() -> list[Any]:
    """Last 28 days of events for the setup scan, only if Calendar access
    was already granted: the scan must never cause a permission prompt."""
    from datetime import datetime, timedelta

    from tools import calendar_access

    try:
        backend = calendar_access.get_backend()
        if not backend.ready():
            return []
        now = datetime.now()
        return await asyncio.to_thread(
            calendar_access.list, now - timedelta(days=28), now,
        )
    except Exception:
        logger.exception("setup scan calendar read failed")
        return []


@app.get("/health")
async def health() -> dict[str, Any]:
    body: dict[str, Any] = {"status": "ok", "version": "0.1.0"}
    mac = getattr(app.state, "mac_listener", None)
    if mac is not None:
        body["listeners"] = {"nsworkspace": mac.status()}
    return body


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
        # Disabled (#23) and quarantined (#28) tools live in subdirectories
        # the loader skips. A name already listed above wins.
        for sub, flag in (("disabled", "disabled"), ("quarantine", "quarantined")):
            subdir = generated_dir / sub
            if not subdir.is_dir():
                continue
            for path in sorted(subdir.glob("*.py")):
                name = path.stem
                if name.startswith(".") or name in seen:
                    continue
                seen.add(name)
                row: dict[str, Any] = {
                    "name": name,
                    "description": _description_from_source(path),
                    "is_generated": True,
                    flag: True,
                }
                if flag == "quarantined":
                    row["error"] = _first_error_line(subdir / f"{name}.error.txt")
                rows.append(row)
    return rows


def _first_error_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    return lines[0][:240] if lines else ""


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
    ``provider_<role>``: ``""`` (or ``"default"``) means "same as
    ``provider``"; otherwise ``"local"`` or ``"groq"``.
    """

    provider: Optional[str] = None
    provider_main: Optional[str] = None
    provider_analysis: Optional[str] = None
    provider_drafting: Optional[str] = None
    groq_api_key: Optional[str] = None
    groq_main_model: Optional[str] = None
    groq_analysis_model: Optional[str] = None
    groq_drafting_model: Optional[str] = None


_ENGINE_ROLES = ("main", "analysis", "drafting")
# (request field, env/settings-file key, engine role) for the #26 overrides.
_OVERRIDE_FIELDS = tuple(
    (f"provider_{role}", f"AGENT_PROVIDER_{role.upper()}", role)
    for role in _ENGINE_ROLES
)
_MODEL_FIELDS = (
    ("groq_main_model", "GROQ_MAIN_MODEL"),
    ("groq_analysis_model", "GROQ_ANALYSIS_MODEL"),
    ("groq_drafting_model", "GROQ_DRAFTING_MODEL"),
)


def _running_value(field_name: str) -> str:
    """What this process uses for ``field_name`` (overrides normalized)."""
    if field_name.startswith("provider_"):
        return settings.engine_override(field_name[len("provider_"):])
    return str(getattr(settings, field_name) or "")


# (request field, env/settings-file key)
_SETTING_FIELDS = (
    ("provider", "AGENT_PROVIDER"),
    *((f, k) for f, k, _ in _OVERRIDE_FIELDS),
    ("groq_api_key", "GROQ_API_KEY"),
    *_MODEL_FIELDS,
)


def _clean_provider(value: str) -> str:
    """Lower-case a provider name; ``""`` when blank or invalid."""
    value = (value or "").strip().lower()
    return value if value in user_settings.PROVIDERS else ""


def _effective_providers(values: dict[str, str]) -> dict[str, str]:
    """Per-engine providers from a ``field -> value`` view (#26)."""
    default = _clean_provider(values["provider"]) or "local"
    return {
        role: _clean_provider(values[f"provider_{role}"]) or default
        for role in _ENGINE_ROLES
    }


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
    for field_name, env_key in _SETTING_FIELDS:
        running[field_name] = _running_value(field_name)
        if env_key in user_settings.shell_exported:
            # A shell export beats the file, so a restart won't change it.
            saved[field_name] = running[field_name]
        elif field_name.startswith("provider_"):
            # A blank override is meaningful ("same as default"), so an
            # override missing from the file is blank, unless it came
            # from .env (still in effect after a restart).
            saved[field_name] = (
                _clean_provider(saved_file.get(env_key, ""))
                or running[field_name]
            )
        else:
            saved[field_name] = saved_file.get(env_key) or running[field_name]
    saved_engines = _effective_providers(saved)
    return {
        "provider": saved["provider"].lower(),
        "running_provider": running["provider"].lower(),
        **{f: saved[f] for f, _, _ in _OVERRIDE_FIELDS},
        "engine_providers": saved_engines,
        "running_engine_providers": settings.engine_providers(),
        "groq_api_key_required": "groq" in saved_engines.values(),
        "groq_api_key_set": bool(saved["groq_api_key"]),
        "groq_api_key_hint": _key_hint(saved["groq_api_key"]),
        "groq_main_model": saved["groq_main_model"],
        "groq_analysis_model": saved["groq_analysis_model"],
        "groq_drafting_model": saved["groq_drafting_model"],
        "restart_required": saved != running,
        "can_restart": _can_self_restart(),
        # Fields the UI can't change because they're exported in the shell.
        "locked_fields": [
            field_name for field_name, env_key in _SETTING_FIELDS
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
    for field_name, env_key, role in _OVERRIDE_FIELDS:
        value = getattr(body, field_name)
        if value is None:
            continue
        value = value.strip().lower()
        if value == "default":
            value = ""
        if value and value not in user_settings.PROVIDERS:
            raise HTTPException(
                status_code=400, detail=f"unknown provider for {role} engine"
            )
        updates[env_key] = value
    if body.groq_api_key is not None:
        updates["GROQ_API_KEY"] = body.groq_api_key.strip()
    for field_name, env_key in _MODEL_FIELDS:
        value = getattr(body, field_name)
        if value is not None:
            value = value.strip()
            # Don't pin the built-in default: storing "" lets a future
            # default change reach this user.
            if value == GROQ_DEFAULT_MODELS[env_key]:
                value = ""
            updates[env_key] = value

    # Refuse to save a Groq engine without a key, or the next start would
    # fail. The key is required only if some engine uses Groq (#26).
    current = user_settings.read()

    def _next(field_name: str, env_key: str) -> str:
        if env_key in user_settings.shell_exported:
            return _running_value(field_name)
        if env_key in updates:
            return updates[env_key]
        if env_key in current:
            return current[env_key]
        return _running_value(field_name)

    next_values = {f: _next(f, k) for f, k in _SETTING_FIELDS}
    if (
        "groq" in _effective_providers(next_values).values()
        and not next_values["groq_api_key"]
    ):
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


def _checked_generated_name(tool_name: str, verb: str) -> None:
    """400 on a non-identifier or a built-in name (DELETE / disable / enable)."""
    if not _IDENTIFIER_RE.match(tool_name):
        raise HTTPException(status_code=400, detail="invalid tool name")
    orchestrator: Orchestrator = app.state.orchestrator
    if tool_name in orchestrator._builtin_tool_names:
        raise HTTPException(
            status_code=400, detail=f"cannot {verb} a built-in tool"
        )


def _commit_tool_paths(generated_dir: Path, rel_paths: list[str], message: str) -> None:
    """Best-effort nested-repo commit of each path (a move is two paths)."""
    try:
        from tools.meta_tool_generator import _git_commit  # noqa: WPS433
        for rel in rel_paths:
            ok, summary = _git_commit(generated_dir, rel, message)
            if not ok:
                logger.info("git commit of %s skipped: %s", rel, summary)
    except Exception:
        logger.exception("git commit of %s failed", rel_paths)


@app.delete("/api/tools/{tool_name}")
async def delete_tool(tool_name: str) -> dict[str, Any]:
    """Uninstall an agent-authored tool. Refuses to touch built-ins.

    Also removes a disabled (``disabled/``) or quarantined
    (``quarantine/`` plus its ``.error.txt``) copy.
    """
    _checked_generated_name(tool_name, "delete")
    orchestrator: Orchestrator = app.state.orchestrator
    generated_dir = settings.generated_tools_dir
    rel_paths = [
        f"{tool_name}.py",
        f"disabled/{tool_name}.py",
        f"quarantine/{tool_name}.py",
    ]
    present = [rel for rel in rel_paths if (generated_dir / rel).exists()]
    if not present:
        raise HTTPException(status_code=404, detail="tool not found")
    unloaded = orchestrator.unload_dynamic_tool(tool_name)
    try:
        for rel in present:
            (generated_dir / rel).unlink()
        (generated_dir / "quarantine" / f"{tool_name}.error.txt").unlink(
            missing_ok=True
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _commit_tool_paths(
        generated_dir, present, f"Removed generated tool: {tool_name}"
    )
    if not unloaded:
        # A disabled or quarantined copy: unload sent no tools_changed (#29).
        orchestrator.notify_tools_changed("deleted", [tool_name])
    return {"status": "ok", "tool_name": tool_name, "unloaded": unloaded}


@app.post("/api/tools/{tool_name}/disable")
async def disable_tool(tool_name: str) -> dict[str, Any]:
    """Suspend a generated tool: move it to ``disabled/`` and unload it (#23)."""
    _checked_generated_name(tool_name, "disable")
    orchestrator: Orchestrator = app.state.orchestrator
    generated_dir = settings.generated_tools_dir
    src = generated_dir / f"{tool_name}.py"
    dest = generated_dir / "disabled" / f"{tool_name}.py"
    if not src.exists():
        if dest.exists():
            return {"status": "ok", "tool_name": tool_name, "unloaded": False}
        raise HTTPException(status_code=404, detail="tool not found")
    if dest.exists():
        raise HTTPException(
            status_code=409, detail="a disabled copy already exists"
        )
    unloaded = orchestrator.unload_dynamic_tool(tool_name)
    try:
        dest.parent.mkdir(exist_ok=True)
        os.replace(src, dest)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _commit_tool_paths(
        generated_dir,
        [f"disabled/{tool_name}.py", f"{tool_name}.py"],
        f"Disabled generated tool: {tool_name}",
    )
    return {"status": "ok", "tool_name": tool_name, "unloaded": unloaded}


@app.post("/api/tools/{tool_name}/enable")
async def enable_tool(tool_name: str) -> dict[str, Any]:
    """Move a disabled (or quarantined) tool back and load it (#23).

    If it fails to load again the loader re-quarantines it; the response
    then has ``status: "quarantined"`` and the error's first line.
    """
    _checked_generated_name(tool_name, "enable")
    orchestrator: Orchestrator = app.state.orchestrator
    generated_dir = settings.generated_tools_dir
    dest = generated_dir / f"{tool_name}.py"
    sources = [
        generated_dir / "disabled" / f"{tool_name}.py",
        generated_dir / "quarantine" / f"{tool_name}.py",
    ]
    src = next((p for p in sources if p.exists()), None)
    if src is None:
        if dest.exists():
            return {"status": "ok", "tool_name": tool_name,
                    "loaded": tool_name in orchestrator._dynamic_tool_names}
        raise HTTPException(status_code=404, detail="tool not found")
    if dest.exists():
        raise HTTPException(
            status_code=409, detail="an enabled tool with this name exists"
        )
    try:
        os.replace(src, dest)
        if src.parent.name == "quarantine":
            (src.parent / f"{tool_name}.error.txt").unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    _commit_tool_paths(
        generated_dir,
        [f"{tool_name}.py", f"{src.parent.name}/{tool_name}.py"],
        f"Enabled generated tool: {tool_name}",
    )
    loaded = tool_name in await asyncio.to_thread(
        orchestrator.load_dynamic_tools, generated_dir
    )
    if not loaded:
        error = _first_error_line(
            generated_dir / "quarantine" / f"{tool_name}.error.txt"
        )
        return {"status": "quarantined", "tool_name": tool_name,
                "loaded": False, "error": error}
    return {"status": "ok", "tool_name": tool_name, "loaded": True}


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
        app.state.mac_listener = None
    elif action == "resume_listeners":
        logger.info("resume_listeners requested; no-op in Phase 1")
    elif action == "reload_model":
        try:
            await app.state.engine.load()
        except Exception:
            logger.exception("Failed to reload model")
    elif action == "clear_memory":
        try:
            removed = await asyncio.to_thread(app.state.memory.clear_memory)
            logger.info("clear_memory removed %s memories", removed)
        except Exception:
            logger.exception("Failed to clear memory")
    elif action == "reload_dynamic_tools":
        try:
            app.state.orchestrator.load_dynamic_tools()
        except Exception:
            logger.exception("Failed to reload dynamic tools")


def _listener_health() -> dict[str, dict[str, Any]] | None:
    """Liveness per supervised listener (#16/#19); ``None`` when none run."""
    mac = getattr(app.state, "mac_listener", None)
    if mac is None:
        return None
    try:
        return {"nsworkspace": mac.status()}
    except Exception:
        logger.exception("mac_listener.status() failed")
        return None


def _status_frame() -> Status:
    main_engine = app.state.engine
    return Status(
        seq=_next_seq(),
        timestamp=_now_iso(),
        payload=StatusPayload(
            state="ready",
            model_loaded=getattr(main_engine, "model_name", settings.model_path),
            listeners_active=list(app.state.listener_names),
            listener_health=_listener_health(),
            engine_providers=settings.engine_providers(),
            cloud_personal_text=personal_text_allowed(),
        ),
    )


# Fields that make a listener-health change worth a new status frame
# (``uptime_s`` changes every second and is left out).
_HEALTH_KEYS = ("alive", "pid", "restart_count", "last_error", "last_exit_code")
LISTENER_HEALTH_POLL_SECONDS = 5.0


def _health_signature(health: dict[str, dict[str, Any]] | None) -> Any:
    if not health:
        return None
    return {
        name: tuple(entry.get(k) for k in _HEALTH_KEYS)
        for name, entry in health.items()
    }


async def _push_listener_health(
    websocket: WebSocket, initial: dict[str, dict[str, Any]] | None
) -> None:
    """Send a fresh status frame when a listener dies or restarts."""
    last = _health_signature(initial)
    try:
        while True:
            await asyncio.sleep(LISTENER_HEALTH_POLL_SECONDS)
            sig = _health_signature(_listener_health())
            if sig == last:
                continue
            last = sig
            await websocket.send_text(_status_frame().model_dump_json())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("listener health push stopped", exc_info=True)


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

    hello = _status_frame()
    await websocket.send_text(hello.model_dump_json())
    # Notices published before this client connected (#19).
    for frame in orchestrator.notice_frames():
        await websocket.send_text(frame.model_dump_json())
    health_task = asyncio.create_task(
        _push_listener_health(websocket, hello.payload.listener_health),
        name="ws-listener-health",
    )

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
        health_task.cancel()
        orchestrator.remove_subscriber(_ws_send)
