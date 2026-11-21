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
import time
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
from ai import activity_store, egress_log
from ai.idea_outcomes import (
    DecisionLog,
    OutcomeStore,
    default_decisions_path,
    default_outcomes_path,
)
from ai.idea_sources import (
    CHAT_GAP_SYSTEM_PROMPT,
    NEEDS_REPLY_JUDGE_SYSTEM_PROMPT,
    NEXT_STEP_SYSTEM_PROMPT,
    ChatGapDetector,
    IdeaState,
    NextStepSuggester,
)
from ai.open_help import (
    IDEA_JUDGE_SYSTEM_PROMPT,
    OPEN_HELP_SYSTEM_PROMPT,
    JUDGE_MAX_TOKENS,
    IdeaFeedback,
    OpenHelpSuggester,
)
# GroqEngine is imported lazily inside ``lifespan`` so the daemon still
# boots cleanly when ``settings.provider == "local"`` and the optional
# Groq dependency tree is missing.
from config import GROQ_DEFAULT_MODELS, default_mlx_models, settings
import providers
from ai.idea_match import DeclinedIdeaMatcher, make_sync_judge
from ai.friction import (
    FRICTION_JUDGE_SYSTEM_PROMPT,
    FRICTION_SYSTEM_PROMPT,
    FrictionDetector,
)
from ai.routine_ideas import RoutineIdeaSource
from ai.setup_scan import (
    SETUP_JUDGE_SYSTEM_PROMPT,
    SETUP_SCAN_SYSTEM_PROMPT,
    SetupScanner,
)
from events.calendar_watcher import CalendarWatcher
from events.clipboard_watcher import ClipboardWatcher
from events.screen_watcher import ScreenWatcher  # #147
from events.daily_brief import DailyBriefScheduler
# #128: imported as the module on purpose (``weekly_report`` is also its tool).
from events import weekly_report as weekly_report_mod
from events.weekly_report import WeeklyReportScheduler
from events.event_bus import ContextEvent, EventBus
from events.file_watcher import FileWatcher
from events.imessage_watcher import IMessageWatcher
from events.mac_listeners import MacAppListener
from events.mail_watcher import MailWatcher
from events.meeting_prep import MeetingPrepScheduler
from schemas.ws_messages import (
    ApprovalResponse,
    CalendarRequest,
    CalendarRequestPayload,
    CalendarResponse,
    CalendarStatus,
    CodeApprovalResponse,
    Command,
    ContactsRequest,
    ContactsRequestPayload,
    ContactsResponse,
    ContactsStatus,
    DemonstrationDraft,
    DemonstrationTrace,
    DemonstrationTracePayload,
    MeetingAudioReady,
    MeetingRecordOffer,
    MeetingRecordOfferPayload,
    MeetingRecordState,
    MeetingRecordStop,
    MeetingRecordStopPayload,
    ScreenTextRequest,
    ScreenTextRequestPayload,
    ScreenTextResponse,
    ScreenTextStatus,
    Status,
    StatusPayload,
    UserMessage,
    WSMessage,
)
from tools import AVAILABLE_TOOLS
from tools import calendar_access
# #127: Contacts through the menu bar app (the module; the tool is find_contact).
from tools import contacts as contacts_tool
# #89: the module (``tools.read_front_window`` stays the module, see
# tools/__init__.py), for its WS bridge to the menu bar app.
from tools import read_front_window as screen_text
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
    Any preset (``"groq"``, ``"openai"``, ...) builds an
    :class:`~ai.groq_engine.OpenAICompatibleEngine` on that role's model
    (#97); ``"local"`` an :class:`MLXEngine` on its MLX model. Each
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
    from ai import first_run  # noqa: WPS433

    if first_run.is_pending():
        # #93: nothing loads or downloads until the user picks an engine.
        logger.info("First run not answered yet: no engine loaded")
        pending = [first_run.PendingEngine(r) for r in ("main", "analysis", "drafting")]
        return pending[0], pending[1], pending[2]
    engines: list[Any] = []
    for role in ("main", "analysis", "drafting"):
        provider = settings.engine_provider(role)
        if provider != "local":
            # Every non-local provider is an OpenAI-compatible preset (#97).
            from ai.groq_engine import build_engine  # noqa: WPS433
            engines.append(build_engine(settings, provider, role))
        else:
            # #136: a model id that fails to load falls back to this
            # role's default (only if that is already on this Mac).
            engines.append(MLXEngine(
                local_models[role],
                fallback_model=default_mlx_models()[_MLX_MODEL_KEYS[role]],
            ))
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
    # #132: seeded from the clock (microseconds), like Orchestrator._seq, so
    # seq only grows across daemon restarts.
    app.state.seq = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
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
    from ai.first_run import PendingEngine  # noqa: WPS433

    if os.environ.get("AGENT_EAGER_LOAD", "1") != "0" and not isinstance(engine, PendingEngine):
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
            # #147: idle until the user starts a screen-watch session.
            ("screen_watcher", ScreenWatcher),
            ("daily_brief", DailyBriefScheduler),
            ("weekly_report", WeeklyReportScheduler),
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
    # #76: next-step cards for documents and mail invites / deadlines
    # arrive filled in. Read-only preparation, one detection-engine call.
    from ai.prepared_offers import Preparer  # noqa: WPS433

    preparer = Preparer(
        evaluate=make_detection_evaluator(
            detection_engine,
            system_prompt="Extract facts; reply with one JSON object only.",
        ),
        role="detection",
    )
    idea_sources = [
        NextStepSuggester(
            event_bus=bus,
            evaluate=make_detection_evaluator(
                detection_engine, system_prompt=NEXT_STEP_SYSTEM_PROMPT,
            ),
            state=idea_state,
            feedback=idea_feedback,
            # #70: reply drafts for mail that asks the user something.
            reply_judge=make_detection_evaluator(
                detection_engine, max_tokens=JUDGE_MAX_TOKENS,
                system_prompt=NEEDS_REPLY_JUDGE_SYSTEM_PROMPT,
            ),
            preparer=preparer,
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
                detection_engine, max_tokens=JUDGE_MAX_TOKENS,
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
            detection_engine, max_tokens=JUDGE_MAX_TOKENS,
            system_prompt=FRICTION_JUDGE_SYSTEM_PROMPT,
        ),
        feedback=idea_feedback,
        records_feedback=not settings.open_help_enabled,
    ))
    # #71: ``event_starting_soon`` -> one read-only ``meeting_prep`` event.
    # An idea source (the orchestrator calls its observe) and a listener
    # (stopped at shutdown). It only reacts to calendar_watcher events, so
    # it is harmless without listeners.
    try:
        meeting_prep = MeetingPrepScheduler(bus)
        meeting_prep.start()
        listeners.append(meeting_prep)
        listener_names.append("meeting_prep")
        idea_sources.append(meeting_prep)
    except Exception:
        logger.exception("Failed to start meeting_prep")
    # #86: offers to take meeting notes (the app records, only on a click)
    # and turns a finished recording into one prepared-offer card. The
    # transcript's summary uses LOCAL engines only (never a cloud one).
    app.state.meeting_notes = None
    try:
        from events.meeting_notes import MeetingNotes  # noqa: WPS433

        meeting_notes = MeetingNotes(bus, evaluators=[
            (role, make_detection_evaluator(
                eng, system_prompt="Summarize; reply with one JSON object only."))
            for role, eng in (("main", engine), ("analysis", analysis_engine),
                              ("drafting", drafting_engine))
            if eng is not None
        ])
        meeting_notes.start()
        listeners.append(meeting_notes)
        listener_names.append("meeting_notes")
        idea_sources.append(meeting_notes)
        app.state.meeting_notes = meeting_notes
    except Exception:
        logger.exception("Failed to start meeting_notes")
    # #79: what happens to each suggestion, and every card decision.
    idea_outcomes = OutcomeStore(_store_path("AGENT_IDEA_OUTCOMES_PATH", default_outcomes_path()))
    approval_decisions = DecisionLog(
        _store_path("AGENT_APPROVAL_DECISIONS_PATH", default_decisions_path())
    )
    # #88: the user's own "watch for X, offer Y" rules: an idea source (the
    # orchestrator calls its observe) and a listener (time watches tick).
    from ai import watches as watches_mod  # noqa: WPS433

    watch_store = watches_mod.WatchStore()
    try:
        watch_source = watches_mod.WatchSource(bus, watch_store, decisions=approval_decisions)
        watch_source.start()
        listeners.append(watch_source)
        listener_names.append("watches")
        idea_sources.append(watch_source)
    except Exception:
        logger.exception("Failed to start watches")
    app.state.watches = watch_store
    app.state.watch_compiler = watches_mod.Compiler(
        make_detection_evaluator(detection_engine, system_prompt=watches_mod.COMPILE_SYSTEM_PROMPT),
        role="detection",
    )
    # Rarely-kept judged sources get a higher judge bar and a longer cooldown.
    for source in idea_sources:
        if isinstance(source, (OpenHelpSuggester, FrictionDetector)):
            source.outcomes = idea_outcomes
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
    orchestrator.declined_matcher = DeclinedIdeaMatcher(
        idea_feedback, judge=make_sync_judge(detection_engine),
    )
    orchestrator.idea_outcomes = idea_outcomes
    orchestrator.approval_decisions = approval_decisions
    # #83: every approved side-effecting call, with Undo where it can.
    from ai import action_journal  # noqa: WPS433

    app.state.action_journal = action_journal.ActionJournal(
        _store_path("AGENT_ACTIONS_DB", action_journal.default_path()), outcomes=idea_outcomes,
    )
    orchestrator.action_journal = app.state.action_journal
    # #126: the timeline tool reads the journal the daemon already has open.
    from tools.recall_activity import set_sources as set_recall_sources  # noqa: WPS433

    set_recall_sources(journal=app.state.action_journal)
    # #128: the weekly report counts from the stores the daemon holds.
    weekly_report_mod.configure(
        journal=app.state.action_journal, outcomes=idea_outcomes,
        decisions=approval_decisions, watches=watch_store,
        generated_dir=settings.generated_tools_dir,
    )
    # #118: opt-in training corpus (records nothing while the opt-in is off).
    from ai import training_corpus  # noqa: WPS433

    app.state.training_corpus = training_corpus.TrainingCorpus(
        _store_path("AGENT_TRAINING_DB", training_corpus.default_path()),
    )
    orchestrator.training_corpus = app.state.training_corpus
    app.state.action_journal.add_undo_listener(app.state.training_corpus.undo_listener)
    # #119: offer to train a personal model once the corpus is big enough.
    from ai import personal_training  # noqa: WPS433
    from ai.mlx_engine import DEFAULT_MODEL as _MLX_DEFAULT, MLXEngine as _MLX  # noqa: WPS433

    def _training_base_model() -> str:
        # A LoRA adapter sits on the local MLX model, even when chat uses a cloud provider.
        return engine.model_name if isinstance(engine, _MLX) else _MLX_DEFAULT

    async def _training_finished(status: dict) -> None:
        state = status.get("state")
        if state == "done":
            body = "Your personal model finished training. It is checked against the original before it is used."
            if status.get("adapter_dir"):
                asyncio.create_task(_gate_and_offer(str(status["adapter_dir"])))
        elif state == "failed":
            body = f"Training stopped: {status.get('error') or 'unknown error'}. Nothing changed."
        else:
            return
        orchestrator.publish_notice({
            "id": f"training_{state}:{status.get('adapter_dir') or ''}", "level": "info",
            "title": "Personal model", "body": body,
        })

    async def _gate_and_offer(adapter_dir: str) -> None:
        """#120: score the new adapter against the base model (#121 gate);
        on a win, ask with a normal card; otherwise say why in a notice."""
        pm = app.state.personal_model
        try:
            gate = await pm.gate(adapter_dir)
            summary, lines = model_eval.switch_offer(gate)
            n = int(Path(adapter_dir).name)
            if summary is None:
                orchestrator.publish_notice({
                    "id": f"personal_model_gate:{n}", "level": "info",
                    "title": "Personal model", "body": " ".join(lines),
                })
                return
            approved, _ = await orchestrator._request_approval(
                f"use_model_{n}", "use_personal_model", {"kwargs": {"n": n}},
                " ".join(lines), summary=summary,
            )
            if approved:
                await pm.use_adapter(n)
        except Exception:
            logger.exception("personal model gate/offer failed")

    app.state.personal_training = personal_training.PersonalTraining(
        app.state.training_corpus, base_model=_training_base_model,
        on_finished=_training_finished,
    )
    orchestrator.personal_training = app.state.personal_training
    # #120: the user's personal model (adapter) — used only after it wins the
    # #121 gate and the user says so; "Use base model" any time.
    from ai import model_compare, model_eval  # noqa: WPS433

    app.state.personal_model = model_eval.PersonalModel(
        personal_training.default_models_dir(), engine,
        base_model=_training_base_model, corpus=app.state.training_corpus,
        settings=settings,
        comparison=model_compare.LiveComparison(app.state.training_corpus),
    )
    orchestrator.personal_model = app.state.personal_model
    try:
        await app.state.personal_model.apply_active()
    except Exception:
        logger.exception("personal model: apply_active failed")
    # #118/#79: approved tools never run in 30 days count against their offer.
    try:
        for _offer_id in idea_outcomes.never_run_offer_ids():
            app.state.training_corpus.mark_tool_never_run(_offer_id)
    except Exception:
        logger.exception("training corpus: never-run sweep failed")
    # #90: generated-tool health; repeated failures offer a repair.
    from ai import tool_health  # noqa: WPS433

    app.state.tool_health = tool_health.ToolHealthLog(
        _store_path("AGENT_TOOL_HEALTH_PATH", tool_health.default_path()), state=idea_state,
    )
    orchestrator.tool_health = app.state.tool_health
    # #87: hold offers at bad moments, the daily cap, the Inbox and a real
    # Pause (listeners keep running).
    from ai.delivery import Delivery, default_held_path, default_settings  # noqa: WPS433

    def _broadcast_status() -> None:
        try:
            orchestrator._schedule(orchestrator._send(_status_frame()))
        except Exception:
            logger.exception("status broadcast after a delivery change failed")

    delivery = Delivery(
        default_held_path(), settings=default_settings(), outcomes=idea_outcomes,
        gate=_delivery_gate(), on_change=_broadcast_status,
    )
    orchestrator.delivery = delivery
    app.state.delivery = delivery
    # #134: the local knowledge index (every source off until the user turns
    # it on) and its background indexer; Chat searches it (part 2).
    from ai import knowledge_index as knowledge_index_mod  # noqa: WPS433

    app.state.knowledge_index = knowledge_index_mod.KnowledgeIndex()
    knowledge_index_mod.set_default_index(app.state.knowledge_index)
    orchestrator.knowledge_index = app.state.knowledge_index
    app.state.knowledge_indexer = knowledge_index_mod.KnowledgeIndexer(app.state.knowledge_index)
    await app.state.knowledge_indexer.start()
    # #78: standing rules ("always do this"): offers after 5 clean
    # approvals, runs without a card (journaled, Undo), never while paused.
    from ai import standing_rules as standing_rules_mod  # noqa: WPS433

    standing_rules = standing_rules_mod.StandingRules(
        standing_rules_mod.default_path(),
        paused=lambda: bool(orchestrator.delivery is not None and orchestrator.delivery.paused),
        classify=NextStepSuggester.classify,
        watch_lookup=watch_store.get,
    )
    orchestrator.standing_rules = standing_rules
    orchestrator.watch_lookup = watch_store.get
    app.state.action_journal.add_undo_listener(standing_rules.on_undo)
    app.state.standing_rules = standing_rules
    # #116 Always allow (the card's button) and #115 one card per burst of
    # email suggestions.
    from ai import always_allow as always_allow_mod  # noqa: WPS433
    from ai.card_batch import CardBatcher  # noqa: WPS433

    always_allow_store = always_allow_mod.default_store()
    orchestrator.always_allow = always_allow_store
    app.state.always_allow = always_allow_store
    app.state.action_journal.add_undo_listener(always_allow_store.on_undo)
    orchestrator.card_batcher = CardBatcher(orchestrator.event_bus.push_threadsafe)

    # #84 "Watch me": a recording takes events first (Orchestrator._handle_event).
    from ai.demonstration import DemonstrationController  # noqa: WPS433

    async def _send_demo_trace(payload: dict[str, Any]) -> None:
        await orchestrator._send(DemonstrationTrace(
            seq=orchestrator._next_seq(), timestamp=_now_iso(),
            payload=DemonstrationTracePayload(**payload),
        ))

    demonstration = DemonstrationController(
        publish=bus.push, send=_send_demo_trace,
        shortcuts=lambda: _shortcut_names_or_none(),
    )
    orchestrator.demonstration = demonstration
    app.state.demonstration = demonstration

    # #85 iPhone remote (off by default, 7c): the own Messages thread can
    # ask, and answer a mirrored card with "yes <code>".
    from events.remote_channel import RemoteChannel  # noqa: WPS433

    remote = RemoteChannel(bus, paused=lambda: bool(delivery.paused))
    remote.attach_orchestrator(orchestrator, asyncio.get_running_loop())
    orchestrator.remote_channel = remote
    app.state.remote_channel = remote
    if _listeners_enabled():
        try:
            remote.start()
            listeners.append(remote)
            listener_names.append("remote_channel")
        except Exception:
            logger.exception("Failed to start the remote channel")

    # #89: read_front_window answers only in a turn the user started.
    screen_text.set_turn_kind_provider(lambda: getattr(orchestrator, "_turn_kind", ""))
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
    # #69: other apps' local MCP servers (stdio). Tools stay off until the
    # user enables them in Tool Manager, and stay gated unless marked.
    from ai.mcp_client import McpManager, attach_to_orchestrator  # noqa: WPS433

    mcp_manager = McpManager()
    try:
        attach_to_orchestrator(mcp_manager, orchestrator)
        await asyncio.to_thread(mcp_manager.start, 20)
    except Exception:
        logger.exception("MCP manager failed to start")
    app.state.mcp_manager = mcp_manager
    await orchestrator.start()
    delivery_task = asyncio.create_task(delivery.run_release_loop(bus), name="delivery-release")

    async def _personal_model_daily() -> None:
        # #121 decision 5: once a day, switch back to the base model if the
        # personal model does worse on live cards (a notice, not a card).
        while True:
            await asyncio.sleep(24 * 3600)
            try:
                text = await app.state.personal_model.check_switch_back()
                if text:
                    orchestrator.publish_notice({
                        "id": f"personal_model_switch_back:{int(time.time() // 86400)}",
                        "level": "info", "title": "Personal model", "body": text,
                    })
            except Exception:
                logger.exception("personal model daily check failed")

    personal_model_task = asyncio.create_task(_personal_model_daily(), name="personal-model-daily")

    # #75: once a day, mine the activity history for routines and offer
    # one through the idea pipeline (source "routine").
    routine_ideas: Optional[RoutineIdeaSource] = None
    if _listeners_enabled() and os.environ.get("AGENT_ROUTINE_IDEAS", "1") != "0":
        routine_ideas = RoutineIdeaSource(
            event_bus=bus, state=idea_state, feedback=idea_feedback,
            shortcuts=_shortcut_names_or_none,
        )
        routine_ideas.start()

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
                detection_engine, max_tokens=JUDGE_MAX_TOKENS,
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
    # #130: chat on the idea / code cards (memory only; main engine for the
    # idea, the drafting engine for the code, as for what rewrites them).
    from ai import card_chat as card_chat_mod  # noqa: WPS433
    from tools import meta_tool_generator as _mtg_chat  # noqa: WPS433

    app.state.card_chat = card_chat_mod.CardChatStore(
        engine_for=lambda kind: orchestrator.engine if kind == card_chat_mod.IDEA
        else (_mtg_chat._DRAFT_ENGINE or orchestrator.engine),
    )
    orchestrator.card_chat = app.state.card_chat
    card_chat_mod.set_default_store(app.state.card_chat)
    app.state.idea_feedback = idea_feedback
    app.state.idea_outcomes = idea_outcomes
    app.state.approval_decisions = approval_decisions
    app.state.pattern_recognizer = pattern_recognizer
    app.state.listeners = listeners
    # #65: the run-now route uses the scheduler when it is running.
    app.state.daily_brief = next(
        (l for l in listeners if isinstance(l, DailyBriefScheduler)), None
    )
    # #73 / #77: offers after the scheduled brief share the idea cooldowns
    # and the declined-idea store.
    if app.state.daily_brief is not None:
        app.state.daily_brief.attach_ideas(idea_state, idea_feedback)
    app.state.weekly_report = next(
        (l for l in listeners if isinstance(l, WeeklyReportScheduler)), None,
    )
    app.state.listener_names = listener_names

    try:
        yield
    finally:
        try:
            await app.state.knowledge_indexer.stop()  # #134
            app.state.knowledge_index.close()
        except Exception:
            logger.exception("Error stopping the knowledge indexer")
        knowledge_index_mod.set_default_index(None)
        orchestrator.knowledge_index = None
        delivery_task.cancel()
        personal_model_task.cancel()
        try:
            await delivery_task
        except (asyncio.CancelledError, Exception):
            pass
        if routine_ideas is not None:
            try:
                await routine_ideas.stop()
            except Exception:
                logger.exception("Error stopping routine ideas")
        try:
            await asyncio.to_thread(app.state.mcp_manager.stop)
        except Exception:
            logger.exception("Error stopping MCP servers")
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
        try:
            app.state.action_journal.close()
        except Exception:
            logger.exception("Error closing the action journal")
        try:
            await app.state.personal_training.cancel()  # #119: never leave a child training
        except Exception:
            logger.exception("Error stopping personal training")
        try:
            app.state.training_corpus.close()
        except Exception:
            logger.exception("Error closing the training corpus")
        set_default_memory(None)
        screen_text.set_turn_kind_provider(None)
        try:
            # #107: the vision model is never built here or eager-loaded (it
            # loads on the first look_at_screen); free it if one was used.
            from ai import vision_engine as _vision  # noqa: WPS433
            _vision.reset_local_engine()
        except Exception:
            logger.exception("Error unloading the vision model")
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
# Wave 8 routers (todo rule 12): declined ideas (#56), permissions (#59),
# tool detail (#61). The app-wide AuthMiddleware covers them.
from routes import activity as activity_routes  # noqa: E402
from routes import daily_brief as daily_brief_routes  # noqa: E402
from routes import weekly_report as weekly_report_routes  # noqa: E402
from routes import web_search as web_search_routes  # noqa: E402
from routes import ideas as ideas_routes  # noqa: E402
from routes import mcp as mcp_routes  # noqa: E402
from routes import meeting_prep as meeting_prep_routes  # noqa: E402
from routes import outcomes as outcomes_routes  # noqa: E402
from routes import permissions as permissions_routes  # noqa: E402
from routes import preferences as preferences_routes  # noqa: E402
from routes import providers as providers_routes  # noqa: E402
from routes.tool_detail import router as tool_detail_router  # noqa: E402

