"""Settings the user edits from the MenuBar app's Settings window.

Stored as JSON at ``~/.bot-inator/settings.json`` (override with
``AGENT_SETTINGS_PATH``), file mode 0600 because it can hold provider API
keys. Keys use the same names as the environment variables they stand in
for, so :mod:`config` can merge them into ``os.environ`` at startup.

Precedence, highest first: real shell exports, this file, ``.env``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import providers

# Only these keys may be written from the UI.
ALLOWED_KEYS = frozenset({
    "AGENT_PROVIDER",
    "AGENT_PROVIDER_MAIN",
    "AGENT_PROVIDER_ANALYSIS",
    "AGENT_PROVIDER_DRAFTING",
    "GROQ_API_KEY",
    "GROQ_MAIN_MODEL",
    "GROQ_ANALYSIS_MODEL",
    "GROQ_DRAFTING_MODEL",
    "MLX_MAIN_MODEL",
    "MLX_ANALYSIS_MODEL",
    "MLX_DRAFTING_MODEL",
    # "1"/"0": let cloud engines see email and message text (#52). Read
    # live by ai.privacy.personal_text_allowed, so it needs no restart.
    "AGENT_CLOUD_PERSONAL_TEXT",
    # Morning brief (#65): "1"/"0" (default on) and "HH:MM" local time
    # (default "08:00"). Read live by events.daily_brief; no restart.
    "AGENT_DAILY_BRIEF",
    "AGENT_DAILY_BRIEF_TIME",
    # Meeting prep (#71): "1"/"0" (default on) and whole minutes before the
    # meeting, 1..10 (default 10). Read live by events.meeting_prep.
    "AGENT_MEETING_PREP",
    "AGENT_MEETING_PREP_MINUTES",
    # Activity history (#75): "1"/"0" (default on). Read live by
    # ai.activity_store through os.environ (routes/activity.py updates it).
    "AGENT_ACTIVITY_HISTORY",
    # Waiting on (#73): whole days a sent question may go unanswered before
    # the brief lists it, 1..14 (default 3). Read live by ai.waiting_on.
    "AGENT_WAITING_ON_DAYS",
    # Privacy ledger (#82): "1" keeps what was sent (7 days) next to each
    # row; default "0" keeps metadata only. Read live by ai.egress_log.
    "AGENT_EGRESS_KEEP_TEXT",
    # iPhone remote (#85): "1"/"0" (default off, 7c), the own handle (E.164
    # phone or Apple ID email) and idle minutes 1..120 (default 10). Read
    # live by events.remote_channel.
    "AGENT_REMOTE",
    "AGENT_REMOTE_HANDLE",
    "AGENT_REMOTE_IDLE_MINUTES",
    # Meeting notes (#86): "1"/"0" (default off) and the engine "apple" |
    # "whisper". Read live by events.meeting_notes.
    "AGENT_MEETING_NOTES",
    "AGENT_MEETING_NOTES_ENGINE",
    # First run (#93): "local" | "cloud" | "mixed", what the user picked on
    # the first-run screen. Informational; its presence is not the gate
    # (the settings file existing is).
    "AGENT_FIRST_RUN_CHOICE",
    # Vision (#107): "local" (default) or a preset id, and the local MLX
    # vision model. A cloud preset also needs <PREFIX>_VISION_MODEL (below)
    # and AGENT_CLOUD_PERSONAL_TEXT=1. Read live by ai.vision_engine.
    "AGENT_VISION_PROVIDER",
    "MLX_VISION_MODEL",
}) | providers.vision_env_keys() | providers.preset_env_keys()  # <PRESET>_API_KEY / _API_BASE / _<ROLE>_MODEL (#97)
PROVIDERS = providers.PROVIDERS


def settings_path() -> Path:
    override = os.environ.get("AGENT_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bot-inator" / "settings.json"


def read() -> dict[str, str]:
    """Return the saved settings, or ``{}`` if the file is missing/corrupt."""
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: str(v) for k, v in data.items()
        if k in ALLOWED_KEYS and isinstance(v, str)
    }


def write(updates: dict[str, str]) -> dict[str, str]:
    """Merge ``updates`` into the saved file and return the new contents.

    An empty-string value removes that key. Written atomically with 0600
    permissions so the API key is never world-readable, even briefly.
    """
    unknown = set(updates) - ALLOWED_KEYS
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    merged = read()
    for key, value in updates.items():
        if value:
            merged[key] = value
        else:
            merged.pop(key, None)

    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return merged


# Keys the user exported in their shell before the daemon started. These
# beat the settings file, so the UI can't change them. Filled by
# :func:`apply_to_environ`, which must run before ``load_dotenv``.
shell_exported: frozenset[str] = frozenset()


def apply_to_environ() -> None:
    """Copy saved settings into ``os.environ`` without overriding exports."""
    global shell_exported
    shell_exported = frozenset(k for k in ALLOWED_KEYS if os.environ.get(k))
    for key, value in read().items():
        if not os.environ.get(key):
            os.environ[key] = value


def clear_inherited_environ() -> None:
    """Drop values this process put in ``os.environ`` (from this file or
    ``.env``) so a re-exec'd daemon reads the settings file fresh."""
    for key in ALLOWED_KEYS - shell_exported:
        os.environ.pop(key, None)
