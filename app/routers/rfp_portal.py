"""Portal invitations (NGEM and BuildingConnected): the /rfp-portal API
(docs/RFP_NGEM_PORTAL.md section 5, docs/RFP_BUILDINGCONNECTED.md section 6).

Every state transition lives in app/services/rfp_portal_ingest.py (and the
BuildingConnected actions in app/services/rfp_bc_portal.py); this module
validates the request, maps the service's refusals onto HTTP statuses and
shapes the responses the portal tabs are written against. The
BuildingConnected OAuth, status, run and GC-alias routes live in
app/routers/rfp_bc.py.

Access, three locks deep, the same contract as /rfp-emails:

- The env switch. `require_rfp_portal` is a ROUTER-level dependency: while
  no portal is served (RFP_INGESTION_ENABLED and at least one of
  RFP_NGEM_ENABLED / RFP_BC_ENABLED) every route 404s with the bare "Not
  Found" body before a token is read. On top of it every request checks
  that the portal it names (the list's `portal`, the row's `portal` on the
  detail and the actions, the literal /ngem/* paths) is served, and 404s
  the same way otherwise, so turning one portal off never exposes it
  through the other's switch. A configured account is NOT part of the
  switch: the tab still answers with the status block saying so.
- Roles: `RFP_REVIEW_ROLES` (Estimating Admin, IT Admin, Executive and both
  engineer focuses) on every route, except apply-dates, which writes the
  project's confidential actual bid date and is ACTUAL_BID_EDITOR_ROLES
  (Estimating Admin, Executive, IT Admin); the read-only accountant and the
  external estimator never reach any of them.
- Rate limits: the catch-all budget on every route.

What never leaves here: `view_url` and the download URLs (session-bound
portal tokens), the cookie jar and the password, and the BuildingConnected
raw `payload` except to IT Admin on the detail (D33). The detail selects an
explicit column list in the service and the status route reads the session
row's bookkeeping columns only.

Confidential dates: candidate breakdowns store date KINDS, never values, and
the detail passes through `rfp_match.redact_candidates` for roles outside
ACTUAL_BID_VIEWER_ROLES; the list withholds `match_score` for those roles
outright (the row does not carry the candidates that would say whether the
score used the actual date). On BuildingConnected rows the GC's dates
(close_at, job_walk_at, expected_start_at, expected_finish_at) and the
old/new values of their change entries are nulled for those roles too, on
the list and the detail alike (contract D8): the due date is the project's
actual bid date, which the project page hides from the same person.

Audit: every mutation is audited by the service inside the conditional
update that performs it (`rfp_portal.<action>`).

Event-loop rule: every handler is a plain `def` (the Supabase SDK is sync).
"""

from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_role
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.ratelimit import rate_limit
from app.core.roles import (
    ACTUAL_BID_EDITOR_ROLES,
    ACTUAL_BID_VIEWER_ROLES,
    RFP_REVIEW_ROLES,
    Role,
)
from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)


# ── Feature gate ─────────────────────────────────────────────────────────

PORTAL_NGEM = "ngem"
PORTAL_BUILDINGCONNECTED = "buildingconnected"

# The per-portal switch under the master RFP_INGESTION_ENABLED (the same two
# switches GET /features reports as rfp_ngem / rfp_buildingconnected).
_PORTAL_FLAGS = {
    PORTAL_NGEM: "rfp_ngem_enabled",
    PORTAL_BUILDINGCONNECTED: "rfp_bc_enabled",
}


def portal_served(portal: str | None) -> bool:
    """Whether this deployment serves `portal`: RFP_INGESTION_ENABLED AND
    the portal's own switch. An unknown portal (or none) is never served.
    Read through `getattr` so an absent flag fails CLOSED, never open."""
    flag = _PORTAL_FLAGS.get(str(portal or ""))
    if flag is None:
        return False
    settings = get_settings()
    return bool(getattr(settings, "rfp_ingest_enabled", False)) and bool(
        getattr(settings, flag, False)
    )


def rfp_portal_enabled() -> bool:
    """Whether this deployment serves ANY portal (the router-level gate):
    the master RFP_INGESTION_ENABLED switch AND RFP_NGEM_ENABLED or
    RFP_BC_ENABLED. Fails closed on an absent flag."""
    return any(portal_served(p) for p in _PORTAL_FLAGS)


