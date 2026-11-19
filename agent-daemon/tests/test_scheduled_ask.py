"""#125: a ``time`` watch that asks a saved question and shows the answer.

The ``ask`` action is a time-trigger-only question; a due watch publishes a
``scheduled_ask`` event (never an idea, never a card). The orchestrator runs
it as a background turn ("scheduled") through the normal approval gate:
read-only tools run, side-effecting tools raise their card, building a tool
is refused, the web tools ask first, and the answer is one thought plus one
notification.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai import delivery as dl
from ai import watches as w
from ai.mlx_engine import MLXEngine
from ai.orchestrator import Orchestrator
from events.event_bus import ContextEvent, EventBus
from tests.test_delivery import env, settings  # noqa: F401  - fixtures
from tests.test_injected_shortcut import _FakeTool, _Turn
from tests.test_notices_privacy import _LoopThread, _drain
from tests.test_watches import NOW, TIME_SPEC, _source

QUESTION = "Which emails from this week haven't I answered?"
ASK_SPEC = {
    "trigger": {"kind": "time", "weekdays": ["mon", "tue"], "at": "08:30"},
    "action": {"tool": "ask", "arguments": {"question": QUESTION}},
}


# ── the spec ─────────────────────────────────────────────────────────────

def test_ask_is_valid_on_a_time_trigger_and_words_say_what_it_does() -> None:
    spec = w.validate_spec(ASK_SPEC)
    assert spec["action"] == {"tool": "ask", "arguments": {"question": QUESTION}}
    assert w.is_ask(spec) and not w.is_ask(w.validate_spec(TIME_SPEC))
    words = w.describe(spec)
    assert QUESTION in words and "show you the answer" in words
    assert "still needs your approval" in words


@pytest.mark.parametrize("trigger", [
    {"kind": "file", "folder": "~/Downloads", "extension": "pdf"},
    {"kind": "mail_received", "sender": "acme.com"},
    {"kind": "event_starting_soon", "title_contains": "standup"},
])
def test_ask_is_refused_on_any_other_trigger(trigger) -> None:
    with pytest.raises(w.SpecError) as err:
        w.validate_spec({"trigger": trigger, "action": ASK_SPEC["action"]})
    assert "time trigger" in str(err.value.problems)


@pytest.mark.parametrize("question, problem", [
    ("What did {sender} send?", "{sender}"),
    ("What is on {path}?", "{path}"),
    ("", "needs 'question'"),
    ("x" * (w.MAX_QUESTION + 1), "too long"),
    ("stray } brace", "stray brace"),
])
def test_bad_questions_are_refused(question, problem) -> None:
    with pytest.raises(w.SpecError) as err:
        w.validate_spec({"trigger": ASK_SPEC["trigger"],
                         "action": {"tool": "ask", "arguments": {"question": question}}})
    assert problem in " ".join(err.value.problems)


def test_ask_takes_only_a_question_and_date_is_filled() -> None:
    with pytest.raises(w.SpecError):
        w.validate_spec({"trigger": ASK_SPEC["trigger"],
                         "action": {"tool": "ask", "arguments": {"question": "q", "to": "x@y.z"}}})
    spec = w.validate_spec({"trigger": ASK_SPEC["trigger"],
                            "action": {"tool": "ask", "arguments": {"question": "What is due on {date}?"}}})
    event = w.ask_event({"id": "abc", "spec": spec}, {"date": "2026-11-10"})
    assert event.metadata["question"] == "What is due on 2026-11-10?"


def test_the_compile_prompt_offers_ask_for_time_only() -> None:
    assert "ask(question)" in w.COMPILE_SYSTEM_PROMPT
    assert "for a time trigger only" in w.COMPILE_SYSTEM_PROMPT


# ── firing ───────────────────────────────────────────────────────────────

def test_a_due_ask_publishes_one_scheduled_ask_not_an_idea(tmp_path) -> None:
    src, bus, watch = _source(tmp_path, ASK_SPEC)
    assert src.tick(NOW.replace(hour=8, minute=0)) == []
    [fired] = src.tick(NOW.replace(hour=8, minute=31))
    assert [e.event_type for e in bus.pushed] == ["scheduled_ask"]
    meta = bus.pushed[0].metadata
    assert meta["question"] == QUESTION and meta["watch_id"] == watch["id"]
    assert meta["trigger"] == w.TRIGGER_PREFIX + watch["id"]
    assert "builtin_tool" not in meta and "prepared_offer" not in meta
    assert fired["question"] == QUESTION
    # once a day; not on other weekdays; not when missed by more than the grace
    assert src.tick(NOW.replace(hour=9, minute=0)) == []
    assert src.tick(NOW + timedelta(days=1)) == []
    late = src.store.add(ASK_SPEC, cooldown_minutes=0)
    assert src.tick(NOW.replace(hour=11)) == []
    assert src.store.get(late["id"])["time_fired_on"] == "2026-11-10"
    assert len(bus.pushed) == 1


def test_backtest_lists_the_next_three_runs() -> None:
    result = w.backtest(ASK_SPEC, now=NOW)  # a Tuesday 09:00, after 08:30
    assert result["next_runs"] == [
        "Mon 2026-11-16 08:30", "Tue 2026-11-17 08:30", "Mon 2026-11-23 08:30",
    ]
    assert result["summary"].startswith("Asks next: Mon 2026-11-16 08:30")
    assert result["examples"] == []
    # an ordinary time watch keeps its past examples
    assert w.backtest(TIME_SPEC, now=NOW)["examples"]


def test_delivery_holds_a_scheduled_ask_like_an_offer(env) -> None:  # noqa: F811
    env.d.pause("1h")
    held = env.d.decide(ContextEvent(event_type="scheduled_ask", metadata={"question": "q"}))
    assert held.action == "hold" and held.reason == "paused"


# ── the turn ─────────────────────────────────────────────────────────────

def _scheduled(loop: Any) -> _Turn:
    turn = _Turn(loop, kind="event")
    turn.orch._turn_kind = "scheduled"
    turn.orch._current_event_type = "scheduled_ask"
    turn.orch._current_event_meta = {"question": QUESTION}
    turn.orch._taint.begin_turn("event", event_type="scheduled_ask", metadata={"question": QUESTION})
    return turn


def test_read_only_tools_run_without_a_card_and_others_ask() -> None:
    reader = _FakeTool("list_reminders", {"list_name": {}})
    writer = _FakeTool("create_reminder", {"title": {}, "due": {}, "list_name": {}})
    with _LoopThread() as loop:
        turn = _scheduled(loop)
        turn.call(reader, list_name="")
        turn.call(writer, title="Reply to Sam")
        _drain(loop)
    assert reader.ran == [{"list_name": ""}]
    assert [c["tool_name"] for c in turn.cards] == ["create_reminder"]


def test_building_or_repairing_a_tool_is_refused_before_any_card() -> None:
    gen = _FakeTool("generate_custom_tool", {"tool_name": {}, "description": {}, "expected_logic": {}})
    rep = _FakeTool("repair_generated_tool", {"tool_name": {}})
    with _LoopThread() as loop:
        turn = _scheduled(loop)
        a = turn.call(gen, tool_name="t", description="d", expected_logic="e")
        b = turn.call(rep, tool_name="t")
        _drain(loop)
    assert turn.cards == [] and gen.ran == [] and rep.ran == []
    assert a["error"] == b["error"] == "not_in_a_scheduled_question"


def test_web_search_asks_first_in_a_scheduled_turn() -> None:
    search = _FakeTool("web_search", {"query": {}, "limit": {}}, result={"status": "ok", "results": []})
    with _LoopThread() as loop:
        turn = _scheduled(loop)
        turn.call(search, query="macOS release")
        _drain(loop)
    assert [c["tool_name"] for c in turn.cards] == ["web_search"]


def test_the_handler_runs_the_question_and_shows_the_answer(monkeypatch) -> None:
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    thoughts: list[tuple[str, str]] = []
    shown: list[tuple[str, str]] = []
    prompts: list[str] = []
    kinds: list[str] = []

    async def emit(event_id, kind, text):
        thoughts.append((kind, text))

    async def run_agent(event_id, prompt, max_steps=None):
        prompts.append(prompt)
        kinds.append(orch._turn_kind)
        return "Two: Sam (Tue) and the landlord (Wed)."

    monkeypatch.setattr(orch, "_emit_thought", emit)
    monkeypatch.setattr(orch, "_run_agent", run_agent)
    monkeypatch.setattr(Orchestrator, "_notify_scheduled_ask",
                        staticmethod(lambda q, a: shown.append((q, a))))
    spec = w.validate_spec(ASK_SPEC)
    event = w.ask_event({"id": "abc", "spec": spec}, {"date": "2026-11-10"})
    asyncio.run(orch._handle_scheduled_ask(event, "evt_ask"))
    assert kinds == ["scheduled"]
    assert QUESTION in prompts[0] and "2026-11-10" in prompts[0]
    assert "Do not draft, send, create, move or change anything" in prompts[0]
    assert thoughts[-1] == ("complete", "Two: Sam (Tue) and the landlord (Wed).")
    assert shown == [(QUESTION, "Two: Sam (Tue) and the landlord (Wed).")]
    assert Orchestrator._describe_event(event).startswith("Scheduled question: ")


def test_an_empty_answer_still_says_something(monkeypatch) -> None:
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    shown: list[tuple[str, str]] = []

    async def emit(*_):
        return None

    async def run_agent(*_, **__):
        raise RuntimeError("model down")

    monkeypatch.setattr(orch, "_emit_thought", emit)
    monkeypatch.setattr(orch, "_run_agent", run_agent)
    monkeypatch.setattr(Orchestrator, "_notify_scheduled_ask",
                        staticmethod(lambda q, a: shown.append((q, a))))
    event = ContextEvent(event_type="scheduled_ask", metadata={"question": QUESTION, "date": "d"})
    asyncio.run(orch._handle_scheduled_ask(event, "evt_ask"))
    assert shown and shown[0][1].startswith("No answer this time")


def test_the_event_is_routed_to_the_handler(monkeypatch) -> None:
    orch = Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])
    seen: list[str] = []

    async def handler(event, event_id):
        seen.append(event.event_type)

    async def emit(*_):
        return None

    async def no_agent(*_, **__):
        raise AssertionError("routed to the general agent turn")

    monkeypatch.setattr(orch, "_handle_scheduled_ask", handler)
    monkeypatch.setattr(orch, "_emit_thought", emit)
    monkeypatch.setattr(orch, "_run_agent", no_agent)
    handle = getattr(orch, "_handle_event", None) or getattr(orch, "handle_event")
    asyncio.run(handle(ContextEvent(event_type="scheduled_ask", metadata={"question": "q"})))
    assert seen == ["scheduled_ask"]
