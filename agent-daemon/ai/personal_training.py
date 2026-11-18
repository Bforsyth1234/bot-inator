"""Offer to train a personal model once there's enough data (#119).

Three parts, all local:

* **Threshold** (:func:`offer_due`). The offer is due once the #118 corpus
  holds :data:`MIN_ROWS` (300) untainted usable rows with at least
  :data:`MIN_NEGATIVE` (50) denials or undos (env ``AGENT_TRAIN_MIN_ROWS``
  / ``AGENT_TRAIN_MIN_NEGATIVE`` override). Not due while a run is going or
  an accepted offer waits to start, while an offer card is pending, after a
  decline until the corpus has doubled since the decline, and after a run
  until the corpus has doubled since that run (so "no adapter trained on
  this many rows" never re-offers after one new row).
  The small state lives in ``~/.bot-inator/personal_training.json`` (0600,
  atomic writes, :class:`TrainingState`).

* **The card** (:func:`offer_card`). Training is a side effect, so it is a
  normal approval card (invariant 7a) raised by the orchestrator; this
  module only supplies the text and records the answer
  (:meth:`TrainingState.record_accept` / :meth:`record_decline`). The
  adapter never touches consent (LIVING_SPEC §1 "Personal model"): nothing
  here approves anything.

* **The run** (:class:`TrainingRunner`). Builds ``train.jsonl`` /
  ``valid.jsonl`` from :meth:`TrainingCorpus.training_rows` after
  :meth:`freeze_split` (held-out rows never reach train/valid), then
  launches ``python -m mlx_lm lora --train`` in a CHILD process writing to
  ``~/.bot-inator/models/<n>/``. It starts only on AC power and after
  :data:`IDLE_START_SECONDS` (180 s) without keyboard/mouse input. When the
  user comes back (idle < :data:`IDLE_RESUME_SECONDS`, 30 s) or the Mac goes
  on battery, the child is PAUSED with SIGSTOP and resumed with SIGCONT
  once idle and on AC again (chosen over terminate-and-resume: no progress
  is lost and mlx_lm needs no checkpoint; the child's memory stays held
  while paused). Cancel kills the child and removes the partial adapter
  folder. One run at a time.

Dataset: approved and edited rows are targets; the target is the final
card (``final_args`` for an edited row) written as the ``<tool_call>``
block the orchestrator parses. Denied and undone rows are NOT targets
(SFT on them would teach the model to make the cards the user rejected);
#121's personal suite uses them for offer-accuracy scoring. Rows whose
prompt was stripped (#118 mail toggle off) or is empty are left out: a
target with no input teaches nothing. The jsonl files hold user text, so
the ``data/`` folder is deleted as soon as the child ends.

Memory: if a LoRA run on the base model does not fit next to the warm
model, pass ``before_start`` / ``after_end`` hooks (e.g. ``engine.unload``
/ ``engine.load``); :func:`offer_card` then says the assistant pauses.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence, Union

logger = logging.getLogger(__name__)

DATA_DIR = Path.home() / ".bot-inator"

MIN_ROWS = 300
MIN_NEGATIVE = 50
ENV_MIN_ROWS = "AGENT_TRAIN_MIN_ROWS"
ENV_MIN_NEGATIVE = "AGENT_TRAIN_MIN_NEGATIVE"

IDLE_START_SECONDS = 180.0
IDLE_RESUME_SECONDS = 30.0
POLL_SECONDS = 5.0
OFFER_PENDING_TTL = 24 * 3600.0

TRAIN_TOOL_NAME = "train_personal_model"

# Training shape. A few epochs over a few hundred short examples.
BATCH_SIZE = 4
EPOCHS = 3
MIN_ITERS = 50
MAX_ITERS = 1000
NUM_LAYERS = 8
MAX_SEQ_LENGTH = 2048
STEPS_PER_REPORT = 5
VALID_FRACTION = 0.1
# Rough guess for the card ("About N min"); replaced by the measured rate
# once iterations come in. Env AGENT_TRAIN_SECONDS_PER_ITER tunes it.
SECONDS_PER_ITER = 3.0

STATES = ("idle", "waiting_for_idle", "running", "paused", "done", "failed", "cancelled")
ACTIVE_STATES = ("waiting_for_idle", "running", "paused")

SYSTEM_PROMPT = (
    "You are a local macOS assistant. Decide whether an action would help the "
    "user and, if so, propose exactly one tool call as a <tool_call> JSON block."
)

Clock = Callable[[], float]
ProgressCallback = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]
Hook = Callable[[], Union[None, Awaitable[None]]]
CommandFactory = Callable[[str, Path, Path, int], Sequence[str]]


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "") or default)
    except ValueError:
        return default


def min_rows() -> int:
    return _env_int(ENV_MIN_ROWS, MIN_ROWS)


def min_negative() -> int:
    return _env_int(ENV_MIN_NEGATIVE, MIN_NEGATIVE)


def default_state_path() -> Path:
    return DATA_DIR / "personal_training.json"


def default_models_dir() -> Path:
    return DATA_DIR / "models"


# ---------------------------------------------------------------------------
# Guards: power and idle
# ---------------------------------------------------------------------------


def parse_pmset(text: str) -> Optional[str]:
    """``pmset -g batt`` → ``"ac"`` / ``"battery"`` / None (unreadable)."""
    m = re.search(r"drawing from '([^']+)'", text or "")
    if not m:
        return None
    source = m.group(1).lower()
    if "ac" in source.split() or source.startswith("ac"):
        return "ac"
    if "battery" in source:
        return "battery"
    return None


def power_source() -> Optional[str]:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_pmset(out.stdout)


def input_idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard/mouse input: the orchestrator's
    Quartz reader, else ``ioreg`` HIDIdleTime; None when both fail."""
    try:
        from ai.orchestrator import _input_idle_seconds  # noqa: WPS433 - lazy, heavy module

        value = _input_idle_seconds()
        if value is not None:
            return value
    except Exception:
        pass
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5)
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out.stdout)
        return int(m.group(1)) / 1e9 if m else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Persistent state (decline / accept / trained counts)