def require_rfp_portal() -> None:
    """Router-level dependency: while no portal is served every route 404s
    with the bare "Not Found" body before auth runs."""
    if not rfp_portal_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def _require_portal(portal: str | None) -> None:
    """The per-request check: the portal this request names (or the row it
    touches belongs to) must be served, else the same bare 404."""
    if not portal_served(portal):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


REVIEW_QUEUE_ROLES = RFP_REVIEW_ROLES
require_review_queue = require_role(*REVIEW_QUEUE_ROLES)
# apply-dates writes actual_bid_at (confidential): the editors only (D8).
require_actual_bid_editor = require_role(*sorted(ACTUAL_BID_EDITOR_ROLES))

rfp_portal_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

router = APIRouter(
    prefix="/rfp-portal",
    tags=["rfp-portal"],
    dependencies=[Depends(require_rfp_portal)],
)

_INVITATION_NOT_FOUND = "Invitation not found"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
# The refusal of a harvest on a BuildingConnected row (mirrors
# rfp_portal_ingest.MSG_NO_HARVEST; the router refuses before the service
# so no job is queued for a portal that has nothing to harvest).
_MSG_NO_HARVEST = "BuildingConnected invitations carry no documents to harvest."
# The BuildingConnected dates a role outside ACTUAL_BID_VIEWER_ROLES never
# sees on the tab (contract D8): the GC's due date is the project's actual
# bid date, hidden from the same person on the project page.
_BC_CONFIDENTIAL_DATES = ("close_at", "job_walk_at", "expected_start_at", "expected_finish_at")

# Every lane name of every portal (the OpenAPI enum); each request is then
# checked against its own portal's lanes (rfp_portal_ingest.VIEWS_BY_PORTAL),
# so an NGEM list never answers a BuildingConnected lane or the reverse.
View = Literal[
    "review", "new", "existing", "ignored", "all",
    "needs_action", "open", "historical", "expired_withdrawn",
]
Portal = Literal["ngem", "buildingconnected"]
# The lane a list opens on when the caller names none (D21).
_DEFAULT_VIEW = {PORTAL_NGEM: "review", PORTAL_BUILDINGCONNECTED: "needs_action"}
_STATE_MAX_CHARS = 32


# ── Service seams ────────────────────────────────────────────────────────


def _service():
    """app/services/rfp_portal_ingest, imported on first use."""
    from app.services import rfp_portal_ingest

    return rfp_portal_ingest


def _match():
    """app/services/rfp_match (the pure scorer module), imported on first use."""
    from app.services import rfp_match

    return rfp_match


def _bc_portal():
    """app/services/rfp_bc_portal (the BuildingConnected actions), imported
    on first use."""
    from app.services import rfp_bc_portal

    return rfp_bc_portal


# ── Helpers ──────────────────────────────────────────────────────────────


def _uuid_or_404(value: str | None, message: str) -> str:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, message) from None
    return str(value)


