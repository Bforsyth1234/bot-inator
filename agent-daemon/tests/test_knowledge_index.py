"""#134 part 1: the local knowledge index (ai/knowledge_index.py, routes/knowledge.py).

Every test uses tmp paths and fake readers: never the user's real Mail,
memory.db or ~/.bot-inator.
"""
from __future__ import annotations

import hashlib
import math
import socket
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai import knowledge_index as ki
from routes import knowledge as knowledge_routes


def word_embedder(text: str) -> list[float]:
    """Deterministic bag-of-words vector (384 dims) so the vector path runs
    without MiniLM."""
    vec = [0.0] * ki.EMBED_DIM
    for w in ki.re.findall(r"\w+", text.lower()):
        h = int(hashlib.sha256(w.encode()).hexdigest(), 16)
        vec[h % ki.EMBED_DIM] += 1.0
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


class FakeReader:
    def __init__(self, items=None, complete=True):
        self.items = list(items or [])
        self.complete = complete
        self.loads = 0
        self.calls = 0

    def add(self, item_id, text, version="1", title="", link=""):
        def load(t=text):
            self.loads += 1
            return {"text": t}
        self.items = [i for i in self.items if i.item_id != item_id]
        self.items.append(ki.SourceItem(item_id=item_id, version=version, title=title or item_id,
                                        link=link, load=load))

    def __call__(self):
        self.calls += 1
        return ki.Listing(list(self.items), complete=self.complete)


LEASE = ("Hi, about the lease renewal: the landlord agreed the rent stays at 1450 "
         "until next August, and the deposit is returned within 30 days.")
INVOICE = "Acme invoice 2291 is due on December 3. Please pay by bank transfer."


@pytest.fixture
def readers():
    mail = FakeReader()
    mail.add("101", LEASE, title="Lease renewal", link="message://%3Clease@example.com%3E")
    mail.add("102", INVOICE, title="Acme invoice", link="message://%3Cacme@example.com%3E")
    return {"mail": mail, "folders": FakeReader(), "chats": FakeReader(),
            "meeting_notes": FakeReader()}


def make(tmp_path, readers, embedder=word_embedder):
    return ki.KnowledgeIndex(tmp_path / "idx" / "knowledge.db", tmp_path / "idx" / "settings.json",
                             embedder=embedder, readers=readers)


def count(index, table, source):
    db = sqlite3.connect(index.db_path)
    try:
        return db.execute(f"SELECT COUNT(*) FROM {table} WHERE source=?", (source,)).fetchone()[0]
    finally:
        db.close()


def test_all_sources_off_by_default_and_nothing_indexed(tmp_path, readers):
    index = make(tmp_path, readers)
    assert [s["enabled"] for s in index.status()["sources"]] == [False] * 4
    result = index.run_pass()
    assert result["sources"] == {}
    assert index.index_source("mail")["indexed"] == 0
    assert readers["mail"].calls == 0 and readers["mail"].loads == 0
    assert index.search("lease landlord") == []
    assert index.index_chat_turn("e1", "hello", "hi") is False
    # No db file at all for a user who never opted in.
    assert not index.db_path.exists()


def test_index_and_search_returns_snippet_with_source_link(tmp_path, readers):
    index = make(tmp_path, readers)
    index.set_source_enabled("mail", True)
    res = index.run_pass()
    assert res["sources"]["mail"]["indexed"] == 2
    hits = index.search("what did my landlord say about the lease?")
    assert hits, "no hits"
    top = hits[0]
    assert top["source"] == "mail" and top["item_id"] == "101"
    assert top["title"] == "Lease renewal"
    assert top["link"] == "message://%3Clease@example.com%3E"
    assert "1450" in top["text"]
    assert index.search("when is the acme invoice due")[0]["item_id"] == "102"


def concept_embedder(text: str) -> list[float]:
    """Fake "meaning": housing words share one axis, money words another."""
    words = set(ki.re.findall(r"\w+", text.lower()))
    vec = [0.0] * ki.EMBED_DIM
    vec[0] = float(bool(words & {"lease", "landlord", "apartment", "flat", "rent"}))
    vec[1] = float(bool(words & {"invoice", "bill", "payment", "pay"}))
    vec[2] = 0.0 if (vec[0] or vec[1]) else 1.0
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec]


def test_vector_path_finds_meaning_without_keyword(tmp_path, readers):
    index = make(tmp_path, readers, embedder=concept_embedder)
    index.set_source_enabled("mail", True)
    index.run_pass()
    assert index.vector_enabled()
    hits = index.search("my apartment")  # no word in common with either email
    assert [h["item_id"] for h in hits] == ["101"] and hits[0]["match"] == "meaning"
    both = index.search("lease")
    assert both[0]["item_id"] == "101" and both[0]["match"] == "both"


