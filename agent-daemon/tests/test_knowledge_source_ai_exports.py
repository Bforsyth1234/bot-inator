"""#141: imported ChatGPT / Claude exports as a knowledge source
(ai/knowledge_sources/ai_exports.py).

Every fixture is a small MADE-UP export built here in tmp_path: never a
real export, never ~/.bot-inator. The module is imported as a namespace
subpackage (ai/knowledge_sources has no __init__.py yet; the wave runner
owns it).
"""
from __future__ import annotations

import importlib
import json
import zipfile
from pathlib import Path

import pytest

from ai import knowledge_index as ki

ax = importlib.import_module("ai.knowledge_sources.ai_exports")

GPT_ID_1 = "6a1f0c2e-1111-4a2b-9c3d-000000000001"
GPT_ID_2 = "6a1f0c2e-2222-4a2b-9c3d-000000000002"
CL_ID_1 = "0c1a0de0-1111-4a2b-9c3d-000000000001"
CL_ID_2 = "0c1a0de0-2222-4a2b-9c3d-000000000002"


# ---------------------------------------------------------------------------
# Made-up fixtures
# ---------------------------------------------------------------------------


def gpt_msg(role, parts, content_type="text", **extra):
    msg = {"id": "m", "author": {"role": role}, "content": {"content_type": content_type, "parts": parts},
           "recipient": "all", "weight": 1.0, "metadata": {}}
    msg.update(extra)
    return msg


def gpt_conversation(conv_id=GPT_ID_1, title="Sourdough starter help", update_time=1_700_000_500.0,
                     extra_text="", middle="Feed it equal parts rye flour and water every twelve hours."):
    """root -> sys -> u1 -> a1 -> u2 -> a2 (active), with an abandoned
    regenerated sibling a1x of a1 and a hidden/tool/image mix."""
    mapping = {
        "root": {"id": "root", "parent": None, "children": ["sys"], "message": None},
        "sys": {"id": "sys", "parent": "root", "children": ["u1"],
                "message": gpt_msg("system", ["You are a helpful assistant."])},
        "u1": {"id": "u1", "parent": "sys", "children": ["a1x", "a1"],
               "message": gpt_msg("user", ["How do I keep my sourdough starter alive?",
                                           {"content_type": "image_asset_pointer",
                                            "asset_pointer": "file-service://file-PICTURE"}],
                                  content_type="multimodal_text")},
        "a1x": {"id": "a1x", "parent": "u1", "children": [],
                "message": gpt_msg("assistant", ["ABANDONED BRANCH about pickled herring."])},
        "a1": {"id": "a1", "parent": "u1", "children": ["t1"],
               "message": gpt_msg("assistant", [middle])},
        "t1": {"id": "t1", "parent": "a1", "children": ["c1"],
               "message": gpt_msg("tool", ["TOOL OUTPUT the browser saw"])},
        "c1": {"id": "c1", "parent": "t1", "children": ["h1"],
               "message": {"id": "c1", "author": {"role": "assistant"}, "recipient": "python",
                           "content": {"content_type": "code", "text": "print('CODE CALL')"}}},
        "h1": {"id": "h1", "parent": "c1", "children": ["u2"],
               "message": gpt_msg("user", ["HIDDEN CUSTOM INSTRUCTIONS"],
                                  metadata={"is_visually_hidden_from_conversation": True})},
        "u2": {"id": "u2", "parent": "h1", "children": ["a2"],
               "message": gpt_msg("user", ["What temperature is best?" + extra_text])},
        "a2": {"id": "a2", "parent": "u2", "children": [],
               "message": gpt_msg("assistant", ["Around 24 degrees Celsius."])},
    }
    return {"id": conv_id, "conversation_id": conv_id, "title": title, "create_time": 1_700_000_000.0,
            "update_time": update_time, "current_node": "a2", "mapping": mapping}


