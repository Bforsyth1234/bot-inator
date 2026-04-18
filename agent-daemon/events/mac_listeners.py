"""PyObjC listener for macOS active-application changes.

Subscribes to ``NSWorkspaceDidActivateApplicationNotification`` and runs a
background ``NSRunLoop`` on a dedicated daemon thread so the asyncio event
loop is never blocked. There is no polling: Cocoa delivers each activation
as a callback on the listener thread, which then forwards a
:class:`ContextEvent` to the shared :class:`EventBus`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .event_bus import ContextEvent, EventBus

logger = logging.getLogger(__name__)


def _build_listener_class() -> Any:
    """Build the NSObject subclass lazily so import works off-macOS for tests."""
    from AppKit import NSWorkspace  # noqa: F401  # imported for its side effects
    from Foundation import NSObject
    import objc

    class _MacAppObserver(NSObject):
        def initWithBus_(self, bus: EventBus):  # noqa: N802 (Obj-C selector)
            self = objc.super(_MacAppObserver, self).init()
            if self is None:
                return None
            self._bus = bus
            return self

        def appActivated_(self, notification) -> None:  # noqa: N802 (Obj-C selector)
            try:
                user_info = notification.userInfo()
                nsapp = user_info.objectForKey_("NSWorkspaceApplicationKey")
                app_name = str(nsapp.localizedName()) if nsapp is not None else None
                bundle_id = str(nsapp.bundleIdentifier()) if nsapp is not None else None
                pid = int(nsapp.processIdentifier()) if nsapp is not None else None
                event = ContextEvent(
                    event_type="app_activated",
                    app_name=app_name,
                    window_title=None,
                    metadata={"bundle_id": bundle_id, "pid": pid},
                )
                self._bus.push_threadsafe(event)
            except Exception:  # pragma: no cover - defensive, Obj-C callback
                logger.exception("Failed to handle NSWorkspace activation notification")

    return _MacAppObserver


class MacAppListener:
    """Owns the background NSRunLoop thread and the NSWorkspace observer."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._thread: threading.Thread | None = None
        self._observer: Any | None = None
        self._stop_event = threading.Event()
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="MacAppListener", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        self._stop_event.set()
        if self._observer is not None:
            try:
                from AppKit import NSWorkspace

                NSWorkspace.sharedWorkspace().notificationCenter().removeObserver_(
                    self._observer
                )
            except Exception:  # pragma: no cover
                logger.exception("Failed to remove NSWorkspace observer")
            self._observer = None

    def _run(self) -> None:
        try:
            from AppKit import (
                NSWorkspace,
                NSWorkspaceDidActivateApplicationNotification,
            )
            from Foundation import NSRunLoop, NSDate
        except ImportError:  # pragma: no cover - non-macOS environment
            logger.error("PyObjC AppKit/Foundation unavailable; listener disabled")
            self._ready.set()
            return

        observer_cls = _build_listener_class()
        self._observer = observer_cls.alloc().initWithBus_(self._bus)

        NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
            self._observer,
            b"appActivated:",
            NSWorkspaceDidActivateApplicationNotification,
            None,
        )
        self._ready.set()

        run_loop = NSRunLoop.currentRunLoop()
        while not self._stop_event.is_set():
            run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(1.0))
