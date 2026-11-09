"""Tests for the friction idea source (#34) and the clipboard watcher."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.friction import (
    FRICTION_JUDGE_SYSTEM_PROMPT,
    FRICTION_SYSTEM_PROMPT,
    FrictionDetector,
    names_an_app,
)
from ai.idea_sources import NO_SUGGESTION
from ai.open_help import IdeaFeedback
from events.clipboard_watcher import ClipboardWatcher, classify_types
from events.event_bus import ContextEvent, EventBus

SECRET = "hunter2-SECRET-CLIPBOARD-CONTENT"

IDEA = {
    "tool_name": "mail_totals_to_numbers",
    "description": "Copy invoice totals from selected Mail messages into the open Numbers sheet.",
    "expected_logic": (
        "Input: sheet (str), column (str). Read the selected messages in Mail via "
        "osascript, extract amounts, append rows to the Numbers sheet. dry_run "
        "defaults to True and only lists the rows."
    ),
}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _act(app: str) -> ContextEvent:
    return ContextEvent(event_type="app_activated", app_name=app)


def _copy(app: Optional[str] = None, kind: str = "text") -> ContextEvent:
    return ContextEvent(event_type="clipboard_changed", app_name=app,
                        metadata={"app": app, "kind": kind})


def _detector(clock: FakeClock, *, idea=IDEA, score: int = 9, **kw: Any):
    calls: dict[str, list[str]] = {"evaluate": [], "judge": []}
    published: list[ContextEvent] = []

    async def evaluate(prompt: str) -> str:
        calls["evaluate"].append(prompt)
        return json.dumps(idea) if idea else NO_SUGGESTION

    async def judge(prompt: str) -> str:
        calls["judge"].append(prompt)
        return json.dumps({"score": score, "reason": "moves totals directly"})

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    det = FrictionDetector(
        event_bus=EventBus(), evaluate=evaluate, judge=judge,
        feedback=kw.pop("feedback", IdeaFeedback()), publish=publish, clock=clock, **kw,
    )
    return det, calls, published


async def _feed(det: FrictionDetector, events: list[tuple[float, ContextEvent]], clock: FakeClock):
    for dt, ev in events:
        clock.t += dt
        task = det.observe(ev)
        if task:
            await task


def _cycles(n: int, a: str = "Mail", b: str = "Numbers", gap: float = 20.0):
    """n rounds of: in A, copy, switch to B, switch back to A."""
    seq: list[tuple[float, ContextEvent]] = [(0.0, _act(a))]
    for _ in range(n):
        seq += [(gap, _copy()), (3.0, _act(b)), (gap, _act(a))]
    return seq


def _run(det, seq, clock):
    asyncio.run(_feed(det, seq, clock))


# ---- detection ---------------------------------------------------------

def test_five_copy_switch_cycles_publish_friction_idea() -> None:
    clock = FakeClock()
    det, calls, published = _detector(clock)
    _run(det, _cycles(5), clock)
    assert len(published) == 1
    meta = published[0].metadata
    assert published[0].event_type == "pattern_detected"
    assert meta["source"] == "friction" and meta["trigger"] == "copy_paste"
    assert meta["apps"] == ["Mail", "Numbers"] and meta["count"] == 5
    assert meta["judge_score"] == 9
    prompt = calls["evaluate"][0]
    assert "Mail" in prompt and "Numbers" in prompt and "5 times" in prompt


def test_four_cycles_is_below_threshold() -> None:
    clock = FakeClock()
    det, calls, published = _detector(clock)
    _run(det, _cycles(4), clock)
    assert published == [] and calls["evaluate"] == []


def test_threshold_is_a_parameter() -> None:
    clock = FakeClock()
    det, _, published = _detector(clock, min_transfers=3)
    _run(det, _cycles(3), clock)
    assert len(published) == 1


def test_cycles_outside_window_do_not_count() -> None:
    clock = FakeClock()
    det, calls, _ = _detector(clock, window_seconds=600.0)
    # each round takes ~5 min, so at most 2-3 fall within 10 minutes
    _run(det, _cycles(6, gap=150.0), clock)
    assert calls["evaluate"] == []


def test_switch_long_after_copy_is_not_a_transfer() -> None:
    clock = FakeClock()
    det, calls, _ = _detector(clock, copy_to_switch_seconds=60.0)
    seq = [(0.0, _act("Mail"))]
    for _ in range(6):
        seq += [(1.0, _copy()), (90.0, _act("Numbers")), (5.0, _act("Mail"))]
    _run(det, seq, clock)
    assert calls["evaluate"] == []


def test_copy_attributed_to_last_activated_app_not_watcher_field() -> None:
    clock = FakeClock()
    det, _, published = _detector(clock)
    seq = [(0.0, _act("Mail"))]
    for _ in range(5):
        # the watcher's lsappinfo guess is wrong (says Numbers); ignored
        seq += [(5.0, _copy("Numbers")), (3.0, _act("Numbers")), (5.0, _act("Mail"))]
    _run(det, seq, clock)
    assert published and published[0].metadata["apps"] == ["Mail", "Numbers"]


def test_cooldown_per_app_pair() -> None:
    clock = FakeClock()
    det, calls, published = _detector(clock, cooldown_seconds=3600.0)
    _run(det, _cycles(5), clock)
    _run(det, _cycles(5), clock)  # same pair again, inside the cooldown
    assert len(calls["evaluate"]) == 1 and len(published) == 1
    # a different pair is not blocked
    _run(det, _cycles(5, a="Mail", b="Pages"), clock)
    assert len(published) == 2
    # after the cooldown the first pair can fire again
    clock.t += 3601.0
    _run(det, _cycles(5), clock)
    assert len(published) == 3


def test_cooldown_holds_even_when_judge_rejects() -> None:
    clock = FakeClock()
    det, calls, published = _detector(clock, score=3)
    _run(det, _cycles(5), clock)
    _run(det, _cycles(5), clock)
    assert published == [] and len(calls["judge"]) == 1


def test_ping_pong_without_copies_is_weaker_signal() -> None:
    clock = FakeClock()
    det, calls, published = _detector(clock, pingpong_min_switches=6, score=7)
    seq = [(4.0, _act(app)) for app in ["Mail", "Numbers"] * 4]  # 7 switches
    _run(det, seq, clock)
    assert len(calls["evaluate"]) == 1
    assert "back and forth" in calls["evaluate"][0]
    assert published == []  # score 7 < pingpong_min_score 8

    clock2 = FakeClock()
    det2, _, published2 = _detector(clock2, pingpong_min_switches=6, score=8)
    _run(det2, seq, clock2)
    assert len(published2) == 1 and published2[0].metadata["trigger"] == "ping_pong"


def test_ping_pong_needs_enough_switches_in_window() -> None:
    clock = FakeClock()
    det, calls, _ = _detector(clock, pingpong_min_switches=6, pingpong_window_seconds=60.0)
    _run(det, [(20.0, _act(app)) for app in ["Mail", "Numbers"] * 4], clock)
    assert calls["evaluate"] == []  # 7 switches, but spread over 140 s
    clock2 = FakeClock()
    det2, calls2, _ = _detector(clock2, pingpong_min_switches=6)
    _run(det2, [(4.0, _act(app)) for app in ["Mail", "Numbers", "Notes"] * 3], clock2)
    assert calls2["evaluate"] == []  # three apps: no A<->B run


# ---- filter ------------------------------------------------------------

def test_judge_rejected_idea_is_dropped() -> None:
    clock = FakeClock()
    det, calls, published = _detector(clock, score=4)
    _run(det, _cycles(5), clock)
    assert len(calls["judge"]) == 1 and published == []
    assert det.last_verdict["stage"] == "judge" and det.last_verdict["score"] == 4


def test_malformed_and_ungrounded_ideas_skip_judge() -> None:
    clock = FakeClock()
    det, calls, published = _detector(clock, idea={"tool_name": "X", "description": "", "expected_logic": ""})
    _run(det, _cycles(5), clock)
    assert calls["judge"] == [] and published == []

    unrelated = dict(IDEA, tool_name="clipboard_history",
                     description="Keep a searchable clipboard history.",
                     expected_logic="Input: limit (int). Store each clipboard entry in a "
                                    "local list and show the last N on request; dry_run True.")
    clock2 = FakeClock()
    det2, calls2, _ = _detector(clock2, idea=unrelated)
    _run(det2, _cycles(5), clock2)
    assert calls2["judge"] == [] and det2.last_verdict["stage"] == "grounding"


def test_rejected_feedback_suppresses_and_reaches_judge() -> None:
    fb = IdeaFeedback()
    fb.record(IDEA["tool_name"], IDEA["description"], approved=False)
    clock = FakeClock()
    det, calls, published = _detector(clock, feedback=fb)
    _run(det, _cycles(5), clock)
    assert calls["judge"] == [] and published == []
    assert det.last_verdict["stage"] == "feedback"


def test_record_feedback_respects_shared_store() -> None:
    fb = IdeaFeedback()
    det, _, _ = _detector(FakeClock(), feedback=fb, records_feedback=False)
    det.record_feedback("x_tool", "d", True)
    assert fb.entries == []
    det2, _, _ = _detector(FakeClock(), feedback=fb)
    det2.record_feedback("x_tool", "d", True)
    assert len(fb.entries) == 1


def test_names_an_app() -> None:
    assert names_an_app(IDEA, ("Mail", "Numbers"))
    assert not names_an_app({"tool_name": "foo_bar", "description": "x"}, ("Mail", "Numbers"))


def test_prompts_ask_for_direct_transfer() -> None:
    assert "directly" in FRICTION_SYSTEM_PROMPT and NO_SUGGESTION in FRICTION_SYSTEM_PROMPT
    assert '"score"' in FRICTION_JUDGE_SYSTEM_PROMPT


# ---- privacy -----------------------------------------------------------

class FakePasteboard:
    """changeCount/types only; any content accessor fails the test."""

    def __init__(self, types: list[str]) -> None:
        self.count = 1
        self._types = types

    def changeCount(self) -> int:
        return self.count

    def types(self) -> list[str]:
        return self._types

    def __getattr__(self, name: str):  # stringForType_, dataForType_, ...
        raise AssertionError(f"clipboard content accessor called: {name}")


def test_watcher_emits_app_and_kind_never_content() -> None:
    pb = FakePasteboard(["public.utf8-plain-text"])
    w = ClipboardWatcher(EventBus(), pasteboard_factory=lambda: pb, frontmost_app=lambda: "Mail")
    assert w.poll_once() is None  # baseline
    assert w.poll_once() is None  # unchanged
    pb.count = 2
    ev = w.poll_once()
    assert ev is not None and ev.event_type == "clipboard_changed"
    assert ev.metadata == {"app": "Mail", "kind": "text"} and ev.app_name == "Mail"
    assert SECRET not in repr(ev)


def test_concealed_copies_are_not_reported() -> None:
    pb = FakePasteboard(["public.utf8-plain-text", "org.nspasteboard.ConcealedType"])
    w = ClipboardWatcher(EventBus(), pasteboard_factory=lambda: pb, frontmost_app=lambda: "1Password")
    w.poll_once()
    pb.count = 2
    assert w.poll_once() is None


def test_classify_types() -> None:
    assert classify_types(["public.file-url", "public.utf8-plain-text"]) == "file"
    assert classify_types(["public.png"]) == "image"
    assert classify_types(["public.url", "public.utf8-plain-text"]) == "url"
    assert classify_types(["com.example.custom"]) == "other"
    assert classify_types([]) is None


def test_published_idea_carries_no_clipboard_content() -> None:
    clock = FakeClock()
    det, calls, published = _detector(clock)
    seq = [(0.0, _act("Mail"))]
    for _ in range(5):
        # even if an event somehow carried content, it must not reach prompts
        ev = _copy(kind="text")
        ev.metadata["text"] = SECRET
        seq += [(5.0, ev), (3.0, _act("Numbers")), (5.0, _act("Mail"))]
    _run(det, seq, clock)
    blob = json.dumps([published[0].metadata, calls])
    assert SECRET not in blob
