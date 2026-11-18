"""Personal: on the user's own held-out decisions (#118 corpus, frozen split),
would this model have made the offers the user approved, and only those,
and how close are its drafts to what the user finally sent? (#121 decision 2)

Cases are built from corpus rows, never from a file: :func:`cases_from_corpus`
(the frozen held-out rows) or :func:`build_cases` (any rows). Each case
replays the row's stored prompt through the real smolagents adapter with
``tool_calls``'s stub tools (a call is recorded and answered with a canned
result; nothing runs, no approval is asked).

Scoring (:func:`score`):

* **Offer accuracy.** An approved / edited row (not undone) is ``ok`` when
  the model calls the row's tool. A denied row, or an approved one the user
  undid, is ``ok`` when it does NOT call that tool. Other tools don't count.
* **Draft closeness.** For approved / edited rows whose final arguments
  have text fields (:data:`TEXT_FIELDS`): for the model's first call to the
  row's tool, ``difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()``
  per field (0..1; texts casefolded, whitespace collapsed, capped at
  :data:`MAX_TEXT_CHARS`), averaged over the fields; a field the model left
  out scores 0. A run where the model made no such call has closeness
  ``None`` and is left out of the mean (offer accuracy already counts it).

Rows that can't be replayed are skipped and listed: no prompt, a prompt
stripped of email text (#118 decision 1), or a tool that is not a built-in
(the stub set is ``tools.AVAILABLE_TOOLS``; generated tools are not
offered, as in ``tool_calls``).

Results are the user's data (decision 8): they are NEVER saved to
``evals/results/``. :func:`evaluate_personal` always calls
``evals.run.evaluate(..., save=False)``; :func:`load_cases` refuses, so
``python -m evals.run --suite personal`` (which saves) stops before any
model runs. Use ``python -m evals.suites.personal [--adapter DIR]``.
"""
from __future__ import annotations

import asyncio
import difflib
import re
import sys
from typing import Any, Iterable, Mapping, Optional

from evals.suites import tool_calls

NEVER_SAVE = True  # for evals.run: this suite's results stay out of evals/results/

TEXT_FIELDS = ("body", "title", "notes", "note", "text", "subject", "message",
               "content", "description")
MAX_TEXT_CHARS = 4000
CHAT_MAX_STEPS = tool_calls.CHAT_MAX_STEPS


def load_cases(path: Any = None) -> list[dict[str, Any]]:
    raise ValueError(
        "personal cases come from the training corpus's held-out rows and their results "
        "must not be saved to evals/results/; use evals.suites.personal.evaluate_personal(...) "
        "or `python -m evals.suites.personal`"
    )


def role(settings: Any) -> str:
    return "main"


# --- cases ------------------------------------------------------------------

def _max_steps(event_type: str) -> int:
    # Step budgets as Orchestrator._handle_user_message / _handle_event.
    if event_type in ("user_message", ""):
        return CHAT_MAX_STEPS
    return 3 if event_type == "mail_received" else 2


def _text_fields(args: Any) -> dict[str, str]:
    if not isinstance(args, Mapping):
        return {}
    return {k: v for k, v in args.items() if k in TEXT_FIELDS and isinstance(v, str) and v.strip()}


def _tool_names() -> set[str]:
    import ai  # noqa: F401  - must precede ``tools`` (circular import)
    from tools import AVAILABLE_TOOLS

    return {t.name for t in AVAILABLE_TOOLS}