def test_incremental_pass_skips_unchanged_items(tmp_path, readers):
    index = make(tmp_path, readers)
    index.set_source_enabled("mail", True)
    index.run_pass()
    assert readers["mail"].loads == 2
    second = index.run_pass()["sources"]["mail"]
    assert second["indexed"] == 0 and second["skipped"] == 2
    assert readers["mail"].loads == 2
    readers["mail"].add("102", INVOICE.replace("December 3", "January 9"), version="2",
                        title="Acme invoice")
    third = index.run_pass()["sources"]["mail"]
    assert third["indexed"] == 1 and third["skipped"] == 1
    assert readers["mail"].loads == 3
    assert "January 9" in index.search("acme invoice due")[0]["text"]


def test_complete_listing_prunes_items_gone_at_source(tmp_path, readers):
    index = make(tmp_path, readers)
    index.set_source_enabled("mail", True)
    index.run_pass()
    readers["mail"].items = [i for i in readers["mail"].items if i.item_id != "102"]
    assert index.run_pass()["sources"]["mail"]["removed"] == 1
    assert index.search("acme invoice") == []


def test_turning_a_source_off_removes_its_rows(tmp_path, readers):
    readers["chats"].items = [ki.chat_item("ev1", [("user", "remind me about the dentist"),
                                                  ("assistant", "Noted: dentist Tuesday")])]
    index = make(tmp_path, readers)
    index.set_source_enabled("mail", True)
    index.set_source_enabled("chats", True)
    index.set_cloud_allowed("mail", True)
    index.run_pass()
    assert count(index, "items", "mail") == 2 and count(index, "chunks", "mail") >= 2
    assert index.set_source_enabled("mail", False) == 2
    assert count(index, "items", "mail") == 0 and count(index, "chunks", "mail") == 0
    db = sqlite3.connect(index.db_path)
    fts = db.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'landlord'").fetchone()[0]
    fts_all = db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    db.close()
    assert fts == 0 and fts_all == count(index, "chunks", "chats")
    assert count(index, "items", "chats") == 1  # other sources untouched
    assert index.search("landlord lease") == []
    assert index.cloud_allowed("mail") is False  # off clears the cloud toggle
    index.set_source_enabled("mail", True)
    assert index.search("landlord") == []  # back on starts from nothing


def test_forget_everything_empties_the_index(tmp_path, readers):
    index = make(tmp_path, readers)
    index.set_source_enabled("mail", True)
    index.run_pass()
    assert index.forget_everything() == 2
    assert not index.db_path.exists()
    status = index.status()
    assert all(not s["enabled"] and s["items"] == 0 for s in status["sources"])
    assert index.search("lease") == []
    assert not index.db_path.exists()


def test_cloud_toggle_defaults_off_and_filters_for_cloud(tmp_path, readers):
    readers["chats"].items = [ki.chat_item("ev1", [("user", "my lease question"),
                                                  ("assistant", "lease answer")])]
    index = make(tmp_path, readers)
    for s in ki.SOURCES:
        assert index.cloud_allowed(s) is False
    index.set_source_enabled("mail", True)
    index.set_source_enabled("chats", True)
    assert index.cloud_allowed("mail") is False
    index.run_pass()
    assert {h["source"] for h in index.search("lease")} == {"mail", "chats"}
    assert index.search("lease", for_cloud=True) == []
    index.set_cloud_allowed("chats", True)
    assert {h["source"] for h in index.search("lease", for_cloud=True)} == {"chats"}
    with pytest.raises(ki.SourceError):
        index.set_cloud_allowed("folders", True)  # an off source can't go to the cloud
    again = make(tmp_path, readers)  # persisted in its own settings file
    assert again.cloud_allowed("chats") and not again.cloud_allowed("mail")


def test_fts_only_fallback_without_embedder(tmp_path, readers):
    index = make(tmp_path, readers, embedder=None)
    index.set_source_enabled("mail", True)
    index.run_pass()
    assert index.vector_enabled() is False
    hits = index.search("landlord lease")
    assert hits[0]["item_id"] == "101" and hits[0]["match"] == "keyword"


def test_fts_only_fallback_without_vec_extension(tmp_path, readers, monkeypatch):
    monkeypatch.setattr(ki, "_try_load_vec", lambda conn: "sqlite-vec not installed")
    index = make(tmp_path, readers)
    index.set_source_enabled("mail", True)
    index.run_pass()
    assert index.vec_loaded is False and index.vector_enabled() is False
    assert index.search("acme invoice")[0]["item_id"] == "102"


