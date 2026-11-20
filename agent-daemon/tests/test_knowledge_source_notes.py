"""#139: the Apple Notes reader (ai/knowledge_sources/notes.py).

Every test uses a fake osascript runner and tmp paths: never the user's real
Notes or ~/.bot-inator. ``ai/knowledge_sources`` has no ``__init__.py``
(the integrator owns it), so the module is imported as a namespace-package
submodule with ``importlib``.
"""
from __future__ import annotations

import importlib
import json
import subprocess

import pytest

from ai import knowledge_index as ki

notes = importlib.import_module("ai.knowledge_sources.notes")


class FakeNotes:
    """A fake Notes library behind the runner interface. Records every call."""

    def __init__(self) -> None:
        # folder path tuple -> list of note dicts
        self.folders: dict[tuple[str, ...], list[dict]] = {}
        self.body_requests: list[str] = []
        self.list_calls = 0
        self.body_calls = 0
        self.fail: BaseException | None = None

    def add(self, path, nid, name, body, modified="2026-11-01T10:00:00.000Z", locked=False):
        self.folders.setdefault(tuple(path), []).append(
            {"id": nid, "name": name, "body": body, "modified": modified, "locked": locked})

    def find(self, nid):
        for ns in self.folders.values():
            for n in ns:
                if n["id"] == nid:
                    return n
        return None

    def __call__(self, script, args, timeout):
        if self.fail is not None:
            raise self.fail
        if script == notes.LIST_SCRIPT:
            self.list_calls += 1
            skip = set(json.loads(args[0]))
            out = []
            for path, ns in self.folders.items():
                if path[-1] in skip:
                    continue
                out.append({"path": list(path), "ids": [n["id"] for n in ns],
                            "names": [n["name"] for n in ns],
                            "modified": [n["modified"] for n in ns],
                            "locked": [n["locked"] for n in ns]})
            return json.dumps({"folders": out})
        assert script == notes.BODY_SCRIPT
        self.body_calls += 1
        res = {}
        for nid in args:
            self.body_requests.append(nid)
            n = self.find(nid)
            if n is None:
                res[nid] = {"missing": True}
            elif n["locked"]:
                res[nid] = {"locked": True}
            else:
                res[nid] = {"body": n["body"]}
        return json.dumps(res)


def make_index(tmp_path, fake, options=None, **kw):
    """A real KnowledgeIndex with this reader in the "mail" slot (the source
    is not registered yet; the integrator adds ``apple_notes`` to SOURCES)."""
    reader = lambda: notes.listing(options or {}, runner=fake, **kw)  # noqa: E731
    idx = ki.KnowledgeIndex(tmp_path / "k.db", tmp_path / "s.json",
                            readers={"mail": reader}, embedder=None)
    idx.set_source_enabled("mail", True)
    return idx


@pytest.fixture
def lib():
    f = FakeNotes()
    f.add(["iCloud", "Notes"], "x-coredata://A/ICNote/p1", "Garden plan",
          "<div><h1>Garden plan</h1></div><div>Plant the <b>zucchini</b> in May</div>")
    f.add(["iCloud", "Work"], "x-coredata://A/ICNote/p2", "Standup",
          "<div>Ship the quarterly report</div>")
    return f


def test_interface_constants():
    assert notes.NAME == "apple_notes" and notes.NAME != "meeting_notes"
    assert notes.TITLE and notes.TAINT_LABEL
    assert "locked" in notes.DESCRIPTION and "Mac" in notes.DESCRIPTION
    json.dumps(notes.DEFAULT_OPTIONS)
    assert notes.validate_options(notes.DEFAULT_OPTIONS) == notes.DEFAULT_OPTIONS


def test_note_found_by_body_word(lib):
    listing = notes.listing({}, runner=lib)
    assert listing.complete and not listing.error
    items = {i.item_id: i for i in listing.items}
    item = items["x-coredata://A/ICNote/p1"].content()
    assert "zucchini" in item.text and "<b>" not in item.text
    assert "Folder: iCloud / Notes" in item.text
    assert item.title == "Garden plan" and item.link == ""
    assert item.version == item.date == "2026-11-01T10:00:00.000Z"


