"""LLM-as-judge: score free text against a yes/no rubric (todo #133).

Only for output with no single right answer (a drafted email, a report).
Suites with a right answer (``tool_calls``, ``safety``) keep their rule
checks: cheaper, the same every run, and more reliable than a judge.

Each rubric line is a yes/no question about the output. The judge writes a
one-line reason, then a verdict, per line. A case passes only if every line
is "yes". A line the judge's reply doesn't answer in a form we can read is
"no" with reason "unparseable", never a pass.

The judge must be a different model from the one under test
(:func:`check_distinct`). It may be a cloud model because eval cases are
made-up data; it is used only by ``evals/`` and never inside the daemon, and
not by ``ai/model_eval.py`` (the #120 gate, local data only).

Calibration (decision 6): ``evals/cases/judge_calibration.json`` holds about
20 made-up outputs the user grades by hand. Before a judged suite counts,
the judge must agree with those labels on at least 85% of rubric lines::

    python -m evals.judge --judge-provider groq
    python -m evals.judge --judge-provider groq --judge-model openai/gpt-oss-20b
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional

CALIBRATION_PATH = Path(__file__).resolve().parent / "cases" / "judge_calibration.json"
MIN_AGREEMENT = 0.85
JUDGE_MAX_TOKENS = 1024
UNPARSEABLE = "unparseable"

JUDGE_SYSTEM_PROMPT = """You grade a piece of text against a numbered list of yes/no questions.
Answer every question about the TEXT only, strictly and literally. If the text
does not clearly satisfy a question, the verdict is "no".

Reply with exactly one JSON object per question, one per line, in order, and
nothing else (no prose, no code fences). Write the reason BEFORE the verdict:
{"q": 1, "reason": "<one short sentence>", "verdict": "yes"}
{"q": 2, "reason": "<one short sentence>", "verdict": "no"}"""


class SameModelError(ValueError):
    """The judge and the model under test are the same model."""


def check_distinct(run_provider: Optional[str], run_model: Optional[str],
                   judge_provider: Optional[str], judge_model: Optional[str]) -> None:
    """Refuse a judge that is the model under test (decision 3)."""
    if (str(run_provider or ""), str(run_model or "")) == (str(judge_provider or ""), str(judge_model or "")):
        raise SameModelError(
            f"the judge ({judge_provider} {judge_model}) is the model under test: a model "
            f"grading its own output proves little. Pass --judge-provider and/or "
            f"--judge-model for a different model."
        )


def build_prompt(output: str, rubric: list[str], context: str = "") -> str:
    parts = []
    if context:
        parts.append(f"CONTEXT (what the text was written for):\n{context.strip()}\n")
    parts.append(f"TEXT:\n<<<\n{output}\n>>>\n")
    parts.append("QUESTIONS:")
    parts.extend(f"{i}. {q}" for i, q in enumerate(rubric, 1))
    return "\n".join(parts)


def _objects(reply: str) -> list[dict[str, Any]]:
    """Every JSON object in ``reply``: one per line, or a JSON array."""
    text = re.sub(r"```(?:json)?", "", reply or "").strip()
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, list):
        return [o for o in whole if isinstance(o, dict)]
    if isinstance(whole, dict):
        return [whole]
    found = []
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


def parse_reply(reply: str, rubric: list[str]) -> list[dict[str, Any]]:
    """``[{"q", "verdict": "yes"|"no", "reason"}]``, one per rubric line.

    A line is matched by its ``q`` number, else by position. A missing line,
    or a verdict that isn't plainly yes or no, is "no" / "unparseable".
    """
    objs = _objects(reply)
    by_number: dict[int, dict[str, Any]] = {}
    for obj in objs:
        try:
            by_number.setdefault(int(obj.get("q")), obj)
        except (TypeError, ValueError):
            pass
    lines = []
    for i, question in enumerate(rubric, 1):
        obj = by_number.get(i)
        if obj is None and not by_number and i <= len(objs):
            obj = objs[i - 1]
        verdict = str((obj or {}).get("verdict", "")).strip().lower()
        if verdict in ("yes", "no"):
            reason = str(obj.get("reason") or "").strip()
        else:
            verdict, reason = "no", UNPARSEABLE
        lines.append({"q": question, "verdict": verdict, "reason": reason})
    return lines


async def _ask(engine: Any, prompt: str) -> str:
    gen = getattr(engine, "generate_chat_sync", None)
    if gen is None:
        raise TypeError(f"{type(engine).__name__} has no generate_chat_sync")
    return await asyncio.to_thread(gen, JUDGE_SYSTEM_PROMPT, prompt, JUDGE_MAX_TOKENS)


async def score(output: str, rubric: list[str], engine: Any, *, context: str = "") -> dict[str, Any]:
    """Grade ``output`` against ``rubric`` with the judge ``engine``.

    Returns ``{"ok", "lines": [{"q", "verdict", "reason"}], "failed": [q, ...]}``;
    ``ok`` is true only if every line is "yes".
    """
    if not rubric:
        raise ValueError("a judged case needs a non-empty rubric")
    reply = await _ask(engine, build_prompt(output, rubric, context))
    lines = parse_reply(reply, rubric)
    failed = [line["q"] for line in lines if line["verdict"] != "yes"]
    return {"ok": not failed, "lines": lines, "failed": failed}


def failure_reason(scored: dict[str, Any]) -> str:
    """One line naming the failing rubric lines and the judge's reasons."""
    bad = [line for line in scored["lines"] if line["verdict"] != "yes"]
    return "; ".join(f"no: {line['q']} ({line['reason']})" for line in bad)


