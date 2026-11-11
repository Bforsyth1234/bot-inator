"""First-run engine choice (#93).

- ``GET  /api/first_run``           state, this Mac's memory, the
  recommendation, the cloud presets (#97), what leaves the Mac per choice,
  the local download size and progress.
- ``POST /api/first_run/download``  start downloading the stock local
  models. The only place a first-run download starts (decision 8c).
- ``POST /api/first_run/choice``    save the choice. The app then calls
  ``POST /api/restart``, which builds and loads the chosen engines.

Every POST is refused once first run is answered (409), so an existing
install is never changed from here (decision 7). The key in the body is
saved through ``user_settings`` (0600) and never logged or echoed.

Wiring (main.py): ``app.include_router(first_run_routes.router)``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import providers
import user_settings
from ai import first_run
from config import ENGINE_ROLES, MLX_DEFAULT_MODELS, settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/first_run", tags=["first_run"])

LOCAL_SOURCES = ("download", "folder", "server")
LEDGER_LINE = "Everything that leaves this Mac is listed in Settings → Privacy ledger."
# #31/#52: under a cloud engine, message and email text is replaced unless
# the user turns "Let cloud engines see email and message text" on.
PERSONAL_LINE = "Message and email text is replaced before it is sent, unless you turn that on in Settings."
GROQ_PLAN_NOTE = (
    "Groq's default models were checked with a Developer-plan key; whether a "
    "free-plan key can call them is not verified."
)


class ChoiceBody(BaseModel):
    choice: str
    # Local and Mixed: where the local models come from.
    local_source: Optional[str] = None
    model_folders: dict[str, str] = {}
    server_base: Optional[str] = None
    server_models: dict[str, str] = {}
    # Cloud and Mixed: the #97 preset, the user's own key, model per role.
    provider: Optional[str] = None
    api_key: Optional[str] = None
    models: dict[str, str] = {}


def _local_models() -> dict[str, str]:
    return {
        "main": settings.model_path,
        "analysis": settings.analysis_model_path,
        "drafting": settings.drafting_model_path,
    }


def _roles(choice: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(local roles, cloud roles) for a choice."""
    if choice == "local":
        return ENGINE_ROLES, ()
    if choice == "cloud":
        return (), ENGINE_ROLES
    return ("main", "analysis"), ("drafting",)


def _cloud_presets() -> list[dict[str, Any]]:
    return [
        {
            "id": p.id, "label": p.label, "tested": p.tested,
            "key_required": p.key_required, "api_base": p.api_base,
            "default_models": dict(p.default_models), "docs_url": p.docs_url,
            "note": GROQ_PLAN_NOTE if p.id == "groq" else "",
        }
        for p in providers.PRESETS.values()
        if p.api_base and not providers.is_loopback_url(p.api_base)
    ]


def _view() -> dict[str, Any]:
    gb = first_run.physical_memory_gb()
    models = _local_models()
    download = first_run.local_download_bytes(list(models.values()))
    return {
        "pending": first_run.is_pending(),
        "memory_gb": None if gb is None else round(gb, 1),
        "recommended": first_run.recommend(gb),
        "cloud_below_gb": first_run.CLOUD_BELOW_GB,
        "choices": [
            {"id": "local", "title": "Local",
             "leaves_mac": "Nothing: every engine runs on this Mac."},
            {"id": "cloud", "title": "Cloud key",
             "leaves_mac": "Event summaries, chat and tool drafts go to the provider you pick. "
                           + PERSONAL_LINE},
            {"id": "mixed", "title": "Mixed",
             "leaves_mac": "Only tool drafts go to the provider you pick: the tool you asked "
                           "for and its code. " + PERSONAL_LINE},
        ],
        "ledger_line": LEDGER_LINE,
        "presets": _cloud_presets(),
        "local": {
            "models": models,
            "stock": dict(MLX_DEFAULT_MODELS),
            "downloaded": {r: first_run.is_downloaded(m) for r, m in models.items()},
            "download": download,
            "server_base": providers.PRESETS["local_server"].api_base,
        },
        "download": first_run.downloader.state(),
    }


def _require_pending() -> None:
    if not first_run.is_pending():
        raise HTTPException(status_code=409, detail="first run is already answered")


@router.get("")
async def get_first_run() -> dict[str, Any]:
    return await asyncio.to_thread(_view)


