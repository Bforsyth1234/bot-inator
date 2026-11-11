"""Tests for ai.activity_store (#75)."""

from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai.activity_store import STORED_FIELDS, ActivityStore
from events.event_bus import ContextEvent

_today = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
T0 = _today - timedelta(days=_today.weekday() + 7)  # last week's Monday, 09:00 UTC

SECRETS = ("Quarterly salary review", "Secret Window Title", "hunter2",
           "https://bank.example/login", "alice@example.com", "Board dinner",
           "invoice-acme-2026")


def _store(tmp_path: Path, **kw) -> ActivityStore:
    kw.setdefault("enabled", True)
    return ActivityStore(tmp_path / "activity.db", tz=timezone.utc, **kw)


def _content_events(ts: datetime) -> list[ContextEvent]:
    return [
        ContextEvent("app_activated", app_name="Mail", window_title="Secret Window Title",
                     timestamp=ts, metadata={"bundle_id": "com.apple.mail", "pid": 1}),
        ContextEvent("mail_received", timestamp=ts, metadata={
            "sender": "alice@example.com", "subject": "Quarterly salary review",
            "date": ts.isoformat(), "mailbox": "INBOX", "message_id": 7, "bulk": False}),
        ContextEvent("imessage_received", timestamp=ts, metadata={
            "sender": "alice@example.com", "text": "hunter2", "rowid": 3}),
        ContextEvent("clipboard_changed", app_name="Safari", timestamp=ts,
                     metadata={"app": "Safari", "kind": "url",
                               "text": "https://bank.example/login"}),
        ContextEvent("event_starting_soon", timestamp=ts, metadata={
            "title": "Board dinner", "url": "https://bank.example/login",
            "location": "hunter2"}),
        ContextEvent("file", timestamp=ts, metadata={
            "path": str(Path.home() / "Downloads" / "invoice-acme-2026.pdf"),
            "action": "created"}),
    ]


def _raw_dump(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT * FROM activity").fetchall()
        rows += conn.execute("SELECT * FROM app_pids").fetchall()
    finally:
        conn.close()
    return repr(rows)


def test_only_allow_listed_fields_are_stored(tmp_path):
    store = _store(tmp_path)
    for ev in _content_events(T0):
        assert store.record(ev) is True
    store.close()

    dump = _raw_dump(tmp_path / "activity.db")
    for secret in SECRETS:
        assert secret not in dump, secret
    # The table has exactly the allow-listed columns (+ id).
    conn = sqlite3.connect(tmp_path / "activity.db")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(activity)")]
    conn.close()
    assert cols == ["id", *STORED_FIELDS]

    rows = ActivityStore(tmp_path / "activity.db", enabled=True).rows()
    by_type = {r["event_type"]: r for r in rows}
    assert by_type["file"]["ext"] == ".pdf"
    assert by_type["file"]["folder"] == "~/Downloads"
    assert by_type["mail_received"]["bulk"] == 0
    assert by_type["clipboard_changed"]["kind"] == "url"
    assert by_type["app_activated"]["app"] == "Mail"
    assert by_type["app_activated"]["weekday"] == 0
    assert by_type["app_activated"]["hour"] == 9


def test_file_is_private(tmp_path):
    store = _store(tmp_path)
    store.record(_content_events(T0)[0])
    mode = stat.S_IMODE(os.stat(tmp_path / "activity.db").st_mode)
    assert mode == 0o600


def test_disabled_stores_nothing(tmp_path):
    store = _store(tmp_path, enabled=False)
    for ev in _content_events(T0):
        assert store.record(ev) is False
    store.enabled = True
    assert store.count() == 0


def test_switch_follows_setting(tmp_path, monkeypatch):
    store = ActivityStore(tmp_path / "activity.db", tz=timezone.utc)
    monkeypatch.setenv("AGENT_ACTIVITY_HISTORY", "0")
    assert store.record(_content_events(T0)[0]) is False
    monkeypatch.setenv("AGENT_ACTIVITY_HISTORY", "1")
    assert store.record(_content_events(T0)[0]) is True
    assert store.count() == 1


def test_unknown_types_and_modified_files_are_ignored(tmp_path):
    store = _store(tmp_path)
    assert store.record(ContextEvent("daily_brief", metadata={"x": 1})) is False
    assert store.record(ContextEvent("file", metadata={
        "path": "/tmp/a.txt", "action": "modified"})) is False
    assert store.count() == 0


def test_launch_vs_switch(tmp_path):
    store = _store(tmp_path)
    ev = lambda pid, m: ContextEvent("app_activated", app_name="Xcode",
                                     timestamp=T0 + timedelta(minutes=m),
                                     metadata={"pid": pid})
    store.record(ev(10, 0))
    store.record(ev(10, 1))   # switch back, same process
    store.record(ev(11, 2))   # relaunched
    store.close()
    # Survives a restart: same pid is still a switch.
    store2 = _store(tmp_path)
    store2.record(ev(11, 3))
    assert [r["launched"] for r in store2.rows()] == [1, 0, 1, 0]


def test_retention_drops_old_rows(tmp_path):
    store = _store(tmp_path)
    now = datetime.now(timezone.utc)
    store.record(ContextEvent("imessage_received", timestamp=now - timedelta(days=91)))
    store.record(ContextEvent("imessage_received", timestamp=now - timedelta(days=89)))
    assert store.count() == 2
    removed = store.prune(now=now)
    assert removed == 1
    assert store.count() == 1


def test_row_cap_keeps_newest(tmp_path):
    store = _store(tmp_path, max_rows=5, prune_every=1)
    now = datetime.now(timezone.utc)
    for i in range(12):
        store.record(ContextEvent("imessage_received",
                                  timestamp=now - timedelta(minutes=60 - i)))
    assert store.count() == 5
    newest = max(r["ts"] for r in store.rows())
    assert abs(newest - (now - timedelta(minutes=49)).timestamp()) < 1


def test_clear_empties_it(tmp_path):
    store = _store(tmp_path)
    for ev in _content_events(T0):
        store.record(ev)
    assert store.count() > 0
    store.clear()
    assert store.count() == 0
    assert store.stats()["count"] == 0
