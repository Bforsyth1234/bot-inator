"""#143: a Chat turn the user can stop.

Each Chat turn runs as its own ``asyncio.Task`` with a :class:`ChatTurn`
handle, bound to a :class:`contextvars.ContextVar` for the turn's task. The
agent loop runs in a worker thread (``asyncio.to_thread`` copies the context,
and smolagents copies it again for parallel tool calls), so every place that
could still act after a Stop can ask :func:`stop_requested` from any thread:

* the approval gate (``Orchestrator._wrap_tool_for_approval``) refuses every
  tool call once the turn is stopped, before any card, read-only or not;
* the model adapters stop generating (MLX: between tokens) or drop a result
  that comes back after the Stop, by raising :class:`TurnStopped`, which ends
  the smolagents run;
* a card the turn opened is withdrawn: its future is resolved as NOT
  approved (:class:`CardWithdrawn`), never as approved.

A tool call that was already approved and running is never interrupted (a
side effect is not cut in half): the turn waits for the worker thread to
exit, then says in the stopped text that the approved action finished.

Turns stay serialised with every other event (the orchestrator's agent and
its per-turn fields are shared), so stopping one never lets another turn's
state in while this turn's worker can still run.
"""
from __future__ import annotations

import contextvars
import threading
import time
from collections import OrderedDict
from typing import Any, Optional

STOPPED_TEXT = "Stopped."
WITHDRAWN_NOTE = "withdrawn: the chat request was stopped"
WITHDRAWN = "withdrawn"

# Stop asked for an id that is not running yet (queued behind another turn):
# remembered briefly so the turn ends as stopped when it comes up.
MAX_EARLY_STOPS = 32
EARLY_STOP_TTL_S = 15 * 60.0

REFUSAL = {
    "skipped": True,
    "reason": "turn_stopped",
    "message": "The user stopped this request. Do not call any more tools.",
}


class TurnStopped(RuntimeError):
    """Raised in the agent's worker thread after a Stop: ends the run, and
    whatever the model returned is dropped."""


class CardWithdrawn(Exception):
    """Set on an approval card's future when its turn is stopped: the card
    is resolved as NOT approved (withdrawn), never as approved."""


class ChatTurn:
    """One running Chat turn: its task, its worker thread and what happened
    after a Stop. Thread-safe (the gate and the model run in worker threads)."""

    def __init__(self, event_id: str) -> None:
        self.event_id = str(event_id)
        self.task: Optional[Any] = None
        self.worker: Optional[Any] = None  # asyncio future of the agent thread
        self.user_logged = False
        self.user_log: Optional[Any] = None  # future of the user-row insert
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._answered = False
        self._running: list[str] = []
        self.finished_after_stop: list[tuple[str, bool]] = []
        self.withdrawn: list[str] = []

    # ---- stop --------------------------------------------------------------

    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def request_stop(self) -> bool:
        """True the first time; False when already stopping or the turn has
        its answer (a late Stop never replaces a real reply)."""
        with self._lock:
            if self._stop.is_set() or self._answered:
                return False
            self._stop.set()
            return True

    def mark_answered(self) -> bool:
        """The reply is final; a Stop from now on is a no-op. False when the
        Stop came first."""
        with self._lock:
            if self._stop.is_set():
                return False
            self._answered = True
            return True

    # ---- what ran ----------------------------------------------------------

    def tool_started(self, name: str) -> None:
        with self._lock:
            self._running.append(name)

    def tool_finished(self, name: str, ok: bool) -> None:
        with self._lock:
            if name in self._running:
                self._running.remove(name)
            if self._stop.is_set():
                self.finished_after_stop.append((name, bool(ok)))

    def note_withdrawn(self, tool_name: str) -> None:
        with self._lock:
            self.withdrawn.append(str(tool_name or "card"))


_CURRENT: contextvars.ContextVar[Optional[ChatTurn]] = contextvars.ContextVar(
    "bot_inator_chat_turn", default=None,
)


def bind(turn: Optional[ChatTurn]) -> contextvars.Token:
    return _CURRENT.set(turn)


def current() -> Optional[ChatTurn]:
    return _CURRENT.get()


def stop_requested() -> bool:
    """True in (any thread of) a Chat turn the user stopped."""
    turn = _CURRENT.get()
    return turn is not None and turn.stop_requested()


def raise_if_stopped() -> None:
    if stop_requested():
        raise TurnStopped("the user stopped this chat request")


def tool_ok(result: Any) -> bool:
    """Did a finished call change anything? (for the stopped text only)."""
    if isinstance(result, dict):
        if result.get("skipped"):
            return False
        if str(result.get("status") or "").lower() in ("denied", "error"):
            return False
    return True


def stopped_text(turn: ChatTurn) -> str:
    """The reply of a stopped turn. Always starts with :data:`STOPPED_TEXT`
    (the app shows a stopped turn by that prefix)."""
    parts = [STOPPED_TEXT]
    for name, ok in turn.finished_after_stop:
        if ok:
            parts.append(f"{name} was already approved and running, so it was "
                         "allowed to finish; it completed.")
        else:
            parts.append(f"{name} was already approved and running; it finished "
                         "without making a change.")
    if turn.withdrawn:
        n = len(turn.withdrawn)
        parts.append(("The approval card was" if n == 1 else f"{n} approval cards were")
                     + " withdrawn: not approved, nothing on "
                     + ("it" if n == 1 else "them") + " ran.")
    if len(parts) == 1:
        parts.append("Nothing was changed.")
    return " ".join(parts)


class EarlyStops:
    """Stops for ids not running yet, bounded in count and age."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._ids: "OrderedDict[str, float]" = OrderedDict()

    def add(self, event_id: str) -> None:
        self._prune()
        self._ids.pop(event_id, None)
        self._ids[event_id] = self._clock()
        while len(self._ids) > MAX_EARLY_STOPS:
            self._ids.popitem(last=False)

    def take(self, event_id: str) -> bool:
        self._prune()
        return self._ids.pop(event_id, None) is not None

    def _prune(self) -> None:
        now = self._clock()
        for key, at in list(self._ids.items()):
            if now - at > EARLY_STOP_TTL_S:
                self._ids.pop(key, None)

    def __len__(self) -> int:
        return len(self._ids)