# ---------------------------------------------------------------------------


class TrainingState:
    """Small JSON state, 0600 and atomic like ``ai.idea_outcomes``.
    ``path=None`` keeps it in memory (tests)."""

    VERSION = 1
    FIELDS = ("declined_at_rows", "declined_at", "accepted", "accepted_at", "accepted_rows",
              "offered_at", "offered_rows", "last_trained_rows", "last_trained_at",
              "last_adapter")

    def __init__(self, path: Optional[Path] = None, *, clock: Optional[Clock] = None) -> None:
        self.path = Path(path) if path is not None else None
        self.clock: Clock = clock or time.time
        self._lock = threading.RLock()
        self.data: dict[str, Any] = {k: None for k in self.FIELDS}
        self.data["accepted"] = False
        self.data.update({k: v for k, v in self._read().items() if k in self.FIELDS})

    def _read(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("%s unreadable; starting empty", self.path)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.stem}-")
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"version": self.VERSION, **self.data}, fh, indent=1)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            logger.exception("failed to save %s", self.path)

    def _set(self, **values: Any) -> None:
        with self._lock:
            self.data.update(values)
            self._save()

    def get(self, key: str) -> Any:
        return self.data.get(key)

    @property
    def accepted(self) -> bool:
        return bool(self.data.get("accepted"))

    def offer_pending(self) -> bool:
        at = self.data.get("offered_at")
        return at is not None and float(self.clock()) - float(at) < OFFER_PENDING_TTL

    def record_offered(self, rows: int) -> None:
        self._set(offered_at=float(self.clock()), offered_rows=int(rows))

    def record_decline(self, rows: int) -> None:
        """Denied or timed out: due again only once the corpus doubles."""
        self._set(declined_at_rows=int(rows), declined_at=float(self.clock()),
                  offered_at=None, accepted=False)

    def record_accept(self, rows: int) -> None:
        """The user approved the card: one start is allowed."""
        self._set(accepted=True, accepted_at=float(self.clock()), accepted_rows=int(rows),
                  offered_at=None)

    def consume_accept(self) -> bool:
        with self._lock:
            if not self.accepted:
                return False
            self._set(accepted=False)
            return True

    def record_trained(self, rows: int, adapter_dir: str) -> None:
        self._set(last_trained_rows=int(rows), last_trained_at=float(self.clock()),
                  last_adapter=str(adapter_dir))


def offer_due(corpus: Any, state: TrainingState, *, running: bool = False) -> bool:
    """True when the training card should be raised now (#119 decision 1-2)."""
    counts = corpus.usable_counts()
    total, negatives = int(counts.get("total", 0)), int(counts.get("negatives", 0))
    if total < min_rows() or negatives < min_negative():
        return False
    if running or state.accepted or state.offer_pending():
        return False
    declined = state.get("declined_at_rows")
    if declined and total < 2 * int(declined):
        return False
    trained = state.get("last_trained_rows")
    if trained and total < 2 * int(trained):
        return False
    return True