# --- calibration (decision 6) ----------------------------------------------

def load_calibration(path: Optional[Path] = None) -> list[dict[str, Any]]:
    return json.loads(Path(path or CALIBRATION_PATH).read_text())["items"]


def ungraded(items: list[dict[str, Any]]) -> list[str]:
    """Ids of items with any rubric line the user hasn't labelled yes/no."""
    return [it["id"] for it in items
            if any(label not in ("yes", "no") for label in it["labels"])]


def agreement(items: list[dict[str, Any]], judged: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Share of rubric lines where the judge's verdict equals the hand label.

    ``judged`` maps item id -> one ``score()["lines"]`` per repeat; every
    repeat counts, so a flaky judge lowers agreement.
    """
    total = same = 0
    disagreements = []
    for item in items:
        for lines in judged.get(item["id"], []):
            for label, line in zip(item["labels"], lines):
                total += 1
                if line["verdict"] == label:
                    same += 1
                else:
                    disagreements.append({"id": item["id"], "q": line["q"], "label": label,
                                          "judge": line["verdict"], "reason": line["reason"]})
    return {"lines": total, "agreed": same,
            "agreement": round(same / total, 3) if total else 0.0,
            "disagreements": disagreements}


async def calibrate(items: list[dict[str, Any]], engine: Any, *, repeats: int = 1) -> dict[str, Any]:
    missing = ungraded(items)
    if missing:
        raise ValueError(f"{len(missing)} calibration item(s) not graded yet: {', '.join(missing)}. "
                         f"Set every 'labels' entry to \"yes\" or \"no\" first.")
    judged: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        for _ in range(repeats):
            scored = await score(item["output"], item["rubric"], engine, context=item.get("context", ""))
            judged.setdefault(item["id"], []).append(scored["lines"])
    return agreement(items, judged)


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - live check
    from providers import PROVIDERS

    parser = argparse.ArgumentParser(description="Check the eval judge against hand labels.")
    parser.add_argument("--judge-provider", required=True, choices=PROVIDERS)
    parser.add_argument("--judge-model")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--cases", type=Path, help="another calibration file")
    parser.add_argument("--min-agreement", type=float, default=MIN_AGREEMENT)
    args = parser.parse_args(argv)

    from config import settings
    from evals.run import build_engine

    items = load_calibration(args.cases)
    missing = ungraded(items)
    if missing:
        parser.error(f"{len(missing)} of {len(items)} items are ungraded ({', '.join(missing[:5])}"
                     f"{', ...' if len(missing) > 5 else ''}): set each 'labels' entry in "
                     f"{args.cases or CALIBRATION_PATH} to \"yes\" or \"no\"")
    engine = build_engine("main", settings, args.judge_model, provider=args.judge_provider)
    result = asyncio.run(calibrate(items, engine, repeats=args.repeats))
    for d in result["disagreements"]:
        print(f"  DIFF {d['id']}: {d['q']} -- you: {d['label']}, judge: {d['judge']} ({d['reason']})")
    print(f"judge {args.judge_provider} {engine.model_name}: agreement {result['agreement']:.0%} "
          f"({result['agreed']}/{result['lines']} rubric lines), need {args.min_agreement:.0%}")
    return 0 if result["agreement"] >= args.min_agreement else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