def gpt_conversation_2(conv_id=GPT_ID_2):
    return {"id": conv_id, "title": "Bike gears", "create_time": 1_700_001_000.0,
            "update_time": 1_700_001_100.0, "current_node": "b",
            "mapping": {"a": {"id": "a", "parent": None, "children": ["b"],
                              "message": gpt_msg("user", ["Why does my chain skip on the third cog?"])},
                        "b": {"id": "b", "parent": "a", "children": [],
                              "message": gpt_msg("assistant", ["Your derailleur cable has stretched."])}}}


def claude_export():
    return [
        {"uuid": CL_ID_1, "name": "Planning a balcony garden", "created_at": "2025-03-01T10:00:00Z",
         "updated_at": "2025-03-01T11:00:00.000000Z", "account": {"uuid": "acct"},
         "chat_messages": [
             {"uuid": "1", "sender": "human", "text": "Which herbs grow on a shady balcony?",
              "content": [{"type": "text", "text": "Which herbs grow on a shady balcony?"}],
              "attachments": [{"file_name": "ATTACHED.txt", "extracted_content": "ATTACHMENT BODY"}],
              "files": []},
             {"uuid": "2", "sender": "assistant", "text": "",
              "content": [{"type": "thinking", "thinking": "SECRET THINKING"},
                          {"type": "text", "text": "Mint and parsley tolerate partial shade well."},
                          {"type": "tool_use", "name": "web_search", "input": {"q": "TOOL QUERY"}}]},
             {"uuid": "3", "sender": "human", "text": "TOOL RESULT TEXT",
              "content": [{"type": "tool_result", "name": "web_search", "content": [
                  {"type": "text", "text": "TOOL RESULT TEXT"}]}]},
             {"uuid": "4", "sender": "assistant", "text": "Water them every second morning."},
         ]},
        {"uuid": CL_ID_2, "name": "", "created_at": "2025-03-02T10:00:00Z",
         "updated_at": "2025-03-02T10:05:00Z",
         "chat_messages": [{"sender": "human", "text": "Translate good morning into Welsh"},
                           {"sender": "assistant", "text": "Bore da."}]},
    ]


def write_json(path: Path, data) -> Path:
    path.write_text(json.dumps(data), "utf-8")
    return path


def write_zip(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data if isinstance(data, str) else json.dumps(data))
    return path


def entry(path: Path, imp_id: str, provider="chatgpt", conversations=1):
    return {"id": imp_id, "path": str(path), "provider": provider,
            "added_at": "2026-11-20T00:00:00+00:00", "conversations": conversations}


def loaded(listing):
    return {item.item_id: item.content() for item in listing.items}


# ---------------------------------------------------------------------------
# Formats
# ---------------------------------------------------------------------------


def test_chatgpt_json_lists_conversations_and_middle_phrase_is_in_text(tmp_path):
    path = write_json(tmp_path / "conversations.json", [gpt_conversation(), gpt_conversation_2()])
    listing = ax.listing({"imports": [entry(path, "a")]})
    assert listing.error == "" and listing.complete is True
    items = loaded(listing)
    assert set(items) == {f"chatgpt:{GPT_ID_1}", f"chatgpt:{GPT_ID_2}"}
    item = items[f"chatgpt:{GPT_ID_1}"]
    assert item.title == "ChatGPT: Sourdough starter help"
    assert item.link == f"https://chatgpt.com/c/{GPT_ID_1}"
    assert item.date.startswith("2023-11-14")
    assert "equal parts rye flour and water" in item.text
    # Turns in order, labelled.
    assert item.text.index("You: How do I keep") < item.text.index("Assistant: Feed it") \
        < item.text.index("You: What temperature") < item.text.index("Assistant: Around 24")


def test_chatgpt_abandoned_branch_and_non_text_parts_skipped(tmp_path):
    path = write_json(tmp_path / "conversations.json", [gpt_conversation()])
    text = loaded(ax.listing({"imports": [entry(path, "a")]}))[f"chatgpt:{GPT_ID_1}"].text
    assert "ABANDONED BRANCH" not in text
    assert "file-service" not in text and "image_asset_pointer" not in text
    for skipped in ("TOOL OUTPUT", "CODE CALL", "HIDDEN CUSTOM", "helpful assistant"):
        assert skipped not in text, skipped


