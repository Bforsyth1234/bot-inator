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
import importlib.util
import logging
import sys
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
    Thought,
    ThoughtPayload,
)
from tools import READ_ONLY_TOOL_NAMES

from .mlx_engine import MLXEngine
from .memory import Memory

logger = logging.getLogger(__name__)

WSSendCallable = Callable[[Any], Awaitable[None]]


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class Orchestrator:
    """Drives the agent loop on top of an :class:`MLXEngine`."""

    def __init__(
        self,
        engine: MLXEngine,
        event_bus: EventBus,
        memory: Optional[Memory] = None,
        tools: Optional[list[Any]] = None,
        approval_timeout: float = 120.0,
        analysis_engine: Optional[MLXEngine] = None,
        generated_tools_dir: Optional[Path] = None,
        code_approval_timeout: float = 300.0,
        pattern_recognizer: Any = None,
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
        # when a new tool was installed (or matched an existing file) so
        # :meth:`_handle_user_message` can transparently re-run the same
        # prompt against the rebuilt agent, which now has the tool in
        # its schema. Cleared immediately after consumption to keep the
        # auto-rerun single-shot per user turn.
        self._pending_rerun_tool: Optional[str] = None
        self.pattern_recognizer = pattern_recognizer
        self._agent: Any = None

    def mark_tool_ready_for_rerun(self, tool_name: str) -> None:
        """Flag the current user turn for an auto-rerun with ``tool_name``.

        Called from the meta-tool generator after a successful install
        (or when an identically-named tool already exists on disk). The
        flag is consumed by :meth:`_handle_user_message`.
        """
        self._pending_rerun_tool = tool_name

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
        fut: asyncio.Future[CodeApprovalResponsePayload] = (
            asyncio.get_event_loop().create_future()
        )
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

        Returns the sorted list of dynamic tool names currently loaded.

        Files starting with ``.`` (e.g. ``.gitkeep``) and the nested
        ``.git`` bookkeeping directory are always skipped.

        Thread-safe: uses ``_tool_load_lock`` to serialize concurrent calls.
        """
        with self._tool_load_lock:
            target = Path(directory) if directory else self.generated_tools_dir
            if target is None or not target.exists():
                return sorted(self._dynamic_tool_names)

            loaded: list[str] = []
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
                    tool_obj = self._import_tool_module(name, path)
                except Exception:
                    logger.exception(
                        "Failed to load dynamic tool from %s; skipping", path
                    )
                    continue
                if tool_obj is None:
                    logger.warning(
                        "No smolagents Tool instance found in %s; skipping", path
                    )
                    continue
                self._replace_tool(name, tool_obj)
                self._dynamic_tool_names.add(name)
                loaded.append(name)

            if loaded:
                self._agent = self._build_agent()
                logger.info("Loaded dynamic tools: %s", ", ".join(loaded))
            return sorted(self._dynamic_tool_names)

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
            self.tools = [
                t
                for t in self.tools
                if getattr(t, "name", getattr(t, "__name__", "")) != name
            ]
            self._dynamic_tool_names.discard(name)
            sys.modules.pop(f"tools.generated.{name}", None)
            self._agent = self._build_agent()
            logger.info("Unloaded dynamic tool: %s", name)
            return True

    @staticmethod
    def _import_tool_module(name: str, path: Path) -> Any:
        """Import ``path`` as ``tools.generated.<name>`` and return its Tool."""
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
        for attr in vars(module).values():
            if Tool is not None and isinstance(attr, Tool):
                return attr
            if callable(attr) and getattr(attr, "name", None) == name:
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

    async def _run(self) -> None:
        while True:
            event = await self.event_bus.consume()
            try:
                await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error handling event: %s", event)

    async def _handle_event(self, event: ContextEvent) -> None:
        event_id = event.metadata.get("event_id") or f"evt_{uuid.uuid4().hex[:12]}"
        await self._emit_thought(event_id, "event_received", self._describe_event(event))

        if self.pattern_recognizer is not None:
            try:
                self.pattern_recognizer.observe(event)
            except Exception:
                logger.exception("pattern_recognizer.observe failed")

        if event.event_type == "pattern_detected":
            prompt = self._build_pattern_prompt(event)
            await self._emit_thought(
                event_id, "reasoning", f"Prompt: {prompt[:200]}"
            )
            result = await self._run_agent(event_id, prompt, max_steps=4)
            await self._emit_thought(event_id, "complete", str(result)[:500])
            return

        if event.event_type == "user_message":
            await self._handle_user_message(event, event_id)
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
        # into an endless parse-retry loop.
        result = await self._run_agent(event_id, prompt, max_steps=2)
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
        result = await self._run_agent(event_id, prompt, max_steps=6)
        reply = str(result) if result is not None else ""

        # If the turn generated (or matched) a tool, the current agent's
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
            return await self.analysis_engine.evaluate_event(
                self._describe_event(event)
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
        _MLXModel = _make_mlx_model_class()
        model = _MLXModel(self.engine)
        return ToolCallingAgent(tools=wrapped, model=model)

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

        original_forward = getattr(tool, "forward", None) or getattr(tool, "__call__", None)
        tool_name = getattr(tool, "name", getattr(tool, "__name__", "tool"))
        is_read_only = tool_name in READ_ONLY_TOOL_NAMES

        def gated_forward(*args: Any, **kwargs: Any) -> Any:
            loop = orchestrator.event_bus.loop or asyncio.get_event_loop()
            event_id = getattr(orchestrator, "_current_event_id", "evt_unknown")
            call_args: list[Any] = list(args)
            call_kwargs: dict[str, Any] = dict(kwargs)
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
            label = f"{tool_name} (read-only)" if is_read_only else tool_name
            asyncio.run_coroutine_threadsafe(
                orchestrator._emit_thought(event_id, "tool_result", f"{label}: {result}"),
                loop,
            )
            return result

        if hasattr(tool, "forward"):
            tool.forward = gated_forward  # type: ignore[attr-defined]
            return tool
        return gated_forward

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
        fut: asyncio.Future[ApprovalResponsePayload] = asyncio.get_event_loop().create_future()
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
            return bool(resp.approved), resp.edited_args
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            logger.info("Approval %s timed out", request_id)
            return False, None

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
        if event.event_type == "imessage_received":
            return self._build_imessage_prompt(event, memories=memories)

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
        description = meta.get("description", "")
        logic = meta.get("expected_logic", "")
        return (
            "You are an on-device macOS assistant. The pattern recognizer "
            "has observed the user performing a repeatable workflow and "
            "suggested a new tool that would automate it.\n\n"
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
        if event.event_type == "user_message":
            text = event.metadata.get("text", "") or ""
            preview = text if len(text) <= 120 else text[:120] + "…"
            return f"User said: {preview}"
        return (
            f"{event.event_type}"
            + (f" in {event.app_name}" if event.app_name else "")
            + (f" — {event.window_title}" if event.window_title else "")
        )


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