app.include_router(daily_brief_routes.router)
app.include_router(weekly_report_routes.router)
app.include_router(web_search_routes.router)
app.include_router(ideas_routes.router)
app.include_router(permissions_routes.router)
app.include_router(tool_detail_router)
# Wave 13: outcomes (#79), activity history (#75), meeting prep (#71),
# MCP servers (#69).
app.include_router(outcomes_routes.router)
app.include_router(activity_routes.router)
app.include_router(meeting_prep_routes.router)
app.include_router(mcp_routes.router)
# Wave 14 (#72): learned preferences.
app.include_router(preferences_routes.router)
# Wave 15: History + Undo (#83), privacy ledger (#82), delivery / Inbox /
# Pause (#87), watches (#88), tool health + repair (#90).
from routes import actions as actions_routes  # noqa: E402
from routes import delivery as delivery_routes  # noqa: E402
from routes import egress as egress_routes  # noqa: E402
from routes import tool_health as tool_health_routes  # noqa: E402
from routes import watches as watch_routes  # noqa: E402

app.include_router(actions_routes.router)
app.include_router(egress_routes.router)
app.include_router(delivery_routes.router)
app.include_router(watch_routes.router)
app.include_router(tool_health_routes.router)
# Wave 16: standing rules (#78), iPhone remote (#85), meeting notes (#86).
from routes import meeting_notes as meeting_notes_routes  # noqa: E402
from routes import remote as remote_routes  # noqa: E402
from routes import rules as rules_routes  # noqa: E402