def _exists_or_404(sb, invitation_id: str) -> dict:
    """The row's id, portal and GC (the GC card's "same" decision reads it),
    or a 404: a missing row, and a row whose portal this deployment does not
    serve (the bare body, as if the route did not exist)."""
    rows = (
        sb.table(_service().INVITATIONS_TABLE)
        .select("id, portal, gc_id")
        .eq("id", invitation_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _INVITATION_NOT_FOUND)
    _require_portal(rows[0].get("portal"))
    return rows[0]


def _refused(exc: Exception) -> HTTPException:
    """A refused service action (`RfpPortalError` only: any other exception
    is a bug and 500s like one). The code rides in X-Error-Code; the
    project-required, reason-invalid, GC-required and dates-unchanged codes
    are 400s (the caller's input), the locked code a 503, everything else a
    409, unless the error names its own `http_status` (Run now while the
    slice is inactive: 503 under rfp_portal_not_available)."""
    svc = _service()
    code = str(getattr(exc, "code", None) or svc.CODE_NOT_ACTIONABLE)
    http_status = getattr(exc, "http_status", None)
    if not http_status:
        if code in (
            svc.CODE_PROJECT_REQUIRED, svc.CODE_REASON_INVALID,
            ErrorCode.RFP_PORTAL_GC_REQUIRED.value, ErrorCode.RFP_PORTAL_DATES_UNCHANGED.value,
        ):
            http_status = status.HTTP_400_BAD_REQUEST
        elif code == svc.CODE_LOCKED:
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
        else:
            http_status = status.HTTP_409_CONFLICT
    return HTTPException(
        int(http_status),
        str(exc) or "That invitation is no longer waiting for this decision.",
        headers={"X-Error-Code": code},
    )


def _redact_bc_dates(row: dict) -> dict:
    """A BuildingConnected row for a role outside ACTUAL_BID_VIEWER_ROLES:
    the GC's dates (the due date is the project's actual bid date, which
    the project page hides from the same person; the other three ride with
    it) leave as null, and the change entries for those fields keep the
    field and the instant but lose their old and new values (contract D8,
    the same rule as projects._redact_portal_sources). NGEM rows are
    untouched: their close date is a public notice."""
    if row.get("portal") != PORTAL_BUILDINGCONNECTED:
        return row
    for field in _BC_CONFIDENTIAL_DATES:
        if field in row:
            row[field] = None
    for key in ("change_log", "changes"):
        entries = row.get(key)
        if isinstance(entries, list):
            row[key] = [
                {**c, "old": None, "new": None}
                if isinstance(c, dict) and c.get("field") in _BC_CONFIDENTIAL_DATES
                else c
                for c in entries
            ]
    return row


def _redacted(row: dict | None, role) -> dict:
    """The detail as the role may see it: never `view_url`; the raw
    BuildingConnected `payload` for IT Admin only (D33); the candidates,
    the match score and (BuildingConnected) the GC's dates and change
    values redacted for roles outside ACTUAL_BID_VIEWER_ROLES. A row whose
    portal is not served is a 404 like a missing one."""
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _INVITATION_NOT_FOUND)
    _require_portal(row.get("portal"))
    row.pop("view_url", None)
    if role != Role.IT_ADMIN:
        row.pop("payload", None)
    if role not in ACTUAL_BID_VIEWER_ROLES:
        row = _match().redact_candidates(row, role)
        row = _redact_bc_dates(row)
    return row


def _detail(sb, invitation_id: str, role) -> dict:
    return _redacted(_service().detail(sb, invitation_id), role)


def _act(sb, invitation_id: str, role, action) -> dict:
    """Run one service action (which answers the fresh detail) and redact.
    `action` receives the row's id / portal / gc_id read by the 404 check."""
    _uuid_or_404(invitation_id, _INVITATION_NOT_FOUND)
    found = _exists_or_404(sb, invitation_id)
    try:
        row = action(found)
    except _service().RfpPortalError as exc:
        raise _refused(exc) from exc
    return _redacted(row, role)


# ── Invitations ──────────────────────────────────────────────────────────


@router.get("/invitations", dependencies=[Depends(rfp_portal_rate_limit)])
def list_invitations(
    portal: Portal = "ngem",
    view: View | None = None,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=1_000_000),
    state: str | None = Query(default=None, max_length=_STATE_MAX_CHARS),
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """One lane of a portal tab, newest close date first: `{items, total,
    offset, limit, counts}`. The portal must be served (404 otherwise) and
    the lane must be one of ITS lanes (422 otherwise); no lane opens the
    portal's default (NGEM `review`, BuildingConnected `needs_action`).
    `state` filters a BuildingConnected list on submission_state.
    `match_score` is null for roles outside ACTUAL_BID_VIEWER_ROLES (see
    the module docstring)."""
    _require_portal(portal)
    lanes = _service().VIEWS_BY_PORTAL.get(portal) or {}
    lane = view or _DEFAULT_VIEW.get(portal, "all")
    if lane not in lanes:
        raise HTTPException(
            422,
            f"Unknown view {lane!r} for portal {portal!r}.",
        )
    out = _service().list_invitations(
        get_supabase(), portal=portal, view=lane, q=q, limit=limit, offset=offset,
        state=state if isinstance(state, str) else None,
    )
    if user.role not in ACTUAL_BID_VIEWER_ROLES:
        for row in out.get("items") or []:
            row["match_score"] = None
            _redact_bc_dates(row)
    return out


@router.get("/invitations/{invitation_id}", dependencies=[Depends(rfp_portal_rate_limit)])
def get_invitation(
    invitation_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """The detail: the row without `view_url`, the candidates (redacted per
    role), `excluded_projects`, `change_log`, `harvest`, `harvest_job` and
    `possible_rebid`. Never a portal link, a cookie or the password."""
    _uuid_or_404(invitation_id, _INVITATION_NOT_FOUND)
    return _detail(get_supabase(), invitation_id, user.role)


class ResolveIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=64)


