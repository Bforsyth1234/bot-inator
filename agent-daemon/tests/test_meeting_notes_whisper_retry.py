"""#108: a Whisper failure in the daemon keeps the audio for the app's Retry.

mlx-whisper is never imported: the transcriber is injected (a fake that
fails, then works) or ``mlx_whisper`` is stubbed in ``sys.modules``.
"""
from __future__ import annotations

import asyncio
import json
import stat
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from ai import prepared_offers
from events import meeting_notes as MN
from events.event_bus import ContextEvent
from schemas.ws_messages import MeetingRecordStop, MeetingRecordStopPayload

FIXTURE = Path(__file__).parent / "fixtures" / "meeting_notes_transcript.txt"
NOW = datetime(2026, 11, 11, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setattr(MN.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


class FakeLink(MN.RecorderLink):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[tuple[str, dict]] = []

    def send(self, frame_type, payload):
        self.frames.append((frame_type, dict(payload)))
        return True


class FlakyWhisper:
    """Fails until ``ok`` is set: a missing / broken model, then fixed."""

    def __init__(self) -> None:
        self.ok = False
        self.calls: list[str] = []

    def __call__(self, path: str) -> str:
        self.calls.append(Path(path).name)
        if not self.ok:
            raise RuntimeError("model download failed")
        return FIXTURE.read_text("utf-8") if path.endswith("-me.m4a") else "Thanks all."


def make(transcriber, clock=lambda: NOW):
    link, published = FakeLink(), []

    async def publish(ev: ContextEvent) -> None:
        published.append(ev)

    notes = MN.MeetingNotes(link=link, clock=clock, timer=lambda d, fn: None,
                            enabled=lambda: True, lead_minutes=lambda: 10,
                            engine_name=lambda: "whisper", full_name="",
                            transcriber=transcriber, publish=publish)
    return notes, link, published


def _audio(name: str) -> Path:
    p = MN.audio_dir() / name
    p.write_bytes(b"\0" * 32)
    p.chmod(0o644)
    return p


def _payload(mic: Path, other: Path | None = None, mid: str = "m108") -> dict[str, Any]:
    return {"meeting_id": mid, "title": "Design sync", "started_at": "2026-11-11T14:00:00Z",
            "ended_at": "2026-11-11T14:30:00Z", "stop_reason": "user", "audio_path": str(mic),
            "system_audio_path": str(other) if other else None}


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def _transcription_frames(link: FakeLink) -> list[dict]:
    return [f for t, f in link.frames if t == MN.FRAME_STOP and f.get("transcription")]


def test_failure_keeps_audio_0600_with_the_apps_marker_and_tells_the_app() -> None:
    mic, other = _audio("m108-me.m4a"), _audio("m108-others.m4a")
    whisper = FlakyWhisper()
    notes, link, published = make(whisper)
    out = asyncio.run(notes.process(_payload(mic, other)))
    assert out == {"status": "error", "reason": "model download failed", "kept": True}
    assert mic.exists() and other.exists() and published == []
    assert _mode(mic) == 0o600 and _mode(other) == 0o600
    marker = MN.marker_path("m108")
    assert marker.parent == MN.audio_dir() and _mode(marker) == 0o600
    data = json.loads(marker.read_text("utf-8"))
    # The keys and Date encoding of the app's `FailedRecording` (#105), so
    # its 24-hour purge keeps it and its panel can show it.
    assert data["meetingId"] == "m108" and data["title"] == "Design sync"
    assert data["startedAt"] == "2026-11-11T14:00:00Z" and data["endedAt"] == "2026-11-11T14:30:00Z"
    assert data["stopReason"] == "user" and data["error"] == "model download failed"
    assert data["micPath"] == str(mic) and data["systemPath"] == str(other)
    assert data["engine"] == "whisper"
    apple_epoch = datetime(2001, 1, 1, tzinfo=timezone.utc)
    assert apple_epoch + timedelta(seconds=data["failedAt"]) == NOW
    [frame] = _transcription_frames(link)
    assert frame["transcription"] == "failed" and frame["kept"] is True
    assert frame["meeting_id"] == "m108" and frame["error"] == "model download failed"
    assert frame["audio_path"] == str(mic) and frame["system_audio_path"] == str(other)
    assert frame["expires_at"] == (NOW + timedelta(hours=24)).isoformat(timespec="seconds")
    # The frame survives the daemon's real serializer (main._meeting_sender).
    wire = MeetingRecordStop(seq=1, timestamp=NOW.isoformat(), payload=MeetingRecordStopPayload(**frame))
    assert json.loads(wire.model_dump_json())["payload"]["transcription"] == "failed"


def test_retry_after_the_fix_gives_one_card_and_deletes_audio_and_marker() -> None:
    mic, other = _audio("m108-me.m4a"), _audio("m108-others.m4a")
    whisper = FlakyWhisper()
    notes, link, published = make(whisper)
    asyncio.run(notes.process(_payload(mic, other)))
    assert published == [] and mic.exists()
    whisper.ok = True
    # The panel's Retry sends the same meeting_audio_ready again.
    out = asyncio.run(notes.process(_payload(mic, other)))
    assert out["status"] == "offered"
    assert len(published) == 1
    assert not mic.exists() and not other.exists() and not MN.marker_path("m108").exists()
    assert list(MN.audio_dir().iterdir()) == []
    offer = published[0].metadata[prepared_offers.PREPARED_KEY]
    assert offer["actions"]
    frames = _transcription_frames(link)
    assert [f["transcription"] for f in frames] == ["failed", "done"]


def test_two_failures_then_success_is_still_exactly_one_card() -> None:
    mic = _audio("m108-me.m4a")
    whisper = FlakyWhisper()
    notes, link, published = make(whisper)
    for _ in range(2):
        out = asyncio.run(notes.process(_payload(mic)))
        assert out["kept"] is True and mic.exists()
    whisper.ok = True
    asyncio.run(notes.process(_payload(mic)))
    assert len(published) == 1 and not mic.exists()


def test_24_hours_run_from_the_first_failure() -> None:
    mic = _audio("m108-me.m4a")
    now = [NOW]
    notes, link, _ = make(FlakyWhisper(), clock=lambda: now[0])
    asyncio.run(notes.process(_payload(mic)))
    now[0] = NOW + timedelta(hours=5)
    asyncio.run(notes.process(_payload(mic)))
    data = json.loads(MN.marker_path("m108").read_text("utf-8"))
    assert datetime(2001, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=data["failedAt"]) == NOW
    # A Retry failing after the day is up keeps nothing.
    now[0] = NOW + timedelta(hours=24)
    out = asyncio.run(notes.process(_payload(mic)))
    assert out["kept"] is False and not mic.exists() and not MN.marker_path("m108").exists()
    assert _transcription_frames(link)[-1]["kept"] is False


def test_an_apple_marker_is_replaced_and_its_clock_kept() -> None:
    """#105's "Use Whisper" hands an on-device failure to the daemon."""
    mic = _audio("m108-me.m4a")
    first = NOW - timedelta(hours=3)
    MN.marker_path("m108").write_text(json.dumps({
        "meetingId": "m108", "title": "Design sync", "startedAt": "", "endedAt": "",
        "error": "Siri and Dictation are disabled", "micPath": str(mic),
        "failedAt": (first - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds()}))
    notes, *_ = make(FlakyWhisper())
    asyncio.run(notes.process(_payload(mic)))
    data = json.loads(MN.marker_path("m108").read_text("utf-8"))
    assert data["engine"] == "whisper" and data["error"] == "model download failed"
    assert datetime(2001, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=data["failedAt"]) == first


def test_nothing_outside_the_audio_folder_is_kept_or_marked(home) -> None:
    outside = home / "elsewhere.m4a"
    outside.write_bytes(b"x")
    notes, link, _ = make(FlakyWhisper())
    out = asyncio.run(notes.process(_payload(outside)))
    assert out["status"] == "error" and out.get("kept") in (None, False)
    assert outside.exists() and not MN.marker_path("m108").exists()


def test_an_app_reported_error_still_deletes_the_audio() -> None:
    mic = _audio("m108-me.m4a")
    notes, *_ = make(FlakyWhisper())
    payload = {**_payload(mic), "error": "mic lost"}
    out = asyncio.run(notes.process(payload))
    assert out["status"] == "error" and not mic.exists() and not MN.marker_path("m108").exists()


def test_missing_mlx_whisper_is_a_kept_failure(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)  # import raises
    mic = _audio("m108-me.m4a")
    notes, link, _ = make(None)
    out = asyncio.run(notes.process(_payload(mic)))
    assert out["kept"] is True and "mlx-whisper is not installed" in out["reason"] and mic.exists()
    # Installed later: the Retry uses it.
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = lambda path, path_or_hf_repo: {"text": FIXTURE.read_text("utf-8")}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    out = asyncio.run(notes.process(_payload(mic)))
    assert out["status"] == "offered" and not mic.exists()


def test_main_does_not_notify_on_a_kept_failure() -> None:
    # The app's panel already shows it with Retry / Delete (#108 runner).
    import main

    mic = _audio("m108-me.m4a")
    notes, *_ = make(FlakyWhisper())
    with mock.patch("tools.show_notification.show_notification") as notify:
        out = asyncio.run(main._process_meeting_audio(notes, _payload(mic)))
    assert out["kept"] is True and mic.exists() and notify.call_count == 0
