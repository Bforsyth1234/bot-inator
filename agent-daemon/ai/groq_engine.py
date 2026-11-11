"""OpenAI-compatible HTTP engine (#97). Mirrors :class:`MLXEngine`'s surface.

One class serves every cloud or local-server preset in :mod:`providers`
(Groq, OpenAI, Anthropic, Gemini, xAI, OpenRouter, Ollama / LM Studio,
custom). The class keeps its old name ``GroqEngine`` (existing imports,
tests and the egress-ledger hook points use it) with
``OpenAICompatibleEngine`` as an alias; the module keeps its old name too.

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

from .mlx_engine import EVENT_ANALYSIS_SYSTEM_PROMPT, THINK_BLOCK_RE

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_API_BASE = "https://api.groq.com/openai/v1"


class GroqEngine:
    """Async wrapper around an OpenAI-compatible chat completions API.

    Named ``GroqEngine`` for history (the egress-ledger hook points and the
    wave-15 wiring refer to ``GroqEngine._chat_completion``); new code uses
    the ``OpenAICompatibleEngine`` alias below.

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
        role: Optional[str] = None,
        provider: str = "groq",
        label: str = "Groq",
        key_env: str = "GROQ_API_KEY",
        key_required: bool = True,
        token_param: str = "max_tokens",
    ) -> None:
        self.model_name: str = model_name
        # #82: which engine this is (main / analysis / drafting / detection),
        # for the privacy ledger's rows.
        self.role: Optional[str] = role
        self.api_key: Optional[str] = api_key
        self.api_base: str = api_base
        self.provider: str = provider
        self.label: str = label
        self.key_env: str = key_env
        self.key_required: bool = key_required
        # ``max_completion_tokens`` for OpenAI's reasoning models (#97).
        self.token_param: str = token_param
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
            logger.exception("evaluate_event (%s) failed", self.provider)
            return ""
        return THINK_BLOCK_RE.sub("", raw or "").strip()

    # ---- internal helpers --------------------------------------------

    async def _load_locked(self) -> None:
        if self._loaded:
            return
        if self.key_required and not self.api_key:
            raise RuntimeError(
                f"{self.label} provider selected but {self.key_env} is not set"
            )
        if not self.model_name:
            raise RuntimeError(
                f"{self.label} provider selected but no model name is set"
            )
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:  # pragma: no cover - dep not installed
            raise RuntimeError(
                f"openai package is not installed; cannot use {self.label}"
            ) from exc
        with GroqEngine._client_lock:
            # The OpenAI client refuses an empty key; a local server
            # (Ollama, LM Studio) ignores whatever is sent.
            self._client = OpenAI(
                api_key=self.api_key or "not-needed", base_url=self.api_base
            )
        self._loaded = True
        logger.info(
            "%s engine ready: model=%s base=%s",
            self.label, self.model_name, self.api_base,
        )

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
            raise RuntimeError(
                f"{self.label} client not initialised; call load() first"
            )
        from . import egress_log

        # #82: one privacy-ledger row per call (what was sent, to where).
        request = {"model": self.model_name, "messages": messages, self.token_param: max_tokens}
        with self.generation_lock:
            try:
                resp = client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    timeout=self.request_timeout,
                    **{self.token_param: max_tokens},
                )
            except Exception as exc:
                egress_log.record_completion(
                    role=self.role, model=self.model_name, api_base=self.api_base,
                    request=request, error=exc,
                )
                raise
        egress_log.record_completion(
            role=self.role, model=self.model_name, api_base=self.api_base,
            request=request, response=resp,
        )
        try:
            return resp.choices[0].message.content or ""
        except (AttributeError, IndexError):
            logger.warning("%s response missing content: %r", self.label, resp)
            return ""


# The name new code uses (#97); same class.
OpenAICompatibleEngine = GroqEngine


def build_engine(settings: Any, provider: str, role: str) -> OpenAICompatibleEngine:
    """The engine for ``role`` on preset ``provider`` (#97)."""
    import providers as _providers  # noqa: WPS433 (top-level daemon module)

    preset = _providers.PRESETS[provider]
    return OpenAICompatibleEngine(
        settings.provider_model(provider, role),
        api_key=settings.provider_api_key(provider),
        api_base=settings.provider_api_base(provider),
        provider=provider,
        label=preset.label,
        key_env=preset.key_env(),
        key_required=preset.key_required,
        token_param=preset.token_param,
        role=role,
    )
