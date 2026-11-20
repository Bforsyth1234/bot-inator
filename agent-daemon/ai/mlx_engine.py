"""MLX-LM engine wrapper with async generation and model swap logic."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

logger = logging.getLogger(__name__)

# How many generations between ``mx.metal.clear_cache()`` calls. MLX-LM
# on Apple Silicon accumulates Metal heap allocations across generations
# which eventually triggers sporadic segfaults; periodically flushing the
# cache keeps the heap bounded without measurably affecting throughput.
_CACHE_CLEAR_EVERY_N = int(os.environ.get("AGENT_MLX_CACHE_EVERY", "16"))

DEFAULT_MODEL = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"

EVENT_ANALYSIS_SYSTEM_PROMPT = (
    "You are an on-device macOS assistant analysing a single system event. "
    "Respond in one or two short sentences describing, in plain language, "
    "what the user appears to be doing and whether the event looks routine "
    "or noteworthy. Do not propose actions, do not call tools, do not use "
    "markdown, and do not include reasoning tags."
)

# Matches a full ``<think>…</think>`` block, or a leading ``…</think>`` whose
# opening tag the chat template put in the prompt (Qwen3.5+ thinking mode).
# Chat templates also get ``enable_thinking=False``: Qwen3.5+ templates
# think by default, which would eat these prompts' small token budgets.
# Older templates ignore the extra variable.
THINK_BLOCK_RE = re.compile(
    r"^(?:(?!<think>).)*?</think>|<think>.*?</think>", re.DOTALL
)


# #120: mlx-lm 0.32 adapter layers whose output is ``base(x) + scale * z``
# (``mlx_lm/tuner/lora.py``): ``scale = 0`` gives exactly the base output,
# so the adapter can be switched off for a turn without reloading. DoRA
# layers (``tuner/dora.py``) also rescale by a learned magnitude ``m`` and a
# full fine-tune replaces weights, so neither can be switched off this way:
# :meth:`MLXEngine.using_base` falls back to reloading for those.
LORA_LAYER_CLASSES = frozenset({"LoRALinear", "LoRASwitchLinear", "LoRAEmbedding"})
UNTOGGLEABLE_LAYER_CLASSES = frozenset({"DoRALinear", "DoRAEmbedding"})


def _walk_modules(root: Any) -> Iterator[Any]:
    """Every sub-module of ``root`` (an ``mlx.nn.Module`` tree, or any
    object tree of dicts / lists / attributes in tests), each once."""
    seen: set[int] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, (list, tuple)):
            stack.extend(node)
            continue
        if isinstance(node, dict) and type(node) is dict:
            stack.extend(node.values())
            continue
        if isinstance(node, (str, bytes, int, float, bool)):
            continue
        yield node
        children = getattr(node, "children", None)
        if callable(children):  # mlx.nn.Module
            try:
                stack.extend(children().values())
                continue
            except Exception:  # pragma: no cover - not an nn.Module after all
                pass
        if hasattr(node, "__dict__"):
            stack.extend(v for k, v in vars(node).items() if not k.startswith("_"))


def adapter_layers(model: Any) -> tuple[list[Any], list[Any]]:
    """``(toggleable, untoggleable)`` adapter layers in ``model``, by the
    mlx-lm class names above."""
    on, off = [], []
    for mod in _walk_modules(model):
        name = type(mod).__name__
        if name in LORA_LAYER_CLASSES and hasattr(mod, "scale"):
            on.append(mod)
        elif name in UNTOGGLEABLE_LAYER_CLASSES:
            off.append(mod)
    return on, off


def _clear_cache_fn(mx: Any) -> Any:
    """``mx.clear_cache`` (MLX 0.30+), else the deprecated ``mx.metal`` one."""
    clear = getattr(mx, "clear_cache", None)
    if not callable(clear):
        clear = getattr(getattr(mx, "metal", None), "clear_cache", None)
    return clear if callable(clear) else None


def local_model_bytes(model_name: str, cache_dir: Optional[Path] = None) -> Optional[int]:
    """Bytes ``model_name`` takes on disk, from this Mac only (no network).

    A local folder: the size of its files. A Hugging Face repo id: the size
    of its snapshot in the local cache (``refs/main``'s, else the only one).
    ``None`` when unknown: not a folder, not in the cache, or no
    ``*.safetensors`` weights there yet (#136).
    """
    try:
        folder = Path(model_name).expanduser()
        if folder.is_dir():
            snapshot = folder
        else:
            if cache_dir is None:
                from .egress_log import _hf_cache_dir  # noqa: WPS433
                cache_dir = _hf_cache_dir()
            repo = Path(cache_dir) / ("models--" + model_name.replace("/", "--"))
            snaps = repo / "snapshots"
            ref = repo / "refs" / "main"
            snapshot = None
            if ref.is_file():
                candidate = snaps / ref.read_text(encoding="utf-8").strip()
                if candidate.is_dir():
                    snapshot = candidate
            if snapshot is None and snaps.is_dir():
                dirs = [d for d in snaps.iterdir() if d.is_dir()]
                if len(dirs) == 1:
                    snapshot = dirs[0]
            if snapshot is None:
                return None
        total, weights, seen = 0, False, set()
        for entry in snapshot.rglob("*"):
            try:
                real = entry.resolve()
                if not real.is_file() or real in seen:
                    continue
                seen.add(real)
                total += real.stat().st_size
                weights = weights or entry.suffix == ".safetensors"
            except OSError:
                continue
        return total if weights and total > 0 else None
    except Exception:
        return None


def _first_line(exc: BaseException, limit: int = 240) -> str:
    text = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    return text if len(text) <= limit else text[: limit - 1] + "…"


class MLXEngine:
    """Async wrapper around mlx-lm for local inference on Apple Silicon.

    Keeps at most one model warm in unified memory; swap replaces the loaded
    model (unload → load) under an internal lock so generation calls are
    serialized against swaps.

    An optional LoRA ``adapter_path`` (a folder written by ``mlx_lm.lora``,
    #119/#121) is applied on top of the base model at load time; it is
    passed to ``mlx_lm.load(..., adapter_path=...)`` only when set.
    """

    # Class-level Metal serialization lock. Shared across every MLXEngine
    # instance so the main tool-calling engine (e.g. Hermes-3) and the
    # analysis engine (e.g. Qwen2.5) never encode to the GPU at the same
    # time. The Metal command-buffer coalescer aborts the process
    # (``A command encoder is already encoding to this command buffer``)
    # when two MLX generations race, even though each instance has its
    # own model weights, because they share the same physical device.
    _metal_lock: threading.Lock = threading.Lock()

    # #120: this engine can carry a LoRA adapter (routes check this, not
    # the class, so tests can use fakes).
    supports_adapters: bool = True

    def __init__(
        self, model_name: str = DEFAULT_MODEL, adapter_path: Optional[str] = None,
        fallback_model: Optional[str] = None,
    ) -> None:
        self.model_name: str = model_name
        # #136: if ``model_name`` (as configured) fails to load, load
        # ``fallback_model`` (the default for this role) instead, but only
        # when it is already on this Mac: a fallback never downloads.
        # ``load_error`` says what failed ("" once a load succeeds).
        self._configured_model: str = model_name
        self.fallback_model: Optional[str] = fallback_model
        self.load_error: str = ""
        self.adapter_path: Optional[str] = str(adapter_path) if adapter_path else None
        self._model: Any = None
        self._tokenizer: Any = None
        self._lock: asyncio.Lock = asyncio.Lock()
        # Alias the class-level Metal lock so existing callers that reach
        # for ``engine.generation_lock`` (e.g. the smolagents model
        # adapter that wraps ``mlx_lm.stream_generate``) keep working
        # while every engine shares one cross-instance serializer.
        self.generation_lock: threading.Lock = MLXEngine._metal_lock
        self._gen_count: int = 0
        self._loaded: bool = False
        # #120: saved ``scale`` per LoRA layer while the adapter is off.
        self._saved_scales: Optional[list[tuple[Any, Any]]] = None

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def current_model(self) -> Optional[str]:
        return self.model_name if self._loaded else None

    @property
    def current_adapter(self) -> Optional[str]:
        """The adapter folder applied to the loaded model, or ``None`` (base
        model only, or nothing loaded)."""
        return self.adapter_path if self._loaded else None

    @property
    def adapter_active(self) -> bool:
        """True while the loaded model's adapter is applied (loaded, an
        adapter set, and not switched off by :meth:`using_base`)."""
        return bool(self._loaded and self.adapter_path and self._saved_scales is None)

    def adapter_toggleable(self) -> bool:
        """True when the loaded adapter can be switched off in place (plain
        LoRA layers only; see :data:`LORA_LAYER_CLASSES`)."""
        if not (self._loaded and self.adapter_path):
            return False
        on, off = adapter_layers(self._model)
        return bool(on) and not off

    def _set_adapter_off_locked(self) -> int:
        """Caller holds :attr:`generation_lock`. Zero every LoRA scale and
        remember the exact old values. Returns the number of layers."""
        if self._saved_scales is not None:
            return len(self._saved_scales)
        on, _ = adapter_layers(self._model)
        saved = [(layer, layer.scale) for layer in on]
        for layer, _ in saved:
            layer.scale = 0.0
        self._saved_scales = saved
        return len(saved)

    def _set_adapter_on_locked(self) -> None:
        """Caller holds :attr:`generation_lock`. Put back the saved scales
        (the same objects, so the values are exact)."""
        saved, self._saved_scales = self._saved_scales, None
        for layer, value in saved or ():
            layer.scale = value

    def _toggle(self, off: bool) -> int:
        with self.generation_lock:  # no generation sees a half-toggled model
            if off:
                return self._set_adapter_off_locked()
            self._set_adapter_on_locked()
            return 0

    @contextlib.asynccontextmanager
    async def using_base(self) -> AsyncIterator[str]:
        """Run the body on the base model, then put the adapter back (#120
        decision 4: the 10% base share). Yields how: ``"none"`` (no adapter
        loaded, nothing to do), ``"toggle"`` (LoRA scales set to 0 under the
        generation lock and restored exactly afterwards; no reload) or
        ``"reload"`` (DoRA / full fine-tune: swapped to the base model and
        back, which reloads twice).

        Holds the engine's swap lock for the whole body, so a swap (e.g.
        "Use base model") waits until the turn is done. The body must not
        call :meth:`swap`, :meth:`load` or :meth:`unload` on this engine.
        Not re-entrant.
        """
        if not self.adapter_path:
            yield "none"
            return
        async with self._lock:
            if not self._loaded:
                await self._load_locked(self.model_name)
            on, off = adapter_layers(self._model)
            if on and not off:
                await asyncio.to_thread(self._toggle, True)
                try:
                    yield "toggle"
                finally:
                    await asyncio.to_thread(self._toggle, False)
                return
            # Fallback: reload without the adapter, then with it.
            adapter = self.adapter_path
            await self._unload_locked()
            self.adapter_path = None
            await self._load_locked(self.model_name)
            try:
                yield "reload"
            finally:
                await self._unload_locked()
                self.adapter_path = adapter
                await self._load_locked(self.model_name)

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

    async def swap(
        self, new_model_name: str, adapter_path: Optional[str] = None
    ) -> None:
        """Unload the current model and load ``new_model_name``, with
        ``adapter_path`` on top (``None``: the base model alone; an adapter
        is trained for one base model, so it is never carried over)."""
        adapter = str(adapter_path) if adapter_path else None
        async with self._lock:
            if (self._loaded and new_model_name == self.model_name
                    and adapter == self.adapter_path):
                return
            await self._unload_locked()
            self.model_name = new_model_name
            self.adapter_path = adapter
            await self._load_locked(new_model_name)

    async def generate(self, prompt: str, max_tokens: int = 512) -> str:
        """Run synchronous mlx-lm generation in a thread and return the text."""
        if not self._loaded:
            await self.load()
        return await asyncio.to_thread(self._generate_sync, prompt, max_tokens)

    def generate_sync(self, prompt: str, max_tokens: int = 512) -> str:
        """Blocking variant of :meth:`generate` for worker-thread callers.

        Meta-tools dispatched inside ``asyncio.to_thread(agent.run, …)``
        can invoke this without constructing a nested event loop. Must not
        be called from the main event-loop thread — it holds
        :attr:`generation_lock` while MLX runs, which would starve the loop.
        """
        if not self._loaded:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self.load())
            finally:
                loop.close()
        return self._generate_sync(prompt, max_tokens)

    def generate_chat_sync(
        self, system: str, user: str, max_tokens: int = 512
    ) -> str:
        """Blocking chat-templated generation for worker-thread callers.

        Wraps :meth:`generate_sync` with the tokenizer's chat template so
        instruction-tuned models (Hermes-3, Qwen2.5, etc.) see the proper
        ``system``/``user``/``assistant`` scaffold. Falls back to a plain
        ``System:/User:/Assistant:`` concatenation when the tokenizer has
        no ``apply_chat_template``.
        """
        if not self._loaded:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(self.load())
            finally:
                loop.close()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        apply = getattr(self._tokenizer, "apply_chat_template", None)
        prompt: Any
        if callable(apply):
            try:
                prompt = apply(
                    messages,
                    add_generation_prompt=True,
                    tokenize=False,
                    enable_thinking=False,
                )
            except Exception:
                logger.debug("apply_chat_template failed; using plain prompt")
                prompt = f"System: {system}\nUser: {user}\nAssistant:"
        else:
            prompt = f"System: {system}\nUser: {user}\nAssistant:"
        return self._generate_sync(prompt, max_tokens)

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
        adapter = self.adapter_path
        logger.info("Loading MLX model: %s%s", model_name,
                    f" + adapter {adapter}" if adapter else "")
        try:
            from mlx_lm import load  # type: ignore
        except ImportError as exc:  # pragma: no cover - dep not installed
            raise RuntimeError(
                "mlx-lm is not installed; cannot load model"
            ) from exc

        from . import egress_log

        def _do_load(name: str) -> tuple[Any, Any]:
            # #82: a first load downloads the weights from Hugging Face.
            with egress_log.model_download(name):
                if adapter:
                    return load(name, adapter_path=adapter)
                return load(name)

        try:
            self._model, self._tokenizer = await asyncio.to_thread(_do_load, model_name)
            if model_name == self._configured_model:
                self.load_error = ""  # a later reload of the fallback keeps it
        except Exception as exc:
            fallback = self.fallback_model
            if not (fallback and not adapter and fallback != model_name
                    and model_name == self._configured_model):
                self.load_error = f"{model_name} failed to load: {_first_line(exc)}"
                raise
            if local_model_bytes(fallback) is None:
                self.load_error = (
                    f"{model_name} failed to load: {_first_line(exc)}. The default "
                    f"{fallback} is not downloaded, so nothing was loaded."
                )
                raise
            # #136: a typo'd id must not leave the engine without a model.
            logger.exception("MLX model %s failed to load; using the default %s",
                             model_name, fallback)
            try:
                self._model, self._tokenizer = await asyncio.to_thread(_do_load, fallback)
            except Exception as fallback_exc:
                self.load_error = (
                    f"{model_name} failed to load: {_first_line(exc)}. The default "
                    f"{fallback} failed too: {_first_line(fallback_exc)}"
                )
                raise
            self.model_name = model_name = fallback
            self.load_error = (
                f"{self._configured_model} failed to load: {_first_line(exc)}. "
                f"Using the default {fallback}."
            )
        self._loaded = True
        logger.info("MLX model loaded: %s", model_name)

    async def _unload_locked(self) -> None:
        if not self._loaded:
            return
        logger.info("Unloading MLX model: %s", self.model_name)
        self._saved_scales = None
        self._model = None
        self._tokenizer = None
        self._loaded = False
        try:
            import gc
            gc.collect()
            import mlx.core as mx  # type: ignore
            clear = _clear_cache_fn(mx)
            if clear is not None:
                clear()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass

    def _generate_sync(self, prompt: str, max_tokens: int) -> str:
        from mlx_lm import generate  # type: ignore

        with self.generation_lock:
            try:
                return generate(
                    self._model,
                    self._tokenizer,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    verbose=False,
                )
            finally:
                self._maybe_clear_metal_cache_locked()

    def _evaluate_event_sync(self, event_context: str, max_tokens: int) -> str:
        from mlx_lm import generate  # type: ignore

        prompt = self._build_event_prompt(event_context)
        with self.generation_lock:
            try:
                raw = generate(
                    self._model,
                    self._tokenizer,
                    prompt=prompt,
                    max_tokens=max_tokens,
                    verbose=False,
                )
            finally:
                self._maybe_clear_metal_cache_locked()
        return self._postprocess_event_output(raw)

    def _maybe_clear_metal_cache_locked(self) -> None:
        """Flush the Metal allocator every ``_CACHE_CLEAR_EVERY_N`` calls.

        Invoked from the sync path while :attr:`generation_lock` is held,
        so it never races with another in-flight generation. Silently
        no-ops on non-Metal builds or older ``mlx.core`` releases.
        """
        self._gen_count += 1
        if self._gen_count % _CACHE_CLEAR_EVERY_N != 0:
            return
        try:
            import mlx.core as mx  # type: ignore
            clear = _clear_cache_fn(mx)
            if clear is not None:
                clear()
                logger.debug(
                    "Cleared MLX metal cache after %d generations",
                    self._gen_count,
                )
        except Exception:  # pragma: no cover - best-effort cleanup
            pass

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
                    enable_thinking=False,
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
        cleaned = THINK_BLOCK_RE.sub("", text or "").strip()
        return cleaned