@router.post("/invitations/{invitation_id}/resolve", dependencies=[Depends(rfp_portal_rate_limit)])
def resolve_invitation(
    invitation_id: str, body: ResolveIn, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """Same as project X: review_match -> exists."""
    sb = get_supabase()
    return _act(
        sb, invitation_id, user.role,
        lambda _row: _service().resolve_exists(sb, invitation_id, body.project_id, user.id),
    )


@router.post("/invitations/{invitation_id}/new", dependencies=[Depends(rfp_portal_rate_limit)])
def new_invitation(
    invitation_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """Not a match: review_match -> harvest with the candidates excluded."""
    sb = get_supabase()
    return _act(
        sb, invitation_id, user.role,
        lambda _row: _service().resolve_new(sb, invitation_id, user.id),
    )


class ReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/invitations/{invitation_id}/reopen", dependencies=[Depends(rfp_portal_rate_limit)])
def reopen_invitation(
    invitation_id: str,
    body: ReasonIn | None = None,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """exists -> match with the project excluded."""
    sb = get_supabase()
    reason = body.reason if body else None
    return _act(
        sb, invitation_id, user.role,
        lambda _row: _service().reopen(sb, invitation_id, reason, user.id),
    )


@router.post("/invitations/{invitation_id}/ignore", dependencies=[Depends(rfp_portal_rate_limit)])
def ignore_invitation(
    invitation_id: str,
    body: ReasonIn | None = None,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """match / review_match / harvest -> ignored (reason optional)."""
    sb = get_supabase()
    reason = body.reason if body else None
    return _act(
        sb, invitation_id, user.role,
        lambda _row: _service().ignore(sb, invitation_id, reason, user.id),
    )


@router.post("/invitations/{invitation_id}/unignore", dependencies=[Depends(rfp_portal_rate_limit)])
def unignore_invitation(
    invitation_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """ignored -> match."""
    sb = get_supabase()
    return _act(
        sb, invitation_id, user.role,
        lambda _row: _service().unignore(sb, invitation_id, user.id),
    )


class HarvestIn(BaseModel):
    """`force` refreshes a complete harvest instead of reusing it."""

    force: bool = True


@router.post(
    "/invitations/{invitation_id}/harvest",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rfp_portal_rate_limit)],
)
def harvest_invitation(
    invitation_id: str,
    body: HarvestIn | None = None,
    user: CurrentUser = Depends(require_review_queue),
) -> dict:
    """Harvest again: 202 `{job}`; 409 rfp_portal_harvest_active while a job
    is queued or running; 409 rfp_portal_not_available when the status is
    not one a harvest may run from; 409 rfp_portal_not_actionable on a
    BuildingConnected row (its invitations carry no documents to harvest,
    so no job is ever queued for one); 503 rfp_portal_locked while portal
    logins are locked or the account is not configured."""
    _uuid_or_404(invitation_id, _INVITATION_NOT_FOUND)
    sb = get_supabase()
    found = _exists_or_404(sb, invitation_id)
    if found.get("portal") == PORTAL_BUILDINGCONNECTED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            _MSG_NO_HARVEST,
            headers={"X-Error-Code": _service().CODE_NOT_ACTIONABLE},
        )
    force = bool(body.force) if body else True
    try:
        job = _service().harvest_again(sb, invitation_id, user.id, force=force)
    except _service().RfpPortalError as exc:
        raise _refused(exc) from exc
    return {"job": job}


# X-Error-Code values of the creation refusals (docs/RFP_CREATE.md 8); the
# same two the email router names.
CODE_CREATE_REFUSED = "rfp_create_refused"
CODE_CREATE_IN_PROGRESS = "rfp_create_in_progress"


@router.post("/invitations/{invitation_id}/create", dependencies=[Depends(rfp_portal_rate_limit)])
def create_invitation_project(
    invitation_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """"Create project": a bidding project from a `done` invitation, parked
    in Go/No-Go with the title, the close date and the harvested documents,
    `is_ngem` set and no GC (the agency is the owner). `{project_id, number,
    linked}`; 409 `rfp_create_refused` with the sentence, 409
    `rfp_create_in_progress` while another worker holds the harvest's
    claim. Audited `rfp_portal.create` by the service."""
    _uuid_or_404(invitation_id, _INVITATION_NOT_FOUND)
    sb = get_supabase()
    _exists_or_404(sb, invitation_id)
    from app.services import rfp_create

    try:
        made = _service().create_project(sb, invitation_id, user.id)
    except rfp_create.CreateInProgress as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, str(exc), headers={"X-Error-Code": CODE_CREATE_IN_PROGRESS}
        ) from exc
    except rfp_create.CreateRefused as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, str(exc), headers={"X-Error-Code": CODE_CREATE_REFUSED}
        ) from exc
    except _service().RfpPortalError as exc:
        raise _refused(exc) from exc
    return {"project_id": made.project_id, "number": made.number, "linked": bool(made.linked)}


# ── BuildingConnected actions (docs/RFP_BUILDINGCONNECTED.md section 6) ─


def _gc_required(message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_400_BAD_REQUEST, message,
        headers={"X-Error-Code": ErrorCode.RFP_PORTAL_GC_REQUIRED.value},
    )


class GcCreateIn(BaseModel):
    """"Add a GC": the company (pre-filled from BuildingConnected) and the
    lead as its first contact."""

    name: str = Field(min_length=1, max_length=200)
    contact_name: str | None = Field(default=None, max_length=200)
    contact_email: str | None = Field(default=None, max_length=254)
    contact_phone: str | None = Field(default=None, max_length=64)


class GcDecisionIn(BaseModel):
    """The GC card: `same` confirms the provisional GC on the row, `pick`
    names another directory GC (`gc_id`), `create` adds one (`create`)."""

    decision: Literal["same", "pick", "create"]
    gc_id: str | None = Field(default=None, max_length=64)
    create: GcCreateIn | None = None


@router.post("/invitations/{invitation_id}/gc", dependencies=[Depends(rfp_portal_rate_limit)])
def confirm_invitation_gc(
    invitation_id: str, body: GcDecisionIn, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """Confirm, pick or add the GC behind a BuildingConnected company: the
    alias is written, the row's GC follows, a row parked for the unresolved
    GC goes back to `match`, and a project the row already has swaps its GC
    link (rfp_bc_portal.confirm_gc). Answers the fresh detail. 400
    rfp_portal_gc_required when the decision names no usable GC (nothing
    picked, `same` on a row with no provisional GC, a GC that is gone, an
    NDA-masked company); 409 rfp_portal_not_actionable on a non
    BuildingConnected row."""
    sb = get_supabase()

    def action(found: dict) -> dict:
        gc_id: str | None = None
        create: dict | None = None
        if body.decision == "same":
            gc_id = str(found.get("gc_id") or "") or None
            if not gc_id:
                raise _gc_required("This invitation has no suggested GC to confirm; pick one or add one.")
        elif body.decision == "pick":
            gc_id = (body.gc_id or "").strip() or None
            if not gc_id:
                raise _gc_required("Pick the GC this BuildingConnected company is.")
        else:
            if body.create is None:
                raise _gc_required("Name the GC to add.")
            create = body.create.model_dump()
        from app.services import gc_aliases

        try:
            return _bc_portal().confirm_gc(sb, invitation_id, user.id, gc_id=gc_id, create=create)
        except (ValueError, gc_aliases.GcAliasError) as exc:
            raise _gc_required(str(exc) or "That GC cannot be used.") from exc

    return _act(sb, invitation_id, user.role, action)


ApplyDateField = Literal["actual_bid_at", "job_walk_at", "est_start_date", "est_finish_date"]
# What the apply-dates answer says about the project ("ProjectOut-lite").
_PROJECT_LITE_KEYS = (
    "id", "name", "number", "actual_bid_at", "bid_time_unknown", "job_walk_at",
    "est_start_date", "est_finish_date", "updated_at",
)


class ApplyDatesIn(BaseModel):
    fields: list[ApplyDateField] = Field(min_length=1, max_length=4)


@router.post(
    "/invitations/{invitation_id}/apply-dates", dependencies=[Depends(rfp_portal_rate_limit)]
)
def apply_invitation_dates(
    invitation_id: str, body: ApplyDatesIn, user: CurrentUser = Depends(require_actual_bid_editor)
) -> dict:
    """The GC moved a date: write the ticked ones from the invitation onto
    its project (rfp_bc_portal.apply_dates; never internal_bid_at).
    ACTUAL_BID_EDITOR_ROLES only (it writes the actual bid date). Answers
    `{project: <the project's date fields>, applied: [...]}`; 400
    rfp_portal_project_required when the row has no project, 400
    rfp_portal_dates_unchanged when nothing picked has a value."""
    _uuid_or_404(invitation_id, _INVITATION_NOT_FOUND)
    sb = get_supabase()
    _exists_or_404(sb, invitation_id)
    try:
        out = _bc_portal().apply_dates(sb, invitation_id, user.id, list(dict.fromkeys(body.fields)))
    except _service().RfpPortalError as exc:
        raise _refused(exc) from exc
    except LookupError as exc:
        # The project went away between the read and the write (deleted,
        # docs/PROJECT_DELETE.md): a 404, never a raw 500. A KeyError or an
        # IndexError is a bug, not a missing project.
        if isinstance(exc, (KeyError, IndexError)):
            raise
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc) or "Project not found") from exc
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, str(exc) or "Nothing to apply.",
            headers={"X-Error-Code": ErrorCode.RFP_PORTAL_DATES_UNCHANGED.value},
        ) from exc
    project = out.get("project") or {}
    return {
        "project": {k: project[k] for k in _PROJECT_LITE_KEYS if k in project},
        "applied": list(out.get("applied") or []),
    }


