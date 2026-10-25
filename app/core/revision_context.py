"""The Revise proposal lock (docs/PROPOSAL_REVISIONS.md section 5).

While a revision is open, every change to the bid happens inside the Revise
proposal modal. The modal's API calls carry the open revision's id in the
`X-Proposal-Revision` header; the cost, pricing and proposal write routes
depend on `require_revision_context`, which lets a request through when no
revision is open (nothing changes outside a revision) or when the header
names the open one, and otherwise answers 409 `revision_open`. A stale tab or
the regular project page therefore cannot edit a bid under revision. Email
ingestion never goes through these routes, so quotes keep landing.

Sync on purpose: FastAPI runs a plain `def` dependency in its threadpool, so
the Supabase read here never blocks the event loop.
"""

from fastapi import Depends, Header, HTTPException, status

from app.core.deps import CurrentUser, get_current_user

from app.core.error_codes import ErrorCode

REVISION_HEADER = "X-Proposal-Revision"


def revision_open_error(revision_number: int) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        f"Revision {revision_number} is open on this bid. All changes to this bid "
        "happen in Revise proposal.",
        headers={"X-Error-Code": ErrorCode.REVISION_OPEN.value},
    )


def _open_revision(project_id: str) -> dict | None:
    """The lookup the lock runs (a seam the test suite defaults to "no
    revision open", like notification routing's overrides)."""
    from app.services import proposal_revisions

    return proposal_revisions.open_revision(project_id)


def require_revision_context(
    project_id: str,
    x_proposal_revision: str | None = Header(default=None, alias=REVISION_HEADER),
    user: CurrentUser = Depends(get_current_user),
) -> str | None:
    """The open revision's id when this request is one of its own (the
    header matches), None when no revision is open; 409 `revision_open`
    otherwise."""
    rev = _open_revision(project_id)
    if rev is None:
        return None
    if x_proposal_revision and x_proposal_revision == rev["id"]:
        return rev["id"]
    raise revision_open_error(rev["revision_number"])
