"""Tests for the Spotlight ``find_files`` tool (#44). ``mdfind`` is mocked."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
import importlib

# ``tools.find_files`` as an attribute is the registered tool (same name
# as its module), so fetch the module itself.
ff = importlib.import_module("tools.find_files")
from tools.find_files import READ_ONLY, find_files


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["mdfind"], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


def _make(path: Path, mtime: float, size: int = 3) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return str(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(ff.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _run(stdout: str, **kwargs):
    with mock.patch.object(ff.subprocess, "run", return_value=_proc(stdout)) as run:
        out = find_files(**kwargs)
    return out, run


def test_query_and_kind_flags(home):
    docs = home / "Documents"
    docs.mkdir()
    out, run = _run("", query='invoice "x"', folder=str(docs), kind="pdf")
    assert out == {"status": "ok", "count": 0, "results": []}
    args, kwargs = run.call_args
    cmd = args[0]
    assert isinstance(cmd, list) and kwargs.get("shell") is not True
    assert kwargs["timeout"] == ff.MDFIND_TIMEOUT_S
    assert cmd[:3] == ["mdfind", "-onlyin", str(docs.resolve())]
    assert 'kMDItemDisplayName == "*invoice \\"x\\"*"cd' in cmd[3]
    assert 'kMDItemContentTypeTree == "com.adobe.pdf"' in cmd[3]


def test_no_folder_no_kind(home):
    _, run = _run("", query="taxes")
    cmd = run.call_args[0][0]
    assert cmd[0] == "mdfind" and "-onlyin" not in cmd and len(cmd) == 2
    assert "kMDItemContentType" not in cmd[1]


@pytest.mark.parametrize("kind,needle", [
    ("image", "public.image"), ("document", "public.composite-content"),
    ("folder", 'kMDItemContentType == "public.folder"'),
])
def test_kind_filters(home, kind, needle):
    _, run = _run("", query="a", kind=kind)
    assert needle in run.call_args[0][0][-1]


def test_bad_inputs_do_not_run_mdfind(home):
    with mock.patch.object(ff.subprocess, "run") as run:
        assert find_files(query="  ")["status"] == "error"
        assert find_files(query="a", kind="spreadsheet")["status"] == "error"
        assert find_files(query="a", folder=str(home / "nope"))["status"] == "error"
    run.assert_not_called()


def test_results_newest_first_with_fields(home):
    old = _make(home / "old.pdf", 1_000, size=5)
    new = _make(home / "new.pdf", 3_000, size=7)
    mid = _make(home / "mid.pdf", 2_000)
    gone = str(home / "deleted.pdf")
    out, _ = _run("\n".join([old, gone, new, mid]) + "\n", query="pdf")
    assert [r["path"] for r in out["results"]] == [new, mid, old]
    assert out["results"][0] == {"path": new, "name": "new.pdf",
                                 "modified": 3_000, "size": 7}
    assert out["count"] == 3


def test_cap_at_50(home):
    paths = [_make(home / f"f{i}.txt", 1_000 + i) for i in range(60)]
    out, _ = _run("\n".join(paths), query="f", limit=500)
    assert out["count"] == 50
    assert out["results"][0]["name"] == "f59.txt"
    out, _ = _run("\n".join(paths), query="f", limit=3)
    assert [r["name"] for r in out["results"]] == ["f59.txt", "f58.txt", "f57.txt"]


def test_library_filtered_unless_folder_asks(home):
    lib = _make(home / "Library" / "Caches" / "invoice.pdf", 5_000)
    doc = _make(home / "Documents" / "invoice.pdf", 1_000)
    libby = _make(home / "Librarything" / "invoice.pdf", 2_000)  # not ~/Library
    stdout = "\n".join([lib, doc, libby])
    out, _ = _run(stdout, query="invoice")
    assert [r["path"] for r in out["results"]] == [libby, doc]
    out, _ = _run(stdout, query="invoice", folder=str(home / "Library"))
    assert out["results"][0]["path"] == lib


def test_empty_result(home):
    out, _ = _run("", query="nothing-matches")
    assert out == {"status": "ok", "count": 0, "results": []}


def test_mdfind_failure_and_timeout(home):
    with mock.patch.object(ff.subprocess, "run",
                           return_value=_proc(returncode=1, stderr="boom")):
        assert find_files(query="a") == {"status": "error", "error": "boom",
                                         "count": 0, "results": []}
    with mock.patch.object(ff.subprocess, "run",
                           side_effect=subprocess.TimeoutExpired("mdfind", 10)):
        assert find_files(query="a")["error"] == "mdfind timed out"


def test_read_only_gate():
    """Shared decision 5: find_files bypasses approval (rule 7a)."""
    assert READ_ONLY == {"find_files"}
    import tools
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in tools.AVAILABLE_TOOLS}
    if "find_files" not in names:
        pytest.skip("find_files not registered in tools/__init__.py yet "
                    "(the wave runner adds it, rule 9)")
    assert READ_ONLY <= tools.READ_ONLY_TOOL_NAMES


# ---- #126: since / until ----------------------------------------------------


@pytest.fixture
def chicago(monkeypatch):
    """A fixed zone, so the UTC literals in the clause can be written out."""
    import time
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _run_dated(stdout: str, added: dict[str, str] | None = None, mdls_rc: int = 0, **kwargs):
    """Run ``find_files`` with ``mdfind`` and ``mdls`` both faked.

    ``added`` maps a path to the ``mdls -raw`` date text; other paths get
    the null marker. Returns (result, [mdfind commands], [mdls commands]).
    """
    added = added or {}
    seen: dict[str, list] = {"mdfind": [], "mdls": []}

    def fake(cmd, **kw):
        assert isinstance(cmd, list) and kw.get("shell") is not True
        seen[cmd[0]].append(cmd)
        if cmd[0] == "mdfind":
            return _proc(stdout)
        assert cmd[:6] == ["mdls", "-name", "kMDItemDateAdded", "-raw", "-nullMarker", "(null)"]
        return _proc("\0".join(added.get(p, "(null)") for p in cmd[6:]), returncode=mdls_rc)

    with mock.patch.object(ff.subprocess, "run", side_effect=fake):
        out = find_files(**kwargs)
    return out, seen["mdfind"], seen["mdls"]


def test_since_until_build_the_date_clause(home, chicago):
    out, mdfind, _ = _run_dated("", query="invoice", kind="pdf",
                                since="2026-11-10", until="2026-11-12")
    assert out["status"] == "ok"
    expr = mdfind[0][-1]
    # Local midnight (CST, UTC-6) on the 10th up to midnight after the 12th.
    lo, hi = "$time.iso(2026-11-10T06:00:00Z)", "$time.iso(2026-11-13T06:00:00Z)"
    assert (f"((kMDItemDateAdded >= {lo} && kMDItemDateAdded < {hi})"
            f" || (kMDItemFSCreationDate >= {lo} && kMDItemFSCreationDate < {hi}))") in expr
    assert 'kMDItemDisplayName == "*invoice*"cd' in expr and "com.adobe.pdf" in expr
    # One bound only.
    _, mdfind, _ = _run_dated("", query="a", since="2026-11-10")
    assert f"(kMDItemDateAdded >= {lo})" in mdfind[0][-1] and " < " not in mdfind[0][-1]
    _, mdfind, _ = _run_dated("", query="a", until="2026-11-12")
    assert f"(kMDItemDateAdded < {hi})" in mdfind[0][-1] and " >= " not in mdfind[0][-1]


@pytest.mark.parametrize("since,until", [
    ("last tuesday", ""), ("", "11/12/2026"), ("2026-13-40", ""),
    ('2026-11-10) || kMDItemFSName == "*"', ""), ("2026-11-10T00:00:00Z", ""),
    ("2026-11-12", "2026-11-10"),
])
def test_malformed_date_is_an_error_not_a_query(home, since, until):
    with mock.patch.object(ff.subprocess, "run") as run:
        out = find_files(query="invoice", since=since, until=until)
    assert out["status"] == "error" and out["results"] == [] and out["error"]
    run.assert_not_called()


def test_empty_query_needs_a_date_or_a_folder(home, chicago):
    docs = home / "Documents"
    docs.mkdir()
    out, mdfind, _ = _run_dated("", query="", since="2026-11-10")
    assert out["status"] == "ok"
    assert mdfind[0] == ["mdfind", "((kMDItemDateAdded >= $time.iso(2026-11-10T06:00:00Z))"
                                   " || (kMDItemFSCreationDate >= $time.iso(2026-11-10T06:00:00Z)))"]
    out, mdfind, mdls = _run_dated("", query=" ", folder=str(docs))
    assert out["status"] == "ok"
    assert mdfind[0] == ["mdfind", "-onlyin", str(docs.resolve()), 'kMDItemFSName == "*"']
    assert mdls == []
    with mock.patch.object(ff.subprocess, "run") as run:
        assert find_files(query="")["status"] == "error"
        assert find_files()["status"] == "error"
        assert find_files(query="", kind="pdf")["status"] == "error"
    run.assert_not_called()


def test_date_added_where_present_else_created(home, chicago, monkeypatch):
    # mtime order is the reverse of arrival order, to show which one sorts.
    first = _make(home / "Downloads" / "first.pdf", 9_000)
    second = _make(home / "Downloads" / "second.pdf", 1_000)
    moved_in_later = _make(home / "Downloads" / "later.pdf", 5_000)
    no_added = _make(home / "Documents" / "plain.txt", 2_000)
    old_plain = _make(home / "Documents" / "old.txt", 3_000)
    ts = lambda text: ff._parse_mdls_date(text)
    created = {no_added: ts("2026-11-10 20:00:00 +0000"),      # in range
               old_plain: ts("2026-11-01 20:00:00 +0000"),     # before it
               moved_in_later: ts("2026-11-10 12:00:00 +0000")}  # in range, but...
    monkeypatch.setattr(ff, "_birthtime",
                        lambda st: created.get(next(p for p in created
                                                    if os.stat(p).st_ino == st.st_ino), 0.0)
                        if any(os.stat(p).st_ino == st.st_ino for p in created) else 0.0)
    added = {first: "2026-11-10 15:00:00 +0000", second: "2026-11-10 23:30:00 +0000",
             moved_in_later: "2026-11-14 09:00:00 +0000"}       # ...added after the range
    stdout = "\n".join([first, second, moved_in_later, no_added, old_plain])
    out, _, mdls = _run_dated(stdout, added, since="2026-11-10", until="2026-11-10")
    assert len(mdls) == 1 and mdls[0][6:] == [first, second, moved_in_later, no_added, old_plain]
    assert [(r["name"], r["date_used"]) for r in out["results"]] == [
        ("second.pdf", "added"), ("plain.txt", "created"), ("first.pdf", "added")]
    assert out["results"][0]["date"] == ts("2026-11-10 23:30:00 +0000")
    assert out["results"][1]["date"] == created[no_added]
    assert out["count"] == 3 and all("_created" not in r for r in out["results"])
    assert set(out["results"][0]) == {"path", "name", "modified", "size", "date", "date_used"}
    # Still capped by ``limit``, newest arrival kept.
    out, _, _ = _run_dated(stdout, added, since="2026-11-10", until="2026-11-10", limit=1)
    assert [r["name"] for r in out["results"]] == ["second.pdf"]


def test_mdls_failure_falls_back_to_creation_date(home, chicago, monkeypatch):
    a = _make(home / "a.pdf", 1_000)
    monkeypatch.setattr(ff, "_birthtime",
                        lambda st: ff._parse_mdls_date("2026-11-10 20:00:00 +0000"))
    out, _, mdls = _run_dated(a, {a: "2026-11-10 15:00:00 +0000"}, mdls_rc=1,
                              since="2026-11-10", until="2026-11-10")
    assert len(mdls) == 1
    assert [(r["name"], r["date_used"]) for r in out["results"]] == [("a.pdf", "created")]


def test_no_dates_no_mdls_and_old_result_shape(home):
    a = _make(home / "a.pdf", 1_000)
    out, mdfind, mdls = _run_dated(a, query="a")
    assert mdls == [] and len(mdfind) == 1
    assert out["results"] == [{"path": a, "name": "a.pdf", "modified": 1_000, "size": 3}]


def test_cap_at_50_with_dates(home, chicago, monkeypatch):
    paths = [_make(home / f"f{i}.txt", 1_000 + i) for i in range(60)]
    monkeypatch.setattr(ff, "_birthtime",
                        lambda st: ff._parse_mdls_date("2026-11-10 20:00:00 +0000"))
    out, _, _ = _run_dated("\n".join(paths), since="2026-11-10", limit=500)
    assert out["count"] == 50
