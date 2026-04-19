"""Subprocess helper for NSWorkspace activation observation.

The parent daemon cannot observe ``NSWorkspaceDidActivateApplicationNotification``
directly because uvicorn occupies the main thread with the asyncio loop and
nothing is servicing the ``CFRunLoop`` that the distributed-notification
mach-port source needs. This helper runs ``NSRunLoop`` on *its* main thread
and emits a JSON line per activation on stdout for the parent to consume.
"""
from __future__ import annotations

import json
import sys

from AppKit import NSWorkspace, NSWorkspaceDidActivateApplicationNotification
from Foundation import NSDate, NSObject, NSRunLoop


class _Observer(NSObject):
    def appActivated_(self, notification):  # noqa: N802 (Obj-C selector)
        try:
            ui = notification.userInfo()
            app = ui.objectForKey_("NSWorkspaceApplicationKey") if ui else None
            if app is None:
                return
            localized = app.localizedName()
            bundle = app.bundleIdentifier()
            payload = {
                "app_name": str(localized) if localized is not None else None,
                "bundle_id": str(bundle) if bundle is not None else None,
                "pid": int(app.processIdentifier()),
            }
            sys.stdout.write(json.dumps(payload) + "\n")
            sys.stdout.flush()
        except Exception as exc:  # pragma: no cover - defensive
            sys.stderr.write(f"helper error: {exc!r}\n")
            sys.stderr.flush()


def main() -> None:
    observer = _Observer.alloc().init()
    NSWorkspace.sharedWorkspace().notificationCenter().addObserver_selector_name_object_(
        observer,
        "appActivated:",
        NSWorkspaceDidActivateApplicationNotification,
        None,
    )
    sys.stderr.write("mac_app_helper: observer registered\n")
    sys.stderr.flush()
    run_loop = NSRunLoop.currentRunLoop()
    # Run forever; the parent terminates us by closing stdin / sending SIGTERM.
    while True:
        run_loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(60.0))


if __name__ == "__main__":
    main()
