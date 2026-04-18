"""MLX-LM engine wrapper with async generation and model swap logic."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "mlx-community/Qwen2.5-Coder-7B-4bit"


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
