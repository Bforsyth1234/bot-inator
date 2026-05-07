"""Groq HTTP engine wrapper. Mirrors :class:`MLXEngine`'s public surface.

The orchestrator and meta-tool generator are written against a duck-typed
engine interface (``generate``, ``generate_sync``, ``generate_chat_sync``,
``evaluate_event``, ``load``/``unload``/``swap``, ``loaded`` /
``current_model`` / ``model_name``). Selecting Groq as the provider just
swaps in instances of this class without touching the rest of the daemon.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from .mlx_engine import EVENT_ANALYSIS_SYSTEM_PROMPT, _THINK_BLOCK_RE

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_API_BASE = "https://api.groq.com/openai/v1"


class GroqEngine:
    """Async wrapper around Groq's OpenAI-compatible chat completions API.

    There is no model to "load" — every call is a network round-trip — so
    :meth:`load`/:meth:`unload`/:meth:`swap` are cheap bookkeeping
    operations preserved for API compatibility with :class:`MLXEngine`.
    A class-level :class:`threading.Lock` is exposed as
    :attr:`generation_lock` for parity with the MLX adapter's blocking
    callers, but it does **not** serialise across instances since Groq
    handles concurrency server-side.
    """

    _client_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        api_key: Optional[str] = None,
        api_base: str = DEFAULT_API_BASE,
        request_timeout: float = 60.0,
    ) -> None:
        self.model_name: str = model_name
        self.api_key: Optional[str] = api_key
        self.api_base: str = api_base
        self.request_timeout: float = request_timeout
        self._client: Any = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self.generation_lock: threading.Lock = threading.Lock()
        self._loaded: bool = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def current_model(self) -> Optional[str]:
        return self.model_name if self._loaded else None

    @property
    def model(self) -> Any:
        return None

    @property
    def tokenizer(self) -> Any:
        return None

    async def load(self) -> None:
        """Lazily build the OpenAI client. Validates an API key is present."""
        async with self._lock:
            await self._load_locked()

    async def unload(self) -> None:
        async with self._lock:
            self._client = None
            self._loaded = False

    async def swap(self, new_model_name: str) -> None:
        async with self._lock:
            self.model_name = new_model_name
            self._loaded = bool(self._client)

    async def generate(self, prompt: str, max_tokens: int = 512) -> str:
        if not self._loaded:
            await self.load()
        return await asyncio.to_thread(self._generate_sync, prompt, max_tokens)

    def generate_sync(self, prompt: str, max_tokens: int = 512) -> str:
        if not self._loaded:
            self._ensure_loaded_blocking()
        return self._generate_sync(prompt, max_tokens)

    def generate_chat_sync(
        self, system: str, user: str, max_tokens: int = 512
    ) -> str:
        if not self._loaded:
            self._ensure_loaded_blocking()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._chat_completion(messages, max_tokens)

    async def evaluate_event(
        self, event_context: str, max_tokens: int = 128
    ) -> str:
        if not self._loaded:
            await self.load()
        try:
            raw = await asyncio.to_thread(
                self.generate_chat_sync,
                EVENT_ANALYSIS_SYSTEM_PROMPT,
                event_context,
                max_tokens,
            )
        except Exception:
            logger.exception("evaluate_event (groq) failed")
            return ""
        return _THINK_BLOCK_RE.sub("", raw or "").strip()

    # ---- internal helpers --------------------------------------------

    async def _load_locked(self) -> None:
        if self._loaded:
            return
        if not self.api_key:
            raise RuntimeError(
                "Groq provider selected but GROQ_API_KEY is not set"
            )
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover - dep not installed
            raise RuntimeError(
                "openai package is not installed; cannot use Groq provider"
            ) from exc
        with GroqEngine._client_lock:
            self._client = OpenAI(api_key=self.api_key, base_url=self.api_base)
        self._loaded = True
        logger.info("Groq engine ready: model=%s base=%s", self.model_name, self.api_base)

    def _ensure_loaded_blocking(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.load())
        finally:
            loop.close()

    def _generate_sync(self, prompt: str, max_tokens: int) -> str:
        return self._chat_completion(
            [{"role": "user", "content": prompt}], max_tokens
        )

    def _chat_completion(
        self, messages: list[dict[str, str]], max_tokens: int
    ) -> str:
        client = self._client
        if client is None:
            raise RuntimeError("Groq client not initialised; call load() first")
        with self.generation_lock:
            resp = client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                max_tokens=max_tokens,
                timeout=self.request_timeout,
            )
        try:
            return resp.choices[0].message.content or ""
        except (AttributeError, IndexError):
            logger.warning("Groq response missing content: %r", resp)
            return ""
