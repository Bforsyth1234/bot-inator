"""Event bus primitives shared by all OS listeners."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ContextEvent:
    """A single observation produced by an OS event source."""

    event_type: str
    app_name: str | None = None
    window_title: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """Thin async wrapper around ``asyncio.Queue`` used by listeners.

    Listeners that run on foreign threads (e.g. NSRunLoop, watchdog Observer)
    must call :meth:`push_threadsafe` so the coroutine is scheduled on the
    owning event loop without blocking the caller.
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[ContextEvent] = asyncio.Queue(maxsize=maxsize)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the event loop that owns this bus (for cross-thread pushes)."""
        self._loop = loop

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    async def push(self, event: ContextEvent) -> None:
        await self._queue.put(event)

    async def consume(self) -> ContextEvent:
        return await self._queue.get()

    def push_threadsafe(self, event: ContextEvent) -> None:
        """Schedule a push from a non-asyncio thread.

        Raises ``RuntimeError`` if :meth:`bind_loop` has not been called.
        """
        loop = self._loop
        if loop is None:
            raise RuntimeError(
                "EventBus.push_threadsafe called before bind_loop(); "
                "no event loop is associated with this bus."
            )
        loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def qsize(self) -> int:
        return self._queue.qsize()
