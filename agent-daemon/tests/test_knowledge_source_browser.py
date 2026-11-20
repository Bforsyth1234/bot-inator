"""Knowledge source ``browser_history`` (#140) on fake history files.

Every test builds Safari ``History.db``, Chromium ``History`` and Firefox
``places.sqlite`` files under ``tmp_path`` and passes ``home=``; ``Path.home``
is also pointed at an empty temp dir and every socket entry point raises,
so no test can read the user's real history or touch the network.

``ai/knowledge_sources/`` has no ``__init__.py`` yet (the wave runner owns
it), so the module is imported as a namespace-package submodule with
``importlib``; that works with or without the ``__init__.py``.
"""
from __future__ import annotations

import importlib
import os
import socket
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest

import ai  # noqa: F401  - import order: ai -> tools avoids a circular import
from ai import egress_log
from ai import knowledge_index as ki

src = importlib.import_module("ai.knowledge_sources.browser")

NOW = 1_800_000_000.0   # 2027-01-15
DAY = 86400.0
SLOT = "meeting_notes"  # stands in for "browser_history" until the runner registers it
FORMATS = ("safari", "chrome", "firefox")


def clock() -> float:
    return NOW


# ---- guards ------------------------------------------------------------------------


class NetworkUsed(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a: Any, **k: Any) -> Any:
        raise NetworkUsed("network touched")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)


@pytest.fixture(autouse=True)
def fake_real_home(tmp_path, monkeypatch):
    empty = tmp_path / "not-the-real-home"
    empty.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: empty))


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


# ---- fake history files (column layouts as on a real Mac, 2026-11-20) -----------------

def _db(path: Path, ddl: str, wal: bool = False) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    if new:
        conn.executescript(ddl)
    return conn


SAFARI_DDL = """
CREATE TABLE history_items (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE,
  domain_expansion TEXT NULL, visit_count INTEGER NOT NULL, daily_visit_counts BLOB NOT NULL,
  weekly_visit_counts BLOB NULL, autocomplete_triggers BLOB NULL,
  should_recompute_derived_visit_counts INTEGER NOT NULL, visit_count_score INTEGER NOT NULL,
  status_code INTEGER NOT NULL DEFAULT 0);
CREATE TABLE history_visits (id INTEGER PRIMARY KEY AUTOINCREMENT,
  history_item INTEGER NOT NULL REFERENCES history_items(id) ON DELETE CASCADE,
  visit_time REAL NOT NULL, title TEXT NULL, load_successful BOOLEAN NOT NULL DEFAULT 1,
  http_non_get BOOLEAN NOT NULL DEFAULT 0, synthesized BOOLEAN NOT NULL DEFAULT 0,
  redirect_source INTEGER NULL UNIQUE, redirect_destination INTEGER NULL UNIQUE,
  origin INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
  attributes INTEGER NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0);
"""

CHROMIUM_DDL = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
CREATE TABLE urls(id INTEGER PRIMARY KEY AUTOINCREMENT, url LONGVARCHAR, title LONGVARCHAR,
  visit_count INTEGER DEFAULT 0 NOT NULL, typed_count INTEGER DEFAULT 0 NOT NULL,
  last_visit_time INTEGER NOT NULL, hidden INTEGER DEFAULT 0 NOT NULL);
CREATE TABLE visits(id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER NOT NULL,
  visit_time INTEGER NOT NULL, from_visit INTEGER, transition INTEGER DEFAULT 0 NOT NULL,
  segment_id INTEGER, visit_duration INTEGER DEFAULT 0 NOT NULL);
"""

FIREFOX_DDL = """
CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url LONGVARCHAR, title LONGVARCHAR,
  rev_host LONGVARCHAR, visit_count INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0 NOT NULL,
  typed INTEGER DEFAULT 0 NOT NULL, frecency INTEGER DEFAULT -1 NOT NULL,
  last_visit_date INTEGER, guid TEXT, foreign_count INTEGER DEFAULT 0 NOT NULL,
  url_hash INTEGER DEFAULT 0 NOT NULL);
CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, from_visit INTEGER, place_id INTEGER,
  visit_date INTEGER, visit_type INTEGER, session INTEGER);
