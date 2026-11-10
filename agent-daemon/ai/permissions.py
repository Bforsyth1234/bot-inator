"""Every macOS permission the daemon uses, with a check and a request (#59).

ONE SOURCE OF TRUTH: :data:`PERMISSIONS` lists each permission (id, name,
why, used_by, check, request). A new built-in tool or watcher that needs a
permission adds its row here. :func:`check_all` returns one dict per row with
``status`` in ``granted | denied | not_asked | unknown``.

CHECKS NEVER PROMPT. ``check_all()`` must not put a macOS dialog on screen:

* Full Disk Access: open ``chat.db`` / list ``~/Library/Mail`` read-only.
* Screen Recording: ``CGPreflightScreenCaptureAccess`` (never prompts).
* Automation: ``AEDeterminePermissionToAutomateTarget`` with
  ``askUserIfNeeded = false``, called through ``ctypes`` on CoreServices
  (pyobjc has no binding for it). A target that isn't running reports
  ``unknown``, not ``denied`` (macOS answers ``procNotFound``).
* Browser "Allow JavaScript from Apple Events" (#58, not TCC): read the
  browser's own preference (Chrome's profile ``Preferences`` JSON, Safari's
  ``defaults``). No JavaScript is ever run in the user's tabs.
* Calendars: provided by the menu bar app via EventKit (#60); ``unknown``
  until #60 calls :func:`set_external_check`.

Only :func:`request` (an explicit ``POST /api/permissions/<id>/request``,
i.e. a user click) may prompt or open System Settings.

Which process macOS attributes a permission to: TCC uses the *responsible*
process (for a daemon the menu bar app starts, usually the app itself; for
one started in a terminal, the terminal). :func:`process_info` reports both
the resolved Python binary and the responsible process so the user grants
the right entry in Settings.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

GRANTED = "granted"
DENIED = "denied"
NOT_ASKED = "not_asked"
UNKNOWN = "unknown"
STATUSES = (GRANTED, DENIED, NOT_ASKED, UNKNOWN)

_PANE = "x-apple.systempreferences:com.apple.preference.security?"
SETTINGS_URLS = {
    "full_disk_access": _PANE + "Privacy_AllFiles",
    "screen_recording": _PANE + "Privacy_ScreenCapture",
    "automation": _PANE + "Privacy_Automation",
    "calendars": _PANE + "Privacy_Calendars",
}

CheckResult = dict[str, str]  # {"status": ..., "detail": ...}


def _result(status: str, detail: str = "") -> CheckResult:
    return {"status": status, "detail": detail}


# ---- primitives (module globals so tests patch them) -------------------------

def _open_url(url: str) -> None:
    """Open a URL (System Settings pane) with ``/usr/bin/open``."""
    subprocess.run(["/usr/bin/open", url], check=False, timeout=10,
                   capture_output=True)


def _quartz() -> Any:
    import Quartz  # type: ignore
    return Quartz


def _cg_preflight_screen_capture() -> Optional[bool]:
    fn = getattr(_quartz(), "CGPreflightScreenCaptureAccess", None)
    return bool(fn()) if callable(fn) else None


def _cg_request_screen_capture() -> bool:
    return bool(_quartz().CGRequestScreenCaptureAccess())


class _AEDesc(ctypes.Structure):
    _fields_ = [("descriptorType", ctypes.c_uint32),
                ("dataHandle", ctypes.c_void_p)]


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


_CORESERVICES = "/System/Library/Frameworks/CoreServices.framework/CoreServices"
_cs_lib: Any = None


def _coreservices() -> Any:
    global _cs_lib
    if _cs_lib is None:
        lib = ctypes.CDLL(_CORESERVICES)
        lib.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_long, ctypes.POINTER(_AEDesc)]
        lib.AECreateDesc.restype = ctypes.c_int16
        lib.AEDisposeDesc.argtypes = [ctypes.POINTER(_AEDesc)]
        lib.AEDisposeDesc.restype = ctypes.c_int16
        lib.AEDeterminePermissionToAutomateTarget.argtypes = [
            ctypes.POINTER(_AEDesc), ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint8]
        lib.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
        _cs_lib = lib
    return _cs_lib


def _ae_determine(bundle_id: str, ask: bool) -> int:
    """``AEDeterminePermissionToAutomateTarget`` for ``bundle_id``.

    ``ask=False`` never prompts. ``ask=True`` may show the Automation dialog
    and blocks until the user answers: only :func:`request` passes it.
    """
    lib = _coreservices()
    data = bundle_id.encode("utf-8")
    desc = _AEDesc()
    err = lib.AECreateDesc(_fourcc("bund"), data, len(data), ctypes.byref(desc))
    if err:
        raise OSError(f"AECreateDesc failed: {err}")
    try:
        wildcard = _fourcc("****")
        return int(lib.AEDeterminePermissionToAutomateTarget(
            ctypes.byref(desc), wildcard, wildcard, 1 if ask else 0))
    finally:
        lib.AEDisposeDesc(ctypes.byref(desc))


_AE_NO_ERR = 0
_AE_NOT_PERMITTED = -1743       # errAEEventNotPermitted
_AE_NEEDS_CONSENT = -1744       # errAEEventWouldRequireUserConsent
_AE_PROC_NOT_FOUND = -600       # procNotFound: target not running


def _read_defaults(domain: str, key: str) -> Optional[str]:
    """``defaults read domain key`` (never prompts); None when unset."""
    proc = subprocess.run(["/usr/bin/defaults", "read", domain, key],
                          capture_output=True, text=True, timeout=5, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


# ---- checks -------------------------------------------------------------------

MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"
MAIL_DIR = Path.home() / "Library" / "Mail"


def check_full_disk_access() -> CheckResult:
    """Open chat.db and list ~/Library/Mail read-only. FDA never prompts."""
    denied = found = False
    probes: list[tuple[Path, Callable[[Path], Any]]] = [
        (MESSAGES_DB, lambda p: os.close(os.open(p, os.O_RDONLY))),
        (MAIL_DIR, lambda p: os.listdir(p)),
    ]
    for path, probe in probes:
        try:
            probe(path)
            found = True
        except FileNotFoundError:
            continue
        except PermissionError:
            denied = True
        except OSError:
            continue
    if found and not denied:
        return _result(GRANTED)
    if denied:
        return _result(DENIED, "can't read ~/Library/Messages or ~/Library/Mail")
    return _result(UNKNOWN, "neither chat.db nor ~/Library/Mail exists")


def check_screen_recording() -> CheckResult:
    try:
        ok = _cg_preflight_screen_capture()
    except Exception as exc:  # pyobjc missing, etc.
        return _result(UNKNOWN, f"can't check: {exc}")
    if ok is None:
        return _result(UNKNOWN, "CGPreflightScreenCaptureAccess unavailable")
    # Preflight can't tell "denied" from "never asked"; both mean "not granted".
    return _result(GRANTED) if ok else _result(
        DENIED, "not granted (macOS doesn't say whether it was asked)")


def check_automation(bundle_id: str, app: str) -> CheckResult:
    try:
        code = _ae_determine(bundle_id, ask=False)
    except Exception as exc:
        return _result(UNKNOWN, f"can't check: {exc}")
    if code == _AE_NO_ERR:
        return _result(GRANTED)
    if code == _AE_NOT_PERMITTED:
        return _result(DENIED)
    if code == _AE_NEEDS_CONSENT:
        return _result(NOT_ASKED)
    if code == _AE_PROC_NOT_FOUND:
        return _result(UNKNOWN, f"open {app} to check")
    return _result(UNKNOWN, f"OSStatus {code}")


CHROME_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


def check_chrome_js() -> CheckResult:
    """Chrome's ``browser.allow_javascript_apple_events`` in the last-used
    profile's Preferences. Never runs JavaScript."""
    try:
        state = json.loads((CHROME_DIR / "Local State").read_text("utf-8"))
        profile = (state.get("profile") or {}).get("last_used") or "Default"
        prefs = json.loads((CHROME_DIR / profile / "Preferences").read_text("utf-8"))
    except FileNotFoundError:
        return _result(UNKNOWN, "Chrome profile not found")
    except (OSError, ValueError) as exc:
        return _result(UNKNOWN, f"can't read Chrome preferences: {exc}")
    value = (prefs.get("browser") or {}).get("allow_javascript_apple_events")
    if value is True:
        return _result(GRANTED)
    if value is False:
        return _result(DENIED)
    return _result(UNKNOWN, "not set in Chrome's preferences (off by default)")