@router.post("/invitations/{invitation_id}/restore", dependencies=[Depends(rfp_portal_rate_limit)])
def restore_invitation(
    invitation_id: str, user: CurrentUser = Depends(require_review_queue)
) -> dict:
    """historical / expired / withdrawn -> match (the FE labels it "Process"
    on a historical row). Answers the fresh detail; 409
    rfp_portal_not_restorable when the row is not parked (or moved on
    meanwhile)."""
    sb = get_supabase()

    def action(_row: dict) -> dict:
        svc = _service()
        try:
            return svc.restore(sb, invitation_id, user.id)
        except svc.RfpPortalError as exc:
            if getattr(exc, "code", None) == svc.CODE_NOT_ACTIONABLE:
                raise svc.RfpPortalError(
                    ErrorCode.RFP_PORTAL_NOT_RESTORABLE.value,
                    str(exc) or "This invitation is not parked; there is nothing to restore.",
                ) from exc
            raise

    return _act(sb, invitation_id, user.role, action)


# ── The portal itself ────────────────────────────────────────────────────


@router.post(
    "/ngem/runs",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rfp_portal_rate_limit)],
)
def run_ngem_now(user: CurrentUser = Depends(require_review_queue)) -> dict:
    """Run now: 202 `{run}`; 409 rfp_portal_run_active while a scan is
    queued or running; 503 rfp_portal_locked while logins are locked; 503
    rfp_portal_not_available while the slice is inactive (the queue that
    runs the scan is off)."""
    _require_portal(PORTAL_NGEM)
    try:
        run = _service().run_now(get_supabase(), user.id, _service().PORTAL_NGEM)
    except _service().RfpPortalError as exc:
        raise _refused(exc) from exc
    return {"run": run}


