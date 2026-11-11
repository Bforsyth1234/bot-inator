"""Daemon configuration settings."""
import importlib.abc
import importlib.machinery
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

import providers
import user_settings

# --- writable home for the app-bundled daemon (#96) ------------------------
# A signed .app is read-only, so the daemon bundled inside it cannot write
# into its own tree. Two files lived there: ``.env`` and the generated
# tools (``tools/generated``, a nested git repo). ``AGENT_DATA_DIR`` (set by
# the menu bar app's DaemonLauncher to ``~/Library/Application Support/
# bot-inator``) moves both; ``AGENT_GENERATED_TOOLS_DIR`` moves only the
# tools. Unset (``./run.sh``), the dev layout is unchanged. Real
# environment only: neither key can come from the settings file.
DATA_DIR_ENV = "AGENT_DATA_DIR"
GENERATED_TOOLS_DIR_ENV = "AGENT_GENERATED_TOOLS_DIR"
_DAEMON_DIR = Path(__file__).resolve().parent
IN_TREE_GENERATED_TOOLS_DIR = _DAEMON_DIR / "tools" / "generated"


def data_dir() -> Optional[Path]:
    """``AGENT_DATA_DIR`` as a path, ``None`` when unset (the dev layout)."""
    raw = os.environ.get(DATA_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else None


def dotenv_path() -> Path:
    """The ``.env`` the daemon loads: in the data dir when one is set."""
    home = data_dir()
    return home / ".env" if home else _DAEMON_DIR / ".env"


def default_generated_tools_dir() -> Path:
    """``AGENT_GENERATED_TOOLS_DIR``, else ``<data dir>/tools/generated``,
    else ``agent-daemon/tools/generated``."""
    raw = os.environ.get(GENERATED_TOOLS_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    home = data_dir()
    return home / "tools" / "generated" if home else IN_TREE_GENERATED_TOOLS_DIR


class _GeneratedToolsFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Resolve the ``tools.generated`` package to a moved generated dir, so
    ``from tools.generated.<name> import <name>`` (one generated tool
    composing another) keeps working when the tools live outside the tree."""

    def __init__(self, directory: Path) -> None:
        self.directory = str(directory)

    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        if fullname != "tools.generated":
            return None
        spec = importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        spec.submodule_search_locations = [self.directory]
        return spec

    def create_module(self, spec):  # noqa: ANN001
        return None

    def exec_module(self, module) -> None:  # noqa: ANN001
        return None


def _use_moved_generated_dir(directory: Path) -> None:
    """Create ``directory`` and point the ``tools.generated`` package at it."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # surfaced again when a tool is installed
        logging.getLogger(__name__).warning(
            "cannot create the generated tools dir %s: %s", directory, exc
        )
    if any(isinstance(f, _GeneratedToolsFinder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _GeneratedToolsFinder(directory))
    loaded = sys.modules.get("tools.generated")
    if loaded is not None:
        loaded.__path__ = [str(directory)]


if default_generated_tools_dir() != IN_TREE_GENERATED_TOOLS_DIR:
    _use_moved_generated_dir(default_generated_tools_dir())

# Load ``agent-daemon/.env`` (resolved relative to this file, not CWD) so the
# daemon picks up ``GROQ_API_KEY`` and friends regardless of where uvicorn is
# launched from. ``override=False`` keeps real shell exports authoritative,
# which matters for tests and for the ``AGENT_PROVIDER=local`` overrides we
# sometimes pass on the launch command line.
#
# Settings saved from the MenuBar app's Settings window are applied first,
# so they win over ``.env`` but still lose to real shell exports.
# With ``AGENT_DATA_DIR`` set (the app-bundled daemon, #96) the ``.env`` is
# ``<data dir>/.env`` instead.
user_settings.apply_to_environ()
load_dotenv(dotenv_path(), override=False)


# Groq model defaults. GPT-OSS models are the general chat models in
# Groq's production tier that a developer-plan key can use; the Llama
# models are Enterprise-only and qwen3-32b is no longer listed
# (checked 2026-11-09 against https://console.groq.com/docs/models).
GROQ_DEFAULT_MODELS: dict[str, str] = {
    "GROQ_MAIN_MODEL": "openai/gpt-oss-120b",
    "GROQ_ANALYSIS_MODEL": "openai/gpt-oss-20b",
    "GROQ_DRAFTING_MODEL": "openai/gpt-oss-120b",
}

# Local MLX model defaults: small enough (~14 GB total) for any Apple
# Silicon Mac. Bigger machines override them with ``MLX_*_MODEL``.
MLX_DEFAULT_MODELS: dict[str, str] = {
    "MLX_MAIN_MODEL": "mlx-community/Hermes-3-Llama-3.1-8B-4bit",
    "MLX_ANALYSIS_MODEL": "mlx-community/Qwen2.5-1.5B-Instruct-4bit",
    "MLX_DRAFTING_MODEL": "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit",
}


logger = logging.getLogger(__name__)

PROVIDERS = user_settings.PROVIDERS
# The three engines, in ``_build_engines`` order. Each one's provider is
# ``AGENT_PROVIDER_<ROLE>`` when set, else ``AGENT_PROVIDER`` (#26).
ENGINE_ROLES: tuple[str, ...] = providers.ENGINE_ROLES
PROVIDER_OVERRIDE_KEYS: dict[str, str] = {
    role: f"AGENT_PROVIDER_{role.upper()}" for role in ENGINE_ROLES
}


def _env(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value else default


def normalize_provider(value: str, *, source: str = "provider") -> str:
    """Return ``value`` as a known provider, ``""`` if blank.

    An unknown name becomes ``"local"`` with a warning: a typo must not
    brick startup, and it must not send data to the cloud either
    (invariant 7c), so it never falls through to a cloud default.
    """
    key = (value or "").strip().lower()
    if not key or key in PROVIDERS:
        return key
    logger.warning("Unknown %s=%r; using local", source, value)
    return "local"


def detection_role(choice: str) -> str:
    """The engine role named by ``AGENT_PATTERN_ENGINE``; unknown → main."""
    key = (choice or "main").strip().lower()
    return key if key in ENGINE_ROLES else "main"


@dataclass
class Settings:
    """Runtime settings for the agent daemon."""

    host: str = "127.0.0.1"
    port: int = 8000
    # Default inference provider for the three engines (main, analysis,
    # drafting). ``"local"`` runs MLX on Apple Silicon; any other name is
    # an OpenAI-compatible preset from ``providers.PRESETS`` (#97: groq,
    # openai, anthropic, gemini, xai, openrouter, local_server, custom).
    # Selected via
    # ``AGENT_PROVIDER``. Use :meth:`engine_provider` rather than reading
    # this directly: each engine can override it.
    provider: str = field(
        default_factory=lambda: _env("AGENT_PROVIDER", "local").lower()
    )
    # Per-engine overrides (#26): ``AGENT_PROVIDER_MAIN``,
    # ``AGENT_PROVIDER_ANALYSIS``, ``AGENT_PROVIDER_DRAFTING``. Blank means
    # "same as ``provider``". E.g. local main + analysis keeps per-event
    # traffic on-device and sends only tool drafts to Groq.
    provider_main: str = field(
        default_factory=lambda: _env("AGENT_PROVIDER_MAIN", "")
    )
    provider_analysis: str = field(
        default_factory=lambda: _env("AGENT_PROVIDER_ANALYSIS", "")
    )
    provider_drafting: str = field(
        default_factory=lambda: _env("AGENT_PROVIDER_DRAFTING", "")
    )
    # Local MLX models (Hugging Face repo ids or local paths), overridable
    # via ``MLX_MAIN_MODEL`` / ``MLX_ANALYSIS_MODEL`` / ``MLX_DRAFTING_MODEL``.
    # The drafting model is used only by the meta-tool generator to draft
    # new @tool modules, so it can be a code specialist while the
    # tool-calling agent stays on a generalist chat model.
    model_path: str = field(
        default_factory=lambda: _env(
            "MLX_MAIN_MODEL", MLX_DEFAULT_MODELS["MLX_MAIN_MODEL"]
        )
    )
    analysis_model_path: str = field(
        default_factory=lambda: _env(
            "MLX_ANALYSIS_MODEL", MLX_DEFAULT_MODELS["MLX_ANALYSIS_MODEL"]
        )
    )
    drafting_model_path: str = field(
        default_factory=lambda: _env(
            "MLX_DRAFTING_MODEL", MLX_DEFAULT_MODELS["MLX_DRAFTING_MODEL"]
        )
    )
    # Groq settings. ``groq_api_key`` falls back to the ``GROQ_API_KEY``
    # environment variable so the secret is never committed. Model ids
    # follow Groq's catalogue (https://console.groq.com/docs/models) and
    # mirror the local-engine triplet: a large model for the main
    # tool-calling loop and the meta-tool generator's drafts, and a small
    # fast model for analysis.
    groq_api_base: str = field(
        default_factory=lambda: _env(
            "GROQ_API_BASE", "https://api.groq.com/openai/v1"
        )
    )
    groq_api_key: str = field(
        default_factory=lambda: _env("GROQ_API_KEY", "")
    )
    groq_main_model: str = field(
        default_factory=lambda: _env(
            "GROQ_MAIN_MODEL", GROQ_DEFAULT_MODELS["GROQ_MAIN_MODEL"]
        )
    )
    groq_analysis_model: str = field(
        default_factory=lambda: _env(
            "GROQ_ANALYSIS_MODEL", GROQ_DEFAULT_MODELS["GROQ_ANALYSIS_MODEL"]
        )
    )
    groq_drafting_model: str = field(
        default_factory=lambda: _env(
            "GROQ_DRAFTING_MODEL", GROQ_DEFAULT_MODELS["GROQ_DRAFTING_MODEL"]
        )
    )
    # Which engine the pattern recognizer uses to spot repeated workflows
    # and write the tool spec: ``"main"`` (default), ``"analysis"`` or
    # ``"drafting"``. The 1.5B analysis model is too small to infer a
    # workflow from raw events, so detection defaults to the main model.
    # Selected via ``AGENT_PATTERN_ENGINE``.
    pattern_detection_engine: str = field(
        default_factory=lambda: _env("AGENT_PATTERN_ENGINE", "main").lower()
    )
    # Open-ended "how could I help?" idea source (LIVING_SPEC §1). On by
    # default; ``AGENT_OPEN_HELP=0`` turns it off. Asks at most once per
    # ``AGENT_OPEN_HELP_INTERVAL`` seconds and only shows ideas the judge
    # scores at or above ``AGENT_OPEN_HELP_MIN_SCORE`` (0-10).
    open_help_enabled: bool = field(
        default_factory=lambda: _env("AGENT_OPEN_HELP", "1") != "0"
    )
    open_help_interval_seconds: float = field(
        default_factory=lambda: float(_env("AGENT_OPEN_HELP_INTERVAL", "600"))
    )
    open_help_min_score: int = field(
        default_factory=lambda: int(_env("AGENT_OPEN_HELP_MIN_SCORE", "7"))
    )
    idea_feedback_path: Path = field(
        default_factory=lambda: Path.home() / ".bot-inator" / "idea_feedback.json"
    )
    watch_dirs: list[Path] = field(
        default_factory=lambda: [Path.home() / "Downloads"]
    )
    # Directory that holds AI-generated smolagents @tool modules. Loaded at
    # startup via :meth:`Orchestrator.load_dynamic_tools` and re-scanned
    # whenever the meta-tool generator installs a new file. Moved out of
    # the tree by ``AGENT_DATA_DIR`` / ``AGENT_GENERATED_TOOLS_DIR`` (#96).
    generated_tools_dir: Path = field(default_factory=default_generated_tools_dir)


    # --- provider resolution (#26) -------------------------------------
    # Resolved on each call (not cached) so tests can patch the fields.

    def default_provider(self) -> str:
        return normalize_provider(self.provider, source="AGENT_PROVIDER") or "local"

    def engine_override(self, role: str) -> str:
        """``role``'s override (a provider name), ``""`` if none."""
        return normalize_provider(
            getattr(self, f"provider_{role}"),
            source=PROVIDER_OVERRIDE_KEYS[role],
        )

    def engine_provider(self, role: str) -> str:
        """The provider ``role`` runs on: its override, else the default."""
        return self.engine_override(role) or self.default_provider()

    def engine_providers(self) -> dict[str, str]:
        return {role: self.engine_provider(role) for role in ENGINE_ROLES}

    # --- OpenAI-compatible presets (#97) ---------------------------------
    # Groq keeps its dataclass fields (tests and old settings use them);
    # the other presets read ``<PREFIX>_*`` from the environment live.

    def provider_api_key(self, provider: str) -> str:
        if provider == "groq":
            return self.groq_api_key or ""
        preset = providers.PRESETS.get(provider)
        return os.environ.get(preset.key_env(), "") if preset else ""

    def provider_api_base(self, provider: str) -> str:
        if provider == "groq":
            return self.groq_api_base
        preset = providers.PRESETS.get(provider)
        if preset is None:
            return ""
        return os.environ.get(preset.base_env(), "") or preset.api_base

    def provider_model(self, provider: str, role: str) -> str:
        if provider == "groq":
            return getattr(self, f"groq_{role}_model")
        preset = providers.PRESETS.get(provider)
        if preset is None:
            return ""
        return (
            os.environ.get(preset.model_env(role), "")
            or preset.default_models.get(role, "")
        )

    def is_cloud(self, role: str) -> bool:
        """True when ``role``'s prompts leave the Mac.

        Decided by host, not by name (#97 decision 6): a preset pointed at
        a loopback URL (Ollama, LM Studio) is local; every other base URL,
        including a blank or unparsable one, is cloud.
        """
        provider = self.engine_provider(role)
        if provider == "local":
            return False
        return not providers.is_loopback_url(self.provider_api_base(provider))

    def uses_cloud(self) -> bool:
        """True when any engine sends prompts off the machine."""
        return any(self.is_cloud(role) for role in ENGINE_ROLES)

    def detection_is_cloud(self) -> bool:
        """True when the pattern/idea/setup-scan engine is a cloud one."""
        return self.is_cloud(detection_role(self.pattern_detection_engine))

    def groq_key_required(self) -> bool:
        return any(p == "groq" for p in self.engine_providers().values())

    def engine_problems(self) -> dict[str, str]:
        """``role -> reason`` for engines that cannot start as configured:
        a missing key, base URL or model name. Empty when all can."""
        problems: dict[str, str] = {}
        for role, provider in self.engine_providers().items():
            preset = providers.PRESETS.get(provider)
            if preset is None:
                continue
            if preset.key_required and not self.provider_api_key(provider):
                problems[role] = f"{preset.label} needs an API key ({preset.key_env()})"
            elif not self.provider_api_base(provider):
                problems[role] = f"{preset.label} needs a base URL ({preset.base_env()})"
            elif not self.provider_model(provider, role):
                problems[role] = (
                    f"{preset.label} needs a {role} model ({preset.model_env(role)})"
                )
        return problems


    # --- vision (#107) ----------------------------------------------------
    # A fourth engine role, used only by ``look_at_screen``. Not in
    # ``ENGINE_ROLES``: it is never built or eager-loaded at start (the
    # model loads on first use, ai/vision_engine.py). Read live from the
    # environment so a Settings change needs no restart.

    def vision_provider(self) -> str:
        """``AGENT_VISION_PROVIDER``: ``"local"`` (default) or a preset id."""
        return normalize_provider(
            os.environ.get(VISION_PROVIDER_ENV, ""), source=VISION_PROVIDER_ENV
        ) or "local"

    def vision_local_model(self) -> str:
        """``MLX_VISION_MODEL``, else the default for this Mac's memory."""
        return os.environ.get(MLX_VISION_MODEL_ENV, "").strip() or default_vision_model()

    def vision_cloud_model(self, provider: str) -> str:
        """The chosen cloud vision model (``<PREFIX>_VISION_MODEL``), ``""``
        when none is chosen. There is no default: decision 3 needs one
        picked in Settings."""
        preset = providers.PRESETS.get(provider)
        if preset is None:
            return ""
        return os.environ.get(preset.vision_model_env(), "").strip()

    def vision_is_cloud(self) -> bool:
        """True when screenshots would go off the Mac (by host, #97 decision 6)."""
        provider = self.vision_provider()
        if provider == "local":
            return False
        return not providers.is_loopback_url(self.provider_api_base(provider))


# Vision defaults (#107). Both are MLX 4-bit Qwen3-VL builds; sizes from the
# Hugging Face cache on 2026-11-11. Measured on this Mac (wave 19): the 4B
# peaks at 4.3-4.8 GB, the 2B at 3.4 GB.
VISION_PROVIDER_ENV = "AGENT_VISION_PROVIDER"
MLX_VISION_MODEL_ENV = "MLX_VISION_MODEL"
VISION_DEFAULT_MODEL = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
VISION_SMALL_MODEL = "mlx-community/Qwen3-VL-2B-Instruct-4bit"
VISION_MODEL_BYTES: dict[str, int] = {
    VISION_DEFAULT_MODEL: 2_900_000_000,
    VISION_SMALL_MODEL: 1_700_000_000,
}
# Below this much memory the 2B is the default (#93's memory check): the
# 4B's ~4.5 GB on top of the text models leaves too little on a 16 GB Mac.
VISION_SMALL_BELOW_GB = 24

_vision_default: Optional[str] = None


def default_vision_model() -> str:
    """The 4B, or the 2B on a Mac with less than ``VISION_SMALL_BELOW_GB``."""
    global _vision_default
    if _vision_default is None:
        try:
            from ai.first_run import physical_memory_gb  # noqa: WPS433

            gb = physical_memory_gb()
        except Exception:
            gb = None
        small = gb is not None and gb < VISION_SMALL_BELOW_GB
        _vision_default = VISION_SMALL_MODEL if small else VISION_DEFAULT_MODEL
    return _vision_default


settings = Settings()
