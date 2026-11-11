"""Read the front window's text in any app, on demand (todo #89).

One read-only tool (belongs in ``tools.READ_ONLY_TOOL_NAMES``):

* ``read_front_window`` -- app, window title and the Accessibility text of
  the window the user is looking at.

The daemon's Python has no Accessibility (TCC) grant, so the menu bar app
walks the focused window's AX tree (``Services/ScreenTextBridge.swift``)
and answers a ``screen_text_request`` frame, the way Calendar works (#60).
``main.py`` registers each WS connection with :data:`bridge`; the app
advertises itself with ``screen_text_status`` (its ``AXIsProcessTrusted``,
read without prompting).

Rules (#89 DECISIONS, binding):

* Accessibility text, not pixels: no screenshot, no OCR, nothing stored.
  The app caps the walk (depth, node count, 8,000 characters, 1 s); this
  module caps again. No AX text -> "nothing readable", never a guess.
* Never read: secure text fields (the app skips them, and reads nothing at
  all while one has focus), the never-watch apps (#87; the list goes in the
  request so the app refuses before reading, and the reply is checked
  again here), bot-inator's own windows, and browser windows (those go
  through ``read_active_tab_text``, which applies the #58 sensitive-site
  rules; private windows are never read this way).
* Only chat and quick-ask turns may call it in v1 (the user asked):
  :func:`set_turn_kind_provider` tells the tool which turn it is in; any
  other turn (events, ideas, open-help on a timer) gets an error and
  nothing is read. With no provider set, nothing is read.
* Under a cloud main engine the text is personal text: the #31
  placeholder unless ``AGENT_CLOUD_PERSONAL_TEXT=1`` (7c). App and title
  are not personal text (``ai/privacy.py`` scope) and stay.
* Errors are distinct: the app is not connected, Accessibility is off,
  and each refusal above.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import threading
import uuid
from typing import Any, Awaitable, Callable, Optional

try:
    from smolagents import tool  # type: ignore
except ImportError:  # pragma: no cover - dep not installed during tests
    def tool(fn):  # type: ignore
        """Fallback no-op decorator when smolagents is unavailable."""
        fn.is_tool = True  # type: ignore[attr-defined]
        return fn


READ_ONLY: frozenset[str] = frozenset({"read_front_window"})

DEFAULT_MAX_CHARS = 8000
MAX_CHARS_CAP = 8000
REQUEST_TIMEOUT_S = 2.0

# The menu bar app's own bundle id prefix (decision 3: never its windows).
OWN_BUNDLE_PREFIX = "com.botinator."

# Browsers (decision 3): their windows go through read_active_tab_text, the
# tool that applies the #58 sensitive-site rules. Prefix match on the
# lower-cased bundle id. The app keeps the same list.
BROWSER_BUNDLE_PREFIXES: tuple[str, ...] = (
    "com.apple.safari",               # Safari, Technology Preview
    "com.google.chrome",              # Chrome, beta, dev, canary
    "org.chromium.chromium",
    "org.mozilla.",                   # Firefox, Nightly, Developer Edition
    "company.thebrowser.",            # Arc
    "com.brave.browser",
    "com.microsoft.edgemac",
    "com.operasoftware.",
    "com.vivaldi.vivaldi",
    "app.zen-browser.",
    "org.torproject.",
    "com.kagi.kagimacos",             # Orion
)

# Turn kinds (``Orchestrator._turn_kind``) in which the user asked (decision 4).
USER_TURN_KINDS: frozenset[str] = frozenset({"chat"})

APP_NOT_CONNECTED = (
    "The front window can't be read: open the bot-inator menu bar app (it "
    "reads windows through Accessibility)."
)
ACCESSIBILITY_OFF = (
    "The front window can't be read: Accessibility is off for the bot-inator "
    "menu bar app. Turn it on in System Settings > Privacy & Security > "
    "Accessibility."
)
NOT_USER_TURN = (
    "read_front_window only runs when the user asks in chat or quick ask, "
    "not from an event or a suggestion."
)
NEVER_WATCH = "the front app is on the never-watch list; nothing was read"
SECURE_FIELD = "a password field has focus; nothing was read"
BROWSER = (
    "the front window is a browser; use read_active_tab_text for the page "
    "(it skips banks, password managers and sign-in pages)"
)
OWN_APP = "the front window is bot-inator's own; nothing was read"
NO_WINDOW = "no window of another app is in front"
NOTHING_READABLE = "nothing readable: this window exposes no Accessibility text"
TIMEOUT = "the menu bar app didn't answer within 2 s"

_CODE_MESSAGES: dict[str, str] = {
    "accessibility_off": ACCESSIBILITY_OFF,
    "no_window": NO_WINDOW,
    "never_watch": NEVER_WATCH,
    "secure_field": SECURE_FIELD,
    "browser": BROWSER,
    "own_app": OWN_APP,
    "nothing_readable": NOTHING_READABLE,
}

CLOUD_NOTE = (
    "Window text was kept on this Mac because the main engine runs in the "
    "cloud (the user can allow sending it in Settings, or with "
    "AGENT_CLOUD_PERSONAL_TEXT=1)."
)


class ScreenTextError(RuntimeError):
    """A read that could not happen; ``code`` is the reply's ``reason``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- bridge to the menu bar app --------------------------------------------