def check_safari_js() -> CheckResult:
    try:
        value = _read_defaults("com.apple.Safari", "AllowJavaScriptFromAppleEvents")
    except Exception as exc:
        return _result(UNKNOWN, f"can't read Safari defaults: {exc}")
    if value == "1":
        return _result(GRANTED)
    if value == "0":
        return _result(DENIED)
    return _result(UNKNOWN, "not set in Safari's defaults (off by default)")


# Hook for #60: the menu bar app owns EventKit, so it reports Calendar status.
_external_checks: dict[str, Callable[[], CheckResult]] = {}
_external_requests: dict[str, Callable[[], dict[str, Any]]] = {}


def set_external_check(perm_id: str, fn: Optional[Callable[[], CheckResult]]) -> None:
    """Let another component (the menu bar app via #60) supply a row's status.
    ``fn`` must not prompt. ``None`` removes it."""
    if fn is None:
        _external_checks.pop(perm_id, None)
    else:
        _external_checks[perm_id] = fn


def set_external_request(perm_id: str, fn: Optional[Callable[[], dict[str, Any]]]) -> None:
    """Let another component handle a row's Request click. ``None`` removes it."""
    if fn is None:
        _external_requests.pop(perm_id, None)
    else:
        _external_requests[perm_id] = fn