app.include_router(rules_routes.router)
app.include_router(remote_routes.router)
app.include_router(meeting_notes_routes.router)
# Wave 22: opt-in training corpus (#118).
from routes import training as training_routes  # noqa: E402

app.include_router(training_routes.router)
from routes import personal_model as personal_model_routes  # noqa: E402

app.include_router(personal_model_routes.router)
# #97: provider presets (List models, Test).
app.include_router(providers_routes.router)
# #93: first-run engine choice.
from routes import first_run as first_run_routes  # noqa: E402

app.include_router(first_run_routes.router)
# #116: Settings → Always allowed.
from routes import always_allow as always_allow_routes  # noqa: E402

app.include_router(always_allow_routes.router)
from routes import knowledge as knowledge_routes  # noqa: E402

app.include_router(knowledge_routes.router)  # #134
from routes import card_chat as card_chat_routes  # noqa: E402

app.include_router(card_chat_routes.router)  # #130
from routes import screen_watch as screen_watch_routes  # noqa: E402

app.include_router(screen_watch_routes.router)  # #147


def _next_seq() -> int:
    app.state.seq += 1
    return app.state.seq


def _delivery_gate() -> Any:
    """#87's busy checks; ``AGENT_DELIVERY_BUSY_CHECKS=0`` (tests) turns off
    the calendar / screen-share / typing checks (Pause still works)."""
    if os.environ.get("AGENT_DELIVERY_BUSY_CHECKS", "1").strip() != "0":
        return None
    from ai.delivery import BusyGate  # noqa: WPS433

    return BusyGate(calendar=lambda now: [], processes=set, seconds_since_key=lambda: 1e9)


