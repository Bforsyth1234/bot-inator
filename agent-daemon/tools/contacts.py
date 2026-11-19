"""Look a name up in the Mac's Contacts, through the menu bar app (todo #127).

One read-only tool (belongs in ``tools.READ_ONLY_TOOL_NAMES``):

* ``find_contact(name, limit=5)`` -- the contacts whose name matches, each
  as ``{name, emails, phones, organization}``.

The daemon's Python can't get the Contacts prompt (a bare interpreter has no
Info.plist usage string), so the menu bar app owns ``CNContactStore``
(``Services/ContactsBridge.swift``) and answers a ``contacts_request`` frame,
the way Calendar works (#60). ``main.py`` registers each WS connection with
:data:`bridge`; the app advertises itself with ``contacts_status`` (its
authorization, read without prompting).

Rules (#127 DECISIONS, binding):

* Read-only, and four fields only: name, email addresses, phone numbers,
  organisation. The app requests only those CNContact keys, and
  :func:`parse_contacts` keeps only those four whatever arrives: no notes,
  birthdays, postal addresses or photos can pass through this module.
* No listing of the address book: ``name`` must be at least
  ``MIN_NAME_CHARS`` characters after trimming and ``limit`` is clamped to
  1-``MAX_LIMIT``. There is no write op.
* The app is not connected, or doesn't answer in time: the result says
  "unavailable: the menu bar app is not running". Never a guess.
* More than one match: all of them come back with an ``instruction`` telling
  the model to ask the user which one. It may not pick silently.
* Under a cloud main engine a result is personal text: :func:`redact_result`
  swaps every field for the #31 placeholder unless the user opted in (7c).
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


READ_ONLY: frozenset[str] = frozenset({"find_contact"})

MIN_NAME_CHARS = 2
MAX_NAME_CHARS = 100
DEFAULT_LIMIT = 5
MAX_LIMIT = 10
# The only keys of a contact that may leave this module (decision 2).
ALLOWED_FIELDS: tuple[str, ...] = ("name", "emails", "phones", "organization")
MAX_VALUES_PER_FIELD = 10
MAX_VALUE_CHARS = 200

REQUEST_TIMEOUT_S = 10.0
# Before the user has answered the macOS prompt the first lookup waits on it.
PROMPT_TIMEOUT_S = 120.0

APP_NOT_CONNECTED = (
    "unavailable: the menu bar app is not running. Open the bot-inator menu "
    "bar app to look up contacts."
)
TIMEOUT = (
    "unavailable: the menu bar app didn't answer the contacts lookup in time."
)
ACCESS_DENIED = (
    "unavailable: Contacts access is off for the bot-inator menu bar app. "
    "Turn it on in System Settings > Privacy & Security > Contacts."
)
NAME_TOO_SHORT = (
    f"name must be at least {MIN_NAME_CHARS} characters; find_contact looks "
    "up one person and can't list the address book."
)
ASK_WHICH = (
    "More than one contact matches. Do not pick one yourself: show the user "
    "these names and ask which one they mean before using an address or number."
)
NO_MATCH = (
    "No contact matches that name. Ask the user for the address or number; "
    "do not guess one."
)
UNAVAILABLE_NOTE = (
    "The lookup did not run. Tell the user why and ask for the address or "
    "number; do not guess one."
)
CLOUD_NOTE = (
    "Contact details were kept on this Mac because this engine runs in the "
    "cloud (the user can allow sending them in Settings, or with "
    "AGENT_CLOUD_PERSONAL_TEXT=1)."
)

_DENIED = frozenset({"denied", "restricted"})


class ContactsError(RuntimeError):
    """A lookup that could not happen; ``code`` says which way."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---- bridge to the menu bar app --------------------------------------------

# send(request_id, op, name, limit, timeout_seconds), a coroutine run on the loop.
ContactsSend = Callable[[str, str, str, int, int], Awaitable[None]]


class _Conn:
    def __init__(self, conn_id: str, send: ContactsSend,
                 loop: asyncio.AbstractEventLoop) -> None:
        self.conn_id = conn_id
        self.send = send
        self.loop = loop
        self.authorization: Optional[str] = None  # None: not a contacts client
        self.advertised_at = 0