"""


def write_safari(home: Path, rows, **_: Any) -> Path:
    path = home / "Library/Safari/History.db"
    conn = _db(path, SAFARI_DDL)
    for url, title, ts, count in rows:
        cur = conn.execute(
            "INSERT INTO history_items (url, visit_count, daily_visit_counts, "
            "should_recompute_derived_visit_counts, visit_count_score) VALUES (?, ?, x'', 0, 0)",
            (url, count))
        conn.execute("INSERT INTO history_visits (history_item, visit_time, title) VALUES (?, ?, ?)",
                     (cur.lastrowid, ts - 978307200.0 - 3 * DAY, title))
        conn.execute("INSERT INTO history_visits (history_item, visit_time, title) VALUES (?, ?, ?)",
                     (cur.lastrowid, ts - 978307200.0, title))
    conn.commit()
    conn.close()
    return path


def write_chromium(home: Path, rows, browser: str = "chrome", profile: str = "Default",
                   **_: Any) -> Path:
    root = {"chrome": "Google/Chrome", "arc": "Arc/User Data",
            "brave": "BraveSoftware/Brave-Browser", "edge": "Microsoft Edge"}[browser]
    path = home / "Library/Application Support" / root / profile / "History"
    conn = _db(path, CHROMIUM_DDL)
    for url, title, ts, count in rows:
        wk = int((ts + 11644473600.0) * 1_000_000)
        cur = conn.execute("INSERT INTO urls (url, title, visit_count, last_visit_time) "
                           "VALUES (?, ?, ?, ?)", (url, title, count, wk))
        conn.execute("INSERT INTO visits (url, visit_time) VALUES (?, ?)", (cur.lastrowid, wk))
    conn.commit()
    conn.close()
    return path


def write_firefox(home: Path, rows, profile: str = "abcd.default-release", wal: bool = False,
                  keep_open: bool = False, **_: Any):
    ff = home / "Library/Application Support/Firefox"
    path = ff / "Profiles" / profile / "places.sqlite"
    conn = _db(path, FIREFOX_DDL, wal=wal)
    (ff / "profiles.ini").write_text(
        f"[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/{profile}\n")
    if wal:
        conn.execute("PRAGMA wal_autocheckpoint=0")
    for url, title, ts, count in rows:
        us = int(ts * 1_000_000)
        cur = conn.execute("INSERT INTO moz_places (url, title, visit_count, last_visit_date) "
                           "VALUES (?, ?, ?, ?)", (url, title, count, us))
        conn.execute("INSERT INTO moz_historyvisits (place_id, visit_date, visit_type) "
                     "VALUES (?, ?, 1)", (cur.lastrowid, us))
    conn.commit()
    if keep_open:
        return conn
    conn.close()
    return path


WRITERS = {"safari": write_safari, "chrome": write_chromium, "firefox": write_firefox}


def L(home: Path, **opts: Any) -> ki.Listing:
    opts.setdefault("lookback_days", 90)
    return src.listing(opts, home=home, clock=clock)


def by_url(listing: ki.Listing) -> dict[str, ki.SourceItem]:
    return {i.link: i for i in listing.items}


class FakeFetch:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    def __call__(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise OSError("down")
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        return {"status": "ok", "title": "Fetched", "text": f"page body zebra{slug} words"}


def make_index(tmp_path: Path, reader) -> ki.KnowledgeIndex:
    index = ki.KnowledgeIndex(tmp_path / "k.db", tmp_path / "s.json", embedder=None,
                              readers={SLOT: reader})
    index.set_source_enabled(SLOT, True)
    return index


def indexed(index: ki.KnowledgeIndex) -> dict[str, str]:
    db = index._conn()
    return {link: version for link, version in db.execute(
        "SELECT link, version FROM items WHERE source = ?", (SLOT,))}


# ---- per format -------------------------------------------------------------------


@pytest.mark.parametrize("fmt", FORMATS)
def test_inside_limit_listed_outside_not(home, fmt):
    WRITERS[fmt](home, [("https://new.example.com/a", "New page", NOW - 5 * DAY, 2),
                        ("https://old.example.com/b", "Old page", NOW - 100 * DAY, 1)])
    got = L(home, lookback_days=90)
    assert got.error == "" and got.complete
    items = by_url(got)
    assert set(items) == {"https://new.example.com/a"}
    item = items["https://new.example.com/a"]
    assert item.title == "New page"
    assert item.date.startswith("2027-01-10")
    assert item.item_id == src.item_id_for("https://new.example.com/a")
    assert "New page" in item.text and "https://new.example.com/a" in item.text
    assert set(by_url(L(home, lookback_days=0))) == {"https://new.example.com/a",
                                                     "https://old.example.com/b"}


@pytest.mark.parametrize("fmt", FORMATS)
def test_shortening_the_limit_prunes_older_rows(tmp_path, home, fmt):
    WRITERS[fmt](home, [("https://recent.example.com/", "Recent", NOW - 2 * DAY, 1),
                        ("https://month.example.com/", "Month", NOW - 20 * DAY, 1),
                        ("https://year.example.com/", "Year", NOW - 200 * DAY, 1)])
    opts = {"lookback_days": 365}
    index = make_index(tmp_path, lambda: src.listing(opts, home=home, clock=clock))
    assert index.index_source(SLOT)["indexed"] == 3
    opts["lookback_days"] = 7
    res = index.index_source(SLOT)
    assert res["removed"] == 2 and res["error"] == ""
    assert set(indexed(index)) == {"https://recent.example.com/"}
    assert set(by_url(L(home, lookback_days=30))) == {"https://recent.example.com/",
                                                      "https://month.example.com/"}


@pytest.mark.parametrize("fmt", FORMATS)
def test_excluded_domain_and_subdomains_never_listed(home, fmt):
    WRITERS[fmt](home, [("https://chase.com/accounts", "Bank", NOW - DAY, 1),
                        ("https://secure.chase.com/x", "Bank 2", NOW - DAY, 1),
                        ("https://notchase.com/", "Not a bank", NOW - DAY, 1),
                        ("https://news.example.org/", "News", NOW - DAY, 1)])
    got = set(by_url(L(home, excluded_domains=["www.Chase.com"])))
    assert got == {"https://notchase.com/", "https://news.example.org/"}
    # the default list excludes it too
    assert "https://chase.com/accounts" not in by_url(src.listing({}, home=home, clock=clock))


@pytest.mark.parametrize("fmt", FORMATS)
def test_non_http_urls_skipped(home, fmt):
    WRITERS[fmt](home, [("file:///Users/me/secret.txt", "file", NOW - DAY, 1),
                        ("chrome://settings/", "settings", NOW - DAY, 1),
                        ("about:blank", "blank", NOW - DAY, 1),
                        ("javascript:alert(1)", "js", NOW - DAY, 1),
                        ("ftp://files.example.com/x", "ftp", NOW - DAY, 1),
                        ("http://plain.example.com/p", "plain", NOW - DAY, 1)])
    assert set(by_url(L(home))) == {"http://plain.example.com/p"}


@pytest.mark.parametrize("fmt", FORMATS)
def test_token_query_and_credentials_stripped(home, fmt):
    WRITERS[fmt](home, [
        ("https://app.example.com/cb?code=abc&state=xyz", "Callback", NOW - DAY, 1),
        ("https://files.example.com/d?id=1&X-Amz-Signature=deadbeef", "Download", NOW - DAY, 1),
        ("https://u:hunter2@site.example.com/in", "Creds", NOW - DAY, 1),
        ("https://reset.example.com/r?t=aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gH3j", "Reset", NOW - DAY, 1),
        ("https://search.example.com/s?q=python+sqlite#top", "Search", NOW - DAY, 1),
    ])
    items = by_url(L(home))
    assert set(items) == {"https://app.example.com/cb", "https://files.example.com/d",
                          "https://site.example.com/in", "https://reset.example.com/r",
                          "https://search.example.com/s?q=python+sqlite"}
    blob = " ".join(i.text + i.link for i in items.values())
    for secret in ("abc", "xyz", "deadbeef", "hunter2", "aB3dE5gH7"):
        assert secret not in blob


@pytest.mark.parametrize("first,second", [("safari", "chrome"), ("chrome", "firefox"),
                                          ("firefox", "safari")])
def test_same_url_in_two_browsers_is_one_item_with_the_latest_visit(home, first, second):
    WRITERS[first](home, [("https://shared.example.com/p", "Old title", NOW - 10 * DAY, 3)])
    WRITERS[second](home, [("https://SHARED.example.com:443/p#frag", "New title", NOW - DAY, 4)])
    items = L(home).items
    assert len(items) == 1
    item = items[0]
    assert item.link == "https://shared.example.com/p"
    assert item.title == "New title"
    assert item.date.startswith("2027-01-14")
    assert "7 visits" in item.text
    for b in (first, second):
        assert src.BROWSER_TITLES[b] in item.text


def test_every_chromium_browser_and_profile_is_read(home):
    for i, browser in enumerate(("chrome", "arc", "brave", "edge")):
        write_chromium(home, [(f"https://{browser}.example.com/", browser, NOW - DAY, 1)],
                       browser=browser)
    write_chromium(home, [("https://p2.example.com/", "p2", NOW - DAY, 1)], profile="Profile 2")
    write_chromium(home, [("https://guest.example.com/", "g", NOW - DAY, 1)], profile="Guest Profile")
    got = set(by_url(L(home)))
    assert got == {"https://chrome.example.com/", "https://arc.example.com/",
                   "https://brave.example.com/", "https://edge.example.com/",
                   "https://p2.example.com/"}
    keys = [p["key"] for p in src.choices({}, home=home)["profiles"]]
    assert keys == ["chrome:Default", "chrome:Profile 2", "arc:Default", "brave:Default",
                    "edge:Default"]
    only = set(by_url(L(home, profiles=["chrome:Profile 2"])))
    assert only == {"https://p2.example.com/"}


def test_firefox_rows_still_in_the_wal_are_read_and_copies_are_deleted(home, monkeypatch):
    conn = write_firefox(home, [("https://walonly.example.com/", "In WAL", NOW - DAY, 1)],
                         wal=True, keep_open=True)
    try:
        wal = home / "Library/Application Support/Firefox/Profiles/abcd.default-release/places.sqlite-wal"
        assert wal.exists() and wal.stat().st_size > 0
        made: list[str] = []
        real = tempfile.mkdtemp
        monkeypatch.setattr(src.tempfile, "mkdtemp",
                            lambda **k: made.append(real(**k)) or made[-1])
        assert set(by_url(L(home))) == {"https://walonly.example.com/"}
        assert made and not any(os.path.exists(d) for d in made)
    finally:
        conn.close()


# ---- page text -----------------------------------------------------------------------


PUBLIC = ["https://one.example.com/alpha", "https://two.example.com/beta"]
PRIVATE = ["http://localhost:3000/dev", "http://192.168.1.10/router", "http://10.0.0.5/x",
           "http://[::1]/y", "http://intranet/wiki", "http://printer.local/status"]


def _mixed(home: Path) -> None:
    write_chromium(home, [(u, f"T{i}", NOW - DAY, 1) for i, u in enumerate(PUBLIC + PRIVATE)]
                   + [("https://app.example.com/cb?access_token=s3cr3t", "Tok", NOW - DAY, 1)])


def test_page_text_off_makes_no_network_call_and_never_calls_fetch(tmp_path, home):
    _mixed(home)
    fetch = FakeFetch()
    opts = {"lookback_days": 90, "page_text": False}
    index = make_index(tmp_path, lambda: src.listing(opts, home=home, clock=clock, fetch=fetch))
    res = index.index_source(SLOT)   # sockets raise (autouse no_network)
    assert res["indexed"] == len(PUBLIC + PRIVATE) + 1 and res["failed"] == 0
    assert fetch.calls == []
    assert all(v.startswith("meta:") for v in indexed(index).values())


def test_page_text_on_fetches_each_changed_public_url_once_per_30_days(tmp_path, home):
    _mixed(home)
    fetch = FakeFetch()
    now = [NOW]
    opts = {"lookback_days": 90, "page_text": True}
    index = make_index(tmp_path, lambda: src.listing(
        opts, home=home, clock=lambda: now[0], fetch=fetch, sleep=lambda s: None))
    res = index.index_source(SLOT)
    assert res["indexed"] == len(PUBLIC + PRIVATE) + 1
    assert sorted(fetch.calls) == sorted(PUBLIC)          # private + token URLs never fetched
    assert index.search("zebraalpha")[0]["link"] == PUBLIC[0]

    # A day later, more visits: same 30-day window -> nothing fetched.
    write_chromium(home, [(u, "again", NOW, 1) for u in PUBLIC])
    now[0] = NOW + DAY
    fetch.calls.clear()
    index.index_source(SLOT)
    assert fetch.calls == []

    # 31 days on, every window has turned: each public URL once more.
    now[0] = NOW + 31 * DAY
    opts["lookback_days"] = 365
    index.index_source(SLOT)
    assert sorted(fetch.calls) == sorted(PUBLIC)


def test_a_failed_fetch_indexes_title_and_url_and_is_not_retried_soon(tmp_path, home):
    write_safari(home, [(PUBLIC[0], "Alpha title", NOW - DAY, 1)])
    fetch = FakeFetch(fail=True)
    now = [NOW]
    opts = {"lookback_days": 90, "page_text": True}
    index = make_index(tmp_path, lambda: src.listing(
        opts, home=home, clock=lambda: now[0], fetch=fetch, sleep=lambda s: None))
    res = index.index_source(SLOT)
    assert res["indexed"] == 1 and res["failed"] == 0
    assert index.search("Alpha title")[0]["link"] == PUBLIC[0]
    assert fetch.calls == [PUBLIC[0]]
    now[0] = NOW + 2 * DAY
    index.index_source(SLOT)
    assert fetch.calls == [PUBLIC[0]]


def test_fetches_are_spaced_out(home):
    write_chromium(home, [(u, "t", NOW - DAY, 1) for u in PUBLIC])
    slept: list[float] = []
    items = src.listing({"page_text": True}, home=home, clock=clock, fetch=FakeFetch(),
                        sleep=slept.append).items
    for item in items:
        item.content()
    assert len(slept) == 1 and 0 < slept[0] <= src.FETCH_GAP_SECONDS


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(egress_log.DB_PATH_ENV, str(tmp_path / "egress.db"))
    monkeypatch.delenv(egress_log.KEEP_TEXT_KEY, raising=False)
    log = egress_log.EgressLog()
    egress_log.set_default_log(log)
    yield log
    egress_log.set_default_log(None)
    log.close()


def test_default_fetch_uses_web_search_checks_and_writes_one_ledger_row(ledger, monkeypatch):
    ws = importlib.import_module("tools.web_search")
    monkeypatch.delenv(ws.ENABLED_KEY, raising=False)   # page text is its own opt-in
    requests: list[str] = []
    dns = {"news.example.com": ["93.184.216.34"], "sneaky.example.com": ["10.1.2.3"]}

    def http_get(url, *, headers=None, connect_ip=None, **_):
        requests.append(url)
        return ws.HttpResponse(200, {"content-type": "text/html"},
                               b"<html><title>N</title><body><p>Hello giraffe</p></body></html>")

    monkeypatch.setattr(ws, "_http_get", http_get)
    monkeypatch.setattr(ws, "_resolve", lambda host, port: dns[host])
    out = src.fetch_public_page("https://news.example.com/a")
    assert out["status"] == "ok" and "giraffe" in out["text"]
    rows = ledger.rows()
    assert len(rows) == 1 and rows[0]["tool"] == src.FETCH_TOOL_NAME
    assert rows[0]["host"] == "news.example.com"

    bad = src.fetch_public_page("https://sneaky.example.com/")   # resolves private
    assert bad["status"] == "error" and "private" in bad["error"]
    assert requests == ["https://news.example.com/a"] and ledger.count() == 1


# ---- failures -------------------------------------------------------------------------


def test_one_unreadable_browser_does_not_hide_or_prune_the_others(tmp_path, home):
    write_safari(home, [("https://safari.example.com/", "S", NOW - DAY, 1)])
    chrome = write_chromium(home, [("https://chrome.example.com/", "C", NOW - DAY, 1)])
    index = make_index(tmp_path, lambda: src.listing({}, home=home, clock=clock))
    assert index.index_source(SLOT)["indexed"] == 2

    chrome.write_bytes(b"this is not a database at all" * 100)
    got = src.listing({}, home=home, clock=clock)
    assert set(by_url(got)) == {"https://safari.example.com/"}
    assert got.complete is False and "Chrome (Default)" in got.error
    res = index.index_source(SLOT)
    assert res["removed"] == 0 and "Chrome" in res["error"]
    assert set(indexed(index)) == {"https://safari.example.com/", "https://chrome.example.com/"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_safari_file_says_full_disk_access(home):
    path = write_safari(home, [("https://s.example.com/", "S", NOW - DAY, 1)])
    write_firefox(home, [("https://f.example.com/", "F", NOW - DAY, 1)])
    path.chmod(0)
    try:
        got = L(home)
        assert "Full Disk Access" in got.error and got.complete is False
        assert set(by_url(got)) == {"https://f.example.com/"}
        safari = [p for p in src.choices({}, home=home)["profiles"] if p["browser"] == "safari"]
        assert safari[0]["readable"] is False
    finally:
        path.chmod(0o600)


def test_missing_chosen_profile_and_empty_home_give_errors_not_exceptions(home):
    got = L(home, profiles=["firefox:gone.default"])
    assert got.items == [] and got.complete is False
    assert "Firefox (gone.default): profile not found" in got.error
    empty = L(home)                     # nothing installed: nothing to list, nothing wrong
    assert empty.items == [] and empty.error == "" and empty.complete
    bad = src.listing({"lookback_days": 12}, home=home, clock=clock)
    assert bad.complete is False and "Look back" in bad.error


def test_unexpected_layout_is_an_error(home):
    path = home / "Library/Application Support/Google/Chrome/Default/History"
    path.parent.mkdir(parents=True)
    sqlite3.connect(path).executescript("CREATE TABLE urls (id INTEGER, url TEXT);").close()
    got = L(home)
    assert "unexpected layout" in got.error and got.complete is False


# ---- options / choices ---------------------------------------------------------------


@pytest.mark.parametrize("junk,word", [
    ("not a dict", "set of options"),
    ({"lookback_days": 12}, "Look back"),
    ({"lookback_days": True}, "Look back"),
    ({"lookback_days": "lots"}, "Look back"),
    ({"page_text": "yes"}, "Page text"),
    ({"excluded_domains": "chase.com"}, "list"),
    ({"excluded_domains": ["not a domain!"]}, "isn't a site"),
    ({"excluded_domains": [42]}, "must be text"),
    ({"excluded_domains": ["x.com"] * 501}, "At most"),
    ({"profiles": "chrome:Default"}, "list"),
    ({"profiles": ["opera:Default"]}, "isn't a browser profile"),
    ({"profiles": ["chrome:../../etc"]}, "isn't a browser profile"),
])
def test_validate_options_rejects_junk(junk, word):
    with pytest.raises(ValueError, match=word):
        src.validate_options(junk)


def test_validate_options_cleans_and_defaults():
    assert src.validate_options(None) == src.DEFAULT_OPTIONS
    out = src.validate_options({"lookback_days": "30", "excluded_domains": [
        "https://www.Example.com/path", "*.bank.example", "example.com"], "junk": 1})
    assert out["lookback_days"] == 30 and out["page_text"] is False
    assert out["excluded_domains"] == ["example.com", "bank.example"]
    assert out["profiles"] is None and "junk" not in out
    import json
    json.dumps(src.DEFAULT_OPTIONS)
    assert src.DEFAULT_OPTIONS["lookback_days"] == 90 and src.DEFAULT_OPTIONS["page_text"] is False


def test_choices_shape(home):
    write_safari(home, [])
    write_firefox(home, [])
    got = src.choices({"profiles": ["safari:default", "edge:Default"]}, home=home)
    assert [d["value"] for d in got["lookback_days"]] == [7, 30, 90, 365, 0]
    assert got["missing_profiles"] == ["edge:Default"]
    by_key = {p["key"]: p for p in got["profiles"]}
    assert set(by_key) == {"safari:default", "firefox:abcd.default-release"}
    assert by_key["firefox:abcd.default-release"]["name"] == "default-release"
    assert by_key["safari:default"]["enabled"] and not by_key["firefox:abcd.default-release"]["enabled"]
    assert got["suggested_excluded_domains"] == list(src.SUGGESTED_EXCLUDED_DOMAINS)


def test_module_interface():
    assert src.NAME == "browser_history" and src.TAINT_LABEL == "indexed web page"
    assert src.TITLE and src.DESCRIPTION.endswith("Mac.")
    with pytest.raises(TypeError):
        src.listing({}, homedir="/")
