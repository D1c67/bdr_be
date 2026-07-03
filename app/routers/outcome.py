"""Win/Loss (bid outcome) — the final step. After a bid is Submitted, the
outcome is recorded: G3's overall result, which GC won the job, and a per-GC
breakdown of who won and whether they went with our number. Recording from
Submitted advances the project to the terminal 'bid_outcome' stage.

Reads are any internal role (incl. the read-only accountant); the write is any
writer role."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from app.core.deps import CurrentUser, require_internal, require_writer
from app.models.schemas import BidOutcomeIn
from app.services import outcome
from app.services.outcome import OutcomeError

router = APIRouter(prefix="/projects/{project_id}", tags=["outcome"])


@router.get("/outcome")
async def get_outcome(
    project_id: str, user: CurrentUser = Depends(require_internal)
):
    return await asyncio.to_thread(outcome.outcome_overview, project_id)


@router.post("/outcome")
async def record_outcome(
    project_id: str,
    body: BidOutcomeIn,
    user: CurrentUser = Depends(require_writer),
):
    try:
        return await asyncio.to_thread(
            outcome.record_outcome, project_id, user.id, body
        )
    except OutcomeError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
