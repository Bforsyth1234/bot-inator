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
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from events.event_bus import ContextEvent, EventBus
from schemas.ws_messages import (
    ApprovalRequest,
    ApprovalRequestPayload,
    ApprovalResponsePayload,
    Thought,
    ThoughtPayload,
)

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
    ) -> None:
        self.engine = engine
        self.event_bus = event_bus
        self.memory = memory
        self.tools: list[Any] = tools or []
        self.approval_timeout = approval_timeout

        self._ws_send: Optional[WSSendCallable] = None
        self._seq: int = 0
        self._task: Optional[asyncio.Task[None]] = None
        self._pending: dict[str, asyncio.Future[ApprovalResponsePayload]] = {}
        self._agent: Any = None

    # ---- lifecycle ----------------------------------------------------

    def register_ws_send(self, send: WSSendCallable) -> None:
        """Register the coroutine used to push messages to the UI."""
        self._ws_send = send

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

        prompt = self._build_prompt(event)
        await self._emit_thought(event_id, "reasoning", f"Prompt: {prompt[:200]}")

        result = await self._run_agent(event_id, prompt)
        await self._emit_thought(event_id, "complete", str(result)[:500])

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

    async def _run_agent(self, event_id: str, prompt: str) -> Any:
        if self._agent is None:
            return await self._fallback_run(event_id, prompt)
        self._current_event_id = event_id
        return await asyncio.to_thread(self._agent.run, prompt)

    async def _fallback_run(self, event_id: str, prompt: str) -> str:
        """Run a minimal loop that invokes each tool under approval gating.

        Used when smolagents is unavailable so the approval plumbing can still
        be exercised end-to-end (e.g. for integration tests).
        """
        for tool in self.tools:
            tool_name = getattr(tool, "name", getattr(tool, "__name__", "tool"))
            approved = await self._request_approval(
                event_id=event_id,
                tool_name=tool_name,
                tool_args={},
                reasoning=f"Fallback invocation of {tool_name}",
            )
            if not approved:
                await self._emit_thought(event_id, "tool_result", f"{tool_name}: skipped")
                continue
            try:
                result = tool() if callable(tool) else None
            except Exception as exc:  # pragma: no cover - defensive
                result = f"error: {exc}"
            await self._emit_thought(event_id, "tool_result", f"{tool_name}: {result}")
        return "done"

    def _wrap_tool_for_approval(self, tool: Any) -> Any:
        """Return a copy of ``tool`` whose invocation is gated on approval."""
        orchestrator = self

        original_forward = getattr(tool, "forward", None) or getattr(tool, "__call__", None)
        tool_name = getattr(tool, "name", getattr(tool, "__name__", "tool"))

        def gated_forward(*args: Any, **kwargs: Any) -> Any:
            loop = orchestrator.event_bus.loop or asyncio.get_event_loop()
            event_id = getattr(orchestrator, "_current_event_id", "evt_unknown")
            coro = orchestrator._request_approval(
                event_id=event_id,
                tool_name=tool_name,
                tool_args={"args": list(args), "kwargs": kwargs},
                reasoning=f"Agent wants to call {tool_name}",
            )
            approved = asyncio.run_coroutine_threadsafe(coro, loop).result(
                timeout=orchestrator.approval_timeout + 5
            )
            if not approved:
                return {"skipped": True, "reason": "user_denied"}
            result = original_forward(*args, **kwargs) if original_forward else None
            asyncio.run_coroutine_threadsafe(
                orchestrator._emit_thought(event_id, "tool_result", f"{tool_name}: {result}"),
                loop,
            )
            return result

        if hasattr(tool, "forward"):
            tool.forward = gated_forward  # type: ignore[attr-defined]
            return tool
        return gated_forward

    # ---- approval + streaming ----------------------------------------

    async def _request_approval(
        self,
        event_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        reasoning: str,
    ) -> bool:
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
            return bool(resp.approved)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            logger.info("Approval %s timed out", request_id)
            return False

    async def _emit_thought(self, event_id: str, stage: str, content: str) -> None:
        msg = Thought(
            seq=self._next_seq(),
            timestamp=_now_iso(),
            payload=ThoughtPayload(event_id=event_id, stage=stage, content=content),
        )
        await self._send(msg)

    async def _send(self, message: Any) -> None:
        if self._ws_send is None:
            logger.debug("ws_send not registered; dropping %s", type(message).__name__)
            return
        try:
            await self._ws_send(message)
        except Exception:
            logger.exception("ws_send failed")

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ---- prompt building ---------------------------------------------

    def _build_prompt(self, event: ContextEvent) -> str:
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
        parts.append(
            "If an action would help, call exactly one tool. Otherwise reply "
            "'no action'."
        )
        return "\n".join(parts)

    @staticmethod
    def _describe_event(event: ContextEvent) -> str:
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

            # Stream-generate tokens
            output_tokens = 0
            text = ""
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
