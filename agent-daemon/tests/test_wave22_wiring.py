"""Wave 22 runner wiring: card decisions reach the #118 training corpus."""
from __future__ import annotations

import asyncio

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.training_corpus import TrainingCorpus
from tests.test_wave13_wiring import _answer_soon, _approval_orch


def _corpus(on: bool) -> TrainingCorpus:
    return TrainingCorpus(None, enabled=lambda: on, keep_mail_text=lambda: False)


def _decide(corpus: TrainingCorpus, approved: bool) -> None:
    async def run() -> None:
        orch = _approval_orch()
        orch.training_corpus = corpus
        orch._current_event_type = "file_created"
        orch._current_prompt = "A PDF landed in Downloads. How could I help?"
        _answer_soon(orch, approved=approved)
        await orch._request_approval(
            "e1", "move_file", {"kwargs": {"src": "~/Downloads/a.pdf", "dst": "~/Documents"}}, "tidy",
        )
        orch._pending.clear()

    asyncio.run(run())


def test_approved_and_denied_cards_are_recorded_with_their_prompt() -> None:
    corpus = _corpus(True)
    _decide(corpus, approved=True)
    _decide(corpus, approved=False)
    rows = corpus.rows(10, 0)
    assert sorted(r["verdict"] for r in rows) == ["approved", "denied"]
    assert all("PDF landed" in r["prompt"] for r in rows)
    assert all(r["tool_name"] == "move_file" for r in rows)


def test_opt_in_off_records_nothing_through_the_approval_path() -> None:
    corpus = _corpus(False)
    _decide(corpus, approved=True)
    assert corpus.count() == 0
