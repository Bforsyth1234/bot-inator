"""#79: suggestion outcomes, and the approval-decision log #72/#78 read."""
from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import auth
from ai import idea_outcomes as io
from ai.idea_outcomes import DecisionLog, OutcomeStore, edit_facts, trigger_kind_of
from routes import outcomes

DAY = 86400.0


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def store(tmp_path, clock) -> OutcomeStore:
    return OutcomeStore(tmp_path / "idea_outcomes.json", clock=clock)


def _row(rows, key, value):
    return next(r for r in rows if r[key] == value)


# ---- outcomes recorded for each path --------------------------------------


def test_each_verdict_path_is_recorded_and_persisted(store, tmp_path, clock) -> None:
    shown = store.record_shown("open-help", "never_answered")
    a = store.record_offer("open_help", "file_pdfs", "approved", idea_kind="pdf_download")
    d = store.record_offer("friction", "copy_helper", "declined", idea_kind="copy_paste")
    t = store.record_offer("next-step", "add_invite", "timed_out", idea_kind="calendar_invite")
    r = store.record_offer("routine", "morning_tidy", "approved", one_shot=True)
    p = store.record_offer(None, "rename_shots", "approved")  # pattern detector: no source
    reloaded = OutcomeStore(tmp_path / "idea_outcomes.json", clock=clock)
    by_id = {o["id"]: o for o in reloaded.offers}
    assert by_id[shown]["verdict"] == "shown" and by_id[shown]["source"] == "open_help"
    assert by_id[a]["verdict"] == "approved" and by_id[a]["idea_kind"] == "pdf_download"
    assert by_id[d]["verdict"] == "declined" and by_id[d]["source"] == "friction"
    assert by_id[t]["verdict"] == "timed_out" and by_id[t]["source"] == "next_step"
    assert by_id[r]["source"] == "routine" and by_id[r]["one_shot"] is True
    assert by_id[p]["source"] == "pattern"
    sources = {r["source"]: r for r in reloaded.stats_by_source()}
    assert sources["open_help"]["shown"] == 2 and sources["open_help"]["approved"] == 1
    assert sources["open_help"]["approval_rate"] == 1.0  # the undecided offer is not counted
    assert sources["next_step"]["approval_rate"] == 0.0  # a timeout is not an approval


def test_bad_verdict_and_unknown_id(store) -> None:
    with pytest.raises(ValueError):
        store.record_verdict("x", "maybe")
    assert store.record_verdict("off_missing", "approved") is False
    assert store.record_tool_run("never_offered") is False


def test_file_is_0600_and_written_atomically(store, tmp_path) -> None:
    store.record_offer("open_help", "a", "approved")
    path = tmp_path / "idea_outcomes.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["version"] == 1
    assert [p.name for p in tmp_path.iterdir()] == ["idea_outcomes.json"]  # no temp left


def test_no_description_or_text_is_stored(store, tmp_path) -> None:
    store.record_offer("open_help", "x" * 500, "approved", idea_kind="k\nwith\nnewlines")
    offer = store.offers[-1]
    assert len(offer["tool_name"]) == io.MAX_LABEL and "\n" not in offer["idea_kind"]
    assert "description" not in offer


# ---- tool runs, kept rate --------------------------------------------------


def test_a_tool_run_twice_shows_count_2(store, clock) -> None:
    off = store.record_offer("open_help", "file_pdfs", "approved")
    store.link_tool(off, "file_pdfs_v2")
    assert store.record_tool_run("file_pdfs") is False  # installed under the new name
    store.record_tool_run("file_pdfs_v2")
    clock.advance(60)
    store.record_tool_run("file_pdfs_v2")
    offer = store.offers[-1]
    assert offer["run_count"] == 2 and offer["last_run_at"] == clock.now


def test_kept_status_rules(store, clock) -> None:
    used = store.record_offer("open_help", "used", "approved")
    store.record_tool_run("used")
    unused = store.record_offer("open_help", "unused", "approved")
    disabled = store.record_offer("open_help", "disabled", "approved")
    store.record_tool_run("disabled")
    store.record_disabled("disabled")
    action = store.record_offer("next_step", "add_event", "approved", one_shot=True)
    undone = store.record_offer("next_step", "move_file", "approved", one_shot=True)
    clock.advance(30 * 60)
    assert store.record_undone(undone) is True
    status = lambda oid: store.kept_status(store._find(oid))  # noqa: E731
    assert status(used) is True
    assert status(unused) is None          # too early to say
    assert status(disabled) is False
    assert status(action) is None          # undo window still open
    assert status(undone) is False
    clock.advance(io.KEPT_GRACE_SECONDS)
    assert status(unused) is False and status(action) is True


def test_undo_after_the_hour_does_not_count(store, clock) -> None:
    off = store.record_offer("next_step", "move_file", "approved", one_shot=True)
    clock.advance(io.UNDO_WINDOW_SECONDS + 1)
    assert store.record_undone(off) is False
    assert store.kept_status(store._find(off)) is True


# ---- adjustments -----------------------------------------------------------


