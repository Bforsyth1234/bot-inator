"""#96 follow-up: on a Mac without the Xcode command line tools,
/usr/bin/git is a stub that pops macOS's "install developer tools" dialog.
The nested-repo helpers must notice and not run it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tools import meta_tool_generator as M


def _reset(monkeypatch) -> None:
    monkeypatch.setattr(M, "_git_ok_cache", None)


def test_stub_git_without_clt_is_not_run(monkeypatch, tmp_path: Path) -> None:
    _reset(monkeypatch)
    ran = []
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        ran.append(cmd[0])
        if cmd[0] == "/usr/bin/xcode-select":
            return subprocess.CompletedProcess(cmd, 2, "", "no developer tools")
        raise AssertionError(f"ran {cmd!r}")

    monkeypatch.setattr(M.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(M.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(M.subprocess, "run", fake_run)
    (tmp_path / "t.py").write_text("x = 1\n")
    ok, summary = M._git_commit(tmp_path, "t.py", "install t")
    assert not ok and "Xcode command line tools" in summary
    assert M._git_read(tmp_path, "log").returncode == 1
    assert M._git_identity_args()  # falls back to the synthetic identity
    assert ran == ["/usr/bin/xcode-select"]
    monkeypatch.setattr(M.subprocess, "run", real_run)


def test_stub_git_with_clt_is_available(monkeypatch) -> None:
    _reset(monkeypatch)
    monkeypatch.setattr(M.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(M.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(M.subprocess, "run",
                        lambda cmd, *a, **kw: subprocess.CompletedProcess(cmd, 0, "/Library/Developer/CommandLineTools\n", ""))
    assert M._git_available()


def test_no_git_on_path(monkeypatch, tmp_path: Path) -> None:
    _reset(monkeypatch)
    monkeypatch.setattr(M.shutil, "which", lambda name: None)
    ok, summary = M._git_commit(tmp_path, "t.py", "m")
    assert not ok and "isn't installed" in summary
