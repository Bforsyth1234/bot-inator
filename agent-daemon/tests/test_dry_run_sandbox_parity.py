"""#114: the draft dry run refuses what the installed-tool sandbox refuses.

``UNSUBSCRIBE`` is the draft the app installed on 2026-11-17, trimmed: it
keeps its list in ``~/.bot-inator`` (the spec named that path). The dry run
allowed the write in its fake home, so the draft reached review; installed,
every sandboxed call failed at the ``mkdir``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tools import draft_dry_run as dr
from tools import tool_sandbox
from tools.draft_dry_run import DryRunCall, DryRunPlan, run_dry_run

needs_sandbox = pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
    reason="macOS sandbox-exec required",
)

UNSUBSCRIBE = '''"""Auto-unsubscribe tool for managing unsubscribe tracking."""
from __future__ import annotations
from smolagents import tool
import json
from pathlib import Path


@tool
def auto_unsubscribe(action: str, sender: str | None = None) -> dict:
    """Track the senders the user unsubscribed from.

    Args:
        action: "track" or "list".
        sender: The address to track.

    Returns:
        dict with a status.
    """
    storage_dir = Path.home() / ".bot-inator"
    storage_dir.mkdir(parents=True, exist_ok=True)
    json_path = storage_dir / "unsubscribed_senders.json"
    data = json.loads(json_path.read_text()) if json_path.is_file() else []
    if action == "track":
        if not sender:
            return {"status": "error", "message": "sender required"}
        if sender not in data:
            data.append(sender)
            json_path.write_text(json.dumps(data))
        return {"status": "ok", "message": f"tracking {sender}"}
    return {"status": "ok", "senders": data}
'''
MOVED = UNSUBSCRIBE.replace(
    'Path.home() / ".bot-inator"', 'Path.home() / "Documents" / "bot-inator"',
)
CALLS = [
    DryRunCall(kwargs={"action": "track", "sender": "deals@shop.test"}, expect_status="ok"),
    DryRunCall(kwargs={"action": "list"}, expect_status="ok"),
]
PLAN = DryRunPlan(files={}, calls=CALLS)
BOT_INATOR_NOTE = "read or write ~/.bot-inator"


@pytest.fixture(autouse=True)
def _fresh_sandbox_cache():
    dr.remove_sandbox_cache()
    yield
    dr.remove_sandbox_cache()


@pytest.fixture()
def real_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the user's home with the bot-inator folder inside it,
    as on a developer's Mac (``~/dev/bot-inator``). The accent is on
    purpose: ``sandbox-exec`` does not decode ``\\uXXXX`` in a rule."""
    home = Path(os.path.realpath(tmp_path)) / "réel"
    daemon = home / "dev" / "bot-inator" / "agent-daemon"
    daemon.mkdir(parents=True)
    monkeypatch.setattr(tool_sandbox, "sandbox_home", lambda: home)
    monkeypatch.setattr(tool_sandbox, "_DAEMON_ROOT", daemon)
    return home


def _tool(body: str, params: str = "", args_doc: str = "") -> str:
    return f'''"""t."""
from __future__ import annotations
from smolagents import tool
import os
import sqlite3
from pathlib import Path


@tool
def t({params}) -> dict:
    """t.
{args_doc}
    Returns:
        dict.
    """
{body}
'''


def _dry_run(name: str, source: str, plan: DryRunPlan) -> dr.DryRunResult:
    # Without this, "no problems" would also be what a broken profile gives:
    # a failed self-test skips the dry run and returns an empty result.
    assert dr.sandbox_available()
    return run_dry_run(name, source, plan)


def _one_call(source: str, call: DryRunCall | None = None, files: dict | None = None):
    return _dry_run("t", source, DryRunPlan(files=files or {}, calls=[call or DryRunCall()]))


# ---- the profile -------------------------------------------------------------

_PATH = re.compile(r'\((?:subpath|literal) ("(?:[^"\\]|\\.)*")\)')


def _closed(profile: str, kind: str) -> set[str]:
    """The paths in a profile's ``(deny file-<kind>* ...)`` lines."""
    return {
        json.loads(quoted)
        for line in profile.splitlines() if line.startswith(f"(deny file-{kind}* ")
        for quoted in _PATH.findall(line)
    }


