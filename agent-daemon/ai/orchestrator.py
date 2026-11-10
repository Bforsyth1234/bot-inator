"""smolagents orchestrator: event → agent → tool → approval loop.

Phase 1 design:
* A background task consumes ``ContextEvent``s from the shared ``EventBus``.
* For each event we run a smolagents ``ToolCallingAgent`` whose LLM is our
  :class:`~ai.mlx_engine.MLXEngine` and whose tools are approval-gated
  wrappers around the real tools.
* The orchestrator streams ``thought`` frames over the WebSocket via an
  injected ``ws_send`` coroutine (registered by ``main.py``) and blocks on
  approval by waiting on an ``asyncio.Future`` keyed by ``request_id``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import importlib.util
import logging
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from events.event_bus import ContextEvent, EventBus
from schemas.ws_messages import (
    ApprovalRequest,
    ApprovalRequestPayload,
    ApprovalResponsePayload,
    CodeApprovalRequest,
    CodeApprovalRequestPayload,
    CodeApprovalResponsePayload,
    NOTICE_EVENT_TYPE,
    Notice,
    NoticePayload,
    Thought,
    ThoughtPayload,
    ToolsChanged,
    ToolsChangedPayload,
)
from tools import READ_ONLY_TOOL_NAMES

from . import privacy

from .mlx_engine import MLXEngine
from .memory import Memory

# ``engine`` is duck-typed against the surface defined by
# :class:`MLXEngine` (load/unload/swap/generate/generate_sync/
# generate_chat_sync/evaluate_event/loaded/current_model/model_name).
# :class:`~ai.groq_engine.GroqEngine` is the second concrete implementation;
# tests pass mocks. ``Any`` keeps the orchestrator from importing the Groq
# module at type-check time so its optional dependency tree stays optional.
EngineLike = Any

logger = logging.getLogger(__name__)

WSSendCallable = Callable[[Any], Awaitable[None]]

# Event types that feed observers (the WebSocket activity stream and the
# pattern recognizer) but do not warrant invoking the tool-calling agent.
# ``app_activated`` is the canonical example: the helper subprocess emits
# one every time NSWorkspace switches focus, which is far too noisy to
# justify a 70B chat-model call (and the model's typical response is a
# tautological ``show_notification`` that asks the user to approve a
# popup informing them of the focus change they just caused). Pattern
# detection still runs because the recognizer's whole job is finding
# repeatable workflows in activation sequences.
# Wave 3 sources are signal-only too: running the tool-calling agent on
# every upcoming meeting or copy would flood the user with approval
# cards. Idea sources still see them. ``mail_received`` does reach the
# agent (it reads the email and may offer a reminder or calendar event),
# but see ``_mail_skips_agent`` for the mail it leaves alone.
_AGENT_TRIGGER_EXCLUDED_EVENT_TYPES: frozenset[str] = frozenset({
    "app_activated",
    "clipboard_changed",
    "event_starting_soon",
})

# Built-in follow-up kinds the NextStepSuggester already turns into an
# approval card for an email; the agent skips those emails so the user
# does not get two cards for one message.
_MAIL_KINDS_HANDLED_BY_IDEAS: frozenset[str] = frozenset({
    "mail_invite",
    "mail_shipping",
    "mail_deadline",
})


def _mail_skips_agent(event: ContextEvent) -> bool:
    """True for mail the tool-calling agent should not act on.

    Junk, sent and draft mailboxes never reach it, and neither does mail
    a built-in follow-up already covers (see
    ``_MAIL_KINDS_HANDLED_BY_IDEAS``).
    """
    # Imported lazily: ``ai.idea_sources`` pulls in the pattern recognizer.
    from ai.idea_sources import _SKIP_MAILBOXES, NextStepSuggester

    if _SKIP_MAILBOXES.search(str((event.metadata or {}).get("mailbox") or "")):
        return True
    return NextStepSuggester.classify(event) in _MAIL_KINDS_HANDLED_BY_IDEAS


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# How ``_build_pattern_prompt`` explains each idea source to the agent.
_PATTERN_SOURCE_INTROS: dict[str, str] = {
    "friction": (
        "The user keeps copying data in one app and pasting it into "
        "another, and a reviewed tool idea would move that data directly."
    ),
    "setup_scan": (
        "A one-time scan of the user's Mac found repeated manual work "
        "(for example a long shell command typed many times), and a "
        "reviewed tool idea passed its quality bar."
    ),
    "starter_catalog": (
        "A curated, known-good tool idea matched the apps and files on "
        "the user's Mac and passed review."
    ),
    "repetition": (
        "The pattern recognizer has observed the user performing a "
        "repeatable workflow and suggested a new tool that would automate it."
    ),
    "next_step": (
        "An event just happened that has an obvious follow-up, and a "
        "new tool was suggested so the user can do it in one click next time."
    ),
    "chat_gap": (
        "In chat, the user asked for something no existing tool could do, "
        "and a new tool was suggested so it works next time."
    ),
    "open_help": (
        "While watching recent activity, the assistant asked how it could "
        "help the user, and a reviewed tool idea passed its quality bar."
    ),
}


class Orchestrator:
    """Drives the agent loop on top of an :class:`MLXEngine`."""

    def __init__(
        self,
        engine: EngineLike,
        event_bus: EventBus,
        memory: Optional[Memory] = None,
        tools: Optional[list[Any]] = None,
        approval_timeout: float = 120.0,
        analysis_engine: Optional[EngineLike] = None,
        generated_tools_dir: Optional[Path] = None,
        code_approval_timeout: float = 300.0,
        pattern_recognizer: Any = None,
        idea_sources: Optional[list[Any]] = None,
    ) -> None:
        self.engine = engine
        self.analysis_engine = analysis_engine or engine
        self.event_bus = event_bus
        self.memory = memory
        self.tools: list[Any] = tools or []
        self._builtin_tool_names: frozenset[str] = frozenset(
            getattr(t, "name", getattr(t, "__name__", "")) for t in self.tools
        )
        self.approval_timeout = approval_timeout
        self.code_approval_timeout = code_approval_timeout
        self.generated_tools_dir = (
            Path(generated_tools_dir) if generated_tools_dir else None
        )

        self._ws_send: Optional[WSSendCallable] = None
        self._subscribers: set[WSSendCallable] = set()
        self._seq: int = 0
        self._task: Optional[asyncio.Task[None]] = None
        self._pending: dict[str, asyncio.Future[ApprovalResponsePayload]] = {}
        self._pending_code: dict[
            str, asyncio.Future[CodeApprovalResponsePayload]
        ] = {}
        # Names of tools injected at runtime via ``load_dynamic_tools``. Kept
        # so :meth:`unload_dynamic_tool` can distinguish agent-authored tools
        # from the built-ins baked in at construction time.
        self._dynamic_tool_names: set[str] = set()
        # Lock to serialize dynamic tool loading/unloading operations. This
        # prevents concurrent modifications to self.tools and self._agent
        # from startup, reload commands, and meta-tool installs.
        import threading
        self._tool_load_lock = threading.RLock()
        # Set by :func:`tools.meta_tool_generator.generate_custom_tool`
        # when a new tool was installed and loaded so
        # :meth:`_handle_user_message` can transparently re-run the same
        # prompt against the rebuilt agent, which now has the tool in
        # its schema. Cleared immediately after consumption to keep the
        # auto-rerun single-shot per user turn.
        self._pending_rerun_tool: Optional[str] = None
        self.pattern_recognizer = pattern_recognizer
        # Phase 5.2 idea sources (``ai.idea_sources``). Each may expose
        # ``observe(event)`` and/or ``observe_chat(user_text, reply,
        # tools_called)``.
        self.idea_sources: list[Any] = list(idea_sources or [])
        # Tool names invoked during the current agent turn (reset at the
        # top of :meth:`_handle_event`), so a chat-gap source can skip turns
        # that already tried ``generate_custom_tool`` and a second
        # ``generate_custom_tool`` call in one turn is refused (#38).
        self._turn_tool_calls: set[str] = set()
        # What started the current agent turn: "chat", "pattern" or
        # "event". generate_custom_tool is only allowed for chat and
        # pattern turns (a background event must not open a build card).
        self._turn_kind: str = "event"
        # Side-effecting calls already run this turn, keyed by tool + args,
        # so a model that repeats a call can't run it twice.
        self._turn_done: dict[str, Any] = {}
        # ``ai.idea_match.DeclinedIdeaMatcher`` set by main.py: catches
        # reworded repeats of a declined tool idea.
        self.declined_matcher: Optional[Any] = None
        # Metadata of the built-in follow-up idea being acted on this turn
        # (#32), so a declined built-in call is remembered as a rejection.
        self._current_builtin_idea: Optional[dict[str, Any]] = None
        # Generated tools moved to ``quarantine/`` by the loader (#28):
        # name -> first line of the error. Cleared when the name loads.
        self._quarantined: dict[str, str] = {}
        # Active notices by id (#19): each id is published once per run and
        # replayed to clients that connect later (:meth:`notice_frames`).
        self._notices: dict[str, NoticePayload] = {}
        self._agent: Any = None

    def mark_tool_ready_for_rerun(self, tool_name: str) -> None:
        """Flag the current user turn for an auto-rerun with ``tool_name``.

        Called from the meta-tool generator after a successful install and
        load. A name that is already a generated tool never gets here: the
        meta-tool rejects it up front with a ``name_collision`` error (#24).
        The flag is consumed by :meth:`_handle_user_message`.
        """
        self._pending_rerun_tool = tool_name

    # ---- notices (#19) -----------------------------------------------

    def publish_notice(self, notice: NoticePayload | dict[str, Any]) -> bool:
        """Broadcast ``notice`` unless its id was already published this run.

        Call on the event loop (listener threads go through the event bus
        with a ``system_notice`` event). Returns ``True`` when broadcast.
        """
        if not isinstance(notice, NoticePayload):
            notice = NoticePayload.model_validate(notice)
        if notice.id in self._notices:
            return False
        self._notices[notice.id] = notice
        logger.info("Notice %s: %s", notice.id, notice.title)
        self._schedule(self._send(self._notice_frame(notice)))
        return True

    def notice_frames(self) -> list[Notice]:
        """A fresh ``notice`` frame per active notice, for a new client."""
        return [self._notice_frame(n) for n in self._notices.values()]

    def _notice_frame(self, notice: NoticePayload) -> Notice:
        return Notice(seq=self._next_seq(), timestamp=_now_iso(), payload=notice)

    # ---- tools_changed (#29) -----------------------------------------

    def notify_tools_changed(self, reason: str, tool_names: list[str]) -> None:
        """Broadcast one ``tools_changed`` frame, from any thread."""
        frame = ToolsChanged(
            seq=self._next_seq(),
            timestamp=_now_iso(),
            payload=ToolsChangedPayload(
                reason=reason, tool_names=sorted(set(tool_names)),
            ),
        )
        self._schedule(self._send(frame))

    def _schedule(self, coro: Any) -> None:
        """Run ``coro`` on the bus's loop, from the loop or any thread.

        On the loop thread it runs as a task, i.e. after the current
        synchronous code (a route moving files) finishes.
        """
        loop = getattr(self.event_bus, "loop", None)
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(coro)
        elif loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            coro.close()

    # ---- lifecycle ----------------------------------------------------

    def register_ws_send(self, send: Optional[WSSendCallable]) -> None:
        """Replace all subscribers with a single send callable.

        Retained for backward compatibility with existing callers/tests.
        Prefer :meth:`add_subscriber` / :meth:`remove_subscriber` when
        multiple clients may be connected concurrently.
        """
        self._ws_send = send
        self._subscribers = {send} if send is not None else set()

    def add_subscriber(self, send: WSSendCallable) -> None:
        """Register a WebSocket send closure to receive broadcast messages."""
        self._subscribers.add(send)
        self._ws_send = send

    def remove_subscriber(self, send: WSSendCallable) -> None:
        """Remove a previously registered subscriber."""
        self._subscribers.discard(send)
        if self._ws_send is send:
            self._ws_send = next(iter(self._subscribers), None)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._agent = self._build_agent()
        self._task = asyncio.create_task(self._run(), name="orchestrator-loop")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    def submit_approval_response(self, payload: ApprovalResponsePayload) -> bool:
        """Resolve a pending approval. Returns ``True`` if a waiter was found."""
        fut = self._pending.pop(payload.request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True

    def submit_code_approval_response(
        self, payload: CodeApprovalResponsePayload
    ) -> bool:
        """Resolve a pending code-approval. Mirrors :meth:`submit_approval_response`."""
        fut = self._pending_code.pop(payload.request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True

    async def request_code_approval(
        self,
        *,
        tool_name: str,
        description: str,
        code: str,
        event_id: Optional[str] = None,
    ) -> CodeApprovalResponsePayload:
        """Prompt the user to review AI-generated Python source.

        Public entry point used by the meta-tool generator. Blocks on a
        :class:`asyncio.Future` keyed by ``request_id`` until the client
        replies with a :class:`CodeApprovalResponsePayload` or the
        :attr:`code_approval_timeout` elapses (in which case a synthetic
        denial is returned).
        """
        request_id = f"code_{uuid.uuid4().hex[:12]}"
        ev_id = event_id or getattr(self, "_current_event_id", "evt_meta")
        # Use asyncio.get_running_loop() for Python 3.10+ compatibility;
        # get_event_loop() is deprecated when called from coroutines.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[CodeApprovalResponsePayload] = loop.create_future()
        self._pending_code[request_id] = fut
        msg = CodeApprovalRequest(
            seq=self._next_seq(),
            timestamp=_now_iso(),
            payload=CodeApprovalRequestPayload(
                request_id=request_id,
                event_id=ev_id,
                tool_name=tool_name,
                description=description,
                code=code,
                timeout_seconds=int(self.code_approval_timeout),
            ),
        )
        await self._send(msg)
        try:
            return await asyncio.wait_for(
                fut, timeout=self.code_approval_timeout
            )
        except asyncio.TimeoutError:
            # Atomically pop to avoid race with submit_code_approval_response
            self._pending_code.pop(request_id, None)
            logger.info("Code approval %s timed out", request_id)
            return CodeApprovalResponsePayload(
                request_id=request_id, approved=False
            )

    # ---- dynamic tool loading ----------------------------------------

    def load_dynamic_tools(
        self, directory: Optional[Path] = None
    ) -> list[str]:
        """Import every ``*.py`` module under ``directory`` as a smolagents tool.

        Called on startup and again by the meta-tool generator after a new
        file is persisted. Each successfully-imported module must expose a
        single ``smolagents.Tool`` instance at module scope (either via the
        ``@tool`` decorator or a ``Tool`` subclass). Imports are idempotent:
        re-loading refreshes the cached module and replaces the previous
        tool instance in :attr:`tools`.

        A module that fails to load (``exec_module`` raises, no Tool found,
        or the agent rebuild fails because of it) is quarantined: moved to
        ``<directory>/quarantine/`` next to a ``<name>.error.txt`` holding
        the error, and reported as a ``tool_result`` thought. The other
        tools stay loaded (#28).

        Returns the sorted list of dynamic tool names currently loaded.

        Files starting with ``.`` (e.g. ``.gitkeep``) and subdirectories
        (``.git``, ``disabled/``, ``quarantine/``) are always skipped.

        Thread-safe: uses ``_tool_load_lock`` to serialize concurrent calls.
        """
        with self._tool_load_lock:
            target = Path(directory) if directory else self.generated_tools_dir
            if target is None or not target.exists():
                return sorted(self._dynamic_tool_names)

            before = set(self._dynamic_tool_names)
            if self._refuse_unsandboxed(target):
                return sorted(self._dynamic_tool_names)
            loaded: list[str] = []
            quarantined: list[tuple[str, str]] = []
            for path in sorted(target.iterdir()):
                if path.name.startswith(".") or path.is_dir():
                    continue
                if path.suffix != ".py":
                    continue
                name = path.stem
                if name in self._builtin_tool_names:
                    logger.warning(
                        "Generated tool %s shadows a built-in; skipping", name
                    )
                    continue
                try:
                    exclude = frozenset(
                        getattr(t, "name", getattr(t, "__name__", ""))
                        for t in self.tools
                    ) - {name}
                    tool_obj = self._load_tool(name, path, exclude)
                except Exception as exc:
                    logger.exception("Failed to load dynamic tool from %s", path)
                    # A sandboxed import failure carries the child's error
                    # and traceback (#22), so quarantine reports the same
                    # ``ModuleNotFoundError: ...`` line as an in-process one.
                    child_error = getattr(exc, "child_error", None)
                    quarantined.append((name, self._quarantine_tool(
                        target, name, child_error or exc,
                        getattr(exc, "child_traceback", "")
                        or traceback.format_exc(),
                    )))
                    continue
                if tool_obj is None:
                    quarantined.append((name, self._quarantine_tool(
                        target, name,
                        f"No smolagents Tool instance found in {path.name}", "",
                    )))
                    continue
                self._replace_tool(name, tool_obj)
                self._dynamic_tool_names.add(name)
                self._quarantined.pop(name, None)
                loaded.append(name)

            if loaded:
                quarantined.extend(self._rebuild_agent_isolating(target, loaded))
                logger.info(
                    "Loaded dynamic tools: %s",
                    ", ".join(n for n in loaded if n in self._dynamic_tool_names),
                )
            elif quarantined:
                self._agent = self._build_agent()
            for name, first_line in quarantined:
                self._report_quarantine(name, first_line)
            # One frame per call, not per tool (#29). A reload that loads
            # the same names (startup, ``reload_dynamic_tools``) is a change
            # too: the tool objects were replaced.
            if quarantined:
                self.notify_tools_changed(
                    "quarantined",
                    [n for n, _ in quarantined]
                    + sorted(self._dynamic_tool_names ^ before),
                )
            elif loaded:
                self.notify_tools_changed("loaded", loaded)
            return sorted(self._dynamic_tool_names)

    def _rebuild_agent_isolating(
        self, target: Path, loaded: list[str]
    ) -> list[tuple[str, str]]:
        """Rebuild the agent; if that fails, quarantine the tools that break it.

        The fast path is one build. On failure the just-loaded tools are
        detached and re-added one at a time, so a tool whose schema breaks
        ``ToolCallingAgent`` is quarantined and the rest stay loaded.
        Returns ``(name, first_error_line)`` for each quarantined tool.
        """
        try:
            self._agent = self._build_agent()
            return []
        except Exception:
            logger.exception(
                "Agent rebuild failed; re-adding new tools one at a time"
            )
        pending = [(n, self._tool_named(n)) for n in loaded]
        for name, _ in pending:
            self._remove_tool(name)
        try:
            self._agent = self._build_agent()
        except Exception:
            # Not attributable to a new tool: restore them and re-raise.
            for name, tool_obj in pending:
                self._replace_tool(name, tool_obj)
                self._dynamic_tool_names.add(name)
            raise
        quarantined: list[tuple[str, str]] = []
        for name, tool_obj in pending:
            self._replace_tool(name, tool_obj)
            self._dynamic_tool_names.add(name)
            try:
                self._agent = self._build_agent()
            except Exception as exc:
                tb = traceback.format_exc()
                logger.warning("Agent rebuild fails with %s; quarantining", name)
                self._remove_tool(name)
                quarantined.append(
                    (name, self._quarantine_tool(target, name, exc, tb))
                )
        if quarantined:
            self._agent = self._build_agent()
        return quarantined

    def _tool_named(self, name: str) -> Any:
        for t in self.tools:
            if getattr(t, "name", getattr(t, "__name__", "")) == name:
                return t
        return None

    def _remove_tool(self, name: str) -> None:
        """Detach ``name`` from :attr:`tools` without rebuilding the agent."""
        self.tools = [
            t
            for t in self.tools
            if getattr(t, "name", getattr(t, "__name__", "")) != name
        ]
        self._dynamic_tool_names.discard(name)
        sys.modules.pop(f"tools.generated.{name}", None)

    def _quarantine_tool(
        self, target: Path, name: str, error: Any, tb: str
    ) -> str:
        """Move ``<target>/<name>.py`` to ``<target>/quarantine/`` (#28).

        Writes ``quarantine/<name>.error.txt``: the error on line one, then
        the traceback. Unloads the tool if it was loaded. Returns the
        error's first line. Never raises: a failed move is logged.
        """
        if isinstance(error, BaseException):
            first_line = f"{type(error).__name__}: {error}".splitlines()[0]
        else:
            first_line = (str(error).splitlines() or [""])[0]
        first_line = first_line[:240]
        self._remove_tool(name)
        self._quarantined[name] = first_line
        qdir = target / "quarantine"
        try:
            qdir.mkdir(exist_ok=True)
            os.replace(target / f"{name}.py", qdir / f"{name}.py")
            (qdir / f"{name}.error.txt").write_text(
                first_line + "\n\n" + (tb or ""), encoding="utf-8"
            )
            logger.warning("Quarantined generated tool %s: %s", name, first_line)
        except OSError:
            logger.exception("Could not quarantine generated tool %s", name)
        return first_line

    def _report_quarantine(self, name: str, first_line: str) -> None:
        """Emit a ``tool_result`` thought for a quarantined tool, from any thread."""
        self._schedule(self._emit_thought(
            getattr(self, "_current_event_id", "evt_tools"),
            "tool_result",
            f"Generated tool {name} failed to load and was moved to "
            f"quarantine/: {first_line}",
        ))

    def unload_dynamic_tool(self, name: str) -> bool:
        """Remove a previously-loaded dynamic tool from the active agent.

        Returns ``True`` when a matching tool was found and detached. Does
        not touch the filesystem — the caller is responsible for unlinking
        the source file.

        Thread-safe: uses ``_tool_load_lock`` to serialize concurrent calls.
        """
        with self._tool_load_lock:
            if name not in self._dynamic_tool_names:
                return False
            self._remove_tool(name)
            self._agent = self._build_agent()
            logger.info("Unloaded dynamic tool: %s", name)
            self.notify_tools_changed("unloaded", [name])
            return True

    def _load_tool(
        self, name: str, path: Path, exclude: frozenset[str] = frozenset()
    ) -> Any:
        """Load ``path`` as a sandboxed proxy, or in-process when opted out (#22)."""
        from tools import tool_sandbox

        if not tool_sandbox.sandbox_enabled():
            return self._import_tool_module(name, path, exclude)
        return tool_sandbox.load_sandboxed_tool(name, path, exclude)

    def _refuse_unsandboxed(self, target: Path) -> bool:
        """With the sandbox on but unusable, load no generated tool (#22).

        Every ``*.py`` tool in ``target`` is unloaded (if loaded), recorded
        like a quarantined tool (so an install reports an error) and
        reported as a ``tool_result`` thought. Files stay where they are:
        they load again once the sandbox works or ``AGENT_TOOL_SANDBOX=0``.
        Returns True when loading was refused.
        """
        from tools import tool_sandbox

        if not tool_sandbox.sandbox_enabled() or tool_sandbox.sandbox_available():
            return False
        reason = (
            "not loaded: the tool sandbox is unavailable "
            f"({tool_sandbox.sandbox_problem() or 'self-test failed'}); "
            f"set {tool_sandbox.SANDBOX_ENV}=0 to run generated tools unsandboxed"
        )
        refused: list[str] = []
        for path in sorted(target.iterdir()):
            if path.name.startswith(".") or path.is_dir() or path.suffix != ".py":
                continue
            name = path.stem
            if name in self._builtin_tool_names:
                continue
            if name in self._dynamic_tool_names:
                self._remove_tool(name)
            self._quarantined[name] = reason
            refused.append(name)
        if not refused:
            return True
        self._agent = self._build_agent()
        for name in refused:
            logger.warning("Generated tool %s %s", name, reason)
            self._schedule(self._emit_thought(
                getattr(self, "_current_event_id", "evt_tools"),
                "tool_result",
                f"Generated tool {name} {reason}",
            ))
        self.notify_tools_changed("quarantined", refused)
        return True

    @staticmethod
    def _import_tool_module(
        name: str, path: Path, exclude: frozenset[str] = frozenset()
    ) -> Any:
        """Import ``path`` as ``tools.generated.<name>`` and return its Tool.

        A generated module may import other tools (``from
        tools.show_notification import show_notification``), so its
        namespace can hold several Tool objects. The tool named after the
        file wins. Otherwise the first Tool whose name is not in
        ``exclude`` (built-ins and other loaded tools) is returned.
        Returning an imported tool would register it twice and break the
        agent rebuild ("Each tool ... should have a unique name").
        """
        mod_name = f"tools.generated.{name}"
        sys.modules.pop(mod_name, None)
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        try:
            from smolagents import Tool  # type: ignore
        except ImportError:
            Tool = None  # type: ignore
        candidates = [
            attr for attr in vars(module).values()
            if (Tool is not None and isinstance(attr, Tool))
            or (callable(attr) and isinstance(getattr(attr, "name", None), str))
        ]
        for attr in candidates:
            if getattr(attr, "name", None) == name:
                return attr
        for attr in candidates:
            if Tool is not None and isinstance(attr, Tool) and attr.name not in exclude:
                return attr
        return None

    def _replace_tool(self, name: str, tool_obj: Any) -> None:
        """Add or replace a tool in :attr:`tools` by name, preserving order."""
        for idx, existing in enumerate(self.tools):
            ex_name = getattr(existing, "name", getattr(existing, "__name__", ""))
            if ex_name == name:
                self.tools[idx] = tool_obj
                return
        self.tools.append(tool_obj)

    # ---- main loop ----------------------------------------------------

    # Warn if the event queue exceeds this depth (indicates processing backlog)
    _QUEUE_DEPTH_WARNING_THRESHOLD = 20

    async def _run(self) -> None:
        while True:
            # Monitor queue depth to detect processing backlog
            queue_size = self.event_bus.qsize()
            if queue_size > self._QUEUE_DEPTH_WARNING_THRESHOLD:
                logger.warning(
                    "Event queue depth %d exceeds threshold %d; "
                    "processing may be falling behind",
                    queue_size,
                    self._QUEUE_DEPTH_WARNING_THRESHOLD,
                )
            event = await self.event_bus.consume()
            try:
                await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error handling event: %s", event)

    async def _handle_event(self, event: ContextEvent) -> None:
        if event.event_type == NOTICE_EVENT_TYPE:
            # A listener thread's notice (#19): not an observation, so no
            # thought, no observers, no agent.
            try:
                self.publish_notice(event.metadata.get("notice") or {})
            except Exception:
                logger.exception("Invalid notice dropped: %r", event.metadata)
            return
        event_id = event.metadata.get("event_id") or f"evt_{uuid.uuid4().hex[:12]}"
        self._turn_tool_calls = set()
        self._turn_done = {}
        self._turn_kind = "event"
        # Personal text an idea may quote later (#51).
        privacy.note_personal_event(event)
        await self._emit_thought(event_id, "event_received", self._describe_event(event))

        if self.pattern_recognizer is not None:
            try:
                self.pattern_recognizer.observe(event)
            except Exception:
                logger.exception("pattern_recognizer.observe failed")
        for source in self.idea_sources:
            observe = getattr(source, "observe", None)
            if observe is None:
                continue
            try:
                observe(event)
            except Exception:
                logger.exception("idea source %r observe failed", source)

        if event.event_type == "pattern_detected":
            prompt = self._build_pattern_prompt(event)
            await self._emit_thought(
                event_id, "reasoning", f"Prompt: {prompt[:200]}"
            )
            meta = event.metadata or {}
            self._current_builtin_idea = meta if meta.get("builtin_tool") else None
            self._turn_kind = "pattern"
            # #61: a tool installed this turn records which idea led to it.
            from tools import meta_tool_generator

            meta_tool_generator.set_current_idea(meta, event_id=event_id)
            try:
                result = await self._run_agent(event_id, prompt, max_steps=4)
            finally:
                self._current_builtin_idea = None
            await self._emit_thought(event_id, "complete", str(result)[:500])
            return

        if event.event_type == "user_message":
            await self._handle_user_message(event, event_id)
            return

        # Signal-only events (e.g. ``app_activated``) feed pattern detection
        # and the activity stream but never trigger the tool-calling agent;
        # see ``_AGENT_TRIGGER_EXCLUDED_EVENT_TYPES`` for the rationale.
        if event.event_type in _AGENT_TRIGGER_EXCLUDED_EVENT_TYPES:
            return
        if event.event_type == "mail_received" and _mail_skips_agent(event):
            return

        analysis = await self._analyse_event(event)
        if analysis:
            await self._emit_thought(event_id, "analysis", analysis)

        memories = await self._recall_memories(event)
        if memories:
            await self._emit_thought(
                event_id, "memory", "Recalled: " + " | ".join(memories)
            )

        prompt = self._build_prompt(event, memories=memories)
        await self._emit_thought(event_id, "reasoning", f"Prompt: {prompt[:200]}")

        # Ambient events must terminate in one tool call or ``no action``;
        # a tight step cap keeps a malformed tool call from spiralling
        # into an endless parse-retry loop. Email gets one extra step for
        # the ``read_email`` call before the action.
        max_steps = 3 if event.event_type == "mail_received" else 2
        result = await self._run_agent(event_id, prompt, max_steps=max_steps)
        await self._emit_thought(event_id, "complete", str(result)[:500])

    async def _handle_user_message(
        self, event: ContextEvent, event_id: str
    ) -> None:
        """Run a direct chat turn and persist both sides of the exchange.

        Mirrors the ambient-event path but skips the analysis engine (the
        user already told us what they want) and logs the user turn + final
        agent reply into the :class:`Memory` chat transcript so the chat
        window can re-hydrate on reconnect.
        """
        text = (event.metadata.get("text") or "").strip()
        if self.memory is not None and text:
            try:
                await asyncio.to_thread(
                    self.memory.log_chat, event_id, "user", text
                )
            except Exception:
                logger.exception("memory.log_chat (user) failed")

        memories = await self._recall_memories(event)
        if memories:
            await self._emit_thought(
                event_id, "memory", "Recalled: " + " | ".join(memories)
            )

        prompt = self._build_chat_prompt(event, memories=memories)
        await self._emit_thought(
            event_id, "reasoning", f"Prompt: {prompt[:200]}"
        )

        # Chat turns may chain: pick tool → call tool → final_answer, and
        # ``generate_custom_tool`` adds a user-approval round-trip, so
        # allow a moderate budget without leaving the loop unbounded.
        self._pending_rerun_tool = None
        self._turn_tool_calls = set()
        self._turn_done = {}
        self._turn_kind = "chat"
        result = await self._run_agent(event_id, prompt, max_steps=6)
        reply = str(result) if result is not None else ""

        # If the turn generated a tool, the current agent's
        # schema was frozen before the tool was available. ``load_dynamic_tools``
        # has already rebuilt ``self._agent`` with the new tool, so one more
        # pass can call it directly without making the user re-ask. The flag
        # is consumed here so a rerun can never cascade into another rerun.
        pending = self._pending_rerun_tool
        self._pending_rerun_tool = None
        if pending:
            rerun_prompt = (
                f"The tool `{pending}` is now installed and available in "
                "your tool list. Call it directly to fulfil the user's "
                "original request below. Do NOT call `generate_custom_tool` "
                f"again for `{pending}`.\n\n{prompt}"
            )
            await self._emit_thought(
                event_id, "reasoning",
                f"Auto-rerun with {pending}: {rerun_prompt[:160]}",
            )
            result = await self._run_agent(event_id, rerun_prompt, max_steps=6)
            reply = str(result) if result is not None else reply

        await self._emit_thought(event_id, "complete", reply[:500])

        for source in self.idea_sources:
            observe_chat = getattr(source, "observe_chat", None)
            if observe_chat is None:
                continue
            try:
                observe_chat(text, reply, frozenset(self._turn_tool_calls))
            except Exception:
                logger.exception("idea source %r observe_chat failed", source)

        if self.memory is not None and reply:
            try:
                await asyncio.to_thread(
                    self.memory.log_chat, event_id, "assistant", reply
                )
            except Exception:
                logger.exception("memory.log_chat (assistant) failed")

    async def _recall_memories(self, event: ContextEvent) -> list[str]:
        """Pull the top few most relevant prior memories for ``event``.

        Runs synchronously off-thread so the embedding call never blocks the
        event loop. Errors are swallowed: memory is a best-effort context
        signal, never a hard dependency for handling an event.
        """
        if self.memory is None:
            return []
        query = self._describe_event(event)
        try:
            return await asyncio.to_thread(self.memory.recall_memory, query, 3)
        except Exception:
            logger.exception("memory.recall_memory failed")
            return []

    async def _analyse_event(self, event: ContextEvent) -> str:
        """Run :meth:`MLXEngine.evaluate_event` for a plain-text summary.

        Uses :attr:`analysis_engine` (a dedicated lightweight model when
        configured, otherwise the main engine). Errors are swallowed so
        the tool-calling path always runs. Returns an empty string when
        analysis is unavailable.
        """
        try:
            # Personal text stays local when the analysis engine is a
            # cloud one (#31).
            return await self.analysis_engine.evaluate_event(
                self._describe_event(privacy.redact_event(event, "analysis"))
            )
        except Exception:
            logger.exception("evaluate_event failed")
            return ""

    # ---- agent + tool wiring ------------------------------------------

    def _build_agent(self) -> Any:
        """Construct a smolagents agent, if the library is available."""
        try:
            from smolagents import ToolCallingAgent  # type: ignore
        except ImportError:
            logger.warning("smolagents not installed; orchestrator runs in fallback mode")
            return None

        wrapped = [self._wrap_tool_for_approval(t) for t in self.tools]
        model = self._build_smolagents_model()
        if model is None:
            return None
        return ToolCallingAgent(tools=wrapped, model=model)

    def _build_smolagents_model(self) -> Any:
        """Pick the right smolagents ``Model`` adapter for ``self.engine``.

        Local MLX engines route through :func:`_make_mlx_model_class` so we
        keep the in-house ``<tool_call>`` parser and shared Metal lock.
        Groq engines plug straight into smolagents' built-in
        ``OpenAIServerModel`` pointed at Groq's OpenAI-compatible endpoint.
        Mocks (used in tests) match neither and fall back to a no-op
        ``ToolCallingAgent``-less path via :meth:`_fallback_run`.
        """
        if isinstance(self.engine, MLXEngine):
            _MLXModel = _make_mlx_model_class()
            return _MLXModel(self.engine)
        try:
            from .groq_engine import GroqEngine  # type: ignore
        except ImportError:
            GroqEngine = None  # type: ignore
        if GroqEngine is not None and isinstance(self.engine, GroqEngine):
            try:
                from smolagents.models import OpenAIServerModel  # type: ignore
            except ImportError:
                logger.warning(
                    "smolagents.OpenAIServerModel unavailable; "
                    "Groq engine cannot drive the agent loop"
                )
                return None
            return _make_groq_model_class(OpenAIServerModel)(
                model_id=self.engine.model_name,
                api_base=self.engine.api_base,
                api_key=self.engine.api_key,
                flatten_messages_as_text=False,
            )
        return None

    async def _run_agent(
        self,
        event_id: str,
        prompt: str,
        max_steps: Optional[int] = None,
    ) -> Any:
        """Run the tool-calling agent, capping reasoning steps.

        ``max_steps`` is passed straight through to ``agent.run`` so
        each caller can pick a budget that matches the event's shape:
        ambient events (one tool or ``no action``) get a tight cap,
        user-chat turns (which may invoke ``generate_custom_tool``)
        get more room. ``None`` falls back to smolagents' default.
        """
        if self._agent is None:
            return await self._fallback_run(event_id, prompt)
        self._current_event_id = event_id
        if max_steps is None:
            return await asyncio.to_thread(self._agent.run, prompt)
        return await asyncio.to_thread(
            lambda: self._agent.run(prompt, max_steps=max_steps)
        )

    async def _fallback_run(self, event_id: str, prompt: str) -> str:
        """Run a minimal loop that invokes each tool under approval gating.

        Used when smolagents is unavailable so the approval plumbing can still
        be exercised end-to-end (e.g. for integration tests).
        """
        for tool in self.tools:
            tool_name = getattr(tool, "name", getattr(tool, "__name__", "tool"))
            if tool_name in READ_ONLY_TOOL_NAMES:
                try:
                    result = tool() if callable(tool) else None
                except Exception as exc:  # pragma: no cover - defensive
                    result = f"error: {exc}"
                await self._emit_thought(
                    event_id, "tool_result", f"{tool_name} (read-only): {result}"
                )
                continue
            approved, edited = await self._request_approval(
                event_id=event_id,
                tool_name=tool_name,
                tool_args={"args": [], "kwargs": {}},
                reasoning=f"Fallback invocation of {tool_name}",
            )
            if not approved:
                await self._emit_thought(event_id, "tool_result", f"{tool_name}: skipped")
                continue
            call_args, call_kwargs = self._apply_edits(edited, [], {})
            try:
                result = (
                    tool(*call_args, **call_kwargs) if callable(tool) else None
                )
            except Exception as exc:  # pragma: no cover - defensive
                result = f"error: {exc}"
            await self._emit_thought(event_id, "tool_result", f"{tool_name}: {result}")
        return "done"

    def _wrap_tool_for_approval(self, tool: Any) -> Any:
        """Return a copy of ``tool`` whose invocation is gated on approval.

        Tools whose name is in :data:`tools.READ_ONLY_TOOL_NAMES` bypass the
        approval round-trip and run inline; a ``tool_result`` thought is
        still emitted so the UI stream reflects the call.
        """
        orchestrator = self

        # Wrap a shallow copy: the tool objects in ``AVAILABLE_TOOLS`` are
        # shared, and ``_build_agent`` runs again after every dynamic-tool
        # load. Replacing ``forward`` on the shared object nested one more
        # approval gate per rebuild (one prompt per layer) and left stale
        # wrappers bound to old orchestrators and closed loops (#48).
        original_forward = getattr(tool, "_ungated_forward", None) or (
            getattr(tool, "forward", None) or getattr(tool, "__call__", None)
        )
        tool_name = getattr(tool, "name", getattr(tool, "__name__", "tool"))
        is_read_only = tool_name in READ_ONLY_TOOL_NAMES

        def gated_forward(*args: Any, **kwargs: Any) -> Any:
            loop = orchestrator.event_bus.loop or asyncio.get_event_loop()
            event_id = getattr(orchestrator, "_current_event_id", "evt_unknown")
            call_args: list[Any] = list(args)
            # Models (GPT-OSS on Groq) send null for optional parameters;
            # drop them so the tool's own defaults apply.
            call_kwargs: dict[str, Any] = {
                k: v for k, v in kwargs.items() if v is not None
            }
            if (
                tool_name == "generate_custom_tool"
                and tool_name in orchestrator._turn_tool_calls
            ):
                # #38: one generation per turn. A retry after a failure
                # asked the user to approve again (4 prompts in one turn).
                result = {
                    "status": "error",
                    "error": "generate_custom_tool_already_called",
                    "message": (
                        "generate_custom_tool was already called in this "
                        "turn. Do not call it again; emit final_answer "
                        "reporting the earlier result."
                    ),
                }
                asyncio.run_coroutine_threadsafe(
                    orchestrator._emit_thought(
                        event_id, "tool_result", f"{tool_name}: {result}"
                    ),
                    loop,
                )
                return result
            refusal: Optional[dict[str, Any]] = None
            if tool_name == "generate_custom_tool":
                declined = orchestrator._previously_declined(call_args, call_kwargs)
                if declined:
                    # The user denied this tool idea before: don't ask again
                    # (live check: one card per incoming iMessage asking to
                    # build a send-iMessage tool).
                    refusal = {
                        "status": "error",
                        "error": "user_declined_before",
                        "declined_tool": declined,
                        "message": (
                            "The user already declined this tool. Do not "
                            "ask again; reply 'no action' or answer without it."
                        ),
                    }
            done_key = ""
            if not is_read_only and refusal is None:
                done_key = tool_name + json.dumps(
                    {"a": call_args, "k": call_kwargs}, sort_keys=True, default=str,
                )
                done = getattr(orchestrator, "_turn_done", {})
                if done_key in done:
                    refusal = {
                        "skipped": True,
                        "reason": "already_done_this_turn",
                        "result": done[done_key],
                        "message": "This exact call already ran this turn; "
                        "don't repeat it. Emit final_answer.",
                    }
            if refusal is not None:
                asyncio.run_coroutine_threadsafe(
                    orchestrator._emit_thought(
                        event_id, "tool_result", f"{tool_name}: {refusal}"
                    ),
                    loop,
                )
                return refusal
            orchestrator._turn_tool_calls.add(tool_name)
            if not is_read_only:
                coro = orchestrator._request_approval(
                    event_id=event_id,
                    tool_name=tool_name,
                    tool_args={"args": list(args), "kwargs": kwargs},
                    reasoning=f"Agent wants to call {tool_name}",
                )
                approved, edited = asyncio.run_coroutine_threadsafe(
                    coro, loop
                ).result(timeout=orchestrator.approval_timeout + 5)
                if not approved:
                    return {"skipped": True, "reason": "user_denied"}
                call_args, call_kwargs = orchestrator._apply_edits(
                    edited, call_args, call_kwargs
                )
            result = (
                original_forward(*call_args, **call_kwargs)
                if original_forward
                else None
            )
            if tool_name == "generate_custom_tool":
                result = orchestrator._flag_quarantined_install(result)
            done_store = getattr(orchestrator, "_turn_done", None)
            if done_key and done_store is not None:
                done_store[done_key] = result
            if tool_name == "read_email":
                # The result goes back into the main engine's prompt (#31).
                result = privacy.redact_email_result(result, "main")
            label = f"{tool_name} (read-only)" if is_read_only else tool_name
            asyncio.run_coroutine_threadsafe(
                orchestrator._emit_thought(event_id, "tool_result", f"{label}: {result}"),
                loop,
            )
            return result

        if hasattr(tool, "forward"):
            wrapped = copy.copy(tool)
            wrapped._ungated_forward = original_forward  # type: ignore[attr-defined]
            wrapped.forward = gated_forward  # type: ignore[attr-defined]
            return wrapped
        return gated_forward

    def _flag_quarantined_install(self, result: Any) -> Any:
        """Turn an "ok" install whose tool was quarantined into an error.

        The meta-tool reports ``ok`` once ``load_dynamic_tools`` returns,
        but a module that failed to load is quarantined there (#28) rather
        than raising. Report the failure and drop the auto-rerun.
        """
        if not isinstance(result, dict) or result.get("status") != "ok":
            return result
        name = result.get("tool_name")
        if name not in self._quarantined or name in self._dynamic_tool_names:
            return result
        if self._pending_rerun_tool == name:
            self._pending_rerun_tool = None
        reason = self._quarantined[name]
        if reason.startswith("not loaded:"):  # sandbox unavailable (#22)
            error = f"installed but {reason}"
        else:
            error = f"installed but failed to load; moved to quarantine/: {reason}"
        return {"status": "error", "tool_name": name, "error": error}

    @staticmethod
    def _apply_edits(
        edited: dict[str, Any] | None,
        default_args: list[Any],
        default_kwargs: dict[str, Any],
    ) -> tuple[list[Any], dict[str, Any]]:
        """Merge user-edited args from the approval response.

        The approval UI sends ``edited_args`` with the same shape as the
        outbound ``tool_args`` (``{"args": [...], "kwargs": {...}}``). Either
        sub-key is optional; missing ones fall back to the values the agent
        originally proposed.
        """
        if not edited:
            return list(default_args), dict(default_kwargs)
        args = edited.get("args")
        kwargs = edited.get("kwargs")
        call_args = list(args) if isinstance(args, list) else list(default_args)
        call_kwargs = (
            dict(kwargs) if isinstance(kwargs, dict) else dict(default_kwargs)
        )
        return call_args, call_kwargs

    # ---- approval + streaming ----------------------------------------

    async def _request_approval(
        self,
        event_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        reasoning: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """Return ``(approved, edited_args)`` from the user.

        ``edited_args`` — when non-None — mirrors the shape of ``tool_args``
        (``{"args": [...], "kwargs": {...}}``) and is honoured by the caller
        in place of the original arguments proposed by the agent.
        """
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        # Use asyncio.get_running_loop() for Python 3.10+ compatibility
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalResponsePayload] = loop.create_future()
        self._pending[request_id] = fut

        msg = ApprovalRequest(
            seq=self._next_seq(),
            timestamp=_now_iso(),
            payload=ApprovalRequestPayload(
                request_id=request_id,
                event_id=event_id,
                tool_name=tool_name,
                tool_args=tool_args,
                reasoning=reasoning,
                timeout_seconds=int(self.approval_timeout),
            ),
        )
        await self._send(msg)

        try:
            resp = await asyncio.wait_for(fut, timeout=self.approval_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            logger.info("Approval %s timed out", request_id)
            return False, None
        if tool_name == "generate_custom_tool":
            self._record_idea_feedback(tool_args, bool(resp.approved))
        idea = self._current_builtin_idea
        if idea is not None and tool_name == idea.get("builtin_tool"):
            # A declined built-in follow-up suppresses that kind of idea;
            # a timeout (handled above) is not a rejection.
            self._record_idea_feedback(
                {"kwargs": {
                    "tool_name": idea.get("tool_name", ""),
                    "description": idea.get("description", ""),
                }},
                bool(resp.approved),
            )
        return bool(resp.approved), resp.edited_args

    def _previously_declined(self, args: list[Any], kwargs: dict[str, Any]) -> str:
        """Name of a declined tool idea this generate_custom_tool call
        matches (same name, or close enough by words), else "".

        Uses the shared ``IdeaFeedback`` the idea sources hold, so a Deny on
        any build card is remembered across turns and restarts.
        """
        name = str(kwargs.get("tool_name") or (args[0] if args else "") or "")
        desc = str(kwargs.get("description") or (args[1] if len(args) > 1 else "") or "")
        if not name:
            return ""
        matcher = getattr(self, "declined_matcher", None)
        if matcher is not None:
            try:
                return matcher.match(name, desc)
            except Exception:
                logger.exception("declined_matcher failed")
        seen: set[int] = set()
        for source in self.idea_sources:
            feedback = getattr(source, "feedback", None)
            check = getattr(feedback, "resembles_rejected", None)
            if check is None or id(feedback) in seen:
                continue
            seen.add(id(feedback))
            try:
                if check(name, desc):
                    return name
            except Exception:
                logger.exception("resembles_rejected failed")
        return ""

    def _record_idea_feedback(self, tool_args: dict[str, Any], approved: bool) -> None:
        """Tell idea sources the user approved or rejected a tool idea.

        Timeouts are not recorded: no answer is not a rejection.
        """
        kwargs = tool_args.get("kwargs") or {}
        args = tool_args.get("args") or []
        name = kwargs.get("tool_name") or (args[0] if args else "")
        desc = kwargs.get("description") or (args[1] if len(args) > 1 else "")
        if not name:
            return
        for source in self.idea_sources:
            record = getattr(source, "record_feedback", None)
            if record is None:
                continue
            try:
                record(str(name), str(desc), approved)
            except Exception:
                logger.exception("idea source %r record_feedback failed", source)

    async def _emit_thought(self, event_id: str, stage: str, content: str) -> None:
        msg = Thought(
            seq=self._next_seq(),
            timestamp=_now_iso(),
            payload=ThoughtPayload(event_id=event_id, stage=stage, content=content),
        )
        await self._send(msg)

    async def _send(self, message: Any) -> None:
        if not self._subscribers:
            logger.debug(
                "no subscribers; dropping %s", type(message).__name__
            )
            return
        dead: list[WSSendCallable] = []
        for send in list(self._subscribers):
            try:
                await send(message)
            except Exception:
                logger.exception("ws_send failed; dropping subscriber")
                dead.append(send)
        for send in dead:
            self._subscribers.discard(send)
            if self._ws_send is send:
                self._ws_send = next(iter(self._subscribers), None)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ---- prompt building ---------------------------------------------

    def _build_prompt(
        self, event: ContextEvent, memories: Optional[list[str]] = None
    ) -> str:
        # Every prompt built here goes to the main engine (#31).
        event = privacy.redact_event(event, "main")
        if event.event_type == "imessage_received":
            return self._build_imessage_prompt(event, memories=memories)
        if event.event_type == "mail_received":
            return self._build_mail_prompt(event, memories=memories)

        parts = [
            "You are an on-device macOS assistant. Decide whether to help "
            "the user based on the following system event.",
            f"Event type: {event.event_type}",
        ]
        if event.app_name:
            parts.append(f"Active app: {event.app_name}")
        if event.window_title:
            parts.append(f"Window: {event.window_title}")
        if event.metadata:
            parts.append(f"Metadata: {event.metadata}")
        memory_block = self._format_memories(memories)
        if memory_block:
            parts.append(memory_block)
        parts.append(
            "If an action would help, call exactly one tool. Otherwise reply "
            "'no action'."
        )
        return "\n".join(parts)

    @staticmethod
    def _build_pattern_prompt(event: ContextEvent) -> str:
        """Steer the agent toward ``generate_custom_tool`` for a detected pattern.

        The pattern recognizer publishes the proposed ``tool_name``,
        ``description`` and ``expected_logic`` in ``event.metadata``; those
        fields are interpolated here so the agent has a concrete call to
        make rather than re-deriving the intent from scratch.
        """
        meta = event.metadata
        tool_name = meta.get("tool_name", "new_helper")
        # A local detection engine may have quoted an iMessage or a mail
        # subject into the idea; a cloud main engine must not see it (#51).
        description = privacy.scrub_idea_text(meta.get("description", ""), "main")
        logic = privacy.scrub_idea_text(meta.get("expected_logic", ""), "main")
        builtin = meta.get("builtin_tool")
        if builtin:
            # Known follow-up a built-in tool can do directly (#32): no new
            # tool is drafted; the built-in raises its own approval card.
            # Mail subjects and iMessage text stay local when the main
            # engine is a cloud one (#31); the redacted copy keeps the ids.
            raw_details = meta.get("event_details") or {}
            if privacy.should_redact("main"):
                raw_details = meta.get("event_details_redacted", privacy.PLACEHOLDER)
            details = json.dumps(raw_details, default=str)[:1500]
            return (
                "You are an on-device macOS assistant. An event just happened "
                "that has a known follow-up.\n\n"
                f"Event details: {details}\n"
                f"Follow-up: {description}\n\n"
                f"Call the built-in `{builtin}` tool now to do this follow-up, "
                "with arguments taken from the event details (if the follow-up "
                "says to read an email first, call `read_email` with its "
                "message_id before that). Do NOT call `generate_custom_tool`. "
                f"`{builtin}` asks the user for approval itself. If the details "
                "don't support the follow-up, reply with the plain text 'no action'."
            )
        intro = _PATTERN_SOURCE_INTROS.get(
            meta.get("source", "repetition"), _PATTERN_SOURCE_INTROS["repetition"]
        )
        return (
            f"You are an on-device macOS assistant. {intro}\n\n"
            f"Proposed tool name: {tool_name}\n"
            f"Description: {description}\n"
            f"Expected logic: {logic}\n\n"
            "Call the `generate_custom_tool` tool exactly once with "
            f"`tool_name={tool_name!r}`, a one-sentence description, and "
            "a clear expected_logic paragraph. The user will review the "
            "drafted source before it is installed; do not attempt to "
            "invoke the new tool in this turn. If the pattern does not "
            "warrant a new tool, reply with the plain text 'no action'."
        )

    @staticmethod
    def _build_chat_prompt(
        event: ContextEvent, memories: Optional[list[str]] = None
    ) -> str:
        """System prompt for a direct user → agent chat turn.

        Unlike the ambient-event prompt this is phrased as a conversation
        and explicitly permits a plain-text reply. Tools remain available
        but are gated on the user's intent; the agent is told not to call
        a tool unless the message clearly asks for one. When the request
        would be solved by a tool we don't yet have, the agent is steered
        toward ``generate_custom_tool`` rather than a vague refusal.
        """
        text = (event.metadata.get("text") or "").strip()
        memory_block = Orchestrator._format_memories(memories)
        memory_suffix = f"\n\n{memory_block}" if memory_block else ""
        return (
            "You are an on-device macOS assistant in a direct chat with "
            "the user. They just sent this message:\n\n"
            f"\"{text}\"\n\n"
            "Decide how to respond, in this order:\n"
            "1. Look at your available tools first. If one of them can "
            "accomplish the request, call it — do not generate a "
            "duplicate.\n"
            "2. If no existing tool fits but a small Python tool could "
            "(e.g. a timer, a calculator, a file utility, a local API "
            "call), call `generate_custom_tool` with a concrete "
            "`tool_name` (snake_case), a one-sentence `description`, and "
            "a short `expected_logic` paragraph. The user will review the "
            "drafted code before it is installed. After the tool "
            "installs, it is NOT callable in this turn — immediately "
            "reply with final_answer telling the user the tool is ready "
            "and asking them to re-issue their original request.\n"
            "3. Otherwise, reply in plain text.\n\n"
            "Be concise. Do not fabricate results. Do not call "
            "`generate_custom_tool` for questions that only need a direct "
            "answer (definitions, explanations, chit-chat)."
            f"{memory_suffix}"
        )

    @staticmethod
    def _build_imessage_prompt(
        event: ContextEvent, memories: Optional[list[str]] = None
    ) -> str:
        event = privacy.redact_event(event, "main")
        sender = event.metadata.get("sender", "unknown")
        text = event.metadata.get("text", "")
        memory_block = Orchestrator._format_memories(memories)
        memory_suffix = f"\n\n{memory_block}" if memory_block else ""
        return (
            "You are an on-device macOS assistant watching the user's "
            "iMessage inbox on their behalf.\n"
            f"A new inbound message just arrived from {sender}:\n\n"
            f"\"{text}\"\n\n"
            "Decide whether a reply is warranted. Good candidates: a "
            "direct question, a plan-making message, or anything that "
            "clearly expects a response. Bad candidates: promotional "
            "SMS, 2FA codes, automated alerts, or messages that are "
            "obviously not for the assistant to answer."
            f"{memory_suffix}\n\n"
            "If — and only if — a reply is warranted, call the "
            "`send_imessage` tool exactly once with "
            f"`target_number={sender!r}` and a short, friendly reply "
            "written in the user's voice. The call will prompt the user "
            "for explicit approval before the message is sent. "
            "Otherwise, reply with the plain text 'no action'."
        )

    @staticmethod
    def _build_mail_prompt(
        event: ContextEvent, memories: Optional[list[str]] = None
    ) -> str:
        """Have the agent read a new email and offer a reminder or event.

        The subject is redacted by ``privacy.redact_event`` when the main
        engine is a cloud one (#31); ``read_email`` results are redacted
        the same way in the gated tool wrapper.
        """
        event = privacy.redact_event(event, "main")
        meta = event.metadata or {}
        sender = meta.get("sender", "unknown")
        subject = meta.get("subject", "") or "(no subject)"
        message_id = meta.get("message_id")
        memory_block = Orchestrator._format_memories(memories)
        memory_suffix = f"\n\n{memory_block}" if memory_block else ""
        return (
            "You are an on-device macOS assistant watching the user's "
            "email inbox on their behalf.\n"
            f"A new email just arrived from {sender}.\n"
            f"Subject: \"{subject}\"\n"
            f"message_id: {message_id}"
            f"{memory_suffix}\n\n"
            "First call `read_email` with "
            f"`message_id={message_id!r}` to read the body.\n"
            "Then decide whether the email asks the user to do something:\n"
            "- A task, to-do or reminder (for example \"Reminder: take the "
            "dog on a walk\"): call `create_reminder` exactly once with a "
            "short title, and a due date only if the email gives one.\n"
            "- A meeting or appointment with a date and time: call "
            "`create_event` exactly once.\n"
            "Both tools ask the user for approval before anything is saved.\n"
            "Newsletters, receipts, promotions, notifications and anything "
            "that needs no action from the user: call `final_answer` with "
            "'no action'."
        )

    @staticmethod
    def _format_memories(memories: Optional[list[str]]) -> str:
        if not memories:
            return ""
        lines = ["Relevant memories from prior sessions:"]
        lines.extend(f"- {m}" for m in memories)
        return "\n".join(lines)

    @staticmethod
    def _describe_event(event: ContextEvent) -> str:
        if event.event_type == "imessage_received":
            sender = event.metadata.get("sender", "unknown")
            text = event.metadata.get("text", "") or ""
            preview = text if len(text) <= 120 else text[:120] + "…"
            return f"iMessage from {sender}: {preview}"
        if event.event_type == "pattern_detected":
            tool_name = event.metadata.get("tool_name", "unknown")
            desc = event.metadata.get("description", "")
            return f"Pattern detected → suggested tool: {tool_name} — {desc}"
        meta = event.metadata or {}
        if event.event_type == "mail_received":
            return (
                f"Email from {meta.get('sender', 'unknown')}: "
                f"{meta.get('subject', '') or '(no subject)'} "
                f"[{meta.get('mailbox', '')}, id {meta.get('message_id')}]"
            )
        if event.event_type == "event_starting_soon":
            start = str(meta.get("start", ""))
            text = f"Meeting starting soon: {meta.get('title', '(untitled)')} at {start[11:16]}"
            if meta.get("location"):
                text += f" — {meta['location']}"
            if meta.get("video_url"):
                text += f" — video link: {meta['video_url']}"
            return text
        if event.event_type == "clipboard_changed":
            return f"Copied {meta.get('kind', 'something')} in {event.app_name or 'an app'}"
        if event.event_type == "user_message":
            text = event.metadata.get("text", "") or ""
            preview = text if len(text) <= 120 else text[:120] + "…"
            return f"User said: {preview}"
        return (
            f"{event.event_type}"
            + (f" in {event.app_name}" if event.app_name else "")
            + (f" — {event.window_title}" if event.window_title else "")
        )


_TOOL_CALL_REJECTIONS = (
    "tool_use_failed",
    "Failed to parse tool call",
    "Tool call validation failed",
    "Tool choice is required",
    "which was not in request.tools",
)


def _is_tool_call_rejection(exc: Exception) -> bool:
    """True for Groq 400s caused by a malformed or invalid tool call."""
    text = str(exc)
    return "400" in text and any(marker in text for marker in _TOOL_CALL_REJECTIONS)


def _allow_null_optionals(spec: dict[str, Any]) -> None:
    """Let optional parameters be null in a tool's JSON schema (in place).

    GPT-OSS sends ``null`` for optional parameters it doesn't use; Groq then
    rejects the call against a plain ``"type": "string"``. ``gated_forward``
    drops null arguments so the tool's own defaults apply.
    """
    params = (spec.get("function") or {}).get("parameters") or {}
    required = set(params.get("required") or [])
    for name, prop in (params.get("properties") or {}).items():
        if name in required or not isinstance(prop, dict):
            continue
        kind = prop.get("type")
        if isinstance(kind, str) and kind != "null":
            prop["type"] = [kind, "null"]
        elif isinstance(kind, list) and "null" not in kind:
            prop["type"] = kind + ["null"]


def _has_tool_result(messages: Any) -> bool:
    """True when the conversation already contains a tool's observation."""
    for msg in messages or []:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        role = getattr(role, "value", role)
        if role in ("tool-response", "tool"):
            return True
    return False


_ANSWER_NUDGE = (
    "Reply to the user now in plain text, using the tool results above. "
    "Do not call any tools."
)


def _text_answer(generate: Any, messages: Any, stop_sequences: Any, kwargs: dict) -> str:
    """One tool-free completion so the model can phrase its final answer."""
    extra = {k: v for k, v in kwargs.items() if k != "tool_choice"}
    try:
        # smolagents wants list-of-parts content (a plain string fails its
        # message cleaning when flatten_messages_as_text is off).
        nudge = {"role": "user", "content": [{"type": "text", "text": _ANSWER_NUDGE}]}
        nudged = list(messages or []) + [nudge]
        reply = generate(nudged, stop_sequences=stop_sequences,
                         tools_to_call_from=None, **extra)
    except Exception:
        logger.exception("tool-free answer attempt failed")
        return ""
    content = getattr(reply, "content", "")
    return content.strip() if isinstance(content, str) else ""


def _make_groq_model_class(base: type) -> type:
    """``OpenAIServerModel`` that tolerates a plain-text reply on Groq.

    smolagents sends ``tool_choice="required"``. Groq enforces it server-side:
    when a model (GPT-OSS) answers in plain text after a tool result, Groq
    returns 400 "Tool choice is required, but model did not call a tool" and
    the whole chat turn fails (found in the 2026-11-09 live checks: the user
    asked "list my shortcuts", ``list_shortcuts`` ran, then no reply).
    Ask for ``tool_choice="auto"`` and treat a plain-text reply as the final
    answer, which is what the model meant.
    """

    class _GroqServerModel(base):  # type: ignore[misc, valid-type]
        def _prepare_completion_kwargs(self, *args, **kwargs):
            completion = super()._prepare_completion_kwargs(*args, **kwargs)
            for spec in completion.get("tools") or []:
                _allow_null_optionals(spec)
            return completion

        def generate(self, messages, stop_sequences=None, response_format=None,
                     tools_to_call_from=None, **kwargs):
            if tools_to_call_from:
                kwargs.setdefault("tool_choice", "auto")
            # Groq validates tool calls server-side and fails the whole turn
            # with a 400 when the model emits bad JSON or a wrong tool name.
            # The model is stochastic, so one retry usually succeeds.
            for attempt in range(2):
                try:
                    message = super().generate(
                        messages,
                        stop_sequences=stop_sequences,
                        response_format=response_format,
                        tools_to_call_from=tools_to_call_from,
                        **kwargs,
                    )
                    break
                except Exception as exc:  # openai.BadRequestError, lazily typed
                    if attempt == 0 and _is_tool_call_rejection(exc):
                        logger.info("Groq rejected a tool call (%s); retrying once", exc)
                        continue
                    raise
            content = (message.content or "").strip() if isinstance(message.content, str) else ""
            if (
                tools_to_call_from and not message.tool_calls and not content
                and _has_tool_result(messages)
            ):
                # Reasoning models sometimes answer a tool result with no
                # visible text. smolagents then retried and the model
                # called the same tool again (live check: one 'open
                # goole.com' opened it 3 times). End the turn instead, but
                # first give the model one chance to write the answer with
                # no tools offered (it said only "Done." to "what's on my
                # calendar today?").
                content = _text_answer(super().generate, messages, stop_sequences, kwargs) or "Done."
            if tools_to_call_from and not message.tool_calls and content:
                from smolagents.models import (  # type: ignore
                    ChatMessageToolCall,
                    ChatMessageToolCallFunction,
                )

                message.tool_calls = [ChatMessageToolCall(
                    id=f"call_{uuid.uuid4().hex[:12]}",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name="final_answer", arguments={"answer": content},
                    ),
                )]
            return message

    return _GroqServerModel


def _make_mlx_model_class() -> type:
    """Build the adapter class lazily so it inherits from smolagents.Model.

    Mirrors the official ``smolagents.MLXModel.generate()`` implementation but
    uses our :class:`MLXEngine` (with model-swap support) under the hood.

    Key improvements over a naive adapter:
    * Detects ``<tool_call>`` / ``</tool_call>`` markers emitted by models
      (e.g. Qwen, Hermes) and returns structured ``ChatMessageToolCall``
      objects so the smolagents ``ToolCallingAgent`` can route them directly
      instead of relying on fragile ``parse_json_blob`` heuristics.
    * Strips ``<think>`` / ``</think>`` reasoning blocks from ``content`` to
      avoid polluting the JSON extraction path.
    * Only passes ``tools`` to ``apply_chat_template`` when tools are
      actually provided (some Jinja2 templates mishandle ``tools=None``).
    * Sets a sensible ``max_tokens`` default (4096) so tool-calling
      responses are not silently truncated at 256 tokens.
    """
    try:
        from smolagents.models import Model as _BaseModel  # type: ignore
    except ImportError:
        _BaseModel = object  # type: ignore

    class _MLXModel(_BaseModel):
        """smolagents ``Model`` adapter around our :class:`MLXEngine`."""

        _DEFAULT_MAX_TOKENS = 4096

        def __init__(self, engine: MLXEngine, **kwargs: Any) -> None:
            self.engine = engine
            self.apply_chat_template_kwargs: dict[str, Any] = {
                "add_generation_prompt": True,
            }
            if _BaseModel is not object:
                # flatten_messages_as_text=True matches smolagents.MLXModel
                super().__init__(
                    flatten_messages_as_text=True,
                    model_id=engine.model_name,
                    **kwargs,
                )
            else:
                self.model_id = engine.model_name

        def _ensure_loaded(self) -> None:
            """Ensure the MLXEngine has a model loaded (blocking)."""
            if not self.engine.loaded:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self.engine.load())
                finally:
                    loop.close()

        # -- tool-call parsing helpers ------------------------------------

        @staticmethod
        def _strip_thinking(text: str) -> str:
            """Remove ``<think>…</think>`` blocks from generated text."""
            import re
            return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

        @staticmethod
        def _extract_tool_calls(text: str) -> tuple[str, list[Any]]:
            """Parse ``<tool_call>…</tool_call>`` blocks into structured calls.

            Returns ``(remaining_content, tool_calls)`` where
            ``tool_calls`` is a list of ``ChatMessageToolCall`` objects.
            If no markers are found, ``tool_calls`` is empty and the
            original text is returned unchanged.
            """
            import json as _json
            import re
            import uuid as _uuid

            from smolagents.models import (  # type: ignore
                ChatMessageToolCall,
                ChatMessageToolCallFunction,
            )

            pattern = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
            matches = pattern.findall(text)
            if not matches:
                return text, []

            tool_calls: list[Any] = []
            for raw_json in matches:
                try:
                    data = _json.loads(raw_json)
                except _json.JSONDecodeError:
                    continue
                name = data.get("name") or data.get("function", "")
                arguments = data.get("arguments") or data.get("parameters") or {}
                if isinstance(arguments, str):
                    try:
                        arguments = _json.loads(arguments)
                    except _json.JSONDecodeError:
                        pass
                tool_calls.append(
                    ChatMessageToolCall(
                        id=str(_uuid.uuid4()),
                        type="function",
                        function=ChatMessageToolCallFunction(
                            name=name,
                            arguments=arguments,
                        ),
                    )
                )

            # Remove tool_call blocks from content
            remaining = pattern.sub("", text).strip()
            return remaining, tool_calls

        # -- main generate ------------------------------------------------

        def generate(
            self,
            messages: list[Any],
            stop_sequences: Optional[list[str]] = None,
            response_format: Optional[dict[str, str]] = None,
            tools_to_call_from: Optional[list[Any]] = None,
            **kwargs: Any,
        ) -> Any:
            """Synchronous generate using chat template + mlx_lm.stream_generate.

            Flow:
            1. ``_prepare_completion_kwargs()`` to clean messages & extract tools
            2. ``tokenizer.apply_chat_template()`` with tool schemas
            3. ``mlx_lm.stream_generate()`` for token-by-token generation
            4. Parse ``<tool_call>`` markers into structured tool calls
            """
            from smolagents import ChatMessage  # type: ignore
            from smolagents.models import MessageRole, TokenUsage  # type: ignore

            try:
                from smolagents.models import remove_content_after_stop_sequences  # type: ignore
            except ImportError:
                def remove_content_after_stop_sequences(text: str, seqs: list[str]) -> str:
                    for s in seqs:
                        idx = text.find(s)
                        if idx != -1:
                            text = text[:idx]
                    return text

            self._ensure_loaded()

            import mlx_lm as _mlx_lm  # type: ignore

            # Use the base class helper to clean messages + extract tool schemas
            if hasattr(self, "_prepare_completion_kwargs"):
                completion_kwargs = self._prepare_completion_kwargs(
                    messages=messages,
                    stop_sequences=stop_sequences,
                    tools_to_call_from=tools_to_call_from,
                    **kwargs,
                )
                messages_clean = completion_kwargs.pop("messages")
                stops = completion_kwargs.pop("stop", [])
                tools = completion_kwargs.pop("tools", None)
                completion_kwargs.pop("tool_choice", None)
            else:
                # Fallback if base class doesn't provide the helper
                messages_clean = messages
                stops = stop_sequences or []
                tools = None
                completion_kwargs = kwargs.copy()

            # Ensure a reasonable max_tokens default
            completion_kwargs.setdefault("max_tokens", self._DEFAULT_MAX_TOKENS)

            # Build the prompt via the model's chat template.
            # Only pass tools when present — some Jinja2 templates
            # mishandle an explicit ``tools=None``.
            tokenizer = self.engine.tokenizer
            chat_tmpl_kwargs = dict(self.apply_chat_template_kwargs)
            if tools is not None:
                chat_tmpl_kwargs["tools"] = tools
            prompt_ids = tokenizer.apply_chat_template(
                messages_clean,
                **chat_tmpl_kwargs,
            )

            # Stream-generate tokens under the engine's generation lock so
            # concurrent callers (``evaluate_event``, ``generate_sync`` from
            # a meta-tool, another agent step) serialize against this loop
            # instead of trampling the shared Metal allocator.
            output_tokens = 0
            text = ""
            with self.engine.generation_lock:
                for response in _mlx_lm.stream_generate(
                    self.engine.model,
                    tokenizer,
                    prompt=prompt_ids,
                    **completion_kwargs,
                ):
                    output_tokens += 1
                    text += response.text
                    if stops and any(
                        (stop_index := text.rfind(stop)) != -1 for stop in stops
                    ):
                        text = text[:stop_index]
                        break
                self.engine._maybe_clear_metal_cache_locked()

            if stop_sequences is not None and not getattr(self, "supports_stop_parameter", False):
                text = remove_content_after_stop_sequences(text, stop_sequences)

            # Strip thinking blocks before tool-call extraction
            text = self._strip_thinking(text)

            # Extract structured tool calls from <tool_call> markers
            content, tool_calls = self._extract_tool_calls(text)

            return ChatMessage(
                role=MessageRole.ASSISTANT,
                content=content or None,
                tool_calls=tool_calls if tool_calls else None,
                raw={"out": text, "completion_kwargs": completion_kwargs},
                token_usage=TokenUsage(
                    input_tokens=len(prompt_ids) if isinstance(prompt_ids, list) else 0,
                    output_tokens=output_tokens,
                ),
            )

    return _MLXModel