def _store_path(env_key: str, default: Path) -> Path:
    """``env_key`` overrides a ``~/.bot-inator`` store path (tests)."""
    override = os.environ.get(env_key, "").strip()
    return Path(override).expanduser() if override else default


def _shortcut_names_or_none() -> list[str]:
    """The user's Shortcut names for routine mapping ([] on any failure)."""
    from tools.shortcuts import _shortcut_names  # noqa: WPS433

    return _shortcut_names()


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
        row = {
            "name": name,
            "description": _description_for_tool(tool_obj),
            "is_generated": is_generated,
        }
        mcp = getattr(orchestrator, "mcp_manager", None)
        if mcp is not None and mcp.is_mcp_tool(name):
            # #69: another app's tool, managed in the MCP servers section.
            row["kind"] = "mcp"
        rows.append(row)

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
    ``provider``"; otherwise ``"local"`` or a preset id (#97).
    """

    provider: Optional[str] = None
    provider_main: Optional[str] = None
    provider_analysis: Optional[str] = None
    provider_drafting: Optional[str] = None
    groq_api_key: Optional[str] = None
    groq_main_model: Optional[str] = None
    groq_analysis_model: Optional[str] = None
    groq_drafting_model: Optional[str] = None
    # Let cloud engines see email and message text (#52). Applies at once.
    cloud_personal_text: Optional[bool] = None
    # #97: per-preset key, base URL and models, keyed by preset id. Groq's
    # entries are the same settings as the ``groq_*`` fields above.
    preset_settings: Optional[dict[str, "PresetUpdate"]] = None
    # #136: the local MLX model per engine (a Hugging Face repo id or a
    # folder). ``""`` removes the saved value (back to the .env value or
    # the default); ignored while ``MLX_<ROLE>_MODEL`` is a shell export.
    mlx_main_model: Optional[str] = None
    mlx_analysis_model: Optional[str] = None
    mlx_drafting_model: Optional[str] = None


class PresetUpdate(BaseModel):
    """One preset's fields in ``PUT /api/settings``. ``None`` keeps the
    saved value, ``""`` clears it (back to the preset default)."""

    api_key: Optional[str] = None
    api_base: Optional[str] = None
    main_model: Optional[str] = None
    analysis_model: Optional[str] = None
    drafting_model: Optional[str] = None


SettingsUpdate.model_rebuild()


# Settings-file key for ``cloud_personal_text`` (same name as the env var).
_PERSONAL_TEXT_KEY = "AGENT_CLOUD_PERSONAL_TEXT"

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


# #136: the local MLX model key, the ``settings`` field this process runs,
# and the ``app.state`` engine, per role.
_MLX_MODEL_KEYS = {role: f"MLX_{role.upper()}_MODEL" for role in _ENGINE_ROLES}
_MLX_RUNNING_ATTRS = {
    "main": "model_path",
    "analysis": "analysis_model_path",
    "drafting": "drafting_model_path",
}
_MLX_ENGINE_ATTRS = {
    "main": "engine",
    "analysis": "analysis_engine",
    "drafting": "drafting_engine",
}


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


def _preset_fields(preset: providers.Preset) -> tuple[tuple[str, str], ...]:
    """(field in ``PresetUpdate``, env/settings-file key) for ``preset``."""
    return (
        ("api_key", preset.key_env()),
        ("api_base", preset.base_env()),
        *((f"{role}_model", preset.model_env(role)) for role in _ENGINE_ROLES),
    )


def _preset_values(
    preset: providers.Preset, overlay: Optional[dict[str, str]] = None
) -> dict[str, str]:
    """The values ``preset`` gets on the next start (blank → its default).

    Precedence: shell export, then ``overlay`` (unsaved updates), then the
    settings file, then this process's environment (``.env``)."""
    saved_file = user_settings.read()
    overlay = overlay or {}
    out: dict[str, str] = {}
    for field_name, env_key in _preset_fields(preset):
        if env_key in user_settings.shell_exported:
            value = os.environ.get(env_key, "")
        elif env_key in overlay:
            value = overlay[env_key]
        elif env_key in saved_file:
            value = saved_file[env_key]
        else:
            value = os.environ.get(env_key, "")
        if not value and field_name == "api_base":
            value = preset.api_base
        if not value and field_name.endswith("_model"):
            value = preset.default_models.get(field_name[: -len("_model")], "")
        out[field_name] = value
    return out


