"""Revise proposal (migration 0178, docs/PROPOSAL_REVISIONS.md): after the
bid was submitted, revise the numbers and send each GC a Revised Proposal.

Reads are any internal role (the read-only accountant included); writes are
the roles that send proposals today (require_writer, the Send Out guard), so
the estimator and the accountant cannot. Every write path acts on the OPEN
revision named in the URL: a stale tab acting on a revision that was
completed or cancelled meanwhile gets a 409, never a write to another one.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, require_internal, require_role, require_writer
from app.core.ratelimit import outbound_email_rate_limit
from app.core.roles import ACTUAL_BID_VIEWER_ROLES, VERIFY_ROLES
from app.models.schemas import (
    ProjectUpdate,
    ProposalRevisionGcsIn,
    ProposalRevisionGenerateIn,
    ProposalRevisionMarkIn,
    ProposalRevisionSendIn,
    ProposalRevisionStartIn,
    VerifyOverrideIn,
)
from app.services import office_preview, proposal_revisions
from app.services.proposal_send import ProposalSendError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/proposal-revisions", tags=["proposals"])

_ACTUAL_KEYS = ("previous_actual_bid_at", "new_actual_bid_at")


def _http(exc: ProposalSendError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


def _redact(payload: dict, role) -> dict:
    """The confidential actual bid date never leaves the server for a role
    that may not see it (same rule as the project wire)."""
    if role in ACTUAL_BID_VIEWER_ROLES:
        return payload
    for rev in [payload.get("open"), *payload.get("history", [])]:
        if rev:
            for key in _ACTUAL_KEYS:
                rev[key] = None
    return payload


def _overview(project_id: str, role) -> dict:
    try:
        return _redact(proposal_revisions.overview(project_id), role)
    except ProposalSendError as exc:
        raise _http(exc) from exc


@router.get("")
def get_revisions(project_id: str, user: CurrentUser = Depends(require_internal)):
    """The open revision (per-GC rows, documents, statuses, GCs added during
    it) and the history of past revisions."""
    return _overview(project_id, user.role)


@router.post("", status_code=status.HTTP_201_CREATED)
def start_revision(
    project_id: str,
    body: ProposalRevisionStartIn,
    user: CurrentUser = Depends(require_writer),
):
    """Open Rev N, then apply the optional new bid dates through the normal
    project PATCH (its role rule, outbound email budget and bid date change
    email included). The revision is created FIRST, so a refused start never
    moves a date or emails anyone. A refused date change leaves the revision
    open and is reported in `bid_date_error`."""
    from app.routers import projects as projects_router

    patch = body.model_dump(include={"internal_bid_at", "actual_bid_at"}, exclude_none=True)
    try:
        rev = proposal_revisions.start_revision(
            project_id,
            user.id,
            body.reason,
            new_bid_dates={k: v.isoformat() for k, v in patch.items()},
        )
    except ProposalSendError as exc:
        raise _http(exc) from exc
    bid_date_error = None
    if patch:
        try:
            projects_router.update_project(project_id, ProjectUpdate(**patch), user)
        except HTTPException as exc:
            proposal_revisions.clear_requested_bid_dates(rev["id"])
            bid_date_error = (
                f"The revision is open, but the bid date was not changed: {exc.detail}"
            )
        except Exception:  # noqa: BLE001 - the revision stands; say what failed
            logger.exception("revision start: bid date change failed for %s", project_id)
            proposal_revisions.clear_requested_bid_dates(rev["id"])
            bid_date_error = (
                "The revision is open, but the bid date could not be changed. Change it "
                "in the project details."
            )
    return {**_overview(project_id, user.role), "bid_date_error": bid_date_error}


@router.put("/{revision_id}/gcs")
def set_revision_gcs(
    project_id: str,
    revision_id: str,
    body: ProposalRevisionGcsIn,
    user: CurrentUser = Depends(require_writer),
):
    """Check / uncheck the GCs that get this revision."""
    try:
        proposal_revisions.set_included_gcs(project_id, revision_id, user.id, body.gc_ids)
    except ProposalSendError as exc:
        raise _http(exc) from exc
    return _overview(project_id, user.role)


@router.post("/{revision_id}/generate")
def generate_revision(
    project_id: str,
    revision_id: str,
    background: BackgroundTasks,
    body: ProposalRevisionGenerateIn | None = None,
    user: CurrentUser = Depends(require_writer),
):
    """Render the revised documents (refused while the bid waits on the
    Executive's re-verify)."""
    try:
        created = proposal_revisions.generate_revision_documents(
            project_id, revision_id, user.id, gc_ids=body.gc_ids if body else None
        )
    except ProposalSendError as exc:
        raise _http(exc) from exc
    for row in created:
        file = row.pop("_file", None)
        if file and office_preview.is_convertible(file["filename"], "proposal"):
            background.add_task(office_preview.generate_preview, file["id"])
    return _overview(project_id, user.role)


@router.post("/{revision_id}/send", dependencies=[Depends(outbound_email_rate_limit)])
def send_revision(
    project_id: str,
    revision_id: str,
    body: ProposalRevisionSendIn,
    user: CurrentUser = Depends(require_writer),
):
    """Email each listed GC its revised document (editable subject and
    body, chosen contacts and CC, extra files)."""
    try:
        out = proposal_revisions.send_revision_documents(
            project_id,
            revision_id,
            user.id,
            body.proposal_ids,
            subject=body.subject,
            email_body=body.email_body,
            force=body.force,
            contacts=body.contacts,
            extra_file_ids=body.extra_attachment_file_ids,
            cc=body.cc,
        )
    except ProposalSendError as exc:
        raise _http(exc) from exc
    return {**out, "overview": _overview(project_id, user.role)}


@router.post("/{revision_id}/mark-submitted")
def mark_revision_submitted(
    project_id: str,
    revision_id: str,
    body: ProposalRevisionMarkIn,
    user: CurrentUser = Depends(require_writer),
):
    """Record revised proposals as submitted outside the app (no email)."""
    try:
        out = proposal_revisions.mark_revision_submitted(
            project_id, revision_id, user.id, body.proposal_ids
        )
    except ProposalSendError as exc:
        raise _http(exc) from exc
    return {**out, "overview": _overview(project_id, user.role)}


@router.post("/{revision_id}/approve-costs")
def approve_revision_costs(
    project_id: str,
    revision_id: str,
    background: BackgroundTasks,
    body: VerifyOverrideIn | None = None,
    user: CurrentUser = Depends(require_role(*VERIFY_ROLES)),
):
    """The Executive's approval of the revised costs, inside the revision:
    re-commits the pricing snapshot (the Verify commit, with its optional
    per-figure overrides) without moving the bid off Submitted."""
    try:
        proposal_revisions.approve_costs(project_id, revision_id, user, body, background)
    except ProposalSendError as exc:
        raise _http(exc) from exc
    return _overview(project_id, user.role)


@router.post("/{revision_id}/request-approval")
def request_revision_approval(
    project_id: str, revision_id: str, user: CurrentUser = Depends(require_writer)
):
    """Ask the Executives again to approve the revised costs."""
    try:
        proposal_revisions.request_approval(project_id, revision_id, user.id)
    except ProposalSendError as exc:
        raise _http(exc) from exc
    return _overview(project_id, user.role)


@router.post("/{revision_id}/complete")
def complete_revision(
    project_id: str, revision_id: str, user: CurrentUser = Depends(require_writer)
):
    """Done: close the revision with what went out so far."""
    try:
        proposal_revisions.complete_revision(project_id, revision_id, user.id)
    except ProposalSendError as exc:
        raise _http(exc) from exc
    return _overview(project_id, user.role)


@router.post("/{revision_id}/cancel")
def cancel_revision(
    project_id: str, revision_id: str, user: CurrentUser = Depends(require_writer)
):
    """Cancel while nothing went out. Pricing edits made meanwhile stay."""
    try:
        proposal_revisions.cancel_revision(project_id, revision_id, user.id)
    except ProposalSendError as exc:
        raise _http(exc) from exc
    return _overview(project_id, user.role)
