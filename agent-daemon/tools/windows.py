"""See which windows are open, and open an installed app.

``list_windows`` reads the on-screen window list through Quartz
(``CGWindowListCopyWindowInfo``). It is read-only. Window titles need the
Screen Recording permission on recent macOS; without it, Quartz returns
app names only, and the result says so instead of guessing.

``open_app`` runs ``open -a`` on an app found in the standard app folders.
It launches something, so it is approval-gated (rule 7a).

Privacy: window titles can hold private text (document names, email
subjects). With ``AGENT_PROVIDER=groq`` they go to the cloud like any
other prompt text (tracked by #31).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        """Fallback no-op decorator when smolagents is unavailable."""
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


# Gate placement (shared decision 5). The wave runner copies these into
# ``tools.READ_ONLY_TOOL_NAMES``; tests assert against both.
READ_ONLY: frozenset[str] = frozenset({"list_windows"})
GATED: frozenset[str] = frozenset({"open_app"})

APP_DIRS: tuple[str, ...] = (
    "/Applications",
    os.path.expanduser("~/Applications"),
    "/System/Applications",
    "/System/Applications/Utilities",
)

_MAX_WINDOWS = 100
_NO_TITLES_NOTE = (
    "Window titles are unavailable: grant Screen Recording permission to "
    "the daemon (System Settings > Privacy & Security > Screen Recording). "
    "Only app names are listed."
)


def _quartz() -> Any:
    """Import Quartz lazily so tests can substitute a fake module."""
    import Quartz  # type: ignore

    return Quartz


def _titles_permitted(quartz: Any, windows: list[dict[str, Any]]) -> bool:
    """Return whether window titles are readable.

    Uses ``CGPreflightScreenCaptureAccess`` (never prompts) when present;
    otherwise falls back to "at least one window has a title".
    """
    preflight = getattr(quartz, "CGPreflightScreenCaptureAccess", None)
    if callable(preflight):
        try:
            return bool(preflight())
        except Exception:
            pass
    return any(w["title"] for w in windows)


@tool
def list_windows() -> dict[str, Any]:
    """List the user's open, on-screen app windows (app name and title).

    Window titles need the Screen Recording permission; without it the
    titles are empty, ``titles_available`` is False and ``note`` says why.

    Returns:
        A dict with ``status`` ("ok" or "error"), ``windows`` (a list of
        ``{"app": str, "title": str}``, front-most first), ``apps`` (sorted
        unique app names), ``titles_available`` (bool) and, when titles are
        missing, ``note``.
    """
    try:
        quartz = _quartz()
        options = (
            quartz.kCGWindowListOptionOnScreenOnly
            | quartz.kCGWindowListExcludeDesktopElements
        )
        raw = quartz.CGWindowListCopyWindowInfo(options, quartz.kCGNullWindowID) or []
    except Exception as exc:
        return {"status": "error", "error": f"could not read window list: {exc}",
                "windows": [], "apps": [], "titles_available": False}

    windows: list[dict[str, Any]] = []
    for info in raw:
        # Layer 0 is normal app windows; menu bar items, the Dock and
        # overlays live on other layers.
        if info.get("kCGWindowLayer", 0) != 0:
            continue
        app = str(info.get("kCGWindowOwnerName") or "")
        if not app:
            continue
        windows.append({"app": app, "title": str(info.get("kCGWindowName") or "")})
        if len(windows) >= _MAX_WINDOWS:
            break

    titles_available = _titles_permitted(quartz, windows)
    result: dict[str, Any] = {
        "status": "ok",
        "windows": windows,
        "apps": sorted({w["app"] for w in windows}),
        "titles_available": titles_available,
    }
    if not titles_available:
        result["note"] = _NO_TITLES_NOTE
    return result


def _installed_apps() -> dict[str, str]:
    """Map lower-cased app name (no ``.app``) to its bundle path."""
    found: dict[str, str] = {}
    for folder in APP_DIRS:
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            if entry.endswith(".app"):
                found.setdefault(entry[:-4].lower(), os.path.join(folder, entry))
    return found


def _resolve_app(name: str) -> str | None:
    """Return the bundle path for ``name``, or None if it isn't installed."""
    cleaned = name.strip()
    if cleaned.lower().endswith(".app"):
        cleaned = cleaned[:-4]
    if not cleaned or "/" in cleaned or cleaned.startswith("."):
        return None
    return _installed_apps().get(cleaned.lower())


@tool
def open_app(name: str) -> dict[str, Any]:
    """Open (launch or bring to front) an installed macOS app by name.

    Args:
        name: The app's name as it appears in /Applications, e.g. "Numbers"
            or "Numbers.app". Case does not matter; paths are not accepted.

    Returns:
        A dict with ``name``, ``status`` ("ok" or "error"), and ``path``
        on success or ``error`` on failure.
    """
    path = _resolve_app(name)
    if path is None:
        return {
            "name": name,
            "status": "error",
            "error": "no app with that name in /Applications, ~/Applications "
                     "or /System/Applications",
        }
    try:
        result = subprocess.run(
            ["open", "-a", path], capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        return {"name": name, "status": "error", "error": str(exc)}
    if result.returncode != 0:
        return {"name": name, "status": "error",
                "error": (result.stderr or "").strip() or f"open exited {result.returncode}"}
    return {"name": name, "status": "ok", "path": path}
