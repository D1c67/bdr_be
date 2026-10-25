"""Auto send (docs/AUTO_SEND.md section 8).

  GET   /auto-send/settings                     the switch + markup (any internal role)
  PATCH /auto-send/settings/enabled             on/off (IT Admin only)
  PATCH /auto-send/settings/markup              markup % (Executive, IT Admin)
  GET   /projects/{id}/auto-send                the project's Auto send panel
  POST  /projects/{id}/auto-send/send-now       push a missed / failed / partial run
  POST  /projects/{id}/auto-send/accept         record the auto send as the bid

The panel read is open to every internal role (the accountant reads, the
estimator never reaches it). Send now and Accept are AUTO_SEND_ACTION_ROLES
(Estimating Admin, Executive, IT Admin). Mounted with the Bidding flag in
main.py. Handlers are plain `def` so the sync Supabase SDK and the Graph
sends run in the threadpool.
"""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, require_internal, require_role
from app.core.ratelimit import outbound_email_rate_limit
from app.core.revision_context import require_revision_context
from app.core.roles import AUTO_SEND_ACTION_ROLES, AUTO_SEND_PCT_ROLES, Role
from app.services import auto_send
from app.services.auto_send import AutoSendError

router = APIRouter(tags=["auto-send"])

require_auto_send_switch = require_role(Role.IT_ADMIN)
require_auto_send_pct = require_role(*AUTO_SEND_PCT_ROLES)
require_auto_send_action = require_role(*AUTO_SEND_ACTION_ROLES)


class AutoSendEnabledIn(BaseModel):
    enabled: bool


class AutoSendMarkupIn(BaseModel):
    markup_pct: Decimal = Field(ge=0, le=auto_send.MAX_MARKUP_PCT)


class AutoSendNowIn(BaseModel):
    force_unknown: bool = False


class AutoSendAcceptIn(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)


def _raise(exc: AutoSendError):
    headers = {"X-Error-Code": exc.code} if exc.code else None
    raise HTTPException(exc.status_code, str(exc), headers=headers) from exc


@router.get("/auto-send/settings")
def get_auto_send_settings(user: CurrentUser = Depends(require_internal)) -> dict:
    return auto_send.settings_summary(user)


@router.patch("/auto-send/settings/enabled")
def patch_enabled(
    body: AutoSendEnabledIn, user: CurrentUser = Depends(require_auto_send_switch)
) -> dict:
    auto_send.set_enabled(body.enabled, user.id)
    return auto_send.settings_summary(user)


@router.patch("/auto-send/settings/markup")
def patch_markup(
    body: AutoSendMarkupIn, user: CurrentUser = Depends(require_auto_send_pct)
) -> dict:
    try:
        auto_send.set_markup_pct(body.markup_pct, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return auto_send.settings_summary(user)


@router.get("/projects/{project_id}/auto-send")
def get_project_auto_send(
    project_id: str, user: CurrentUser = Depends(require_internal)
) -> dict:
    try:
        return auto_send.project_summary(project_id, user)
    except AutoSendError as exc:
        _raise(exc)


# Send now emails every GC left on the run, so it shares the hourly
# outbound-email budget.
@router.post(
    "/projects/{project_id}/auto-send/send-now",
    dependencies=[Depends(require_revision_context), Depends(outbound_email_rate_limit)],
)
def post_send_now(
    project_id: str,
    body: AutoSendNowIn | None = None,
    user: CurrentUser = Depends(require_auto_send_action),
) -> dict:
    try:
        auto_send.send_now(
            project_id, user.id, force_unknown=bool(body and body.force_unknown)
        )
        return auto_send.project_summary(project_id, user)
    except AutoSendError as exc:
        _raise(exc)


@router.post("/projects/{project_id}/auto-send/accept", dependencies=[Depends(require_revision_context)])
def post_accept(
    project_id: str,
    body: AutoSendAcceptIn,
    user: CurrentUser = Depends(require_auto_send_action),
) -> dict:
    try:
        auto_send.accept_as_submitted(project_id, body.run_id, user.id)
        return auto_send.project_summary(project_id, user)
    except AutoSendError as exc:
        _raise(exc)
