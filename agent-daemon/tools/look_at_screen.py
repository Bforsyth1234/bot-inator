"""Look at the screen: ask a vision model about a window (todo #107).

One built-in, ``look_at_screen(question, target="front_window")``:

1. Approval-gated like ``take_screenshot`` (#102): NOT in
   ``READ_ONLY_TOOL_NAMES``, and the card's line (:func:`card_note`) names
   what will be captured, the window's title and app or "the whole
   screen", and which model will see it. Nothing is looked at without a
   click. Only in a turn the user started (chat / quick ask, the #89 rule):
   no event, idea or timer may call it, so there is no background capture.
2. Captures once with ``/usr/sbin/screencapture`` in the daemon (the tool
   sandbox can't run it, #102): ``-l <window id>`` for the front window,
   ``-m`` for the main display. Into a fresh 0700 temp folder, scaled to
   ``ai.vision_engine.MAX_IMAGE_SIDE``, then asked, then DELETED, on
   success and on every failure.
3. The model is ``ai.vision_engine``: local MLX by default; a cloud model
   only with the personal-text opt-in AND a chosen vision model (decision
   3). A refused route is refused BEFORE capturing.
4. Never looks (decision 4, #89's skip list): while secure input is on (a
   password field has focus, or an app holds secure input), while a
   never-watch app (#87: 1Password, Keychain Access, …) is in front or is
   the target, for "screen" while any never-watch app has a window on
   screen, at a private browser window, or at a Chrome / Safari tab on the
   #58 sensitive-site list. bot-inator's own windows are skipped: "front
   window" is the first window of another app, so asking from the chat
   panel looks at the window behind it.
5. The answer is about the user's screen, so under a cloud MAIN engine it
   is personal text: the #31 placeholder unless the opt-in is on.
"""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn

from .quit_app import in_tool_sandbox

logger = logging.getLogger(__name__)

GATED: frozenset[str] = frozenset({"look_at_screen"})

SCREENCAPTURE = "/usr/sbin/screencapture"
TIMEOUT_SECONDS = 20.0
TARGETS = ("front_window", "screen")
PIN_SECONDS = 300.0
MIN_WINDOW_SIDE = 40
OWN_BUNDLE_PREFIX = "com.botinator."
TEMP_PREFIX = "botinator-look-"

PERMISSION_HINT = (
    "Grant Screen Recording to the daemon in Settings → Permissions → "
    "Screen Recording, then try again."
)
SANDBOX_NOTE = (
    "look_at_screen can't run inside a generated tool (the tool sandbox does "
    "not allow screencapture). Call the look_at_screen tool directly instead."
)
NOT_USER_TURN = (
    "look_at_screen only runs when the user asks in chat or quick ask, not "
    "from an event or a suggestion."
)
SECURE_INPUT = "a password field or another secure-input app has focus; nothing was captured"
NEVER_WATCH = "{app} is on the never-watch list; nothing was captured"
NEVER_WATCH_ON_SCREEN = "{app} (on the never-watch list) has a window on screen; nothing was captured"
PRIVATE_WINDOW = "the window is a private browsing window; nothing was captured"
SENSITIVE_SITE = "the browser tab is on the sensitive-site list (banks, password managers, sign-in pages); nothing was captured"
NO_WINDOW = "no window of another app is on screen"
BAD_TARGET = "target must be \"front_window\" or \"screen\""
CLOUD_NOTE = (
    "The answer was kept on this Mac because the main engine runs in the "
    "cloud (the user can allow sending it in Settings, or with "
    "AGENT_CLOUD_PERSONAL_TEXT=1)."
)

# Words browsers put in a private window's title.
PRIVATE_TITLE_MARKERS: tuple[str, ...] = (
    "private browsing", "(incognito)", "incognito", "inprivate", "private window",
)
# Bundle ids whose front tab URL can be asked (read_active_tab's browsers).
_URL_BROWSERS: dict[str, str] = {
    "com.google.chrome": "Google Chrome",
    "com.apple.safari": "Safari",
}


