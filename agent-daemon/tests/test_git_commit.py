"""Integration tests for the nested-repo auto-commit (todo #30, DONE #8).

Drives ``meta_tool_generator._git_commit`` against a real on-disk repo in
``tmp_path``. Every test points ``HOME`` / ``GIT_CONFIG_GLOBAL`` at files
under ``tmp_path`` and sets ``GIT_CONFIG_NOSYSTEM``, so the developer's
real git config is neither read nor written. Skipped when ``git`` is not
on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tools import meta_tool_generator as mtg

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git executable not on PATH",
)

# Env vars that would override the ``-c user.*`` args or redirect git.
_GIT_OVERRIDES = (
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL", "EMAIL", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_INDEX_FILE", "GIT_CONFIG", "GIT_CONFIG_COUNT",
)


@pytest.fixture
def git_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME with an empty global config; returns its gitconfig path."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("")
    for var in _GIT_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return gitconfig


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "generated"
    d.mkdir()
    return d


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=directory, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _write(directory: Path, name: str, body: str) -> None:
    (directory / name).write_text(body)


def test_first_commit_inits_repo_with_fallback_identity(git_home, repo_dir):
    _write(repo_dir, "my_tool.py", "x = 1\n")
    assert not (repo_dir / ".git").exists()

    ok, summary = mtg._git_commit(repo_dir, "my_tool.py", "add my_tool")

    assert ok, summary
    assert (repo_dir / ".git").is_dir()
    assert _git(repo_dir, "rev-list", "--count", "HEAD") == "1"
    assert _git(repo_dir, "log", "-1", "--format=%s") == "add my_tool"
    assert _git(repo_dir, "log", "-1", "--format=%an <%ae>") == (
        "bot-inator <bot-inator@local>"
    )
    assert _git(repo_dir, "ls-files") == "my_tool.py"


def test_identity_comes_from_global_config(git_home, repo_dir):
    git_home.write_text("[user]\n\tname = Ada Host\n\temail = ada@host.test\n")
    assert mtg._git_identity_args() == [
        "-c", "user.name=Ada Host", "-c", "user.email=ada@host.test",
    ]
    _write(repo_dir, "t.py", "y = 2\n")

    ok, summary = mtg._git_commit(repo_dir, "t.py", "add t")

    assert ok, summary
    assert _git(repo_dir, "log", "-1", "--format=%an <%ae>") == (
        "Ada Host <ada@host.test>"
    )
    assert _git(repo_dir, "log", "-1", "--format=%cn <%ce>") == (
        "Ada Host <ada@host.test>"
    )


def test_partial_global_identity_falls_back_per_key(git_home, repo_dir):
    git_home.write_text("[user]\n\tname = Only Name\n")
    assert mtg._git_identity_args() == [
        "-c", "user.name=Only Name", "-c", "user.email=bot-inator@local",
    ]


def test_second_commit_on_existing_repo(git_home, repo_dir):
    _write(repo_dir, "a.py", "a = 1\n")
    assert mtg._git_commit(repo_dir, "a.py", "add a")[0]
    _write(repo_dir, "a.py", "a = 2\n")
    _write(repo_dir, "b.py", "b = 1\n")

    ok, summary = mtg._git_commit(repo_dir, "a.py", "update a")

    assert ok, summary
    assert _git(repo_dir, "rev-list", "--count", "HEAD") == "2"
    assert _git(repo_dir, "log", "-1", "--format=%s") == "update a"
    # Only the named file is staged; the stray b.py stays untracked.
    assert _git(repo_dir, "ls-files") == "a.py"


def test_nothing_to_commit_is_reported_not_raised(git_home, repo_dir):
    _write(repo_dir, "a.py", "a = 1\n")
    assert mtg._git_commit(repo_dir, "a.py", "add a")[0]

    ok, summary = mtg._git_commit(repo_dir, "a.py", "again")

    assert ok is False
    assert "nothing" in summary.lower()


def test_missing_git_is_non_fatal(git_home, repo_dir, tmp_path, monkeypatch):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    _write(repo_dir, "a.py", "a = 1\n")

    ok, summary = mtg._git_commit(repo_dir, "a.py", "add a")

    assert (ok, summary) == (False, "git executable not found on PATH")
    assert not (repo_dir / ".git").exists()


def test_missing_directory_is_non_fatal(git_home, tmp_path):
    ok, summary = mtg._git_commit(tmp_path / "nope", "a.py", "add a")
    assert ok is False
    assert "directory missing" in summary