# send(request_id, max_chars, never_watch, timeout_seconds), run on the loop.
ScreenTextSend = Callable[[str, int, list, int], Awaitable[None]]


class _Conn:
    def __init__(self, conn_id: str, send: ScreenTextSend,
                 loop: asyncio.AbstractEventLoop) -> None:
        self.conn_id = conn_id
        self.send = send
        self.loop = loop
        self.trusted: Optional[bool] = None  # None: not a screen-text client
        self.advertised_at = 0


class ScreenTextBridge:
    """Request/response to the menu bar app over the WebSocket.

    Same shape as ``tools.calendar_access.AppCalendarBridge``: works from
    any thread except the loop's own; requests go to the ONE connection that
    most recently sent ``screen_text_status``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, _Conn] = {}
        self._pending: dict[str, tuple[str, concurrent.futures.Future]] = {}
        self._counter = itertools.count(1)

    # connection lifecycle (main.py, on the loop) ------------------------------

    def connect(self, conn_id: str, send: ScreenTextSend,
                loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._conns[conn_id] = _Conn(conn_id, send, loop)

    def disconnect(self, conn_id: str) -> None:
        with self._lock:
            self._conns.pop(conn_id, None)
            dropped = [rid for rid, (cid, _) in self._pending.items() if cid == conn_id]
            futures = [self._pending.pop(rid)[1] for rid in dropped]
        for fut in futures:
            if not fut.done():
                fut.set_exception(ScreenTextError("app_not_connected", APP_NOT_CONNECTED))

    def set_status(self, conn_id: str, accessibility_trusted: bool) -> None:
        with self._lock:
            conn = self._conns.get(conn_id)
            if conn is None:
                return
            conn.trusted = bool(accessibility_trusted)
            conn.advertised_at = next(self._counter)

    # state (never prompts) ------------------------------------------------------

    def _pick(self) -> Optional[_Conn]:
        with self._lock:
            capable = [c for c in self._conns.values() if c.trusted is not None]
        return max(capable, key=lambda c: c.advertised_at) if capable else None

    def connected(self) -> bool:
        return self._pick() is not None

    def accessibility_trusted(self) -> Optional[bool]:
        conn = self._pick()
        return conn.trusted if conn else None

    # request / response -----------------------------------------------------------

    def submit_response(self, conn_id: str, payload: dict[str, Any]) -> bool:
        """Resolve a pending request (main.py, on ``screen_text_response``)."""
        request_id = str(payload.get("request_id") or "")
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is not None and entry[0] == conn_id:
                self._pending.pop(request_id, None)
            else:
                entry = None  # unknown id, or another connection's request
            trusted = payload.get("accessibility_trusted")
            conn = self._conns.get(conn_id)
            if trusted is not None and conn is not None and conn.trusted is not None:
                conn.trusted = bool(trusted)
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(payload)
        return True

    def request(self, max_chars: int, never_watch: list[str],
                timeout: float = REQUEST_TIMEOUT_S) -> dict[str, Any]:
        conn = self._pick()
        if conn is None:
            raise ScreenTextError("app_not_connected", APP_NOT_CONNECTED)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is conn.loop:
            raise ScreenTextError("failed", "read_front_window was called on the event loop")
        request_id = f"scr_{uuid.uuid4().hex[:12]}"
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending[request_id] = (conn.conn_id, fut)
        try:
            sent = asyncio.run_coroutine_threadsafe(
                conn.send(request_id, int(max_chars), list(never_watch),
                          max(1, int(round(timeout)))),
                conn.loop)
            sent.result(timeout=timeout)
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise ScreenTextError("timeout", TIMEOUT) from None
        except ScreenTextError:
            raise
        except Exception as exc:
            raise ScreenTextError("app_not_connected",
                                  f"Couldn't reach the menu bar app: {exc}") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)


bridge = ScreenTextBridge()


# ---- who may call it (decision 4) -------------------------------------------

_turn_kind_provider: Optional[Callable[[], str]] = None


def set_turn_kind_provider(provider: Optional[Callable[[], str]]) -> None:
    """The orchestrator's current turn kind (``"chat"``, ``"event"``, ...).

    Wiring: ``set_turn_kind_provider(lambda: orchestrator._turn_kind)``.
    ``None`` (the default) means no turn may read.
    """
    global _turn_kind_provider
    _turn_kind_provider = provider


def user_asked() -> bool:
    provider = _turn_kind_provider
    if provider is None:
        return False
    try:
        return str(provider() or "") in USER_TURN_KINDS
    except Exception:
        return False


# ---- checks -------------------------------------------------------------------

def is_browser(bundle_id: Optional[str]) -> bool:
    bid = (bundle_id or "").strip().lower()
    return bool(bid) and any(bid.startswith(p) for p in BROWSER_BUNDLE_PREFIXES)


def is_own_app(bundle_id: Optional[str]) -> bool:
    return (bundle_id or "").strip().lower().startswith(OWN_BUNDLE_PREFIX)


def _never_watch_list() -> list[str]:
    from ai import delivery  # lazy: ai pulls in the AI stack

    try:
        return list(delivery.default_settings().never_watch())
    except Exception:
        return list(delivery.NEVER_WATCH_SEED)


def _never_watched(bundle_id: Optional[str], app: Optional[str]) -> bool:
    from ai.delivery import is_never_watched

    return is_never_watched(bundle_id, app)


def _clamp(max_chars: Any) -> int:
    try:
        value = int(max_chars)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return max(1, min(value, MAX_CHARS_CAP))


def _empty(reason: str, error: str, app: str = "", title: str = "") -> dict[str, Any]:
    return {"app": app, "title": title, "text": "", "truncated": False,
            "error": error, "reason": reason}


def _redact(text: str) -> tuple[str, bool]:
    from ai import privacy

    redacted = privacy.redact_text(text, "main")
    return redacted, redacted != text


# ---- the tool -------------------------------------------------------------------

def read_front_window_impl(max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    """Body of :func:`read_front_window` (plain function, for tests)."""
    if not user_asked():
        return _empty("not_user_turn", NOT_USER_TURN)
    return _read(max_chars)


def context_with_window_text(context: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Quick-ask's "include this window's text" tick box (decision 4).

    The app sends ``selection_source: "window"`` with no text; the ticked
    box is the user's request. Returns the context with ``selection`` set to
    the window's text (every rule of :func:`read_front_window` applies, the
    #31 placeholder included), or with the source dropped and a one-line
    ``selection`` saying why nothing was read. Any other context is
    returned unchanged. Blocking: call it in a worker thread, not on the loop.
    """
    if not isinstance(context, dict) or context.get("selection_source") != "window":
        return context
    out = dict(context)
    result = _read(DEFAULT_MAX_CHARS)
    if result.get("text"):
        out["selection"] = result["text"]
        if result.get("truncated"):
            out["selection"] += "\n…(truncated)"
        if result.get("app"):
            out["front_app"] = result["app"]
        if result.get("title"):
            out["window_title"] = result["title"]
    else:
        out["selection"] = f"[the window's text was not read: {result.get('error') or NOTHING_READABLE}]"
        if result.get("reason") == "never_watch":
            out.pop("front_app", None)
            out.pop("window_title", None)
    out["selection_source"] = "window"
    return out


