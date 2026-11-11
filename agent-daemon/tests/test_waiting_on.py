"""Tests for the "waiting on" tracker (todo #73), on an Envelope Index fixture."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai import privacy
from ai import waiting_on as wo

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
ME = "me@example.com"

SENT = "imap://ACCT/%5BGmail%5D/Sent%20Mail"
ALL = "imap://ACCT/%5BGmail%5D/All%20Mail"
INBOX = "imap://ACCT/INBOX"
DRAFTS = "imap://ACCT/Drafts"


class Index:
    """A minimal Envelope Index with the live layout's relevant columns."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
            CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
            CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
            CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, message_id INTEGER,
                global_message_id INTEGER, sender INTEGER, subject INTEGER,
                date_sent INTEGER, date_received INTEGER, mailbox INTEGER,
                conversation_id INTEGER, list_id_hash INTEGER, unsubscribe_type INTEGER);
            CREATE TABLE recipients (ROWID INTEGER PRIMARY KEY, message INTEGER,
                address INTEGER, type INTEGER, position INTEGER);
        """)
        self.boxes = {}
        for url in (SENT, ALL, INBOX, DRAFTS):
            self.boxes[url] = self.conn.execute("INSERT INTO mailboxes (url) VALUES (?)", (url,)).lastrowid
        self.next_gid = 1000

    def _addr(self, address: str, name: str = "") -> int:
        row = self.conn.execute("SELECT ROWID FROM addresses WHERE address = ?", (address,)).fetchone()
        if row:
            return row[0]
        return self.conn.execute("INSERT INTO addresses (address, comment) VALUES (?, ?)", (address, name)).lastrowid

    def add(self, *, sender: str, subject: str, days_ago: float, box: str = SENT,
            to=(), cc=(), conv=None, gid=None, list_id=None, rowid=None) -> int:
        sid = self.conn.execute("INSERT INTO subjects (subject) VALUES (?)", (subject,)).lastrowid
        when = int((NOW - timedelta(days=days_ago)).timestamp())
        if gid is None:
            self.next_gid += 1
            gid = self.next_gid
        mid = self.conn.execute(
            "INSERT INTO messages (ROWID, global_message_id, sender, subject, date_sent, date_received,"
            " mailbox, conversation_id, list_id_hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (rowid, gid, self._addr(sender), sid, when, when, self.boxes[box], conv, list_id),
        ).lastrowid
        for rtype, people in ((0, to), (1, cc)):
            for pos, person in enumerate(people):
                address, _, name = person.partition("|")
                self.conn.execute(
                    "INSERT INTO recipients (message, address, type, position) VALUES (?,?,?,?)",
                    (mid, self._addr(address, name), rtype, pos),
                )
        self.conn.commit()
        return mid


@pytest.fixture
def index(tmp_path: Path) -> Index:
    return Index(tmp_path / "Envelope Index")


def run(index: Index, bodies=None, **kw):
    reads: list[list[int]] = []

    def read_bodies(ids):
        reads.append(list(ids))
        return {i: (bodies or {}).get(i, "") for i in ids}

    result = wo.waiting_on(NOW, days=3, db_path=index.path, read_bodies=read_bodies,
                           redact_subject=kw.pop("redact_subject", lambda s: s), **kw)
    result["_reads"] = reads
    return result


def subjects(result) -> list[str]:
    return [i["subject"] for i in result["items"]]


# ---- the Verify list ------------------------------------------------------------

def test_unanswered_question_is_listed(index: Index) -> None:
    mid = index.add(sender=ME, subject="Budget numbers?", days_ago=5, to=["sam@x.com|Sam Lee"], conv=1)
    result = run(index)
    assert result["status"] == "ok"
    assert [i["message_id"] for i in result["items"]] == [mid]
    item = result["items"][0]
    assert item["line"] == "waiting on Sam Lee: Budget numbers?"
    assert item["days"] == 5 and item["to"] == ["sam@x.com"]


def test_answered_question_is_not_listed(index: Index) -> None:
    index.add(sender=ME, subject="Budget numbers?", days_ago=5, to=["sam@x.com"], conv=1)
    index.add(sender="sam@x.com", subject="Re: Budget numbers?", days_ago=4, box=INBOX, conv=1)
    result = run(index)
    assert result["items"] == [] and result["skipped"] == {"answered": 1}


def test_sent_yesterday_is_not_listed(index: Index) -> None:
    index.add(sender=ME, subject="Lunch Thursday?", days_ago=1, to=["sam@x.com"], conv=1)
    result = run(index)
    assert result["items"] == [] and result["skipped"] == {"too_recent": 1}


def test_bulk_mail_is_not_listed(index: Index) -> None:
    index.add(sender=ME, subject="Can you all vote?", days_ago=5, to=["list@x.com"], conv=1, list_id=77)
    result = run(index)
    assert result["items"] == [] and result["skipped"] == {"bulk": 1}


def test_no_question_mail_is_not_listed(index: Index) -> None:
    mid = index.add(sender=ME, subject="The report", days_ago=5, to=["sam@x.com"], conv=1)
    result = run(index, bodies={mid: "Here it is.\nCheers"})
    assert result["items"] == [] and result["skipped"] == {"no_question": 1}
    # The subject asked nothing, so the local body was read (and only then).
    assert result["_reads"] == [[mid]]


# ---- the rest of the rules ----------------------------------------------------------

def test_question_in_body_is_listed_and_body_never_returned(index: Index) -> None:
    mid = index.add(sender=ME, subject="The report", days_ago=4, to=["sam@x.com"], conv=1)
    result = run(index, bodies={mid: "Attached. Could you sign page 3?\n\n> old quoted text?"})
    assert [i["message_id"] for i in result["items"]] == [mid]
    assert "sign page" not in repr(result["items"])


def test_question_only_in_quoted_history_is_not_a_question(index: Index) -> None:
    mid = index.add(sender=ME, subject="Re: The report", days_ago=4, to=["sam@x.com"], conv=1)
    result = run(index, bodies={mid: "Thanks, got it.\n\nOn Mon, Sam wrote:\n> can you send it?"})
    assert result["items"] == []


def test_reply_subject_question_mark_is_inherited(index: Index) -> None:
    mid = index.add(sender=ME, subject="Re: Lunch?", days_ago=4, to=["sam@x.com"], conv=1)
    result = run(index, bodies={mid: "Sounds good, see you there."})
    assert result["items"] == [] and result["_reads"] == [[mid]]


def test_subject_question_skips_body_read(index: Index) -> None:
    index.add(sender=ME, subject="Free Friday?", days_ago=4, to=["sam@x.com"], conv=1)
    result = run(index)
    assert len(result["items"]) == 1 and result["_reads"] == []


def test_reply_from_someone_else_does_not_count(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com"], conv=1)
    index.add(sender="other@x.com", subject="Re: Budget?", days_ago=4, box=INBOX, conv=1)
    assert len(run(index)["items"]) == 1


def test_reply_before_the_sent_message_does_not_count(index: Index) -> None:
    index.add(sender="sam@x.com", subject="Budget", days_ago=6, box=INBOX, conv=1)
    mid = index.add(sender=ME, subject="Re: Budget", days_ago=5, to=["sam@x.com"], conv=1)
    assert len(run(index, bodies={mid: "Can you resend it?"})["items"]) == 1


def test_reply_in_another_thread_does_not_count(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com"], conv=1)
    index.add(sender="sam@x.com", subject="Weekend", days_ago=4, box=INBOX, conv=2)
    assert len(run(index)["items"]) == 1


def test_reply_without_conversation_id_matches_by_subject(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com"], conv=1)
    index.add(sender="sam@x.com", subject="RE: Budget?", days_ago=4, box=INBOX, conv=None)
    assert run(index)["items"] == []


def test_draft_from_recipient_mailbox_is_not_a_reply(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com"], conv=1)
    index.add(sender="sam@x.com", subject="Re: Budget?", days_ago=4, box=DRAFTS, conv=1)
    assert len(run(index)["items"]) == 1


def test_later_thanks_from_user_closes_the_thread(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=6, to=["sam@x.com"], conv=1)
    mid = index.add(sender=ME, subject="Re: Budget?", days_ago=4, to=["sam@x.com"], conv=1)
    result = run(index, bodies={mid: "Never mind, found it. Thanks"})
    assert result["items"] == []


def test_gmail_label_duplicate_listed_once(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com"], conv=1, gid=55)
    index.add(sender=ME, subject="Budget?", days_ago=5, box=ALL, to=["sam@x.com"], conv=1, gid=55)
    assert len(run(index)["items"]) == 1


def test_automated_recipient_and_broadcast_excluded(index: Index) -> None:
    index.add(sender=ME, subject="Why was I charged?", days_ago=5, to=["no-reply@shop.com"], conv=1)
    index.add(sender=ME, subject="Who is in?", days_ago=5, conv=2,
              to=[f"p{i}@x.com" for i in range(wo.MAX_RECIPIENTS + 1)])
    result = run(index)
    assert result["items"] == []
    assert result["skipped"] == {"automated": 1, "too_many_recipients": 1}


def test_older_than_window_is_ignored(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=20, to=["sam@x.com"], conv=1)
    assert run(index)["items"] == []


def test_wait_days_changes_the_cutoff(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=2, to=["sam@x.com"], conv=1)
    assert run(index)["items"] == []
    assert len(wo.waiting_on(NOW, days=1, db_path=index.path, read_bodies=dict,
                             redact_subject=lambda s: s)["items"]) == 1


def test_name_falls_back_to_address_and_counts_others(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com", "ana@x.com|Ana"], conv=1)
    assert run(index)["items"][0]["name"] == "sam@x.com +1"


def test_oldest_first_and_limit(index: Index) -> None:
    for n in range(4):
        index.add(sender=ME, subject=f"Q{n}?", days_ago=4 + n, to=["sam@x.com"], conv=n + 1)
    result = run(index, limit=2)
    assert subjects(result) == ["Q3?", "Q2?"] and result["truncated"] is True


# ---- privacy -------------------------------------------------------------------------

def test_cloud_engine_sees_placeholder_subject(index: Index, monkeypatch) -> None:
    index.add(sender=ME, subject="Your divorce papers?", days_ago=5, to=["sam@x.com"], conv=1)
    monkeypatch.setattr(privacy, "should_redact", lambda role: role == "main")
    result = wo.waiting_on(NOW, days=3, db_path=index.path, read_bodies=dict)
    item = result["items"][0]
    assert item["subject"] == privacy.PLACEHOLDER
    assert "divorce" not in repr(result)


def test_local_engine_keeps_subject(index: Index, monkeypatch) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com"], conv=1)
    monkeypatch.setattr(privacy, "should_redact", lambda role: False)
    assert subjects(wo.waiting_on(NOW, days=3, db_path=index.path, read_bodies=dict)) == ["Budget?"]


def test_index_opened_read_only(index: Index) -> None:
    index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com"], conv=1)
    index.conn.close()
    os.chmod(index.path, 0o444)
    try:
        assert len(run(index)["items"]) == 1
    finally:
        os.chmod(index.path, 0o644)


def test_default_read_bodies_reads_local_emlx(tmp_path: Path) -> None:
    folder = tmp_path / "V10" / "ACCT" / "Sent.mbox" / "Messages"
    folder.mkdir(parents=True)
    raw = b"From: me@example.com\r\nTo: sam@x.com\r\nSubject: x\r\n\r\nCan you check?\r\n"
    (folder / "12.emlx").write_bytes(f"{len(raw)}\n".encode() + raw + b"<?xml?><plist/>")
    (folder / "13.partial.emlx").write_bytes(f"{len(raw)}\n".encode() + raw)
    bodies = wo.default_read_bodies([12, 13, 99], tmp_path)
    assert set(bodies) == {12, 13} and "Can you check?" in bodies[12]


# ---- unavailable ---------------------------------------------------------------------

def test_missing_index_is_unavailable(tmp_path: Path) -> None:
    result = wo.waiting_on(NOW, days=3, mail_root=tmp_path)
    assert result == {"status": "unavailable", "reason": "no Mail index found (is the Mail app set up?)."}
    assert wo.format_section(result) == ["Waiting on: unavailable: no Mail index found (is the Mail app set up?)."]


def test_schema_change_is_unavailable(index: Index) -> None:
    index.conn.execute("DROP TABLE recipients")
    index.conn.commit()
    result = run(index)
    assert result["status"] == "unavailable" and "recipients" in result["reason"]


def test_no_sent_mailbox_is_empty(tmp_path: Path) -> None:
    index = Index(tmp_path / "Envelope Index")
    index.conn.execute("DELETE FROM mailboxes WHERE url = ?", (SENT,))
    index.conn.commit()
    assert run(index)["items"] == []


# ---- brief section + nudge offer ------------------------------------------------------

def test_format_section(index: Index) -> None:
    mid = index.add(sender=ME, subject="Re: Budget?", days_ago=5, to=["sam@x.com|Sam"], conv=1)
    lines = wo.format_section(run(index, bodies={mid: "Any news on this?"}))
    assert lines == ["Waiting on (1):", f"- waiting on Sam: Budget? (5 days) [id {mid}]"]
    assert wo.format_section({"status": "ok", "items": []}) == ["Waiting on: nothing unanswered"]


def test_nudge_idea_is_an_unsent_draft_offer(index: Index) -> None:
    mid = index.add(sender=ME, subject="Budget?", days_ago=5, to=["sam@x.com|Sam"], conv=1)
    idea = wo.nudge_idea(run(index)["items"][0])
    assert idea["builtin_tool"] == "draft_email"
    assert idea["reply_to_message_id"] == mid
    assert idea["tool_name"] == "draft_email_nudge" and idea["source"] == "waiting_on"
    assert f"reply_to_message_id: {mid}" in idea["event_details"]
    assert "Budget?" in idea["event_details"] and "Budget?" not in idea["event_details_redacted"]
    assert privacy.PLACEHOLDER in idea["event_details_redacted"]
    assert "sam@x.com" in idea["expected_logic"] and "never say the email was sent" in idea["expected_logic"]
    assert "send_email" not in repr(idea)
