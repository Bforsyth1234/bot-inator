"""#27: Origin check and bearer token on every HTTP route and the WS."""
from __future__ import annotations

import os
import stat
from unittest.mock import AsyncMock, patch

import pytest

os.environ["AGENT_DISABLE_LISTENERS"] = "1"
os.environ["AGENT_EAGER_LOAD"] = "0"

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

import auth  # noqa: E402

EVIL = {"Origin": "https://evil.example"}


@pytest.fixture()
def client():
    """A client that sends NO token unless a test adds one."""
    with patch("ai.orchestrator.Orchestrator._build_agent", return_value=None), patch(
        "ai.mlx_engine.MLXEngine.evaluate_event", new_callable=AsyncMock, return_value="",
    ), patch("ai.mlx_engine.MLXEngine.generate_chat_sync", return_value="NO_PATTERN"):
        from main import app

        with TestClient(app) as test_client:
            yield test_client


def _good() -> dict[str, str]:
    return auth.auth_header()


# ── WebSocket ────────────────────────────────────────────────────────────

def test_ws_with_evil_origin_is_refused_even_with_token(client) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/stream", headers={**_good(), **EVIL}):
            pass
    assert exc.value.code == 1008


def test_ws_without_origin_with_token_is_accepted(client) -> None:
    with client.websocket_connect("/ws/stream", headers=_good()) as ws:
        assert ws.receive_json()["type"] == "status"


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": "Basic abc"},
])
def test_ws_without_valid_token_is_refused(client, headers) -> None:
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws/stream", headers=headers):
            pass
    assert exc.value.code == 1008


# ── HTTP ─────────────────────────────────────────────────────────────────

# ``_can_self_restart`` is False so an accepted call answers 409 and never
# schedules a re-exec of the test process (e.g. with a check dropped).
def test_restart_with_origin_is_403(client) -> None:
    with patch("main._can_self_restart", return_value=False):
        resp = client.post("/api/restart", headers={**_good(), **EVIL})
        assert resp.status_code == 403
        assert client.post("/api/restart", headers=_good()).status_code == 409


def test_restart_without_token_is_401(client) -> None:
    with patch("main._can_self_restart", return_value=False):
        resp = client.post("/api/restart")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/tools"),
    ("GET", "/api/chats"),
    ("GET", "/api/settings"),
    ("PUT", "/api/settings"),
    ("DELETE", "/api/tools/some_tool"),
    ("POST", "/api/tools/some_tool/disable"),
    ("POST", "/api/tools/some_tool/enable"),
])
def test_every_api_route_needs_the_token(client, method, path) -> None:
    assert client.request(method, path).status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 43}
    assert client.request(method, path, headers=wrong).status_code == 401


def test_token_is_accepted(client) -> None:
    resp = client.get("/api/tools", headers=_good())
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_health_is_open_without_token_but_not_to_browsers(client) -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/health", headers=EVIL).status_code == 403


def test_preflight_from_a_page_is_refused(client) -> None:
    resp = client.options("/api/restart", headers={
        **EVIL, "Access-Control-Request-Method": "POST",
    })
    assert resp.status_code == 403


def test_allow_listed_origin_passes_but_star_does_not(client, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_ALLOWED_ORIGINS", "*")
    assert client.get("/api/tools", headers={**_good(), **EVIL}).status_code == 403
    monkeypatch.setenv("AGENT_ALLOWED_ORIGINS", "https://ok.example, https://evil.example")
    assert client.get("/api/tools", headers={**_good(), **EVIL}).status_code == 200
    # Allow-listing an origin never waives the token.
    assert client.get("/api/tools", headers=EVIL).status_code == 401


# ── Token file ───────────────────────────────────────────────────────────

def test_token_file_created_private_and_reused(tmp_path) -> None:
    path = tmp_path / "fresh" / "token"
    token = auth.load_or_create_token(path)
    assert len(token) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_text().strip() == token
    assert auth.load_or_create_token(path) == token
    assert auth.load_or_create_token(tmp_path / "other" / "token") != token


def test_loose_permissions_are_tightened(tmp_path) -> None:
    path = tmp_path / "token"
    path.write_text("abc\n")
    os.chmod(path, 0o644)
    assert auth.load_or_create_token(path) == "abc"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_daemon_start_creates_the_token_file(client) -> None:
    assert auth.token_path().exists()
    assert stat.S_IMODE(auth.token_path().stat().st_mode) == 0o600


def test_token_never_logged(client, caplog) -> None:
    caplog.set_level("DEBUG")
    client.get("/api/tools", headers={"Authorization": "Bearer nope"})
    client.get("/api/tools", headers=_good())
    assert auth.get_token() not in caplog.text
