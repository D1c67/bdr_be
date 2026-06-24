"""Win/Loss (bid outcome) — the final step. After a bid is Submitted, the PA
records what happened: G3's overall result, which GC won the job, and a per-GC
breakdown of who won and whether they went with our number. Recording from
Submitted advances the project to the terminal 'bid_outcome' stage.

Reads are any internal role; the write is PA (+ IT admin per convention)."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.core.deps import CurrentUser, require_internal, require_role
from app.core.roles import Role
from app.models.schemas import BidOutcomeIn
from app.services import outcome
from app.services.outcome import OutcomeError

router = APIRouter(prefix="/projects/{project_id}", tags=["outcome"])
_PA = require_role(Role.PA, Role.IT_ADMIN)


@router.get("/outcome")
async def get_outcome(
    project_id: str, user: CurrentUser = Depends(require_internal)
):
    return await asyncio.to_thread(outcome.outcome_overview, project_id)


@router.post("/outcome")
async def record_outcome(
    project_id: str,
    body: BidOutcomeIn,
    user: CurrentUser = Depends(_PA),
):
    try:
        return await asyncio.to_thread(
            outcome.record_outcome, project_id, user.id, body
        )
    except OutcomeError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
