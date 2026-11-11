"""Provider helpers for the Settings picker (#97).

``POST /api/providers/{id}/models`` lists the models the provider offers
(its OpenAI-compatible ``GET /models``), so the user picks real ids instead
of typing them. ``POST /api/providers/{id}/test`` sends one tiny chat
request with a chosen model.

Both run only when the user clicks the button, and both send the key to
that provider's own base URL, nowhere else. A key or base URL in the body
is used for this one call and not saved (the user may test before
saving); otherwise the saved values are used. Errors come back as text
with the key removed.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import providers
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/providers", tags=["providers"])

TIMEOUT_S = 20.0
MAX_MODELS = 300


class ProviderProbe(BaseModel):
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    model: Optional[str] = None


def _resolve(preset_id: str, body: ProviderProbe) -> tuple[providers.Preset, str, str]:
    preset = providers.PRESETS.get(preset_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"unknown provider {preset_id!r}")
    key = (body.api_key or "").strip() or settings.provider_api_key(preset_id)
    base = (body.api_base or "").strip() or settings.provider_api_base(preset_id)
    if not base:
        raise HTTPException(status_code=400, detail=f"{preset.label} needs a base URL")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base URL must start with http:// or https://")
    if preset.key_required and not key:
        raise HTTPException(status_code=400, detail=f"{preset.label} needs an API key")
    return preset, key, base


def _client(key: str, base: str) -> Any:
    """An OpenAI client for ``base``. Off-Mac calls go in the privacy
    ledger (#82): chat requests through the wrapped client, the model list
    as one row here. A loopback base (Ollama, LM Studio) is not egress."""
    from openai import OpenAI  # noqa: WPS433

    from ai import egress_log  # noqa: WPS433

    client = OpenAI(api_key=key or "not-needed", base_url=base, timeout=TIMEOUT_S, max_retries=0)
    if providers.is_loopback_url(base):
        return client
    return egress_log.wrap_openai_client(client, role=None, api_base=base)


def _record_model_list(base: str, ids: list[str]) -> None:
    if providers.is_loopback_url(base):
        return
    from ai import egress_log  # noqa: WPS433

    egress_log.record(
        "provider_check", egress_log.host_of(base),
        bytes_in=sum(len(i) for i in ids), note="listed models",
    )


def _scrub(text: str, key: str) -> str:
    text = str(text)
    if key:
        text = text.replace(key, "…")
    return text[:500]


@router.post("/{preset_id}/models")
async def list_models(preset_id: str, body: ProviderProbe) -> dict[str, Any]:
    preset, key, base = _resolve(preset_id, body)

    def _call() -> list[str]:
        page = _client(key, base).models.list()
        ids = sorted({str(getattr(m, "id", "") or "") for m in page} - {""})
        _record_model_list(base, ids)
        return ids[:MAX_MODELS]

    try:
        ids = await asyncio.to_thread(_call)
    except Exception as exc:  # network, auth, a provider without /models
        logger.info("listing %s models failed: %s", preset.id, type(exc).__name__)
        raise HTTPException(status_code=502, detail=_scrub(exc, key)) from exc
    return {"provider": preset.id, "models": ids}


@router.post("/{preset_id}/test")
async def test_provider(preset_id: str, body: ProviderProbe) -> dict[str, Any]:
    preset, key, base = _resolve(preset_id, body)
    model = (body.model or "").strip() or settings.provider_model(preset_id, "main")
    if not model:
        raise HTTPException(status_code=400, detail="pick a model to test")

    def _call() -> str:
        resp = _client(key, base).chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the word ok."}],
            **{preset.token_param: 16},
        )
        try:
            return resp.choices[0].message.content or ""
        except (AttributeError, IndexError):
            return ""

    try:
        reply = await asyncio.to_thread(_call)
    except Exception as exc:
        logger.info("testing %s failed: %s", preset.id, type(exc).__name__)
        return {"ok": False, "provider": preset.id, "model": model, "error": _scrub(exc, key)}
    # A reasoning model can spend 16 tokens thinking and return no text;
    # the request still succeeded, which is what this checks.
    return {"ok": True, "provider": preset.id, "model": model, "reply": reply[:200]}
