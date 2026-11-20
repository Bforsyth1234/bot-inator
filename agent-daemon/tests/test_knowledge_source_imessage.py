"""#137: the iMessage knowledge source (ai/knowledge_sources/imessage.py).

Every test builds a fake chat.db in tmp_path with the subset of the real
schema the reader touches. Never the user's real ~/Library/Messages.

``ai/knowledge_sources`` has no ``__init__.py`` yet (the wave runner owns it),
so the module is imported with importlib as a namespace subpackage of ``ai``.
"""
from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

src = importlib.import_module("ai.knowledge_sources.imessage")
from ai import knowledge_index as ki  # noqa: E402

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
    style INTEGER, chat_identifier TEXT, service_name TEXT, display_name TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, text TEXT,
    attributedBody BLOB, handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER,
    is_from_me INTEGER DEFAULT 0, associated_message_type INTEGER DEFAULT 0,
    item_type INTEGER DEFAULT 0, date_retracted INTEGER DEFAULT 0,
    cache_has_attachments INTEGER DEFAULT 0);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER);
"""

OWN = "+15550001111"
SAM = "+15552223333"
ALEX = "alex@example.com"
UTC = timezone.utc


def apple(y, mo, d, h=12, mi=0) -> int:
    unix = datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()
    return int((unix - src.APPLE_EPOCH) * 1e9)


class FakeChatDB:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.handles: dict[str, int] = {}

    def handle(self, hid: str) -> int:
        if hid not in self.handles:
            cur = self.conn.execute("INSERT INTO handle (id, service) VALUES (?, 'iMessage')", (hid,))
            self.handles[hid] = cur.lastrowid
        return self.handles[hid]

    def chat(self, guid: str, members: list[str], *, ident: str = "", name: str = "",
             group: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO chat (guid, style, chat_identifier, service_name, display_name)"
            " VALUES (?, ?, ?, 'iMessage', ?)",
            (guid, 43 if group else 45, ident or (members[0] if members else ""), name))
        cid = cur.lastrowid
        for m in members:
            self.conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (cid, self.handle(m)))
        return cid

    def msg(self, chat_id: int, text, date: int, *, sender: str = "", from_me: bool = False,
            assoc: int = 0, attributed: bytes | None = None, retracted: int = 0,
            item_type: int = 0, joined: bool = True) -> int:
        cur = self.conn.execute(
            "INSERT INTO message (text, attributedBody, handle_id, service, date, is_from_me,"
            " associated_message_type, item_type, date_retracted)"
            " VALUES (?, ?, ?, 'iMessage', ?, ?, ?, ?, ?)",
            (text, attributed, self.handle(sender) if sender else 0, date, int(from_me),
             assoc, item_type, retracted))
        if joined:
            self.conn.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)",
                              (chat_id, cur.lastrowid, date))
        self.conn.commit()
        return cur.lastrowid

    def close(self):
        self.conn.commit()
        self.conn.close()


@pytest.fixture
def db(tmp_path):
    fake = FakeChatDB(tmp_path / "chat.db")
    yield fake
    fake.conn.close()


def read(db: FakeChatDB, options=None, **kw):
    db.conn.commit()
    kw.setdefault("own_handle", OWN)
    return src.listing(options or {}, db_path=db.path, tz=UTC, **kw)


def by_id(listing):
    return {item.item_id: item for item in listing.items}


def text_of(item):
    return item.load()["text"]


# ---------------------------------------------------------------------------


def test_interface_constants():
    assert src.NAME == "imessages"
    assert src.TAINT_LABEL and src.TITLE
    assert "other people" in src.DESCRIPTION and "Mac" in src.DESCRIPTION
    assert src.validate_options(src.DEFAULT_OPTIONS) == {"exclude": []}


def test_message_found_with_conversation_and_date(db):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    db.msg(sam, "The spare key is under the blue pot", apple(2026, 11, 18, 9, 30), sender=SAM)
    listing = read(db)
    assert listing.error == "" and listing.complete is True
    items = by_id(listing)
    assert list(items) == [f"iMessage;-;{SAM}#2026-11-18"]
    item = items[f"iMessage;-;{SAM}#2026-11-18"]
    assert item.title == f"{SAM} · 2026-11-18"
    assert item.date.startswith("2026-11-18T09:30")
    assert item.link == ""
    body = text_of(item)
    assert f"Messages with {SAM} on 2026-11-18" in body
    assert f"09:30 {SAM}: The spare key is under the blue pot" in body
    # The indexer's own content() path takes the lazy text.
    assert "blue pot" in item.content().text


def test_attributed_body_only_rows_are_decoded(db):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    words = "dinner moved to eight".encode()
    blob = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00" \
           b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+" \
           + bytes([len(words)]) + words + b"\x86\x84"
    db.msg(sam, None, apple(2026, 11, 18), sender=SAM, attributed=blob)
    (item,) = read(db).items
    assert "dinner moved to eight" in text_of(item)


def test_grouped_per_conversation_per_day_in_order(db):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    group = db.chat("iMessage;+;chat99", [SAM, ALEX], ident="chat99", group=True)
    db.msg(sam, "second", apple(2026, 11, 18, 10), sender=SAM)
    db.msg(sam, "first", apple(2026, 11, 18, 8), from_me=True)
    db.msg(sam, "next day", apple(2026, 11, 19, 8), sender=SAM)
    db.msg(group, "group hello", apple(2026, 11, 18, 9), sender=ALEX)
    items = by_id(read(db))
    assert set(items) == {f"iMessage;-;{SAM}#2026-11-18", f"iMessage;-;{SAM}#2026-11-19",
                          "iMessage;+;chat99#2026-11-18"}
    day = text_of(items[f"iMessage;-;{SAM}#2026-11-18"])
    assert day.index("08:00 Me: first") < day.index(f"10:00 {SAM}: second")
    assert "next day" not in day and "group hello" not in day
    g = items["iMessage;+;chat99#2026-11-18"]
    assert g.title == f"{SAM}, {ALEX} · 2026-11-18"  # no group name: participants
    assert f"09:00 {ALEX}: group hello" in text_of(g)
    # Newest first.
    assert list(items)[0] == f"iMessage;-;{SAM}#2026-11-19"


def test_named_group_uses_its_name(db):
    group = db.chat("iMessage;+;chat7", [SAM, ALEX], ident="chat7", name="Climbing", group=True)
    db.msg(group, "rope?", apple(2026, 11, 18), sender=SAM)
    (item,) = read(db).items
    assert item.title == "Climbing · 2026-11-18"
    assert "Group chat Climbing on 2026-11-18" in text_of(item)


def test_excluded_conversation_yields_nothing_and_listing_stays_complete(db):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    alex = db.chat("iMessage;-;" + ALEX, [ALEX])
    db.msg(sam, "secret plans", apple(2026, 11, 18), sender=SAM)
    db.msg(alex, "lunch?", apple(2026, 11, 18), sender=ALEX)
    for entry in ("iMessage;-;" + SAM, "(555) 222-3333", SAM):  # by guid or by handle
        listing = read(db, {"exclude": [entry]})
        assert [conv for conv in map(src.conversation_of, by_id(listing))] == ["iMessage;-;" + ALEX]
        # complete=True is what lets the index prune the excluded rows.
        assert listing.complete is True and listing.error == ""


def test_excluding_later_prunes_rows_in_the_real_index(db, tmp_path):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    alex = db.chat("iMessage;-;" + ALEX, [ALEX])
    db.msg(sam, "secret plans", apple(2026, 11, 18), sender=SAM)
    db.msg(alex, "lunch tomorrow", apple(2026, 11, 18), sender=ALEX)
    options: dict = {"exclude": []}
    index = ki.KnowledgeIndex(
        db_path=tmp_path / "k.db", settings_path=tmp_path / "k.json", embedder=None,
        readers={"mail": lambda: read(db, options), "folders": ki.Listing,
                 "chats": ki.Listing, "meeting_notes": ki.Listing})
    index.set_source_enabled("mail", True)  # a stand-in slot: SOURCES is the runner's
    assert index.index_source("mail")["indexed"] == 2
    assert index.search("secret plans")
    options["exclude"] = [SAM]
    result = index.index_source("mail")
    assert result["removed"] == 1
    assert not index.search("secret plans")
    assert index.search("lunch tomorrow")


def test_own_remote_thread_never_appears(db):
    own = db.chat("iMessage;-;" + OWN, [OWN])
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    db.msg(own, "what's on my calendar", apple(2026, 11, 18), from_me=True)
    db.msg(sam, "hi", apple(2026, 11, 18), sender=SAM)
    for options in ({}, {"exclude": []}, {"exclude": [SAM]}):
        convs = {src.conversation_of(i) for i in by_id(read(db, options))}
        assert "iMessage;-;" + OWN not in convs
    # Handle given without the country code still matches.
    assert "iMessage;-;" + OWN not in {src.conversation_of(i)
                                        for i in by_id(read(db, own_handle="5550001111"))}
    choice_ids = [c["id"] for c in src.choices({}, db_path=db.path, own_handle=OWN)["conversations"]]
    assert choice_ids == ["iMessage;-;" + SAM]


def test_app_prefixed_thread_excluded_even_without_a_handle(db):
    own = db.chat("iMessage;-;" + OWN, [OWN])
    db.msg(own, "bot-inator: Approved (a).", apple(2026, 11, 18, 8), from_me=True)
    db.msg(own, "remind me", apple(2026, 11, 18, 9), from_me=True)
    assert read(db, own_handle="").items == []


def test_tapbacks_attachments_retracted_deleted_and_events_skipped(db):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    d = apple(2026, 11, 18, 9)
    db.msg(sam, "real words", d, sender=SAM)
    db.msg(sam, "Loved “real words”", d + 1, from_me=True, assoc=2000)  # tapback
    db.msg(sam, "￼", d + 2, sender=SAM)                                     # attachment only
    db.msg(sam, "never mind that", d + 3, from_me=True, retracted=d + 4)         # unsent
    db.msg(sam, "gone forever", d + 5, sender=SAM, joined=False)                 # deleted
    db.msg(sam, "renamed the group", d + 6, sender=SAM, item_type=2)             # group event
    other = db.chat("iMessage;-;" + ALEX, [ALEX])
    db.msg(other, "￼", d, sender=ALEX)                                      # only an attachment
    (item,) = read(db).items
    body = text_of(item)
    assert "real words" in body
    for junk in ("Loved", "￼", "never mind", "gone forever", "renamed"):
        assert junk not in body
    assert len(body.strip().splitlines()) == 3  # header, blank, one line


def test_version_stable_until_the_day_changes(db):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    db.msg(sam, "first", apple(2026, 11, 18, 9), sender=SAM)
    db.msg(sam, "other day", apple(2026, 11, 17, 9), sender=SAM)
    a = {k: v.version for k, v in by_id(read(db)).items()}
    b = {k: v.version for k, v in by_id(read(db)).items()}
    assert a == b
    db.msg(sam, "a new one", apple(2026, 11, 18, 11), from_me=True)
    c = {k: v.version for k, v in by_id(read(db)).items()}
    key18, key17 = f"iMessage;-;{SAM}#2026-11-18", f"iMessage;-;{SAM}#2026-11-17"
    assert c[key18] != a[key18]
    assert c[key17] == a[key17]


def test_cap_keeps_newest_days(db):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    for day in range(1, 8):
        db.msg(sam, f"day {day}", apple(2026, 11, day, 9), sender=SAM)
        db.msg(sam, f"day {day} later", apple(2026, 11, day, 18), sender=SAM)
    listing = read(db, max_items=3)
    ids = [i.item_id for i in listing.items]
    assert ids == [f"iMessage;-;{SAM}#2026-11-0{d}" for d in (7, 6, 5)]
    # A kept day is whole even though the cap was hit mid-scan.
    assert "day 5 later" in text_of(listing.items[2]) and "09:00" in text_of(listing.items[2])


def test_missing_db_is_an_error_not_an_exception(tmp_path):
    listing = src.listing({}, db_path=tmp_path / "nope" / "chat.db", own_handle=OWN)
    assert listing.items == [] and listing.complete is False
    assert "Messages" in listing.error
    assert src.choices({}, db_path=tmp_path / "nope.db", own_handle=OWN)["error"]


def test_unreadable_db_uses_the_full_disk_access_wording(tmp_path):
    bad = tmp_path / "chat.db"
    bad.write_bytes(b"not a database at all" * 10)
    listing = src.listing({}, db_path=bad, own_handle=OWN)
    assert listing.items == [] and listing.complete is False
    assert "Full Disk Access" in listing.error and "iMessages" in listing.error


def test_reader_opens_the_db_read_only(db, monkeypatch):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    db.msg(sam, "hi", apple(2026, 11, 18), sender=SAM)
    seen = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        seen.append((str(target), kw.get("uri")))
        return real(target, *a, **kw)

    monkeypatch.setattr(src.sqlite3, "connect", spy)
    assert len(list(read(db).items)) == 1
    src.choices({}, db_path=db.path, own_handle=OWN)
    assert seen and all(t.endswith("?mode=ro") and uri is True for t, uri in seen)


def test_choices_lists_conversations_with_titles_and_excluded_flag(db):
    sam = db.chat("iMessage;-;" + SAM, [SAM])
    group = db.chat("iMessage;+;chat7", [SAM, ALEX], ident="chat7", name="Climbing", group=True)
    db.msg(sam, "old", apple(2026, 11, 10), sender=SAM)
    db.msg(group, "new", apple(2026, 11, 18), sender=ALEX)
    got = src.choices({"exclude": [SAM]}, db_path=db.path, own_handle=OWN)
    assert got["error"] == ""
    convs = got["conversations"]
    assert [c["id"] for c in convs] == ["iMessage;+;chat7", "iMessage;-;" + SAM]
    assert convs[0]["title"] == "Climbing" and convs[0]["is_group"] is True
    assert convs[1]["excluded"] is True and convs[0]["excluded"] is False
    assert convs[1]["handles"] == [SAM]


@pytest.mark.parametrize("junk", [
    "exclude me", ["a"], {"exclude": "iMessage;-;x"}, {"exclude": [3]},
    {"exclude": [""]}, {"exclude": ["a\nb"]}, {"exclude": ["x" * 400]},
    {"include": []}, {"exclude": ["a"] * 0 + [None]},
])
def test_validate_options_rejects_junk(junk):
    with pytest.raises(ValueError):
        src.validate_options(junk)


def test_validate_options_cleans():
    assert src.validate_options({"exclude": ["  a ", "a", "b"]}) == {"exclude": ["a", "b"]}
    assert src.validate_options(None) == {"exclude": []}
    # Bad options never raise from listing().
    listing = src.listing({"exclude": 5}, db_path=Path("/nonexistent/chat.db"), own_handle=OWN)
    assert listing.complete is False and "exclude" in listing.error
