"""Tests for the read-only built-in list_recent_emails (todo #103)."""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools
from ai import privacy, taint
from tests.test_notices_privacy import (  # noqa: F401  - providers is a fixture
    PH, SECRET_SUBJECT, _LoopThread, _drain, _orchestrator, providers,
)

lre = importlib.import_module("tools.list_recent_emails")

SCHEMA = """
CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY, subject TEXT);
CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY, address TEXT, comment TEXT);
CREATE TABLE mailboxes (ROWID INTEGER PRIMARY KEY, url TEXT);
CREATE TABLE messages (
    ROWID INTEGER PRIMARY KEY AUTOINCREMENT,
    subject INTEGER, sender INTEGER, date_received INTEGER, mailbox INTEGER,
    global_message_id INTEGER, deleted INTEGER DEFAULT 0
);
INSERT INTO mailboxes VALUES (1, 'imap://UUID-1/INBOX');
INSERT INTO mailboxes VALUES (2, 'imap://UUID-1/%5BGmail%5D/Sent%20Mail');
INSERT INTO mailboxes VALUES (3, 'imap://UUID-1/Junk');
INSERT INTO mailboxes VALUES (4, 'imap://UUID-1/%5BGmail%5D/Trash');
INSERT INTO mailboxes VALUES (5, 'imap://UUID-1/Work');
INSERT INTO mailboxes VALUES (6, 'imap://UUID-1/Drafts');
INSERT INTO addresses VALUES (1, 'alice@example.com', 'Alice Example');
INSERT INTO addresses VALUES (2, 'me@example.com', '');
"""


def _add(conn: sqlite3.Connection, subject: str, date: int, mailbox: int,
         sender: int = 1, global_id: Any = None, deleted: int = 0) -> int:
    cur = conn.execute("INSERT INTO subjects (subject) VALUES (?)", (subject,))
    cur = conn.execute(
        "INSERT INTO messages (subject, sender, date_received, mailbox, global_message_id, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?)", (cur.lastrowid, sender, date, mailbox, global_id, deleted),
    )
    return int(cur.lastrowid)


@pytest.fixture()
def mail_root(tmp_path: Path) -> Path:
    root = tmp_path / "Mail"
    path = root / "V10" / "MailData" / "Envelope Index"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    base = 1_762_700_000
    # Inserted out of date order: newest-first must follow date_received.
    _add(conn, "inbox 3", base + 300, 1)
    _add(conn, "inbox 1", base + 100, 1)
    _add(conn, "inbox 2", base + 200, 1)
    _add(conn, "my reply", base + 900, 2, sender=2)
    _add(conn, "spam offer", base + 800, 3)
    _add(conn, "old trash", base + 700, 4)
    _add(conn, "draft", base + 650, 6, sender=2)
    _add(conn, "work 1", base + 400, 5, global_id=77)
    _add(conn, "work 1", base + 400, 1, global_id=77)  # same mail, Gmail label copy
    _add(conn, "deleted one", base + 600, 1, deleted=1)
    conn.commit()
    conn.close()
    return root


def _subjects(result: dict) -> list[str]:
    assert result["status"] == "ok", result
    return [e["subject"] for e in result["emails"]]


def test_newest_first_skipping_sent_drafts_junk_trash_deleted_and_duplicates(mail_root) -> None:
    result = lre.list_recent_emails_from(mail_root, 10)
    assert _subjects(result) == ["work 1", "inbox 3", "inbox 2", "inbox 1"]
    first = result["emails"][0]
    assert set(first) == {"message_id", "sender", "subject", "date", "mailbox"}
    assert first["sender"] == "Alice Example <alice@example.com>"
    assert first["date"].startswith("2025-") or first["date"].startswith("2026-")
    assert result["count"] == 4


def test_count_caps_at_fifty_and_limits(mail_root) -> None:
    assert _subjects(lre.list_recent_emails_from(mail_root, 2)) == ["work 1", "inbox 3"]
    path = mail_root / "V10" / "MailData" / "Envelope Index"
    conn = sqlite3.connect(path)
    for i in range(80):
        _add(conn, f"bulk {i}", 1_762_800_000 + i, 1)
    conn.commit()
    conn.close()
    result = lre.list_recent_emails_from(mail_root, 500)
    assert result["count"] == lre.MAX_COUNT == 50
    assert result["emails"][0]["subject"] == "bulk 79"
    assert lre.list_recent_emails_from(mail_root, 0)["count"] == 1
    assert lre.list_recent_emails_from(mail_root, "x")["status"] == "error"


