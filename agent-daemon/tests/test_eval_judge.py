"""The LLM-as-judge scorer and the email_draft suite (todo #133), driven by
fake engines: no model calls."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from evals import judge, run, suites
from evals.suites import email_draft

RUBRIC = ["Is it addressed to Sam?", "Does it accept Thursday?", "Is it short?"]


class FakeJudge:
    """A judge engine that replies with canned text and records prompts."""

    def __init__(self, reply: str, model_name: str = "fake/judge-1", provider: str = "fakejudge") -> None:
        self.reply = reply
        self.model_name = model_name
        self.provider = provider
        self.prompts: list[tuple[str, str]] = []

    def generate_chat_sync(self, system: str, user: str, max_tokens: int = 512) -> str:
        self.prompts.append((system, user))
        return self.reply


def _lines(*verdicts: str) -> str:
    return "\n".join(json.dumps({"q": i, "reason": f"reason {i}", "verdict": v})
                     for i, v in enumerate(verdicts, 1))


def _score(reply: str, rubric=RUBRIC):
    return asyncio.run(judge.score("Hi Sam, Thursday works.", rubric, FakeJudge(reply)))


# --- Verify 1: a "no" line fails the case and is named -----------------------

def test_score_fails_on_any_no_and_names_the_line() -> None:
    scored = _score(_lines("yes", "yes", "no"))
    assert scored["ok"] is False
    assert scored["failed"] == ["Is it short?"]
    assert [l["verdict"] for l in scored["lines"]] == ["yes", "yes", "no"]
    assert scored["lines"][2]["reason"] == "reason 3"
    assert "Is it short?" in judge.failure_reason(scored)
    assert _score(_lines("yes", "yes", "yes"))["ok"] is True


# --- Verify 2: an unparseable reply is "no", never a pass --------------------

def test_unparseable_reply_is_no() -> None:
    scored = _score("Sure! All three look great to me.")
    assert scored["ok"] is False
    assert all(l["verdict"] == "no" and l["reason"] == judge.UNPARSEABLE for l in scored["lines"])
    # One good line, one garbled verdict, one missing: only the good line passes.
    partial = _score(json.dumps({"q": 1, "reason": "r", "verdict": "yes"}) + "\n"
                     + json.dumps({"q": 2, "reason": "r", "verdict": "probably"}))
    assert [l["verdict"] for l in partial["lines"]] == ["yes", "no", "no"]
    assert partial["lines"][1]["reason"] == partial["lines"][2]["reason"] == judge.UNPARSEABLE
    assert partial["ok"] is False


def test_parse_accepts_array_and_fences() -> None:
    reply = "```json\n" + json.dumps([{"q": 1, "reason": "a", "verdict": "Yes"},
                                      {"q": 2, "reason": "b", "verdict": "yes"},
                                      {"q": 3, "reason": "c", "verdict": "yes"}]) + "\n```"
    assert _score(reply)["ok"] is True


# --- Verify 3: same provider + model for judge and run is refused ------------

def test_same_model_judge_is_refused() -> None:
    with pytest.raises(judge.SameModelError, match="model under test"):
        judge.check_distinct("groq", "openai/gpt-oss-120b", "groq", "openai/gpt-oss-120b")
    judge.check_distinct("groq", "openai/gpt-oss-120b", "groq", "openai/gpt-oss-20b")
    judge.check_distinct("local", "openai/gpt-oss-120b", "groq", "openai/gpt-oss-120b")


def test_evaluate_refuses_same_model_judge_and_missing_judge() -> None:
    engine = SimpleNamespace(model_name="m/1", provider="groq", loaded=True)
    same = FakeJudge(_lines("yes"), model_name="m/1", provider="groq")
    with pytest.raises(judge.SameModelError):
        asyncio.run(run.evaluate("email_draft", engine=engine, judge=same, save=False,
                                 settings=SimpleNamespace(), cases=[]))
    with pytest.raises(ValueError, match="judged suite"):
        asyncio.run(run.evaluate("email_draft", engine=engine, save=False,
                                 settings=SimpleNamespace(), cases=[]))


# --- Verify 4: --min-accuracy exits 1 below it --------------------------------

def _fake_result(accuracy: float) -> dict:
    return {"suite": "safety", "provider": "local", "model": "fake/m", "adapter": None,
            "started_at": "2026-11-19T10:00:00", "cases": [],
            "summary": {"cases": 10, "runs": 10, "ok_runs": int(accuracy * 10),
                        "accuracy": accuracy, "solid": 0, "flaky": [], "errors": 0}}


def _main(monkeypatch, accuracy: float, *flags: str) -> int:
    async def fake_evaluate(*args, **kwargs):
        return _fake_result(accuracy)

    monkeypatch.setattr(run, "build_engine", lambda *a, **k: SimpleNamespace(model_name="fake/m"))
    monkeypatch.setattr(run, "evaluate", fake_evaluate)
    monkeypatch.setattr(run, "previous_result", lambda *a, **k: None)
    return run.main(["--suite", "safety", "--no-save", *flags])


def test_min_accuracy_exits_1_below_it(monkeypatch) -> None:
    assert _main(monkeypatch, 0.8, "--min-accuracy", "0.9") == 1
    assert _main(monkeypatch, 0.8, "--min-accuracy", "0.8") == 0
    assert _main(monkeypatch, 0.8) == 0
    assert run.exit_code(_fake_result(0.8), 0.9) == 1


# --- the email_draft suite ----------------------------------------------------

def test_email_draft_cases_are_well_formed() -> None:
    cases = email_draft.load_cases()
    assert "email_draft" in suites.SUITES and suites.get("email_draft") is email_draft
    assert len({c["id"] for c in cases}) == len(cases) >= 10
    for c in cases:
        assert c["kind"] == "chat" and c["text"], c["id"]
        assert c["rubric"] and all(q.endswith("?") for q in c["rubric"]), c["id"]
        mail = c["tool_results"]["read_email"]
        assert "example" in mail["sender"], c["id"]  # made-up addresses only


def test_judge_draft_scores_captured_call_and_keeps_reasons() -> None:
    case = email_draft.load_cases()[0]
    fake = FakeJudge(_lines("yes", "yes", "no", "yes"))
    calls = [{"tool": "read_email", "args": {"message_id": 4301}},
             {"tool": "draft_email", "args": {"to": "sam@example.org", "subject": "",
                                              "body": "Thursday at 10 works!"}}]
    out = asyncio.run(email_draft.judge_draft(case, calls, fake))
    assert out["ok"] is False and out["got"] == "3/4"
    assert case["rubric"][2] in out["reason"] and "reason 3" in out["reason"]
    assert out["judge"]["lines"][2]["reason"] == "reason 3"
    assert "Thursday at 10 works!" in fake.prompts[0][1]
    assert "Coffee next week?" in fake.prompts[0][1]  # the incoming email is context

    none = asyncio.run(email_draft.judge_draft(case, calls[:1], fake))
    assert none["ok"] is False and none["got"] == "no draft" and len(fake.prompts) == 1


def test_run_suite_passes_judge_and_repeats_it() -> None:
    fake = FakeJudge(_lines("yes", "yes", "yes", "yes"))

    class Suite:
        NEEDS_JUDGE = True

        @staticmethod
        async def run_case(case, engine, settings, *, judge=None):
            calls = [{"tool": "draft_email", "args": {"to": "a@example.org", "subject": "s", "body": "b"}}]
            return await email_draft.judge_draft(case, calls, judge)

    case = email_draft.load_cases()[0]
    runs = asyncio.run(run.run_suite(Suite, [case], None, None, repeats=3, judge=fake))
    assert len(fake.prompts) == 3
    assert all(r["ok"] and r["judge"]["lines"] for r in runs[0]["runs"])


# --- calibration --------------------------------------------------------------

def test_calibration_file_is_ungraded_and_agreement_check_runs() -> None:
    items = judge.load_calibration()
    assert len(items) >= 20
    assert all(len(it["labels"]) == len(it["rubric"]) for it in items)
    assert judge.ungraded(items) == [it["id"] for it in items]  # left for the user
    with pytest.raises(ValueError, match="not graded"):
        asyncio.run(judge.calibrate(items, FakeJudge(_lines("yes"))))

    graded = [{**it, "labels": ["yes"] * len(it["rubric"])} for it in items[:2]]
    graded[1]["labels"][0] = "no"
    result = asyncio.run(judge.calibrate(graded, FakeJudge(_lines("yes", "yes", "yes", "yes"))))
    assert result["lines"] == 8 and result["agreed"] == 7
    assert result["agreement"] == 0.875
    assert result["disagreements"][0]["id"] == graded[1]["id"]


def test_run_case_drives_one_agent_turn_and_judges_the_draft() -> None:
    from tests.test_evals import _scripted_model

    case = email_draft.load_cases()[0]
    fake = FakeJudge(_lines("yes", "yes", "yes", "yes"))
    model = _scripted_model(
        ("read_email", {"message_id": 4301}),
        ("draft_email", {"to": "sam@example.org", "subject": "", "body": "Thursday at 10 works.",
                         "reply_to_message_id": 4301}),
    )
    out = asyncio.run(email_draft.run_case(case, None, SimpleNamespace(), judge=fake, model=model))
    assert out["ok"] is True and out["got"] == "4/4"
    assert [c["tool"] for c in out["calls"]] == ["read_email", "draft_email"]
    assert "Thursday at 10 works." in out["draft"]
