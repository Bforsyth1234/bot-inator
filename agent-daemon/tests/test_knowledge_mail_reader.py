"""The knowledge index's REAL mail reader against a fake Mail store.

Found live on 2026-11-20 (#134): "Index now" with Mail on failed with
``'SimpleTool' object has no attribute '_open'``. ``from tools import
list_recent_emails`` gives the tool object once ``tools`` is imported (as it
always is in the daemon), not the submodule. Part 1's tests used fake readers,
so nothing ran this path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import tools  # noqa: F401  - the daemon always has the package loaded
from ai import knowledge_index as ki
from tests.test_list_recent_emails import SCHEMA, _add

BODY = "The lease renewal is due on March 3. Please sign page four."


def _mail_root(tmp_path: Path) -> tuple[Path, int]:
    root = tmp_path / "Mail"
    index = root / "V10" / "MailData" / "Envelope Index"
    index.parent.mkdir(parents=True)
    conn = sqlite3.connect(index)
    conn.executescript(SCHEMA)
    rowid = _add(conn, "Lease renewal", 1_762_700_300, 1)
    _add(conn, "my reply", 1_762_700_900, 2, sender=2)  # Sent: skipped
    conn.commit()
    conn.close()
    raw = (
        "From: Alice Example <alice@example.com>\r\nSubject: Lease renewal\r\n"
        "Message-ID: <lease-1@example.com>\r\nContent-Type: text/plain\r\n\r\n"
        f"{BODY}\r\n"
    ).encode()
    messages = root / "V10" / "UUID-1" / "INBOX.mbox" / "Data" / "Messages"
    messages.mkdir(parents=True)
    (messages / f"{rowid}.emlx").write_bytes(str(len(raw)).encode() + b"\n" + raw)
    return root, rowid


def test_real_mail_listing_reads_a_mail_store(tmp_path):
    root, rowid = _mail_root(tmp_path)
    listing = ki.mail_listing(root)
    assert listing.error == ""
    assert [item.item_id for item in listing.items] == [str(rowid)]
    item = listing.items[0]
    assert item.title == "Lease renewal"
    loaded = item.load()
    assert BODY in loaded["text"]
    assert loaded["link"].startswith("message://")


def test_real_mail_listing_without_a_store_reports_it(tmp_path):
    listing = ki.mail_listing(tmp_path / "nothing")
    assert listing.items == [] and listing.complete is False
    assert "Mail" in listing.error
