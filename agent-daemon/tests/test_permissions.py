"""#59: permissions checklist — checks never prompt, status mapping, routes."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auth
from ai import permissions as P
from routes import permissions as routes_permissions


class Prompted(AssertionError):
    pass


@pytest.fixture()
def no_prompts(monkeypatch, tmp_path):
    """Every path that could prompt or open Settings raises; checks get
    harmless fakes. Returns the list of (bundle_id, ask) AE calls."""
    ae_calls: list[tuple[str, bool]] = []
    prompted: list[str] = []

    def fake_ae(bundle_id: str, ask: bool) -> int:
        ae_calls.append((bundle_id, ask))
        if ask:
            prompted.append(f"AE ask=True for {bundle_id}")
            raise Prompted(prompted[-1])
        return P._AE_NEEDS_CONSENT

    def boom(*_a, **_k):
        # Recorded, not just raised: _row() turns a check's exception into
        # "unknown", so an exception alone would go unnoticed.
        prompted.append("request path called")
        raise Prompted(prompted[-1])

    monkeypatch.setattr(P, "_ae_determine", fake_ae)
    monkeypatch.setattr(P, "_open_url", boom)
    monkeypatch.setattr(P, "_cg_request_screen_capture", boom)
    monkeypatch.setattr(P, "_cg_preflight_screen_capture", lambda: False)
    monkeypatch.setattr(P, "_read_defaults", lambda *_a: None)
    monkeypatch.setattr(P, "MESSAGES_DB", tmp_path / "missing.db")
    monkeypatch.setattr(P, "MAIL_DIR", tmp_path / "missing-mail")
    monkeypatch.setattr(P, "CHROME_DIR", tmp_path / "no-chrome")
    for perm in P.PERMISSIONS:
        monkeypatch.setattr(perm, "request", boom)
    return ae_calls, prompted


# ── checks never prompt ──────────────────────────────────────────────────

def test_check_all_never_prompts(no_prompts) -> None:
    ae_calls, prompted = no_prompts
    rows = P.check_all()
    assert prompted == []
    assert [r["id"] for r in rows] == [p.id for p in P.PERMISSIONS]
    assert all(r["status"] in P.STATUSES for r in rows)
    # Every Automation row was checked, and only with ask=False.
    automation = [p for p in P.PERMISSIONS if p.id.startswith("automation_")]
    assert len(ae_calls) == len(automation) > 0
    assert all(ask is False for _, ask in ae_calls)


def test_no_prompt_guard_is_not_vacuous(no_prompts, monkeypatch) -> None:
    """A check that calls request() (or AE with ask=True) is caught."""
    ae_calls, prompted = no_prompts
    notes = P.get("automation_notes")
    monkeypatch.setattr(notes, "check", lambda: P._ae_determine("com.apple.Notes", True)
                        and P._result(P.GRANTED))
    rows = {r["id"]: r for r in P.check_all()}
    # _row turns the exception into unknown; the recorded call is the evidence.
    assert ("com.apple.Notes", True) in ae_calls
    assert rows["automation_notes"]["status"] == P.UNKNOWN
    fda = P.get("full_disk_access")
    monkeypatch.setattr(fda, "check", lambda: fda.request())
    prompted.clear()
    P.check_all()
    assert sorted(prompted) == ["AE ask=True for com.apple.Notes", "request path called"]


def test_no_messages_row() -> None:
    assert P.get("automation_messages") is None
    assert all("send_imessage" not in " ".join(p.used_by) for p in P.PERMISSIONS)


# ── status mapping ───────────────────────────────────────────────────────

@pytest.mark.parametrize("code,status", [
    (0, P.GRANTED), (-1743, P.DENIED), (-1744, P.NOT_ASKED),
    (-600, P.UNKNOWN), (-50, P.UNKNOWN),
])
def test_automation_mapping(monkeypatch, code, status) -> None:
    calls = []
    monkeypatch.setattr(P, "_ae_determine", lambda b, ask: calls.append(ask) or code)
    assert P.check_automation("com.apple.Notes", "Notes")["status"] == status
    assert calls == [False]


def test_automation_not_running_says_open_app(monkeypatch) -> None:
    monkeypatch.setattr(P, "_ae_determine", lambda b, ask: -600)
    assert "open Notes" in P.check_automation("com.apple.Notes", "Notes")["detail"]


def test_automation_error_is_unknown(monkeypatch) -> None:
    def broken(b, ask):
        raise OSError("no CoreServices")
    monkeypatch.setattr(P, "_ae_determine", broken)
    assert P.check_automation("x", "X")["status"] == P.UNKNOWN


def test_full_disk_access_granted(monkeypatch, tmp_path) -> None:
    db = tmp_path / "chat.db"
    db.write_bytes(b"")
    monkeypatch.setattr(P, "MESSAGES_DB", db)
    monkeypatch.setattr(P, "MAIL_DIR", tmp_path / "nope")
    assert P.check_full_disk_access()["status"] == P.GRANTED


def test_full_disk_access_denied(monkeypatch, tmp_path) -> None:
    db, mail = tmp_path / "chat.db", tmp_path / "Mail"
    db.write_bytes(b"")
    mail.mkdir()
    db.chmod(0)
    mail.chmod(0)
    monkeypatch.setattr(P, "MESSAGES_DB", db)
    monkeypatch.setattr(P, "MAIL_DIR", mail)
    try:
        assert P.check_full_disk_access()["status"] == P.DENIED
    finally:
        db.chmod(0o600)
        mail.chmod(0o700)


def test_full_disk_access_nothing_there(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(P, "MESSAGES_DB", tmp_path / "a")
    monkeypatch.setattr(P, "MAIL_DIR", tmp_path / "b")
    assert P.check_full_disk_access()["status"] == P.UNKNOWN


@pytest.mark.parametrize("value,status", [(True, P.GRANTED), (False, P.DENIED),
                                          (None, P.UNKNOWN)])
def test_screen_recording_mapping(monkeypatch, value, status) -> None:
    monkeypatch.setattr(P, "_cg_preflight_screen_capture", lambda: value)
    assert P.check_screen_recording()["status"] == status


def _chrome(tmp_path, browser: dict[str, Any]) -> Any:
    (tmp_path / "Profile 1").mkdir()
    (tmp_path / "Local State").write_text(json.dumps({"profile": {"last_used": "Profile 1"}}))
    (tmp_path / "Profile 1" / "Preferences").write_text(json.dumps({"browser": browser}))
    return tmp_path


@pytest.mark.parametrize("browser,status", [
    ({"allow_javascript_apple_events": True}, P.GRANTED),
    ({"allow_javascript_apple_events": False}, P.DENIED),
    ({}, P.UNKNOWN),
])
def test_chrome_js_mapping(monkeypatch, tmp_path, browser, status) -> None:
    monkeypatch.setattr(P, "CHROME_DIR", _chrome(tmp_path, browser))
    assert P.check_chrome_js()["status"] == status


@pytest.mark.parametrize("value,status", [("1", P.GRANTED), ("0", P.DENIED),
                                          (None, P.UNKNOWN)])
def test_safari_js_mapping(monkeypatch, value, status) -> None:
    monkeypatch.setattr(P, "_read_defaults", lambda d, k: value)
    assert P.check_safari_js()["status"] == status


def test_calendars_unknown_until_menu_bar_app_reports() -> None:
    row = P._row(P.get("calendars"))
    assert row["status"] == P.UNKNOWN and "menu bar app" in row["detail"]
    P.set_external_check("calendars", lambda: P._result(P.GRANTED))
    try:
        assert P._row(P.get("calendars"))["status"] == P.GRANTED
    finally:
        P.set_external_check("calendars", None)


def test_bad_status_is_reported_unknown(monkeypatch) -> None:
    perm = P.get("screen_recording")
    monkeypatch.setattr(perm, "check", lambda: {"status": "yes"})
    assert P._row(perm)["status"] == P.UNKNOWN


def test_process_info_resolves_executable() -> None:
    info = P.process_info()
    assert info["executable"] and "/" in info["executable"]


# ── requests (mocked) ────────────────────────────────────────────────────

def test_request_automation_asks(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(P, "_ae_determine", lambda b, ask: calls.append((b, ask)) or 0)
    out = P.request("automation_notes")
    assert ("com.apple.Notes", True) in calls and out["action"] == "prompted"


def test_request_automation_denied_opens_settings(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(P, "_ae_determine", lambda b, ask: -1743)
    monkeypatch.setattr(P, "_open_url", opened.append)
    assert P.request("automation_chrome")["action"] == "opened_settings"
    assert opened == [P.SETTINGS_URLS["automation"]]


def test_request_full_disk_access_opens_pane(monkeypatch, tmp_path) -> None:
    opened = []
    monkeypatch.setattr(P, "_open_url", opened.append)
    monkeypatch.setattr(P, "MESSAGES_DB", tmp_path / "a")
    monkeypatch.setattr(P, "MAIL_DIR", tmp_path / "b")
    P.request("full_disk_access")
    assert opened == [P.SETTINGS_URLS["full_disk_access"]]


def test_request_screen_recording_falls_back_to_pane(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(P, "_cg_request_screen_capture", lambda: False)
    monkeypatch.setattr(P, "_cg_preflight_screen_capture", lambda: False)
    monkeypatch.setattr(P, "_open_url", opened.append)
    assert P.request("screen_recording")["action"] == "opened_settings"
    assert opened == [P.SETTINGS_URLS["screen_recording"]]


def test_request_browser_js_gives_instructions(monkeypatch) -> None:
    monkeypatch.setattr(P, "_read_defaults", lambda d, k: None)
    out = P.request("browser_js_safari")
    assert out["action"] == "instructions" and "Develop" in out["detail"]


# ── routes ───────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(routes_permissions.router)
    with TestClient(app) as c:
        yield c


def test_route_list(client, no_prompts) -> None:
    resp = client.get("/api/permissions", headers=auth.auth_header())
    assert resp.status_code == 200
    body = resp.json()
    assert {r["id"] for r in body["permissions"]} == {p.id for p in P.PERMISSIONS}
    assert body["process"]["executable"]


def test_route_request_calls_the_right_handler(client, monkeypatch) -> None:
    called = []
    for perm in P.PERMISSIONS:
        monkeypatch.setattr(perm, "request",
                            lambda pid=perm.id: called.append(pid) or {"action": "none"})
        monkeypatch.setattr(perm, "check", lambda: P._result(P.NOT_ASKED))
    resp = client.post("/api/permissions/automation_notes/request",
                       headers=auth.auth_header())
    assert resp.status_code == 200
    assert called == ["automation_notes"]
    assert resp.json()["permission"]["status"] == P.NOT_ASKED


def test_route_unknown_id_404(client) -> None:
    resp = client.post("/api/permissions/nope/request", headers=auth.auth_header())
    assert resp.status_code == 404


@pytest.mark.parametrize("method,path", [
    ("get", "/api/permissions"),
    ("post", "/api/permissions/automation_notes/request"),
])
def test_routes_need_token(client, monkeypatch, method, path) -> None:
    called = []
    for perm in P.PERMISSIONS:
        monkeypatch.setattr(perm, "request", lambda: called.append(1) or {})
    assert getattr(client, method)(path).status_code == 401
    assert called == []
