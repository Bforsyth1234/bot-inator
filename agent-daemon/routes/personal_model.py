"""Settings → Personal model (#120): the trained adapters, which is in use,
and one-click back to the base model.

- ``GET  /api/personal_model``           ``{adapters: [{n, created_at,
  base_model, rows, verdict, card_lines, stale, usable, reason, ...}],
  active, active_path, loaded_adapter, engine_ok, reason, current_base}``
- ``POST /api/personal_model/use``       body ``{n}``: switch to adapter
  ``n``. 404 unknown, 403 not usable (not gated as a win, or stale), 409
  when the main engine can't carry it (e.g. a cloud engine).
- ``POST /api/personal_model/use_base``  back to the base model (always).

The button in Settings is the user's own click, so it needs no card; the
after-training switch offer is a normal approval card raised by the
orchestrator. The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(personal_model_routes.router)`` and
``app.state.personal_model = <ai.model_eval.PersonalModel>`` (503 without).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/personal_model", tags=["personal_model"])


class UseAdapter(BaseModel):
    n: int


def _pm(request: Request) -> Any:
    pm = getattr(request.app.state, "personal_model", None)
    if pm is None:
        raise HTTPException(status_code=503, detail="personal model not ready")
    return pm


@router.get("")
def get_personal_model(request: Request) -> dict[str, Any]:
    return _pm(request).status()


@router.post("/use")
async def use_adapter(body: UseAdapter, request: Request) -> dict[str, Any]:
    pm = _pm(request)
    try:
        return await pm.use_adapter(body.n)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no such personal model") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/use_base")
async def use_base(request: Request) -> dict[str, Any]:
    return await _pm(request).use_base()