def test_chatgpt_zip_without_unpacking(tmp_path):
    path = write_zip(tmp_path / "chatgpt-export.zip", {
        "conversations.json": [gpt_conversation()], "chat.html": "<html>IGNORED</html>",
        "user.json": {"id": "user-x"}})
    items = loaded(ax.listing({"imports": [entry(path, "a")]}))
    assert list(items) == [f"chatgpt:{GPT_ID_1}"]
    assert "rye flour" in items[f"chatgpt:{GPT_ID_1}"].text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["chatgpt-export.zip"]


def test_chatgpt_zip_with_split_conversation_files(tmp_path):
    path = write_zip(tmp_path / "export.zip", {"conversations-0.json": [gpt_conversation()],
                                               "conversations-1.json": [gpt_conversation_2()]})
    assert set(loaded(ax.listing({"imports": [entry(path, "a")]}))) == {
        f"chatgpt:{GPT_ID_1}", f"chatgpt:{GPT_ID_2}"}
    assert ax.inspect_export(str(path)) == {"provider": "chatgpt", "conversations": 2, "error": ""}


def test_claude_json_and_zip(tmp_path):
    for path in (write_json(tmp_path / "conversations.json", claude_export()),
                 write_zip(tmp_path / "claude.zip", {"conversations.json": claude_export(),
                                                     "users.json": [], "projects.json": []})):
        listing = ax.listing({"imports": [entry(path, "c", provider="claude", conversations=2)]})
        assert listing.error == ""
        items = loaded(listing)
        assert set(items) == {f"claude:{CL_ID_1}", f"claude:{CL_ID_2}"}
        item = items[f"claude:{CL_ID_1}"]
        assert item.title == "Claude: Planning a balcony garden"
        assert item.link == f"https://claude.ai/chat/{CL_ID_1}"
        assert "Mint and parsley tolerate partial shade" in item.text
        assert item.text.startswith("You: Which herbs")
        assert item.text.rstrip().endswith("Assistant: Water them every second morning.")
        for skipped in ("SECRET THINKING", "TOOL QUERY", "TOOL RESULT", "ATTACHMENT BODY", "ATTACHED.txt"):
            assert skipped not in item.text, skipped
        # Untitled conversation gets its first line as the title.
        assert items[f"claude:{CL_ID_2}"].title == "Claude: Translate good morning into Welsh"


# ---------------------------------------------------------------------------
# Imports: duplicates, updates, removal
# ---------------------------------------------------------------------------


def test_same_export_imported_twice_gives_no_duplicates(tmp_path):
    path = write_json(tmp_path / "conversations.json", [gpt_conversation(), gpt_conversation_2()])
    copy = write_json(tmp_path / "copy.json", [gpt_conversation(), gpt_conversation_2()])
    listing = ax.listing({"imports": [entry(path, "a"), entry(path, "b"), entry(copy, "c")]})
    ids = [i.item_id for i in listing.items]
    assert len(ids) == len(set(ids)) == 2


def test_newer_export_updates_version_and_text(tmp_path):
    old = write_json(tmp_path / "old.json", [gpt_conversation()])
    new = write_json(tmp_path / "new.json", [gpt_conversation(update_time=1_700_009_999.0,
                                                               extra_text=" Also: CANDLEWICK QUESTION")])
    v_old = ax.listing({"imports": [entry(old, "a")]})
    v_old = {i.item_id: i.version for i in v_old.items}[f"chatgpt:{GPT_ID_1}"]
    # Newer export listed FIRST and SECOND: the later update time wins either way.
    for imports in ([entry(old, "a"), entry(new, "b")], [entry(new, "b"), entry(old, "a")]):
        items = loaded(ax.listing({"imports": imports}))
        assert len(items) == 1
        item = items[f"chatgpt:{GPT_ID_1}"]
        assert item.version != v_old
        assert "CANDLEWICK QUESTION" in item.text