def plan_iters(train_rows: int) -> int:
    batches = max(1, math.ceil(train_rows / BATCH_SIZE))
    return max(MIN_ITERS, min(MAX_ITERS, batches * EPOCHS))


def seconds_per_iter() -> float:
    try:
        return float(os.environ.get("AGENT_TRAIN_SECONDS_PER_ITER", "") or SECONDS_PER_ITER)
    except ValueError:
        return SECONDS_PER_ITER


def estimate_minutes(usable_rows: int) -> int:
    train = int(usable_rows * (1 - 0.1) * (1 - VALID_FRACTION))
    return max(1, round(plan_iters(train) * seconds_per_iter() / 60))


def offer_card(corpus: Any, *, base_model: str = "", unloads_assistant: bool = False
               ) -> tuple[str, dict[str, Any], str, str]:
    """``(tool_name, kwargs, reasoning, summary)`` for the training card."""
    counts = corpus.usable_counts()
    total = int(counts.get("total", 0))
    minutes = estimate_minutes(total)
    summary = f"Train your own model? About {minutes} min, uses the GPU, stays on this Mac"
    if unloads_assistant:
        summary += "; the assistant pauses while it trains"
    reasoning = (
        f"You've decided on {total} cards ({int(counts.get('negatives', 0))} said no or undone). "
        "A small add-on to the local model can learn which offers you take and how you "
        "word them. It runs only while you're away and plugged in, and you can cancel it. "
        "It is used only if it beats the current model on decisions it hasn't seen, and it "
        "never decides what needs your approval."
    )
    kwargs = {"base_model": base_model, "rows": total, "minutes": minutes}
    return TRAIN_TOOL_NAME, kwargs, reasoning, summary


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def row_target(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The chat example for one corpus row, or None when it is not a target
    (denied, undone, timed out, tainted, held out, no usable prompt)."""
    if row.get("verdict") not in ("approved", "edited") or row.get("negative"):
        return None
    if row.get("outcome") == "undone" or row.get("tainted") or row.get("heldout"):
        return None
    prompt = str(row.get("prompt") or "").strip()
    if not prompt or row.get("mail_stripped"):
        return None
    card = row.get("card") or {}
    tool = card.get("tool") or row.get("tool_name")
    if not tool:
        return None
    args = row.get("final_args") if row.get("verdict") == "edited" and row.get("final_args") is not None \
        else card.get("args") or {}
    call = json.dumps({"name": tool, "arguments": args}, ensure_ascii=False)
    return {"messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": f"<tool_call>\n{call}\n</tool_call>"},
    ]}


def build_dataset(corpus: Any, data_dir: Path) -> dict[str, Any]:
    """Freeze the split, write ``train.jsonl`` / ``valid.jsonl`` (0600) from
    the training rows only. Returns counts and the split id."""
    split = corpus.freeze_split()
    heldout_ids = {r["id"] for r in corpus.heldout_rows(include_tainted=True)}
    examples = [(r["id"], ex) for r in corpus.training_rows()
                if r["id"] not in heldout_ids and (ex := row_target(r)) is not None]
    if len(examples) < 2 * BATCH_SIZE:
        raise ValueError(f"only {len(examples)} usable training examples (need {2 * BATCH_SIZE})")
    n_valid = max(BATCH_SIZE, math.ceil(len(examples) * VALID_FRACTION))
    # Deterministic: every k-th example goes to valid.
    step = max(1, len(examples) // n_valid)
    valid_idx = set(list(range(0, len(examples), step))[:n_valid])
    train = [ex for i, (_, ex) in enumerate(examples) if i not in valid_idx]
    valid = [ex for i, (_, ex) in enumerate(examples) if i in valid_idx]
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    for name, items in (("train", train), ("valid", valid)):
        path = data_dir / f"{name}.jsonl"
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for ex in items:
                fh.write(json.dumps(ex, ensure_ascii=False) + "\n")
    return {"train": len(train), "valid": len(valid), "split_id": split.get("split_id"),
            "heldout": len(heldout_ids), "row_ids": [i for i, _ in examples]}


# ---------------------------------------------------------------------------
# Progress parsing
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# mlx-lm 0.32 TrainUI.report_train: "  <it>    <loss> ▼    <tok/s>    <k>k"
_TRAIN_RE = re.compile(r"^\s*(\d+)\s+(\d+(?:\.\d+)?)\s*[▼▲]")
# Older mlx-lm: "Iter 10: Train loss 2.345, ..."
_OLD_RE = re.compile(r"Iter\s+(\d+):\s*Train loss\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_VAL_RE = re.compile(r"^\s*(\d+)\s+val\s+(\d+(?:\.\d+)?)|Iter\s+(\d+):\s*Val loss\s+(\d+(?:\.\d+)?)",
                     re.IGNORECASE)


def parse_progress(line: str) -> Optional[dict[str, Any]]:
    """One child output line → ``{iteration, loss}`` / ``{iteration, val_loss}``."""
    text = _ANSI_RE.sub("", line or "")
    m = _TRAIN_RE.search(text) or _OLD_RE.search(text)
    if m:
        return {"iteration": int(m.group(1)), "loss": float(m.group(2))}
    m = _VAL_RE.search(text)
    if m:
        it = m.group(1) or m.group(3)
        val = m.group(2) or m.group(4)
        return {"iteration": int(it), "val_loss": float(val)}
    return None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def default_command(base_model: str, data_dir: Path, adapter_dir: Path, iters: int) -> list[str]:
    return [
        sys.executable, "-m", "mlx_lm", "lora", "--train",
        "--model", base_model, "--data", str(data_dir), "--adapter-path", str(adapter_dir),
        "--iters", str(iters), "--batch-size", str(BATCH_SIZE), "--num-layers", str(NUM_LAYERS),
        "--max-seq-length", str(MAX_SEQ_LENGTH), "--mask-prompt", "--grad-checkpoint",
        "--steps-per-report", str(STEPS_PER_REPORT), "--steps-per-eval", str(max(iters, 1)),
        "--save-every", str(max(iters, 1)),
    ]


async def _call(fn: Optional[Callable[..., Any]], *args: Any) -> None:
    if fn is None:
        return
    try:
        result = fn(*args)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("personal training hook failed")


class TrainingRunner:
    """One LoRA run at a time, in a child process. All guards injectable."""

    def __init__(
        self,
        corpus: Any,
        state: TrainingState,
        *,
        models_dir: Optional[Path] = None,
        command: CommandFactory = default_command,
        power: Callable[[], Optional[str]] = power_source,
        idle: Callable[[], Optional[float]] = input_idle_seconds,
        on_progress: Optional[ProgressCallback] = None,
        on_finished: Optional[Callable[[dict[str, Any]], Any]] = None,
        before_start: Optional[Hook] = None,
        after_end: Optional[Hook] = None,
        poll_seconds: float = POLL_SECONDS,
        idle_start: float = IDLE_START_SECONDS,
        idle_resume: float = IDLE_RESUME_SECONDS,
        clock: Clock = time.monotonic,
        wall_clock: Clock = time.time,
    ) -> None:
        self.corpus = corpus
        self.state = state
        self.models_dir = Path(models_dir) if models_dir is not None else default_models_dir()
        self.command = command
        self.power = power
        self.idle = idle
        self.on_progress = on_progress
        self.on_finished = on_finished
        self.before_start = before_start
        self.after_end = after_end
        self.poll_seconds = poll_seconds
        self.idle_start = idle_start
        self.idle_resume = idle_resume
        self.clock = clock
        self.wall_clock = wall_clock
        self._task: Optional[asyncio.Task] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancel: Optional[asyncio.Event] = None  # created in start(), on the loop
        self._tail: list[str] = []
        self._run_seconds = 0.0
        self._run_mark: Optional[float] = None
        self._status: dict[str, Any] = self._blank("idle")

    # ---- status ------------------------------------------------------------

    @staticmethod
    def _blank(state: str) -> dict[str, Any]:
        return {"state": state, "progress": 0.0, "iteration": 0, "total": 0, "eta_s": None,
                "loss": None, "adapter_dir": None, "error": None, "reason": None,
                "base_model": None, "rows": 0}

    def status(self) -> dict[str, Any]:
        s = dict(self._status)
        it, total = int(s.get("iteration") or 0), int(s.get("total") or 0)
        s["progress"] = round(min(1.0, it / total), 4) if total else (1.0 if s["state"] == "done" else 0.0)
        s["eta_s"] = self._eta(it, total) if s["state"] in ACTIVE_STATES else (0 if s["state"] == "done" else None)
        return s

    def _eta(self, it: int, total: int) -> Optional[int]:
        if not total:
            return None
        elapsed = self._run_seconds + ((self.clock() - self._run_mark) if self._run_mark is not None else 0.0)
        per = elapsed / it if it > 0 and elapsed > 0 else seconds_per_iter()
        return max(0, int(round(per * (total - it))))

    @property
    def running(self) -> bool:
        return self._status["state"] in ACTIVE_STATES

    async def _update(self, **values: Any) -> None:
        self._status.update(values)
        await _call(self.on_progress, self.status())

    def _clock_start(self) -> None:
        if self._run_mark is None:
            self._run_mark = self.clock()

    def _clock_stop(self) -> None:
        if self._run_mark is not None:
            self._run_seconds += self.clock() - self._run_mark
            self._run_mark = None

    # ---- guards --------------------------------------------------------------

    def _blocked(self, threshold: float) -> Optional[str]:
        """None when the run may go; else the reason (shown in status)."""
        try:
            source = self.power()
        except Exception:
            source = None
        if source != "ac":
            return "on battery" if source == "battery" else "power source unknown"
        try:
            idle = self.idle()
        except Exception:
            idle = None
        if idle is None:
            return "can't tell if you're away"
        if idle < threshold:
            return "waiting until you're away"
        return None

    # ---- start / cancel --------------------------------------------------------

    def _next_dir(self) -> Path:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.models_dir, 0o700)
        nums = [int(p.name) for p in self.models_dir.iterdir() if p.is_dir() and p.name.isdigit()]
        n = max(nums, default=0) + 1
        path = self.models_dir / str(n)
        path.mkdir(mode=0o700)
        return path

    async def start(self, base_model: str, *, require_accept: bool = True) -> dict[str, Any]:
        """Build the dataset and wait for AC + idle, then train. Raises
        ``PermissionError`` without an accepted card, ``RuntimeError`` when a
        run is already going, ``ValueError`` when there is too little data."""
        if self.running:
            raise RuntimeError("a training run is already in progress")
        if require_accept and not self.state.consume_accept():
            raise PermissionError("training was not accepted on a card")
        if not base_model:
            raise ValueError("no base model")
        adapter_dir = self._next_dir()
        try:
            info = build_dataset(self.corpus, adapter_dir / "data")
        except Exception:
            shutil.rmtree(adapter_dir, ignore_errors=True)
            raise
        iters = plan_iters(info["train"])
        usable = int(self.corpus.usable_counts().get("total", 0))
        meta = {"base_model": base_model, "created_at": self.wall_clock(), "split_id": info["split_id"],
                "train_rows": info["train"], "valid_rows": info["valid"], "heldout_rows": info["heldout"],
                "usable_rows": usable, "iters": iters, "status": "training"}
        self._write_meta(adapter_dir, meta)
        self._tail, self._run_seconds, self._run_mark = [], 0.0, None
        self._cancel = asyncio.Event()
        self._status = self._blank("waiting_for_idle")
        self._status.update(total=iters, adapter_dir=str(adapter_dir), base_model=base_model, rows=usable,
                            reason=self._blocked(self.idle_start))
        self._task = asyncio.create_task(self._supervise(base_model, adapter_dir, iters, meta))
        await _call(self.on_progress, self.status())
        return self.status()

    async def cancel(self) -> dict[str, Any]:
        if not self.running or self._task is None:
            return self.status()
        assert self._cancel is not None
        self._cancel.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=30)
        except asyncio.TimeoutError:
            self._task.cancel()
        return self.status()

    async def wait(self) -> dict[str, Any]:
        if self._task is not None:
            await asyncio.shield(self._task)
        return self.status()

    @staticmethod
    def _write_meta(adapter_dir: Path, meta: Mapping[str, Any]) -> None:
        path = adapter_dir / "meta.json"
        fd, tmp = tempfile.mkstemp(dir=adapter_dir, prefix=".meta-")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dict(meta), fh, indent=1)
        os.replace(tmp, path)

    # ---- the supervised run ------------------------------------------------------

    async def _sleep_or_cancel(self) -> bool:
        """Sleep one poll; True if cancel was asked meanwhile."""
        assert self._cancel is not None
        try:
            await asyncio.wait_for(self._cancel.wait(), timeout=self.poll_seconds)
        except asyncio.TimeoutError:
            pass
        return self._cancel.is_set()

    async def _supervise(self, base_model: str, adapter_dir: Path, iters: int,
                         meta: dict[str, Any]) -> None:
        hooks_ran = False
        try:
            # 1. Wait for AC power and idle; nothing runs before that.
            while True:
                reason = self._blocked(self.idle_start)
                if reason is None:
                    break
                if self._status.get("reason") != reason:
                    await self._update(reason=reason)
                if await self._sleep_or_cancel():
                    await self._finish_cancelled(adapter_dir)
                    return
            await _call(self.before_start)
            hooks_ran = True
            argv = list(self.command(base_model, adapter_dir / "data", adapter_dir, iters))
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", "TERM": "dumb",
                   "COLUMNS": "120"}
            # Plain Popen + poll(), not asyncio subprocesses: on macOS the
            # asyncio child watcher's waitid() wakes on a SIGSTOPped child
            # and then blocks the event loop in waitpid() until it exits.
            self._proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env, start_new_session=True,
            )
            self._clock_start()
            await self._update(state="running", reason=None)
            lines: asyncio.Queue = asyncio.Queue()
            reader = threading.Thread(target=self._read_output,
                                      args=(self._proc, asyncio.get_running_loop(), lines),
                                      daemon=True, name="personal-training-reader")
            reader.start()
            paused = False
            eof = False
            while True:
                eof = await self._drain(lines, self.poll_seconds) or eof
                if self._proc.poll() is not None:
                    break
                if self._cancel is not None and self._cancel.is_set():
                    await self._kill()
                    break
                if not paused:
                    reason = self._blocked(self.idle_resume)
                    if reason is not None:
                        self._signal(signal.SIGSTOP)
                        paused = True
                        self._clock_stop()
                        await self._update(state="paused", reason=reason)
                else:
                    reason = self._blocked(self.idle_start)
                    if reason is None:
                        self._signal(signal.SIGCONT)
                        paused = False
                        self._clock_start()
                        await self._update(state="running", reason=None)
                    elif self._status.get("reason") != reason:
                        await self._update(reason=reason)
            if not eof:
                await asyncio.to_thread(reader.join, 10)
                await self._drain(lines, 0)
            self._clock_stop()
            code = self._proc.returncode
            if self._cancel is not None and self._cancel.is_set():
                await self._finish_cancelled(adapter_dir)
            elif code == 0 and (adapter_dir / "adapters.safetensors").exists():
                await self._finish_done(adapter_dir, iters, meta)
            else:
                tail = " | ".join(self._tail[-5:])
                await self._finish_failed(adapter_dir, f"training exited with code {code}: {tail}"[:500])
        except asyncio.CancelledError:
            await self._kill()
            shutil.rmtree(adapter_dir, ignore_errors=True)
            self._status.update(state="cancelled", adapter_dir=None)
            raise
        except Exception as exc:  # noqa: BLE001 - reported in status
            logger.exception("personal training run failed")
            await self._kill()
            await self._finish_failed(adapter_dir, str(exc)[:500])
        finally:
            self._proc = None
            shutil.rmtree(adapter_dir / "data", ignore_errors=True)  # user text: never left behind
            if hooks_ran:
                await _call(self.after_end)

    @staticmethod
    def _read_output(proc: subprocess.Popen, loop: asyncio.AbstractEventLoop,
                     out: asyncio.Queue) -> None:
        """Reader thread: child output split on CR/LF; None marks EOF."""
        assert proc.stdout is not None
        buf = ""

        def put(item: Optional[str]) -> None:
            try:
                loop.call_soon_threadsafe(out.put_nowait, item)
            except RuntimeError:  # loop closed
                pass

        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            *parts, buf = re.split(r"[\r\n]", buf)
            for line in parts:
                put(line)
        if buf:
            put(buf)
        put(None)

    async def _drain(self, lines: asyncio.Queue, timeout: float) -> bool:
        """Handle queued output for up to ``timeout`` s; True once EOF seen."""
        end = self.clock() + timeout
        while True:
            remaining = end - self.clock()
            try:
                line = lines.get_nowait() if remaining <= 0 else \
                    await asyncio.wait_for(lines.get(), timeout=remaining)
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                return False
            if line is None:
                return True
            await self._handle_line(line)

    async def _handle_line(self, line: str) -> None:
        clean = _ANSI_RE.sub("", line).strip()
        if not clean:
            return
        self._tail = (self._tail + [clean])[-20:]
        parsed = parse_progress(clean)
        if parsed is None:
            return
        values: dict[str, Any] = {}
        if "loss" in parsed:
            values["loss"] = parsed["loss"]
        if "val_loss" in parsed:
            values["val_loss"] = parsed["val_loss"]
        it = parsed["iteration"]
        if it >= int(self._status.get("iteration") or 0):
            values["iteration"] = min(it, int(self._status.get("total") or it))
        await self._update(**values)

    def _signal(self, sig: int) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    async def _kill(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._signal(signal.SIGCONT)  # a stopped child can't handle SIGTERM
        self._signal(signal.SIGTERM)
        for _ in range(100):
            if proc.poll() is not None:
                return
            await asyncio.sleep(0.05)
        self._signal(signal.SIGKILL)
        await asyncio.to_thread(proc.wait)

    async def _finish_cancelled(self, adapter_dir: Path) -> None:
        await self._kill()
        shutil.rmtree(adapter_dir, ignore_errors=True)
        await self._update(state="cancelled", adapter_dir=None, reason=None)

    async def _finish_failed(self, adapter_dir: Path, error: str) -> None:
        shutil.rmtree(adapter_dir, ignore_errors=True)
        await self._update(state="failed", adapter_dir=None, error=error, reason=None)

    async def _finish_done(self, adapter_dir: Path, iters: int, meta: dict[str, Any]) -> None:
        meta = {**meta, "status": "done", "finished_at": self.wall_clock(),
                "final_loss": self._status.get("loss"), "train_seconds": round(self._run_seconds, 1)}
        self._write_meta(adapter_dir, meta)
        self.state.record_trained(int(meta.get("usable_rows") or 0), str(adapter_dir))
        await self._update(state="done", iteration=iters, reason=None)
        await _call(self.on_finished, self.status())


# ---------------------------------------------------------------------------
# Facade the daemon wires up
# ---------------------------------------------------------------------------


class PersonalTraining:
    """Threshold + card + runner in one object (``app.state.personal_training``).

    The orchestrator calls :meth:`offer_due` (e.g. after each recorded corpus
    row), raises :meth:`offer_card` through the normal approval path, calls
    :meth:`mark_offered` when the card goes out, and :meth:`on_card_decision`
    with the verdict.
    """

    def __init__(self, corpus: Any, *, state: Optional[TrainingState] = None,
                 runner: Optional[TrainingRunner] = None,
                 base_model: Optional[Callable[[], str]] = None,
                 unloads_assistant: bool = False, **runner_kwargs: Any) -> None:
        self.corpus = corpus
        self.state = state or TrainingState(default_state_path())
        self.runner = runner or TrainingRunner(corpus, self.state, **runner_kwargs)
        self.base_model = base_model or (lambda: "")
        self.unloads_assistant = unloads_assistant

    def _rows(self) -> int:
        return int(self.corpus.usable_counts().get("total", 0))

    def offer_due(self) -> bool:
        return offer_due(self.corpus, self.state, running=self.runner.running)

    def offer_card(self) -> tuple[str, dict[str, Any], str, str]:
        return offer_card(self.corpus, base_model=self.base_model(),
                          unloads_assistant=self.unloads_assistant)

    def mark_offered(self) -> None:
        self.state.record_offered(self._rows())

    def record_decline(self) -> None:
        self.state.record_decline(self._rows())

    def record_accept(self) -> None:
        self.state.record_accept(self._rows())

    async def on_card_decision(self, approved: bool, base_model: Optional[str] = None) -> dict[str, Any]:
        """Denied / timed out → decline. Approved → accept and start the run
        (it then waits for AC power and idle on its own)."""
        if not approved:
            self.record_decline()
            return self.status()
        self.record_accept()
        return await self.start(base_model)

    async def start(self, base_model: Optional[str] = None) -> dict[str, Any]:
        return await self.runner.start(base_model or self.base_model())

    async def cancel(self) -> dict[str, Any]:
        return await self.runner.cancel()

    def status(self) -> dict[str, Any]:
        counts = self.corpus.usable_counts()
        return {**self.runner.status(), "accepted": self.state.accepted,
                "usable": int(counts.get("total", 0)), "negatives": int(counts.get("negatives", 0)),
                "min_rows": min_rows(), "min_negative": min_negative()}
