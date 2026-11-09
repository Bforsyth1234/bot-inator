"""Tests for the one-time setup scan and the starter catalog (#33, #35).

Fixtures only: nothing here reads the real ~/.zsh_history, ~/Downloads,
/Applications or calendar.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

import ai  # noqa: F401  - must precede ``tools`` (circular import)
from ai.open_help import IdeaFeedback, passes_checks
from ai.setup_scan import (
    DEFAULT_CATALOG_PATH,
    SetupScanner,
    SetupSignals,
    load_catalog,
    match_catalog,
    parse_zsh_history,
    scan_downloads,
    top_repeated_commands,
)
from events.event_bus import ContextEvent, EventBus

FIX = Path(__file__).parent / "fixtures" / "setup_scan"
NOW = 1_800_000_000.0
DOCKER = "docker compose -f infra/docker-compose.yml up -d --build api worker"

GENERATED_IDEA = {
    "tool_name": "start_docker_stack",
    "description": "Start the docker compose stack for infra with one click.",
    "expected_logic": (
        "Inputs: compose_file (str, default infra/docker-compose.yml), services "
        "(list of str, default ['api', 'worker']). Run docker compose -f <file> up "
        "-d --build <services> with subprocess.run and return its output."
    ),
}


@pytest.fixture()
def mac(tmp_path: Path) -> dict[str, Path]:
    apps = tmp_path / "Applications"
    apps.mkdir()
    for name in (FIX / "apps.txt").read_text().split():
        (apps / f"{name}.app").mkdir()
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "a-folder").mkdir()
    for name, age_days in json.loads((FIX / "downloads.json").read_text()).items():
        f = downloads / name
        f.write_text("x")
        t = NOW - age_days * 86400
        os.utime(f, (t, t))
    return {
        "apps": apps,
        "downloads": downloads,
        "history": FIX / "zsh_history",
        "marker": tmp_path / "state" / "setup_scan.json",
    }


class Recorder:
    def __init__(self, idea: dict[str, Any] | None = None, scores: dict[str, int] | None = None,
                 default_score: int = 8) -> None:
        self.idea = idea
        self.scores = scores or {}
        self.default_score = default_score
        self.evaluate_prompts: list[str] = []
        self.judge_prompts: list[str] = []
        self.published: list[ContextEvent] = []
        self.sleeps: list[float] = []

    async def evaluate(self, text: str) -> str:
        self.evaluate_prompts.append(text)
        return json.dumps(self.idea) if self.idea else "NO_SUGGESTION"

    async def judge(self, text: str) -> str:
        self.judge_prompts.append(text)
        name = text.split("Proposed tool: ", 1)[1].split("\n", 1)[0]
        return json.dumps({"score": self.scores.get(name, self.default_score), "reason": "ok"})

    async def publish(self, ev: ContextEvent) -> None:
        self.published.append(ev)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _scanner(mac: dict[str, Path], rec: Recorder, **kw: Any) -> SetupScanner:
    opts: dict[str, Any] = dict(
        event_bus=EventBus(), evaluate=rec.evaluate, judge=rec.judge, cloud=False,
        feedback=IdeaFeedback(), marker_path=mac["marker"], app_dirs=[mac["apps"]],
        history_path=mac["history"], downloads_dir=mac["downloads"],
        publish=rec.publish, sleep=rec.sleep, clock=lambda: NOW,
        initial_delay_seconds=60.0, stagger_seconds=3600.0,
    )
    opts.update(kw)
    return SetupScanner(**opts)


def _names(rec: Recorder) -> list[str]:
    return [e.metadata["tool_name"] for e in rec.published]


# ---- signals ----------------------------------------------------------


def test_parse_extended_history_and_continuations() -> None:
    cmds = parse_zsh_history((FIX / "zsh_history").read_bytes())
    assert cmds.count(DOCKER) == 40
    assert "python manage.py migrate \n  --settings=app.settings.local" in cmds
    assert cmds[-1] == "git status"
    assert not any(c.startswith(": ") for c in cmds)


def test_parse_unmetafies_non_ascii() -> None:
    # zsh stores 0xC3 0xA9 ("é") as 0xC3 0x83 0x89 (0xA9 ^ 32 = 0x89).
    assert parse_zsh_history(b": 1:0;echo caf\xc3\x83\x89\n") == ["echo café"]


def test_top_repeated_drops_short_and_secret_commands() -> None:
    top = dict(top_repeated_commands(parse_zsh_history((FIX / "zsh_history").read_bytes())))
    assert top[DOCKER] == 40
    assert top["brew update && brew upgrade"] == 12
    assert "ls" not in top
    assert not any("Bearer" in c for c in top)


def test_scan_downloads_counts_and_age(mac: dict[str, Path]) -> None:
    counts, total, oldest = scan_downloads(mac["downloads"], NOW)
    assert counts[".dmg"] == 3 and counts[".pdf"] == 5 and counts[".pkg"] == 1
    assert ".ds_store" not in counts and "" not in counts  # hidden file, folder ignored
    assert total == 11
    assert oldest == 200


# ---- catalog ----------------------------------------------------------


def test_catalog_entries_are_well_formed() -> None:
    catalog = load_catalog()
    assert len(catalog) >= 10
    assert len({e["id"] for e in catalog}) == len(catalog)
    for entry in catalog:
        assert passes_checks(entry), entry["id"]
        assert entry["requires"], entry["id"]


def test_catalog_matching() -> None:
    by_id = {e["id"]: e for e in load_catalog()}
    signals = SetupSignals(
        apps={"mail", "preview"}, downloads_by_ext={".pdf": 6, ".dmg": 2, ".pkg": 1},
        downloads_total=9,
    )
    text = match_catalog(by_id["file_pdf_attachments"], signals)
    assert text and "Mail, Preview" in text and "6 .pdf" in text
    assert match_catalog(by_id["tidy_installers"], signals)  # 2 .dmg + 1 .pkg = 3
    assert match_catalog(by_id["extract_archives"], signals) is None
    signals.apps.discard("preview")
    assert match_catalog(by_id["file_pdf_attachments"], signals) is None
    assert match_catalog(by_id["brew_update"], signals) is None
    signals.shell_commands = [("brew update && brew upgrade", 12)]
    assert "typed 12 times" in match_catalog(by_id["brew_update"], signals)


# ---- the source -------------------------------------------------------


def test_scan_publishes_staggered_with_sources(mac: dict[str, Path]) -> None:
    rec = Recorder(idea=GENERATED_IDEA, scores={"start_docker_stack": 9})
    asyncio.run(_scanner(mac, rec).run())
    names = _names(rec)
    assert len(names) == 3  # max_ideas
    assert names[0] == "start_docker_stack"  # highest score first
    assert rec.published[0].event_type == "pattern_detected"
    assert rec.published[0].metadata["source"] == "setup_scan"
    assert {e.metadata["source"] for e in rec.published[1:]} == {"starter_catalog"}
    assert rec.sleeps == [60.0, 3600.0, 3600.0]  # one now(ish), the rest spaced out
    assert DOCKER in rec.evaluate_prompts[0]


def test_at_most_max_ideas_published(mac: dict[str, Path]) -> None:
    rec = Recorder(idea=GENERATED_IDEA)
    asyncio.run(_scanner(mac, rec, max_ideas=1).run())
    assert len(rec.published) == 1
    assert len(rec.judge_prompts) > 1  # more passed the judge than were shown


def test_runs_once_per_install(mac: dict[str, Path]) -> None:
    rec = Recorder(idea=GENERATED_IDEA)
    asyncio.run(_scanner(mac, rec).run())
    assert mac["marker"].exists()
    first = (len(rec.evaluate_prompts), len(rec.judge_prompts), len(rec.published))
    asyncio.run(_scanner(mac, rec).run())
    assert (len(rec.evaluate_prompts), len(rec.judge_prompts), len(rec.published)) == first


def test_restart_resumes_queue_without_rescanning(mac: dict[str, Path]) -> None:
    rec = Recorder(idea=GENERATED_IDEA)

    async def interrupted() -> None:
        scanner = _scanner(mac, rec)
        calls = 0

        async def sleep(_: float) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:  # killed while waiting for the second idea
                raise asyncio.CancelledError

        scanner._sleep = sleep
        with pytest.raises(asyncio.CancelledError):
            await scanner.run()

    asyncio.run(interrupted())
    assert len(rec.published) == 1
    judged = len(rec.judge_prompts)
    asyncio.run(_scanner(mac, rec).run())
    assert len(rec.published) == 3
    assert len(rec.judge_prompts) == judged
    state = json.loads(mac["marker"].read_text())
    assert state["pending"] == [] and len(state["published"]) == 3


def test_cloud_skips_shell_history(mac: dict[str, Path]) -> None:
    rec = Recorder(idea=GENERATED_IDEA)
    calendar_called = []
    scanner = _scanner(mac, rec, cloud=True, calendar_reader=lambda: calendar_called.append(1) or [])
    signals = asyncio.run(scanner.collect())
    assert signals.history_skipped and signals.shell_commands == []
    assert calendar_called == []
    asyncio.run(scanner.run())
    assert rec.evaluate_prompts == []
    for prompt in rec.judge_prompts + [json.dumps(e.metadata) for e in rec.published]:
        assert "docker" not in prompt and "brew" not in prompt


def test_local_reads_shell_history(mac: dict[str, Path]) -> None:
    signals = asyncio.run(_scanner(mac, Recorder()).collect())
    assert signals.shell_commands[0] == (DOCKER, 40)


def test_judge_rejected_idea_is_dropped(mac: dict[str, Path]) -> None:
    rec = Recorder(idea=GENERATED_IDEA, scores={"start_docker_stack": 3}, default_score=2)
    rec.scores["tidy_downloaded_installers"] = 8
    scanner = _scanner(mac, rec)
    asyncio.run(scanner.run())
    assert _names(rec) == ["tidy_downloaded_installers"]
    stages = {v["tool_name"]: v["stage"] for v in scanner.verdicts}
    assert stages["start_docker_stack"] == "judge"


def test_rejected_feedback_drops_before_judge(mac: dict[str, Path]) -> None:
    rec = Recorder(idea=GENERATED_IDEA)
    feedback = IdeaFeedback()
    feedback.record("tidy_downloaded_installers", "Move installers to the Trash.", approved=False)
    scanner = _scanner(mac, rec, feedback=feedback)
    asyncio.run(scanner.run())
    assert "tidy_downloaded_installers" not in _names(rec)
    assert not any("Proposed tool: tidy_downloaded_installers" in p for p in rec.judge_prompts)
    assert "rejected: tidy_downloaded_installers" in rec.judge_prompts[0]


def test_rejection_after_scan_skips_queued_idea(mac: dict[str, Path]) -> None:
    rec = Recorder(idea=GENERATED_IDEA)
    feedback = IdeaFeedback()
    scanner = _scanner(mac, rec, feedback=feedback)
    queued = asyncio.run(scanner.scan_and_filter())
    assert len(queued) == 3
    # The user rejects something like the second idea before it is due.
    feedback.record(queued[1]["tool_name"], queued[1]["description"], approved=False)
    asyncio.run(scanner._drain({"pending": list(queued), "published": [], "next_at": NOW}))
    assert _names(rec) == [queued[0]["tool_name"], queued[2]["tool_name"]]


def test_calendar_reader_feeds_catalog(mac: dict[str, Path]) -> None:
    async def calendar() -> list[dict[str, Any]]:
        return [{"title": "Team standup"}] * 5 + [{"title": "1:1", "recurring": True}]

    rec = Recorder()
    scanner = _scanner(mac, rec, calendar_reader=calendar, max_ideas=10)
    asyncio.run(scanner.run())
    names = _names(rec)
    assert "join_next_video_meeting" in names and "write_daily_agenda_note" in names
    assert rec.published[0].metadata["source"] == "starter_catalog"


def test_unreadable_marker_never_rescans(mac: dict[str, Path]) -> None:
    mac["marker"].parent.mkdir(parents=True)
    mac["marker"].write_text("{not json")
    rec = Recorder(idea=GENERATED_IDEA)
    asyncio.run(_scanner(mac, rec).run())
    assert rec.evaluate_prompts == [] and rec.published == []


def test_default_catalog_path_ships() -> None:
    assert DEFAULT_CATALOG_PATH.exists()