def _read(max_chars: Any) -> dict[str, Any]:
    limit = _clamp(max_chars)
    if not bridge.connected():
        return _empty("app_not_connected", APP_NOT_CONNECTED)
    if bridge.accessibility_trusted() is False:
        return _empty("accessibility_off", ACCESSIBILITY_OFF)
    try:
        reply = bridge.request(limit, _never_watch_list())
    except ScreenTextError as exc:
        return _empty(exc.code, str(exc))

    app = str(reply.get("app") or "")
    bundle_id = str(reply.get("bundle_id") or "")
    title = str(reply.get("title") or "")
    # Checked again here whatever the app answered (decision 3).
    if _never_watched(bundle_id, app):
        return _empty("never_watch", NEVER_WATCH)
    if is_own_app(bundle_id):
        return _empty("own_app", OWN_APP)
    if is_browser(bundle_id):
        return _empty("browser", BROWSER, app=app, title=title)
    if not reply.get("ok"):
        code = str(reply.get("error_code") or "failed")
        message = _CODE_MESSAGES.get(code) or str(reply.get("error") or "the window couldn't be read")
        if code == "never_watch":
            app = title = ""
        return _empty(code, message, app=app, title=title)

    raw = str(reply.get("text") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return _empty("nothing_readable", NOTHING_READABLE, app=app, title=title)
    truncated = bool(reply.get("truncated")) or len(raw) > limit
    text, redacted = _redact(raw[:limit])
    result: dict[str, Any] = {"app": app, "title": title, "text": text,
                              "truncated": truncated}
    if redacted:
        result["note"] = CLOUD_NOTE
    return result


@tool
def read_front_window(max_chars: int = DEFAULT_MAX_CHARS) -> dict[str, Any]:
    """Return the text of the window the user is looking at, in any app.

    Use it when the user asks about "this", "what I'm looking at" or the
    window in front (a document, a chat thread, a mail). Reads the window's
    Accessibility text once; never password fields, password managers,
    bot-inator's own windows, or browser windows (for a web page use
    read_active_tab_text). Only works when the user asked in chat or quick
    ask.

    Args:
        max_chars: Maximum number of characters of window text to return
            (default 8000, which is also the cap).

    Returns:
        ``{"app", "title", "text", "truncated"}``, plus ``error`` and
        ``reason`` when nothing could or may be read (the menu bar app is
        not running, Accessibility is off, a password field has focus, the
        window has no readable text, ...).
    """
    return read_front_window_impl(max_chars)
