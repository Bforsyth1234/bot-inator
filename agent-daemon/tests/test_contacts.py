"""Tests for #127: find_contact (read-only) through the menu bar app."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any, Optional

import pytest
from pydantic import TypeAdapter, ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai  # noqa: F401,E402  - must precede ``tools`` (circular import)
from ai import privacy  # noqa: E402
from schemas import ws_messages as wsm  # noqa: E402
from tools import contacts  # noqa: E402

_ADAPTER: TypeAdapter = TypeAdapter(wsm.WSMessage)
_TS = "2026-11-18T12:00:00.000Z"

SAM_A = {"name": "Sam Abel", "emails": ["sam.abel@example.com"],
         "phones": ["+1 555 0100"], "organization": "Abel & Co"}
SAM_B = {"name": "Sam Baker", "emails": ["sam@baker.example"],
         "phones": [], "organization": ""}


class FakeBridge:
    """Stands in for the WS bridge: records requests, returns ``reply``."""

    def __init__(self, reply: Optional[dict[str, Any]] = None, *,
                 connected: bool = True, authorization: Optional[str] = "authorized",
                 raises: Optional[Exception] = None) -> None:
        self.reply = reply if reply is not None else {"ok": True, "contacts": []}
        self._connected = connected
        self._authorization = authorization
        self.raises = raises
        self.calls: list[tuple[str, str, int]] = []

    def connected(self) -> bool:
        return self._connected

    def authorization(self) -> Optional[str]:
        return self._authorization if self._connected else None

    def request(self, op: str, name: str = "", limit: int = 5,
                timeout: Optional[float] = None) -> dict[str, Any]:
        self.calls.append((op, name, limit))
        if self.raises is not None:
            raise self.raises
        return {"request_id": "x", **self.reply}


@pytest.fixture
def use(monkeypatch):
    def install(fake: FakeBridge) -> FakeBridge:
        monkeypatch.setattr(contacts, "bridge", fake)
        return fake
    return install


@pytest.fixture
def cloud(monkeypatch):
    """Main engine in the cloud; the personal-text opt-in is the argument."""
    def install(opt_in: bool) -> None:
        monkeypatch.setattr(privacy, "role_is_cloud", lambda role: True)
        monkeypatch.setattr(privacy, "personal_text_allowed", lambda: opt_in)
    return install


# ---- the allow-list (reply parser) ----------------------------------------------

def test_parser_drops_every_field_outside_the_allow_list() -> None:
    raw = [{**SAM_A, "note": "door code 4411", "birthday": "1990-02-03",
            "postal_addresses": [{"street": "1 Main St"}], "image_data": "AAAA",
            "identifier": "ABCD-1234", "urls": ["https://example.com"]}]
    parsed = contacts.parse_contacts(raw)
    assert parsed == [SAM_A]
    assert set(parsed[0]) == set(contacts.ALLOWED_FIELDS)


def test_tool_result_never_carries_fields_outside_the_allow_list(use) -> None:
    use(FakeBridge({"ok": True, "contacts": [
        {**SAM_A, "note": "door code 4411", "birthday": "1990-02-03",
         "postal_addresses": [{"street": "1 Main St"}], "image_data": "AAAA"}]}))
    result = contacts.find_contact_impl("Sam")
    assert result["contacts"] == [SAM_A]
    blob = json.dumps(result)
    for leaked in ("4411", "1990-02-03", "Main St", "AAAA", "note", "birthday"):
        assert leaked not in blob


def test_parser_drops_junk_and_nested_values() -> None:
    raw = ["Sam", None, {"note": "only a note"},
           {"name": {"first": "Sam"}, "emails": "sam@example.com",
            "phones": [{"label": "home", "value": "1"}, 5, " +1 555 0100 "],
            "organization": None}]
    assert contacts.parse_contacts(raw) == [
        {"name": "", "emails": [], "phones": ["+1 555 0100"], "organization": ""}]
    assert contacts.parse_contacts(None) == []
    assert contacts.parse_contacts({"name": "Sam"}) == []


def test_parser_caps_the_number_of_contacts_and_values() -> None:
    many = [{"name": f"Sam {i}", "emails": [f"s{j}@example.com" for j in range(30)]}
            for i in range(25)]
    assert len(contacts.parse_contacts(many, limit=3)) == 3
    parsed = contacts.parse_contacts(many, limit=500)
    assert len(parsed) == contacts.MAX_LIMIT
    assert len(parsed[0]["emails"]) == contacts.MAX_VALUES_PER_FIELD


# ---- no listing of the address book ------------------------------------------------

@pytest.mark.parametrize("name", ["S", " s ", "", "   ", "\n", None, 7])
def test_short_name_is_refused_and_nothing_is_asked(use, name) -> None:
    fake = use(FakeBridge({"ok": True, "contacts": [SAM_A]}))
    result = contacts.find_contact_impl(name)
    assert result["status"] == "error"
    assert result["reason"] == "name_too_short"
    assert result["contacts"] == []
    assert fake.calls == []


def test_two_characters_are_enough_and_the_name_is_trimmed(use) -> None:
    fake = use(FakeBridge({"ok": True, "contacts": [SAM_A]}))
    assert contacts.find_contact_impl("  Sa  ")["status"] == "ok"
    assert fake.calls == [("find", "Sa", 5)]


@pytest.mark.parametrize("given,sent", [(0, 1), (-4, 1), (1, 1), (7, 7), (10, 10),
                                        (11, 10), (10_000, 10), ("x", 5), (None, 5)])
def test_limit_is_clamped(use, given, sent) -> None:
    fake = use(FakeBridge({"ok": True, "contacts": []}))
    contacts.find_contact_impl("Sam", given)
    assert fake.calls == [("find", "Sam", sent)]


def test_app_sending_more_than_the_limit_is_cut(use) -> None:
    use(FakeBridge({"ok": True, "contacts": [
        {"name": f"Sam {i}", "emails": [f"s{i}@example.com"]} for i in range(40)]}))
    assert contacts.find_contact_impl("Sam", 3)["count"] == 3
    assert len(contacts.find_contact_impl("Sam", 99)["contacts"]) == contacts.MAX_LIMIT


# ---- matches ---------------------------------------------------------------------------

def test_one_match_has_no_ask_which_instruction(use) -> None:
    use(FakeBridge({"ok": True, "contacts": [SAM_A]}))
    result = contacts.find_contact_impl("Sam Abel")
    assert result == {"status": "ok", "query": "Sam Abel", "count": 1,
                      "contacts": [SAM_A], "truncated": False}


def test_two_matches_return_both_with_the_ask_which_instruction(use) -> None:
    use(FakeBridge({"ok": True, "contacts": [SAM_A, SAM_B], "truncated": True}))
    result = contacts.find_contact_impl("Sam")
    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["contacts"] == [SAM_A, SAM_B]
    assert result["truncated"] is True
    assert result["instruction"] == contacts.ASK_WHICH
    assert "ask which one" in result["instruction"]
    assert "Do not pick" in result["instruction"]


def test_tool_description_tells_the_model_to_ask_which_one() -> None:
    described = getattr(contacts.find_contact, "description", None) or contacts.find_contact.__doc__
    assert "do NOT choose one" in described
    assert "ask which one" in described


def test_no_match_says_not_to_guess(use) -> None:
    use(FakeBridge({"ok": True, "contacts": []}))
    result = contacts.find_contact_impl("Zebedee")
    assert result["count"] == 0 and result["contacts"] == []
    assert result["instruction"] == contacts.NO_MATCH


# ---- unavailable: never a guess ----------------------------------------------------------

def _assert_unavailable(result: dict[str, Any], reason: str) -> None:
    assert result["status"] == "unavailable"
    assert result["reason"] == reason
    assert result["error"].startswith("unavailable:")
    assert result["contacts"] == [] and result["count"] == 0
    assert "do not guess" in result["instruction"]


def test_app_not_connected_is_unavailable(use) -> None:
    fake = use(FakeBridge({"ok": True, "contacts": [SAM_A]}, connected=False))
    result = contacts.find_contact_impl("Sam")
    _assert_unavailable(result, "app_not_connected")
    assert "the menu bar app is not running" in result["error"]
    assert fake.calls == []


def test_real_bridge_with_no_connection_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(contacts, "bridge", contacts.ContactsBridge())
    result = contacts.find_contact_impl("Sam")
    _assert_unavailable(result, "app_not_connected")
    assert "the menu bar app is not running" in result["error"]


def test_timeout_is_unavailable(use) -> None:
    use(FakeBridge(raises=contacts.ContactsError("timeout", contacts.TIMEOUT)))
    _assert_unavailable(contacts.find_contact_impl("Sam"), "timeout")


@pytest.mark.parametrize("auth", ["denied", "restricted"])
def test_access_denied_is_unavailable_without_asking(use, auth) -> None:
    fake = use(FakeBridge({"ok": True, "contacts": [SAM_A]}, authorization=auth))
    _assert_unavailable(contacts.find_contact_impl("Sam"), "access_denied")
    assert fake.calls == []


def test_failed_reply_is_unavailable_and_its_contacts_are_ignored(use) -> None:
    use(FakeBridge({"ok": False, "error": "Contacts error: boom", "contacts": [SAM_A]}))
    result = contacts.find_contact_impl("Sam")
    _assert_unavailable(result, "failed")
    assert "boom" in result["error"]

    use(FakeBridge({"ok": False, "error": "x", "authorization": "denied"},
                   authorization="not_determined"))
    _assert_unavailable(contacts.find_contact_impl("Sam"), "access_denied")


# ---- the real bridge over a fake socket ---------------------------------------------------

class _Loop:
    """An event loop on its own thread, as the daemon's is for a tool thread."""

    def __enter__(self) -> asyncio.AbstractEventLoop:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        return self.loop

    def __exit__(self, *exc: Any) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


