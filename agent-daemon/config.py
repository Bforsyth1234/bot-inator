"""Daemon configuration settings."""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    """Runtime settings for the agent daemon."""

    host: str = "127.0.0.1"
    port: int = 8000
    model_path: str = "mlx-community/Hermes-3-Llama-3.1-8B-4bit"
    analysis_model_path: str = "mlx-community/Qwen2.5-1.5B-Instruct-4bit"
    watch_dirs: list[Path] = field(
        default_factory=lambda: [Path.home() / "Downloads"]
    )


settings = Settings()