@router.post("/download")
async def start_download() -> dict[str, Any]:
    _require_pending()
    first_run.downloader.start(list(_local_models().values()))
    return first_run.downloader.state()


def _local_updates(body: ChoiceBody, roles: tuple[str, ...]) -> tuple[dict[str, str], str]:
    """Settings for the local roles, and the provider they run on."""
    source = (body.local_source or "").strip().lower()
    if source not in LOCAL_SOURCES:
        raise HTTPException(status_code=400, detail="choose where the local models come from")
    updates: dict[str, str] = {}
    if source == "download":
        models = _local_models()
        missing = [r for r in roles if not first_run.is_downloaded(models[r])]
        if missing:
            raise HTTPException(
                status_code=400,
                detail="Download the models first (the Download button): "
                       + ", ".join(missing) + " not downloaded yet.",
            )
        return updates, "local"
    if source == "folder":
        for role in roles:
            path = (body.model_folders.get(role) or "").strip()
            problem = first_run.check_model_folder(path)
            if problem:
                raise HTTPException(status_code=400, detail=f"{role} model: {problem}")
            updates[f"MLX_{role.upper()}_MODEL"] = path
        return updates, "local"
    preset = providers.PRESETS["local_server"]
    base = (body.server_base or "").strip() or preset.api_base
    if not providers.is_loopback_url(base):
        raise HTTPException(
            status_code=400,
            detail="A local server must be on this Mac (localhost); use Cloud key for anything else.",
        )
    if base.rstrip("/") != preset.api_base.rstrip("/"):
        updates[preset.base_env()] = base
    for role in roles:
        model = (body.server_models.get(role) or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail=f"pick the server's {role} model")
        updates[preset.model_env(role)] = model
    return updates, "local_server"


def _cloud_updates(body: ChoiceBody, roles: tuple[str, ...]) -> tuple[dict[str, str], str]:
    pid = (body.provider or "").strip().lower()
    preset = providers.PRESETS.get(pid)
    if preset is None or not preset.api_base or providers.is_loopback_url(preset.api_base):
        raise HTTPException(status_code=400, detail="pick a cloud provider")
    key = (body.api_key or "").strip()
    if preset.key_required and not key:
        raise HTTPException(status_code=400, detail=f"{preset.label} needs your API key")
    updates = {preset.key_env(): key} if key else {}
    for role in roles:
        model = (body.models.get(role) or "").strip()
        default = preset.default_models.get(role, "")
        if not (model or default):
            raise HTTPException(status_code=400, detail=f"pick a {role} model for {preset.label}")
        if model and model != default:  # don't pin a default (as Settings)
            updates[preset.model_env(role)] = model
    return updates, pid


def choice_updates(body: ChoiceBody) -> dict[str, str]:
    """The settings a first-run choice saves. Raises 400 on a bad choice."""
    choice = (body.choice or "").strip().lower()
    if choice not in first_run.CHOICES:
        raise HTTPException(status_code=400, detail="choice must be local, cloud or mixed")
    local_roles, cloud_roles = _roles(choice)
    updates: dict[str, str] = {first_run.CHOICE_KEY: choice}
    if local_roles:
        local, local_provider = _local_updates(body, local_roles)
        updates.update(local)
        updates["AGENT_PROVIDER"] = local_provider
    if cloud_roles:
        cloud, cloud_provider = _cloud_updates(body, cloud_roles)
        updates.update(cloud)
        if local_roles:
            for role in cloud_roles:
                updates[f"AGENT_PROVIDER_{role.upper()}"] = cloud_provider
        else:
            updates["AGENT_PROVIDER"] = cloud_provider
    return updates


@router.post("/choice")
async def post_choice(body: ChoiceBody) -> dict[str, Any]:
    _require_pending()
    updates = choice_updates(body)
    try:
        await asyncio.to_thread(user_settings.write, updates)
    except (OSError, ValueError) as exc:
        logger.error("Saving the first-run choice failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="could not save settings") from exc
    logger.info("First run answered: %s (provider %s)", updates[first_run.CHOICE_KEY],
                updates.get("AGENT_PROVIDER"))
    # Engines are built at startup; the app restarts the daemon next.
    return {"ok": True, "choice": updates[first_run.CHOICE_KEY], "restart_required": True}
