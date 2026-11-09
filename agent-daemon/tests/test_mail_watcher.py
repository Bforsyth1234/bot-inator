"""Tests for events.mail_watcher and tools.read_email (todo #41)."""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
from events.event_bus import EventBus
from events import mail_watcher as mw
from events.mail_watcher import MailWatcher, is_own_mailbox, mailbox_name
import importlib

# Once registered, ``tools.read_email`` is the tool, not the module.
re_mod = importlib.import_module("tools.read_email")
from tools.read_email import read_email, read_email_from

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "mail"

SCHEMA_SQL = """
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
CREATE TABLE messages (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    subject INTEGER, sender INTEGER, date_received INTEGER, mailbox INTEGER
);
INSERT INTO mailboxes VALUES (1, 'imap://UUID-1/INBOX');
INSERT INTO mailboxes VALUES (2, 'imap://UUID-1/%5BGmail%5D/Sent%20Mail');
INSERT INTO addresses VALUES (1, 'alice@example.com', 'Alice Example');
INSERT INTO addresses VALUES (2, 'me@example.com', '');
INSERT INTO subjects VALUES (1, 'historical');
INSERT INTO messages (subject, sender, date_received, mailbox) VALUES (1, 1, 1700000000, 1);
"""


def _make_index(root: Path, sql: str = SCHEMA_SQL) -> sqlite3.Connection:
    path = root / "V10" / "MailData" / "Envelope Index"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(sql)
    conn.commit()
    return conn


def _add(conn, subject: str, sender: int, mailbox: int) -> None:
    cur = conn.execute("INSERT INTO subjects (subject) VALUES (?)", (subject,))
    conn.execute(
        "INSERT INTO messages (subject, sender, date_received, mailbox) "
        "VALUES (?, ?, 1762711200, ?)", (cur.lastrowid, sender, mailbox),
    )
    conn.commit()


def _run_watcher(root: Path, during=None) -> list:
    loop = asyncio.new_event_loop()
    try:
        bus = EventBus()
        bus.bind_loop(loop)
        watcher = MailWatcher(bus, mail_root=root, poll_interval=0.05)
        watcher.start()
        time.sleep(0.15)
        if during:
            during()
        time.sleep(0.25)
        watcher.stop()
        events = []
        while True:
            try:
                events.append(loop.run_until_complete(
                    asyncio.wait_for(bus.consume(), timeout=0.05)))
            except asyncio.TimeoutError:
                return events
    finally:
        loop.close()


def test_mail_watcher_emits_new_received_mail_only(tmp_path) -> None:
    conn = _make_index(tmp_path)

    def during():
        _add(conn, "Lunch on Friday?", sender=1, mailbox=1)
        _add(conn, "my own sent mail", sender=2, mailbox=2)

    events = _run_watcher(tmp_path, during)
    subjects = [e.metadata["subject"] for e in events]
    assert subjects == ["Lunch on Friday?"]
    ev = events[0]
    assert ev.event_type == "mail_received"
    assert ev.metadata["sender"] == "Alice Example <alice@example.com>"
    assert ev.metadata["mailbox"] == "INBOX"
    assert ev.metadata["message_id"] == 2
    assert ev.metadata["date"].startswith("2025-11-09T")
    assert set(ev.metadata) == {"sender", "subject", "date", "mailbox", "message_id"}


def test_mail_watcher_missing_index_idles(tmp_path) -> None:
    assert _run_watcher(tmp_path / "nope") == []


def test_mail_watcher_schema_mismatch_logs_once_and_idles(tmp_path, caplog) -> None:
    _make_index(tmp_path, "CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, x TEXT);")
    with caplog.at_level(logging.WARNING, logger="events.mail_watcher"):
        assert _run_watcher(tmp_path) == []
    hits = [r for r in caplog.records if "schema mismatch" in r.getMessage()]
    assert len(hits) == 1
    assert "missing table 'subjects'" in hits[0].getMessage()
    assert "messages.date_received" in hits[0].getMessage()


def test_mailbox_classification() -> None:
    assert mailbox_name("imap://U/%5BGmail%5D/Sent%20Mail") == "[Gmail]/Sent Mail"
    for url in ("imap://U/Sent%20Messages", "ews://U/Sent%20Items",
                "local://U/Drafts", "imap://U/INBOX/Sent"):
        assert is_own_mailbox(url), url
    for url in ("imap://U/INBOX", "imap://U/Receipts", "imap://U/Sentinel", None):
        assert not is_own_mailbox(url), url


