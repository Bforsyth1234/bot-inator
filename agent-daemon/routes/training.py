"""Settings → Training data (#118): the opt-in corpus, browse and delete.

- ``GET    /api/training``                ``{enabled, keep_mail_text, locked_fields,
  count, usable, negatives, tainted, heldout}``
- ``PUT    /api/training/settings``       body ``{enabled?, keep_mail_text?,
  delete_stored?}``. Turning ``enabled`` off deletes what's stored unless
  ``delete_stored`` is false (decision 3: the default is yes). Answers like
  ``GET`` plus ``deleted``.
- ``GET    /api/training/rows?limit=50&offset=0``  ``{rows, total, limit, offset}``,
  newest first; tainted and held-out rows are flagged.
- ``DELETE /api/training/rows/{row_id}``  delete one (404 if unknown)
- ``DELETE /api/training/rows``           delete all (held-out split too)

Personal model (#119):

- ``GET    /api/training/status``  the run's status (``ai.personal_training``:
  ``state`` idle|waiting_for_idle|running|paused|done|failed|cancelled,
  ``progress`` 0..1, ``iteration``, ``total``, ``eta_s``, ``loss``,
  ``adapter_dir``, ``error``, ``reason``, ``accepted``, ``usable``,
  ``negatives``, ``min_rows``, ``min_negative``)
- ``POST   /api/training/start``   body ``{base_model?}``. 403 unless the user
  accepted the training card (the accept is used up by this start), 409 if a
  run is going, 422 when there are too few usable examples.
- ``POST   /api/training/cancel``  stop the child, remove the partial adapter

The toggles are saved through ``user_settings`` (keys
``AGENT_TRAINING_CORPUS`` and ``AGENT_TRAINING_KEEP_MAIL_TEXT``; the runner
adds them to ``ALLOWED_KEYS``) and read live by ``ai.training_corpus``.
Nothing here sends anything off the Mac. The app-wide ``AuthMiddleware``
(#27) guards every route.

Wiring (main.py): ``app.include_router(training_routes.router)`` and
``app.state.training_corpus = <ai.training_corpus.TrainingCorpus>``;
``app.state.personal_training = <ai.personal_training.PersonalTraining>``
for the #119 routes (503 without it).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import user_settings
from ai import training_corpus as tc

router = APIRouter(prefix="/api/training", tags=["training"])


class TrainingSettingsUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    enabled: Optional[bool] = None
    keep_mail_text: Optional[bool] = None
    delete_stored: bool = True


def _corpus(request: Request) -> Any:
    corpus = getattr(request.app.state, "training_corpus", None)
    if corpus is None:
        raise HTTPException(status_code=503, detail="training corpus not ready")
    return corpus


def _view(corpus: Any) -> dict[str, Any]:
    counts = corpus.usable_counts()
    locked = [
        field for field, key in (("enabled", tc.ENABLED_KEY), ("keep_mail_text", tc.KEEP_MAIL_TEXT_KEY))
        if key in user_settings.shell_exported
    ]
    return {
        "enabled": tc.opted_in(),
        "keep_mail_text": tc.keep_mail_text(),
        "locked_fields": locked,
        "count": counts["all"],
        "usable": counts["total"],
        "negatives": counts["negatives"],
        "tainted": counts["tainted"],
        "heldout": counts["heldout"],
    }


@router.get("")
def get_training(request: Request) -> dict[str, Any]:
    return _view(_corpus(request))


@router.put("/settings")
def put_settings(body: TrainingSettingsUpdate, request: Request) -> dict[str, Any]:
    corpus = _corpus(request)
    updates: dict[str, str] = {}
    if body.enabled is not None:
        # "1"/"0" (never removed) so an explicit "off" sticks.
        updates[tc.ENABLED_KEY] = "1" if body.enabled else "0"
    if body.keep_mail_text is not None:
        updates[tc.KEEP_MAIL_TEXT_KEY] = "1" if body.keep_mail_text else "0"
    if updates:
        try:
            user_settings.write(updates)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
    deleted = 0
    if body.enabled is False:
        deleted = corpus.opt_out(delete_stored=body.delete_stored)
    return {**_view(corpus), "deleted": deleted}


@router.get("/rows")
def list_rows(request: Request, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    corpus = _corpus(request)
    limit = max(1, min(int(limit), tc.MAX_LIST))
    offset = max(0, int(offset))
    return {"rows": corpus.rows(limit, offset), "total": corpus.count(),
            "limit": limit, "offset": offset}


@router.delete("/rows/{row_id}")
def delete_row(row_id: str, request: Request) -> dict[str, Any]:
    corpus = _corpus(request)
    if not corpus.delete(row_id):
        raise HTTPException(status_code=404, detail="no such row")
    return {"ok": True, "count": corpus.count()}


@router.delete("/rows")
def delete_all(request: Request) -> dict[str, Any]:
    corpus = _corpus(request)
    return {"deleted": corpus.delete_all(), "count": corpus.count()}


# ---------------------------------------------------------------------------
# Personal model run (#119)
# ---------------------------------------------------------------------------


class TrainingStart(BaseModel):
    base_model: Optional[str] = None


def _personal(request: Request) -> Any:
    trainer = getattr(request.app.state, "personal_training", None)
    if trainer is None:
        raise HTTPException(status_code=503, detail="personal training not ready")
    return trainer


@router.get("/status")
def training_status(request: Request) -> dict[str, Any]:
    return _personal(request).status()


@router.post("/start")
async def training_start(request: Request, body: Optional[TrainingStart] = None) -> dict[str, Any]:
    trainer = _personal(request)
    if trainer.runner.running:
        raise HTTPException(status_code=409, detail="a training run is already in progress")
    if not trainer.state.accepted:
        raise HTTPException(status_code=403, detail="training starts only after you accept the card")
    try:
        await trainer.start((body.base_model if body else None) or None)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return trainer.status()


@router.post("/cancel")
async def training_cancel(request: Request) -> dict[str, Any]:
    trainer = _personal(request)
    await trainer.cancel()
    return trainer.status()