def build_cases(
    rows: Iterable[Mapping[str, Any]], *, tool_names: Optional[set[str]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """``(cases, skipped)`` from corpus rows. ``skipped`` is
    ``[{"id", "reason"}]`` for rows that can't be replayed."""
    from ai.training_corpus import STRIPPED_PROMPT

    names = _tool_names() if tool_names is None else tool_names
    cases, skipped = [], []
    for row in rows:
        rid = str(row.get("id"))
        prompt = str(row.get("prompt") or "")
        tool = str(row.get("tool_name") or "")
        verdict = row.get("verdict")
        if verdict not in ("approved", "edited", "denied"):
            skipped.append({"id": rid, "reason": f"verdict {verdict}"})
        elif not prompt.strip():
            skipped.append({"id": rid, "reason": "no prompt"})
        elif prompt == STRIPPED_PROMPT:
            skipped.append({"id": rid, "reason": "prompt not kept (email or message text)"})
        elif tool not in names:
            skipped.append({"id": rid, "reason": f"tool {tool!r} is not a built-in"})
        else:
            negative = verdict == "denied" or row.get("outcome") == "undone"
            final = row.get("final_args")
            if final is None:
                final = (row.get("card") or {}).get("args")
            cases.append({
                "id": rid,
                "expected": "no_offer" if negative else "offer",
                "tool": tool,
                "prompt": prompt,
                "event_type": str(row.get("event_type") or ""),
                "final_text": {} if negative else _text_fields(final),
            })
    return cases, skipped


def cases_from_corpus(corpus: Any) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """The frozen held-out rows (#118 ``heldout_rows``) as cases.
    Empty before #119 freezes a split."""
    return build_cases(corpus.heldout_rows())


# --- scoring (pure) ---------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().casefold()[:MAX_TEXT_CHARS]


def similarity(a: str, b: str) -> float:
    """``difflib`` ratio of the normalised texts (1.0 = same, 0.0 = nothing shared)."""
    a, b = _norm(a), _norm(b)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def closeness(drafted: Mapping[str, Any], final_text: Mapping[str, str]) -> tuple[Optional[float], dict[str, float]]:
    """Mean similarity over ``final_text``'s fields (a missing field scores 0)."""
    if not final_text:
        return None, {}
    fields = {k: (similarity(drafted[k], v) if isinstance(drafted.get(k), str) else 0.0)
              for k, v in final_text.items()}
    return sum(fields.values()) / len(fields), fields


def score(case: Mapping[str, Any], calls: list[Mapping[str, Any]]) -> dict[str, Any]:
    """``{"ok", "got", "reason", "closeness", "fields"}`` for recorded calls."""
    mine = [c for c in calls if c.get("tool") == case["tool"]]
    offered = bool(mine)
    want = case["expected"] == "offer"
    ok = offered == want
    reason = "" if ok else (f"did not call {case['tool']}" if want else f"called {case['tool']}, which the user refused")
    value, fields = (None, {})
    if want and offered:
        value, fields = closeness(mine[0].get("args") or {}, case.get("final_text") or {})
    return {"ok": ok, "got": "offer" if offered else "no_offer", "reason": reason,
            "closeness": None if value is None else round(value, 4), "fields": fields}


def personal_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    """Offer accuracy and draft closeness, overall and per repeat, from an
    ``evaluate`` result of this suite."""
    cases = result.get("cases") or []
    repeats = max((len(c["runs"]) for c in cases), default=0)
    per_repeat, close_rep = [], []
    for i in range(repeats):
        runs = [c["runs"][i] for c in cases if len(c["runs"]) > i]
        per_repeat.append(sum(bool(r.get("ok")) for r in runs) / len(runs) if runs else 0.0)
        vals = [r["closeness"] for r in runs if r.get("closeness") is not None]
        close_rep.append(sum(vals) / len(vals) if vals else None)
    all_runs = [r for c in cases for r in c["runs"]]
    vals = [r["closeness"] for r in all_runs if r.get("closeness") is not None]
    return {
        "cases": len(cases),
        "offers": sum(1 for c in cases if c.get("expected") == "offer"),
        "no_offers": sum(1 for c in cases if c.get("expected") == "no_offer"),
        "offer_accuracy": (sum(bool(r.get("ok")) for r in all_runs) / len(all_runs)) if all_runs else None,
        "offer_per_repeat": per_repeat,
        "closeness": sum(vals) / len(vals) if vals else None,
        "closeness_per_repeat": close_rep,
        "closeness_n": len(vals),
        "errors": sum(1 for r in all_runs if r.get("error")),
    }


# --- running ----------------------------------------------------------------

async def run_case(case: dict[str, Any], engine: Any, settings: Any, *, model: Any = None) -> dict[str, Any]:
    """Replay the row's prompt once. ``model`` (a smolagents ``Model``)
    replaces the engine's adapter in tests."""
    from smolagents import ToolCallingAgent

    from tools import AVAILABLE_TOOLS

    if model is None:
        from ai.orchestrator import Orchestrator
        from events.event_bus import EventBus

        if not getattr(engine, "loaded", True):
            await engine.load()
        model = Orchestrator(engine=engine, event_bus=EventBus(), tools=[])._build_smolagents_model()
        if model is None:
            raise RuntimeError(f"no smolagents model adapter for {type(engine).__name__}")
    calls: list[dict[str, Any]] = []
    agent = ToolCallingAgent(tools=tool_calls.stub_tools(list(AVAILABLE_TOOLS), calls),
                             model=model, verbosity_level=0)
    steps = _max_steps(case.get("event_type", ""))
    await asyncio.to_thread(lambda: agent.run(case["prompt"], max_steps=steps))
    out = score(case, calls)
    # Tool names only: the drafted text stays out of the result.
    out["tools"] = [c["tool"] for c in calls]
    out["step_errors"] = tool_calls._step_errors(agent)
    return out


async def evaluate_personal(
    source: Any,
    *,
    engine: Any = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    adapter: Optional[str] = None,
    repeats: int = 3,
    settings: Any = None,
    progress: Any = None,
    suite: Any = None,
) -> dict[str, Any]:
    """Run the personal suite on ``source`` (a ``TrainingCorpus`` → its
    held-out rows, or a list of rows). Never saved. Returns ``evaluate``'s
    dict plus ``personal`` (:func:`personal_summary`) and ``skipped``.
    ``suite`` replaces this module (tests)."""
    from evals import run as eval_run

    if hasattr(source, "heldout_rows"):
        cases, skipped = cases_from_corpus(source)
    else:
        cases, skipped = build_cases(source)
    result = await eval_run.evaluate(
        suite or sys.modules[__name__], provider=provider, model=model,
        adapter=adapter, repeats=repeats, save=False, settings=settings, engine=engine,
        cases=cases, progress=progress,
    )
    result.pop("saved_to", None)
    result["suite"] = "personal"
    result["personal"] = personal_summary(result)
    result["skipped"] = skipped
    return result


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - live check
    import argparse
    from pathlib import Path

    from ai.training_corpus import TrainingCorpus, default_path
    from evals import run as eval_run

    parser = argparse.ArgumentParser(description="Personal suite on the held-out rows (never saved)")
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)
    from config import settings

    corpus = TrainingCorpus(args.db or default_path(), enabled=lambda: True)
    adapter = str(args.adapter.expanduser()) if args.adapter else None
    engine = eval_run.build_engine("main", settings, args.model, adapter=adapter)
    result = asyncio.run(evaluate_personal(corpus, engine=engine, repeats=args.repeats,
                                           settings=settings))
    p = result["personal"]
    print(eval_run.report(result))
    print(f"offer accuracy {p['offer_accuracy']}, per repeat {p['offer_per_repeat']}; "
          f"closeness {p['closeness']} over {p['closeness_n']} drafts; skipped {len(result['skipped'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