@router.get("/ngem/status", dependencies=[Depends(rfp_portal_rate_limit)])
def ngem_status(_: CurrentUser = Depends(require_review_queue)) -> dict:
    """The settings block: configured (the account, never the password),
    the session bookkeeping (never the cookies), the schedule, the next and
    the last run, the active run and the active jobs."""
    _require_portal(PORTAL_NGEM)
    return _service().session_status(_service().PORTAL_NGEM)


# Error codes on this router (docs/ERROR_CODES.md): the values live in
# app/core/error_codes.ErrorCode and are asserted equal to the service's.
_CODES = (
    ErrorCode.RFP_PORTAL_NOT_ACTIONABLE,
    ErrorCode.RFP_PORTAL_PROJECT_REQUIRED,
    ErrorCode.RFP_PORTAL_HARVEST_ACTIVE,
    ErrorCode.RFP_PORTAL_RUN_ACTIVE,
    ErrorCode.RFP_PORTAL_LOCKED,
    ErrorCode.RFP_PORTAL_NOT_AVAILABLE,
    ErrorCode.RFP_PORTAL_REASON_INVALID,
)
# The BuildingConnected invitation actions' codes: the first two equal
# rfp_bc_portal.CODE_GC_REQUIRED / CODE_DATES_UNCHANGED; not_restorable is
# this router's own name for a restore on a row that is not parked.
_BC_CODES = (
    ErrorCode.RFP_PORTAL_GC_REQUIRED,
    ErrorCode.RFP_PORTAL_DATES_UNCHANGED,
    ErrorCode.RFP_PORTAL_NOT_RESTORABLE,
)