def _app(bridge: contacts.ContactsBridge, conn_id: str, reply: Optional[dict[str, Any]],
         sent: list[tuple]) -> contacts.ContactsSend:
    """A fake menu bar app: answers each request with ``reply`` (None: silence)."""
    async def send(request_id: str, op: str, name: str, limit: int, timeout_seconds: int) -> None:
        sent.append((op, name, limit, timeout_seconds))
        if reply is not None:
            bridge.submit_response(conn_id, {"request_id": request_id, **reply})
    return send


def test_bridge_roundtrip_through_main_py_hooks(monkeypatch) -> None:
    bridge = contacts.ContactsBridge()
    monkeypatch.setattr(contacts, "bridge", bridge)
    sent: list[tuple] = []
    with _Loop() as loop:
        contacts.on_connect("c1", _app(bridge, "c1", {
            "ok": True, "authorization": "authorized",
            "contacts": [{**SAM_A, "note": "secret"}, SAM_B]}, sent), loop)
        assert not bridge.connected()  # not a contacts client until it says so
        contacts.on_status("c1", "authorized")
        assert bridge.connected() and bridge.authorization() == "authorized"
        result = contacts.find_contact_impl("Sam", 4)
        assert sent == [("find", "Sam", 4, 10)]
        assert result["contacts"] == [SAM_A, SAM_B]
        assert result["instruction"] == contacts.ASK_WHICH
        contacts.on_disconnect("c1")
        _assert_unavailable(contacts.find_contact_impl("Sam"), "app_not_connected")


