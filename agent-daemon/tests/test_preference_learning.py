"""#72: learn preferences from approvals, denials and edits."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
from ai import preference_learning as pl
from ai.idea_outcomes import DecisionLog
from ai.memory import Memory, _fallback_embed
from ai.orchestrator import Orchestrator
from events.event_bus import ContextEvent
from routes import preferences
from tests.test_wave13_wiring import _answer_soon, _approval_orch


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


@pytest.fixture
def mem(tmp_path):
    m = Memory(db_path=tmp_path / "memory.db", embedder=lambda t: _fallback_embed(t, 8),
               dim=8, max_rows=50)
    yield m
    m.close()


@pytest.fixture
def log():
    return DecisionLog(None, clock=_Clock())


def _deny(log: DecisionLog, n: int, tool="create_reminder", trigger="mail_received") -> None:
    for _ in range(n):
        log.record(tool, trigger, "denied")


def _texts(mem: Memory) -> list[str]:
    return [r["text"] for r in pl.learned(mem)]


# ---- stop offering -----------------------------------------------------


def test_three_denials_learn_one_preference_and_suppress(log, mem) -> None:
    _deny(log, 2)
    assert pl.learn(log, mem) == []
    assert not pl.is_suppressed(mem, "create_reminder", "mail_received")
    _deny(log, 1)
    saved = pl.learn(log, mem)
    assert len(saved) == 1
    assert saved[0]["metadata"]["rule"] == "stop_offer"
    assert "create_reminder" in saved[0]["text"] and "mail received" in saved[0]["text"]
    assert pl.is_suppressed(mem, "create_reminder", "mail_received")
    # Other triggers / tools are untouched.
    assert not pl.is_suppressed(mem, "create_reminder", "file_created")
    assert not pl.is_suppressed(mem, "create_event", "mail_received")


def test_learn_is_idempotent(log, mem) -> None:
    _deny(log, 5)
    pl.learn(log, mem)
    pl.learn(log, mem)
    assert pl.learn(log, mem) == []
    assert len(pl.learned(mem)) == 1


def test_an_approval_in_between_resets_the_streak(log, mem) -> None:
    _deny(log, 2)
    log.record("create_reminder", "mail_received", "approved")
    _deny(log, 2)
    assert pl.learn(log, mem) == []
    _deny(log, 1)
    assert len(pl.learn(log, mem)) == 1


def test_build_cards_and_chat_turns_never_stop(log, mem) -> None:
    _deny(log, 4, tool="generate_custom_tool", trigger="pdf_download")
    _deny(log, 4, tool="send_imessage", trigger="user_message")
    assert pl.learn(log, mem) == []
    assert not pl.is_suppressed(mem, "send_imessage", "user_message")


def _gate(orch: Orchestrator, tool: str) -> dict[str, Any] | None:
    """What the runner's hook does in the gated wrapper (beside
    ``_previously_declined``): refuse a suppressed call, else ask."""
    trigger = orch._current_event_type
    if pl.is_suppressed(orch.memory, tool, trigger):
        return pl.refusal(tool, trigger)
    return None


def test_no_fourth_offer_after_three_denials(mem) -> None:
    """Three denied cards through the real approval path, then the fourth
    call is refused before any card and does not run."""
    async def run() -> tuple[list[bool], Any, int]:
        orch = _approval_orch()
        orch.memory = mem
        orch._current_event_type = "mail_received"
        verdicts = []
        for _ in range(3):
            assert _gate(orch, "create_reminder") is None
            _answer_soon(orch, approved=False)
            ok, _ = await orch._request_approval("e", "create_reminder", {"kwargs": {"title": "x"}}, "r")
            verdicts.append(ok)
            orch._pending.clear()  # the WS resolver pops it in the app
            pl.learn(orch.approval_decisions, mem)
        cards = len(orch.sent)
        return verdicts, _gate(orch, "create_reminder"), cards

    verdicts, fourth, cards = asyncio.run(run())
    assert verdicts == [False, False, False] and cards == 3
    assert fourth is not None and fourth["error"] == "user_declined_before"


def test_suppression_never_approves_anything(log, mem) -> None:
    """A learned preference only refuses; it can't make a call skip its card."""
    _deny(log, 3)
    pl.learn(log, mem)
    result = pl.refusal("create_reminder", "mail_received")
    assert result["status"] == "error"
    assert not any(k in result for k in ("approved", "result", "ran"))
    # A call that is not suppressed still goes to a card.
    async def run() -> int:
        orch = _approval_orch()
        orch.memory = mem
        orch._current_event_type = "file_created"
        assert _gate(orch, "create_reminder") is None
        _answer_soon(orch, approved=True)
        await orch._request_approval("e", "create_reminder", {"kwargs": {}}, "r")
        return len(orch.sent)
    assert asyncio.run(run()) == 1


# ---- deleting -----------------------------------------------------------


def test_delete_lets_the_offer_return_and_old_denials_dont_relearn(log, mem) -> None:
    _deny(log, 3)
    pref = pl.learn(log, mem)[0]
    assert mem.delete_preference(pref["id"], pl.SOURCE, at=log.clock()) is not None
    assert not pl.is_suppressed(mem, "create_reminder", "mail_received")
    assert pl.learn(log, mem) == []  # same old denials: not again
    _deny(log, 2)
    assert pl.learn(log, mem) == []
    _deny(log, 1)  # three new denials after the delete
    assert len(pl.learn(log, mem)) == 1