def _preset_problem(preset: providers.Preset, values: dict[str, str], roles: set[str]) -> str:
    """Why ``preset`` can't run ``roles`` with ``values``; ``""`` if it can."""
    if preset.key_required and not values["api_key"]:
        if preset.id == "groq":
            return "a Groq API key is required for Groq"
        return f"{preset.label} needs an API key"
    if not values["api_base"]:
        return f"{preset.label} needs a base URL"
    for role in sorted(roles):
        if not values[f"{role}_model"]:
            return f"{preset.label} needs a {role} model: pick one in Settings"
    return ""


def _presets_view(saved_engines: dict[str, str]) -> list[dict[str, Any]]:
    """Every preset as the Settings picker shows it. Never a full key."""
    out = []
    for preset in providers.PRESETS.values():
        values = _preset_values(preset)
        roles = {r for r, p in saved_engines.items() if p == preset.id}
        out.append({
            "id": preset.id,
            "label": preset.label,
            "tested": preset.tested,
            "docs_url": preset.docs_url,
            "key_required": preset.key_required,
            "key_set": bool(values["api_key"]),
            "key_hint": _key_hint(values["api_key"]),
            "api_base": values["api_base"],
            "default_api_base": preset.api_base,
            "cloud": not providers.is_loopback_url(values["api_base"]),
            "models": {r: values[f"{r}_model"] for r in _ENGINE_ROLES},
            "default_models": dict(preset.default_models),
            "in_use": sorted(roles),
            "problem": _preset_problem(preset, values, roles) if roles else "",
            "locked_fields": [
                f for f, k in _preset_fields(preset)
                if k in user_settings.shell_exported
            ],
        })
    return out


