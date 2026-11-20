"""Email drafts, scored by an LLM judge (todo #133).

A drafted reply has no single right answer, so a rubric of yes/no
questions is graded by a judge model (:mod:`evals.judge`), a different
model from the one under test. ``python -m evals.run --suite email_draft
--judge-provider groq``.

The run is ``tool_calls``'s: one real agent turn (real prompt, real
smolagents adapter, built-in tool schemas, stub tools). The user asks in
chat for a reply to an email; ``read_email`` is stubbed with the made-up
incoming email, and the ``draft_email`` call's ``to`` / ``subject`` /
``body`` are captured and judged. No ``draft_email`` call is a failed run
and the judge isn't asked.

Case shape (``evals/cases/email_draft.json``)::

    {"id": "...", "kind": "chat", "text": "Reply to Sam's email (message 4301) ...",
     "tool_results": {"read_email": {...}},
     "rubric": ["Is it addressed to Sam?", "Does it accept Thursday?", ...]}

A run is ``ok`` only if every rubric line is "yes" (decision 2). Every run
calls the judge once, so ``--repeats`` repeats the judge too (decision 5).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from evals import judge as judge_mod
from evals.suites import tool_calls

DEFAULT_CASES_PATH = Path(__file__).resolve().parent.parent / "cases" / "email_draft.json"
NEEDS_JUDGE = True


def load_cases(path: Optional[Path] = None) -> list[dict[str, Any]]:
    return json.loads(Path(path or DEFAULT_CASES_PATH).read_text())


def role(settings: Any) -> str:
    return "main"


def draft_text(call: dict[str, Any]) -> str:
    """The captured ``draft_email`` call as the text the judge reads."""
    args = call.get("args") or {}
    return (f"To: {args.get('to', '')}\n"
            f"Subject: {args.get('subject', '')}\n\n"
            f"{args.get('body', '')}")


def context_for(case: dict[str, Any]) -> str:
    """The user's request and the incoming email, for the judge."""
    mail = (case.get("tool_results") or {}).get("read_email") or {}
    return (f"The user asked: {case['text']}\n\n"
            f"The email being replied to:\nFrom: {mail.get('sender', '')}\n"
            f"Subject: {mail.get('subject', '')}\n\n{mail.get('body', '')}")


async def judge_draft(case: dict[str, Any], calls: list[dict[str, Any]], judge: Any) -> dict[str, Any]:
    """Score the last ``draft_email`` call in ``calls`` (pure but for the judge)."""
    drafts = [c for c in calls if c["tool"] == "draft_email"]
    if not drafts:
        got = " > ".join(c["tool"] for c in calls) or "no tool"
        return {"ok": False, "got": "no draft", "reason": f"no draft_email call ({got})", "judge": None}
    draft = draft_text(drafts[-1])
    scored = await judge_mod.score(draft, case["rubric"], judge, context=context_for(case))
    passed = sum(line["verdict"] == "yes" for line in scored["lines"])
    return {
        "ok": scored["ok"],
        "got": f"{passed}/{len(scored['lines'])}",
        "reason": judge_mod.failure_reason(scored),
        "draft": draft,
        "judge": scored,
    }


async def run_case(case: dict[str, Any], engine: Any, settings: Any, *,
                   judge: Any = None, model: Any = None) -> dict[str, Any]:
    if judge is None:
        raise ValueError("email_draft is a judged suite: pass a judge engine (--judge-provider)")
    run = await tool_calls.run_case({**case, "expect": []}, engine, settings, model=model)
    scored = await judge_draft(case, run["calls"], judge)
    return {**run, **scored}
