"""Daemon configuration settings."""
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

import user_settings

# Load ``agent-daemon/.env`` (resolved relative to this file, not CWD) so the
# daemon picks up ``GROQ_API_KEY`` and friends regardless of where uvicorn is
# launched from. ``override=False`` keeps real shell exports authoritative,
# which matters for tests and for the ``AGENT_PROVIDER=local`` overrides we
# sometimes pass on the launch command line.
#
# Settings saved from the MenuBar app's Settings window are applied first,
# so they win over ``.env`` but still lose to real shell exports.
user_settings.apply_to_environ()
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)


# Groq model defaults. GPT-OSS models are the general chat models in
# Groq's production tier that a developer-plan key can use; the Llama
# models are Enterprise-only and qwen3-32b is no longer listed
# (checked 2026-11-09 against https://console.groq.com/docs/models).
GROQ_DEFAULT_MODELS: dict[str, str] = {
    "GROQ_MAIN_MODEL": "openai/gpt-oss-120b",
    "GROQ_ANALYSIS_MODEL": "openai/gpt-oss-20b",
    "GROQ_DRAFTING_MODEL": "openai/gpt-oss-120b",
}


logger = logging.getLogger(__name__)

PROVIDERS = user_settings.PROVIDERS
# The three engines, in ``_build_engines`` order. Each one's provider is
# ``AGENT_PROVIDER_<ROLE>`` when set, else ``AGENT_PROVIDER`` (#26).
ENGINE_ROLES: tuple[str, ...] = ("main", "analysis", "drafting")
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
    # drafting). ``"local"`` runs MLX on Apple Silicon; ``"groq"`` calls
    # Groq's OpenAI-compatible endpoint instead. Selected via
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
    model_path: str = "mlx-community/Hermes-3-Llama-3.1-8B-4bit"
    analysis_model_path: str = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"
    # Code-specialist model used exclusively by the meta-tool generator
    # to draft new @tool modules. Kept separate from ``model_path`` so the
    # tool-calling agent can stay on a generalist chat model while drafts
    # are produced by a model trained primarily on source code.
    drafting_model_path: str = "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"
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
    # whenever the meta-tool generator installs a new file.
    generated_tools_dir: Path = field(
        default_factory=lambda: Path(__file__).resolve().parent
        / "tools"
        / "generated"
    )


    # --- provider resolution (#26) -------------------------------------
    # Resolved on each call (not cached) so tests can patch the fields.

    def default_provider(self) -> str:
        return normalize_provider(self.provider, source="AGENT_PROVIDER") or "local"

    def engine_override(self, role: str) -> str:
        """``role``'s override (``"local"``/``"groq"``), ``""`` if none."""
        return normalize_provider(
            getattr(self, f"provider_{role}"),
            source=PROVIDER_OVERRIDE_KEYS[role],
        )

    def engine_provider(self, role: str) -> str:
        """The provider ``role`` runs on: its override, else the default."""
        return self.engine_override(role) or self.default_provider()

    def engine_providers(self) -> dict[str, str]:
        return {role: self.engine_provider(role) for role in ENGINE_ROLES}

    def is_cloud(self, role: str) -> bool:
        return self.engine_provider(role) != "local"

    def uses_cloud(self) -> bool:
        """True when any engine sends prompts off the machine."""
        return any(self.is_cloud(role) for role in ENGINE_ROLES)

    def detection_is_cloud(self) -> bool:
        """True when the pattern/idea/setup-scan engine is a cloud one."""
        return self.is_cloud(detection_role(self.pattern_detection_engine))

    def groq_key_required(self) -> bool:
        return any(p == "groq" for p in self.engine_providers().values())


settings = Settings()
