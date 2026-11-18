"""Wave 24 runner wiring: #120 base-share routing and comparison records."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from events import remote_channel
from tests.test_wave13_wiring import _answer_soon, _approval_orch


class _Comparison:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def record(self, offer_id: str, source: str) -> None:
        self.records.append((offer_id, source))


class _PM:
    def __init__(self, active: bool, source: str) -> None:
        self.adapter_active, self.source = active, source
        self.base_turns = 0
        self.comparison = _Comparison()

    def pick_source(self) -> str:
        return self.source

    @contextlib.asynccontextmanager
    async def using_base(self):
        self.base_turns += 1
        yield "toggle"


def _turn(pm: _PM, kind: str = "event") -> Any:
    async def run() -> Any:
        orch = _approval_orch()
        orch.personal_model = pm
        orch._agent = None
        seen: list[str] = []

        async def fallback(event_id: str, prompt: str) -> str:
            seen.append(orch._turn_model_source)
            return "no action"

        orch._fallback_run = fallback  # type: ignore[method-assign]
        orch._turn_kind = kind
        await orch._run_agent("e1", "p")
        return seen, orch

    return asyncio.run(run())


def test_base_share_turn_runs_inside_using_base() -> None:
    pm = _PM(active=True, source="base")
    seen, _ = _turn(pm)
    assert pm.base_turns == 1 and seen == ["base"]


def test_adapter_turn_and_chat_turns_skip_the_base_share() -> None:
    pm = _PM(active=True, source="adapter")
    seen, _ = _turn(pm)
    assert pm.base_turns == 0 and seen == ["adapter"]
    pm = _PM(active=True, source="base")
    seen, _ = _turn(pm, kind="chat")
    assert pm.base_turns == 0 and seen == [""]


def test_card_records_which_model_made_it() -> None:
    async def run() -> _PM:
        pm = _PM(active=True, source="adapter")
        orch = _approval_orch()
        orch.personal_model = pm
        orch._turn_model_source = "base"
        orch._record_offer_shown = lambda tool, args: "off_1"  # type: ignore[method-assign]
        _answer_soon(orch, approved=False)
        await orch._request_approval("e1", "move_file", {"kwargs": {"src": "a", "dst_dir": "b"}}, "r")
        return pm

    pm = asyncio.run(run())
    assert pm.comparison.records == [("off_1", "base")]


def test_switch_card_is_never_approvable_from_the_phone() -> None:
    assert "use_personal_model" in remote_channel.NEVER_FROM_PHONE