def test_bridge_times_out_when_the_app_does_not_answer(monkeypatch) -> None:
    bridge = contacts.ContactsBridge()
    monkeypatch.setattr(contacts, "bridge", bridge)
    monkeypatch.setattr(contacts, "REQUEST_TIMEOUT_S", 0.2)
    sent: list[tuple] = []
    with _Loop() as loop:
        bridge.connect("c1", _app(bridge, "c1", None, sent), loop)
        bridge.set_status("c1", "authorized")
        result = contacts.find_contact_impl("Sam")
        assert len(sent) == 1
        _assert_unavailable(result, "timeout")
        assert bridge._pending == {}


def test_bridge_ignores_an_answer_from_another_connection() -> None:
    bridge = contacts.ContactsBridge()
    sent: list[tuple] = []
    with _Loop() as loop:
        async def send(request_id: str, op: str, name: str, limit: int, t: int) -> None:
            sent.append(request_id)
            # A second client answers a request that was not sent to it.
            assert bridge.submit_response("other", {
                "request_id": request_id, "ok": True, "contacts": [SAM_A]}) is False
        bridge.connect("c1", send, loop)
        bridge.connect("other", send, loop)
        bridge.set_status("c1", "authorized")
        with pytest.raises(contacts.ContactsError) as err:
            bridge.request("find", "Sam", 5, timeout=0.2)
        assert err.value.code == "timeout" and len(sent) == 1


