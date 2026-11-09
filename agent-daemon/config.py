"""Daemon configuration settings."""
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


def _env(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value else default


@dataclass
class Settings:
    """Runtime settings for the agent daemon."""

    host: str = "127.0.0.1"
    port: int = 8000
    # Inference provider for all three engines (main, analysis, drafting).
    # ``"local"`` runs MLX on Apple Silicon; ``"groq"`` calls Groq's
    # OpenAI-compatible endpoint instead. Selected via ``AGENT_PROVIDER``.
    provider: str = field(
        default_factory=lambda: _env("AGENT_PROVIDER", "local").lower()
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


settings = Settings()
