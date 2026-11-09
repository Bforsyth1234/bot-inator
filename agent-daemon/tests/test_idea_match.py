"""Reworded repeats of a declined tool idea are caught (live check request:
"we should not let a reworded one be asked")."""

from __future__ import annotations

import numpy as np

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.idea_match import DeclinedIdeaMatcher, _parse_same
from ai.open_help import IdeaFeedback

# Fake embedding space: each text maps to a fixed direction.
VECS = {
    "send imessage: Send an iMessage to a phone number with text.": [1.0, 0.0, 0.0],
    "send text message: Send a text to a contact.": [0.9, 0.1, 0.0],      # ~0.99 → high
    "read imessages: List my recent iMessages.": [0.6, 0.8, 0.0],          # 0.60 → judge
    "file invoices: Move invoice PDFs.": [0.0, 0.0, 1.0],                  # 0.0 → no
}


def _embed(texts):
    return [np.array(VECS[t]) for t in texts]


def _matcher(judge=None, calls=None):
    fb = IdeaFeedback()
    fb.record("send_imessage", "Send an iMessage to a phone number with text.", False)
    fb.record("file_invoices_ok", "unrelated approved idea", True)

    def counting(prompt: str) -> str:
        calls.append(prompt)
        return judge(prompt)

    return DeclinedIdeaMatcher(fb, embed=_embed, judge=counting if judge else None)


def test_close_rewording_is_declined_without_a_model_call() -> None:
    calls: list[str] = []
    m = _matcher(judge=lambda p: '{"same": false}', calls=calls)
    assert m.match("send_text_message", "Send a text to a contact.") == "send_imessage"
    assert calls == []


def test_borderline_asks_the_judge_once() -> None:
    calls: list[str] = []
    m = _matcher(judge=lambda p: '{"same": false}', calls=calls)
    assert m.match("read_imessages", "List my recent iMessages.") == ""
    assert len(calls) == 1 and "send_imessage" in calls[0]
    calls.clear()
    m2 = _matcher(judge=lambda p: '{"same": true}', calls=calls)
    assert m2.match("read_imessages", "List my recent iMessages.") == "send_imessage"


def test_unrelated_idea_is_not_declined() -> None:
    calls: list[str] = []
    m = _matcher(judge=lambda p: '{"same": true}', calls=calls)
    assert m.match("file_invoices", "Move invoice PDFs.") == ""
    assert calls == []


def test_exact_name_still_declined_without_embeddings() -> None:
    fb = IdeaFeedback()
    fb.record("send_imessage", "Send an iMessage.", False)
    m = DeclinedIdeaMatcher(fb, embed=None)
    m._load_failed = True  # simulate model2vec missing
    assert m.match("send_imessage", "anything") == "send_imessage"
    assert m.match("reply_imessage", "Reply to a message") == ""


def test_parse_same() -> None:
    assert _parse_same('{"same": true}') is True
    assert _parse_same('sure: {"same": false}') is False
    assert _parse_same("dunno") is None