def test_deleted_preference_is_not_recalled(log, mem) -> None:
    for _ in range(2):
        log.record("draft_email", "mail_received", "edited", {"body": "shorter"})
    pref = pl.learn(log, mem)[0]
    assert pref["text"] in pl.recall_with_preferences(mem, "draft a reply")
    mem.delete_preference(pref["id"], pl.SOURCE, at=log.clock())
    recalled = pl.recall_with_preferences(mem, "draft a reply", top_k=10)
    assert pref["text"] not in recalled
    assert mem.recall_memory("keeps drafts short", 10) == []


# ---- edits and notes ---------------------------------------------------------


def test_one_shortening_is_not_enough(log, mem) -> None:
    log.record("draft_email", "mail_received", "edited", {"body": "shorter"})
    log.record("draft_email", "mail_received", "edited", {"subject": "shorter"})
    assert pl.learn(log, mem) == []


def test_two_shortenings_recalled_in_next_draft_prompt(mem) -> None:
    """Two real approval cards where the user cut the body -> the next mail
    prompt carries "keeps drafts short"."""
    async def run() -> DecisionLog:
        orch = _approval_orch()
        orch._current_event_type = "mail_received"
        long_body = "Hello, thanks a lot for your message, I will get back to you soon."
        for _ in range(2):
            _answer_soon(orch, approved=True,
                         edited={"kwargs": {"to": "a@b.c", "body": "Thanks!"}})
            await orch._request_approval(
                "e", "draft_email", {"kwargs": {"to": "a@b.c", "body": long_body}}, "r")
            orch._pending.clear()  # the WS resolver pops it in the app
            pl.learn(orch.approval_decisions, mem)
        return orch.approval_decisions

    asyncio.run(run())
    assert any("keeps drafts short" in t for t in _texts(mem))
    # Newer, unrelated memories crowd similarity / recency recall.
    for i in range(20):
        mem.save_memory(f"observation {i}: opened Finder", {"kind": "observation"})
    assert not any("keeps drafts short" in t for t in mem.recall_memory("mail from a@b.c", 3))
    event = ContextEvent(event_type="mail_received",
                         metadata={"sender": "a@b.c", "subject": "hi", "message_id": "m1"})
    prompt = Orchestrator._build_mail_prompt(
        event, memories=pl.recall_with_preferences(mem, "mail from a@b.c"))
    assert "keeps drafts short" in prompt


def test_lengthening_as_often_cancels_it(log, mem) -> None:
    for fact in ("shorter", "shorter", "longer", "longer"):
        log.record("draft_email", "mail_received", "edited", {"body": fact})
    assert pl.learn(log, mem) == []


def test_note_saved_verbatim_once(log, mem) -> None:
    note = "Never  remind me about newsletters."
    log.record("create_reminder", "mail_received", "denied", note=note)
    log.record("create_reminder", "mail_received", "denied", note=note.upper())
    saved = pl.learn(log, mem)
    assert [s["text"] for s in saved] == ["Never remind me about newsletters."]
    assert saved[0]["metadata"]["source"] == "approval"
    assert saved[0]["metadata"]["kind"] == "preference"


def test_derive_uses_only_templates_and_notes(log) -> None:
    """Every sentence is a fixed template or the note itself: no model."""
    _deny(log, 3)
    for _ in range(2):
        log.record("draft_email", "mail_received", "edited", {"body": "shorter"})
    log.record("draft_email", "mail_received", "approved", note="sign off with Cheers")
    texts = [p["text"] for p in pl.derive(log.decisions)]
    assert texts[0].startswith("Don't offer create reminder (create_reminder)")
    assert texts[1].startswith("The user keeps drafts short")
    assert texts[2] == "sign off with Cheers"
    assert pl.derive(log.decisions) == pl.derive(log.decisions)


def test_learn_survives_a_broken_store(log) -> None:
    _deny(log, 3)
    broken = SimpleNamespace(list_preferences=lambda **k: 1 / 0)
    assert pl.learn(log, broken) == []
    assert pl.is_suppressed(broken, "create_reminder", "mail_received") is False


# ---- routes --------------------------------------------------------------


@pytest.fixture
def client(mem, log):
    _deny(log, 3)
    pl.learn(log, mem)
    mem.save_memory("I like tea", {"kind": "preference"})  # user-saved: not listed
    app = FastAPI()
    app.include_router(preferences.router)
    app.add_middleware(auth.AuthMiddleware)
    app.state.memory = mem
    with TestClient(app) as c:
        yield c


def test_route_lists_and_deletes_learned_only(client, mem) -> None:
    h = auth.auth_header()
    rows = client.get("/api/preferences/learned", headers=h).json()
    assert len(rows) == 1 and rows[0]["rule"] == "stop_offer"
    assert rows[0]["tool_name"] == "create_reminder"
    user_pref = next(r for r in mem.list_preferences() if r["text"] == "I like tea")
    assert client.delete(f"/api/preferences/learned/{user_pref['id']}", headers=h).status_code == 404
    resp = client.delete(f"/api/preferences/learned/{rows[0]['id']}", headers=h)
    assert resp.status_code == 200
    assert client.get("/api/preferences/learned", headers=h).json() == []
    assert not pl.is_suppressed(mem, "create_reminder", "mail_received")
    assert [r["text"] for r in mem.list_preferences()] == ["I like tea"]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/preferences/learned"),
    ("delete", "/api/preferences/learned/1"),
])
def test_routes_need_the_token(client, mem, method, path) -> None:
    assert getattr(client, method)(path).status_code == 401
    assert len(pl.learned(mem)) == 1
