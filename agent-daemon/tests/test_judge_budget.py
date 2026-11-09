"""Judge calls need room for reasoning models (#54).

GPT-OSS on Groq returned an empty reply at max_tokens=120, so every judged
idea (open-help, friction, setup scan) was dropped as unparseable.
"""

from __future__ import annotations

import re
from pathlib import Path

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.open_help import JUDGE_MAX_TOKENS

ROOT = Path(__file__).resolve().parent.parent


def test_judge_budget_leaves_room_for_reasoning() -> None:
    assert JUDGE_MAX_TOKENS >= 400


def test_no_judge_evaluator_uses_a_small_literal_budget() -> None:
    for rel in ("main.py", "ai/open_help.py"):
        src = (ROOT / rel).read_text()
        small = [int(n) for n in re.findall(r"max_tokens=(\d+)", src) if int(n) < 400]
        assert small == [], f"{rel} has judge budgets {small}"
