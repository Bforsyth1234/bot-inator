"""Wave 23 runner wiring: the #119 training card and the #79 never-run sweep."""
from __future__ import annotations

import asyncio
from typing import Any

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.idea_outcomes import REMOVAL_AFTER_SECONDS, OutcomeStore
from events import remote_channel
from tests.test_wave13_wiring import _answer_soon, _approval_orch


class _FakeTrainer:
    def __init__(self, due: bool) -> None:
        self.due, self.offered, self.decisions = due, 0, []

    def offer_due(self) -> bool:
        return self.due and not self.offered

    def offer_card(self) -> tuple[str, dict[str, Any], str, str]:
        return ("train_personal_model", {"base_model": "m", "rows": 300, "minutes": 20},
                "enough decisions", "Train your own model? About 20 min, uses the GPU, stays on this Mac")

    def mark_offered(self) -> None:
        self.offered += 1

    async def on_card_decision(self, approved: bool, base_model: Any = None) -> dict[str, Any]:
        self.decisions.append(approved)
        return {}


def _run(trainer: _FakeTrainer, answer_training: bool = True) -> list[str]:
    async def run() -> list[str]:
        orch = _approval_orch()
        orch.personal_training = trainer
        _answer_soon(orch, approved=True)
        await orch._request_approval("e1", "move_file", {"kwargs": {"src": "a", "dst_dir": "b"}}, "r")
        orch._pending.clear()  # the WS resolver pops it in the app
        if answer_training and trainer.due:
            _answer_soon(orch, approved=True)
        for _ in range(200):  # let the follow-up card task run
            await asyncio.sleep(0.01)
            if trainer.decisions or not trainer.due:
                break
        tools = [getattr(getattr(m, "payload", None), "tool_name", "") for m in orch.sent]
        return [t for t in tools if t]

    return asyncio.run(run())


def test_a_decision_past_the_threshold_raises_one_training_card() -> None:
    trainer = _FakeTrainer(due=True)
    tools = _run(trainer)
    assert tools.count("train_personal_model") == 1
    assert trainer.decisions == [True]


def test_no_training_card_below_the_threshold() -> None:
    trainer = _FakeTrainer(due=False)
    tools = _run(trainer)
    assert "train_personal_model" not in tools and trainer.decisions == []


def test_training_card_is_never_approvable_from_the_phone() -> None:
    assert "train_personal_model" in remote_channel.NEVER_FROM_PHONE


def test_never_run_offer_ids_only_old_unused_tools(tmp_path) -> None:
    now = [1_000_000.0]
    store = OutcomeStore(tmp_path / "o.json", clock=lambda: now[0])
    old = store.record_shown("pattern", "tool_a", idea_kind="k")
    store.record_verdict(old, "approved")
    used = store.record_shown("pattern", "tool_b", idea_kind="k")
    store.record_verdict(used, "approved")
    store.record_tool_run("tool_b")
    assert store.never_run_offer_ids() == []  # too early for both
    now[0] += REMOVAL_AFTER_SECONDS + 1
    assert store.never_run_offer_ids() == [old]