def test_note_found_by_body_word_through_index(tmp_path, lib):
    idx = make_index(tmp_path, lib)
    idx.index_source("mail")
    found = idx.search("zucchini")
    assert found and found[0]["title"] == "Garden plan"


def test_edit_new_version_delete_absent(tmp_path, lib):
    before = {i.item_id: i.version for i in notes.listing({}, runner=lib).items}
    lib.find("x-coredata://A/ICNote/p1").update(
        body="<div>Plant tomatoes instead</div>", modified="2026-11-05T09:00:00.000Z")
    lib.folders[("iCloud", "Work")].clear()
    listing = notes.listing({}, runner=lib)
    after = {i.item_id: i.version for i in listing.items}
    assert listing.complete
    assert after["x-coredata://A/ICNote/p1"] != before["x-coredata://A/ICNote/p1"]
    assert "x-coredata://A/ICNote/p2" not in after


def test_edit_reindexed_delete_pruned_through_index(tmp_path, lib):
    idx = make_index(tmp_path, lib)
    idx.index_source("mail")
    lib.find("x-coredata://A/ICNote/p1").update(
        body="<div>Plant tomatoes instead</div>", modified="2026-11-05T09:00:00.000Z")
    lib.folders[("iCloud", "Work")].clear()
    r = idx.index_source("mail")
    assert r["indexed"] == 1 and r["removed"] == 1
    assert idx.search("tomatoes") and not idx.search("zucchini")
    assert not idx.search("quarterly")


def test_locked_note_never_listed_nor_requested(tmp_path, lib):
    lib.add(["iCloud", "Notes"], "x-coredata://A/ICNote/p9", "Secret", "<div>vault code</div>",
            locked=True)
    listing = notes.listing({}, runner=lib, known_versions={})
    ids = [i.item_id for i in listing.items]
    assert "x-coredata://A/ICNote/p9" not in ids
    for i in listing.items:
        i.content()
    assert "x-coredata://A/ICNote/p9" not in lib.body_requests


def test_note_locked_after_listing_is_not_read(lib):
    listing = notes.listing({}, runner=lib)
    item = next(i for i in listing.items if i.item_id == "x-coredata://A/ICNote/p1")
    lib.find("x-coredata://A/ICNote/p1")["locked"] = True
    with pytest.raises(notes.NotesError):
        item.content()
    assert item.text is None


def test_recently_deleted_skipped(lib):
    lib.add(["iCloud", "Recently Deleted"], "x-coredata://A/ICNote/p7", "Old", "<div>trashed</div>")
    ids = [i.item_id for i in notes.listing({}, runner=lib).items]
    assert "x-coredata://A/ICNote/p7" not in ids and len(ids) == 2


def test_recently_deleted_skipped_even_if_script_returns_it(lib):
    # Belt and braces: a listing that still carries the folder is filtered in Python.
    lib.add(["iCloud", "Recently Deleted"], "x-coredata://A/ICNote/p7", "Old", "<div>trashed</div>")
    real = lib.__call__

    def no_skip(script, args, timeout):
        if script == notes.LIST_SCRIPT:
            args = ["[]"]
        return real(script, args, timeout)

    ids = [i.item_id for i in notes.listing({}, runner=no_skip).items]
    assert "x-coredata://A/ICNote/p7" not in ids


def test_unchanged_notes_not_loaded(tmp_path, lib):
    idx = make_index(tmp_path, lib)
    first = idx.index_source("mail")
    assert first["indexed"] == 2
    lib.body_requests.clear()
    second = idx.index_source("mail")
    assert second["indexed"] == 0 and second["skipped"] == 2
    assert lib.body_requests == []
    assert lib.list_calls == 2  # one listing call per pass


