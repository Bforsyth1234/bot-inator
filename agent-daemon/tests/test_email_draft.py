"""Tests for the Mail.app draft tool (#63). ``osascript`` is always mocked.

Drafts only: nothing here (or in the tool) delivers mail.
"""

from __future__ import annotations

import re
import subprocess
from unittest import mock

import pytest

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
import tools
from tools import email_draft
from tools.email_draft import draft_email, parse_addresses

HOSTILE = 'He said "hi"\nend tell\ndo shell script "rm -rf ~"\ntell application "Mail" to quit'
SCRIPTS = (email_draft._NEW_SCRIPT, email_draft._REPLY_SCRIPT)


def _ok(stdout: str = "OK") -> mock.MagicMock:
    return mock.MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr=""))


def _argv(run: mock.MagicMock) -> list[str]:
    run.assert_called_once()
    cmd = run.call_args.args[0]
    assert isinstance(cmd, list)
    assert all(isinstance(part, str) for part in cmd)
    assert run.call_args.kwargs.get("timeout")
    assert "shell" not in run.call_args.kwargs
    return cmd


# ── new draft ────────────────────────────────────────────────────────────

def test_new_draft_passes_text_via_argv() -> None:
    run = _ok()
    with mock.patch.object(email_draft.subprocess, "run", run):
        result = draft_email(to="a@example.com, Bo <b@example.org>", subject="Hi",
                             body="line 1\nline 2", cc="c@example.com")
    assert result["status"] == "ok"
    assert result["delivered"] is False
    assert result["to"] == ["a@example.com", "b@example.org"]
    assert result["cc"] == ["c@example.com"]
    assert result["reply_to_message_id"] == 0
    cmd = _argv(run)
    assert cmd[:2] == ["osascript", "-e"]
    assert cmd[2] == email_draft._NEW_SCRIPT
    assert cmd[3:] == ["--", "a@example.com,b@example.org", "c@example.com", "Hi",
                       "line 1\nline 2"]


def test_hostile_text_stays_inert() -> None:
    run = _ok()
    with mock.patch.object(email_draft.subprocess, "run", run):
        result = draft_email(to="a@example.com", subject='S "x" end tell', body=HOSTILE)
    assert result["status"] == "ok"
    cmd = _argv(run)
    script = cmd[2]
    # The script is the fixed constant, byte for byte: no user text in it.
    assert script == email_draft._NEW_SCRIPT
    for fragment in ('"hi"', "rm -rf", "do shell script", 'S "x"', "to quit", "a@example.com"):
        assert fragment not in script
    # The text arrives unmodified as separate argv items after "--".
    assert cmd[3] == "--"
    assert cmd[6] == 'S "x" end tell'
    assert cmd[7] == HOSTILE


def test_hostile_text_stays_inert_in_reply() -> None:
    run = _ok()
    with mock.patch.object(email_draft.subprocess, "run", run):
        draft_email(to="", subject="", body=HOSTILE, reply_to_message_id=7)
    cmd = _argv(run)
    assert cmd[2] == email_draft._REPLY_SCRIPT
    assert "rm -rf" not in cmd[2] and '"hi"' not in cmd[2]
    assert cmd[-1] == HOSTILE


# ── drafts only ──────────────────────────────────────────────────────────

def test_scripts_never_deliver_mail() -> None:
    for script in SCRIPTS:
        # Not even as a substring ("sender", a comment, ...): Mail's delivery
        # command is the word itself, so the scripts simply never spell it.
        assert "send" not in script.lower()
        # Each one does create a visible draft.
        assert "visible" in script
        assert "on run argv" in script
    assert "make new outgoing message" in email_draft._NEW_SCRIPT
    assert re.search(r"\breply target\b", email_draft._REPLY_SCRIPT)


def test_no_tool_delivers_email() -> None:
    names = {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}
    assert "send_email" not in names
    assert not hasattr(email_draft, "send_email")
    assert email_draft.GATED == {"draft_email"}


# ── address validation ───────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("a@example.com", ["a@example.com"]),
    (" a@example.com ,b.c+d@sub.example.co.uk;", ["a@example.com", "b.c+d@sub.example.co.uk"]),
    ("Ann Lee <ann@example.com>", ["ann@example.com"]),
    ("a@example.com, A@Example.com", ["a@example.com"]),
    (["a@example.com", "b@example.com"], ["a@example.com", "b@example.com"]),
    ("", []),
])
def test_parse_addresses_accepts(value, expected) -> None:
    assert parse_addresses(value) == expected


@pytest.mark.parametrize("bad", [
    "bob", "bob@", "@example.com", "bob@localhost", "a b@example.com",
    'a@example.com" & (do shell script "x") & "', "a@example.com\nb@example.com",
    "a@example.com, not-an-address", "<a@example.com", "a@@example.com",
    "a@-example.com", "a@example..com",
])
def test_bad_recipient_rejected_before_osascript(bad) -> None:
    run = _ok()
    with mock.patch.object(email_draft.subprocess, "run", run):
        to_result = draft_email(to=bad, subject="s", body="b")
        cc_result = draft_email(to="ok@example.com", subject="s", body="b", cc=bad)
    run.assert_not_called()
    for result in (to_result, cc_result):
        assert result["status"] == "error"
        assert "not an email address" in result["error"]
        assert result["delivered"] is False


