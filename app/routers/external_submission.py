"""Mark submitted from the project side menu (0140): record a proposal that
was sent outside the app, from any stage past Go/No-Go.

  GET    /projects/{id}/external-submission                    the modal's state
  GET    /projects/{id}/external-submission/pending-count      side-menu badge
  GET    /projects/{id}/external-submission/wizard             categories + prefill
  POST   /projects/{id}/external-submission                    submit (one transaction)
  POST   /projects/{id}/external-submission/undo               undo (approvers)
  POST   /projects/{id}/external-submission/requests           ask for permission
  POST   /projects/{id}/external-submission/requests/{rid}/cancel
  POST   /projects/{id}/external-submission/requests/{rid}/grant
  POST   /projects/{id}/external-submission/requests/{rid}/deny
  POST   /projects/{id}/external-submission/requests/{rid}/revoke

Every route is a writer route (the accountant and the estimator never reach
it); the approver / requester split and the grant check live in
services/external_submission.py, enforced on every write. Handlers are plain
`def` so the sync Supabase SDK runs in the threadpool.
"""

from fastapi import APIRouter, Depends, HTTPException

from app.core.deps import CurrentUser, require_writer
from app.core.ratelimit import gc_pricing_request_rate_limit, outbound_email_rate_limit
from app.models.schemas import (
    ExternalSubmissionDenyIn,
    ExternalSubmissionIn,
    ExternalSubmissionRequestIn,
    ExternalSubmissionUndoIn,
)
from app.services import external_submission as xs
from app.services.external_submission import ExternalSubmissionError

router = APIRouter(prefix="/projects/{project_id}/external-submission", tags=["external-submission"])


def _raise(exc: ExternalSubmissionError):
    headers = {"X-Error-Code": exc.code} if exc.code else None
    raise HTTPException(exc.status_code, str(exc), headers=headers) from exc


@router.get("")
def get_state(project_id: str, user: CurrentUser = Depends(require_writer)):
    try:
        return xs.overview(project_id, user.id, user.role)
    except ExternalSubmissionError as exc:
        _raise(exc)


@router.get("/pending-count")
def get_pending_count(project_id: str, user: CurrentUser = Depends(require_writer)):
    """Pending requests on the project (0 for anyone who cannot decide them):
    the side-menu badge polls this, so it stays one small read."""
    return {"count": xs.pending_count(project_id, user.role)}


@router.get("/wizard")
def get_wizard(project_id: str, user: CurrentUser = Depends(require_writer)):
    try:
        return xs.wizard_data(project_id, user.id, user.role)
    except ExternalSubmissionError as exc:
        _raise(exc)


# A submit notifies and emails every materials engineer, labor engineer and
# executive, and undo re-opens the project for another submit, so both share
# the hourly outbound-email budget.
@router.post("", dependencies=[Depends(outbound_email_rate_limit)])
def submit(
    project_id: str, body: ExternalSubmissionIn, user: CurrentUser = Depends(require_writer)
):
    try:
        return xs.submit(project_id, user.id, user.role, body)
    except ExternalSubmissionError as exc:
        _raise(exc)


@router.post("/undo", dependencies=[Depends(outbound_email_rate_limit)])
def undo(
    project_id: str, body: ExternalSubmissionUndoIn, user: CurrentUser = Depends(require_writer)
):
    try:
        return xs.undo(project_id, user.id, user.role, body.reason)
    except ExternalSubmissionError as exc:
        _raise(exc)


# A request notifies and emails every approver, so it shares the hourly
# budget of the other approval request that fans out to executives.
@router.post("/requests", dependencies=[Depends(gc_pricing_request_rate_limit)])
def create_request(
    project_id: str,
    body: ExternalSubmissionRequestIn,
    user: CurrentUser = Depends(require_writer),
):
    try:
        xs.create_request(project_id, user.id, user.role, body.message)
        return xs.overview(project_id, user.id, user.role)
    except ExternalSubmissionError as exc:
        _raise(exc)


@router.post("/requests/{request_id}/cancel")
def cancel_request(
    project_id: str, request_id: str, user: CurrentUser = Depends(require_writer)
):
    try:
        xs.cancel_request(project_id, request_id, user.id)
        return xs.overview(project_id, user.id, user.role)
    except ExternalSubmissionError as exc:
        _raise(exc)


@router.post("/requests/{request_id}/grant")
def grant_request(
    project_id: str, request_id: str, user: CurrentUser = Depends(require_writer)
):
    try:
        xs.grant_request(project_id, request_id, user.id, user.role)
        return xs.overview(project_id, user.id, user.role)
    except ExternalSubmissionError as exc:
        _raise(exc)


@router.post("/requests/{request_id}/deny")
def deny_request(
    project_id: str,
    request_id: str,
    body: ExternalSubmissionDenyIn,
    user: CurrentUser = Depends(require_writer),
):
    try:
        xs.deny_request(project_id, request_id, user.id, user.role, body.reason)
        return xs.overview(project_id, user.id, user.role)
    except ExternalSubmissionError as exc:
        _raise(exc)


@router.post("/requests/{request_id}/revoke")
def revoke_grant(
    project_id: str, request_id: str, user: CurrentUser = Depends(require_writer)
):
    try:
        xs.revoke_grant(project_id, request_id, user.id, user.role)
        return xs.overview(project_id, user.id, user.role)
    except ExternalSubmissionError as exc:
        _raise(exc)
