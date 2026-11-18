"""Use the personal model only if it beats the base model (#120).

Three parts:

* **Registry** (:func:`list_adapters`). Every ``~/.bot-inator/models/<n>/``
  folder #119 finished (``meta.json`` status ``done``) with its gate result
  (``gate.json``: the verdict, :func:`ai.model_compare.compare_gate`'s
  ``numbers`` and a timestamp, never raw eval results or any text from the
  user's rows) and two flags: ``stale`` when the adapter was trained on
  another base model than the current one (decision 3) and ``usable``
  (done, gated ``adapter_wins``, not stale).
* **Gate** (:func:`run_gate`). Personal (held-out), ``tool_calls``,
  ``idea_judge`` and ``safety`` on the base model, then the same with the
  adapter, on one MLX engine (one model in memory at a time, unloaded at
  the end). Nothing is saved to ``evals/results/``.
* **Active adapter** (:class:`PersonalModel`, ``app.state.personal_model``).
  The setting ``AGENT_PERSONAL_ADAPTER`` holds the folder in use ("" = the
  base model). :meth:`PersonalModel.use_adapter` works only for a usable
  adapter and only when the main engine is the local MLX engine on that
  adapter's base model; :meth:`PersonalModel.use_base` always works.
  :meth:`PersonalModel.apply_active` makes the engine match the setting.

What this module never does (LIVING_SPEC §1 "Personal model" 1): approve,
skip or shorten a card. Switching TO the personal model is a side effect,
so the runner raises :func:`switch_offer`'s text as a normal approval card
(never always-allowed, counted down or answerable from the phone); going
back to the base model needs no card (it is the safe direction).
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

logger = logging.getLogger(__name__)

ACTIVE_KEY = "AGENT_PERSONAL_ADAPTER"
GATE_FILE = "gate.json"
META_FILE = "meta.json"
WEIGHTS_FILE = "adapters.safetensors"
REGRESSION_SUITES = ("tool_calls", "idea_judge")
SAFETY_SUITE = "safety"

CLOUD_REASON = ("Your model works with the local engine. Switch the main "
                "engine to Local in Settings to use it.")
WRONG_BASE_REASON = ("Your model was trained on {base}; the local engine runs "
                     "{model}. Retrain it on the current model.")
STALE_REASON = "Trained on an older base model ({base}). Retrain to use it."
NOT_GATED_REASON = "Not checked against the original yet."
NOT_DONE_REASON = "Training hasn't finished."

PathLike = Union[str, Path]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def default_models_dir() -> Path:
    from ai.personal_training import default_models_dir as _d

    return _d()


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_meta(folder: PathLike) -> Optional[dict[str, Any]]:
    return _read_json(Path(folder) / META_FILE)


def read_gate(folder: PathLike) -> Optional[dict[str, Any]]:
    return _read_json(Path(folder) / GATE_FILE)


def gate_record(gate: Mapping[str, Any], *, at: float) -> dict[str, Any]:
    """What ``gate.json`` holds: only the verdict, ``compare_gate``'s
    ``numbers`` and the time (#121: never raw results, case runs, model
    output or row text)."""
    return {"verdict": str(gate.get("verdict") or ""),
            "numbers": json.loads(json.dumps(gate.get("numbers") or {})),
            "gated_at": float(at)}


def write_gate(folder: PathLike, gate: Mapping[str, Any], *, at: float) -> dict[str, Any]:
    folder = Path(folder)
    record = gate_record(gate, at=at)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".gate-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1)
        os.replace(tmp, folder / GATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return record


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _entry(folder: Path, meta: Mapping[str, Any], current_base: Optional[str]) -> dict[str, Any]:
    gate = read_gate(folder) or {}
    verdict = gate.get("verdict") or None
    numbers = gate.get("numbers") or {}
    base = str(meta.get("base_model") or "")
    stale = bool(current_base) and base != current_base
    has_weights = (folder / WEIGHTS_FILE).is_file()
    usable = verdict == "adapter_wins" and not stale and has_weights
    if stale:
        reason = STALE_REASON.format(base=base or "unknown")
    elif not verdict:
        reason = NOT_GATED_REASON
    elif not has_weights:
        reason = "The adapter file is missing."
    elif verdict != "adapter_wins":
        reason = outcome_text(verdict)
    else:
        reason = None
    return {
        "n": int(folder.name),
        "path": str(folder),
        "created_at": meta.get("created_at"),
        "finished_at": meta.get("finished_at"),
        "base_model": base,
        "rows": meta.get("usable_rows", meta.get("train_rows")),
        "final_loss": meta.get("final_loss"),
        "verdict": verdict,
        "gated_at": gate.get("gated_at"),
        "card_lines": list((numbers.get("card") or {}).get("lines") or []),
        "stale": stale,
        "usable": usable,
        "reason": reason,
    }


def list_adapters(models_dir: Optional[PathLike] = None,
                  current_base: Optional[str] = None) -> list[dict[str, Any]]:
    """Finished adapters, newest first. ``current_base`` is the base model
    the app runs now (``None``: staleness isn't checked)."""
    root = Path(models_dir) if models_dir is not None else default_models_dir()
    if not root.is_dir():
        return []
    out = []
    for folder in root.iterdir():
        if not (folder.is_dir() and folder.name.isdigit()):
            continue
        meta = read_meta(folder)
        if not meta or meta.get("status") != "done":
            continue
        out.append(_entry(folder, meta, current_base))
    out.sort(key=lambda e: e["n"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Card text (#121 decision 7)
# ---------------------------------------------------------------------------


OUTCOME_TEXT = {
    "not_enough_difference": "Not enough difference to tell yet. Keeping the original model.",
    "adapter_worse": "Your model did worse than the original on your past decisions. Keeping the original.",
    "blocked_safety": "Your model failed safety checks the original passes. Keeping the original.",
    "blocked_regression": "Your model got worse at general tool use. Keeping the original.",
}


def outcome_text(verdict: Optional[str]) -> str:
    if verdict == "adapter_wins":
        return "Your model did better than the original on your past decisions."
    return OUTCOME_TEXT.get(str(verdict), "The check didn't finish. Keeping the original model.")


def switch_offer(gate: Mapping[str, Any]) -> tuple[Optional[str], list[str]]:
    """``(summary, lines)`` for the switch card when the adapter won
    (``summary`` is the card's one-line title; buttons are [Use my model]
    [Keep the original]). Any other verdict: ``(None, lines)`` where
    ``lines`` is the notice text (no card: nothing to approve)."""
    verdict = gate.get("verdict")
    lines = list(((gate.get("numbers") or {}).get("card") or {}).get("lines") or [])
    if verdict == "adapter_wins":
        return "Use your personal model?", lines
    return None, [outcome_text(verdict), *lines]


SWITCH_BUTTONS = ("Use my model", "Keep the original")


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def _default_engine_factory(model: str, adapter: Optional[str] = None) -> Any:
    from ai.mlx_engine import MLXEngine

    return MLXEngine(model, adapter_path=adapter)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _run_suites(engine: Any, *, corpus: Any, settings: Any, repeats: int,
                      evaluate: Callable[..., Awaitable[dict]],
                      evaluate_personal: Callable[..., Awaitable[dict]]) -> dict[str, Any]:
    personal = await evaluate_personal(corpus, engine=engine, repeats=repeats, settings=settings)
    regression = {}
    for suite in REGRESSION_SUITES:
        regression[suite] = await evaluate(suite, engine=engine, save=False,
                                           repeats=repeats, settings=settings)
    safety = await evaluate(SAFETY_SUITE, engine=engine, save=False, repeats=repeats,
                            settings=settings)
    return {"personal": personal, "regression": regression, "safety": safety}


async def run_gate(
    adapter_dir: PathLike,
    *,
    corpus: Any,
    base_model: str,
    settings: Any = None,
    engine_factory: Optional[Callable[..., Any]] = None,
    evaluate: Optional[Callable[..., Awaitable[dict]]] = None,
    evaluate_personal: Optional[Callable[..., Awaitable[dict]]] = None,
    compare: Optional[Callable[..., dict]] = None,
    repeats: int = 3,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Score the base model, then the adapter, and write ``gate.json``.

    One engine (``engine_factory(base_model)``, a fresh local MLX engine by
    default) runs the base suites, is swapped to ``base_model`` +
    ``adapter_dir`` for the adapter suites and is unloaded at the end, also
    on error, so at most one gate model is in memory. Every suite runs with
    ``save=False``. Returns ``{"verdict", "numbers"}``; raises if a suite
    run fails as a whole (nothing is written then).
    """
    adapter_dir = Path(adapter_dir)
    if evaluate is None:
        from evals.run import evaluate as evaluate  # noqa: PLW0127
    if evaluate_personal is None:
        from evals.suites.personal import evaluate_personal as evaluate_personal  # noqa: PLW0127
    if compare is None:
        from ai.model_compare import compare_gate as compare  # noqa: PLW0127
    engine = (engine_factory or _default_engine_factory)(base_model)
    kw = dict(corpus=corpus, settings=settings, repeats=repeats,
              evaluate=evaluate, evaluate_personal=evaluate_personal)
    try:
        base = await _run_suites(engine, **kw)
        await _maybe_await(engine.swap(base_model, str(adapter_dir)))
        adapter = await _run_suites(engine, **kw)
    finally:
        unload = getattr(engine, "unload", None)
        if callable(unload):
            await _maybe_await(unload())
    gate = compare(base["personal"], adapter["personal"], base["regression"],
                   adapter["regression"], base["safety"], adapter["safety"])
    record = write_gate(adapter_dir, gate, at=clock())
    return {"verdict": record["verdict"], "numbers": record["numbers"]}


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


def _is_adapter_engine(engine: Any) -> bool:
    return bool(engine is not None and getattr(engine, "supports_adapters", False)
                and callable(getattr(engine, "swap", None)))


class PersonalModel:
    """The adapters, the active one and the main engine
    (``app.state.personal_model``).

    ``engine``: the daemon's main engine (may be a cloud engine: then
    nothing is activated). ``base_model``: returns the base model adapters
    must match (main.py: the main engine's model when it is MLX, else the
    MLX default). ``write_settings``: persists ``{ACTIVE_KEY: value}``
    (``user_settings.write``). ``comparison``: an
    :class:`ai.model_compare.LiveComparison` for the daily switch-back.
    """

    def __init__(
        self,
        models_dir: Optional[PathLike] = None,
        engine: Any = None,
        *,
        base_model: Optional[Callable[[], str]] = None,
        corpus: Any = None,
        settings: Any = None,
        comparison: Any = None,
        write_settings: Optional[Callable[[dict[str, str]], Any]] = None,
        environ: Optional[dict[str, str]] = None,
        gate: Optional[Callable[..., Awaitable[dict]]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.models_dir = Path(models_dir) if models_dir is not None else default_models_dir()
        self.engine = engine
        self._base_model = base_model
        self.corpus = corpus
        self.settings = settings
        self.comparison = comparison
        if write_settings is None:
            import user_settings

            write_settings = user_settings.write
        self._write_settings = write_settings
        self.environ = environ if environ is not None else os.environ
        self._gate = gate or run_gate
        self.clock = clock

    # ---- state -------------------------------------------------------------

    def current_base(self) -> str:
        if self._base_model is not None:
            return self._base_model() or ""
        if _is_adapter_engine(self.engine):
            return str(getattr(self.engine, "model_name", "") or "")
        from ai.mlx_engine import DEFAULT_MODEL

        return DEFAULT_MODEL

    def adapters(self) -> list[dict[str, Any]]:
        return list_adapters(self.models_dir, self.current_base())

    def get(self, n: int) -> Optional[dict[str, Any]]:
        return next((e for e in self.adapters() if e["n"] == int(n)), None)

    def active_path(self) -> str:
        return (self.environ.get(ACTIVE_KEY) or "").strip()

    def _entry_for_path(self, path: str) -> Optional[dict[str, Any]]:
        if not path:
            return None
        want = Path(path).expanduser().resolve()
        for e in self.adapters():
            if Path(e["path"]).resolve() == want:
                return e
        return None

    def engine_status(self, entry: Optional[Mapping[str, Any]] = None) -> tuple[bool, Optional[str]]:
        """Can the main engine carry an adapter (for ``entry``, if given)?"""
        if not _is_adapter_engine(self.engine):
            return False, CLOUD_REASON
        model = str(getattr(self.engine, "model_name", "") or "")
        base = str((entry or {}).get("base_model") or self.current_base())
        if base and model != base:
            return False, WRONG_BASE_REASON.format(base=base, model=model)
        return True, None

    @property
    def adapter_active(self) -> bool:
        """True when the main engine is running with an adapter now."""
        eng = self.engine
        if not _is_adapter_engine(eng):
            return False
        active = getattr(eng, "adapter_active", None)
        if isinstance(active, bool):
            return active
        return bool(getattr(eng, "current_adapter", None))

    def status(self) -> dict[str, Any]:
        adapters = self.adapters()
        path = self.active_path()
        active = self._entry_for_path(path)
        ok, reason = self.engine_status(active)
        loaded = getattr(self.engine, "current_adapter", None) if _is_adapter_engine(self.engine) else None
        return {
            "adapters": adapters,
            "active": active["n"] if active else None,
            "active_path": path or None,
            "loaded_adapter": loaded,
            "engine_ok": ok,
            "reason": reason,
            "current_base": self.current_base(),
        }

    # ---- switching ---------------------------------------------------------

    def _persist(self, value: str) -> None:
        self._write_settings({ACTIVE_KEY: value})
        if value:
            self.environ[ACTIVE_KEY] = value
        else:
            self.environ.pop(ACTIVE_KEY, None)

    async def use_adapter(self, n: int) -> dict[str, Any]:
        """Switch to adapter ``n``. Raises ``KeyError`` (no such finished
        adapter), ``PermissionError`` (not usable: not gated as a win, or
        stale) or ``RuntimeError`` (the main engine can't carry it, e.g. a
        cloud engine: nothing changes). The caller has the user's consent
        (the switch card or the Settings button)."""
        entry = self.get(n)
        if entry is None:
            raise KeyError(n)
        if not entry["usable"]:
            raise PermissionError(entry["reason"] or "This model can't be used.")
        ok, reason = self.engine_status(entry)
        if not ok:
            raise RuntimeError(reason)
        self._persist(entry["path"])
        await self.apply_active()
        return self.status()

    async def use_base(self) -> dict[str, Any]:
        """Back to the base model (always allowed; no card needed)."""
        self._persist("")
        await self.apply_active()
        return self.status()

    async def apply_active(self, engine: Any = None) -> dict[str, Any]:
        """Make the main engine match the setting (startup and after every
        change). An active setting that is no longer usable (stale, gate
        file gone, folder deleted) is cleared. Returns ``{"adapter":
        path | None, "applied": bool, "reason"}``."""
        if engine is not None:
            self.engine = engine
        eng = self.engine
        path = self.active_path()
        entry = self._entry_for_path(path)
        reason: Optional[str] = None
        if path and (entry is None or not entry["usable"]):
            reason = (entry or {}).get("reason") or "That model is gone. Using the original."
            logger.info("Personal model %s is not usable (%s): back to the base model", path, reason)
            self._persist("")
            entry = None
        if not _is_adapter_engine(eng):
            return {"adapter": None, "applied": False, "reason": reason or (CLOUD_REASON if entry else None)}
        model = str(getattr(eng, "model_name", "") or "")
        if entry is not None:
            ok, why = self.engine_status(entry)
            if not ok:
                entry, reason = None, why
        want = entry["path"] if entry else None
        have = getattr(eng, "adapter_path", None)
        if (want or None) != (have or None):
            await _maybe_await(eng.swap(model, want))
        return {"adapter": want, "applied": want is not None, "reason": reason}

    # ---- the base share (decision 4) ---------------------------------------

    def pick_source(self) -> str:
        """``"base"`` ~10% of offer turns while an adapter is active (via
        the live comparison), else ``"adapter"``; ``"base"`` with none."""
        active = self.adapter_active
        if self.comparison is not None:
            return self.comparison.pick_source(active)
        return "adapter" if active else "base"

    def using_base(self) -> Any:
        """Async context manager: run the body on the base model
        (:meth:`ai.mlx_engine.MLXEngine.using_base`). A no-op for an engine
        without adapters."""
        eng = self.engine
        cm = getattr(eng, "using_base", None) if _is_adapter_engine(eng) else None
        if cm is None:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _noop() -> Any:
                yield "none"

            return _noop()
        return cm()

    # ---- after training / daily ---------------------------------------------

    async def gate(self, adapter_dir: PathLike, **kw: Any) -> dict[str, Any]:
        """Run the #121 gate on a finished adapter. On failure returns
        ``{"verdict": "error", "error": ...}`` and writes nothing, so the
        adapter stays unusable."""
        meta = read_meta(adapter_dir) or {}
        base = str(meta.get("base_model") or self.current_base())
        try:
            return await self._gate(adapter_dir, corpus=self.corpus, base_model=base,
                                    settings=self.settings, clock=self.clock, **kw)
        except Exception as exc:  # noqa: BLE001 - reported, adapter stays unusable
            logger.exception("Personal model gate failed for %s", adapter_dir)
            return {"verdict": "error", "error": f"{type(exc).__name__}: {exc}", "numbers": {}}

    async def check_switch_back(self) -> Optional[str]:
        """Daily: while an adapter is in use, go back to the base model if
        the live comparison says so (no card: the safe direction). Returns
        the notice text when it switched back, else ``None``."""
        if not self.active_path() or self.comparison is None:
            return None
        if not self.comparison.should_switch_back():
            return None
        text = self.comparison.notice_text()
        await self.use_base()
        return text