def check_calendars() -> CheckResult:
    fn = _external_checks.get("calendars")
    if fn is None:
        return _result(UNKNOWN, "provided by the menu bar app (EventKit, #60)")
    return fn()


# ---- requests (only on an explicit user click) --------------------------------

def _opened(url: str, detail: str = "") -> dict[str, Any]:
    _open_url(url)
    return {"action": "opened_settings", "url": url, "detail": detail}


def request_full_disk_access() -> dict[str, Any]:
    return _opened(SETTINGS_URLS["full_disk_access"],
                   "macOS can't prompt for Full Disk Access; add the process below")


def request_screen_recording() -> dict[str, Any]:
    try:
        if _cg_request_screen_capture():
            return {"action": "prompted", "detail": "granted"}
    except Exception as exc:
        logger.info("CGRequestScreenCaptureAccess failed: %s", exc)
    return _opened(SETTINGS_URLS["screen_recording"])


def request_automation(bundle_id: str, app: str) -> dict[str, Any]:
    code = _ae_determine(bundle_id, ask=True)
    if code == _AE_NO_ERR:
        return {"action": "prompted", "detail": "granted"}
    if code == _AE_PROC_NOT_FOUND:
        return {"action": "none", "detail": f"open {app} first, then click Request"}
    return _opened(SETTINGS_URLS["automation"],
                   f"turn on {app} under this process in Automation")


def _instructions(text: str) -> Callable[[], dict[str, Any]]:
    return lambda: {"action": "instructions", "detail": text}


def request_calendars() -> dict[str, Any]:
    fn = _external_requests.get("calendars")
    if fn is not None:
        return fn()
    return _opened(SETTINGS_URLS["calendars"],
                   "Calendar access moves to the menu bar app (#60)")


# ---- the table ---------------------------------------------------------------

@dataclass
class Permission:
    id: str
    name: str
    why: str
    used_by: tuple[str, ...]
    check: Callable[[], CheckResult]
    request: Callable[[], dict[str, Any]]
    # "request": a click may prompt; "settings": opens System Settings;
    # "instructions": macOS has no API, the detail says what to do.
    request_kind: str = "request"


def _automation(perm_id: str, app: str, bundle_id: str, why: str,
                used_by: tuple[str, ...]) -> Permission:
    return Permission(
        id=perm_id, name=f"Automation → {app}", why=why, used_by=used_by,
        check=lambda: check_automation(bundle_id, app),
        request=lambda: request_automation(bundle_id, app),
    )