def test_bridge_waits_longer_while_the_prompt_is_open() -> None:
    bridge = contacts.ContactsBridge()
    sent: list[tuple] = []
    with _Loop() as loop:
        bridge.connect("c1", _app(bridge, "c1", {"ok": True, "authorization": "authorized",
                                                 "contacts": []}, sent), loop)
        bridge.set_status("c1", "not_determined")
        bridge.request("find", "Sam", 5)
        assert bridge.authorization() == "authorized"  # the reply's status is kept
        bridge.request("find", "Sam", 5)
        bridge.request("request_access")
    assert [s[3] for s in sent] == [120, 10, 120]


# ---- Permissions row ------------------------------------------------------------------------

def test_permission_check_maps_the_apps_status(use) -> None:
    use(FakeBridge(connected=False))
    assert contacts.permission_check()["status"] == "unknown"
    for auth, status in [("authorized", "granted"), ("not_determined", "not_asked"),
                         ("denied", "denied"), ("restricted", "denied")]:
        use(FakeBridge(authorization=auth))
        assert contacts.permission_check()["status"] == status
    assert contacts.permission_check()["detail"] != ""
    use(FakeBridge(authorization="authorized"))
    assert contacts.permission_check() == {"status": "granted", "detail": ""}


def test_permission_request_prompts_only_when_it_can(use) -> None:
    fake = use(FakeBridge(connected=False))
    assert contacts.permission_request()["action"] == "none"
    fake = use(FakeBridge(authorization="denied"))
    assert contacts.permission_request()["action"] == "none"
    assert fake.calls == []
    fake = use(FakeBridge({"ok": True, "authorization": "authorized"},
                          authorization="not_determined"))
    assert contacts.permission_request() == {"action": "prompted", "detail": "authorized"}
    assert fake.calls == [("request_access", "", 5)]


def test_register_permission_hooks_points_the_contacts_row_at_the_app() -> None:
    from ai import permissions

    try:
        contacts.register_permission_hooks()
        assert permissions._external_checks["contacts"] is contacts.permission_check
        assert permissions._external_requests["contacts"] is contacts.permission_request
    finally:
        permissions.set_external_check("contacts", None)
        permissions.set_external_request("contacts", None)


# ---- personal text under a cloud engine (#31 / #52) ----------------------------------------

def _two() -> dict[str, Any]:
    return {"status": "ok", "query": "Sam", "count": 2, "contacts": [dict(SAM_A), dict(SAM_B)],
            "truncated": False, "instruction": contacts.ASK_WHICH}


def test_cloud_role_without_opt_in_gets_the_placeholder(cloud) -> None:
    cloud(False)
    original = _two()
    out = contacts.redact_result(original, "main")
    p = privacy.PLACEHOLDER
    assert out["contacts"] == [
        {"name": p, "emails": [p], "phones": [p], "organization": p},
        {"name": p, "emails": [p], "phones": [], "organization": ""},
    ]
    assert out["note"] == contacts.CLOUD_NOTE
    assert out["count"] == 2 and out["instruction"] == contacts.ASK_WHICH
    blob = json.dumps(out)
    for personal in ("Abel", "Baker", "example", "555"):
        assert personal not in blob
    assert original == _two()  # the caller's result is not changed


