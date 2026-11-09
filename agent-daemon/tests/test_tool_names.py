"""Name collisions between generated tools (todo #24).

``generate_custom_tool`` must reject a name that is already taken by a
generated tool (on disk or loaded), naming the existing tool and its
description, before any drafting or approval work. After the tool is
deleted the same name installs normally. All state lives in ``tmp_path``;
the module globals are monkeypatched, never the real generated dir.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from tools import meta_tool_generator as mtg

_EXISTING_SRC = '''"""Module doc."""
from smolagents import tool


@tool
def summarize_pdf(path: str) -> str:
    """Summarize a PDF by extracting its first page of text.

    Args:
        path: The PDF file.
    """
    return path
'''

_NEW_SRC = '''from smolagents import tool


@tool
def summarize_pdf(path: str) -> str:
    """Summarize a PDF, v2.

    Args:
        path: The PDF file.
    """
    return path.upper()
'''


class _Calls:
    def __init__(self) -> None:
        self.drafts: list[str] = []
        self.approvals: list[str] = []
        self.reruns: list[str] = []
        self.loads = 0


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gen = tmp_path / "generated"
    gen.mkdir()
    calls = _Calls()

    def _load() -> list[str]:
        calls.loads += 1
        orch._dynamic_tool_names.update(p.stem for p in gen.glob("*.py"))
        return sorted(orch._dynamic_tool_names)

    orch = SimpleNamespace(
        tools=[],
        _dynamic_tool_names=set(),
        mark_tool_ready_for_rerun=calls.reruns.append,
        load_dynamic_tools=_load,
    )

    def _draft(engine, tool_name, description, expected_logic):
        calls.drafts.append(tool_name)
        return _NEW_SRC, 1

    def _approve(tool_name, description, source):
        calls.approvals.append(tool_name)
        return SimpleNamespace(approved=True, edited_code=None, user_note=None)

    monkeypatch.setattr(mtg, "_ORCHESTRATOR", orch)
    monkeypatch.setattr(mtg, "_ENGINE", object())
    monkeypatch.setattr(mtg, "_DRAFT_ENGINE", None)
    monkeypatch.setattr(mtg, "_GENERATED_DIR", gen)
    monkeypatch.setattr(mtg, "_draft_until_clean", _draft)
    monkeypatch.setattr(mtg, "_request_code_approval", _approve)
    monkeypatch.setattr(mtg, "_git_commit", lambda *a: (True, "committed"))
    return SimpleNamespace(gen=gen, orch=orch, calls=calls)


def _generate(name: str = "summarize_pdf") -> dict:
    return mtg.generate_custom_tool(
        tool_name=name,
        description="Summarize a PDF document into a short paragraph.",
        expected_logic="Read the PDF and return a summary string.",
    )


def _assert_rejected(result: dict, env, description: str) -> None:
    assert result["status"] == "error"
    assert result["error"] == "name_collision"
    assert result["existing_tool"] == "summarize_pdf"
    assert result["existing_description"] == description
    assert "summarize_pdf" in result["message"]
    assert description in result["message"]
    assert "different name" in result["message"]
    assert "delete" in result["message"]
    # Rejected before any draft / approval work, and no silent rerun.
    assert env.calls.drafts == []
    assert env.calls.approvals == []
    assert env.calls.reruns == []


def test_on_disk_name_is_rejected_with_existing_description(env):
    (env.gen / "summarize_pdf.py").write_text(_EXISTING_SRC)

    result = _generate()

    _assert_rejected(result, env, "Summarize a PDF by extracting its first page of text.")
    assert result["path"] == str(env.gen / "summarize_pdf.py")
    assert (env.gen / "summarize_pdf.py").read_text() == _EXISTING_SRC


def test_loaded_name_is_rejected_not_routed_as_similar(env):
    # Loaded but not on disk (e.g. file removed out from under the daemon).
    env.orch.tools.append(SimpleNamespace(
        name="summarize_pdf", description="Loaded summarizer.\nMore text.",
    ))
    env.orch._dynamic_tool_names.add("summarize_pdf")

    result = _generate()

    # Without the collision check this hit the similarity guard instead
    # (status "similar_exists", rerun marked).
    _assert_rejected(result, env, "Loaded summarizer.")


def test_reinstall_after_delete_works(env):
    path = env.gen / "summarize_pdf.py"
    path.write_text(_EXISTING_SRC)
    env.orch._dynamic_tool_names.add("summarize_pdf")
    assert _generate()["error"] == "name_collision"

    # What DELETE /api/tools/{name} does: unload, then unlink.
    env.orch._dynamic_tool_names.discard("summarize_pdf")
    path.unlink()

    result = _generate()

    assert result["status"] == "ok", result
    assert env.calls.drafts == ["summarize_pdf"]
    assert env.calls.approvals == ["summarize_pdf"]
    assert path.read_text() == _NEW_SRC
    assert "summarize_pdf" in env.orch._dynamic_tool_names


def test_unrelated_name_is_not_a_collision(env):
    (env.gen / "summarize_pdf.py").write_text(_EXISTING_SRC)
    assert mtg._existing_generated_tool("start_timer") is None
    assert mtg._existing_generated_tool("summarize_pdf")["where"] == "on disk"


def test_file_description_is_parsed_without_import(env):
    path = env.gen / "boom.py"
    path.write_text('raise SystemExit("imported!")\n\ndef boom():\n    """Says boom."""\n')
    assert mtg._file_tool_description(path, "boom") == "Says boom."
    (env.gen / "bad.py").write_text("def (:\n")
    assert mtg._file_tool_description(env.gen / "bad.py", "bad") == ""
