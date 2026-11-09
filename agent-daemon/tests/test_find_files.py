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