def test_cloud_role_with_opt_in_and_local_role_are_unchanged(cloud, monkeypatch) -> None:
    cloud(True)
    assert contacts.redact_result(_two(), "main") == _two()
    monkeypatch.setattr(privacy, "role_is_cloud", lambda role: False)
    monkeypatch.setattr(privacy, "personal_text_allowed", lambda: False)
    assert contacts.redact_result(_two(), "main") == _two()
    assert contacts.redact_result(_two(), None) == _two()


def test_redaction_leaves_errors_and_empty_results_alone(cloud) -> None:
    cloud(False)
    unavailable = contacts._unavailable("timeout", contacts.TIMEOUT)
    assert contacts.redact_result(unavailable, "main") == unavailable
    assert contacts.redact_result("text", "main") == "text"


# ---- registration facts for the runner -------------------------------------------------------

def test_module_declares_its_read_only_tool() -> None:
    assert contacts.READ_ONLY == frozenset({"find_contact"})
    assert callable(contacts.find_contact)


# ---- WS schema round trips ---------------------------------------------------------------------

def _roundtrip(message) -> None:
    raw = message.model_dump_json()
    parsed = _ADAPTER.validate_python(json.loads(raw))
    assert parsed == message
    assert type(parsed) is type(message)


def test_contacts_request_roundtrip() -> None:
    _roundtrip(wsm.ContactsRequest(seq=1, timestamp=_TS, payload=wsm.ContactsRequestPayload(
        request_id="con_1", op="find", name="Sam", limit=3, timeout_seconds=10)))
    _roundtrip(wsm.ContactsRequest(seq=2, timestamp=_TS, payload=wsm.ContactsRequestPayload(
        request_id="con_2", op="request_access")))
    raw = json.loads(wsm.ContactsRequest(seq=3, timestamp=_TS, payload=wsm.ContactsRequestPayload(
        request_id="con_3", name="Sam")).model_dump_json())
    assert raw == {"seq": 3, "timestamp": _TS, "type": "contacts_request",
                   "payload": {"request_id": "con_3", "op": "find", "name": "Sam",
                               "limit": 5, "timeout_seconds": 10}}


def test_contacts_request_has_no_list_op() -> None:
    with pytest.raises(ValidationError):
        wsm.ContactsRequestPayload(request_id="con_1", op="list")


def test_contacts_response_roundtrip() -> None:
    _roundtrip(wsm.ContactsResponse(seq=4, timestamp=_TS, payload=wsm.ContactsResponsePayload(
        request_id="con_1", ok=True, contacts=[SAM_A, SAM_B], truncated=True,
        authorization="authorized")))
    _roundtrip(wsm.ContactsResponse(seq=5, timestamp=_TS, payload=wsm.ContactsResponsePayload(
        request_id="con_1", ok=False, error="denied", authorization="denied")))
    # The frame the Swift app sends (snake_case keys, nulls left out).
    parsed = _ADAPTER.validate_python({
        "type": "contacts_response", "seq": 6, "timestamp": _TS,
        "payload": {"request_id": "con_9", "ok": True, "truncated": False,
                    "contacts": [SAM_A], "authorization": "authorized"}})
    assert isinstance(parsed, wsm.ContactsResponse)
    assert parsed.payload.contacts == [SAM_A]


def test_contacts_status_roundtrip() -> None:
    for auth in ("not_determined", "restricted", "denied", "authorized", "limited"):
        _roundtrip(wsm.ContactsStatus(seq=7, timestamp=_TS,
                                      payload=wsm.ContactsStatusPayload(authorization=auth)))
    with pytest.raises(ValidationError):
        wsm.ContactsStatusPayload(authorization="full_access")
    parsed = _ADAPTER.validate_python({"type": "contacts_status", "seq": 8, "timestamp": _TS,
                                       "payload": {"authorization": "not_determined"}})
    assert isinstance(parsed, wsm.ContactsStatus)