class ContactsBridge:
    """Request/response to the menu bar app over the WebSocket.

    Same shape as ``tools.calendar_access.AppCalendarBridge``: works from
    any thread except the loop's own; requests go to the ONE connection that
    most recently sent ``contacts_status``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conns: dict[str, _Conn] = {}
        self._pending: dict[str, tuple[str, concurrent.futures.Future]] = {}
        self._counter = itertools.count(1)

    # connection lifecycle (main.py, on the loop) ------------------------------

    def connect(self, conn_id: str, send: ContactsSend,
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
                fut.set_exception(ContactsError("app_not_connected", APP_NOT_CONNECTED))

    def set_status(self, conn_id: str, authorization: str) -> None:
        """``contacts_status`` arrived (main.py)."""
        with self._lock:
            conn = self._conns.get(conn_id)
            if conn is None:
                return
            conn.authorization = str(authorization)
            conn.advertised_at = next(self._counter)

    # state (never prompts) ------------------------------------------------------

    def _pick(self) -> Optional[_Conn]:
        with self._lock:
            capable = [c for c in self._conns.values() if c.authorization is not None]
        return max(capable, key=lambda c: c.advertised_at) if capable else None

    def connected(self) -> bool:
        return self._pick() is not None

    def authorization(self) -> Optional[str]:
        conn = self._pick()
        return conn.authorization if conn else None

    # request / response -----------------------------------------------------------

    def submit_response(self, conn_id: str, payload: dict[str, Any]) -> bool:
        """Resolve a pending request (main.py, on ``contacts_response``)."""
        request_id = str(payload.get("request_id") or "")
        with self._lock:
            entry = self._pending.get(request_id)
            if entry is not None and entry[0] == conn_id:
                self._pending.pop(request_id, None)
            else:
                entry = None  # unknown id, or another connection's request
            auth = payload.get("authorization")
            conn = self._conns.get(conn_id)
            if auth and conn is not None and conn.authorization is not None:
                conn.authorization = str(auth)
        if entry is None or entry[1].done():
            return False
        entry[1].set_result(payload)
        return True

    def request(self, op: str, name: str = "", limit: int = DEFAULT_LIMIT,
                timeout: Optional[float] = None) -> dict[str, Any]:
        """Send one ``contacts_request`` and wait for its answer.

        Raises :class:`ContactsError` (``app_not_connected``, ``timeout``,
        ``failed``); a reply with ``ok = false`` is returned, not raised.
        """
        conn = self._pick()
        if conn is None:
            raise ContactsError("app_not_connected", APP_NOT_CONNECTED)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is conn.loop:
            raise ContactsError("failed", "find_contact was called on the event loop")
        if timeout is None:
            prompt = op == "request_access" or conn.authorization == "not_determined"
            timeout = PROMPT_TIMEOUT_S if prompt else REQUEST_TIMEOUT_S
        request_id = f"con_{uuid.uuid4().hex[:12]}"
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending[request_id] = (conn.conn_id, fut)
        try:
            sent = asyncio.run_coroutine_threadsafe(
                conn.send(request_id, op, name, int(limit), max(1, int(round(timeout)))),
                conn.loop)
            sent.result(timeout=min(timeout, REQUEST_TIMEOUT_S))
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise ContactsError("timeout", TIMEOUT) from None
        except ContactsError:
            raise
        except Exception as exc:
            raise ContactsError(
                "app_not_connected",
                f"unavailable: couldn't reach the menu bar app ({exc})") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)


bridge = ContactsBridge()


# ---- wiring for main.py (mirrors calendar_access) -----------------------------

def on_connect(conn_id: str, send: ContactsSend, loop: asyncio.AbstractEventLoop) -> None:
    """A WS client connected; it serves Contacts once it sends ``contacts_status``."""
    bridge.connect(conn_id, send, loop)


def on_disconnect(conn_id: str) -> None:
    bridge.disconnect(conn_id)


def on_status(conn_id: str, authorization: str) -> None:
    """A ``contacts_status`` frame arrived."""
    bridge.set_status(conn_id, authorization)


def on_response(conn_id: str, payload: dict[str, Any]) -> bool:
    """A ``contacts_response`` frame arrived (``payload.model_dump()``)."""
    return bridge.submit_response(conn_id, payload)


# ---- Permissions row ``contacts`` ---------------------------------------------

def permission_check() -> dict[str, str]:
    """What the app reported for Contacts. Never prompts."""
    auth = bridge.authorization()
    if auth is None:
        return {"status": "unknown", "detail": "open the bot-inator menu bar app to check"}
    status = {"authorized": "granted", "not_determined": "not_asked",
              "denied": "denied", "restricted": "denied",
              "limited": "denied"}.get(auth, "unknown")
    detail = "" if status == "granted" else f"Contacts status in the menu bar app: {auth}"
    return {"status": status, "detail": detail}


def permission_request() -> dict[str, Any]:
    """Permissions "Request" click: the app shows the macOS Contacts prompt."""
    auth = bridge.authorization()
    if auth is None:
        return {"action": "none", "detail": "open the bot-inator menu bar app, then click Request"}
    if auth in _DENIED or auth == "limited":
        return {"action": "none", "detail": (
            f"Contacts access is {auth} for the menu bar app; change it in System Settings "
            "> Privacy & Security > Contacts")}
    try:
        reply = bridge.request("request_access")
    except ContactsError as exc:
        return {"action": "none", "detail": str(exc)}
    return {"action": "prompted", "detail": str(reply.get("authorization") or "")}


def register_permission_hooks() -> None:
    """Point the Permissions ``contacts`` row at the app (the #59 hook)."""
    from ai import permissions  # lazy: tools must not import the AI stack at import time

    permissions.set_external_check("contacts", permission_check)
    permissions.set_external_request("contacts", permission_request)


# ---- reply parser (the allow-list) ----------------------------------------------

def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_VALUE_CHARS]


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= MAX_VALUES_PER_FIELD:
            break
    return out