@dataclass(frozen=True)
class Window:
    window_id: int
    pid: int
    app: str
    bundle_id: str
    title: str

    def describe(self) -> str:
        title = self.title.strip()
        return f"the window “{title}” ({self.app})" if title else f"the front {self.app} window"


class Refused(Exception):
    """Nothing may be captured; the message says why."""


# ---- macOS state (tests replace these) ---------------------------------------

def _secure_input_enabled() -> bool:
    """``IsSecureEventInputEnabled()``: True while a password field (or an
    app such as a password manager or Terminal's Secure Keyboard Entry)
    holds secure input. Unknown counts as on: when in doubt, don't look."""
    try:
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        fn = carbon.IsSecureEventInputEnabled
        fn.restype = ctypes.c_bool
        return bool(fn())
    except Exception:
        return True


def _frontmost_app() -> tuple[str, str]:
    """``(bundle id, name)`` of the frontmost app; blanks when unknown."""
    try:
        from AppKit import NSWorkspace  # type: ignore

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return "", ""
        return str(app.bundleIdentifier() or ""), str(app.localizedName() or "")
    except Exception:
        return "", ""


def _bundle_id_for_pid(pid: int) -> str:
    try:
        from AppKit import NSRunningApplication  # type: ignore

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        return str(app.bundleIdentifier() or "") if app is not None else ""
    except Exception:
        return ""


def _window_list() -> list[dict[str, Any]]:
    """On-screen windows, front to back (``CGWindowListCopyWindowInfo``)."""
    try:
        import Quartz  # type: ignore

        opts = (Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements)
        info = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    except Exception:
        return []
    out = []
    for w in info:
        bounds = w.get("kCGWindowBounds") or {}
        out.append({
            "id": int(w.get("kCGWindowNumber") or 0),
            "pid": int(w.get("kCGWindowOwnerPID") or 0),
            "owner": str(w.get("kCGWindowOwnerName") or ""),
            "title": str(w.get("kCGWindowName") or ""),
            "layer": int(w.get("kCGWindowLayer") or 0),
            "alpha": float(w.get("kCGWindowAlpha") or 0.0),
            "width": float(bounds.get("Width") or 0),
            "height": float(bounds.get("Height") or 0),
        })
    return out


def _browser_tab_url(app_name: str) -> Optional[str]:
    """The front tab's URL of Chrome or Safari, ``None`` when unknown."""
    try:
        from . import read_active_tab as rat

        found = rat._query(app_name, rat._META_SCRIPTS[app_name])
        return found["url"] if found else None
    except Exception:
        return None


