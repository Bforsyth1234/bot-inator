"""Idea judge: does the open-help filter pass and drop the right ideas?

Cases are the recorded event windows in
``tests/fixtures/open_help_windows.json``. Each carries the idea a
generator produced, so only the filter (checks, grounding, judge) is
measured. ``ok`` means the pass/drop verdict matches ``expected``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ai.open_help import (
    IDEA_JUDGE_SYSTEM_PROMPT,
    JUDGE_MAX_TOKENS,
    evaluate_windows,
    load_windows,
)
from ai.pattern_recognizer import make_detection_evaluator
from config import detection_role


def load_cases(path: Optional[Path] = None) -> list[dict[str, Any]]:
    return load_windows(path)


def role(settings: Any) -> str:
    return detection_role(settings.pattern_detection_engine)


async def run_case(case: dict[str, Any], engine: Any, settings: Any) -> dict[str, Any]:
    judge = make_detection_evaluator(
        engine, max_tokens=JUDGE_MAX_TOKENS, system_prompt=IDEA_JUDGE_SYSTEM_PROMPT,
    )
    (result,) = await evaluate_windows(
        [case], judge=judge, min_score=settings.open_help_min_score,
    )
    return result