def test_version_stable_and_hash_fallback(tmp_path):
    conv = gpt_conversation(update_time=None)
    a = write_json(tmp_path / "a.json", [conv])
    first = next(iter(ax.listing({"imports": [entry(a, "a")]}).items)).version
    again = next(iter(ax.listing({"imports": [entry(a, "a")]}).items)).version
    assert first == again and ":h" in first
    b = write_json(tmp_path / "b.json", [gpt_conversation(update_time=None, extra_text=" changed")])
    assert next(iter(ax.listing({"imports": [entry(b, "b")]}).items)).version != first


def test_removing_an_import_drops_only_its_conversations(tmp_path):
    gpt = write_json(tmp_path / "gpt.json", [gpt_conversation()])
    claude = write_json(tmp_path / "claude.json", claude_export())
    both = {"imports": [entry(gpt, "g"), entry(claude, "c", provider="claude")]}
    assert len(list(ax.listing(both).items)) == 3
    only_claude = {"imports": [entry(claude, "c", provider="claude")]}
    listing = ax.listing(only_claude)
    assert listing.complete is True and listing.error == ""
    assert {i.item_id for i in listing.items} == {f"claude:{CL_ID_1}", f"claude:{CL_ID_2}"}


def test_removed_import_kept_when_another_import_has_the_conversation(tmp_path):
    a = write_json(tmp_path / "a.json", [gpt_conversation(), gpt_conversation_2()])
    b = write_json(tmp_path / "b.json", [gpt_conversation()])
    ids = {i.item_id for i in ax.listing({"imports": [entry(b, "b")]}).items}
    assert ids == {f"chatgpt:{GPT_ID_1}"}
    assert len(list(ax.listing({"imports": [entry(a, "a"), entry(b, "b")]}).items)) == 2


# ---------------------------------------------------------------------------
# Errors: missing, malformed, wrong shape, zip without conversations.json
# ---------------------------------------------------------------------------


def test_missing_file_reports_error_and_is_incomplete(tmp_path):
    good = write_json(tmp_path / "good.json", [gpt_conversation_2()])
    gone = tmp_path / "gone.json"
    listing = ax.listing({"imports": [entry(good, "g"), entry(gone, "x")]})
    assert listing.complete is False
    assert "gone.json is missing" in listing.error
    assert {i.item_id for i in listing.items} == {f"chatgpt:{GPT_ID_2}"}
    rows = ax.choices({"imports": [entry(good, "g"), entry(gone, "x")]})["imports"]
    assert [(r["file_name"], r["present"]) for r in rows] == [("good.json", True), ("gone.json", False)]


@pytest.mark.parametrize("content, needle", [
    ('[{"id": "x", "mapping": {', "cut off or not valid JSON"),
    ("this is not json", "not a ChatGPT or Claude export"),
    ('{"conversations": []}', "not a ChatGPT or Claude export"),
    ('[1, 2, "three"]', "has no ChatGPT or Claude conversations"),
    ('[{"name": "a list of something else"}]', "has no ChatGPT or Claude conversations"),
    ("", "is empty"),
])
def test_malformed_or_wrong_shaped_file_clear_error_nothing_listed(tmp_path, content, needle):
    path = tmp_path / "conversations.json"
    # A valid conversation first, so "nothing listed" also covers a file
    # that breaks after readable conversations.
    if content.startswith("[{\"id\": \"x\""):
        content = "[" + json.dumps(gpt_conversation()) + "," + content[1:]
    path.write_text(content, "utf-8")
    listing = ax.listing({"imports": [entry(path, "m")]})
    assert list(listing.items) == []
    assert listing.complete is False
    assert needle in listing.error and "conversations.json" in listing.error
    got = ax.inspect_export(str(path))
    assert got["conversations"] == 0 and needle in got["error"]