def _run_screencapture(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([SCREENCAPTURE, *args], capture_output=True, text=True,
                          timeout=TIMEOUT_SECONDS)


def _screen_recording_granted() -> Optional[bool]:
    try:
        import Quartz  # type: ignore

        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


# ---- checks ---------------------------------------------------------------------

def _never_watched(bundle_id: str, app: str) -> bool:
    from ai.delivery import is_never_watched

    return is_never_watched(bundle_id or None, app or None)


def _is_own(bundle_id: str, pid: int) -> bool:
    return bundle_id.lower().startswith(OWN_BUNDLE_PREFIX) or pid == os.getpid()


def _app_windows() -> list[Window]:
    """Normal (layer 0), visible app windows, front to back, bundle ids filled."""
    bundles: dict[int, str] = {}
    out = []
    for w in _window_list():
        if w["layer"] != 0 or w["alpha"] <= 0:
            continue
        if min(w["width"], w["height"]) < MIN_WINDOW_SIDE:
            continue
        pid = w["pid"]
        if pid not in bundles:
            bundles[pid] = _bundle_id_for_pid(pid)
        out.append(Window(w["id"], pid, w["owner"], bundles[pid], w["title"]))
    return out


def _front_window(windows: list[Window]) -> Optional[Window]:
    for w in windows:
        if not _is_own(w.bundle_id, w.pid):
            return w
    return None


def _check_window(w: Window) -> None:
    if _never_watched(w.bundle_id, w.app):
        raise Refused(NEVER_WATCH.format(app=w.app or w.bundle_id))
    title = w.title.lower()
    if any(m in title for m in PRIVATE_TITLE_MARKERS):
        raise Refused(PRIVATE_WINDOW)
    browser = _URL_BROWSERS.get(w.bundle_id.lower())
    if browser:
        url = _browser_tab_url(browser)
        if url:
            from .read_active_tab import is_sensitive_url

            if is_sensitive_url(url):
                raise Refused(SENSITIVE_SITE)


def resolve(target: str) -> tuple[Optional[Window], str]:
    """``(window or None for the screen, description)``; raises :class:`Refused`.

    Every decision-4 check runs here, at card time and again at capture.
    """
    if target not in TARGETS:
        raise Refused(BAD_TARGET)
    if _secure_input_enabled():
        raise Refused(SECURE_INPUT)
    bundle, name = _frontmost_app()
    if (bundle or name) and _never_watched(bundle, name):
        raise Refused(NEVER_WATCH.format(app=name or bundle))
    windows = _app_windows()
    if target == "screen":
        for w in windows:
            if _never_watched(w.bundle_id, w.app):
                raise Refused(NEVER_WATCH_ON_SCREEN.format(app=w.app or w.bundle_id))
            if any(m in w.title.lower() for m in PRIVATE_TITLE_MARKERS):
                raise Refused(PRIVATE_WINDOW)
        return None, "the whole screen"
    front = _front_window(windows)
    if front is None:
        raise Refused(NO_WINDOW)
    _check_window(front)
    return front, front.describe()


# ---- the card's window, pinned (the window shown is the window captured) ----

_pin_lock = threading.Lock()
_pinned: Optional[tuple[float, Window]] = None


def _pin(w: Optional[Window]) -> None:
    global _pinned
    with _pin_lock:
        _pinned = (time.monotonic(), w) if w is not None else None


def _resolve_for_capture(target: str) -> tuple[Optional[Window], str]:
    """Like :func:`resolve`, but keeps the window the card named when it is
    still on screen (the user may click around before approving)."""
    global _pinned
    with _pin_lock:
        pinned, _pinned = _pinned, None
    window, what = resolve(target)
    if target != "front_window" or pinned is None:
        return window, what
    stamp, w = pinned
    if time.monotonic() - stamp > PIN_SECONDS:
        return window, what
    for cur in _app_windows():
        if cur.window_id == w.window_id and cur.pid == w.pid:
            _check_window(cur)
            return cur, cur.describe()
    return window, what


# ---- helpers ------------------------------------------------------------------------

def _user_asked() -> bool:
    from . import read_front_window as screen_text

    return screen_text.user_asked()


def _route():
    from ai import vision_engine

    return vision_engine.route()


def _redact_answer(text: str) -> tuple[str, bool]:
    from ai import privacy

    redacted = privacy.redact_text(text, "main")
    return redacted, redacted != text


def card_note(question: str = "", target: str = "front_window", *args: Any, **kwargs: Any) -> str:
    """One line for the approval card: what will be captured, and by whom."""
    question = question or str(kwargs.get("question") or "")
    target = (target or str(kwargs.get("target") or "front_window")).strip() or "front_window"
    try:
        window, what = resolve(target)
    except Refused as exc:
        _pin(None)
        return f"Would capture the screen, but it is refused: {exc}."
    _pin(window)
    r = _route()
    if not r.ok:
        return f"Would capture {what}, but it is refused: {r.refused}"
    note = f"Captures {what} once and asks {r.describe()}"
    if question.strip():
        note += f": “{question.strip()[:200]}”"
    note += ". The image is deleted afterwards."
    if r.kind == "cloud":
        note += " The screenshot leaves this Mac."
    else:
        from ai import vision_engine

        if not vision_engine.model_cached(r.model):
            from config import VISION_MODEL_BYTES

            size = VISION_MODEL_BYTES.get(r.model)
            gb = f" (about {size / 1e9:.1f} GB)" if size else ""
            note += f" First use downloads the model{gb} from Hugging Face."
    return note


def _capture(window: Optional[Window], folder: Path) -> Path:
    path = folder / "look.png"
    args = ["-x", "-o", "-l", str(window.window_id), str(path)] if window else ["-x", "-m", str(path)]
    try:
        proc = _run_screencapture(args)
    except Exception as exc:
        raise RuntimeError(f"screencapture failed: {exc}") from exc
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if proc.returncode != 0 or size == 0:
        detail = (proc.stderr or "").strip() or f"screencapture exited {proc.returncode}"
        raise RuntimeError(f"nothing was captured ({detail}). {PERMISSION_HINT}")
    return path


def look_at_screen_impl(question: str, target: str = "front_window") -> dict[str, Any]:
    """Body of :func:`look_at_screen` (plain function, for tests)."""
    if in_tool_sandbox():
        return {"status": "error", "error": SANDBOX_NOTE}
    if not _user_asked():
        return {"status": "error", "error": NOT_USER_TURN}
    target = (target or "front_window").strip() or "front_window"
    try:
        window, what = _resolve_for_capture(target)
    except Refused as exc:
        return {"status": "error", "error": f"refused: {exc}"}
    r = _route()
    if not r.ok:
        return {"status": "error", "error": f"refused: {r.refused}"}

    from ai import vision_engine

    folder = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        os.chmod(folder, 0o700)
        try:
            path = _capture(window, folder)
            vision_engine.prepare_image(path)
            reply = vision_engine.answer(path, question, what=what, r=r)
        except vision_engine.VisionError as exc:
            return {"status": "error", "error": str(exc), "captured": what}
        except Exception as exc:
            logger.warning("look_at_screen failed: %s", exc)
            return {"status": "error", "error": str(exc), "captured": what}
    finally:
        shutil.rmtree(folder, ignore_errors=True)

    answer, redacted = _redact_answer(reply["answer"])
    result: dict[str, Any] = {"status": "ok", "answer": answer, "captured": what,
                              "engine": reply["engine"]}
    if redacted:
        result["note"] = CLOUD_NOTE
    if _screen_recording_granted() is False:
        result["note"] = (result.get("note", "") + " Screen Recording isn't granted, so "
                          "the image may show only the desktop. " + PERMISSION_HINT).strip()
    return result


@tool
def look_at_screen(question: str, target: str = "front_window") -> dict[str, Any]:
    """Look at the user's screen once and answer a question about what it shows.

    Use it when the user asks about something visible that text tools can't
    read: a chart, an image, a layout, text inside a picture ("what does
    this chart say?", "what's wrong with this layout?"). It captures the
    window in front (or the whole screen), asks a vision model, deletes the
    image, and returns the answer. Never write a tool to capture or look at
    the screen: a generated tool cannot.

    Args:
        question: What the user wants to know about the window or screen.
        target: "front_window" (default: the window of the app in front,
            not bot-inator's own) or "screen" (the whole main display).

    Returns:
        ``{"status": "ok", "answer", "captured", "engine"}``, or
        ``{"status": "error", "error"}`` when nothing may or could be
        captured (a password field has focus, a password manager is in
        front, a cloud vision model without the opt-in, ...).
    """
    return look_at_screen_impl(question, target)


# Read by the approval gate (``Orchestrator._wrap_tool_for_approval``).
look_at_screen.card_note = card_note  # type: ignore[attr-defined]
