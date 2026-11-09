"""Idea-source state survives restarts; declined ideas stay declined (todo #32)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.idea_sources import ChatGapDetector, IdeaState, NextStepSuggester
from ai.open_help import IdeaFeedback, OpenHelpSuggester
from events.event_bus import ContextEvent, EventBus

PROPOSAL = json.dumps({
    "tool_name": "install_dmg",
    "description": "Mount a downloaded .dmg and copy the app to /Applications.",
    "expected_logic": "Input: path (str).\n1. hdiutil attach.\n2. Copy .app.",
})
GAP_TURN = ("what's on my calendar today?", "I can't access your calendar.", frozenset())


def _dmg() -> ContextEvent:
    return ContextEvent(event_type="file", metadata={"path": "/Users/me/Downloads/Zoom.dmg", "action": "created"})


def _invite_mail(mid: int = 7) -> ContextEvent:
    return ContextEvent(event_type="mail_received", metadata={
        "sender": "Sam <sam@example.com>", "subject": "Invitation: Sync @ Tue Nov 10, 2026 2pm",
        "date": "2026-11-09T10:00:00", "mailbox": "INBOX", "message_id": mid,
    })


class _Recorder:
    def __init__(self, reply: str = PROPOSAL) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.published: list[ContextEvent] = []

    async def evaluate(self, text: str) -> str:
        self.prompts.append(text)
        return self.reply

    async def publish(self, ev: ContextEvent) -> None:
        self.published.append(ev)


def _next_step(rec: _Recorder, state: IdeaState, feedback=None) -> NextStepSuggester:
    return NextStepSuggester(
        event_bus=EventBus(), evaluate=rec.evaluate, publish=rec.publish,
        state=state, feedback=feedback,
    )


def _drive(source, events) -> None:
    async def scenario():
        tasks = [t for t in (source.observe(e) for e in events) if t]
        await asyncio.gather(*tasks)
    asyncio.run(scenario())


def _drive_chat(source, turns) -> None:
    async def scenario():
        tasks = [t for t in (source.observe_chat(*t) for t in turns) if t]
        await asyncio.gather(*tasks)
    asyncio.run(scenario())


# ---- persistence ------------------------------------------------------

def test_next_step_cooldown_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "idea_state.json"
    first = _Recorder()
    _drive(_next_step(first, IdeaState(path)), [_dmg()])
    assert len(first.published) == 1
    assert path.exists()

    second = _Recorder()  # a new process: fresh instances, same file
    _drive(_next_step(second, IdeaState(path)), [_dmg()])
    assert second.prompts == [] and second.published == []


def test_without_persistence_a_restart_forgets(tmp_path: Path) -> None:
    """Control: the same scenario with no file suggests again."""
    first = _Recorder()
    _drive(_next_step(first, IdeaState()), [_dmg()])
    second = _Recorder()
    _drive(_next_step(second, IdeaState()), [_dmg()])
    assert len(first.published) == len(second.published) == 1


def test_builtin_cooldown_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "idea_state.json"
    first = _Recorder()
    _drive(_next_step(first, IdeaState(path)), [_invite_mail(7)])
    second = _Recorder()
    _drive(_next_step(second, IdeaState(path)), [_invite_mail(7), _invite_mail(8)])
    assert len(first.published) == 1
    # the same email is not offered again; a different one is
    assert [e.metadata["trigger"] for e in second.published] == ["mail_invite"]
    assert "message_id: 8" in second.published[0].metadata["event_details"]


def test_chat_gap_cooldown_survives_restart_and_hashes_text(tmp_path: Path) -> None:
    path = tmp_path / "idea_state.json"
    for expected in (1, 0):
        rec = _Recorder()
        det = ChatGapDetector(
            event_bus=EventBus(), evaluate=rec.evaluate, publish=rec.publish,
            state=IdeaState(path),
        )
        _drive_chat(det, [GAP_TURN])
        assert len(rec.published) == expected
    assert "calendar" not in path.read_text()


def test_open_help_idea_cooldown_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "idea_state.json"
    idea = json.dumps({
        "tool_name": "sort_invoices",
        "description": "Move invoice PDFs from Downloads into the Invoices folder.",
        "expected_logic": "Input: folder (str). List invoice*.pdf files in Downloads and move each into ~/Invoices.",
    })
    events = [
        ContextEvent(event_type="file", metadata={"path": f"/Users/me/Downloads/invoice_{i}.pdf", "action": "created"})
        for i in range(3)
    ]

    def run() -> list[ContextEvent]:
        rec = _Recorder(idea)

        async def judge(_: str) -> str:
            return '{"score": 9, "reason": "moves invoices"}'

        s = OpenHelpSuggester(
            event_bus=EventBus(), evaluate=rec.evaluate, judge=judge,
            feedback=IdeaFeedback(), interval_seconds=0.0, min_events=3,
            publish=rec.publish, state=IdeaState(path),
        )
        _drive(s, events)
        return rec.published

    assert len(run()) == 1
    assert run() == []


def test_expired_entries_pruned_on_load_and_save(tmp_path: Path) -> None:
    path = tmp_path / "idea_state.json"
    now = 1_000_000.0
    path.write_text(json.dumps({"version": 1, "cooldowns": {
        "next_step": {"old": now - 1, "live": now + 60},
        "chat_gap": {"gone": now - 3600},
    }}))
    state = IdeaState(path, clock=lambda: now)
    assert state.sections["next_step"] == {"live": now + 60}
    assert state.sections["chat_gap"] == {}
    assert not state.active("next_step", "old") and state.active("next_step", "live")

    clock = [now + 120]  # "live" has now expired too
    state2 = IdeaState(path, clock=lambda: clock[0])
    assert state2.sections["next_step"] == {}
    state2.start("open_help", "x", 10)
    saved = json.loads(path.read_text())["cooldowns"]
    assert saved["next_step"] == {} and saved["chat_gap"] == {}
    assert saved["open_help"] == {"x": clock[0] + 10}


def test_unreadable_state_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "idea_state.json"
    path.write_text("{not json")
    state = IdeaState(path)
    assert state.sections == {}
    state.start("next_step", "k", 60)
    assert IdeaState(path).active("next_step", "k")


# ---- declined ideas ---------------------------------------------------

def test_declined_next_step_idea_not_offered_again(tmp_path: Path) -> None:
    feedback = IdeaFeedback(tmp_path / "idea_feedback.json")
    feedback.record("install_dmg", "Mount a downloaded .dmg and copy the app.", approved=False)
    rec = _Recorder()
    # cooldown 0 so only the feedback can stop it
    s = NextStepSuggester(
        event_bus=EventBus(), evaluate=rec.evaluate, publish=rec.publish,
        cooldown_seconds=0.0, state=IdeaState(), feedback=IdeaFeedback(tmp_path / "idea_feedback.json"),
    )
    _drive(s, [_dmg()])
    assert len(rec.prompts) == 1 and rec.published == []


def test_approved_next_step_idea_still_offered() -> None:
    feedback = IdeaFeedback()
    feedback.record("install_dmg", "Mount a downloaded .dmg and copy the app.", approved=True)
    rec = _Recorder()
    _drive(_next_step(rec, IdeaState(), feedback), [_dmg()])
    assert len(rec.published) == 1


def test_declined_builtin_kind_not_offered_again() -> None:
    feedback = IdeaFeedback()
    rec = _Recorder()
    _drive(_next_step(rec, IdeaState(), feedback), [_invite_mail(1)])
    name = rec.published[0].metadata["tool_name"]
    feedback.record(name, rec.published[0].metadata["description"], approved=False)
    _drive(_next_step(rec, IdeaState(), feedback), [_invite_mail(2)])
    assert len(rec.published) == 1
    assert rec.prompts == []  # built-in kinds never call the model


def test_declined_chat_gap_idea_not_offered_again() -> None:
    feedback = IdeaFeedback()
    feedback.record("install_dmg", "Mount a downloaded .dmg", approved=False)
    rec = _Recorder()
    det = ChatGapDetector(
        event_bus=EventBus(), evaluate=rec.evaluate, publish=rec.publish,
        state=IdeaState(), feedback=feedback,
    )
    _drive_chat(det, [GAP_TURN])
    assert len(rec.prompts) == 1 and rec.published == []