def test_zip_without_conversations_json(tmp_path):
    path = write_zip(tmp_path / "photos.zip", {"user.json": {}, "notes/conversations.txt": "x",
                                               "../conversations.json": [gpt_conversation()]})
    listing = ax.listing({"imports": [entry(path, "z")]})
    assert list(listing.items) == [] and listing.complete is False
    assert "no conversations.json" in listing.error
    assert "no conversations.json" in ax.inspect_export(str(path))["error"]


def test_damaged_zip_and_size_cap(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"PK\x03\x04" + b"\x00" * 40)
    assert ax.inspect_export(str(bad))["error"]
    big = write_json(tmp_path / "big.json", [gpt_conversation()])
    listing = ax.listing({"imports": [entry(big, "b")]}, max_bytes=100)
    assert "larger than" in listing.error and list(listing.items) == []


def test_streaming_reads_across_small_chunks(tmp_path):
    convs = [gpt_conversation(conv_id=f"6a1f0c2e-3333-4a2b-9c3d-{n:012d}") for n in range(30)]
    path = write_json(tmp_path / "conversations.json", convs)
    with open(path, encoding="utf-8") as fh:
        got = list(ax._iter_json_array(fh, chunk=7))
    assert got == convs


# ---------------------------------------------------------------------------
# inspect / make / validate / choices
# ---------------------------------------------------------------------------


def test_inspect_export_reports_provider_and_count(tmp_path):
    gpt = write_json(tmp_path / "g.json", [gpt_conversation(), gpt_conversation_2()])
    claude = write_zip(tmp_path / "c.zip", {"conversations.json": claude_export()})
    assert ax.inspect_export(str(gpt)) == {"provider": "chatgpt", "conversations": 2, "error": ""}
    assert ax.inspect_export(str(claude)) == {"provider": "claude", "conversations": 2, "error": ""}
    assert "missing" in ax.inspect_export(str(tmp_path / "nope.json"))["error"]


def test_make_import_builds_a_valid_entry(tmp_path):
    gpt = write_json(tmp_path / "g.json", [gpt_conversation(), gpt_conversation_2()])
    got = ax.make_import(str(gpt), clock=lambda: 1_700_000_000.0, new_id=lambda: "imp1")
    assert got == {"id": "imp1", "path": str(gpt), "provider": "chatgpt",
                   "added_at": "2023-11-14T22:13:20+00:00", "conversations": 2}
    assert ax.validate_options({"imports": [got]}) == {"imports": [got]}
    json.dumps(got)
    bad = write_json(tmp_path / "bad.json", {"x": 1})
    with pytest.raises(ValueError, match="not a ChatGPT or Claude export"):
        ax.make_import(str(bad))


@pytest.mark.parametrize("junk", [
    "imports", [], {"imports": "x"}, {"imports": [1]},
    {"imports": [{"path": "/a/b.json", "provider": "chatgpt"}]},                       # no id
    {"imports": [{"id": "a", "path": "rel.json", "provider": "chatgpt"}]},             # relative
    {"imports": [{"id": "a", "path": "/a/b.exe", "provider": "chatgpt"}]},             # wrong type
    {"imports": [{"id": "a", "path": "/a/b.json", "provider": "gemini"}]},             # provider
    {"imports": [{"id": "a", "path": "/a/b.json", "provider": "claude", "conversations": -1}]},
    {"imports": [{"id": "a", "path": "/a/b.json", "provider": "claude"},
                 {"id": "a", "path": "/a/c.json", "provider": "claude"}]},             # dup id
    {"imports": [{"id": "../x", "path": "/a/b.json", "provider": "claude"}]},
])
def test_validate_options_rejects_junk(junk):
    with pytest.raises(ValueError):
        ax.validate_options(junk)
    listing = ax.listing(junk) if isinstance(junk, dict) else None
    if listing is not None:
        assert list(listing.items) == [] and listing.complete is False and listing.error


