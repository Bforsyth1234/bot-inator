"""MLX-LM engine wrapper with async generation and model swap logic."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"

EVENT_ANALYSIS_SYSTEM_PROMPT = (
    "You are an on-device macOS assistant analysing a single system event. "
    "Respond in one or two short sentences describing, in plain language, "
    "what the user appears to be doing and whether the event looks routine "
    "or noteworthy. Do not propose actions, do not call tools, do not use "
    "markdown, and do not include reasoning tags."
)

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class MLXEngine:
    """Async wrapper around mlx-lm for local inference on Apple Silicon.

    Keeps at most one model warm in unified memory; swap replaces the loaded
    model (unload → load) under an internal lock so generation calls are
    serialized against swaps.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name: str = model_name
        self._model: Any = None
        self._tokenizer: Any = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._loaded: bool = False

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def current_model(self) -> Optional[str]:
        return self.model_name if self._loaded else None

    @property
    def model(self) -> Any:
        """Return the raw mlx-lm model (or ``None`` if not loaded)."""
        return self._model

    @property
    def tokenizer(self) -> Any:
        """Return the raw mlx-lm tokenizer (or ``None`` if not loaded)."""
        return self._tokenizer

    async def load(self) -> None:
        """Load the configured model into memory."""
        async with self._lock:
            await self._load_locked(self.model_name)

    async def unload(self) -> None:
        """Release the currently loaded model."""
        async with self._lock:
            await self._unload_locked()

    async def swap(self, new_model_name: str) -> None:
        """Unload the current model and load ``new_model_name``."""
        async with self._lock:
            if self._loaded and new_model_name == self.model_name:
                return
            await self._unload_locked()
            self.model_name = new_model_name
            await self._load_locked(new_model_name)

    async def generate(self, prompt: str, max_tokens: int = 512) -> str:
        """Run synchronous mlx-lm generation in a thread and return the text."""
        if not self._loaded:
            await self.load()
        return await asyncio.to_thread(self._generate_sync, prompt, max_tokens)

    async def evaluate_event(
        self, event_context: str, max_tokens: int = 128
    ) -> str:
        """Run a single-shot plain-text analysis of a macOS event.

        Runs alongside (not in place of) the orchestrator's tool-calling
        loop. Callers can use this to produce a short human-readable
        summary of an event — e.g. to emit as a ``thought`` frame —
        without going through the agent's tool-selection pipeline.

        Args:
            event_context: Free-form description of the event
                (e.g. ``"User switched to Safari: Jira - BUG-123"``).
            max_tokens: Generation cap for the reply.

        Returns:
            A one-to-two sentence plain-text analysis, or an empty
            string if generation failed.
        """
        if not self._loaded:
            await self.load()
        try:
            return await asyncio.to_thread(
                self._evaluate_event_sync, event_context, max_tokens
            )
        except Exception:
            logger.exception("evaluate_event failed")
            return ""

    # ---- internal helpers (assume lock is held where noted) ----

    async def _load_locked(self, model_name: str) -> None:
        if self._loaded:
            return
        logger.info("Loading MLX model: %s", model_name)
        try:
            from mlx_lm import load  # type: ignore
        except ImportError as exc:  # pragma: no cover - dep not installed
            raise RuntimeError(
                "mlx-lm is not installed; cannot load model"
            ) from exc

        def _do_load() -> tuple[Any, Any]:
            return load(model_name)

        self._model, self._tokenizer = await asyncio.to_thread(_do_load)
        self._loaded = True
        logger.info("MLX model loaded: %s", model_name)

    async def _unload_locked(self) -> None:
        if not self._loaded:
            return
        logger.info("Unloading MLX model: %s", self.model_name)
        self._model = None
        self._tokenizer = None
        self._loaded = False
        try:
            import gc
            gc.collect()
            import mlx.core as mx  # type: ignore
            if hasattr(mx, "metal") and hasattr(mx.metal, "clear_cache"):
                mx.metal.clear_cache()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass

    def _generate_sync(self, prompt: str, max_tokens: int) -> str:
        from mlx_lm import generate  # type: ignore

        return generate(
            self._model,
            self._tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
            verbose=False,
        )

    def _evaluate_event_sync(self, event_context: str, max_tokens: int) -> str:
        from mlx_lm import generate  # type: ignore

        prompt = self._build_event_prompt(event_context)
        raw = generate(
            self._model,
            self._tokenizer,
            prompt=prompt,
            max_tokens=max_tokens,
            verbose=False,
        )
        return self._postprocess_event_output(raw)

    def _build_event_prompt(self, event_context: str) -> Any:
        """Render the chat-templated prompt for event analysis.

        Falls back to a plain concatenated prompt if the tokenizer does
        not expose ``apply_chat_template``.
        """
        messages = [
            {"role": "system", "content": EVENT_ANALYSIS_SYSTEM_PROMPT},
            {"role": "user", "content": event_context},
        ]
        apply = getattr(self._tokenizer, "apply_chat_template", None)
        if callable(apply):
            try:
                return apply(
                    messages,
                    add_generation_prompt=True,
                    tokenize=False,
                )
            except Exception:  # pragma: no cover - tokenizer quirks
                logger.debug("apply_chat_template failed; using plain prompt")
        return (
            f"System: {EVENT_ANALYSIS_SYSTEM_PROMPT}\n"
            f"User: {event_context}\n"
            "Assistant:"
        )

    @staticmethod
    def _postprocess_event_output(text: str) -> str:
        cleaned = _THINK_BLOCK_RE.sub("", text or "").strip()
        return cleaned