def test_newest_version_dir_wins(tmp_path) -> None:
    for v in ("V2", "V10", "V9"):
        p = tmp_path / v / "MailData" / "Envelope Index"
        p.parent.mkdir(parents=True)
        p.touch()
    assert mw.find_index(tmp_path).parent.parent.name == "V10"


def test_schema_diagnostic_prints_tables(tmp_path) -> None:
    _make_index(tmp_path)
    out = io.StringIO()
    assert mw.print_schema(tmp_path, out=out) == 0
    text = out.getvalue()
    assert "messages: ROWID, subject, sender, date_received, mailbox" in text
    assert "Schema check: OK" in text
    assert "[own] [Gmail]/Sent Mail" in text
    assert mw.print_schema(tmp_path / "nope", out=io.StringIO()) == 1


# ---- read_email -------------------------------------------------------------


def test_read_email_plain_text_preferred() -> None:
    r = read_email_from(101, FIXTURE_ROOT)
    assert r["status"] == "ok"
    assert r["sender"] == "Alice Example <alice@example.com>"
    assert r["subject"] == "Lunch on Friday?"
    assert "lunch on Friday at noon" in r["body"]
    assert "HTML VERSION" not in r["body"]
    assert "flags" not in r["body"] and "plist" not in r["body"]
    assert r["truncated"] is False and r["partial"] is False


def test_read_email_html_only_is_stripped() -> None:
    r = read_email_from(102, FIXTURE_ROOT)
    assert r["status"] == "ok"
    assert "Tracking number 1Z999AA10123456784" in r["body"]
    assert "Your order & tracking:" in r["body"]
    assert "<" not in r["body"]
    assert "alert" not in r["body"] and "color:red" not in r["body"]
    assert "8590195713" not in r["body"]  # trailing plist stripped


def test_read_email_partial_emlx() -> None:
    r = read_email_from(103, FIXTURE_ROOT)
    assert r["status"] == "ok"
    assert r["partial"] is True
    assert r["body"] == "Here are the slides for tomorrow."


def test_read_email_missing_and_invalid_id() -> None:
    assert read_email_from(999, FIXTURE_ROOT)["status"] == "error"
    assert read_email_from("abc", FIXTURE_ROOT)["status"] == "error"
    assert read_email_from(101, FIXTURE_ROOT / "nope")["status"] == "error"


def test_read_email_truncates_long_body(tmp_path) -> None:
    dest = tmp_path / "V10" / "acct" / "INBOX.mbox" / "Data" / "Messages"
    dest.mkdir(parents=True)
    raw = (b"From: a@example.com\r\nSubject: long\r\n\r\n" + b"word " * 2000)
    (dest / "7.emlx").write_bytes(str(len(raw)).encode() + b"\n" + raw + b"<?xml?>")
    r = read_email_from(7, tmp_path)
    assert r["truncated"] is True
    assert len(r["body"]) <= re_mod.MAX_BODY_CHARS + 1


def test_read_email_tool_uses_mail_root_env(tmp_path, monkeypatch) -> None:
    shutil.copytree(FIXTURE_ROOT, tmp_path / "mail")
    monkeypatch.setenv(re_mod.MAIL_ROOT_ENV, str(tmp_path / "mail"))
    r = read_email(message_id=101)
    assert r["status"] == "ok" and r["subject"] == "Lunch on Friday?"


def test_outbox_and_sendlater_are_own_mail() -> None:
    """Live check (2026-11-09): the real index has Outbox and SendLater."""
    import importlib

    mw = importlib.import_module("events.mail_watcher")
    for url in ("imap://x/Outbox", "imap://x/SendLater", "imap://x/Send%20Later",
                "imap://x/%5BGmail%5D/Sent%20Mail", "imap://x/Sent%20Items"):
        assert mw.is_own_mailbox(url), url
    for url in ("imap://x/INBOX", "imap://x/%5BMailbox%5D/To%20Reply", "imap://x/Job%20Hunt"):
        assert not mw.is_own_mailbox(url), url
