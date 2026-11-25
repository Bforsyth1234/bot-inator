"""#150: restart the daemon now, or as soon as it is idle.

``POST /api/restart?when=idle`` (sent by the app after a save that needs a
restart) must never drop a turn or withdraw an approval card, so it waits
until no turn is running and no card is open, checking every ~2 s. A
plain ``POST /api/restart`` (the Restart Daemon button) cancels any wait
and restarts at once. ``main.py`` only calls :class:`DeferredRestart`;
the re-exec itself stays there.

Idle (:func:`daemon_idle`) =
* the orchestrator loop is not handling an event (``EventBus.consumer_busy``)
  and no event is queued for it, and
* no approval_request is waiting (``Orchestrator._pending``) and no code
  review is waiting (``Orchestrator._pending_code``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0
# Lets the HTTP reply get out before the process is replaced.
REPLY_DELAY_SECONDS = 0.5

NOW = "now"
PENDING = "pending"


def daemon_idle(orchestrator: Any, event_bus: Any) -> bool:
    """True when no turn is running or queued and no card is open."""
    if orchestrator is not None:
        if getattr(orchestrator, "_pending", None):
            return False  # an approval card is open
        if getattr(orchestrator, "_pending_code", None):
            return False  # a code review card is open
    if event_bus is not None:
        if event_bus.qsize() > 0:
            return False  # an event waits for its turn
        task = getattr(orchestrator, "_task", None)
        loop_alive = task is not None and not task.done()
        if loop_alive and getattr(event_bus, "consumer_busy", False):
            return False  # the orchestrator is handling an event (a turn)
    return True


class DeferredRestart:
    """At most one pending restart; ``reexec`` runs once when it fires."""

    def __init__(
        self,
        is_idle: Callable[[], bool],
        reexec: Callable[[], None],
        *,
        poll_seconds: float = POLL_SECONDS,
        reply_delay: float = REPLY_DELAY_SECONDS,
    ) -> None:
        self._is_idle = is_idle
        self._reexec = reexec
        self._poll = poll_seconds
        self._reply_delay = reply_delay
        self._waiter: Optional[asyncio.Task[None]] = None
        self._fired = False

    @property
    def pending(self) -> bool:
        return self._waiter is not None and not self._waiter.done()

    def request(self, *, when_idle: bool) -> str:
        """Schedule a restart; returns ``"now"`` or ``"pending"``."""
        if not when_idle:
            self.cancel()
            self._fire_soon(recheck=False)
            return NOW
        if self.pending:
            return PENDING  # one pending restart, however many saves
        if self._safe_idle():
            self._fire_soon(recheck=True)
            return NOW
        self._start_waiting()
        return PENDING

    def cancel(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.cancel()
        self._waiter = None

    def _safe_idle(self) -> bool:
        try:
            return bool(self._is_idle())
        except Exception:
            logger.exception("Idle check failed; treating the daemon as busy")
            return False

    def _start_waiting(self) -> None:
        self._waiter = asyncio.get_running_loop().create_task(
            self._wait_then_fire(), name="deferred-restart",
        )
        logger.info("Restart requested; waiting until the daemon is idle")

    def _fire_soon(self, *, recheck: bool) -> None:
        def _later() -> None:
            # A turn may have started in the reply delay: wait for it.
            if recheck and not self._safe_idle():
                if not self.pending and not self._fired:
                    self._start_waiting()
                return
            self._fire()

        asyncio.get_running_loop().call_later(self._reply_delay, _later)

    def _fire(self) -> None:
        if self._fired:
            return  # one re-exec, however many requests
        self._fired = True
        try:
            self._reexec()
        except Exception:
            self._fired = False  # the process is still here: allow a retry
            logger.exception("Restart failed")

    async def _wait_then_fire(self) -> None:
        while not self._safe_idle():
            await asyncio.sleep(self._poll)
        # Fire from this task (no reply to wait for). ``_fire`` replaces
        # the process, so nothing after it runs in production.
        self._fire()