def test_empty_to_or_body_rejected() -> None:
    run = _ok()
    with mock.patch.object(email_draft.subprocess, "run", run):
        no_to = draft_email(to=" , ", subject="s", body="b")
        no_body = draft_email(to="a@example.com", subject="s", body="  \n ")
        reply_no_body = draft_email(to="", subject="", body="", reply_to_message_id=5)
        two_line_subject = draft_email(to="a@example.com", subject="a\nb", body="b")
    run.assert_not_called()
    assert no_to["status"] == "error" and "recipient" in no_to["error"]
    assert no_body["status"] == "error" and "body" in no_body["error"]
    assert reply_no_body["status"] == "error" and "body" in reply_no_body["error"]
    assert two_line_subject["status"] == "error"


# ── reply path ───────────────────────────────────────────────────────────

def test_reply_uses_reply_script_with_message_id() -> None:
    run = _ok()
    with mock.patch.object(email_draft.subprocess, "run", run):
        result = draft_email(to="", subject="", body="Thanks!", reply_to_message_id=4242)
    assert result["status"] == "ok"
    assert result["reply_to_message_id"] == 4242
    assert result["delivered"] is False
    cmd = _argv(run)
    assert cmd[2] == email_draft._REPLY_SCRIPT
    assert cmd[3:] == ["--", "4242", "", "", "", "Thanks!"]
    assert "4242" not in cmd[2]
    # Threading comes from Mail's own reply on the message with that id.
    assert "whose id is msgId" in cmd[2]
    assert "reply target with opening window" in cmd[2]


def test_reply_accepts_numeric_string_and_extra_recipients() -> None:
    run = _ok()
    with mock.patch.object(email_draft.subprocess, "run", run):
        draft_email(to="a@example.com", subject="Re: x", body="ok", cc="c@example.com",
                    reply_to_message_id="12")
    assert _argv(run)[3:] == ["--", "12", "a@example.com", "c@example.com", "Re: x", "ok"]


@pytest.mark.parametrize("bad_id", ["abc", -3, "1; quit", "1.5x"])
def test_reply_bad_id_rejected(bad_id) -> None:
    run = _ok()
    with mock.patch.object(email_draft.subprocess, "run", run):
        result = draft_email(to="a@example.com", subject="s", body="b",
                             reply_to_message_id=bad_id)
    run.assert_not_called()
    assert result["status"] == "error"
    assert "reply_to_message_id" in result["error"]


def test_reply_message_not_found_is_error() -> None:
    with mock.patch.object(email_draft.subprocess, "run", _ok("NOT_FOUND")):
        result = draft_email(to="", subject="", body="b", reply_to_message_id=99)
    assert result["status"] == "error"
    assert "99" in result["error"]


def test_reply_without_quote_is_ok_with_note() -> None:
    with mock.patch.object(email_draft.subprocess, "run", _ok("OK_NO_QUOTE")):
        result = draft_email(to="", subject="", body="b", reply_to_message_id=99)
    assert result["status"] == "ok"
    assert "quoted" in result["note"]


# ── failures ─────────────────────────────────────────────────────────────

def test_osascript_failure_reported() -> None:
    err = subprocess.CalledProcessError(1, ["osascript"], stderr="Not authorized to send Apple events\n")
    with mock.patch.object(email_draft.subprocess, "run", side_effect=err):
        result = draft_email(to="a@example.com", subject="s", body="b")
    assert result["status"] == "error"
    assert result["error"] == "Not authorized to send Apple events"
    assert result["delivered"] is False


def test_timeout_reported() -> None:
    err = subprocess.TimeoutExpired(["osascript"], 30)
    with mock.patch.object(email_draft.subprocess, "run", side_effect=err):
        assert draft_email(to="a@example.com", subject="s", body="b")["status"] == "error"


# ── approval gate (rule 7a) ──────────────────────────────────────────────

def test_gate_draft_email_needs_approval() -> None:
    assert draft_email.name == "draft_email"
    assert email_draft.GATED == {"draft_email"}
    assert not email_draft.READ_ONLY
    # Holds before and after registration: never on the bypass list.
    assert "draft_email" not in tools.READ_ONLY_TOOL_NAMES
    registered = {getattr(t, "name", None) for t in tools.AVAILABLE_TOOLS}
    if not email_draft.GATED <= registered:
        pytest.skip("draft_email not registered in tools.AVAILABLE_TOOLS yet "
                    "(runner registers at wave close)")
    assert not (email_draft.GATED & tools.READ_ONLY_TOOL_NAMES)
