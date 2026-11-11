"""The deterministic "asked something / needs a reply" test (todo #70)."""

from __future__ import annotations

import pytest

from ai.needs_reply import (
    asks_something,
    extract_addresses,
    first_lines,
    is_automated_sender,
    needs_reply,
    strip_subject_prefix,
)

ME = ["me@example.com"]


def _check(**kw):
    base = dict(
        sender="Sam <sam@other.org>", to="Me <ME@example.com>", cc="",
        subject="Report", body="Can you send me the report by Friday?",
        user_addresses=ME, bulk=False,
    )
    base.update(kw)
    return needs_reply(**base)


def test_direct_question_needs_reply() -> None:
    check = _check()
    assert check and check.reason == "ok"


@pytest.mark.parametrize("kw,reason", [
    ({"bulk": True}, "bulk"),
    ({"sender": "no-reply@shop.com"}, "automated"),
    ({"sender": "Notifications <notifications@github.com>"}, "automated"),
    ({"sender": "MAILER-DAEMON@mx.example.com"}, "automated"),
    ({"user_addresses": []}, "no_user_address"),
    ({"sender": "Me <me@example.com>"}, "from_user"),
    ({"to": "team@other.org", "cc": "me@example.com"}, "cc_only"),
    ({"to": "team@other.org"}, "not_in_to"),
    ({"subject": "Report", "body": "Here is the report. Thanks!"}, "no_question"),
    ({"subject": "FYI", "body": "> Can you send it?\nAttached."}, "no_question"),
])
def test_reasons_for_no(kw, reason) -> None:
    check = _check(**kw)
    assert not check and check.reason == reason


def test_list_of_recipients_and_case() -> None:
    assert _check(to=["a@x.com", "ME@Example.COM"])


@pytest.mark.parametrize("subject,body,asks", [
    ("Lunch Friday?", "", True),
    ("Re: Re: Fwd: budget", "Could you take a look?", True),
    ("Update", "Please let me know what works.", True),
    ("Update", "Do you have the slides from Tuesday", True),
    ("Invoice", "Please find attached the invoice.", False),
    ("Link", "See https://x.com/a?b=c for details.", False),
    ("Thanks", "Thanks for yesterday.\n\nOn Mon, Sam wrote:\n> can you help?", False),
    ("Hi", "\n\n" + "\n".join(f"line {i}" for i in range(10)) + "\nAny thoughts?", False),
])
def test_asks_something(subject, body, asks) -> None:
    assert asks_something(subject, body) is asks


def test_helpers() -> None:
    assert extract_addresses("Sam <Sam@X.com>, b@y.org") == ["sam@x.com", "b@y.org"]
    assert extract_addresses(None) == []
    assert strip_subject_prefix("Re: FWD: Report") == "Report"
    assert first_lines("a\n\n> q\nb\n-----Original Message-----\nc") == ["a", "b"]
    assert is_automated_sender("donotreply@bank.com")
    assert not is_automated_sender("Sam <sam@other.org>")