def _dotenv_file_values() -> dict[str, str]:
    """The ``.env`` file as written. Not ``os.environ``: that also holds
    values copied from the settings file at start, which a save may have
    removed since (#136)."""
    import config as config_module  # noqa: WPS433
    from dotenv import dotenv_values  # noqa: WPS433

    try:
        values = dotenv_values(config_module.dotenv_path())
    except Exception:
        return {}
    return {k: (v or "").strip() for k, v in values.items()}


def _mlx_next_model(
    role: str, saved_file: dict[str, str], dotenv: dict[str, str]
) -> tuple[str, str]:
    """``(value, source)`` for ``role``'s MLX model on the next start, in the
    order a start applies them: shell export, settings file, ``.env``.
    ``value`` is ``""`` (source ``"default"``) when none is set."""
    key = _MLX_MODEL_KEYS[role]
    if key in user_settings.shell_exported:
        return os.environ.get(key, "").strip(), "shell"
    if saved_file.get(key, "").strip():
        return saved_file[key].strip(), "settings"
    if dotenv.get(key):
        return dotenv[key], "env_file"
    return "", "default"


# Metal lets the GPU wire about three quarters of unified memory, and the
# OS and apps need the rest, so weights above that will not all stay loaded.
_MLX_MEMORY_SHARE = 0.75


def mlx_memory_note(
    models: list[str], memory_gb: Optional[float], *,
    cache_dir: Optional[Path] = None,
) -> str:
    """One line when ``models`` together likely exceed this Mac's memory,
    else ``""``. Sizes come from the local Hugging Face cache or the model
    folder only (no network); any unknown size, or unknown memory, gives
    ``""``. Informational: it never blocks a save (#136 decision 2)."""
    from ai.mlx_engine import local_model_bytes  # noqa: WPS433

    if not models or not memory_gb:
        return ""
    total = 0
    for model in models:
        size = local_model_bytes(model, cache_dir)
        if size is None:
            return ""
        total += size
    need_gb = total / 1024**3
    if need_gb <= memory_gb * _MLX_MEMORY_SHARE:
        return ""
    return (
        f"These local models take about {need_gb:.1f} GB together and this Mac "
        f"has {memory_gb:.0f} GB of memory, so they may not all fit: expect slow "
        "or failed loads, or pick smaller models."
    )


_physical_memory_cache: list[Optional[float]] = []


def _physical_memory_gb() -> Optional[float]:
    if not _physical_memory_cache:
        from ai.first_run import physical_memory_gb  # noqa: WPS433
        _physical_memory_cache.append(physical_memory_gb())
    return _physical_memory_cache[0]


