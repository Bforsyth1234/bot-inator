"""#56: review and remove remembered tool-idea denials."""
from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
from ai.idea_match import DeclinedIdeaMatcher, idea_text
from ai.open_help import IdeaFeedback
from routes import ideas

SEND = ("send_imessage", "Send an iMessage to a phone number with text.")
REWORD = ("send_text_message", "Send a text to a contact.")
VECS = {
    idea_text(*SEND): [1.0, 0.0, 0.0],
    idea_text(*REWORD): [0.95, 0.05, 0.0],
    idea_text("file_invoices", "Move invoice PDFs."): [0.0, 0.0, 1.0],
    idea_text("file_invoices", "Move invoice PDFs to Finance."): [0.0, 0.0, 1.0],
    idea_text("keep_me", "approved idea"): [0.0, 1.0, 0.0],
}


def _embed(texts):
    return [np.array(VECS[t]) for t in texts]


def _feedback(path=None) -> IdeaFeedback:
    fb = IdeaFeedback(path)
    fb.record(*SEND, False)
    fb.record("file_invoices", "Move invoice PDFs.", False)
    fb.record("keep_me", "approved idea", True)
    fb.record(*SEND, False)
    fb.record("file_invoices", "Move invoice PDFs to Finance.", False)
    # Deterministic times: send_imessage last declined at 40, file_invoices at 50.
    for entry, at in zip(fb.entries, (10, 20, 30, 40, 50)):
        entry["at"] = float(at)
    fb._save()
    return fb


@pytest.fixture()
def setup(tmp_path):
    path = tmp_path / "idea_feedback.json"
    fb = _feedback(path)
    matcher = DeclinedIdeaMatcher(fb, embed=_embed)
    app = FastAPI()
    app.include_router(ideas.router)
    app.add_middleware(auth.AuthMiddleware)
    app.state.idea_feedback = fb
    app.state.orchestrator = SimpleNamespace(declined_matcher=matcher)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, fb=fb, matcher=matcher, path=path)


def _h() -> dict[str, str]:
    return auth.auth_header()


def test_list_groups_by_name_newest_first(setup) -> None:
    rows = setup.client.get("/api/ideas/declined", headers=_h()).json()
    assert rows == [
        {"name": "file_invoices", "description": "Move invoice PDFs to Finance.",
         "last_declined_at": 50.0, "count": 2},
        {"name": "send_imessage", "description": SEND[1],
         "last_declined_at": 40.0, "count": 2},
    ]


def test_delete_removes_every_entry_with_that_name_and_saves(setup) -> None:
    resp = setup.client.delete("/api/ideas/declined/send_imessage", headers=_h())
    assert resp.status_code == 200 and resp.json()["removed"] == 2
    names = [r["name"] for r in setup.client.get("/api/ideas/declined", headers=_h()).json()]
    assert names == ["file_invoices"]
    on_disk = json.loads(setup.path.read_text())
    assert not any(e["tool_name"] == "send_imessage" for e in on_disk)
    assert any(e["tool_name"] == "keep_me" for e in on_disk)


def test_delete_unknown_or_approved_only_name_is_404(setup) -> None:
    assert setup.client.delete("/api/ideas/declined/nope", headers=_h()).status_code == 404
    assert setup.client.delete("/api/ideas/declined/keep_me", headers=_h()).status_code == 404
    assert [e["tool_name"] for e in setup.fb.entries].count("keep_me") == 1


def test_clear_keeps_approvals(setup) -> None:
    resp = setup.client.post("/api/ideas/declined/clear", headers=_h())
    assert resp.json() == {"removed": 4}
    assert setup.client.get("/api/ideas/declined", headers=_h()).json() == []
    on_disk = json.loads(setup.path.read_text())
    assert [(e["tool_name"], e["approved"]) for e in on_disk] == [("keep_me", True)]
    assert IdeaFeedback(setup.path).declined_groups() == []


@pytest.mark.parametrize("method,path", [
    ("get", "/api/ideas/declined"),
    ("delete", "/api/ideas/declined/send_imessage"),
    ("post", "/api/ideas/declined/clear"),
])
def test_routes_need_the_token(setup, method, path) -> None:
    resp = getattr(setup.client, method)(path)
    assert resp.status_code == 401
    bad = getattr(setup.client, method)(path, headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    assert len(setup.fb.entries) == 5  # nothing removed


def test_removed_idea_stops_matching_and_leaves_the_cache(setup) -> None:
    m = setup.matcher
    assert m.match(*REWORD) == "send_imessage"
    assert idea_text(*SEND) in m._cache
    setup.client.delete("/api/ideas/declined/send_imessage", headers=_h())
    assert idea_text(*SEND) not in m._cache
    assert m.match(*REWORD) == ""
    assert m.match(*SEND) == ""


def test_clear_empties_the_matcher_cache_of_declined_ideas(setup) -> None:
    m = setup.matcher
    assert m.match(*REWORD) == "send_imessage"
    setup.client.post("/api/ideas/declined/clear", headers=_h())
    assert not any(t in m._cache for t in (
        idea_text(*SEND), idea_text("file_invoices", "Move invoice PDFs."),
    ))
    assert m.match(*REWORD) == ""