def test_mailbox_filter(mail_root) -> None:
    assert _subjects(lre.list_recent_emails_from(mail_root, 10, "work")) == ["work 1"]
    assert _subjects(lre.list_recent_emails_from(mail_root, 10, "INBOX")) == [
        "work 1", "inbox 3", "inbox 2", "inbox 1"]
    # A skipped mailbox stays skipped even when asked for.
    assert _subjects(lre.list_recent_emails_from(mail_root, 10, "Junk")) == []


@pytest.mark.parametrize("url,skipped", [
    ("imap://U/INBOX", False),
    ("imap://U/Work", False),
    ("imap://U/%5BGmail%5D/Sent%20Mail", True),
    ("imap://U/Drafts", True),
    ("imap://U/Junk", True),
    ("imap://U/%5BGmail%5D/Spam", True),
    ("imap://U/%5BGmail%5D/Trash", True),
    ("imap://U/Deleted%20Messages", True),
])
def test_skipped_mailboxes(url: str, skipped: bool) -> None:
    assert lre.is_skipped_mailbox(url) is skipped


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_missing_full_disk_access_says_so(tmp_path: Path) -> None:
    root = tmp_path / "Mail"
    root.mkdir()
    root.chmod(0)
    try:
        result = lre.list_recent_emails_from(root, 5)
    finally:
        root.chmod(0o700)
    assert result == {"status": "error", "error": lre.FDA_ERROR}
    assert "Settings → Permissions → Full Disk Access" in lre.FDA_ERROR


def test_no_index_is_a_clear_error(tmp_path: Path) -> None:
    result = lre.list_recent_emails_from(tmp_path / "nothing", 5)
    assert result["status"] == "error" and result["error"].startswith("no Mail message index")


def test_tool_reads_the_configured_mail_root(mail_root, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MAIL_ROOT", str(mail_root))
    assert _subjects(tools.list_recent_emails(count=1)) == ["work 1"]


def test_registered_read_only_and_marks_the_turn() -> None:
    names = {getattr(t, "name", "") for t in tools.AVAILABLE_TOOLS}
    assert "list_recent_emails" in names
    assert "list_recent_emails" in tools.READ_ONLY_TOOL_NAMES
    assert lre.READ_ONLY <= tools.READ_ONLY_TOOL_NAMES
    # #81: subjects are outside text, so a later card notes it.
    assert taint.OUTSIDE_TEXT_TOOLS["list_recent_emails"] == "email"


# ---- privacy -----------------------------------------------------------------


def _gated_list(orch: Any, loop: asyncio.AbstractEventLoop) -> Any:
    from smolagents import tool

    @tool
    def list_recent_emails(count: int = 10, mailbox: str = "") -> dict:
        """List emails.

        Args:
            count: how many.
            mailbox: which.
        """
        return {"status": "ok", "count": 1, "emails": [{
            "message_id": 321, "sender": "lab@example.com", "subject": SECRET_SUBJECT,
            "date": "d", "mailbox": "INBOX"}]}

    wrapped = orch._wrap_tool_for_approval(list_recent_emails)
    return asyncio.run_coroutine_threadsafe(
        asyncio.to_thread(wrapped.forward, 5), loop,
    ).result(5)


def test_subjects_redacted_for_a_cloud_main_engine(providers, monkeypatch) -> None:
    with _LoopThread() as loop:
        orch, frames = _orchestrator(loop)
        providers(analysis="groq")                    # main local
        local = _gated_list(orch, loop)
        providers(main="groq")
        cloud = _gated_list(orch, loop)
        monkeypatch.setenv(privacy.OPT_IN_ENV, "1")
        opted_in = _gated_list(orch, loop)
        _drain(loop)
    assert local["emails"][0]["subject"] == SECRET_SUBJECT
    assert cloud["emails"][0]["subject"] == PH
    assert cloud["emails"][0]["sender"] == "lab@example.com"
    assert cloud["emails"][0]["message_id"] == 321
    assert "note" in cloud and "note" not in cloud["emails"][0]
    assert opted_in["emails"][0]["subject"] == SECRET_SUBJECT
    shown = [f.payload.content for f in frames
             if f.payload.content.startswith("list_recent_emails")]
    assert [SECRET_SUBJECT in c for c in shown] == [True, False, True]


def test_redact_result_leaves_errors_alone(providers) -> None:
    providers(main="groq")
    err = {"status": "error", "error": "x"}
    assert lre.redact_result(err, "main") is err