def parse_contacts(raw: Any, limit: int = MAX_LIMIT) -> list[dict[str, Any]]:
    """The app's ``contacts`` as ``[{name, emails, phones, organization}]``.

    Built field by field from :data:`ALLOWED_FIELDS`: any other key the app
    sends (notes, birthday, postal addresses, a photo, an identifier) is
    never copied. Entries that are not dicts, or carry none of the four
    fields, are dropped; at most ``limit`` (never more than ``MAX_LIMIT``).
    """
    cap = clamp_limit(limit)
    out: list[dict[str, Any]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        contact: dict[str, Any] = {
            "name": _clean(item.get("name")),
            "emails": _clean_list(item.get("emails")),
            "phones": _clean_list(item.get("phones")),
            "organization": _clean(item.get("organization")),
        }
        if not any(contact.values()):
            continue
        out.append(contact)
        if len(out) >= cap:
            break
    return out


def clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


# ---- personal text (#31 / #52) --------------------------------------------------

def redact_result(result: Any, role: Optional[str]) -> Any:
    """A ``find_contact`` result as the ``role`` engine may see it.

    Pure apart from ``ai.privacy.should_redact``: under a cloud ``role``
    without the personal-text opt-in every name, address, number and
    organisation becomes the placeholder (the count of matches stays);
    otherwise the result is returned unchanged.
    """
    from ai import privacy  # lazy: tools must not import the AI stack at import time

    if not isinstance(result, dict) or not privacy.should_redact(role):
        return result
    contacts = result.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        return result
    hidden: list[dict[str, Any]] = []
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        hidden.append({
            "name": privacy.PLACEHOLDER if contact.get("name") else "",
            "emails": [privacy.PLACEHOLDER for _ in contact.get("emails") or []],
            "phones": [privacy.PLACEHOLDER for _ in contact.get("phones") or []],
            "organization": privacy.PLACEHOLDER if contact.get("organization") else "",
        })
    out = dict(result)
    out["contacts"] = hidden
    out["note"] = CLOUD_NOTE
    return out


# ---- the tool -------------------------------------------------------------------

def _unavailable(code: str, message: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": code, "error": message,
            "count": 0, "contacts": [], "instruction": UNAVAILABLE_NOTE}


def find_contact_impl(name: Any, limit: Any = DEFAULT_LIMIT) -> dict[str, Any]:
    """Body of :func:`find_contact` (plain function, for tests)."""
    query = " ".join(name.split()) if isinstance(name, str) else ""
    if len(query) < MIN_NAME_CHARS:
        return {"status": "error", "reason": "name_too_short", "error": NAME_TOO_SHORT,
                "count": 0, "contacts": []}
    query = query[:MAX_NAME_CHARS]
    cap = clamp_limit(limit)
    if not bridge.connected():
        return _unavailable("app_not_connected", APP_NOT_CONNECTED)
    if bridge.authorization() in _DENIED:
        return _unavailable("access_denied", ACCESS_DENIED)
    try:
        reply = bridge.request("find", query, cap)
    except ContactsError as exc:
        return _unavailable(exc.code, str(exc))
    if not isinstance(reply, dict) or not reply.get("ok"):
        auth = reply.get("authorization") if isinstance(reply, dict) else None
        if auth in _DENIED or auth == "not_determined":
            return _unavailable("access_denied", ACCESS_DENIED)
        detail = _clean(reply.get("error")) if isinstance(reply, dict) else ""
        return _unavailable(
            "failed", "unavailable: the menu bar app couldn't read Contacts"
            + (f" ({detail})" if detail else "") + ".")

    contacts = parse_contacts(reply.get("contacts"), cap)
    result: dict[str, Any] = {
        "status": "ok", "query": query, "count": len(contacts), "contacts": contacts,
        "truncated": bool(reply.get("truncated")),
    }
    if len(contacts) > 1:
        result["instruction"] = ASK_WHICH
    elif not contacts:
        result["instruction"] = NO_MATCH
    return result


@tool
def find_contact(name: str, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Look a person up in the user's Contacts by name, to get an email address or phone number.

    Use it before drafting an email or a message to someone the user names
    ("draft an email to Sam") when you don't already have the address. It
    only reads. If more than one contact matches, do NOT choose one: show the
    user the matching names and ask which one they mean. If nothing matches,
    or the result is "unavailable", ask the user for the address; never
    guess or invent one.

    Args:
        name: The person's name or part of it (at least 2 characters). It
            cannot be empty: this tool can't list the address book.
        limit: Maximum number of matches to return (default 5, at most 10).

    Returns:
        ``{"status": "ok", "count", "contacts": [{"name", "emails",
        "phones", "organization"}], "truncated"}``, plus ``instruction``
        when there are several matches (ask the user which one) or none.
        ``{"status": "unavailable", "error"}`` when the menu bar app is not
        running, didn't answer, or has no Contacts access;
        ``{"status": "error", "error"}`` when ``name`` is too short.
    """
    return find_contact_impl(name, limit)
