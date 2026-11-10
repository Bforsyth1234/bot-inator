"""Eval suites. Each module exposes:

``load_cases(path=None) -> list[dict]``
    The recorded cases; every case has a unique ``id``.
``role(settings) -> str``
    Which engine the suite measures: ``main``, ``analysis`` or ``drafting``.
``async run_case(case, engine, settings) -> dict``
    One model run of one case. Must include ``ok`` (bool) and ``got``
    (the verdict, used to spot cases that flip between runs).
"""
from __future__ import annotations

from importlib import import_module
from types import ModuleType

SUITES = ("idea_judge", "tool_calls")


def get(name: str) -> ModuleType:
    if name not in SUITES:
        raise ValueError(f"unknown suite {name!r}; choose from {', '.join(SUITES)}")
    return import_module(f"evals.suites.{name}")