def test_the_dry_run_closes_what_the_installed_sandbox_closes(
    real_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under the real home: at the same ``~``-relative place in the fake
    one. For reading: the real paths too (absolute paths, the secrets)."""
    root = real_home.parent
    work = root / "work"
    fake = work / "home"
    monkeypatch.setenv("AGENT_TOKEN_PATH", str(root / "moved" / "token"))
    installed = tool_sandbox.build_profile(
        home=real_home, work=root / "tool-work", python=sys.executable, network=False,
        extra_read_denied=tool_sandbox._extra_read_denied(),
    )
    dry = dr._profile(work, sys.executable, fake)

    def in_fake(path: str) -> str | None:
        try:
            return str(fake / Path(path).relative_to(real_home))
        except ValueError:
            return None

    for kind in ("write", "read"):
        closed = _closed(installed, kind) - {str(root / "tool-work" / "bin")}  # per-call shims
        moved = {in_fake(p) for p in closed} - {None}
        assert moved and moved <= _closed(dry, kind), (kind, moved - _closed(dry, kind))
    assert _closed(installed, "read") <= _closed(dry, "read")
    # Named one by one, so a slip in the parsing above cannot hide a gap.
    assert str(fake / ".bot-inator") in _closed(dry, "write") & _closed(dry, "read")
    assert str(fake / "Library") in _closed(dry, "write") - _closed(dry, "read")
    assert str(fake / ".zshrc") in _closed(dry, "write")
    assert str(fake / "dev" / "bot-inator") in _closed(dry, "write")
    assert str(fake / "dev" / "bot-inator" / "agent-daemon" / ".env") in _closed(dry, "read")
    assert str(real_home / ".bot-inator") in _closed(dry, "read")
    assert str(root / "moved" / "token") in _closed(dry, "read")
    # Written as it is on disk: an escaped "é" names no folder.
    assert "réel" in dry and "réel" in installed
    assert "\\u00e9" not in dry and "\\u00e9" not in installed
    # Writes: nothing outside ``work`` is allowed, so the real home's write
    # rule is not repeated (it would close a temp dir under ~/Library).
    assert not [p for p in _closed(dry, "write") if not p.startswith(str(fake) + "/")]
    lines = dry.splitlines()
    assert lines.count(f'(allow file-write* (subpath "{work}") (literal "/dev/null"))') == 1
    allowed_at = next(i for i, ln in enumerate(lines) if ln.startswith("(allow file-write*"))
    denied_at = [i for i, ln in enumerate(lines) if ln.startswith("(deny file-") and "(subpath" in ln]
    # 3 closed-path lines + #123's unlink line, all after the allowance.
    assert len(denied_at) == 4 and min(denied_at) > allowed_at  # the last matching rule wins


def test_a_rule_with_no_path_is_left_out() -> None:
    """A bare ``(deny file-read* )`` would close every file."""
    empty = {"write": [], "write_files": [], "read": [], "read_files": []}
    assert tool_sandbox.deny_rules(empty) == []
    rules = tool_sandbox.deny_rules({**empty, "read": [Path("/a/.ssh")]})
    assert rules == ['(deny file-read* (subpath "/a/.ssh") )']
    both = {**empty, "write": [Path("/a/Library")], "read_files": [Path("/a/.env")]}
    assert len(tool_sandbox.deny_rules(both)) == 2
    assert tool_sandbox.deny_rules(both, writes=False) == ['(deny file-read*  (literal "/a/.env"))']


def test_moved_home_keeps_the_place_and_drops_what_is_outside() -> None:
    closed = {
        "write": [Path("/Users/me/Library"), Path("/Users/me/dev/app"), Path("/opt/app"), Path("/Users/me")],
        "write_files": [Path("/Users/me/.zshrc")],
        "read": [Path("/Users/me/.ssh")],
        "read_files": [Path("/Users/me/dev/app/.env"), Path("/etc/token")],
    }
    assert tool_sandbox.moved_home(closed, Path("/Users/me"), Path("/t/home")) == {
        "write": [Path("/t/home/Library"), Path("/t/home/dev/app")],  # never the home itself
        "write_files": [Path("/t/home/.zshrc")],
        "read": [Path("/t/home/.ssh")],
        "read_files": [Path("/t/home/dev/app/.env")],
    }


# ---- the draft that got through ----------------------------------------------


@needs_sandbox
def test_a_draft_that_keeps_its_file_in_bot_inator_fails_the_dry_run() -> None:
    result = _dry_run("auto_unsubscribe", UNSUBSCRIBE, PLAN)
    raised = [p for p in result.hard if "raised PermissionError" in p]
    assert len(raised) == 2, result.all
    assert all("storage_dir.mkdir" in p for p in raised)
    # The reason comes last and once, however many calls hit it.
    note = result.hard[-1]
    assert BOT_INATOR_NOTE in note and f"{tool_sandbox.TOOL_FILES_DIR}/" in note
    assert len(result.hard) == 3


@needs_sandbox
def test_it_fails_like_the_installed_tool_when_the_folder_exists() -> None:
    """A real ``~/.bot-inator`` exists; ``mkdir(exist_ok=True)`` cannot see
    that it is a folder (3.13+: ``FileExistsError``, as on 2026-11-17)."""
    plan = DryRunPlan(files={".bot-inator/unsubscribed_senders.json": "[]"}, calls=CALLS)
    result = _dry_run("auto_unsubscribe", UNSUBSCRIBE, plan)
    raised = [p for p in result.hard if "raised FileExistsError" in p or "raised PermissionError" in p]
    assert len(raised) == 2, result.all
    assert BOT_INATOR_NOTE in result.hard[-1]


@needs_sandbox
def test_the_same_tool_passes_with_its_file_under_documents() -> None:
    assert _dry_run("auto_unsubscribe", MOVED, PLAN).all == []
    out, after = dr._execute("auto_unsubscribe", MOVED, {}, CALLS[0].kwargs)
    assert out["result"]["status"] == "ok"
    assert "Documents/bot-inator/unsubscribed_senders.json" in after


# ---- the other closed paths ---------------------------------------------------


@needs_sandbox
def test_library_can_be_read_but_not_written() -> None:
    read = _tool('''    with open(os.path.expanduser("~/Library/Preferences/x.txt")) as fh:
        return {"status": "ok", "text": fh.read()}''')
    plan_files = {"Library/Preferences/x.txt": "hello"}
    assert _one_call(read, DryRunCall(expect_status="ok"), plan_files).all == []
    write = _tool('''    os.makedirs(os.path.expanduser("~/Library/Thing"), exist_ok=True)
    with open(os.path.expanduser("~/Library/Thing/state.json"), "w") as fh:
        fh.write("{}")
    return {"status": "ok"}''')
    result = _one_call(write)
    assert any("raised PermissionError" in p for p in result.hard), result.all
    assert "write under ~/Library" in result.hard[-1]


@needs_sandbox
def test_shell_startup_files_are_closed_for_writing() -> None:
    body = '''    with open(os.path.expanduser("~/.zshrc"), "a") as fh:
        fh.write("export X=1")
    return {"status": "ok"}'''
    result = _one_call(_tool(body))
    assert any("raised PermissionError" in p for p in result.hard), result.all
    assert "write ~/.zshrc" in result.hard[-1]


@needs_sandbox
def test_the_bot_inator_folder_is_closed_at_its_place_under_the_home(real_home: Path) -> None:
    """The installed sandbox closes the repo; ``~/dev/bot-inator`` is the
    same folder, reached through ``~``, so the fake home closes it too."""
    body = '''    folder = Path.home() / "dev" / "bot-inator" / "notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "x.txt").write_text("x")
    return {"status": "ok"}'''
    result = _one_call(_tool(body))
    assert any("raised PermissionError" in p for p in result.hard), result.all
    assert "write under ~/dev/bot-inator" in result.hard[-1]


@needs_sandbox
def test_a_draft_cannot_read_the_real_bot_inator_folder(real_home: Path) -> None:
    """Unapproved code, an absolute path: the app's token stays unread
    (also with an accent in the path, see ``real_home``)."""
    token = real_home / ".bot-inator" / "token"
    token.parent.mkdir()
    token.write_text("secret-token-value", encoding="utf-8")
    source = _tool(
        '''    with open(path) as fh:
        return {"status": "ok", "text": fh.read()}''',
        params="path: str", args_doc="\n    Args:\n        path: A file.\n",
    )
    result = _one_call(source, DryRunCall(kwargs={"path": str(token)}))
    assert any("raised PermissionError" in p for p in result.hard), result.all
    assert "secret-token-value" not in " ".join(result.all)
    assert BOT_INATOR_NOTE in result.hard[-1]


@needs_sandbox
def test_relative_paths_mean_the_home_folder() -> None:
    """An installed tool starts in ``$HOME``; so does the dry-run child."""
    closed = _one_call(_tool('''    Path(".bot-inator").mkdir(exist_ok=True)
    Path(".bot-inator/state.json").write_text("{}")
    return {"status": "ok"}'''))
    assert any("raised PermissionError" in p for p in closed.hard), closed.all
    assert BOT_INATOR_NOTE in closed.hard[-1]
    source = _tool('''    Path("Notes").mkdir(exist_ok=True)
    Path("Notes/out.txt").write_text("x")
    return {"status": "ok", "seen": os.path.exists("Downloads/a.pdf")}''')
    out, after = dr._execute("t", source, {"Downloads/a.pdf": "x"}, {})
    assert out["result"] == {"status": "ok", "seen": True}
    assert "Notes/out.txt" in after


@needs_sandbox
def test_a_temp_dir_inside_a_closed_folder_still_works(
    real_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``TMPDIR`` under ``~/Library``: the real home's write rule would
    close the dry run's own work dir, and a failed self-test skips it."""
    tmp = real_home / "Library" / "T"
    tmp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    assert dr.sandbox_available()
    source = _tool('''    Path.home().joinpath("out.txt").write_text("x")
    return {"status": "ok"}''')
    out, after = dr._execute("t", source, {}, {})
    assert out["result"] == {"status": "ok"} and "out.txt" in after
    assert str(real_home / "Library" / "T") in out["home"]


@needs_sandbox
def test_ordinary_home_folders_are_untouched() -> None:
    """No new refusal for a tool that stays out of the closed folders, also
    one whose folder only starts like a closed one."""
    body = '''    os.makedirs(os.path.expanduser("~/.bot-inator-tools"), exist_ok=True)
    with open(os.path.expanduser("~/.bot-inator-tools/a.txt"), "w") as fh:
        fh.write("x")
    os.makedirs(os.path.expanduser("~/Library2"))
    return {"status": "ok"}'''
    assert _one_call(_tool(body), DryRunCall(expect_status="ok")).all == []
    out, after = dr._execute("t", _tool(body), {}, {})
    assert out["result"] == {"status": "ok"} and ".bot-inator-tools/a.txt" in after


# ---- what counts as a refusal -------------------------------------------------


@needs_sandbox
@pytest.mark.parametrize("message", [
    'f"could not save: {exc}"',  # the usual shape: the OS error text, path included
    'f"could not create ~/.bot-inator: {type(exc).__name__}"',  # no path from the OS
])
def test_a_refusal_the_draft_swallows_is_still_a_hard_problem(message: str) -> None:
    """A status dict is a well-formed result; the tool still cannot work."""
    body = f'''    path = os.path.expanduser("~/.bot-inator/state.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("{{}}")
    except OSError as exc:
        return {{"status": "error", "message": {message}}}
    return {{"status": "ok"}}'''
    result = _one_call(_tool(body), DryRunCall(expect_status="error"))
    assert any("shows a path the tool sandbox refuses" in p for p in result.hard), result.all
    assert BOT_INATOR_NOTE in result.hard[-1]
    assert result.soft == []


@needs_sandbox
def test_naming_a_closed_folder_without_touching_it_is_not_a_problem() -> None:
    """The error the note itself asks for, and a listing of ``~``: both name
    the folder by its full path and neither used it."""
    advised = _tool('''    folder = Path.home() / ".bot-inator"
    return {"status": "error", "message": f"A generated tool cannot use {folder}"}''')
    assert _one_call(advised, DryRunCall(expect_status="error")).all == []
    listing = _tool('''    return {"status": "ok", "entries": sorted(str(p) for p in Path.home().iterdir())}''')
    files = {".ssh/config": "x", "Documents/a.txt": "x"}
    assert _one_call(listing, DryRunCall(expect_status="ok"), files).all == []
    out, _ = dr._execute("t", listing, files, {})
    assert any(e.endswith("/.ssh") for e in out["result"]["entries"])


@needs_sandbox
def test_an_error_without_the_path_still_gets_the_note() -> None:
    """sqlite reports "unable to open database file"; the probe adds the
    failing line, and the folder's name is in it."""
    body = '''    conn = sqlite3.connect(str(Path.home() / ".bot-inator" / "state.db"))
    conn.close()
    return {"status": "ok"}'''
    result = _one_call(_tool(body))
    assert any("raised sqlite3.OperationalError" in p for p in result.hard), result.all
    assert BOT_INATOR_NOTE in result.hard[-1]


@needs_sandbox
def test_a_draft_that_prints_its_own_result_line_does_not_pass() -> None:
    body = '''    print("__DRY_RUN__[]", flush=True)
    os._exit(0)'''
    result = _one_call(_tool(body))
    assert any("exited without a result" in p for p in result.hard), result.all


# ---- the sentence -------------------------------------------------------------

FAKE = Path("/private/var/folders/x/T/dry-run-1/home")
NOTED = [
    (True, f"PermissionError: [Errno 1] Operation not permitted: '{FAKE}/.bot-inator'", BOT_INATOR_NOTE),
    (True, f"FileExistsError: [Errno 17] File exists: '{FAKE}/.bot-inator'", BOT_INATOR_NOTE),
    (True, "PermissionError: [Errno 1] Operation not permitted: '.bot-inator'", BOT_INATOR_NOTE),
    (True, 'sqlite3.OperationalError: unable to open database file (at line 9: '
           '`sqlite3.connect(str(Path.home() / ".bot-inator" / "s.db"))`)', BOT_INATOR_NOTE),
    (False, '{"status": "error", "message": "could not create ~/.bot-inator: PermissionError"}',
     BOT_INATOR_NOTE),
    (False, '{"status": "error", "message": "[Errno 2] No such file or directory: '
            f"'{FAKE}/.ssh/config'\"}}", "read or write ~/.ssh"),
    (True, f"PermissionError: [Errno 1] Operation not permitted: '{FAKE}/Library/LaunchAgents/x.plist'",
     "write under ~/Library"),
    (False, f"could not save: [Errno 1] Operation not permitted: '{FAKE}/library/x'",
     "write under ~/Library"),
    (True, "PermissionError: [Errno 13] Permission denied: '~/.zshrc'", "write ~/.zshrc"),
    (True, f"PermissionError: [Errno 1] Operation not permitted: '{FAKE}/dev/bot-inator/notes'",
     "write under ~/dev/bot-inator"),
    (True, f"PermissionError: [Errno 1] Operation not permitted: "
           f"'{FAKE}/dev/bot-inator/agent-daemon/.env'", "read ~/dev/bot-inator/agent-daemon/.env"),
]
SILENT = [
    (True, ""),
    (True, f"FileNotFoundError: [Errno 2] No such file or directory: '{FAKE}/Downloads/a.pdf'"),
    # Reading ~/Library is allowed: a missing file there is not a refusal.
    (True, f"FileNotFoundError: [Errno 2] No such file or directory: '{FAKE}/Library/x.plist'"),
    (False, f'{{"status": "ok", "files": ["{FAKE}/Library/Preferences/x.plist"]}}'),
    # Named, not used: the error this note asks for, and a listing of ~.
    (False, f'{{"status": "error", "message": "A generated tool cannot use {FAKE}/.bot-inator"}}'),
    (False, f'{{"status": "ok", "entries": ["{FAKE}/.ssh", "{FAKE}/Documents"]}}'),
    (False, '{"status": "error", "message": "this tool cannot use ~/.bot-inator"}'),
    # Longer names are other folders; another disk's /Users/... is not this home.
    (True, f"PermissionError: [Errno 1] Operation not permitted: '{FAKE}/.bot-inator-tools/x'"),
    (True, f"PermissionError: [Errno 1] Operation not permitted: '{FAKE}/Library2/x'"),
    (True, f"PermissionError: [Errno 13] Permission denied: '/Volumes/Backup{FAKE}/Library/x'"),
    (True, "PermissionError: [Errno 13] Permission denied: '/Library/Frameworks/x'"),
]


@pytest.mark.parametrize("raised, text, expected", NOTED)
def test_the_note_names_the_closed_path(real_home: Path, raised: bool, text: str, expected: str) -> None:
    note = tool_sandbox.protected_path_note(text, [Path("/"), FAKE, real_home], raised=raised)
    assert expected in note and f"{tool_sandbox.TOOL_FILES_DIR}/" in note


@pytest.mark.parametrize("raised, text", SILENT)
def test_the_note_stays_silent(real_home: Path, raised: bool, text: str) -> None:
    assert tool_sandbox.protected_path_note(text, [Path("/"), FAKE, real_home], raised=raised) == ""


def test_a_caught_refusal_is_hard_and_a_plain_error_stays_soft() -> None:
    """``evaluate_call`` alone, on recorded probe output (no sandbox)."""
    home = "/tmp/dry-run-x/home"
    call = DryRunCall(expect_status="ok")
    caught = {"exception": None, "home": home, "result": {
        "status": "error",
        "message": f"could not save: [Errno 1] Operation not permitted: '{home}/.bot-inator/a'",
    }}
    one = dr.evaluate_call(call, caught, set(), "dry run 1")
    assert len(one.hard) == 1 and "tool sandbox refuses" in one.hard[0] and one.soft == []
    assert BOT_INATOR_NOTE in dr.refusal_note(caught)
    for message in ("nothing to do", f"a generated tool cannot use {home}/.bot-inator"):
        plain = {"exception": None, "home": home, "result": {"status": "error", "message": message}}
        one = dr.evaluate_call(call, plain, set(), "dry run 1")
        assert one.hard == [] and len(one.soft) == 1, message
        assert dr.refusal_note(plain) == ""


# ---- the generator loop -------------------------------------------------------


class _Engine:
    """Drafts ``UNSUBSCRIBE``, then ``MOVED``; plans the two calls."""

    def __init__(self) -> None:
        self.drafts = iter([UNSUBSCRIBE, MOVED])
        self.requests: list[str] = []

    def generate_chat_sync(self, system: str, user: str, max_tokens: int = 0) -> str:
        if system == dr.PLANNER_SYSTEM_PROMPT:
            return json.dumps({"files": {}, "calls": [
                {"kwargs": c.kwargs, "expect_status": c.expect_status} for c in CALLS
            ]})
        self.requests.append(user)
        return next(self.drafts)


@needs_sandbox
def test_the_generator_repairs_the_draft_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real checks and dry run; the two model roles are scripted and the
    critic (a model pass) is off. Draft 1 uses the folder the spec names,
    the repair turn is told why that fails, draft 2 goes to review."""
    from tools import meta_tool_generator as mtg

    monkeypatch.setattr(mtg, "_GENERATED_DIR", tmp_path)
    monkeypatch.setattr(mtg, "critique", lambda *a: [])
    monkeypatch.delenv("AGENT_DRAFT_DRY_RUN", raising=False)
    assert dr.sandbox_available()
    engine = _Engine()
    source, attempts = mtg._draft_until_clean(
        engine, "auto_unsubscribe", "Track unsubscribed senders",
        "Maintain a local JSON file at ~/.bot-inator/unsubscribed_senders.json",
    )
    assert attempts == 2 and source == MOVED.strip()
    repair = engine.requests[1]
    assert "raised PermissionError" in repair
    assert BOT_INATOR_NOTE in repair and f"{tool_sandbox.TOOL_FILES_DIR}/" in repair
    rejected = list((tmp_path / "_rejected").glob("auto_unsubscribe.*.py"))
    assert len(rejected) == 1
    assert rejected[0].read_text(encoding="utf-8").startswith("# rejected: dry run:")
