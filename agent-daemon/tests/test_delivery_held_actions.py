"""#131: held file events get a readable title, and held offers can be
shown now, dismissed one by one, or cleared all at once."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
from ai import delivery as dl
from events.event_bus import ContextEvent, EventBus

T0 = datetime(2026, 11, 10, 10, 0).astimezone().timestamp()


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def d(tmp_path: Path):
    settings = dl.DeliverySettings(tmp_path / "delivery_settings.json")
    dl.set_default_settings(settings)
    clock = Clock()
    typing = {"idle": 0.0}  # always typing -> every file event is held
    gate = dl.BusyGate(calendar=lambda now: [], processes=lambda: set(),
                       seconds_since_key=lambda: typing["idle"], clock=clock,
                       calendar_ttl=0.0)
    out = dl.Delivery(tmp_path / "held_offers.json", settings=settings, gate=gate, clock=clock)
    out.typing = typing  # type: ignore[attr-defined]
    yield out
    dl.set_default_settings(None)


def file_event(name: str, action: str = "created", **extra: str) -> ContextEvent:
    return ContextEvent(event_type="file", metadata={
        "path": f"/Users/me/Downloads/{name}", "action": action, **extra})


def _saved_ids(d: dl.Delivery) -> list[str]:
    return [h["id"] for h in json.loads(Path(d.path).read_text())["held"]]


# ---- title ---------------------------------------------------------------


def test_file_title_names_the_file_and_folder() -> None:
    t = dl._title("file", {"path": "/Users/me/Downloads/x.pdf", "action": "created"})
    assert "x.pdf" in t and t == "New in Downloads: x.pdf"


@pytest.mark.parametrize("meta,want", [
    ({"path": "/U/Downloads/Zoom.dmg", "action": "renamed",
      "from": "/U/Downloads/Zoom.dmg.crdownload"}, "New in Downloads: Zoom.dmg"),
    ({"path": "/U/Invoices/a.pdf", "action": "renamed", "from": "/U/Downloads/a.pdf"},
     "Moved to Invoices: a.pdf"),
    ({"path": "/U/Downloads/b.txt", "action": "renamed", "from": "/U/Downloads/a.txt"},
     "Renamed in Downloads: b.txt"),
    ({"path": "/U/Downloads/n.txt", "action": "modified"}, "Changed in Downloads: n.txt"),
    ({"path": "/U/Downloads/bot-inator.app", "action": "created"},
     "New in Downloads: bot-inator.app"),
])
def test_file_title_per_action(meta, want) -> None:
    assert dl._title("file", meta) == want


def test_file_title_falls_back_without_a_path_and_others_unchanged() -> None:
    assert dl._title("file", {}) == "file"
    assert dl._title("pattern_detected", {"description": "Remind me"}) == "Remind me"


def test_held_summary_uses_the_file_title(d) -> None:
    assert d.decide(file_event("x.pdf")).action == "hold"
    assert d.status()["held"][0]["title"] == "New in Downloads: x.pdf"


# ---- dismiss / clear / open ----------------------------------------------


def test_dismissing_a_held_offer_removes_it_from_status_and_file(d) -> None:
    a = d.decide(file_event("a.pdf")).offer_id
    b = d.decide(file_event("b.pdf")).offer_id
    assert _saved_ids(d) == [a, b]
    assert d.dismiss_held(a) is True
    assert [h["id"] for h in d.status()["held"]] == [b]
    assert _saved_ids(d) == [b]
    assert d.dismiss_held(a) is False
    # survives a restart
    again = dl.Delivery(d.path, settings=d.settings, gate=d.gate, clock=d.clock)
    assert [h.id for h in again.held] == [b]


def test_clear_held_drops_every_held_offer(d) -> None:
    for i in range(5):
        d.decide(file_event(f"f{i}.pdf"))
    assert d.clear_held() == 5
    assert d.status()["held"] == [] and _saved_ids(d) == []
    assert d.clear_held() == 0


def test_open_held_bypasses_the_busy_gate(d) -> None:
    offer = d.decide(file_event("a.pdf")).offer_id
    event = d.open_held(offer)
    assert event is not None and event.metadata["path"].endswith("a.pdf")
    assert event.metadata[dl.BYPASS_KEY] == "held"
    assert d.status()["held"] == []
    # still typing, yet the opened event runs
    assert d.decide(event).action == "run"
    assert d.open_held(offer) is None


def test_open_held_idea_is_not_counted_toward_the_cap(d) -> None:
    ev = ContextEvent(event_type="pattern_detected",
                      metadata={"trigger": "mail_deadline", "description": "x"})
    offer = d.decide(ev).offer_id
    d.open_held(offer)
    assert d._user_opened == 1


# ---- routes ----------------------------------------------------------------


@pytest.fixture
def client(d):
    from routes import delivery as routes

    app = FastAPI()
    app.include_router(routes.router)
    app.state.delivery = d
    app.state.event_bus = EventBus()
    with TestClient(app) as c:
        c.bus = app.state.event_bus  # type: ignore[attr-defined]
        yield c


def test_routes_held_open_dismiss_and_clear(client, d) -> None:
    ids = [d.decide(file_event(f"f{i}.pdf")).offer_id for i in range(4)]
    r = client.post(f"/api/delivery/held/{ids[0]}/open")
    assert r.status_code == 200 and r.json()["opened"]
    pushed = client.bus._queue.get_nowait()  # type: ignore[attr-defined]
    assert pushed.metadata[dl.BYPASS_KEY] == "held"
    assert client.delete(f"/api/delivery/held/{ids[1]}").json()["dismissed"]
    assert client.delete(f"/api/delivery/held/{ids[1]}").status_code == 404
    assert client.post("/api/delivery/held/nope/open").status_code == 404
    assert client.delete("/api/delivery/held").json() == {"cleared": 2}
    assert client.get("/api/delivery").json()["held"] == []
