"""Quit a running macOS app, politely (todo #102).

A generated tool cannot do this: macOS refuses the ``quit`` Apple Event
from any process under ``sandbox-exec`` (-10004 "A privilege violation
occurred", even with a profile of only ``(allow default)``). So the daemon
does it, after the user approves the card (rule 7a: approval-gated, not in
``READ_ONLY_TOOL_NAMES``).

``quit_app`` resolves the name to a running app through
``NSRunningApplication`` and calls ``terminate()``: the same request as
Quit in the app's menu, so the app still asks to save unsaved work. It
refuses a name that isn't running, and refuses the daemon itself, Finder,
loginwindow, the bot-inator menu bar app and a few system UI processes.

Called from inside a generated tool it runs in the sandbox like every
built-in and cannot work there; it says so instead of failing obscurely.
"""

from __future__ import annotations

import os
from typing import Any, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


GATED: frozenset[str] = frozenset({"quit_app"})

# Never quit these (bundle ids, lower-cased).
PROTECTED_BUNDLE_IDS: frozenset[str] = frozenset({
    "com.apple.finder",
    "com.apple.loginwindow",
    "com.botinator.menubarui",
    # System UI: quitting them blanks the Dock / menu bar / Control Center.
    "com.apple.dock",
    "com.apple.systemuiserver",
    "com.apple.controlcenter",
    "com.apple.windowmanager",
})
PROTECTED_NAMES: frozenset[str] = frozenset({"finder", "loginwindow", "bot-inator", "menubarui"})

# NSApplicationActivationPolicyProhibited: background processes, not apps.
_POLICY_PROHIBITED = 2

SANDBOX_NOTE = (
    "quit_app can't run inside a generated tool (the tool sandbox blocks "
    "quitting apps). Call the quit_app tool directly instead."
)


def in_tool_sandbox() -> bool:
    """True inside a ``tools.tool_sandbox`` child (its TMPDIR is the call's work dir)."""
    return "/tool-sandbox-" in os.environ.get("TMPDIR", "")


def _running_apps() -> list[Any]:
    """``NSWorkspace``'s running apps; imported lazily so tests can fake it."""
    from AppKit import NSWorkspace  # type: ignore

    return list(NSWorkspace.sharedWorkspace().runningApplications() or [])


def _str(value: Any) -> str:
    try:
        return str(value() if callable(value) else value or "")
    except Exception:
        return ""


def _info(app: Any) -> dict[str, Any]:
    bundle_url = _str(getattr(app, "bundleURL", None))
    file_name = os.path.basename(bundle_url.rstrip("/"))
    if file_name.endswith(".app"):
        file_name = file_name[:-4]
    try:
        pid = int(app.processIdentifier())
    except Exception:
        pid = -1
    try:
        policy = int(app.activationPolicy())
    except Exception:
        policy = 0
    return {
        "name": _str(getattr(app, "localizedName", None)),
        "bundle_id": _str(getattr(app, "bundleIdentifier", None)),
        "file_name": file_name,
        "pid": pid,
        "policy": policy,
    }


def _clean(name: str) -> str:
    cleaned = (name or "").strip()
    if cleaned.lower().endswith(".app"):
        cleaned = cleaned[:-4]
    return cleaned.strip().lower()


def resolve_running_app(app_name: str) -> tuple[Optional[Any], Optional[dict[str, Any]]]:
    """``(NSRunningApplication, info)`` for ``app_name``, or ``(None, None)``.

    Matches the app's display name, its bundle's file name, or its bundle
    id, ignoring case and a trailing ``.app``. Background-only processes
    are skipped.
    """
    wanted = _clean(app_name)
    if not wanted:
        return None, None
    for app in _running_apps():
        info = _info(app)
        if info["policy"] == _POLICY_PROHIBITED:
            continue
        keys = {info["name"].lower(), info["file_name"].lower(), info["bundle_id"].lower()}
        if wanted in keys:
            return app, info
    return None, None


def refusal(info: dict[str, Any]) -> Optional[str]:
    """Why ``info``'s app may not be quit, or ``None``."""
    if info["pid"] == os.getpid():
        return "that is the bot-inator daemon itself"
    if info["bundle_id"].lower() in PROTECTED_BUNDLE_IDS or info["name"].lower() in PROTECTED_NAMES:
        return f"{info['name'] or info['bundle_id']} is protected (macOS or bot-inator needs it running)"
    return None


def card_note(app_name: str = "", *args: Any, **kwargs: Any) -> str:
    """One line for the approval card: which app, and that it may ask to save."""
    app_name = app_name or str(kwargs.get("app_name") or "")
    try:
        _, info = resolve_running_app(app_name)
    except Exception:
        info = None
    if info is None:
        return f"Quits {app_name!r} (not running right now, so this will fail)."
    why = refusal(info)
    if why:
        return f"Would quit {info['name']} ({info['bundle_id']}), but it will refuse: {why}."
    return (
        f"Quits {info['name']} ({info['bundle_id'] or 'no bundle id'}). "
        "Like Quit in its menu, it asks to save unsaved work."
    )


@tool
def quit_app(app_name: str) -> dict[str, Any]:
    """Quit a running macOS app, like choosing Quit in its menu (it still asks to save unsaved work).

    Use this whenever the user asks to quit or close an app. Do not write a
    new tool for it: a generated tool cannot quit apps.

    Args:
        app_name: The running app's name, e.g. "Spotify" or "Spotify.app"
            (case does not matter), or its bundle id.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``app`` and ``bundle_id``
        on success, or ``error`` explaining why it refused or failed.
    """
    if in_tool_sandbox():
        return {"status": "error", "error": SANDBOX_NOTE}
    try:
        app, info = resolve_running_app(app_name)
    except Exception as exc:
        return {"status": "error", "error": f"could not list running apps: {exc}"}
    if app is None or info is None:
        return {"status": "error", "app": app_name,
                "error": f"no running app named {app_name!r}"}
    why = refusal(info)
    if why:
        return {"status": "error", "app": info["name"], "bundle_id": info["bundle_id"],
                "error": f"refused: {why}"}
    try:
        sent = bool(app.terminate())
    except Exception as exc:
        return {"status": "error", "app": info["name"], "error": f"could not quit: {exc}"}
    if not sent:
        return {"status": "error", "app": info["name"], "bundle_id": info["bundle_id"],
                "error": "macOS did not deliver the quit request"}
    return {
        "status": "ok", "app": info["name"], "bundle_id": info["bundle_id"],
        "note": "Asked the app to quit; if it has unsaved work it shows its save prompt.",
    }


# Read by the approval gate (``Orchestrator._wrap_tool_for_approval``).
quit_app.card_note = card_note  # type: ignore[attr-defined]
