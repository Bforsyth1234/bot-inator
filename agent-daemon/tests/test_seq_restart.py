"""#132: the orchestrator's frame ``seq`` only grows across daemon restarts.

The app keeps frames from earlier daemon runs, so a restarted daemon that
counted from 0 again would reuse seq values the app already holds.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai  # noqa: F401,E402  - must precede ``tools`` (circular import)
import ai.orchestrator as orchestrator_mod  # noqa: E402
from ai.mlx_engine import MLXEngine  # noqa: E402
from ai.orchestrator import Orchestrator  # noqa: E402
from events.event_bus import EventBus  # noqa: E402


def _orchestrator() -> Orchestrator:
    return Orchestrator(engine=MLXEngine(), event_bus=EventBus(), tools=[])


class _Clock:
    """Stands in for ``datetime`` in ai.orchestrator; ``now`` is settable."""

    at = datetime(2026, 11, 19, 15, 41, 25, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001
        return cls.at


def test_fresh_orchestrator_seq_is_above_an_earlier_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator_mod, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "at", _Clock.at)  # restored after the test
    earlier = _orchestrator()
    last = 0
    for _ in range(10_000):  # the earlier run sent plenty of frames
        last = earlier._next_seq()
    _Clock.at += timedelta(seconds=2)  # the daemon restarts 2 s later
    restarted = _orchestrator()
    assert restarted._next_seq() > last


def test_seeded_seq_fits_swift_int_and_json_doubles() -> None:
    seq = _orchestrator()._next_seq()
    assert 0 < seq < 2**53  # exact as a JSON double; Swift Int is 2**63