def test_listing_is_one_call_and_loads_nothing(lib):
    for k in range(50):
        lib.add(["iCloud", "Notes"], f"x-coredata://A/ICNote/q{k}", f"n{k}", "<div>x</div>")
    listing = notes.listing({}, runner=lib)
    assert len(list(listing.items)) == 52
    assert lib.list_calls == 1 and lib.body_calls == 0


def test_batch_loads_only_changed_notes(lib):
    for k in range(10):
        lib.add(["iCloud", "Notes"], f"x-coredata://A/ICNote/q{k}", f"n{k}", f"<div>word{k}</div>")
    known = {i.item_id: i.version for i in notes.listing({}, runner=lib).items}
    for k in (2, 5, 7):
        lib.find(f"x-coredata://A/ICNote/q{k}")["modified"] = "2026-11-10T00:00:00.000Z"
    listing = notes.listing({}, runner=lib, known_versions=known)
    changed = [i for i in listing.items if known.get(i.item_id) != i.version]
    for i in changed:
        i.content()
    assert lib.body_calls == 1
    assert sorted(lib.body_requests) == sorted(i.item_id for i in changed)
    assert [i.text.split("\n\n", 1)[1] for i in changed] == ["word2", "word5", "word7"]


@pytest.mark.parametrize("exc, phrase", [
    (subprocess.CalledProcessError(1, "osascript", stderr=(
        "execution error: Error: Error: Not authorized to send Apple events to Notes. (-1743)")),
     "Automation"),
    (subprocess.TimeoutExpired("osascript", 60), "didn't answer"),
    (subprocess.CalledProcessError(1, "osascript", stderr="Error: Can't get application. (-1728)"),
     "isn't available"),
    (FileNotFoundError("osascript"), "osascript is missing"),
])
def test_errors_become_incomplete_listing(lib, exc, phrase):
    lib.fail = exc
    listing = notes.listing({}, runner=lib)
    assert listing.complete is False and list(listing.items) == []
    assert phrase in listing.error
    assert notes.choices({}, runner=lib)["error"]


def test_garbled_output_is_an_error(lib):
    listing = notes.listing({}, runner=lambda s, a, t: "not json")
    assert listing.complete is False and listing.error


def test_error_listing_prunes_nothing(tmp_path, lib):
    idx = make_index(tmp_path, lib)
    idx.index_source("mail")
    lib.fail = subprocess.TimeoutExpired("osascript", 60)
    r = idx.index_source("mail")
    assert r["removed"] == 0 and r["error"]
    assert idx.search("zucchini")


def test_excluded_folder_not_listed(lib):
    ids = [i.item_id for i in notes.listing({"exclude_folders": ["iCloud / Work"]},
                                            runner=lib).items]
    assert ids == ["x-coredata://A/ICNote/p1"]
    ch = notes.choices({}, runner=lib)
    assert ch["error"] == "" and [f["path"] for f in ch["folders"]] == ["iCloud / Notes",
                                                                        "iCloud / Work"]


@pytest.mark.parametrize("bad", [
    "folders", ["iCloud"], {"exclude_folders": "Work"}, {"exclude_folders": [3]},
    {"exclude_folders": ["  "]}, {"exclude_folders": ["a\nb"]}, {"surprise": 1},
    {"exclude_folders": ["x" * 400]}, {"exclude_folders": [f"f{i}" for i in range(300)]},
])
def test_validate_options_rejects_junk(bad):
    with pytest.raises(ValueError):
        notes.validate_options(bad)


def test_validate_options_cleans():
    got = notes.validate_options({"exclude_folders": [" iCloud / Work ", "iCloud / Work"]})
    assert got == {"exclude_folders": ["iCloud / Work"]}
    assert notes.validate_options(None) == notes.DEFAULT_OPTIONS


def test_bad_options_give_error_not_exception(lib):
    listing = notes.listing({"exclude_folders": 5}, runner=lib)
    assert listing.complete is False and listing.error and lib.list_calls == 0
