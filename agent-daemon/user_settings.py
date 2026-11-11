"""Settings the user edits from the MenuBar app's Settings window.

Stored as JSON at ``~/.bot-inator/settings.json`` (override with
``AGENT_SETTINGS_PATH``), file mode 0600 because it can hold the Groq API
key. Keys use the same names as the environment variables they stand in
for, so :mod:`config` can merge them into ``os.environ`` at startup.

Precedence, highest first: real shell exports, this file, ``.env``.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

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
})
PROVIDERS = ("local", "groq")


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
