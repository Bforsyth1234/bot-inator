"""One card for a burst of suggestions from email (#115).

Live finding 2026-11-18: three emails in three minutes each raised their
own "add a reminder" card. Events run one at a time, so each mail turn
waited on its own card. Now, in an ambient ``mail_received`` turn, a call
of a :data:`BATCH_TOOLS` tool is not shown at once: it is queued and the
turn goes on (the model is told it was queued). :data:`WINDOW_SECONDS`
after the first queued call (or as soon as :data:`MAX_ITEMS` are queued)
the batch becomes ONE card with a tick box per action (the #76 bundle
card). Only the ticked actions run, after the user approves; a denied or
timed-out card runs nothing. The card is still the consent (7a).

The flush pushes a :data:`BATCH_EVENT_TYPE` event onto the bus, so the
card is shown by the normal one-at-a-time event loop
(``Orchestrator._handle_card_batch``), never from the timer thread.

``AGENT_CARD_BATCH_SECONDS=0`` turns batching off (every call asks at once).
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ai import prepared_offers

logger = logging.getLogger(__name__)

BATCH_EVENT_TYPE = "card_batch_ready"
BATCH_TOOLS: frozenset[str] = frozenset({"create_reminder", "create_event", "create_note"})
BATCH_EVENT_TYPES: frozenset[str] = frozenset({"mail_received"})
WINDOW_ENV = "AGENT_CARD_BATCH_SECONDS"
WINDOW_SECONDS = 180.0
MAX_ITEMS = 6

_VERBS = {
    "create_reminder": ("Add reminder", "title"),
    "create_event": ("Add event", "title"),
    "create_note": ("Add note", "title"),
}


def window_seconds() -> float:
    raw = os.environ.get(WINDOW_ENV)
    if raw is None:
        return WINDOW_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return WINDOW_SECONDS


def action_label(tool: str, kwargs: dict[str, Any]) -> str:
    verb, field_name = _VERBS.get(tool, (tool, ""))
    value = str(kwargs.get(field_name) or "").strip() if field_name else ""
    return f"{verb}: {value[:80]}" if value else verb


@dataclass
class _Item:
    tool: str
    kwargs: dict[str, Any]
    event_id: str
    trigger: str
    notes: list[str] = field(default_factory=list)


class CardBatcher:
    """Thread-safe queue of card-less calls waiting for one bundle card."""

    def __init__(
        self,
        push: Callable[[Any], None],
        *,
        window_s: Optional[float] = None,
        max_items: int = MAX_ITEMS,
        timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
    ) -> None:
        self._push = push
        self._window = window_seconds() if window_s is None else float(window_s)
        self._max = max(1, int(max_items))
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._items: list[_Item] = []
        self._timer: Any = None

    @property
    def enabled(self) -> bool:
        return self._window > 0

    def eligible(self, tool: str, event_type: str, turn_kind: str) -> bool:
        return (
            self.enabled and turn_kind == "event"
            and tool in BATCH_TOOLS and tool in prepared_offers.BUNDLE_TOOLS
            and event_type in BATCH_EVENT_TYPES
        )

    def pending(self) -> int:
        with self._lock:
            return len(self._items)

    def add(self, tool: str, kwargs: dict[str, Any], *, event_id: str,
            trigger: str, notes: Optional[list[str]] = None) -> int:
        """Queue one call; its 1-based place in the batch."""
        flush_now = False
        with self._lock:
            self._items.append(_Item(tool, dict(kwargs), event_id, trigger, list(notes or [])))
            count = len(self._items)
            if count >= self._max:
                flush_now = True
            elif self._timer is None:
                self._timer = self._timer_factory(self._window, self.flush)
                try:
                    self._timer.daemon = True
                except Exception:
                    pass
                self._timer.start()
        if flush_now:
            self.flush()
        return count

    def flush(self) -> bool:
        """Turn the queue into one card event. False when it was empty."""
        with self._lock:
            items, self._items = self._items, []
            timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if not items:
            return False
        try:
            self._push(build_event(items))
        except Exception:
            logger.exception("card batch: push failed; %d suggestions dropped", len(items))
            return False
        return True


def build_event(items: list[_Item]) -> Any:
    from events.event_bus import ContextEvent

    actions = [
        prepared_offers.OfferAction(i.tool, dict(i.kwargs), True, action_label(i.tool, i.kwargs))
        for i in items
    ]
    n = len(actions)
    headline = (
        "From your email: " + actions[0].label if n == 1
        else f"{n} suggestions from your email. Untick any you don't want."
    )
    notes: list[str] = []
    for item in items:
        for line in item.notes:
            if line not in notes:
                notes.append(line)
    offer = prepared_offers.PreparedOffer("card_batch", headline, actions)
    return ContextEvent(
        event_type=BATCH_EVENT_TYPE,
        metadata={
            prepared_offers.PREPARED_KEY: offer.to_dict(),
            "origin_notes": notes,
            "event_ids": [i.event_id for i in items],
            "trigger": items[0].trigger,
            "event_type": "mail_received",
        },
    )


QUEUED_MESSAGE = (
    "Queued: the user will see this with the other suggestions from recent "
    "email on one card in a few minutes. Do not call it again; emit final_answer."
)

__all__ = [
    "BATCH_EVENT_TYPE", "BATCH_EVENT_TYPES", "BATCH_TOOLS", "CardBatcher", "MAX_ITEMS",
    "QUEUED_MESSAGE", "WINDOW_ENV", "WINDOW_SECONDS", "action_label", "build_event",
    "window_seconds",
]