def test_validate_options_cleans_and_defaults():
    assert ax.validate_options(None) == {"imports": []}
    assert ax.validate_options(ax.DEFAULT_OPTIONS) == {"imports": []}
    got = ax.validate_options({"imports": [{"id": "a", "path": "/x/conversations.json",
                                            "provider": "claude", "junk": 1}], "other": 2})
    assert got == {"imports": [{"id": "a", "path": "/x/conversations.json", "provider": "claude",
                                "added_at": "", "conversations": 0}]}
    assert ax.NAME == "ai_chats" and ax.TAINT_LABEL and ax.TITLE
    assert "OpenAI" in ax.DESCRIPTION and "Anthropic" in ax.DESCRIPTION


def test_choices_shape(tmp_path):
    path = write_json(tmp_path / "conversations.json", claude_export())
    got = ax.choices({"imports": [entry(path, "c", provider="claude", conversations=2)]})
    assert got == {"error": "", "imports": [{
        "id": "c", "file_name": "conversations.json", "path": str(path), "provider": "claude",
        "provider_label": "Claude", "added_at": "2026-11-20T00:00:00+00:00", "conversations": 2,
        "present": True}]}


# ---------------------------------------------------------------------------
# Through the real KnowledgeIndex (reader plugged into an existing source slot)
# ---------------------------------------------------------------------------


def test_through_the_index_search_prune_and_no_prune_on_missing(tmp_path):
    gpt = write_json(tmp_path / "gpt.json", [gpt_conversation()])
    claude = write_json(tmp_path / "claude.json", claude_export())
    opts = {"imports": [entry(gpt, "g"), entry(claude, "c", provider="claude")]}
    index = ki.KnowledgeIndex(tmp_path / "idx" / "k.db", tmp_path / "idx" / "s.json",
                              embedder=None, readers={"folders": lambda: ax.listing(opts)})
    index.set_source_enabled("folders", True)
    assert index.index_source("folders")["indexed"] == 3
    hits = index.search("rye flour water twelve hours")
    assert hits and hits[0]["item_id"] == f"chatgpt:{GPT_ID_1}"
    assert hits[0]["title"] == "ChatGPT: Sourdough starter help"
    # Unchanged second pass: nothing reloaded.
    again = index.index_source("folders")
    assert again["indexed"] == 0 and again["skipped"] == 3
    # The ChatGPT file goes missing: error, nothing pruned.
    gpt.rename(tmp_path / "moved.json")
    res = index.index_source("folders")
    assert "gpt.json is missing" in res["error"] and res["removed"] == 0
    assert index.search("rye flour water twelve hours")
    # The user removes that import: its conversation is pruned, Claude's stay.
    opts["imports"] = [entry(claude, "c", provider="claude")]
    res = index.index_source("folders")
    assert res["removed"] == 1 and res["error"] == ""
    assert all(not h["item_id"].startswith("chatgpt:") for h in index.search("rye flour twelve hours"))
    assert index.search("mint parsley shade")[0]["item_id"] == f"claude:{CL_ID_1}"


def test_loader_holds_a_window_and_rereads_for_the_rest(tmp_path, monkeypatch):
    convs = [gpt_conversation(conv_id=f"6a1f0c2e-4444-4a2b-9c3d-{n:012d}",
                              middle=f"Unique phrase number {n} here.") for n in range(5)]
    path = write_json(tmp_path / "conversations.json", convs)
    monkeypatch.setattr(ax, "LOAD_WINDOW", 2)
    scans = []
    real_scan = ax._scan
    monkeypatch.setattr(ax, "_scan", lambda *a, **k: scans.append(k.get("keep_text")) or real_scan(*a, **k))
    items = list(ax.listing({"imports": [entry(path, "a")]}).items)
    texts = [i.content().text for i in items]
    assert all(f"Unique phrase number {n} here." in texts[n] for n in range(5))
    kept = [s for s in scans if s is not None]
    assert len(kept) == 3 and max(len(s) for s in kept) == 2
