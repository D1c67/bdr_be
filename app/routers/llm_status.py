"""AI model status — is the active LLM pool connected and serving?

Two reads of the same snapshot (see services/llm_health, which owns the probing,
the grading and the cache):

  GET  /model-status          cached; re-probes only when the snapshot is stale
  POST /model-status/check    forced re-probe ("Check now" in the modal)

On the shared spine, not behind a sub-app flag: the AI features it reports on
span Bidding (BOQ, proposals, quotes) and PM (email matching, submittal
aliases), so the indicator must not disappear with any one module.

Readable by any internal role including the read-only accountant — a stale AI
answer misleads whoever reads it, so everyone benefits from seeing the model is
down. The external estimator is excluded (require_internal); nothing here is
part of their portal.

Both handlers are plain `def` so FastAPI runs them in the threadpool: the probe
uses the SYNC provider SDKs, and the app's rule is that sync SDK work never
happens inside `async def` (see app/services/llm_health.polling_loop).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import CurrentUser, get_current_user, require_internal
from app.core.ratelimit import model_status_rate_limit
from app.core.roles import Role
from app.services import llm_health, llm_target

router = APIRouter(prefix="/model-status", tags=["meta"])


@router.get("")
def get_model_status(_: CurrentUser = Depends(require_internal)) -> dict:
    """The current snapshot. Cheap: normally the poller's cached result."""
    return llm_health.cached(get_settings()).to_dict()


@router.post("/check", dependencies=[Depends(model_status_rate_limit)])
def force_check(_: CurrentUser = Depends(require_internal)) -> dict:
    """Re-probe now, bypassing the cache — the modal's "Check now" button.

    Rate-limited per account (scope `model_status`): each call reaches out to
    the provider, and the result is shared, so there is nothing to gain from
    hammering it.
    """
    return llm_health.cached(get_settings(), force=True).to_dict()


# ── Live box switch (Settings > AI model server) ────────────────────────────
#
# Which self-hosted EC2 box serves every AI feature, switchable without a
# redeploy (services/llm_target). IT Admins and dev accounts only: a switch
# moves every AI feature for every user at once.


async def require_model_admin(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not (user.role == Role.IT_ADMIN or user.is_dev):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "IT Admin or dev account required")
    return user


class TargetSwitchIn(BaseModel):
    # None = clear the override and go back to the Railway default.
    target: str | None = Field(default=None, max_length=16)
    force: bool = False


@router.get("/target", dependencies=[Depends(model_status_rate_limit)])
def get_target(_: CurrentUser = Depends(require_model_admin)) -> dict:
    """The boxes the API knows about, which one is live, and recent switches."""
    llm_target.refresh()
    return llm_target.state()


@router.put("/target", dependencies=[Depends(model_status_rate_limit)])
def put_target(
    body: TargetSwitchIn, user: CurrentUser = Depends(require_model_admin)
) -> dict:
    """Switch the live box. Probes it first: an unhealthy box answers
    {ok: false, probe} unless `force` (the UI's "Switch anyway")."""
    try:
        return llm_target.switch(body.target, user_id=user.id, force=body.force)
    except llm_target.TargetUnavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
