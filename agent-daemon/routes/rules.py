"""Standing rules routes (#78). Creating, enabling, disabling or deleting a
rule is a USER action through these routes only; no tool the model can call
reaches them, and there is no route that takes a rule spec: a rule can only
come from an offer the daemon computed from the decision log, and accepting
re-checks every limit (``ai.standing_rules``).

- ``GET    /api/rules``                    ``{rules, offers, runs}``: rules with
  on/off and evidence, pending "always do this?" offers, and the last
  automatic runs (History entries with ``source="rule"``)
- ``POST   /api/rules/offers/{id}/accept`` the user's yes -> the new rule
  (404 unknown offer, 409 no longer eligible)
- ``POST   /api/rules/offers/{id}/dismiss`` "No thanks"
- ``PATCH  /api/rules/{id}``               ``{"enabled": bool}`` only
- ``DELETE /api/rules/{id}``
- ``POST   /api/rules/runs/{entry_id}/undo`` undo one automatic run (the
  journal's undo; two undos switch the rule off via its undo listener)

The app-wide ``AuthMiddleware`` (#27) guards every route.

Wiring (main.py): ``app.include_router(rules.router)``;
``app.state.standing_rules = <ai.standing_rules.StandingRules>``;
``app.state.approval_decisions`` and ``app.state.action_journal`` (already set).
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ai.standing_rules import RuleError

router = APIRouter(prefix="/api/rules", tags=["rules"])

MAX_RUNS_SHOWN = 30


class EnableBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _rules(request: Request) -> Any:
    store = getattr(request.app.state, "standing_rules", None)
    if store is None:
        raise HTTPException(status_code=503, detail="standing rules not ready")
    return store


def _fail(exc: RuleError) -> HTTPException:
    return HTTPException(status_code=exc.code, detail=str(exc))


@router.get("")
async def list_rules(request: Request) -> dict[str, Any]:
    store = _rules(request)
    journal = getattr(request.app.state, "action_journal", None)
    runs: list[dict[str, Any]] = []
    if journal is not None:
        rows = await asyncio.to_thread(journal.rows, 500)
        runs = [r for r in rows if r.get("source") == "rule"][:MAX_RUNS_SHOWN]
    return {"rules": store.list_rules(), "offers": store.pending_offers(), "runs": runs}


@router.post("/offers/{offer_id}/accept")
async def accept_offer(offer_id: str, request: Request) -> dict[str, Any]:
    store = _rules(request)
    decisions = getattr(request.app.state, "approval_decisions", None)
    try:
        return await asyncio.to_thread(store.accept, offer_id, decisions)
    except RuleError as exc:
        raise _fail(exc) from exc


@router.post("/offers/{offer_id}/dismiss")
async def dismiss_offer(offer_id: str, request: Request) -> dict[str, Any]:
    try:
        return _rules(request).dismiss(offer_id)
    except RuleError as exc:
        raise _fail(exc) from exc


@router.patch("/{rule_id}")
async def update_rule(rule_id: str, body: EnableBody, request: Request) -> dict[str, Any]:
    try:
        return _rules(request).set_enabled(rule_id, body.enabled)
    except RuleError as exc:
        raise _fail(exc) from exc


@router.delete("/{rule_id}")
async def delete_rule(rule_id: str, request: Request) -> dict[str, Any]:
    if not _rules(request).delete(rule_id):
        raise HTTPException(status_code=404, detail="no such rule")
    return {"deleted": rule_id}


@router.post("/runs/{entry_id}/undo")
async def undo_run(entry_id: str, request: Request) -> JSONResponse:
    journal = getattr(request.app.state, "action_journal", None)
    if journal is None:
        raise HTTPException(status_code=503, detail="action journal not ready")
    entry = journal.get(entry_id)
    if entry is None or entry.get("source") != "rule":
        raise HTTPException(status_code=404, detail="no such automatic run")
    result = await asyncio.to_thread(journal.undo, entry_id)
    code = {"undone": 200, "not_found": 404}.get(result["status"], 409)
    return JSONResponse(result, status_code=code)