def _mlx_models_view(
    saved_file: dict[str, str], saved_engines: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Per role: the local model as Settings shows it (#136)."""
    dotenv = _dotenv_file_values()
    defaults = default_mlx_models()
    out: dict[str, dict[str, Any]] = {}
    for role in _ENGINE_ROLES:
        key = _MLX_MODEL_KEYS[role]
        value, source = _mlx_next_model(role, saved_file, dotenv)
        engine = getattr(app.state, _MLX_ENGINE_ATTRS[role], None)
        loaded = getattr(engine, "model_name", "") if isinstance(engine, MLXEngine) else ""
        error = getattr(engine, "load_error", "") if isinstance(engine, MLXEngine) else ""
        out[role] = {
            "key": key,
            # What the next start uses; "" means the default below.
            "value": value,
            "default": defaults[key],
            # "shell" | "settings" | "env_file" | "default"
            "source": source,
            "locked": source == "shell",
            # The role runs on MLX after the next start / in this process.
            "local": saved_engines[role] == "local",
            "running_local": settings.engine_provider(role) == "local",
            # Configured in this process, and actually loaded (differs
            # after a fallback; "" while nothing is loaded).
            "running": str(getattr(settings, _MLX_RUNNING_ATTRS[role]) or ""),
            "loaded": loaded if getattr(engine, "loaded", False) else "",
            "load_error": error if isinstance(error, str) else "",
        }
    return out


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
    mlx_models = _mlx_models_view(saved_file, saved_engines)
    mlx_changed = any(
        m["local"] and (m["value"] or m["default"]) != m["running"]
        for m in mlx_models.values()
    )
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
        "presets": _presets_view(saved_engines),
        # Read live by ai.privacy, so it is not part of restart_required.
        "cloud_personal_text": personal_text_allowed(),
        # #136: per-role local MLX model and the memory note ("" = none).
        "mlx_models": mlx_models,
        "mlx_memory_note": mlx_memory_note(
            [m["value"] or m["default"] for m in mlx_models.values() if m["local"]],
            _physical_memory_gb(),
        ),
        "restart_required": saved != running or mlx_changed,
        "can_restart": _can_self_restart(),
        # Fields the UI can't change because they're exported in the shell.
        "locked_fields": [
            field_name for field_name, env_key in _SETTING_FIELDS
            if env_key in user_settings.shell_exported
        ] + (
            ["cloud_personal_text"]
            if _PERSONAL_TEXT_KEY in user_settings.shell_exported else []
        ) + [
            f"mlx_{role}_model" for role, m in mlx_models.items() if m["locked"]
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
    """Save settings from the UI. Provider/model changes take effect after
    a restart; ``cloud_personal_text`` applies to the next prompt (#52)."""
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
    for role in _ENGINE_ROLES:
        value = getattr(body, f"mlx_{role}_model")
        key = _MLX_MODEL_KEYS[role]
        if value is None or key in user_settings.shell_exported:
            continue  # unchanged, or locked by a shell export (#136)
        value = value.strip()
        # Don't pin the default (as for Groq), unless it overrides .env.
        if value == default_mlx_models()[key] and not _dotenv_file_values().get(key):
            value = ""
        updates[key] = value  # "" removes the key from settings.json
    if body.cloud_personal_text is not None:
        # Stored as "1"/"0" (never removed) so an explicit "off" beats a
        # ``=1`` left in .env. A shell export still wins (locked in the UI).
        updates[_PERSONAL_TEXT_KEY] = "1" if body.cloud_personal_text else "0"

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

    for preset_id, fields in (body.preset_settings or {}).items():
        preset = providers.PRESETS.get(preset_id)
        if preset is None:
            raise HTTPException(status_code=400, detail=f"unknown provider {preset_id!r}")
        for field_name, env_key in _preset_fields(preset):
            value = getattr(fields, field_name)
            if value is None:
                continue
            value = value.strip()
            if field_name == "api_base" and value:
                if not value.startswith(("http://", "https://")):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{preset.label} base URL must start with http:// or https://",
                    )
                if value.rstrip("/") == preset.api_base.rstrip("/"):
                    value = ""
            if field_name.endswith("_model") and value == preset.default_models.get(
                field_name[: -len("_model")]
            ):
                value = ""  # don't pin a default, as for Groq below
            updates[env_key] = value

    # Refuse to save an engine that could not start: a preset in use with
    # no key, base URL or model for its role (#97; Groq since #26).
    next_values = {f: _next(f, k) for f, k in _SETTING_FIELDS}
    next_engines = _effective_providers(next_values)
    for preset_id in sorted(set(next_engines.values()) - {"local"}):
        preset = providers.PRESETS[preset_id]
        values = _preset_values(preset, updates)
        if preset_id == "groq":
            values["api_key"] = next_values["groq_api_key"]
            for role in _ENGINE_ROLES:
                values[f"{role}_model"] = (
                    next_values[f"groq_{role}_model"]
                    or GROQ_DEFAULT_MODELS[f"GROQ_{role.upper()}_MODEL"]
                )
        roles = {r for r, p in next_engines.items() if p == preset_id}
        problem = _preset_problem(preset, values, roles)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
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


def _record_outcome(state: Any, method: str, tool_name: str) -> None:
    """#79: a generated tool was disabled or deleted (best effort)."""
    store = getattr(state, "idea_outcomes", None)
    if store is None:
        return
    try:
        getattr(store, method)(tool_name)
    except Exception:
        logger.exception("idea_outcomes.%s failed", method)


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
    _record_outcome(app.state, "record_deleted", tool_name)
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
    _record_outcome(app.state, "record_disabled", tool_name)
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
        # #87: a real Pause. Listeners keep running (Resume needs no
        # restart); the delivery gate holds agent work and drops the rest.
        duration = getattr(msg.payload, "duration", None) or "indefinite"
        try:
            app.state.delivery.pause(duration)
        except Exception:
            logger.exception("Failed to pause")
    elif action == "resume_listeners":
        try:
            app.state.delivery.resume()
        except Exception:
            logger.exception("Failed to resume")
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
        # #75: the activity history is memory too.
        try:
            rows = await asyncio.to_thread(activity_store.clear)
            logger.info("clear_memory removed %s activity rows", rows)
        except Exception:
            logger.exception("Failed to clear activity history")
        # #82: the privacy ledger is a record of what left; Clear empties it.
        try:
            rows = await asyncio.to_thread(egress_log.clear)
            logger.info("clear_memory removed %s privacy-ledger rows", rows)
        except Exception:
            logger.exception("Failed to clear the privacy ledger")
    elif action == "reload_dynamic_tools":
        try:
            app.state.orchestrator.load_dynamic_tools()
        except Exception:
            logger.exception("Failed to reload dynamic tools")
    elif action == "chat_cancel":
        # #143: Stop on a chat turn. An unknown or finished id is a no-op;
        # open cards are withdrawn, never approved.
        try:
            app.state.orchestrator.cancel_chat_turn(getattr(msg.payload, "message_id", None))
        except Exception:
            logger.exception("Failed to stop chat turn")
    elif action in ("demonstration_start", "demonstration_stop", "demonstration_cancel"):
        # #84 "Watch me": the user's own menu item / hotkey.
        demo = getattr(app.state, "demonstration", None)
        if demo is None:
            return
        try:
            if action == "demonstration_start":
                await demo.start()
            elif action == "demonstration_stop":
                await demo.stop("user")
            else:
                await demo.cancel()
        except Exception:
            logger.exception("Failed to run %s", action)


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
            **_delivery_status(),
        ),
    )


def _delivery_status() -> dict[str, Any]:
    """#87: Pause, held and Inbox counts for the status frame ({} if unset)."""
    delivery = getattr(app.state, "delivery", None)
    if delivery is None:
        return {}
    try:
        st = delivery.status()
    except Exception:
        logger.exception("delivery.status() failed")
        return {}
    return {
        "paused": bool(st.get("paused")),
        "paused_until": st.get("paused_until"),
        "held_count": int(st.get("held_count") or 0),
        "inbox_count": int(st.get("inbox_count") or 0),
    }


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


