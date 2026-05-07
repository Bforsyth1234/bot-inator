"""Daemon configuration settings."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load ``agent-daemon/.env`` (resolved relative to this file, not CWD) so the
# daemon picks up ``GROQ_API_KEY`` and friends regardless of where uvicorn is
# launched from. ``override=False`` keeps real shell exports authoritative,
# which matters for tests and for the ``AGENT_PROVIDER=local`` overrides we
# sometimes pass on the launch command line.
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)


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
    # mirror the local-engine triplet: a generalist chat model for the
    # main tool-calling loop, a fast analysis model, and a code-specialist
    # for the meta-tool generator's drafts.
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
            "GROQ_MAIN_MODEL", "llama-3.3-70b-versatile"
        )
    )
    groq_analysis_model: str = field(
        default_factory=lambda: _env(
            "GROQ_ANALYSIS_MODEL", "llama-3.1-8b-instant"
        )
    )
    groq_drafting_model: str = field(
        default_factory=lambda: _env(
            "GROQ_DRAFTING_MODEL", "qwen/qwen3-32b"
        )
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