PERMISSIONS: list[Permission] = [
    Permission(
        id="full_disk_access", name="Full Disk Access",
        why="Read new iMessages (chat.db) and Mail messages.",
        used_by=("events/imessage_watcher.py", "events/mail_watcher.py",
                 "tools/read_email.py"),
        check=lambda: check_full_disk_access(),
        request=lambda: request_full_disk_access(), request_kind="settings",
    ),
    Permission(
        id="screen_recording", name="Screen Recording",
        why="See window titles when listing open windows.",
        used_by=("tools/windows.py",),
        check=lambda: check_screen_recording(),
        request=lambda: request_screen_recording(),
    ),
    Permission(
        id="calendars", name="Calendars",
        why="List and create calendar events (provided by the menu bar app).",
        used_by=("tools/calendar_access.py", "tools/list_events.py",
                 "tools/create_event.py", "events/calendar_watcher.py"),
        check=lambda: check_calendars(),
        request=lambda: request_calendars(), request_kind="settings",
    ),
    _automation("automation_reminders", "Reminders", "com.apple.reminders",
                "List and create reminders (AGENT_REMINDERS_BACKEND=applescript).",
                ("tools/reminders.py", "tools/calendar_access.py")),
    _automation("automation_calendar", "Calendar", "com.apple.iCal",
                "AppleScript fallback for calendar events until #60.",
                ("tools/calendar_access.py",)),
    _automation("automation_notes", "Notes", "com.apple.Notes",
                "Create and append to notes.", ("tools/notes.py",)),
    _automation("automation_mail", "Mail", "com.apple.mail",
                "Open email drafts in Mail (drafts only; nothing is sent).",
                ("tools/email_draft.py",)),
    _automation("automation_chrome", "Google Chrome", "com.google.Chrome",
                "Read the active Chrome tab.", ("tools/read_active_tab.py",)),
    _automation("automation_safari", "Safari", "com.apple.Safari",
                "Read the active Safari tab.", ("tools/read_active_tab.py",)),
    _automation("automation_system_events", "System Events",
                "com.apple.systemevents",
                "List running apps (generated recent_apps_list tool).",
                ("tools/generated/recent_apps_list.py",)),
    Permission(
        id="browser_js_chrome", name="Chrome: Allow JavaScript from Apple Events",
        why="Read page text from the active Chrome tab (not a macOS permission).",
        used_by=("tools/read_active_tab.py",),
        check=lambda: check_chrome_js(),
        request=_instructions(
            "In Chrome: View → Developer → Allow JavaScript from Apple Events."),
        request_kind="instructions",
    ),
    Permission(
        id="browser_js_safari", name="Safari: Allow JavaScript from Apple Events",
        why="Read page text from the active Safari tab (not a macOS permission).",
        used_by=("tools/read_active_tab.py",),
        check=lambda: check_safari_js(),
        request=_instructions(
            "In Safari: Settings → Advanced → Show features for web developers, "
            "then Develop → Allow JavaScript from Apple Events."),
        request_kind="instructions",
    ),
]
# Not listed: Automation → Messages. tools/send_imessage.py is not a
# registered tool (the user doesn't want it); add a row if it ever is.

_BY_ID = {p.id: p for p in PERMISSIONS}


def get(perm_id: str) -> Optional[Permission]:
    return _BY_ID.get(perm_id)


# ---- process attribution ------------------------------------------------------

def _pid_path(libc: Any, pid: int) -> Optional[str]:
    buf = ctypes.create_string_buffer(4096)
    libc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    libc.proc_pidpath.restype = ctypes.c_int
    n = libc.proc_pidpath(pid, buf, 4096)
    return buf.value.decode("utf-8", "replace") if n > 0 else None


def _responsible_process() -> Optional[str]:
    """Path of the process TCC attributes our permissions to (private but
    stable libSystem call; None when unavailable)."""
    try:
        libc = ctypes.CDLL(None)
        fn = libc.responsibility_get_pid_responsible_for_pid
        fn.argtypes = [ctypes.c_int]
        fn.restype = ctypes.c_int
        pid = fn(os.getpid())
        return _pid_path(libc, pid) if pid > 0 else None
    except Exception:
        return None


def process_info() -> dict[str, Any]:
    return {
        "executable": os.path.realpath(sys.executable),
        "responsible_process": _responsible_process(),
        "note": ("macOS attributes these permissions to the responsible "
                 "process; grant that entry in System Settings."),
    }


# ---- public API ---------------------------------------------------------------

def _row(perm: Permission) -> dict[str, Any]:
    try:
        res = perm.check()
    except Exception as exc:
        logger.exception("permission check %s failed", perm.id)
        res = _result(UNKNOWN, f"check failed: {exc}")
    status = res.get("status")
    return {
        "id": perm.id, "name": perm.name, "why": perm.why,
        "used_by": list(perm.used_by), "request_kind": perm.request_kind,
        "status": status if status in STATUSES else UNKNOWN,
        "detail": res.get("detail", ""),
    }


def check_all() -> list[dict[str, Any]]:
    """Status of every permission. Never prompts."""
    return [_row(p) for p in PERMISSIONS]


def request(perm_id: str) -> dict[str, Any]:
    """Prompt for, or open Settings for, one permission. Only on a user click.
    Raises KeyError for an unknown id."""
    perm = _BY_ID[perm_id]
    outcome = perm.request()
    return {"id": perm_id, **outcome, "permission": _row(perm)}
