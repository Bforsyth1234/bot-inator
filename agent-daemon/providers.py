"""Inference providers (#97): local MLX plus OpenAI-compatible presets.

Every provider except ``local`` speaks the OpenAI chat-completions format,
so one engine (:class:`ai.groq_engine.OpenAICompatibleEngine`) and one
agent-loop model (smolagents ``OpenAIServerModel``) serve all of them. A
preset only fills in the base URL, the settings keys and, where known, the
default model names.

Base URLs were looked up on 2026-11-10 in each provider's docs:
OpenAI https://developers.openai.com/api/docs/models ·
Gemini https://ai.google.dev/gemini-api/docs/openai ·
xAI https://docs.x.ai/developers/models ·
Anthropic https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk ·
Groq https://console.groq.com/docs/models.
Default models are set only where they were confirmed; the others are
blank and the user picks from the provider's own ``/models`` list in
Settings. Nothing here is written from memory.

``tested`` is True only for a preset with a recorded live run (chat turn,
tool call with a card, judge call, tool draft). The picker labels the
others "untested".

Privacy is decided by host, never by name (#97 decision 6): any base URL
that is not loopback is a cloud engine.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlparse

ENGINE_ROLES: tuple[str, ...] = ("main", "analysis", "drafting")


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    prefix: str  # settings-key prefix: <prefix>_API_KEY, <prefix>_MAIN_MODEL, ...
    api_base: str
    default_models: dict[str, str] = field(default_factory=dict)
    key_required: bool = True
    # The OpenAI API rejects ``max_tokens`` on its reasoning models and
    # wants ``max_completion_tokens``; other providers take ``max_tokens``.
    token_param: str = "max_tokens"
    tested: bool = False
    docs_url: str = ""

    def key_env(self) -> str:
        return f"{self.prefix}_API_KEY"

    def base_env(self) -> str:
        return f"{self.prefix}_API_BASE"

    def model_env(self, role: str) -> str:
        return f"{self.prefix}_{role.upper()}_MODEL"

    def vision_model_env(self) -> str:
        """The cloud vision model for ``look_at_screen`` (#107). Not an
        engine role: blank means "no vision model chosen", and then no
        screenshot goes to this provider."""
        return f"{self.prefix}_VISION_MODEL"

    def env_keys(self) -> tuple[str, ...]:
        return (
            self.key_env(), self.base_env(),
            *(self.model_env(r) for r in ENGINE_ROLES),
        )


PRESETS: dict[str, Preset] = {p.id: p for p in (
    Preset(
        "groq", "Groq", "GROQ", "https://api.groq.com/openai/v1",
        # Checked 2026-11-09 (config.py history): GPT-OSS is what a
        # developer-plan key can call.
        {"main": "openai/gpt-oss-120b", "analysis": "openai/gpt-oss-20b",
         "drafting": "openai/gpt-oss-120b"},
        tested=True, docs_url="https://console.groq.com/docs/models",
    ),
    Preset(
        "openai", "OpenAI", "OPENAI", "https://api.openai.com/v1",
        token_param="max_completion_tokens",
        docs_url="https://developers.openai.com/api/docs/models",
    ),
    Preset(
        "anthropic", "Anthropic (Claude)", "ANTHROPIC",
        "https://api.anthropic.com/v1/",
        {"main": "claude-sonnet-5-5", "analysis": "claude-haiku-5-5",
         "drafting": "claude-sonnet-5-5"},
        docs_url="https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk",
    ),
    Preset(
        "gemini", "Google Gemini", "GEMINI",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
    ),
    Preset(
        "xai", "xAI (Grok)", "XAI", "https://api.x.ai/v1",
        docs_url="https://docs.x.ai/developers/models",
    ),
    Preset(
        "openrouter", "OpenRouter", "OPENROUTER", "https://openrouter.ai/api/v1",
        docs_url="https://openrouter.ai/models",
    ),
    Preset(
        "local_server", "Local server (Ollama, LM Studio)", "LOCAL_SERVER",
        "http://localhost:11434/v1", key_required=False,
    ),
    Preset("custom", "Custom (OpenAI-compatible)", "CUSTOM", ""),
)}

PROVIDERS: tuple[str, ...] = ("local", *PRESETS)


def preset_env_keys() -> frozenset[str]:
    return frozenset(k for p in PRESETS.values() for k in p.env_keys())


def vision_env_keys() -> frozenset[str]:
    """``<PREFIX>_VISION_MODEL`` for every preset (#107)."""
    return frozenset(p.vision_model_env() for p in PRESETS.values())


def is_loopback_url(url: str) -> bool:
    """True when ``url`` points at this Mac (localhost, 127/8, ::1).

    Anything unparsable or blank is NOT loopback, so it counts as cloud:
    an unclear destination must never be treated as local (7c).
    """
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