# The Permissions checklist's ``calendars`` row reads what the menu bar app
# reported over the WS, and its Request click asks the app to prompt (#60).
calendar_access.register_permission_hooks()
# #127: the ``contacts`` row, the same way.
contacts_tool.register_permission_hooks()


def _calendar_sender(websocket: WebSocket):
    """``calendar_request`` frames go to this one connection only (#60)."""

    async def send(request_id: str, op: str, args: dict, timeout_seconds: int) -> None:
        frame = CalendarRequest(
            seq=_next_seq(),
            timestamp=_now_iso(),
            payload=CalendarRequestPayload(
                request_id=request_id, op=op, args=args,
                timeout_seconds=timeout_seconds,
            ),
        )
        await websocket.send_text(frame.model_dump_json())

    return send


def _contacts_sender(websocket: WebSocket):
    """``contacts_request`` frames go to this one connection only (#127)."""

    async def send(request_id: str, op: str, name: str, limit: int,
                   timeout_seconds: int) -> None:
        frame = ContactsRequest(
            seq=_next_seq(),
            timestamp=_now_iso(),
            payload=ContactsRequestPayload(
                request_id=request_id, op=op, name=name, limit=limit,
                timeout_seconds=timeout_seconds,
            ),
        )
        await websocket.send_text(frame.model_dump_json())

    return send


def _screen_text_sender(websocket: WebSocket):
    """``screen_text_request`` frames go to this one connection only (#89)."""

    async def send(request_id: str, max_chars: int, never_watch: list[str],
                   timeout_seconds: int) -> None:
        frame = ScreenTextRequest(
            seq=_next_seq(),
            timestamp=_now_iso(),
            payload=ScreenTextRequestPayload(
                request_id=request_id, max_chars=max_chars,
                never_watch=never_watch, timeout_seconds=timeout_seconds,
            ),
        )
        await websocket.send_text(frame.model_dump_json())

    return send


_MEETING_FRAMES = {
    "meeting_record_offer": (MeetingRecordOffer, MeetingRecordOfferPayload),
    "meeting_record_stop": (MeetingRecordStop, MeetingRecordStopPayload),
}


def _meeting_sender(websocket: WebSocket):
    """#86: the meeting-notes offer / stop frames, to this connection."""

    async def send(frame_type: str, payload: dict[str, Any]) -> None:
        cls, payload_cls = _MEETING_FRAMES[frame_type]
        frame = cls(seq=_next_seq(), timestamp=_now_iso(), payload=payload_cls(**payload))
        await websocket.send_text(frame.model_dump_json())

    return send


# Strong refs to running meeting-audio jobs (a bare create_task can be GC'd).
_MEETING_JOBS: set[asyncio.Task[Any]] = set()


async def _process_meeting_audio(meeting_notes: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """#86: transcript / audio -> one card. Anything but a card is told to
    the user as a notification, except a failure whose audio was kept
    (#108): the app's panel already shows it with Retry / Delete."""
    try:
        result = await meeting_notes.process(payload)
    except Exception as exc:
        logger.exception("meeting notes: processing failed")
        result = {"status": "error", "reason": str(exc)[:300]}
    if result.get("status") != "offered" and not result.get("kept"):
        try:
            from tools.show_notification import show_notification  # noqa: WPS433

            await asyncio.to_thread(
                show_notification, "Meeting notes", str(result.get("reason") or "No notes."),
            )
        except Exception:
            logger.exception("meeting notes notification failed")
    return result


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
    # Each connection may serve Calendar once it sends ``calendar_status``.
    conn_id = f"ws_{id(websocket):x}_{_next_seq()}"
    calendar_access.app_bridge.connect(
        conn_id, _calendar_sender(websocket), asyncio.get_running_loop(),
    )
    # #89: this connection may serve read_front_window once it reports
    # ``screen_text_status``.
    screen_text.bridge.connect(
        conn_id, _screen_text_sender(websocket), asyncio.get_running_loop(),
    )
    # #127: this connection may serve find_contact.
    contacts_tool.on_connect(
        conn_id, _contacts_sender(websocket), asyncio.get_running_loop(),
    )
    # #86: the meeting recorder lives in the app.
    meeting_notes = getattr(app.state, "meeting_notes", None)
    if meeting_notes is not None:
        meeting_notes.link.connect(
            conn_id, _meeting_sender(websocket), asyncio.get_running_loop(),
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
            elif isinstance(msg, CalendarResponse):
                calendar_access.app_bridge.submit_response(
                    conn_id, msg.payload.model_dump(),
                )
            elif isinstance(msg, CalendarStatus):
                calendar_access.app_bridge.set_status(
                    conn_id, msg.payload.authorization,
                )
            elif isinstance(msg, ScreenTextResponse):
                screen_text.bridge.submit_response(conn_id, msg.payload.model_dump())
            elif isinstance(msg, ScreenTextStatus):
                screen_text.bridge.set_status(conn_id, msg.payload.accessibility_trusted)
            elif isinstance(msg, ContactsResponse):
                contacts_tool.on_response(conn_id, msg.payload.model_dump())
            elif isinstance(msg, ContactsStatus):
                contacts_tool.on_status(conn_id, msg.payload.authorization)
            elif isinstance(msg, DemonstrationDraft):
                demo = getattr(app.state, "demonstration", None)
                if demo is not None:
                    await demo.draft(msg.payload.sentence, msg.payload.removed_step_ids)
            elif isinstance(msg, MeetingRecordState):
                if meeting_notes is not None:
                    meeting_notes.on_record_state(conn_id, msg.payload.model_dump())
            elif isinstance(msg, MeetingAudioReady):
                if meeting_notes is not None:
                    task = asyncio.create_task(
                        _process_meeting_audio(meeting_notes, msg.payload.model_dump()),
                        name="meeting-notes",
                    )
                    _MEETING_JOBS.add(task)
                    task.add_done_callback(_MEETING_JOBS.discard)
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
                            # #74: quick-ask context, this turn only.
                            "context": msg.payload.context.model_dump(exclude_none=True) if msg.payload.context else None,
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
        calendar_access.app_bridge.disconnect(conn_id)
        screen_text.bridge.disconnect(conn_id)
        contacts_tool.on_disconnect(conn_id)
        if meeting_notes is not None:
            meeting_notes.link.disconnect(conn_id)
