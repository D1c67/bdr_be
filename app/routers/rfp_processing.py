"""The RFP Processing page API: /rfp-processing (docs/RFP_PROCESSING.md
section 2).

An operations view over the RFP intake pipeline: every email row and portal
invitation still in flight, in lanes (the three human review lanes,
processing, stuck), plus one action of its own, "Retry" on a stuck email
row. Every other decision on these rows is the /rfp-emails and /rfp-portal
routes the detail dialogs already call.

Access, the same three locks as /rfp-emails, reusing its pieces:

- The env flag: `rfp_emails.require_rfp_emails` is the ROUTER-level
  dependency, so while the email intake is not served every route 404s with
  the bare "Not Found" body before a token is read.
- Roles: the reads are `require_view_queue` (the review roles plus the
  read-only accountant), the retry is `require_review_queue`.
- Rate limits: the catch-all budget on every route.

Scope: every email read goes through `rfp_email_visibility.apply_scope`
(inside services/rfp_processing), and the retry loads its row through
`rfp_emails._email_row_or_404(..., user=)`, so a row outside the viewer's
mailboxes is the unknown-id 404. Portal invitations are not mailbox rows:
they are included whenever this backend serves at least one portal (the
`rfp_ngem` / `rfp_buildingconnected` flags of GET /features, contract D4)
AND the viewer is a review role, since the portal's own routes (the
invitation dialog) are review roles only and the accountant could not open
one. Only the rows of a SERVED portal are loaded: a portal switched off
after its rows exist is dropped, the way its dialog would 404 them.

Nothing here carries a body, a preview, a link, a `view_url` or a
`match_score` (the list route of /rfp-emails explains why the score may
carry the actual-date bucket).

Event-loop rule: every handler is a plain `def` (the Supabase SDK is sync).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.config import get_settings
from app.core.deps import CurrentUser
from app.core.error_codes import RateLimitScope
from app.core.ratelimit import rate_limit
from app.core.roles import RFP_REVIEW_ROLES
from app.core.supabase_client import get_supabase
from app.services import rfp_processing as svc

# The queue router's gates and helpers, imported rather than duplicated (the
# same way rfp_created leans on shared pieces): the flag gate, the two role
# dependencies, the per-row visibility 404, the detail every action answers,
# the list columns and the three reference lookups. Several are private to
# that module; importing them keeps one definition of each rule.
from app.routers.rfp_emails import (  # noqa: PLC2701 - deliberate reuse, see above
    _EMAIL_NOT_FOUND,
    _LIST_SELECT,
    _detail,
    _email_row_or_404,
    _gcs_by_id,
    _harvest_status_by_id,
    _projects_by_id,
    _uuid_or_404,
    require_rfp_emails,
    require_review_queue,
    require_view_queue,
)

logger = logging.getLogger(__name__)

# X-Error-Code of the retry refusal (docs/RFP_PROCESSING.md 2.3). Named here
# like the create codes on rfp_emails rather than in app/core/error_codes.
CODE_NOT_RETRYABLE = "rfp_processing_not_retryable"

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200

Lane = Literal[
    "all", "review", "review_llm", "flagged_unauthorized", "review_match", "processing", "stuck",
]

rfp_processing_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

router = APIRouter(
    prefix="/rfp-processing",
    tags=["rfp-processing"],
    dependencies=[Depends(require_rfp_emails)],
)


def _ingest():
    """app/services/rfp_email_ingest, imported on first use (the tests' seam)."""
    from app.services import rfp_email_ingest

    return rfp_email_ingest


def _now() -> datetime:
    return datetime.now(timezone.utc)


# The per-portal switch under the master RFP_INGESTION_ENABLED: the same
# two the /rfp-portal router and GET /features (rfp_ngem, rfp_buildingconnected)
# read. A portal absent here is never served.
_PORTAL_FLAGS = {"ngem": "rfp_ngem_enabled", "buildingconnected": "rfp_bc_enabled"}


def served_portals() -> frozenset[str]:
    """The portals this backend serves: RFP_INGESTION_ENABLED and the
    portal's own switch, read through `getattr` so an absent flag fails
    CLOSED. The rows of a portal that is off never reach the page, since
    their dialog (GET /rfp-portal/invitations/{id}) would 404 them."""
    settings = get_settings()
    if not bool(getattr(settings, "rfp_ingest_enabled", False)):
        return frozenset()
    return frozenset(
        portal for portal, flag in _PORTAL_FLAGS.items() if bool(getattr(settings, flag, False))
    )


def portal_served() -> bool:
    """Whether this backend serves ANY portal (`rfp_portal_any_enabled`,
    contract D4): the master switch and at least one of RFP_NGEM_ENABLED /
    RFP_BC_ENABLED. Fails closed on an absent flag."""
    settings = get_settings()
    computed = getattr(settings, "rfp_portal_any_enabled", None)
    if computed is not None and bool(getattr(settings, "rfp_ingest_enabled", False)):
        return bool(computed)
    return bool(served_portals())


def _portal_for(user: CurrentUser) -> bool:
    """Portal invitations for this viewer: served, and a review role (the
    invitation dialog's routes are review roles only)."""
    return portal_served() and user.role in RFP_REVIEW_ROLES


def _classified(sb, user: CurrentUser, now: datetime, *, email_select: str):
    """`[(source, row, lane, stuck)]` over every in-flight row the viewer can
    see, plus the portal flag and the harvest statuses already looked up."""
    settings = get_settings()
    include_portal = _portal_for(user)
    emails = svc.load_email_rows(sb, user, now=now, select=email_select)
    portals = svc.load_portal_rows(sb, portals=served_portals()) if include_portal else []
    harvests = svc.harvest_statuses(
        sb,
        svc.harvest_ids_to_check(emails) | svc.harvest_ids_to_check(portals),
        _harvest_status_by_id,
    )
    out = [
        (svc.SOURCE_EMAIL, row, lane, stuck)
        for row, lane, stuck in svc.classify_all(
            emails, source=svc.SOURCE_EMAIL, now=now, settings=settings, harvests=harvests
        )
    ]
    out += [
        (svc.SOURCE_PORTAL, row, lane, stuck)
        for row, lane, stuck in svc.classify_all(
            portals, source=svc.SOURCE_PORTAL, now=now, settings=settings, harvests=harvests
        )
    ]
    return out, include_portal, harvests


# ── Reads (literal routes first) ─────────────────────────────────────────


@router.get("/summary", dependencies=[Depends(rfp_processing_rate_limit)])
def rfp_processing_summary(user: CurrentUser = Depends(require_view_queue)) -> dict:
    """`{lanes, steps, stuck_kinds, waiting_on_review, portal_served,
    generated_at}` (section 2.1): lane counts (plus `review_total` and
    `total`), the processing rows per automated step, the stuck rows per
    reason, the processing rows parked behind a copy in a review lane. Counted inside the
    viewer's mailbox scope; an empty scope reads no email row at all.
    `portal_served` is true when portal invitations are part of the counts
    for this viewer."""
    sb = get_supabase()
    now = _now()
    classified, include_portal, _ = _classified(
        sb, user, now, email_select=svc.EMAIL_CLASSIFY_SELECT
    )
    return svc.summarize(classified, portal_served=include_portal, now=now)


@router.get("", dependencies=[Depends(rfp_processing_rate_limit)])
def list_rfp_processing(
    lane: Lane = "all",
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    user: CurrentUser = Depends(require_view_queue),
) -> dict:
    """`{items, total, offset, limit, lane}` (section 2.2): the in-flight rows
    of one lane (`all`, `review` for the three review lanes, or one lane),
    review rows first, then stuck, then processing, the longest wait first
    inside each group. `total` counts the lane. Reference lookups (projects,
    GCs, harvest states) run once over the page's ids."""
    sb = get_supabase()
    now = _now()
    classified, _, harvests = _classified(
        sb, user, now, email_select=svc.email_list_select(_LIST_SELECT)
    )
    hits = svc.select_lane(classified, lane)
    page = hits[offset: offset + limit]

    rows = [row for _, row, _, _ in page]
    projects = _projects_by_id(sb, {r.get("match_project_id") for r in rows})
    gcs = _gcs_by_id(
        sb, {r.get("resolved_gc_id") for s, r, _, _ in page if s == svc.SOURCE_EMAIL}
    )
    missing = {str(r["harvest_id"]) for r in rows if r.get("harvest_id")} - set(harvests)
    if missing:
        harvests = {**harvests, **svc.harvest_statuses(sb, missing, _harvest_status_by_id)}

    waits = svc.review_waits(classified)
    items = []
    for source, row, row_lane, stuck in page:
        if source == svc.SOURCE_PORTAL:
            items.append(svc.portal_item(row, row_lane, stuck, projects=projects, harvests=harvests))
        else:
            items.append(
                svc.email_item(
                    row, row_lane, stuck, projects=projects, gcs=gcs, harvests=harvests,
                    waiting_on=waits.get(str(row.get("id"))),
                )
            )
    return {"items": items, "total": len(hits), "offset": offset, "limit": limit, "lane": lane}


# ── Retry (section 2.3) ──────────────────────────────────────────────────


@router.post("/emails/{email_id}/retry", dependencies=[Depends(rfp_processing_rate_limit)])
def retry_rfp_processing_email(
    email_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """"Retry" on a stuck email row: a `failed` row goes back to the step
    that failed with a fresh attempt budget, a retrying or model-waiting row
    is made due now (`rfp_email_ingest.retry_row`). 404 for an unknown id or
    a row outside the viewer's mailboxes; 409 `rfp_processing_not_retryable`
    for any other status, a sibling follower, or a lost race. Audited
    `rfp_email.retry` by the service, inside the write that wins. Answers
    the detail, like every action on /rfp-emails."""
    _uuid_or_404(email_id, _EMAIL_NOT_FOUND)
    sb = get_supabase()
    _email_row_or_404(sb, email_id, select="id", user=user)
    try:
        _ingest().retry_row(sb, email_id, user.id)
    except LookupError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            str(exc) or "This email cannot be retried right now.",
            headers={"X-Error-Code": CODE_NOT_RETRYABLE},
        ) from exc
    return _detail(sb, email_id, user.role)
