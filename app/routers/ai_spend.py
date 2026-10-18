"""AI Estimating spend cap (docs/PLANS_READER.md section 6, services/ai_spend).

  GET   /ai-spend          today's spend against the cap (any internal role)
  PATCH /ai-spend/cap      change the daily cap (IT Admin, Executive)
  POST  /ai-spend/reset    start a fresh window now (IT Admin, Executive)

The read is open to every internal role, read-only accountant included,
because the AI Estimating page shows the remaining allowance to whoever is
about to start a run. The external estimator never reaches it
(require_internal). Only IT Admins and Executives can move money: they are the
two roles the owner named, and the two roles the cap-hit email goes to.

On the shared spine, not behind a sub-app flag, so the Settings card keeps
working when Bidding is switched off. Gated on AI Estimating's own flag
instead (require_plans_reader): while PLANS_READER_ENABLED is false every
route 404s before auth, the same as /ai-estimating, and the frontend card
hides itself on that 404. AI_ESTIMATING_STATE narrows it the same way as the
page (require_ai_estimating): in test only Executives and IT Admins reach it,
in dev nobody does. Self-hosted mode (FULL_SELF_HOSTED_LLMS_ENABLED)
changes nothing here: AI Estimating stays on OpenAI in both modes, so the
cap keeps guarding real spend.

GET stays open to every internal role on purpose: the AI Estimating page,
which every internal role can open, shows the same payload (the frontend's
AiEstimatingStatus.spend from GET /ai-estimating/status), so narrowing this
route would hide nothing. docs/PLANS_READER.md section 6 still says "for IT
Admin and Executive"; only the two writes are. Handlers are plain `def` (sync Supabase SDK in the
threadpool, see docs on event-loop blocking).
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, require_ai_estimating, require_internal, require_role
from app.core.features import require_plans_reader
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.services import ai_spend

router = APIRouter(
    prefix="/ai-spend",
    tags=["ai-spend"],
    dependencies=[Depends(require_plans_reader), Depends(require_ai_estimating)],
)

require_spend_admin = require_role(Role.IT_ADMIN, Role.EXECUTIVE)


class SpendStatusOut(BaseModel):
    cap_usd: float
    spent_usd: float
    remaining_usd: float
    window_started_at: str
    cap_hit: bool
    # cap_hit, or a step refused this window because its estimate would
    # pass the cap (services/ai_spend.status).
    blocked: bool = False
    cap_hit_notified_at: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None


class CapIn(BaseModel):
    cap_usd: Decimal = Field(ge=ai_spend.MIN_CAP_USD, le=ai_spend.MAX_CAP_USD)


@router.get("", response_model=SpendStatusOut)
def get_spend(_: CurrentUser = Depends(require_internal)) -> dict:
    """Spend in the current window, the cap and what is left."""
    return ai_spend.status(get_supabase())


@router.patch("/cap", response_model=SpendStatusOut)
def patch_cap(body: CapIn, user: CurrentUser = Depends(require_spend_admin)) -> dict:
    """Change the daily cap (1 to 10,000 dollars)."""
    try:
        return ai_spend.set_cap(get_supabase(), cap_usd=body.cap_usd, actor_id=user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.post("/reset", response_model=SpendStatusOut)
def post_reset(user: CurrentUser = Depends(require_spend_admin)) -> dict:
    """Start a fresh window now; spend counts from zero until Pacific midnight."""
    return ai_spend.reset_window(get_supabase(), actor_id=user.id)