def test_no_imessage_source(tmp_path, readers):
    assert "imessage" not in ki.SOURCES and "messages" not in ki.SOURCES
    index = make(tmp_path, readers)
    with pytest.raises(ki.SourceError):
        index.set_source_enabled("imessage", True)
    messages = Path.home() / "Library" / "Messages"
    if messages.is_dir():
        with pytest.raises(ki.SourceError):
            ki.check_folder(str(messages))
    index.settings.path.parent.mkdir(parents=True, exist_ok=True)
    index.settings.path.write_text('{"sources": {"imessage": {"enabled": true}}}')
    assert "imessage" not in [s["source"] for s in make(tmp_path, readers).status()["sources"]]


def test_folders_index_files_and_removed_folder_loses_rows(tmp_path):
    docs = tmp_path / "docs"
    (docs / ".hidden").mkdir(parents=True)
    (docs / "lease.md").write_text("The landlord keeps the deposit in escrow.")
    (docs / ".hidden" / "secret.txt").write_text("landlord hidden")
    (docs / "photo.jpg").write_bytes(b"\xff\xd8landlord")
    index = ki.KnowledgeIndex(tmp_path / "idx" / "k.db", tmp_path / "idx" / "s.json",
                              embedder=None)
    index.set_folders([str(docs)])
    index.set_source_enabled("folders", True)
    index.run_pass()
    hits = index.search("landlord")
    assert [h["item_id"] for h in hits] == [str((docs / "lease.md").resolve())]
    assert hits[0]["link"].startswith("file://")
    assert index.set_folders([]) == 1
    assert index.search("landlord") == []


def test_index_chat_turn_and_chat_listing_agree(tmp_path, readers):
    index = make(tmp_path, readers)
    index.set_source_enabled("chats", True)
    assert index.index_chat_turn("ev9", "Where is my passport?", "In the desk drawer.")
    hits = index.search("passport")
    assert hits[0]["source"] == "chats" and hits[0]["item_id"] == "ev9"

    class Mem:
        def get_chat_log(self, limit):
            return [{"event_id": "ev9", "role": "user", "text": "Where is my passport?"},
                    {"event_id": "ev9", "role": "assistant", "text": "In the desk drawer."}]

    readers["chats"].items = list(ki.chat_listing(Mem()).items)
    assert index.index_source("chats")["skipped"] == 1


def test_background_guard_ac_or_idle():
    assert ki.background_blocked(lambda: "ac", lambda: 0.0) is None
    assert ki.background_blocked(lambda: "battery", lambda: 999.0) is None
    assert ki.background_blocked(lambda: "battery", lambda: 5.0) is not None
    assert ki.background_blocked(lambda: None, lambda: None) is not None


def test_background_pass_stops_when_guard_fails(tmp_path, readers):
    index = make(tmp_path, readers)
    index.set_source_enabled("mail", True)
    res = index.run_pass(keep_going=lambda: False)
    assert res["sources"]["mail"]["indexed"] == 0
    assert readers["mail"].loads == 0


def test_no_network_during_index_and_search(tmp_path, readers, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call from the knowledge index")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    index = make(tmp_path, readers)
    index.set_source_enabled("mail", True)
    index.run_pass()
    assert index.search("lease")


def test_routes(tmp_path, readers):
    index = make(tmp_path, readers)
    app = FastAPI()
    app.include_router(knowledge_routes.router)
    app.state.knowledge_index = index
    app.state.knowledge_indexer = ki.KnowledgeIndexer(index, power=lambda: "battery",
                                                      idle=lambda: 0.0)
    with TestClient(app) as c:
        body = c.get("/api/knowledge").json()
        assert [s["enabled"] for s in body["sources"]] == [False] * 4
        assert c.put("/api/knowledge/sources/imessage", json={"enabled": True}).status_code == 404
        assert c.put("/api/knowledge/sources/mail", json={"cloud": True}).status_code == 400
        assert c.put("/api/knowledge/sources/mail", json={"enabled": True}).status_code == 200
        assert c.post("/api/knowledge/index-now").json()["started"] is True
        # The pass writes items before it stamps last_indexed: wait for both
        # (the full gate on a loaded Mac caught the gap).
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            mail = c.get("/api/knowledge").json()["sources"][0]
            if mail["items"] == 2 and mail["last_indexed"] and not app.state.knowledge_indexer.running:
                break
            time.sleep(0.02)
        assert mail["items"] == 2 and mail["last_indexed"]
        assert c.put("/api/knowledge/folders", json={"folders": ["relative/path"]}).status_code == 400
        off = c.put("/api/knowledge/sources/mail", json={"enabled": False}).json()
        assert off["deleted"] == 2 and off["sources"][0]["items"] == 0
        c.put("/api/knowledge/sources/mail", json={"enabled": True})
        index.run_pass()
        forgot = c.post("/api/knowledge/forget").json()
        assert forgot["deleted"] == 2 and not forgot["sources"][0]["enabled"]