def _approved_unused(store, source, n, kind="k"):
    for i in range(n):
        store.record_offer(source, f"{source}_{kind}_{i}", "approved", idea_kind=kind)


def test_low_kept_rate_raises_that_sources_threshold(store, clock) -> None:
    _approved_unused(store, "friction", 3)
    for i in range(3):
        store.record_offer("open_help", f"good_{i}", "approved")
        store.record_tool_run(f"good_{i}")
    # Before the grace period the unused tools are pending: no change yet.
    assert store.judge_threshold("friction", 7) == 7
    clock.advance(io.KEPT_GRACE_SECONDS)
    assert store.judge_threshold("friction", 7) == 8
    assert store.cooldown_multiplier("friction") == 2.0
    assert store.judge_threshold("open_help", 7) == 7      # other sources unaffected
    assert store.cooldown_multiplier("open_help") == 1.0
    assert _row(store.stats_by_source(), "source", "friction")["kept_rate"] == 0.0


def test_very_low_kept_rate_with_more_data_bumps_twice_and_caps(store, clock) -> None:
    _approved_unused(store, "friction", 5)
    clock.advance(io.KEPT_GRACE_SECONDS)
    assert store.judge_threshold("friction", 7) == 9
    assert store.cooldown_multiplier("friction") == 4.0
    assert store.judge_threshold("friction", 9) == io.MAX_JUDGE_SCORE


def test_too_few_settled_offers_change_nothing(store, clock) -> None:
    _approved_unused(store, "friction", 2)
    clock.advance(io.KEPT_GRACE_SECONDS)
    assert store.judge_threshold("friction", 7) == 7


def test_idea_kind_bump_applies_across_sources(store, clock) -> None:
    for i, source in enumerate(("open_help", "next_step", "routine")):
        store.record_offer(source, f"inv_{i}", "approved", idea_kind="calendar_invite")
    for i in range(4):
        store.record_offer("open_help", f"ok_{i}", "approved", idea_kind="pdf_download")
        store.record_tool_run(f"ok_{i}")
    clock.advance(io.KEPT_GRACE_SECONDS)
    assert store.judge_threshold("open_help", 7) == 7  # 4/7 kept overall
    assert store.judge_threshold("open_help", 7, idea_kind="calendar_invite") == 8
    assert _row(store.stats_by_kind(), "idea_kind", "calendar_invite")["kept_rate"] == 0.0


# ---- removal candidates ----------------------------------------------------


def test_unused_tool_listed_for_removal_after_30_days(store, clock) -> None:
    store.record_offer("open_help", "unused_tool", "approved")
    store.record_offer("open_help", "used_tool", "approved")
    store.record_tool_run("used_tool")
    store.record_offer("open_help", "declined_tool", "declined")
    store.record_offer("next_step", "one_off", "approved", one_shot=True)
    clock.advance(29 * DAY)
    assert store.removal_candidates() == []
    clock.advance(1 * DAY)
    rows = store.removal_candidates()
    assert [r["tool_name"] for r in rows] == ["unused_tool"]
    assert rows[0]["days_unused"] == 30
    store.dismiss_removal("unused_tool")
    assert store.removal_candidates() == []


def test_deleted_tool_is_not_listed(store, clock) -> None:
    store.record_offer("open_help", "gone", "approved")
    store.record_deleted("gone")
    clock.advance(31 * DAY)
    assert store.removal_candidates() == []


# ---- decision log ----------------------------------------------------------


@pytest.fixture()
def log(tmp_path, clock) -> DecisionLog:
    return DecisionLog(tmp_path / "approval_decisions.json", clock=clock)


def test_clean_approvals_count_and_reset(log) -> None:
    for _ in range(4):
        log.record("move_file", "pdf_download", "approved")
    assert log.clean_approvals("move_file", "pdf_download") == 4
    log.record("move_file", "pdf_download", "approved")
    assert log.clean_approvals("move_file", "pdf_download") >= 5
    log.record("move_file", "pdf_download", "approved", edited_field_names=["destination"])
    assert log.clean_approvals("move_file", "pdf_download") == 0   # an edit resets
    log.record("move_file", "pdf_download", "approved")
    log.record("move_file", "pdf_download", "denied")
    assert log.clean_approvals("move_file", "pdf_download") == 0   # a denial resets
    log.record("move_file", "screenshot", "approved")
    assert log.clean_approvals("move_file", "pdf_download") == 0   # other trigger kinds apart
    assert log.clean_approvals("move_file", "screenshot") == 1


def test_clean_approvals_by_arg_pattern(log) -> None:
    pdf = {"ext": ".pdf", "folder": "Finance"}
    for _ in range(3):
        log.record("move_file", "file_created", "approved", arg_pattern=pdf)
    log.record("move_file", "file_created", "approved", arg_pattern={"ext": ".png"})
    assert log.clean_approvals("move_file", "file_created", arg_pattern=pdf) == 3
    assert log.clean_approvals("move_file", "file_created") == 4
    log.record("move_file", "file_created", "denied", arg_pattern={"ext": ".png"})
    assert log.clean_approvals("move_file", "file_created", arg_pattern=pdf) == 0


