"""Per-tool trust: a reviewed generated tool may run outside the sandbox (#104).

The sandbox stays the default (``tools.tool_sandbox``). A generated tool
runs in the daemon process only when the user marked it trusted, from the
app: Tool Manager → tool detail ("Run outside the sandbox", which needs the
app token like every route, #27) or the off-by-default checkbox on the code
review card. No tool, chat command, standing rule (#78) or watch (#88) can
set it: only ``Orchestrator`` (the code-review answer) and
``routes.tool_detail`` import this module, and the file lives under
``~/.bot-inator``, which the sandbox denies writes to.

Trust is tied to the code: each entry is the tool name plus the SHA-256 of
its source file. Any change to the file (an edit, a repair #90, a
reinstall) makes the hash differ; :meth:`ToolTrust.check` then drops the
entry and the tool loads sandboxed again. Every call still asks (7a).

Stored in ``~/.bot-inator/tool_trust.json`` (0600), or
``$AGENT_TOOL_TRUST_PATH``::

    {"version": 1, "tools": {"close_app": {"sha256": "...",
        "trusted_at": "2026-11-11T10:00:00+00:00", "via": "tool_manager"}}}

``path=None`` keeps the store in memory (tests).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PATH_ENV = "AGENT_TOOL_TRUST_PATH"
VIA_TOOL_MANAGER = "tool_manager"
VIA_CODE_REVIEW = "code_review"

# What the sandbox blocks (``tools.tool_sandbox`` module docstring and
# ``build_profile``), for the confirm sheet. Keep in step with the profile.
SANDBOX_BLOCKS: tuple[str, ...] = (
    "Writing in ~/Library, ~/.ssh, ~/.bot-inator, your shell start-up files "
    "and the bot-inator folder (a write there would run on the next start).",
    "Reading ~/.ssh, ~/.bot-inator (the app's API token and memory) and the "
    "daemon's .env (cloud API keys).",
    "Apple Events to Terminal, iTerm, Script Editor, System Events and "
    "Finder (apps that can run a shell command for it).",
    "Running programs other than Python, osascript, shortcuts and mdfind, "
    "and opening apps, files or URLs except through the checked open proxy.",
    "Network access, unless the tool imports a network module.",
)
NETWORK_NOTE = (
    "This tool imports a network module ({reason}). Outside the sandbox its "
    "network use is not recorded in the privacy ledger."
)


def default_path() -> Path:
    override = os.environ.get(PATH_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".bot-inator" / "tool_trust.json"


def source_sha256(path: Path) -> Optional[str]:
    """Hex SHA-256 of the file's bytes; ``None`` when it can't be read."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def text_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class TrustError(ValueError):
    """The source is missing or is not the code the user looked at."""


class ToolTrust:
    """The trusted-tool list. Thread-safe; re-reads the file on each use."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._memory: dict[str, dict[str, Any]] = {}
        # Code-review checkbox: name -> SHA-256 of the approved source. It
        # becomes trust at the next load only if the installed file has that
        # exact hash (the meta-tool writes the approved source unchanged).
        self._pending: dict[str, str] = {}

    # ---- storage ------------------------------------------------------

    def _read(self) -> dict[str, dict[str, Any]]:
        if self.path is None:
            return {k: dict(v) for k, v in self._memory.items()}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("tool trust: unreadable %s; treating as empty", self.path)
            return {}
        tools = data.get("tools") if isinstance(data, dict) else None
        if not isinstance(tools, dict):
            return {}
        return {
            str(k): dict(v) for k, v in tools.items()
            if isinstance(v, dict) and isinstance(v.get("sha256"), str)
        }

    def _write(self, tools: dict[str, dict[str, Any]]) -> None:
        if self.path is None:
            self._memory = {k: dict(v) for k, v in tools.items()}
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"version": 1, "tools": tools}, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(prefix=".tool_trust.", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        os.chmod(self.path, 0o600)

    # ---- queries ------------------------------------------------------

    def entry(self, name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._read().get(name)

    def is_trusted(self, name: str, source_path: Path) -> bool:
        """Read-only: trusted with a hash matching the file right now."""
        entry = self.entry(name)
        return bool(entry) and entry.get("sha256") == source_sha256(source_path)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._read())

    # ---- changes (the app only) ----------------------------------------

    def trust(
        self, name: str, source_path: Path, *, expected_sha256: Optional[str] = None,
        via: str = VIA_TOOL_MANAGER,
    ) -> dict[str, Any]:
        """Trust ``name`` at its current source. ``expected_sha256`` (the hash
        the app showed) must match, so the user trusts the code they saw."""
        sha = source_sha256(source_path)
        if sha is None:
            raise TrustError(f"no source for {name!r}")
        if expected_sha256 is not None and expected_sha256 != sha:
            raise TrustError(f"{name!r} changed since it was shown; review it again")
        entry = {
            "sha256": sha, "via": via,
            "trusted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with self._lock:
            tools = self._read()
            tools[name] = entry
            self._write(tools)
            self._pending.pop(name, None)
        logger.info("tool trust: %s trusted (%s, %s)", name, via, sha[:12])
        return dict(entry)

    def revoke(self, name: str) -> bool:
        with self._lock:
            self._pending.pop(name, None)
            tools = self._read()
            if name not in tools:
                return False
            del tools[name]
            self._write(tools)
        logger.info("tool trust: %s revoked", name)
        return True

    def offer_from_review(self, name: str, approved_source: str) -> None:
        """The code review card's checkbox was ticked for this source."""
        with self._lock:
            self._pending[name] = text_sha256(approved_source)

    # ---- the loader ----------------------------------------------------

    def check(self, name: str, source_path: Path) -> bool:
        """At load time: True when ``name`` may load outside the sandbox.

        A pending code-review trust becomes an entry if the file is exactly
        the approved source. An entry whose hash no longer matches the file
        is dropped (the code changed), so the tool loads sandboxed and stays
        that way until the user trusts it again.
        """
        sha = source_sha256(source_path)
        pending = None
        with self._lock:
            pending = self._pending.pop(name, None)
        if pending is not None and sha is not None and pending == sha:
            self.trust(name, source_path, expected_sha256=sha, via=VIA_CODE_REVIEW)
            return True
        with self._lock:
            tools = self._read()
            entry = tools.get(name)
            if entry is None:
                return False
            if sha is not None and entry.get("sha256") == sha:
                return True
            del tools[name]
            self._write(tools)
        logger.warning("tool trust: %s changed since it was trusted; trust dropped", name)
        return False


_DEFAULT: Optional[ToolTrust] = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> ToolTrust:
    """One store per daemon at :func:`default_path` (shared by the loader and
    the routes, so a pending code-review trust is seen by both)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        path = default_path()
        if _DEFAULT is None or _DEFAULT.path != path:
            _DEFAULT = ToolTrust(path)
        return _DEFAULT


def store_for(owner: Any) -> ToolTrust:
    """``owner.tool_trust`` when set (tests, main), else :func:`default_store`."""
    store = getattr(owner, "tool_trust", None)
    return store if isinstance(store, ToolTrust) else default_store()
