"""Tests for the opt-in training corpus (#118) and its frozen held-out split."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
import user_settings
from ai import training_corpus as tc
from ai.training_corpus import TrainingCorpus
from routes import training as training_routes

EMAIL_BODY = "Hi, the invoice for the Henderson project is attached, please pay by Friday."
EMAIL_SUBJECT = "Invoice for the Henderson project"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Flag:
    def __init__(self, value: bool) -> None:
        self.value = value

    def __call__(self) -> bool:
        return self.value


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def on() -> Flag:
    return Flag(True)


@pytest.fixture()
def mail() -> Flag:
    return Flag(False)


@pytest.fixture()
def corpus(tmp_path, clock, on, mail) -> TrainingCorpus:
    c = TrainingCorpus(tmp_path / "training.db", enabled=on, keep_mail_text=mail, clock=clock)
    yield c
    c.close()


def _decide(corpus: TrainingCorpus, verdict: str = "approved", **kw):
    args = {"tool_name": "move_file",
            "tool_args": {"args": [], "kwargs": {"src": "/a.pdf", "dst_dir": "/Docs"}},
            "verdict": verdict, "prompt": "A PDF landed in Downloads.", "offer_id": None,
            "event_type": "file_created", "trigger": "file_created",
            "event_meta": {"path": "/a.pdf"}, "reasoning": "Tidy up", "summary": "Move a.pdf"}
    args.update(kw)
    return corpus.record(**args)


def _mail_card(corpus: TrainingCorpus, **kw):
    return corpus.record(
        tool_name="draft_email",
        tool_args={"kwargs": {"to": "pat@example.com",
                              "body": f"Thanks Pat. You wrote: \"{EMAIL_BODY}\" I'll pay today."}},
        verdict="approved",
        prompt=f"Email from pat@example.com: {EMAIL_SUBJECT}\n\n{EMAIL_BODY}",
        event_type="mail_received", trigger="mail_received", offer_id="off_mail",
        event_meta={"sender": "pat@example.com", "subject": EMAIL_SUBJECT, "body": EMAIL_BODY,
                    "message_id": "m-1", "mailbox": "INBOX"},
        reasoning="Pat asked to pay", summary="Reply to Pat", **kw,
    )


# ---- opt-in --------------------------------------------------------------


def test_off_writes_nothing(tmp_path, clock, mail):
    path = tmp_path / "training.db"
    c = TrainingCorpus(path, enabled=Flag(False), keep_mail_text=mail, clock=clock)
    assert _decide(c) is None
    assert _decide(c, "denied") is None
    assert not path.exists(), "an opted-out user gets no training.db at all"
    assert c.count() == 0
    assert c.rows() == []


def test_off_writes_nothing_even_with_existing_file(corpus, on):
    _decide(corpus)
    on.value = False
    assert _decide(corpus) is None
    assert corpus.count() == 1


def test_default_opt_in_is_off(monkeypatch, tmp_path):
    monkeypatch.delenv(tc.ENABLED_KEY, raising=False)
    monkeypatch.delenv(tc.KEEP_MAIL_TEXT_KEY, raising=False)
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "none.json"))
    assert tc.opted_in() is False
    assert tc.keep_mail_text() is False
    c = TrainingCorpus(tmp_path / "t.db")  # default flags read the settings
    assert _decide(c) is None
    assert not (tmp_path / "t.db").exists()


def test_env_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "none.json"))
    monkeypatch.setenv(tc.ENABLED_KEY, "1")
    assert tc.opted_in() is True
    assert tc.keep_mail_text() is False


def test_on_records_prompt_card_verdict(corpus):
    row = _decide(corpus, offer_id="off_1", request_id="req_1", event_id="ev_1")
    assert row is not None
    assert row["prompt"] == "A PDF landed in Downloads."
    assert row["card"]["tool"] == "move_file"
    assert row["card"]["args"] == {"src": "/a.pdf", "dst_dir": "/Docs"}
    assert row["card"]["summary"] == "Move a.pdf"
    assert row["verdict"] == "approved"
    assert row["final_args"] == {"src": "/a.pdf", "dst_dir": "/Docs"}
    assert row["offer_id"] == "off_1"
    assert row["payload"] == {"path": "/a.pdf"}
    assert row["tainted"] is False
    assert corpus.count() == 1


def test_edited_verdict_keeps_final_text(corpus):
    row = _decide(corpus, edited_args={"kwargs": {"dst_dir": "/Archive"}})
    assert row["verdict"] == "edited"
    assert row["final_args"] == {"src": "/a.pdf", "dst_dir": "/Archive"}
    assert row["card"]["args"]["dst_dir"] == "/Docs"  # what the model wrote


def test_denied_and_timed_out(corpus):
    assert _decide(corpus, "declined")["verdict"] == "denied"
    row = _decide(corpus, "timed_out")
    assert row["verdict"] == "timed_out" and row["final_args"] is None
    with pytest.raises(ValueError):
        _decide(corpus, "maybe")


# ---- mail / message text ------------------------------------------------


def _all_text(path: Path) -> str:
    db = sqlite3.connect(str(path))
    try:
        return "\n".join(str(v) for r in db.execute("SELECT * FROM rows") for v in r)
    finally:
        db.close()


def test_mail_toggle_off_strips_email_text(corpus):
    row = _mail_card(corpus)
    assert row["mail_stripped"] is True
    assert row["prompt"] == tc.STRIPPED_PROMPT
    assert row["payload"] == {"message_id": "m-1", "mailbox": "INBOX"}
    assert EMAIL_BODY not in row["card"]["args"]["body"]
    assert tc.STRIPPED_QUOTE in row["card"]["args"]["body"]
    assert "I'll pay today." in row["card"]["args"]["body"]  # the model's own draft
    text = _all_text(corpus.path)
    assert EMAIL_BODY not in text
    assert EMAIL_SUBJECT not in text
    assert "pat@example.com" in text  # the card's own "to" argument


def test_mail_toggle_on_keeps_email_text(corpus, mail):
    mail.value = True
    row = _mail_card(corpus)
    assert row["mail_stripped"] is False
    assert EMAIL_BODY in row["prompt"]
    assert row["payload"]["body"] == EMAIL_BODY


@pytest.mark.parametrize("event_type,meta,kinds", [
    ("imessage_received", {"text": EMAIL_BODY, "sender": "+15551234567"}, ()),
    ("pattern_detected", {"trigger": "mail_needs_reply", "event_details": EMAIL_BODY}, ()),
    ("pattern_detected", {"trigger": "tracking_number", "event_details": EMAIL_BODY}, ()),
    ("pattern_detected", {"trigger": "x", "source_event_type": "mail_received",
                          "event_details": EMAIL_BODY}, ()),
    ("user_message", {"text": EMAIL_BODY}, ("email",)),
])
def test_other_mail_and_message_turns_are_stripped(corpus, event_type, meta, kinds):
    row = _decide(corpus, event_type=event_type, event_meta=meta, outside_kinds=kinds,
                  prompt=f"context: {EMAIL_BODY}")
    assert row["mail_stripped"] is True
    assert EMAIL_BODY not in json.dumps(row)


def test_non_mail_turn_is_not_stripped(corpus):
    row = _decide(corpus, outside_kinds=("web_page",))
    assert row["mail_stripped"] is False
    assert row["prompt"] == "A PDF landed in Downloads."


def test_outside_kinds_of_taint():
    from ai.taint import TurnTaint

    shield = TurnTaint()
    shield.begin_turn("event", event_type="mail_received", metadata={"subject": "s", "sender": "a@b.c"})
    assert "email" in tc.outside_kinds_of(shield)
    assert tc.outside_kinds_of(None) == []


# ---- taint -----------------------------------------------------------------


def test_tainted_rows_stored_but_left_out_of_training(corpus):
    _decide(corpus)
    t = _decide(corpus, tainted=True)
    assert corpus.get(t["id"])["tainted"] is True
    assert corpus.count() == 2
    assert [r["id"] for r in corpus.training_rows()] != [] and \
        t["id"] not in {r["id"] for r in corpus.training_rows()}
    assert t["id"] in {r["id"] for r in corpus.training_rows(include_tainted=True)}
    counts = corpus.usable_counts()
    assert counts["total"] == 1 and counts["tainted"] == 1 and counts["all"] == 2


# ---- outcomes ------------------------------------------------------------------


def test_mark_undone_soon_after_by_offer_id(corpus, clock):
    row = _decide(corpus, offer_id="off_9")
    clock.now += 3600
    assert corpus.mark_undone("off_9") == 1
    got = corpus.get(row["id"])
    assert got["outcome"] == "undone" and got["negative"] is True
    assert corpus.usable_counts()["negatives"] == 1


def test_mark_undone_late_is_ignored(corpus, clock):
    _decide(corpus, offer_id="off_9")
    clock.now += tc.UNDO_SOON_SECONDS + 1
    assert corpus.mark_undone("off_9") == 0


def test_undo_listener_matches_event_id(corpus):
    row = _decide(corpus, event_id="ev_7")
    corpus.undo_listener({"offer_id": None, "event_id": "ev_7"}, {"status": "undone"})
    assert corpus.get(row["id"])["outcome"] == "undone"


def test_mark_tool_never_run(corpus):
    row = _decide(corpus, tool_name="generate_custom_tool", offer_id="off_t")
    denied = _decide(corpus, "denied", offer_id="off_t")
    assert corpus.mark_tool_never_run("off_t") == 1  # only the approved row
    assert corpus.get(row["id"])["outcome"] == "tool_never_run"
    assert corpus.get(denied["id"])["outcome"] is None
    assert corpus.mark_tool_never_run("off_unknown") == 0


# ---- counts ----------------------------------------------------------------------


def test_usable_counts(corpus):
    for _ in range(5):
        _decide(corpus)
    for _ in range(3):
        _decide(corpus, "denied")
    _decide(corpus, "timed_out")
    _decide(corpus, "denied", tainted=True)
    _decide(corpus, offer_id="off_u")
    corpus.mark_undone("off_u")
    counts = corpus.usable_counts()
    assert counts == {"total": 9, "negatives": 4, "tainted": 1, "all": 11, "heldout": 0}


# ---- held-out split ------------------------------------------------------------


def test_heldout_frozen_deterministic_and_stable(tmp_path, clock, on, mail):
    path = tmp_path / "training.db"
    c = TrainingCorpus(path, enabled=on, keep_mail_text=mail, clock=clock)
    first = []
    for i in range(40):
        clock.now += 1
        first.append(_decide(c, "denied" if i % 4 == 0 else "approved")["id"])
    _decide(c, tainted=True)
    _decide(c, "timed_out")
    info = c.freeze_split()
    assert info["size"] == 4  # ceil(40 * 0.1); tainted / timed out never held out
    held = {r["id"] for r in c.heldout_rows()}
    train = {r["id"] for r in c.training_rows()}
    assert len(held) == 4 and len(train) == 36 and not held & train

    # Frozen: a second call and new rows don't change it.
    assert c.freeze_split()["split_id"] == info["split_id"]
    for _ in range(20):
        _decide(c)
    assert {r["id"] for r in c.heldout_rows()} == held
    assert len(c.training_rows()) == 56
    c.close()

    # Recorded in the DB: a fresh process sees the same split.
    c2 = TrainingCorpus(path, enabled=on, keep_mail_text=mail, clock=clock)
    assert {r["id"] for r in c2.heldout_rows()} == held
    assert c2.usable_counts()["heldout"] == 4

    # Deterministic: the seeded ranking picks the same rows again.
    expected = sorted(first, key=lambda i: tc._heldout_rank(tc.DEFAULT_SEED, i))[:4]
    assert set(expected) == held

    # A new split is only made on request, and includes the new rows.
    new = c2.freeze_split(refreeze=True)
    assert new["split_id"] != info["split_id"] and new["size"] == 6
    c2.close()


def test_heldout_empty_before_freeze_and_row_delete(corpus):
    for _ in range(10):
        _decide(corpus)
    assert corpus.heldout_rows() == []
    assert len(corpus.training_rows()) == 10
    corpus.freeze_split()
    (held,) = corpus.heldout_rows()
    assert corpus.get(held["id"])["heldout"] is True
    assert corpus.delete(held["id"])
    assert corpus.heldout_rows() == []
    with pytest.raises(ValueError):
        corpus.freeze_split(fraction=1.5, refreeze=True)


# ---- delete + file mode -----------------------------------------------------------


def test_file_mode_0600(corpus):
    _decide(corpus)
    mode = stat.S_IMODE(os.stat(corpus.path).st_mode)
    assert mode == 0o600


def test_delete_one(corpus):
    a = _decide(corpus)
    b = _decide(corpus)
    assert corpus.delete(a["id"]) is True
    assert corpus.delete(a["id"]) is False
    assert [r["id"] for r in corpus.rows()] == [b["id"]]
    assert corpus.delete(b["id"])
    assert corpus.count() == 0 and corpus.rows() == []


def test_delete_all_leaves_no_rows_or_text(corpus):
    _mail_card(corpus)
    for _ in range(12):
        _decide(corpus)
    corpus.freeze_split()
    assert corpus.delete_all() == 13
    assert corpus.count() == 0
    assert corpus.heldout_rows() == [] and corpus.training_rows() == []
    assert corpus.split_info() is None
    assert b"A PDF landed" not in corpus.path.read_bytes()


def test_opt_out_deletes_everything(corpus, on):
    for _ in range(12):
        _decide(corpus)
    corpus.freeze_split()
    on.value = False
    assert corpus.opt_out() == 12
    assert corpus.count() == 0 and corpus.heldout_rows() == []
    assert corpus.usable_counts()["all"] == 0


def test_opt_out_keep(corpus):
    _decide(corpus)
    assert corpus.opt_out(delete_stored=False) == 0
    assert corpus.count() == 1


def test_rows_paged_newest_first(corpus, clock):
    ids = []
    for _ in range(7):
        clock.now += 1
        ids.append(_decide(corpus)["id"])
    page1 = corpus.rows(limit=3)
    page2 = corpus.rows(limit=3, offset=3)
    assert [r["id"] for r in page1] == ids[::-1][:3]
    assert [r["id"] for r in page2] == ids[::-1][3:6]


def test_in_memory_store(clock, on, mail):
    c = TrainingCorpus(None, enabled=on, keep_mail_text=mail, clock=clock)
    _decide(c)
    assert c.count() == 1


# ---- routes ---------------------------------------------------------------------------


@pytest.fixture()
def client(corpus, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv(tc.ENABLED_KEY, raising=False)
    monkeypatch.delenv(tc.KEEP_MAIL_TEXT_KEY, raising=False)
    monkeypatch.setattr(user_settings, "ALLOWED_KEYS",
                        user_settings.ALLOWED_KEYS | {tc.ENABLED_KEY, tc.KEEP_MAIL_TEXT_KEY})
    app = FastAPI()
    app.add_middleware(auth.AuthMiddleware)
    app.include_router(training_routes.router)
    app.state.training_corpus = corpus
    with TestClient(app) as c:
        yield c


def test_routes_need_auth(client):
    assert client.get("/api/training").status_code == 401
    assert client.delete("/api/training/rows").status_code == 401


def test_routes_count_list_delete(client, corpus):
    h = auth.auth_header()
    a = _decide(corpus)
    _decide(corpus, tainted=True)
    _decide(corpus)
    body = client.get("/api/training", headers=h).json()
    assert body["count"] == 3 and body["usable"] == 2 and body["tainted"] == 1
    assert body["enabled"] is False  # the route reads the real setting

    page = client.get("/api/training/rows?limit=2", headers=h).json()
    assert page["total"] == 3 and len(page["rows"]) == 2
    assert client.get("/api/training/rows?limit=2&offset=2", headers=h).json()["rows"][0]["id"] == a["id"]

    assert client.delete(f"/api/training/rows/{a['id']}", headers=h).json() == {"ok": True, "count": 2}
    assert client.delete(f"/api/training/rows/{a['id']}", headers=h).status_code == 404
    assert client.delete("/api/training/rows", headers=h).json() == {"deleted": 2, "count": 0}


def test_routes_settings_toggle_and_opt_out_delete(client, corpus):
    h = auth.auth_header()
    r = client.put("/api/training/settings", json={"enabled": True, "keep_mail_text": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True and r.json()["keep_mail_text"] is True
    assert tc.opted_in() is True
    _decide(corpus)
    _decide(corpus)
    # Off keeps the data only when asked.
    r = client.put("/api/training/settings", json={"enabled": False, "delete_stored": False}, headers=h)
    assert r.json()["deleted"] == 0 and r.json()["count"] == 2
    client.put("/api/training/settings", json={"enabled": True}, headers=h)
    # Default: turning it off deletes what's stored.
    r = client.put("/api/training/settings", json={"enabled": False}, headers=h).json()
    assert r["enabled"] is False and r["deleted"] == 2 and r["count"] == 0