def test_denials_per_tool_and_trigger(log) -> None:
    for _ in range(2):
        log.record("draft_email", "mail_received", "denied")
    log.record("draft_email", "calendar_invite", "denied")
    assert log.denials("draft_email", "mail_received") == 2
    log.record("draft_email", "mail_received", "approved")
    log.record("draft_email", "mail_received", "denied")
    assert log.denials("draft_email", "mail_received") == 3
    assert log.denials("draft_email", "mail_received", since_last_approval=True) == 1


def test_edits_per_field_with_facts(log) -> None:
    orig = {"to": "a@b.c", "body": "a long draft body here", "subject": "Hi"}
    shorter = dict(orig, body="short")
    facts = edit_facts(orig, shorter)
    assert facts == {"body": "shorter"}
    log.record("draft_email", "mail_received", "approved", edited_field_names=facts)
    log.record("draft_email", "mail_received", "edited",
               edited_field_names=edit_facts(orig, dict(orig, body="tiny", subject="Re")))
    edits = log.edits_by_field("draft_email")
    assert edits["body"] == {"total": 2, "shorter": 2}
    assert edits["subject"] == {"total": 1, "changed": 1}
    assert log.decisions[0]["decision"] == "edited"
    assert edit_facts({"a": 1}, {"b": 2}) == {"a": "removed", "b": "added"}


def test_log_keeps_field_names_and_facts_never_values(log, tmp_path) -> None:
    secret_body = "Dear Sam, the merger closes Friday " * 5
    facts = edit_facts({"body": "x"}, {"body": secret_body})
    log.record("draft_email", "mail_received", "edited", edited_field_names=facts,
               note="keep it  short", arg_pattern={"domain": "example.com", "nested": {"x": 1}})
    raw = (tmp_path / "approval_decisions.json").read_text()
    assert "merger" not in raw and "Dear Sam" not in raw
    row = log.decisions[-1]
    assert row["edited_fields"] == ["body"] and row["edit_facts"] == {"body": "longer"}
    assert row["note"] == "keep it short"
    assert row["arg_pattern"] == {"domain": "example.com"}   # non-scalar dropped
    assert stat.S_IMODE((tmp_path / "approval_decisions.json").stat().st_mode) == 0o600
    assert log.notes() == [{"tool_name": "draft_email", "trigger_kind": "mail_received",
                            "decision": "edited", "note": "keep it short", "at": row["at"]}]
    assert len(DecisionLog(tmp_path / "approval_decisions.json").decisions) == 1


def test_decision_validation_and_note_cap(log) -> None:
    with pytest.raises(ValueError):
        log.record("t", "k", "maybe")
    row = log.record("t", "k", "edited", note="n" * 999)  # edited with no fields -> approved
    assert row["decision"] == "approved" and len(row["note"]) == io.MAX_NOTE


def test_trigger_kind_of() -> None:
    assert trigger_kind_of("pattern_detected", {"trigger": "pdf_download"}) == "pdf_download"
    assert trigger_kind_of("pattern_detected", {}) == "pattern_detected"
    assert trigger_kind_of("file_created") == "file_created"


# ---- routes ----------------------------------------------------------------


@pytest.fixture()
def api(store, log, clock):
    store.record_offer("open_help", "unused_tool", "approved")
    store.record_offer("friction", "copy_helper", "declined")
    log.record("move_file", "pdf_download", "approved")
    clock.advance(31 * DAY)
    app = FastAPI()
    app.include_router(outcomes.router)
    app.add_middleware(auth.AuthMiddleware)
    app.state.idea_outcomes = store
    app.state.approval_decisions = log
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=store)


def test_stats_route(api) -> None:
    body = api.client.get("/api/outcomes/stats", headers=auth.auth_header()).json()
    open_help = _row(body["sources"], "source", "open_help")
    assert open_help["approval_rate"] == 1.0 and open_help["kept_rate"] == 0.0
    assert open_help["threshold_bump"] == 0  # one settled offer is too few
    assert _row(body["sources"], "source", "friction")["declined"] == 1
    assert body["decisions"][0]["clean_streak"] == 1


def test_removal_routes(api) -> None:
    h = auth.auth_header()
    rows = api.client.get("/api/outcomes/removal-candidates", headers=h).json()
    assert [r["tool_name"] for r in rows] == ["unused_tool"]
    assert api.client.post("/api/outcomes/removal-candidates/unused_tool/keep",
                           headers=h).status_code == 200
    assert api.client.get("/api/outcomes/removal-candidates", headers=h).json() == []
    assert api.client.post("/api/outcomes/removal-candidates/nope/keep",
                           headers=h).status_code == 404


@pytest.mark.parametrize("method,path", [
    ("get", "/api/outcomes/stats"),
    ("get", "/api/outcomes/removal-candidates"),
    ("post", "/api/outcomes/removal-candidates/unused_tool/keep"),
])
def test_routes_need_the_token(api, method, path) -> None:
    assert getattr(api.client, method)(path).status_code == 401


def test_routes_503_when_not_wired() -> None:
    app = FastAPI()
    app.include_router(outcomes.router)
    with TestClient(app) as client:
        assert client.get("/api/outcomes/stats").status_code == 503
