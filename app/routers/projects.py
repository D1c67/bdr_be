"""Projects + intake (step 1) and the dashboard list."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from postgrest.exceptions import APIError

from app.core.config import get_settings
from app.core.deps import (
    CurrentUser,
    get_current_user,
    require_internal,
    require_role,
    require_writer,
)
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.features import SubApp, require_feature, require_rfp_ingest
from app.core.ratelimit import check_outbound_email, rate_limit
from app.core.roles import (
    ACTUAL_BID_EDITOR_ROLES,
    ACTUAL_BID_VIEWER_ROLES,
    INTERNAL_ROLES,
    PROJECT_DELETE_ROLES,
    RFP_REVIEW_ROLES,
    WRITER_ROLES,
    Role,
)
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    AbandonIn,
    BidsTodayProjectOut,
    ProjectCreate,
    ProjectDeleteIn,
    ProjectGcConfirmIn,
    ProjectGcConfirmOut,
    ProjectGCIn,
    ProjectGCUpdate,
    ProjectOpenIn,
    ProjectOut,
    ProjectUpdate,
    RfpMatchAcknowledgeIn,
    RfpMatchUnmergeIn,
    SimilarProjectsIn,
)
from app.services import (
    bid_date_change_email,
    email_ingest,
    estimator_lifecycle,
    gono,
    pm,
    project_delete,
    project_numbers,
    proposal_send,
    storage,
    workflow,
)
from app.services.bid_invitations import REPORT_TZ, _day_start
from app.services.notifications import (
    ESTIMATOR_NOTIFICATION_TYPES,
    audit,
    dismiss_notifications,
    notify_role,
)
from app.services.project_intake import missing_intake_fields
from app.services.project_numbers import is_duplicate_number
from app.services.project_status import derive_status

router = APIRouter(prefix="/projects", tags=["projects"])

logger = logging.getLogger(__name__)

# This router is the SHARED SPINE — PM and Certified Payroll both create rows in
# `projects` and read them back — so it is mounted ungated (app/main.py). Some of
# its routes are nonetheless pure bidding, and carry the flag individually:
#
#   POST ""                  bid intake — sets current_stage='intake', writes a
#                            stage_event and seeds all four lanes of the bidding
#                            DAG. PM creates via POST /pm/projects, CP via
#                            POST /payroll/projects; neither comes through here.
#   /{id}/abandon, /reactivate   bid lifecycle (reactivate is also the deferred
#                            won→PM entry point).
#   /{id}/gcs …              bid-invitation membership. `project_gcs` is read
#                            only by this router, services/bid_invitations and
#                            services/proposal_send — all bidding.
#   /similar                 the New Bid form's pre-create similar-projects
#                            check (docs/RFP_MATCHING.md 3.9): bidding only,
#                            never gated on the RFP flag.
#   /{id}/rfp-matches …      the "Merged by System" record: bidding AND the
#                            master RFP_INGESTION_ENABLED switch (not the
#                            derived mailbox setting, so records stay visible
#                            and unmergeable while polling is paused).
#
# GET "", GET /{id} and PATCH /{id} deliberately stay open: they are how a PM or
# CP deployment reads and renames the project rows it owns.
_BIDDING_ONLY = [Depends(require_feature(SubApp.BIDDING))]
_RFP_MATCH_DEPS = [Depends(require_rfp_ingest)] + _BIDDING_ONLY

# Who may delete a project (docs/PROJECT_DELETE.md 3); bound once so the
# tests can identify the exact dependency on the two routes.
require_project_delete = require_role(*PROJECT_DELETE_ROLES)
project_delete_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

# Who may reverse a system merge (docs/RFP_MATCHING.md 3.7): it detaches a GC
# the matcher put on the project, so it is narrower than the writer set.
RFP_UNMERGE_ROLES = (Role.ESTIMATING_ADMIN, Role.EXECUTIVE, Role.IT_ADMIN)
# Bound once so the tests can identify the exact dependency on the route.
require_rfp_unmerge = require_role(*RFP_UNMERGE_ROLES)

# The similar-projects check is one cheap read per New Bid submit; the
# catch-all budget is only an abuse backstop.
similar_projects_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)
# The next-number preview is one counter read per New Project modal open; the
# same catch-all budget.
next_number_rate_limit = rate_limit(
    RateLimitScope.DEFAULT, lambda: get_settings().default_rate_limit_per_min
)

# The projects.number unique index (migration 0052) retires every number ever
# used — they can't be re-used, even by an abandoned project. A collision surfaces
# from PostgREST as a 23505 (project_numbers.is_duplicate_number recognizes it);
# translate it into a clean 409 instead of a raw 500. Creation retries past a
# taken number by itself, so this message is only reached by the Budgetary
# toggle (a rename onto a number a PM-created row already holds).
_NUMBER_TAKEN = (
    "That project number is already in use; numbers can't be re-used. "
    "If an earlier attempt seemed to fail, the project may already exist, "
    "so check the project list before retrying."
)

# The PM and Certified Payroll routers (which still take a typed number) import
# the classifier from here; it lives in services/project_numbers now.
_is_duplicate_number = is_duplicate_number


def _ingest():
    """app/services/rfp_email_ingest, imported on first use: it owns every
    rfp_project_matches write and the count queries. Reached lazily so this
    shared-spine router imports on a deployment that never serves RFP
    ingestion, and so the tests have one seam to stub."""
    from app.services import rfp_email_ingest

    return rfp_email_ingest


def _match():
    """app/services/rfp_match (the pure scorer), imported on first use."""
    from app.services import rfp_match

    return rfp_match


def _bc_portal():
    """app/services/rfp_bc_portal (the BuildingConnected portal: the GC
    confirmation lives there), imported on first use; one seam for the tests."""
    from app.services import rfp_bc_portal

    return rfp_bc_portal


def _files_needed():
    """app/services/files_needed (the files flag), imported on first use."""
    from app.services import files_needed

    return files_needed


def redact_for_role(project: dict, role: Role) -> dict:
    """Null the actual (to-GC) bid date for roles that may not see it.

    Redaction is server-side so the date never reaches the client; every
    handler that returns a project row must pass it through here.
    """
    if role in ACTUAL_BID_VIEWER_ROLES:
        return project
    return {**project, "actual_bid_at": None}


def _serialize_cat_state(state: dict[str, dict]) -> dict[str, dict]:
    """Shape workflow.load_category_state output for ProjectOut.category_state
    (each value needs its own `category` key)."""
    return {cat: {"category": cat, **vals} for cat, vals in state.items()}


def _pending_gc_pricing_counts(project_ids: list[str]) -> dict[str, int]:
    """project_id -> GCs whose per-GC price change awaits an Executive (0118).
    The dashboard lists such a project as an Executive task; the stage itself
    never moves for it."""
    if not project_ids:
        return {}
    rows = (
        get_supabase()
        .table("project_gcs")
        .select("project_id")
        .eq("pricing_approval_status", "pending")
        .in_("project_id", project_ids)
        .execute()
    ).data or []
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["project_id"]] = counts.get(r["project_id"], 0) + 1
    return counts


def _send_out_head(cat_state: dict[str, dict] | None) -> str | None:
    return ((cat_state or {}).get("send_out") or {}).get("current_task")


def _gone_out(head: str | None, reverify_return_stage: str | None) -> bool:
    """proposal_send.bid_has_gone_out's rule over data already in hand: the
    send_out head is past send-out, or it is parked at `verify` by a
    post-submission pricing edit that will return there. The service form
    reads `projects` per call; the list route has the rows already."""
    if head in proposal_send.PRICING_APPROVAL_HEADS:
        return True
    return head == "verify" and reverify_return_stage in proposal_send.PRICING_APPROVAL_HEADS


def _rfp_match_counts(
    project_ids: list[str],
    projects_by_id: dict[str, dict],
    cat_states: dict[str, dict[str, dict]],
) -> dict[str, dict]:
    """project_id -> {merged, history, new} from rfp_project_matches
    (docs/RFP_MATCHING.md 3.9) for the list and detail routes.

    Returns {} at once while RFP_INGESTION_ENABLED is off: no query against a
    table that does not exist on a deployment without migration 0122. The
    post-send set is derived here, inline over the loaded category states and
    project rows, so the service's proposal_sends read (which it runs only
    when open merges exist) is restricted to the projects whose bid has gone
    out, never the full listed id set (list_projects is unpaginated)."""
    if not project_ids or not get_settings().rfp_ingest_enabled:
        return {}
    post_send = [
        pid
        for pid in project_ids
        if _gone_out(
            _send_out_head(cat_states.get(pid)),
            (projects_by_id.get(pid) or {}).get("reverify_return_stage"),
        )
    ]
    return _ingest().rfp_match_counts(get_supabase(), list(project_ids), post_send) or {}


_PORTAL_FLAGS = {"ngem": "rfp_ngem_enabled", "buildingconnected": "rfp_bc_enabled"}


def _portal_any_enabled(settings) -> bool:
    """`rfp_portal_any_enabled` (contract D4), computed from the two portal
    switches when the settings object predates it; an absent flag fails
    closed either way."""
    if not getattr(settings, "rfp_ingest_enabled", False):
        return False
    computed = getattr(settings, "rfp_portal_any_enabled", None)
    if computed is not None:
        return bool(computed)
    return any(bool(getattr(settings, flag, False)) for flag in _PORTAL_FLAGS.values())


def _require_bc_served() -> None:
    """The per-request gate of the BuildingConnected-only project routes
    (gc-confirm, contract D4): RFP_INGESTION_ENABLED and RFP_BC_ENABLED,
    read through `getattr` so an absent flag fails CLOSED; otherwise the
    bare 404 the /rfp-portal routes answer for a portal that is not
    served, so switching the portal off never leaves its lead contact or
    its alias writes reachable through the project page."""
    settings = get_settings()
    served = bool(getattr(settings, "rfp_ingest_enabled", False)) and bool(
        getattr(settings, _PORTAL_FLAGS["buildingconnected"], False)
    )
    if not served:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def _portal_change(entry: dict) -> dict:
    """One change-log entry in the PortalChangeOut shape (run ids and any
    other bookkeeping dropped)."""
    return {
        "at": entry.get("at"),
        "field": str(entry.get("field") or ""),
        "old": entry.get("old"),
        "new": entry.get("new"),
    }


def _portal_sources(project_ids: list[str]) -> dict[str, list[dict]]:
    """project_id -> [{portal, invitation_id, agency, bid_number, ...}] from
    rfp_portal_invitations (docs/RFP_NGEM_PORTAL.md section 5, docs/
    RFP_BUILDINGCONNECTED.md 3.5): the invitations resolved to `exists` on
    the project, plus the BuildingConnected one that created it (contract
    D32). Returns {} at once unless RFP_INGESTION_ENABLED and at least one
    portal switch are on (no query against a table a deployment without
    migration 0126 does not have), and drops the rows of a portal whose own
    switch is off; read through `getattr` so an absent flag fails closed.
    The change list is normalized to `changes: [{at, field, old, new}]`."""
    settings = get_settings()
    if not project_ids or not _portal_any_enabled(settings):
        return {}
    from app.services import rfp_portal_ingest

    raw = rfp_portal_ingest.portal_sources_for_projects(get_supabase(), list(project_ids)) or {}
    out: dict[str, list[dict]] = {}
    for pid, rows in raw.items():
        kept = []
        for row in rows or []:
            flag = _PORTAL_FLAGS.get(str(row.get("portal") or ""))
            if flag and not getattr(settings, flag, False):
                continue
            shaped = {k: v for k, v in row.items() if k != "change_log"}
            if "changes" in row or "change_log" in row:
                changes = row.get("changes")
                if changes is None:
                    changes = row.get("change_log")
                shaped["changes"] = [
                    _portal_change(c) for c in (changes or []) if isinstance(c, dict)
                ]
            kept.append(shaped)
        out[pid] = kept
    return out


def _redact_portal_sources(sources: list[dict], role: Role) -> list[dict]:
    """The portal rows for a role: the close date (the GC's due date, the
    actual bid date's twin) and the values of a `close_at` change leave the
    server only for the roles that may see the actual bid date; everyone
    else still learns WHICH field moved (contract D8, D32)."""
    if role in ACTUAL_BID_VIEWER_ROLES:
        return list(sources)
    out = []
    for row in sources:
        shaped = {**row, "close_at": None}
        if "changes" in row:
            shaped["changes"] = [
                {**c, "old": None, "new": None} if c.get("field") == "close_at" else dict(c)
                for c in (row.get("changes") or [])
            ]
        out.append(shaped)
    return out


# The rfp_created_projects columns the project header and the intake rule need.
_RFP_CREATED_SELECT = (
    "project_id, automatic, sender_was_unauthorized, sender_display, sender_allowed_by, "
    "files_status, files_promoted, bid_time_unknown, invitation_method, gc_plan, "
    "source_kind, rfp_email_id, likely_gc_id, likely_gc_name, "
    # The splitter flag (docs/RFP_SPLIT.md 10; 0143): the harvest it reads
    # and the "split outside the app" resolution.
    "harvest_id, split_status, split_job_id, split_resolution, split_resolved_by, split_resolved_at"
)


def _rfp_created_rows(project_ids: list[str], *, with_mailbox: bool = True) -> dict[str, dict]:
    """project_id -> the rfp_created_projects row (docs/RFP_CREATE.md sections
    7 and 8) for every listed project this slice created, with the allowing
    user's name resolved onto `sender_allowed_by_name` and, unless
    `with_mailbox` is off, the receiving mailbox of an email source onto
    `mailbox`. One `in_()` query over the page's ids plus at most one profiles
    read and one rfp_emails read, never per project. Returns {} at once while
    RFP_INGESTION_ENABLED is off (no query against a table a deployment
    without migration 0130 does not have); read through `getattr` so an
    absent flag fails closed."""
    if not project_ids or not getattr(get_settings(), "rfp_ingest_enabled", False):
        return {}
    sb = get_supabase()
    rows = (
        sb.table("rfp_created_projects")
        .select(_RFP_CREATED_SELECT)
        .in_("project_id", list(project_ids))
        .execute()
    ).data or []
    if not rows:
        return {}
    names = _profile_names(sb, (r.get("sender_allowed_by") for r in rows))
    mailboxes = _rfp_email_mailboxes(sb, (r.get("rfp_email_id") for r in rows)) if with_mailbox else {}
    out: dict[str, dict] = {}
    for r in rows:
        pid = r.get("project_id")
        if not pid:
            continue
        out[pid] = {
            **r,
            "sender_allowed_by_name": names.get(r.get("sender_allowed_by") or ""),
            "mailbox": mailboxes.get(r.get("rfp_email_id") or ""),
        }
    return out


def _rfp_email_mailboxes(sb, ids) -> dict[str, str | None]:
    """rfp_emails id -> its primary (receiving) mailbox, one `in_()` read."""
    clean = sorted({i for i in ids if i})
    if not clean:
        return {}
    rows = (
        sb.table("rfp_emails").select("id, primary_mailbox").in_("id", clean).execute()
    ).data or []
    return {r["id"]: r.get("primary_mailbox") for r in rows if r.get("id")}


def _rfp_created_summary(row: dict) -> dict:
    """The RfpCreatedSummary wire shape off a _rfp_created_rows entry."""
    return {
        "automatic": bool(row.get("automatic")),
        "sender_was_unauthorized": bool(row.get("sender_was_unauthorized")),
        "sender_display": row.get("sender_display"),
        "sender_allowed_by_name": row.get("sender_allowed_by_name"),
        "files_status": row.get("files_status") or "none",
        "files_promoted": int(row.get("files_promoted") or 0),
        "bid_time_unknown": bool(row.get("bid_time_unknown")),
        "invitation_method": row.get("invitation_method"),
        "gc_plan": row.get("gc_plan"),
        "source_kind": row.get("source_kind"),
        # Email sources only; a portal source has no receiving mailbox.
        "mailbox": row.get("mailbox") if row.get("source_kind") == "rfp_email" else None,
        "likely_gc_id": row.get("likely_gc_id"),
        "likely_gc_name": row.get("likely_gc_name"),
        # Detail route only (`_split_issue_for`); the list leaves it null.
        "split_issue": row.get("split_issue"),
    }


def _split_issue_for(row: dict | None) -> dict | None:
    """The Bid File Splitter flag for the project page (docs/RFP_SPLIT.md
    10), computed the way the Created from RFP Ingestion page does it. Never
    fails the project read: trouble answers None (no banner)."""
    if not row:
        return None
    try:
        from app.routers.rfp_created import issue_for_project

        return issue_for_project(get_supabase(), row)
    except Exception:  # noqa: BLE001 - a banner, never a gate on the project page
        logger.exception("projects: split flag failed for %s", row.get("project_id"))
        return None


def _present(
    project: dict,
    role: Role,
    cat_state: dict[str, dict] | None = None,
    pending_gc_pricing: int | None = None,
    rfp_counts: dict | None = None,
    portal_sources: list[dict] | None = None,
    rfp_created: dict | None = None,
) -> dict:
    """Attach the derived lifecycle `status` (from the embedded bid outcome, if
    any) plus the per-category `category_state`, and redact. Pass every returned
    project row through here so the API `status` field stays consistent with the
    dashboard/analytics derivation. `pending_gc_pricing` is the 0118 approval
    count for the dashboard and `rfp_counts` the {merged, history, new} match
    counts of docs/RFP_MATCHING.md 3.9 (each left at the schema default when
    not supplied: create, update, abandon, reactivate and bids-today leave
    them, and every consumer of those responses refetches). `rfp_created` is
    the project's rfp_created_projects row when the RFP creation step made
    it (docs/RFP_CREATE.md section 7): it fills `rfp_created` and the
    `rfp_intake_missing` list, which is computed before redaction because the
    bid-time rule reads the actual bid date, and which drops `bid_time` for
    a role that may not see that date."""
    outcome = project.pop("bid_outcomes", None)
    # The projects↔bid_outcomes FK is unique, so PostgREST may embed it as a
    # single object (to-one) or a list depending on version — handle both.
    if isinstance(outcome, list):
        result = outcome[0].get("result") if outcome else None
    elif isinstance(outcome, dict):
        result = outcome.get("result")
    else:
        result = None
    project["status"] = derive_status(
        project.get("current_stage"), project.get("abandoned_at"), result
    )
    if cat_state is not None:
        project["category_state"] = _serialize_cat_state(cat_state)
    if pending_gc_pricing is not None:
        project["gc_pricing_approvals_pending"] = pending_gc_pricing
    if rfp_counts is not None:
        project["rfp_merged_count"] = int(rfp_counts.get("merged") or 0)
        project["rfp_history_count"] = int(rfp_counts.get("history") or 0)
        project["rfp_new_count"] = int(rfp_counts.get("new") or 0)
    if portal_sources is not None:
        # The portal invitations resolved onto (or, for BuildingConnected,
        # that created) the project (detail route only; the dashboard list
        # is unchanged, docs/RFP_NGEM_PORTAL.md section 5), with the close
        # date and its changes redacted like the actual bid date.
        project["portal_sources"] = _redact_portal_sources(list(portal_sources), role)
    if rfp_created is not None:
        project["rfp_created"] = _rfp_created_summary(rfp_created)
        missing = missing_intake_fields(
            project, bid_time_unknown=bool(rfp_created.get("bid_time_unknown"))
        )
        if role not in ACTUAL_BID_VIEWER_ROLES:
            # The actual bid date is redacted for this role, so nothing about
            # it (that its time is unknown) leaves the server either.
            missing = [key for key in missing if key != "bid_time"]
        project["rfp_intake_missing"] = missing
    return redact_for_role(project, role)


def _fetch_project_with_outcome(project_id: str) -> dict:
    """Load a project plus its (0-or-1) bid outcome so `status` is fully derivable.
    A missing project is a 404: `.single()` raises PGRST116 on zero rows (a
    deleted project's stale tab or link, docs/PROJECT_DELETE.md)."""
    try:
        resp = (
            get_supabase()
            .table("projects")
            .select("*, bid_outcomes(result)")
            .eq("id", project_id)
            .single()
            .execute()
        )
    except APIError as exc:
        if getattr(exc, "code", None) == "PGRST116":
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from exc
        raise
    if not resp.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return resp.data


@router.get("", response_model=list[ProjectOut])
def list_projects(
    stage: str | None = None,
    user: CurrentUser = Depends(get_current_user),
):
    """Dashboard list. Estimators never see the full list (assigned-only)."""
    if user.role == Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators use /estimator/projects")
    if stage is not None and stage not in workflow.STAGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown stage: {stage}")
    query = get_supabase().table("projects").select("*, bid_outcomes(result)")
    if stage is not None:
        # Abandon preserves current_stage (so we always know where the bid died),
        # which means a stage-filtered list would still serve abandoned bids. A
        # stage filter asks for that stage's work queue (e.g. the Go/No-Go page),
        # and an abandoned project is on no one's plate — read the marker here.
        # The unfiltered dashboard list keeps them (with their abandoned status).
        query = query.eq("current_stage", stage).is_("abandoned_at", "null")
    else:
        # Projects created directly in Project Management (pm_only) or imported
        # from the legacy Certified Payroll app (cp_only) were never bids — keep
        # them off every bidding surface (dashboard, go/no-go). Explicitly asking
        # for ?stage=pm_only / ?stage=cp_only still returns them.
        query = query.neq("current_stage", "pm_only").neq("current_stage", "cp_only")
    resp = query.order("created_at", desc=True).execute()
    rows = resp.data or []
    ids = [p["id"] for p in rows]
    states = workflow.load_category_states(ids)
    pending = _pending_gc_pricing_counts(ids)
    rfp = _rfp_match_counts(ids, {p["id"]: p for p in rows}, states)
    created = _rfp_created_rows(ids)
    return [
        _present(
            p,
            user.role,
            states.get(p["id"]),
            pending.get(p["id"], 0),
            rfp.get(p["id"]),
            rfp_created=created.get(p["id"]),
        )
        for p in rows
    ]


# Registered before GET /{project_id} so the literal path wins the match.
_SENT_STAGES = ("submitted", "bid_outcome")


def _bids_today_day_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """(start, end) of the office's current calendar day, as aware UTC."""
    now = now or datetime.now(timezone.utc)
    start = _day_start(now.astimezone(REPORT_TZ).date())
    return start, start + timedelta(days=1)


@router.get("/today", response_model=list[BidsTodayProjectOut], dependencies=_BIDDING_ONLY)
def bids_today(user: CurrentUser = Depends(get_current_user)):
    """The Bids Today page: every live bid whose internal due date has arrived
    (office calendar) and which hasn't gone out yet — however overdue — plus
    bids that went out earlier today, which stay for the rest of the day with
    `sent_today` set and drop off tomorrow.

    Membership is driven by internal_bid_at ONLY, never actual_bid_at: if the
    confidential actual date decided when a row appeared, a non-privileged user
    could read it off the calendar (services/bid_invitations applies the same
    rule). Privileged roles just see the actual_bid_at column that
    redact_for_role already leaves in place for them.
    """
    if user.role == Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators use /estimator/projects")
    sb = get_supabase()
    day_start, day_end = _bids_today_day_bounds()
    resp = (
        sb.table("projects")
        .select("*, bid_outcomes(result)")
        .neq("current_stage", "pm_only")
        .neq("current_stage", "cp_only")
        .neq("current_stage", "declined")
        .is_("abandoned_at", "null")
        .not_.is_("internal_bid_at", "null")
        .lt("internal_bid_at", day_end.isoformat())
        .order("internal_bid_at")
        .execute()
    )
    rows = resp.data or []

    # A bid past send-out is on the page only if it went out today. "Sent" is
    # the entry into `submitted` (advance_category emits the stage_event for
    # both the email send and mark-as-submitted), so a same-day win/loss record
    # moving the head to bid_outcome doesn't hide the row early.
    sent_today_ids: set[str] = set()
    already_sent = [p["id"] for p in rows if p["current_stage"] in _SENT_STAGES]
    if already_sent:
        ev = (
            sb.table("stage_events")
            .select("project_id")
            .eq("to_stage", "submitted")
            .gte("entered_at", day_start.isoformat())
            .in_("project_id", already_sent)
            .execute()
        )
        sent_today_ids = {e["project_id"] for e in (ev.data or [])}

    keep = [
        p for p in rows
        if p["current_stage"] not in _SENT_STAGES or p["id"] in sent_today_ids
    ]
    states = workflow.load_category_states([p["id"] for p in keep])
    return [
        {**_present(p, user.role, states.get(p["id"])), "sent_today": p["id"] in sent_today_ids}
        for p in keep
    ]


# ── Next project number preview (docs/RFP_CREATE.md section 2) ─────────────
# Registered before the /{project_id} routes (literal-before-param rule).


@router.get(
    "/next-number", dependencies=_BIDDING_ONLY + [Depends(next_number_rate_limit)]
)
def next_project_number(_: CurrentUser = Depends(require_writer)) -> dict:
    """The number the New Project form will get on save, and its budgetary
    spelling: `{"number": "26.9.7204", "budgetary_number": "26.9.7204B"}`.
    A preview only: the counter is not advanced, so two people opening the
    form see the same value and the save decides who gets it."""
    number = project_numbers.preview(get_supabase())
    return {"number": number, "budgetary_number": number + "B"}


# ── New Bid similar-projects check (docs/RFP_MATCHING.md 3.9) ──────────────
# Registered before the /{project_id} routes (literal-before-param rule).

# Every column the response item needs, plus the GC names embedded. The
# actual bid date is read only so redact_for_role can decide who sees it;
# nothing else here (window, score, order) ever looks at it.
_SIMILAR_SELECT = (
    "id, name, number, current_stage, abandoned_at, internal_bid_at, actual_bid_at, "
    "project_gcs(general_contractors(name)), bid_outcomes(result)"
)


def _parse_ts(value) -> datetime | None:
    """An aware datetime from a PostgREST timestamptz string (or None)."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _outcome_result(outcome) -> str | None:
    """The bid outcome result off a PostgREST embed (to-one object or list)."""
    if isinstance(outcome, list):
        return outcome[0].get("result") if outcome else None
    if isinstance(outcome, dict):
        return outcome.get("result")
    return None


def _similar_item(row: dict, score: float, conflict, role: Role) -> dict:
    """One response item: id, name, number, status, current_stage, the two
    bid dates (the actual one redacted by role), gc_names and the name-only
    breakdown. The key names are the ones SimilarProjectsModal parses."""
    gcs = sorted(
        {
            (link.get("general_contractors") or {}).get("name")
            for link in (row.get("project_gcs") or [])
            if (link.get("general_contractors") or {}).get("name")
        }
    )
    item = {
        "id": row.get("id"),
        "name": row.get("name"),
        "number": row.get("number"),
        "status": derive_status(
            row.get("current_stage"), row.get("abandoned_at"), _outcome_result(row.get("bid_outcomes"))
        ),
        "current_stage": row.get("current_stage"),
        "internal_bid_at": row.get("internal_bid_at"),
        "actual_bid_at": row.get("actual_bid_at"),
        "gc_names": gcs,
        "breakdown": {"name": score, "total": score, "conflict": conflict},
    }
    return redact_for_role(item, role)


@router.post(
    "/similar", dependencies=_BIDDING_ONLY + [Depends(similar_projects_rate_limit)]
)
def similar_projects(
    body: SimilarProjectsIn, user: CurrentUser = Depends(require_writer)
) -> dict:
    """The New Bid form's pre-create check: `{similar, possible_rebids}`.

    Name-only, no LLM, advisory (the form fails open on any error). One query
    on `internal_bid_at >= now() - RFP_MATCH_REBID_LOOKBACK_DAYS` with the
    candidate exclusions (abandoned, declined, PM-only, CP-only), built by
    `rfp_match.precreate_query` (never `candidate_query`, whose or-group
    reads the actual date). `similar` is the slice inside
    RFP_MATCH_PRECREATE_WINDOW_DAYS scored with `name_score` at the precreate
    threshold; `possible_rebids` is `rebid_lookup` over the older slice. The
    window is the internal date, never coalesced with the actual date, so
    membership is a function of a date every caller can see, and no score,
    membership or sort order in the response depends on the actual date.
    Nothing is stored.
    """
    settings = get_settings()
    rm = _match()
    now = datetime.now(timezone.utc)
    lookback_lo = now - timedelta(days=settings.rfp_match_rebid_lookback_days)
    window_lo = now - timedelta(days=settings.rfp_match_precreate_window_days)
    rows = (
        rm.precreate_query(get_supabase(), rm.pg_ts(lookback_lo), select=_SIMILAR_SELECT)
        .execute()
    ).data or []

    recent: list[dict] = []
    older: list[dict] = []
    for row in rows:
        bid_at = _parse_ts(row.get("internal_bid_at"))
        if bid_at is None:
            continue
        (recent if bid_at >= window_lo else older).append(row)

    scored = []
    for row in recent:
        ns = rm.name_score(
            body.name, row.get("name") or "", b_number=row.get("number"), settings=settings
        )
        if ns.score >= settings.rfp_match_precreate_threshold:
            scored.append((ns, row))
    scored.sort(key=lambda pair: (-pair[0].score, (pair[1].get("name") or "").lower()))
    similar = [_similar_item(row, ns.score, ns.conflict, user.role) for ns, row in scored]

    rebids: list[dict] = []
    hit = rm.rebid_lookup(body.name, older, settings) if older else None
    if hit:
        rebid_id, rebid_score = hit
        row = next((r for r in older if r.get("id") == rebid_id), None)
        if row is not None:
            ns = rm.name_score(
                body.name, row.get("name") or "", b_number=row.get("number"), settings=settings
            )
            rebids.append(_similar_item(row, rebid_score, ns.conflict, user.role))
    return {"similar": similar, "possible_rebids": rebids}


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED,
             dependencies=_BIDDING_ONLY)
def create_project(
    body: ProjectCreate,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_writer),
):
    """Create a project (typically the Estimating Admin). Starts in `intake`.
    The number is assigned here, not typed (docs/RFP_CREATE.md section 2):
    `budgetary` only decides whether it carries the B marker."""
    sb = get_supabase()
    payload = body.model_dump(exclude={"gcs", "budgetary"}, mode="json")
    payload["created_by"] = user.id
    payload["current_stage"] = "intake"
    payload["current_owner_role"] = Role.ESTIMATING_ADMIN.value
    try:
        created = project_numbers.insert_with_assigned_number(
            sb, payload, budgetary=body.budgetary
        )
    except project_numbers.NoFreeNumberError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    # All-or-nothing from here: the statements below auto-commit one by one, so
    # a failure part-way would otherwise strand a live project the client was
    # told does NOT exist — it shows on every dashboard, its number is retired,
    # and the due-reminder poller emails the team about it. If anything the
    # client needs fails, delete the row (cascade cleans children) and re-raise
    # so "creation failed" is actually true.
    try:
        if body.gcs:
            links = (
                sb.table("project_gcs").insert(
                    [
                        {"project_id": created["id"], "gc_id": g.gc_id,
                         "needs_by": g.needs_by.isoformat() if g.needs_by else None}
                        for g in body.gcs
                    ]
                ).execute()
            ).data or []
            link_by_gc = {r["gc_id"]: r["id"] for r in links}
            selection_rows = []
            for g in body.gcs:
                if not g.contact_ids or g.gc_id not in link_by_gc:
                    continue
                _assert_contacts_belong_to_gc(sb, g.gc_id, g.contact_ids)
                selection_rows.extend(
                    {"project_gc_id": link_by_gc[g.gc_id], "gc_contact_id": cid}
                    for cid in g.contact_ids
                )
            if selection_rows:
                sb.table("project_gc_contacts").insert(selection_rows).execute()

        # Record the initial stage event so analytics has a start timestamp.
        sb.table("stage_events").insert(
            {"project_id": created["id"], "from_stage": None, "to_stage": "intake",
             "category": "intake", "actor_id": user.id}
        ).execute()
        # Seed the 4-category state: intake active at its first task, the rest locked.
        sb.table("project_category_state").insert(
            [
                {
                    "project_id": created["id"],
                    "category": cat,
                    "current_task": workflow.CATEGORY_TASKS[cat][0],
                    "status": "active" if cat == "intake" else "locked",
                    "owner_role": (
                        workflow.owner_role_for(workflow.CATEGORY_TASKS[cat][0]) or None
                    ),
                }
                for cat in workflow.CATEGORY_ORDER
            ]
        ).execute()
        audit(user.id, "project.create", "project", created["id"], {"number": created["number"]})
        cat_state = workflow.load_category_state(created["id"])
    except Exception:
        try:
            sb.table("projects").delete().eq("id", created["id"]).execute()
        except Exception:  # noqa: BLE001 — the original error still propagates
            logger.exception(
                "Compensating delete failed; half-created project %s remains",
                created["id"],
            )
        raise
    # Learn-back: re-scan Unknown emails against the new project (bid invites
    # often arrive before the project exists). Best-effort, never raises.
    background.add_task(email_ingest.rescan_unknown_for_project, created["id"])
    return _present(created, user.role, cat_state)


_DISCARD_BLOCKED = (
    "Only a just-created project that hasn't started intake can be discarded. "
    "Use Abandon for anything further along."
)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=_BIDDING_ONLY)
def discard_project(project_id: str, user: CurrentUser = Depends(require_writer)):
    """Discard a project whose creation never finished.

    The New Project modal creates the row first and uploads staged files after,
    so a failed upload can strand a live project its creator never considered
    created — one the reminder poller will happily email the team about. This
    is the cleanup path for exactly that window: only the creator may call it,
    and only while the project is still at the very first intake task with
    nothing else hanging off it. Everything further along goes through Abandon,
    which keeps the record instead of erasing it.
    """
    sb = get_supabase()
    rows = (
        sb.table("projects")
        .select(
            "id, number, created_by, current_stage, pm_stage, cp_enrolled_at, "
            "abandoned_at"
        )
        .eq("id", project_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    project = rows[0]
    if project.get("created_by") != user.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only the project's creator can discard it"
        )
    intake = workflow.load_category_state(project_id).get("intake") or {}
    if (
        project["current_stage"] != "intake"
        or project.get("pm_stage") is not None
        or project.get("cp_enrolled_at") is not None
        # A withdrawn project is a record Abandon exists to keep, not a
        # creation leftover — reactivate it instead of erasing it.
        or project.get("abandoned_at") is not None
        or intake.get("current_task") != workflow.CATEGORY_TASKS["intake"][0]
        or intake.get("status") != "active"
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, _DISCARD_BLOCKED)
    # Row first, and conditionally (the files.py estimator-delete pattern): the
    # checks above are reads, so a teammate's transition landing between them
    # and this statement must make the delete match nothing rather than cascade
    # their work away. The intake-lane check can't ride along, but a transition
    # out of the first intake task also moves current_stage off 'intake' (a
    # separate autocommitted statement moments later), so the stage predicate
    # shrinks that window to the gap between the two — milliseconds, the best
    # available without transactions.
    deleted = (
        sb.table("projects")
        .delete()
        .eq("id", project_id)
        .eq("created_by", user.id)
        .eq("current_stage", "intake")
        .is_("pm_stage", "null")
        .is_("cp_enrolled_at", "null")
        .is_("abandoned_at", "null")
        .execute()
    ).data
    if not deleted:
        raise HTTPException(status.HTTP_409_CONFLICT, _DISCARD_BLOCKED)
    # Storage objects don't cascade with the row — sweep everything under the
    # project's prefix (uploads + preview derivatives). AFTER the delete, so a
    # sweep failure only orphans invisible objects instead of tearing files out
    # of a still-live project; by prefix, not project_files rows, so an upload
    # racing the discard can't slip an unrecorded object past the cleanup.
    try:
        storage.delete_project_prefix(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("Storage sweep failed for discarded project %s", project_id)
    audit(user.id, "project.discard", "project", project_id, {"number": project["number"]})


# ── Delete, recoverably (docs/PROJECT_DELETE.md 6) ──────────────────────────
# Not the creator-only DELETE /{project_id} above (a creation leftover, no
# record kept): this removes any bid-side project with a full archive an IT
# Admin can restore from /deleted-projects, and blocks the RFP source that
# made it. Estimating Admin, Executive and IT Admin only.


def _delete_error(exc: project_delete.ProjectDeleteError) -> HTTPException:
    headers = {"X-Error-Code": f"project_delete_{exc.code}"} if exc.code else None
    return HTTPException(exc.status_code, str(exc), headers=headers)


def _uuid_or_404(project_id: str) -> str:
    try:
        uuid.UUID(str(project_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found") from None
    return str(project_id)


@router.get("/{project_id}/delete-info", dependencies=_BIDDING_ONLY + [Depends(project_delete_rate_limit)])
def project_delete_info(project_id: str, _: CurrentUser = Depends(require_project_delete)) -> dict:
    """What the Delete modal shows: the text to type (the name, or the number
    when there is no name), whether RFP ingestion made the project, why it
    cannot be deleted (Project Management, Certified Payroll, or an outside
    record that points at it), what a delete would remove, and the reasons
    offered (`rfp_should_not_exist` only for an RFP-created project)."""
    _uuid_or_404(project_id)
    try:
        return project_delete.delete_info(get_supabase(), project_id)
    except project_delete.ProjectDeleteError as exc:
        raise _delete_error(exc) from exc


@router.post("/{project_id}/delete", dependencies=_BIDDING_ONLY + [Depends(project_delete_rate_limit)])
def archive_delete_project(
    project_id: str, body: ProjectDeleteIn, user: CurrentUser = Depends(require_project_delete),
) -> dict:
    """Delete the project with a full archive (one transaction in the
    database). `confirm_name` must equal the project's trimmed name (its
    number when it has none) exactly; the database checks it again under the
    row lock, so a stray call cannot delete. 422 on a name mismatch, a bad
    reason, `other` without a note, or the RFP reason on a project RFP
    ingestion did not make; 409 when the project is in Project Management or
    Certified Payroll. Nothing is emailed. Audited `project.delete`."""
    _uuid_or_404(project_id)
    try:
        return project_delete.delete_project(
            get_supabase(), project_id, actor_id=user.id, reason=body.reason, note=body.note,
            confirm_name=body.confirm_name,
        )
    except project_delete.ProjectDeleteError as exc:
        raise _delete_error(exc) from exc


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    project = _fetch_project_with_outcome(project_id)
    cat_state = workflow.load_category_state(project_id)
    rfp = _rfp_match_counts([project_id], {project_id: project}, {project_id: cat_state})
    portal = _portal_sources([project_id])
    created = _rfp_created_rows([project_id])
    row = created.get(project_id)
    if row is not None:
        row = {**row, "split_issue": _split_issue_for(row)}
    return _present(
        project,
        user.role,
        cat_state,
        _pending_gc_pricing_counts([project_id]).get(project_id, 0),
        rfp.get(project_id),
        portal.get(project_id, []),
        rfp_created=row,
    )


# Who may edit each intake field via PATCH. Any writer role can edit any field
# (the read-only accountant and the estimator are already rejected by the route
# guard), with one exception: the confidential ACTUAL bid date may only be edited
# by roles allowed to see it (the accountant can view it but not write it).
# Pricing is never patchable here — it lives in the quote/labor/markup steps.
_OPEN = WRITER_ROLES
_FIELD_EDITORS: dict[str, frozenset[Role]] = {
    "internal_bid_at": _OPEN,
    "actual_bid_at": ACTUAL_BID_EDITOR_ROLES,
    "due_from_estimator_at": _OPEN,
    "due_from_vendors_at": _OPEN,
    "est_start_date": _OPEN,
    "est_finish_date": _OPEN,
    "labor_time": _OPEN,
    "wage_type": _OPEN,
    "labor_note": _OPEN,
    "address": _OPEN,
    "bidding_url": _OPEN,
    "no_bidding_url": _OPEN,
    "name": _OPEN,
    # The Budgetary toggle (docs/RFP_CREATE.md section 2): the only way the
    # number changes after creation, and only by its B marker.
    "budgetary": _OPEN,
    "invitation_at": _OPEN,
    "notes": _OPEN,
    "is_ngem": _OPEN,
    # RFP matching (docs/RFP_MATCHING.md 3.9): the GC's bid instructions and
    # the manual rebid flag, both on the project details modal.
    "bid_notes": _OPEN,
    "is_rebid": _OPEN,
    # BuildingConnected facts (docs/RFP_BUILDINGCONNECTED.md 3.7): the job
    # walk and the two "Project details" text blocks, on the same modal.
    "job_walk_at": _OPEN,
    "project_information": _OPEN,
    "trade_instructions": _OPEN,
    # Go/No-Go scoring answers (scored by services/gono at the go_no_go gate;
    # editing them later only changes the displayed score, decisions stand).
    "project_type": _OPEN,
    "owner_type": _OPEN,
    "labor_needed": _OPEN,
    "bid_method": _OPEN,
    "competitor_known": _OPEN,
    "gc_known": _OPEN,
    "subs_needed": _OPEN,
    "est_value_band": _OPEN,
    "scope_fit": _OPEN,
}


def _apply_bidding_url_rules(patch: dict) -> None:
    """Keep the bidding link and its "no link" flag from ever disagreeing.

    They're two halves of one answer, so a patch that sets either side clears
    the other — that way a client can send just the half it changed. The one
    thing a patch may not do is un-answer the question: clearing the URL without
    ticking "no link" would leave the project with neither.
    """
    if "bidding_url" not in patch and "no_bidding_url" not in patch:
        return
    if patch.get("bidding_url"):
        patch["no_bidding_url"] = False
    elif patch.get("no_bidding_url"):
        patch["bidding_url"] = None
    else:
        # The URL was cleared, or "no link" was turned off, with nothing supplied
        # in its place — either way the question ends up unanswered.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Provide a bidding link, or set no_bidding_url if the project has none",
        )


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: str,
    body: ProjectUpdate,
    user: CurrentUser = Depends(require_writer),
):
    # exclude_unset (not exclude_none) so an explicit null clears a field.
    patch = body.model_dump(exclude_unset=True, mode="json")
    if not patch:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")
    if patch.get("name", "") is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name cannot be cleared")
    _apply_bidding_url_rules(patch)
    denied = sorted(f for f in patch if user.role not in _FIELD_EDITORS.get(f, frozenset()))
    if denied:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Your role may not edit: {', '.join(denied)}",
        )
    # The Budgetary toggle is the one way the number changes: a rename of the
    # same number with its B marker added or stripped. A legacy number the
    # format cannot parse is left alone with a 409; a toggle that lands on
    # the number already stored is a no-op.
    budgetary = patch.pop("budgetary", None)
    if budgetary is not None:
        current_number = _stored_number(project_id)
        try:
            renamed = project_numbers.with_budgetary(current_number, budgetary)
        except project_numbers.LegacyNumberError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if renamed != current_number:
            patch["number"] = renamed
        elif not patch:
            return _present(_fetch_project_with_outcome(project_id), user.role)
    # The internal bid date is the deadline the whole team works against, so a
    # change to it is emailed to every internal user (bid_date_change_email).
    # Read the stored value first: the email states old and new, and a patch
    # that re-sends the same date must stay silent.
    previous_bid_at = (
        _stored_internal_bid_at(project_id) if "internal_bid_at" in patch else None
    )
    # A real move fans out one email per internal user, so it is charged to
    # the hourly outbound-email budget before anything is written.
    if "internal_bid_at" in patch and bid_date_change_email.bid_date_changed(
        previous_bid_at, patch["internal_bid_at"]
    ):
        check_outbound_email(user.id)
    try:
        updated = (
            get_supabase().table("projects").update(patch).eq("id", project_id).execute()
        ).data
    except Exception as exc:  # noqa: BLE001 — unique violation → number re-use
        if is_duplicate_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _NUMBER_TAKEN) from exc
        raise
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    audit(user.id, "project.update", "project", project_id, patch)
    if "internal_bid_at" in patch:
        current_bid_at = updated[0].get("internal_bid_at")
        if bid_date_change_email.bid_date_changed(previous_bid_at, current_bid_at):
            bid_date_change_email.queue_internal_bid_date_change(
                project_id, previous_bid_at, current_bid_at, user.id
            )
    if _settle_rfp_intake(project_id, updated[0], patch, user.id):
        return _present(_fetch_project_with_outcome(project_id), user.role)
    return _present(updated[0], user.role)


def _stored_number(project_id: str) -> str:
    """The project's number as stored, for the Budgetary rename; 404 when the
    project does not exist."""
    rows = (
        get_supabase()
        .table("projects")
        .select("number")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return rows[0].get("number") or ""


def _settle_rfp_intake(project_id: str, updated: dict, patch: dict, actor_id: str) -> bool:
    """After a PATCH on a project the RFP creation step made (docs/RFP_CREATE.md
    section 7): an edit of the actual bid date settles its time (the
    date-only marker clears), and once nothing is missing the Estimating
    Admin's "intake details needed" bell is dismissed and a score at the Go
    threshold auto-Goes the project (gono.auto_go_if_scored). One read of
    rfp_created_projects, skipped while RFP_INGESTION_ENABLED is off (as
    _rfp_created_rows is); best effort, the PATCH is already committed.
    Returns True when the project moved (so the caller re-reads it)."""
    try:
        created = _rfp_created_rows([project_id], with_mailbox=False).get(project_id)
        if created is None:
            return False
        bid_time_unknown = bool(created.get("bid_time_unknown"))
        if bid_time_unknown and "actual_bid_at" in patch:
            get_supabase().table("rfp_created_projects").update(
                {"bid_time_unknown": False}
            ).eq("project_id", project_id).execute()
            bid_time_unknown = False
        if not missing_intake_fields(updated, bid_time_unknown=bid_time_unknown):
            dismiss_notifications(project_id=project_id, types=["rfp_create.intake_needed"])
            return gono.auto_go_if_scored(project_id, actor_id) == "go"
    except Exception:  # noqa: BLE001
        logger.exception("RFP intake settlement failed after PATCH of %s", project_id)
    return False


def _stored_internal_bid_at(project_id: str) -> str | None:
    """The internal bid date as it stands before a PATCH rewrites it. A missing
    project reads as None here and 404s on the update that follows."""
    rows = (
        get_supabase()
        .table("projects")
        .select("internal_bid_at")
        .eq("id", project_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0].get("internal_bid_at") if rows else None


# ── Abandon / reactivate ────────────────────────────────────────────────────
# A status change that is NOT a category transition: abandon leaves the category
# state and headline current_stage untouched (so we know where the bid died) and
# only flips the abandon marker via a direct projects.update. Reversible via
# /reactivate. Both are open to any writer role (the read-only accountant and the
# estimator are rejected).


def _project_status_row(project_id: str) -> dict:
    row = (
        get_supabase()
        .table("projects")
        # `number` and the estimator due date ride along for the estimator-facing
        # withdrawn/reactivated notices, which name the project the way the
        # estimator knows it and restate the deadline when it comes back.
        .select(
            "id, name, number, current_stage, abandoned_at, pm_stage, "
            "due_from_estimator_at"
        )
        .eq("id", project_id)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return row[0]


def _sweep_estimator_notifications(project_id: str) -> None:
    """Clear the assigned estimators' bells for a project they can no longer open.

    Abandon closes the portal on them (`require_project_assignment` 403s and the
    portal dashboard hides the row), so any live notification would only deep-link
    into a dead end. Scoped per assignee — the internal team keeps its own bells,
    and only the estimator-facing types are touched, so a shared type like
    `estimator_note` doesn't sweep the internal side of the thread. Best-effort:
    the abandon is already committed and must not be undone by a failed sweep.
    """
    try:
        assignees = (
            get_supabase()
            .table("estimator_assignments")
            .select("estimator_id")
            .eq("project_id", project_id)
            .execute()
        ).data or []
        for est_id in {a["estimator_id"] for a in assignees if a.get("estimator_id")}:
            dismiss_notifications(
                project_id=project_id,
                types=ESTIMATOR_NOTIFICATION_TYPES,
                user_id=est_id,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Estimator notification sweep failed on abandon of %s", project_id)


@router.post("/{project_id}/abandon", response_model=ProjectOut, dependencies=_BIDDING_ONLY)
def abandon_project(
    project_id: str,
    body: AbandonIn | None = None,
    user: CurrentUser = Depends(require_writer),
):
    """Abandon a bid at its current stage. `current_stage` is preserved; the
    derived status becomes `abandoned`. Reversible via /reactivate."""
    existing = _project_status_row(project_id)
    if existing.get("abandoned_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is already abandoned")
    # Abandon is a BID lifecycle marker. A project that entered Project
    # Management (won, or created there directly) is no longer just a bid —
    # abandoning it would rewrite a won job's history to "abandoned". cp_only
    # rows (legacy Certified Payroll imports) were never bids at all.
    if existing.get("pm_stage") or existing.get("current_stage") in ("pm_only", "cp_only"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This project is not an active bid and can no longer be abandoned as one.",
        )
    now = datetime.now(timezone.utc).isoformat()
    updated = (
        get_supabase()
        .table("projects")
        .update({"abandoned_at": now, "abandoned_by": user.id})
        .eq("id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    note = body.note if body else None
    audit(user.id, "project.abandon", "project", project_id,
          {"stage": existing["current_stage"], "note": note})
    _sweep_estimator_notifications(project_id)
    # A dead bid is not an Executive task: any late-GC pricing request still
    # open drops out of the bell (the project_gcs row keeps its state).
    dismiss_notifications(project_id=project_id, types=["gc_pricing.approval_requested"])
    # AFTER the sweep: the withdrawn notice is the one estimator-facing row that
    # must survive it — it's what explains where the rest of their bells went.
    # The reason note is written in the modal as estimator-facing text, so it
    # rides along into their email (see estimator_lifecycle).
    estimator_lifecycle.notify_withdrawn(existing, note=note, actor_id=user.id)
    notify_role(Role.EXECUTIVE, project_id, "project_abandoned",
                f"Project abandoned: {existing['name']}")
    return _present(updated[0], user.role)


@router.post("/{project_id}/reactivate", response_model=ProjectOut, dependencies=_BIDDING_ONLY)
def reactivate_project(
    project_id: str,
    user: CurrentUser = Depends(require_writer),
):
    """Reactivate an abandoned project, returning it to its stage-derived status."""
    existing = _project_status_row(project_id)
    if not existing.get("abandoned_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Project is not abandoned")
    updated = (
        get_supabase()
        .table("projects")
        .update({"abandoned_at": None, "abandoned_by": None})
        .eq("id", project_id)
        .execute()
    ).data
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    audit(user.id, "project.reactivate", "project", project_id,
          {"stage": existing["current_stage"]})
    # The estimators were told it was dead; they get told it isn't. This also
    # clears the withdrawn bell, which is now false.
    estimator_lifecycle.notify_reactivated(existing, actor_id=user.id)
    # A bid abandoned at `submitted` can still have its outcome recorded as won;
    # PM entry is deferred until the project is revived — this is that moment.
    pm.activate_pm_if_won(project_id, user.id)
    # Re-fetch with the outcome embedded so a reactivated win/loss bid reports its
    # true status (won/lost), not just the abandon-free fallback.
    return _present(_fetch_project_with_outcome(project_id), user.role)


# ── project ↔ GC membership ────────────────────────────────────────────────
# Editable at ANY stage by any writer role (the read-only accountant and the
# estimator are rejected): GCs
# join and drop out of bids mid-pipeline, so membership can't be frozen at
# intake. Membership is the whole story — any GC on the project is a bid
# candidate; who we actually bid to is recorded by which proposals were sent.
# The send path is hardened against the set changing under it
# (assert_send_isolation re-verifies every row against the live GC).


def _project_or_404(project_id: str) -> dict:
    row = (
        get_supabase()
        .table("projects")
        .select("id, name, current_stage, abandoned_at")
        .eq("id", project_id)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return row[0]


@router.post("/{project_id}/opens", status_code=status.HTTP_204_NO_CONTENT)
def log_project_open(
    project_id: str,
    body: ProjectOpenIn,
    user: CurrentUser = Depends(require_internal),
):
    """Fire-and-forget page-open beacon (project page / Project Details box),
    feeding the Estimating Engineer (Labor) analytics. The frontend throttles
    itself, so a row reads as one visit. Internal roles only - the estimator
    portal never reaches these surfaces. The read-only accountant is a no-op:
    it never feeds the report (only labor-focused engineers count) and must
    not write."""
    if user.role == Role.ACCOUNTANT:
        return
    _project_or_404(project_id)
    get_supabase().table("project_open_events").insert(
        {"project_id": project_id, "user_id": user.id, "kind": body.kind}
    ).execute()


def _project_gc_rows(project_id: str) -> list[dict]:
    """Wire shape shared by the GET and returned from every membership write
    (the panel swaps its whole list for the response). Lives in proposal_send
    since the pricing-approval endpoints there return the same rows."""
    return proposal_send.project_gc_rows(project_id)


def _assert_contacts_belong_to_gc(sb, gc_id: str, contact_ids: list[str]) -> None:
    """400 unless every id is a live contact of THIS GC; the selection must
    never smuggle in people from another company."""
    if not contact_ids:
        return
    rows = (
        sb.table("gc_contacts").select("id").eq("gc_id", gc_id)
        .in_("id", contact_ids).execute()
    ).data or []
    if len(rows) != len(set(contact_ids)):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "One or more selected contacts do not belong to this GC (or no longer exist).",
        )


def _replace_gc_contact_selection(
    sb, project_gc_id: str, gc_id: str, contact_ids: list[str]
) -> None:
    """Swap the link row's selection for the given set ([] clears it)."""
    _assert_contacts_belong_to_gc(sb, gc_id, contact_ids)
    sb.table("project_gc_contacts").delete().eq("project_gc_id", project_gc_id).execute()
    if contact_ids:
        sb.table("project_gc_contacts").insert(
            [{"project_gc_id": project_gc_id, "gc_contact_id": cid} for cid in contact_ids]
        ).execute()


def _proposal_send_http(exc: proposal_send.ProposalSendError) -> HTTPException:
    """A refusal from the shared GC-removal helper, at its own status. The
    helper tags the one refusal the frontend acts on by code
    (`rfp_match_unmerge_required`: the GC was put there by RFP ingestion and
    only Unmerge may detach it); that code rides the X-Error-Code header."""
    code = getattr(exc, "code", None)
    return HTTPException(
        getattr(exc, "status_code", status.HTTP_409_CONFLICT),
        str(exc),
        headers={"X-Error-Code": str(code)} if code else None,
    )


@router.get("/{project_id}/gcs", dependencies=_BIDDING_ONLY)
def list_project_gcs(project_id: str, _: CurrentUser = Depends(require_internal)):
    _project_or_404(project_id)
    return _project_gc_rows(project_id)


@router.post("/{project_id}/gcs", status_code=status.HTTP_201_CREATED,
             dependencies=_BIDDING_ONLY)
def add_project_gc(
    project_id: str,
    body: ProjectGCIn,
    user: CurrentUser = Depends(require_writer),
):
    sb = get_supabase()
    project = _project_or_404(project_id)
    if project.get("abandoned_at"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This bid has been abandoned, so no GC can be added."
        )
    gc = (
        sb.table("general_contractors").select("id, name").eq("id", body.gc_id).execute()
    ).data
    if not gc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GC not found")
    existing = (
        sb.table("project_gcs")
        .select("id")
        .eq("project_id", project_id)
        .eq("gc_id", body.gc_id)
        .limit(1)
        .execute()
    ).data
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{gc[0]['name']} is already on this project"
        )
    link = (
        sb.table("project_gcs").insert(
            {"project_id": project_id, "gc_id": body.gc_id,
             "needs_by": body.needs_by.isoformat() if body.needs_by else None}
        ).execute()
    ).data[0]
    if body.contact_ids:
        _replace_gc_contact_selection(sb, link["id"], body.gc_id, body.contact_ids)
    audit(user.id, "project.gc_add", "project", project_id,
          {"gc_id": body.gc_id, "gc_name": gc[0]["name"],
           "needs_by": body.needs_by.isoformat() if body.needs_by else None,
           "contact_ids": body.contact_ids or None})
    return _project_gc_rows(project_id)


@router.patch("/{project_id}/gcs/{gc_id}", dependencies=_BIDDING_ONLY)
def update_project_gc(
    project_id: str,
    gc_id: str,
    body: ProjectGCUpdate,
    user: CurrentUser = Depends(require_writer),
):
    """Edit the link's mutable fields: the per-GC needs-by date and/or the
    project's preferred bid contacts at that GC. Each field only moves when its
    key is present in the PATCH body (membership itself is add/remove)."""
    sb = get_supabase()
    _project_or_404(project_id)
    rows = (
        sb.table("project_gcs")
        .select("id")
        .eq("project_id", project_id)
        .eq("gc_id", gc_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GC is not on this project")
    changes: dict = {}
    if "needs_by" in body.model_fields_set:
        needs_by = body.needs_by.isoformat() if body.needs_by else None
        sb.table("project_gcs").update({"needs_by": needs_by}).eq(
            "project_id", project_id
        ).eq("gc_id", gc_id).execute()
        changes["needs_by"] = needs_by
    if "contact_ids" in body.model_fields_set:
        _replace_gc_contact_selection(sb, rows[0]["id"], gc_id, body.contact_ids or [])
        changes["contact_ids"] = body.contact_ids or []
    if changes:
        audit(user.id, "project.gc_update", "project", project_id,
              {"gc_id": gc_id, **changes})
    return _project_gc_rows(project_id)


@router.delete("/{project_id}/gcs/{gc_id}", dependencies=_BIDDING_ONLY)
def remove_project_gc(
    project_id: str,
    gc_id: str,
    user: CurrentUser = Depends(require_writer),
):
    sb = get_supabase()
    _project_or_404(project_id)
    rows = (
        sb.table("project_gcs")
        .select("id")
        .eq("project_id", project_id)
        .eq("gc_id", gc_id)
        .execute()
    ).data
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GC is not on this project")
    # The shared helper (docs/RFP_MATCHING.md 3.7): retires never-sent
    # proposals first (the claim a concurrent send races against), deletes
    # the link unless a send is in flight, drops the GC's pending
    # pricing-approval notifications, and refuses a GC the matcher added
    # (409 rfp_match_unmerge_required: Unmerge is the only detach path). A
    # GC with a SENT proposal may still be removed by hand, as before.
    try:
        proposal_send.remove_gc_link(project_id, gc_id, refuse_if_sent=False)
    except proposal_send.ProposalSendError as exc:
        raise _proposal_send_http(exc) from exc
    audit(user.id, "project.gc_remove", "project", project_id, {"gc_id": gc_id})
    return _project_gc_rows(project_id)


# ── BuildingConnected: the files flag and the GC confirmation ────────────────
# (docs/RFP_BUILDINGCONNECTED.md 3.4 and 3.8, contract 3.5 and D19). The
# files flag lives on `projects` (0136) and needs no RFP table, so Dismiss
# carries the bidding gate only; the GC card reads the BuildingConnected
# invitation, so it rides the master RFP flag like the rfp-matches routes.

_BC_PORTAL = "buildingconnected"
_BC_INVITATION_SELECT = (
    "id, portal, status, gc_external_id, gc_external_name, gc_id, gc_kind, gc_candidates, "
    "gc_confirmed_at, lead, created_project_id, match_project_id, resolved_at, first_seen_at, change_log"
)
_NO_BC_INVITATION = "No BuildingConnected invitation is linked to this project"


@router.post("/{project_id}/files-needed/dismiss", response_model=ProjectOut,
             dependencies=_BIDDING_ONLY)
def dismiss_files_needed(project_id: str, user: CurrentUser = Depends(require_writer)):
    """Clear the "files were not pulled" flag by hand (D19). A conditional
    update: only a set-and-uncleared flag moves, audited when it does; a
    project whose flag is not set (or already cleared) just comes back as
    it is, so a second click is harmless. 404 for a missing project."""
    _project_or_404(project_id)
    _files_needed().dismiss(get_supabase(), project_id, user.id)
    return _present(_fetch_project_with_outcome(project_id), user.role)


def _bc_invitation_for_project(sb, project_id: str) -> dict | None:
    """The BuildingConnected invitation behind a project: a linked row
    whose own GC question is open (the GC swapped on the board, or its
    alias repointed or deleted, not answered since; the one raised last,
    rfp_bc_portal.card_invitation), else the one that created it, else the
    newest one merged onto it (`exists`)."""
    rows = (
        sb.table("rfp_portal_invitations")
        .select(_BC_INVITATION_SELECT)
        .eq("portal", _BC_PORTAL)
        .or_(f"created_project_id.eq.{project_id},match_project_id.eq.{project_id}")
        .execute()
    ).data or []
    if not rows:
        return None
    # The selection rule itself, not the confirm_gc seam (_bc_portal) the
    # tests stand in for.
    from app.services.rfp_bc_portal import card_invitation

    asking = card_invitation(rows, project_id)
    if asking is not None:
        return asking
    created = [r for r in rows if str(r.get("created_project_id") or "") == str(project_id)]
    if created:
        return created[0]
    merged = [r for r in rows if str(r.get("match_project_id") or "") == str(project_id)]
    merged.sort(key=lambda r: (str(r.get("resolved_at") or ""), str(r.get("first_seen_at") or "")),
                reverse=True)
    return merged[0] if merged else rows[0]


def _gc_confirm_view(sb, project: dict, inv: dict | None) -> dict:
    """The ProjectGcConfirmOut shape (contract 3.5)."""
    out = {
        "pending": bool(project.get("gc_confirm_pending")),
        "external_name": None,
        "provisional_gc": None,
        "candidates": [],
        "lead": None,
        "invitation_id": None,
        "portal": None,
    }
    if not inv:
        return out
    gc_id = inv.get("gc_id")
    gc_name = None
    if gc_id:
        rows = (
            sb.table("general_contractors").select("id, name").eq("id", gc_id).limit(1).execute()
        ).data or []
        gc_name = rows[0].get("name") if rows else None
    lead = inv.get("lead") if isinstance(inv.get("lead"), dict) else None
    if lead:
        name = " ".join(
            p for p in (str(lead.get("first_name") or "").strip(), str(lead.get("last_name") or "").strip()) if p
        )
        lead = {"name": name or None, "email": lead.get("email"), "phone": lead.get("phone")}
    candidates = inv.get("gc_candidates") or []
    out.update({
        "external_name": inv.get("gc_external_name"),
        "provisional_gc": {"id": gc_id, "name": gc_name} if gc_id else None,
        "candidates": [c for c in candidates if isinstance(c, dict)],
        "lead": lead,
        "invitation_id": inv.get("id"),
        "portal": inv.get("portal"),
    })
    return out


@router.get("/{project_id}/gc-confirm", response_model=ProjectGcConfirmOut,
            dependencies=_RFP_MATCH_DEPS)
def get_gc_confirm(project_id: str, user: CurrentUser = Depends(require_writer)):
    """The project-page "same GC?" card (docs/RFP_BUILDINGCONNECTED.md 3.4):
    whether the question is still open, BuildingConnected's company name,
    the GC the app attached provisionally, the top candidates, and the
    lead (writer roles only, which this route is). The bare 404 while the
    BuildingConnected portal is not served (RFP_BC_ENABLED off)."""
    _require_bc_served()
    sb = get_supabase()
    project = _project_or_404_with(sb, project_id, "id, gc_confirm_pending")
    return _gc_confirm_view(sb, project, _bc_invitation_for_project(sb, project_id))


@router.post("/{project_id}/gc-confirm", response_model=ProjectGcConfirmOut,
             dependencies=_RFP_MATCH_DEPS)
def post_gc_confirm(
    project_id: str,
    body: ProjectGcConfirmIn,
    user: CurrentUser = Depends(require_writer),
):
    """Answer the card: `same` confirms the provisional GC, `pick` names
    another, `create` adds one. Delegates to rfp_bc_portal.confirm_gc (the
    alias, the invitation's GC columns, the project's link swap and the
    pending flag) and answers with the card's new state. The bare 404
    while the BuildingConnected portal is not served (RFP_BC_ENABLED off),
    404 without a BuildingConnected invitation, 409 `rfp_portal_gc_required`
    when `same` has no provisional GC to confirm."""
    _require_bc_served()
    sb = get_supabase()
    project = _project_or_404_with(sb, project_id, "id, gc_confirm_pending")
    inv = _bc_invitation_for_project(sb, project_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _NO_BC_INVITATION)
    gc_id: str | None = None
    create: dict | None = None
    if body.decision == "same":
        gc_id = inv.get("gc_id")
        if not gc_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This invitation has no provisional GC to confirm; pick or add one.",
                headers={"X-Error-Code": "rfp_portal_gc_required"},
            )
    elif body.decision == "pick":
        gc_id = (body.gc_id or "").strip()
    else:
        create = body.create.model_dump() if body.create else None
    try:
        _bc_portal().confirm_gc(sb, inv["id"], user.id, gc_id=gc_id, create=create)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except proposal_send.ProposalSendError as exc:
        raise _proposal_send_http(exc) from exc
    project = _project_or_404_with(sb, project_id, "id, gc_confirm_pending")
    return _gc_confirm_view(sb, project, _bc_invitation_for_project(sb, project_id))


def _project_or_404_with(sb, project_id: str, select: str) -> dict:
    rows = (sb.table("projects").select(select).eq("id", project_id).limit(1).execute()).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return rows[0]


# ── Merged by System (docs/RFP_MATCHING.md 3.7, 3.7b, 3.9) ──────────────────
# The project side of the RFP matcher: every rfp_project_matches row for a
# project, plus unmerge and acknowledge. Match METADATA goes to every internal
# role; the email block (subject, sender, extracted facts, breakdown,
# candidates, weights) only to RFP_REVIEW_ROLES, so email content never
# leaves the review-queue role set (the accountant sees `email: null`).
# All three routes carry the master RFP flag and the bidding flag.

_RFP_MATCH_NOT_FOUND = "Match record not found"
_RFP_MATCH_EMAIL_SELECT = (
    "id, subject, from_address, from_name, received_at, extracted_project_name, "
    "extracted_gc_name, extracted_bid_due_at, extracted_bid_due_has_time, "
    "extracted_bid_notes"
)
# proposal_sends statuses that describe a live document (a superseded row
# reads as "not sent", the same reading proposal_send.project_gc_rows takes).
_LIVE_SEND_STATUSES = ("generated", "sending", "sent", "failed")


def _rfp_match_error(exc: LookupError) -> HTTPException:
    """An `RfpMatchError` (a LookupError carrying `.code`, docs/RFP_MATCHING.md
    section 12) maps to 409 under its own code (400 for
    rfp_match_gc_required, which never arises on these routes); a bare
    LookupError is the ordinary "moved on" 409 rfp_match_not_actionable."""
    code = getattr(exc, "code", None)
    code = str(code) if code else ErrorCode.RFP_MATCH_NOT_ACTIONABLE.value
    http_status = (
        status.HTTP_400_BAD_REQUEST
        if code == ErrorCode.RFP_MATCH_GC_REQUIRED
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(
        http_status,
        str(exc) or "This match record is no longer open to that action.",
        headers={"X-Error-Code": code},
    )


def _rfp_already_sent(exc: proposal_send.ProposalSendError) -> HTTPException:
    """The removal helper refused the unmerge because a proposal to that GC is
    sent or sending: the GC stays, the match row is untouched."""
    return HTTPException(
        status.HTTP_409_CONFLICT,
        str(exc) or "A proposal has already been sent to this GC; it cannot be unmerged.",
        headers={"X-Error-Code": ErrorCode.RFP_MATCH_GC_ALREADY_SENT.value},
    )


def _rfp_match_or_404(sb, project_id: str, match_id: str) -> dict:
    """The match row by id, and it must belong to THIS project: a match id
    from another project's modal is a 404 here, never acted on. A malformed
    id is refused before any query (a raw 22P02 would be a 500)."""
    try:
        uuid.UUID(str(match_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_404_NOT_FOUND, _RFP_MATCH_NOT_FOUND) from None
    rows = (
        sb.table("rfp_project_matches")
        .select("id, project_id")
        .eq("id", match_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows or rows[0].get("project_id") != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, _RFP_MATCH_NOT_FOUND)
    return rows[0]


def _profile_names(sb, ids) -> dict[str, str | None]:
    clean = sorted({i for i in ids if i})
    if not clean:
        return {}
    rows = (
        sb.table("profiles").select("id, full_name").in_("id", clean).execute()
    ).data or []
    return {r["id"]: r.get("full_name") for r in rows if r.get("id")}


def _rfp_match_items(sb, project_id: str, role: Role) -> list[dict]:
    """Every match row for the project, `decided_at desc`, in the wire shape
    of GET /projects/{id}/rfp-matches (also answered by unmerge and
    acknowledge, so the modal swaps its whole list for the response)."""
    rows = _ingest().match_rows_for_project(sb, project_id) or []
    if not rows:
        return []
    gcs = {
        r["id"]: r.get("name")
        for r in (
            sb.table("general_contractors")
            .select("id, name")
            .in_("id", sorted({r.get("gc_id") for r in rows if r.get("gc_id")}))
            .execute()
        ).data
        or []
    }
    links = (
        sb.table("project_gcs").select("id, gc_id").eq("project_id", project_id).execute()
    ).data or []
    gcs_on_project = {link.get("gc_id") for link in links}
    sends = (
        sb.table("proposal_sends")
        .select("gc_id, status")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    send_status = {
        s["gc_id"]: s["status"] for s in sends if s.get("status") in _LIVE_SEND_STATUSES
    }
    names = _profile_names(
        sb,
        {
            r.get(key)
            for r in rows
            for key in ("decided_by", "acknowledged_by", "unmerged_by")
        },
    )
    emails: dict[str, dict] = {}
    if role in RFP_REVIEW_ROLES:
        email_ids = sorted({r.get("rfp_email_id") for r in rows if r.get("rfp_email_id")})
        if email_ids:
            emails = {
                e["id"]: e
                for e in (
                    sb.table("rfp_emails")
                    .select(_RFP_MATCH_EMAIL_SELECT)
                    .in_("id", email_ids)
                    .execute()
                ).data
                or []
            }

    items = []
    for r in rows:
        gc_id = r.get("gc_id")
        proposal_status = send_status.get(gc_id) or "not_sent"
        open_merge = r.get("kind") == "merged" and not r.get("unmerged_at")
        if not open_merge:
            can_unmerge, blocked = False, "closed"
        elif role not in RFP_UNMERGE_ROLES:
            can_unmerge, blocked = False, "role"
        elif proposal_status in ("sent", "sending"):
            can_unmerge, blocked = False, "sent"
        else:
            can_unmerge, blocked = True, None
        weights = r.get("weights") if isinstance(r.get("weights"), dict) else {}
        email = None
        if role in RFP_REVIEW_ROLES:
            src = emails.get(r.get("rfp_email_id")) or {}
            email = {
                "id": r.get("rfp_email_id"),
                "subject": src.get("subject"),
                "from_address": src.get("from_address"),
                "from_name": src.get("from_name"),
                "received_at": src.get("received_at"),
                "extracted_project_name": src.get("extracted_project_name"),
                "extracted_gc_name": src.get("extracted_gc_name"),
                "extracted_bid_due_at": src.get("extracted_bid_due_at"),
                "extracted_bid_due_has_time": src.get("extracted_bid_due_has_time"),
                "extracted_bid_notes": src.get("extracted_bid_notes"),
                "breakdown": r.get("breakdown"),
                "candidates": r.get("candidates"),
                "weights": r.get("weights"),
            }
        items.append(
            {
                "id": r.get("id"),
                "rfp_email_id": r.get("rfp_email_id"),
                "kind": r.get("kind"),
                "gc_id": gc_id,
                "gc_name": gcs.get(gc_id),
                "gc_added": bool(r.get("gc_added")),
                "gc_on_project": gc_id in gcs_on_project,
                "project_gc_id": r.get("project_gc_id"),
                "proposal_status": proposal_status,
                "score": r.get("score"),
                "candidate_rank": r.get("candidate_rank"),
                "scorer_version": weights.get("scorer_version"),
                "decided_by": r.get("decided_by"),
                "decided_by_name": names.get(r.get("decided_by")),
                "decided_at": r.get("decided_at"),
                "acknowledged_by": r.get("acknowledged_by"),
                "acknowledged_by_name": names.get(r.get("acknowledged_by")),
                "acknowledged_at": r.get("acknowledged_at"),
                "acknowledge_reason": r.get("acknowledge_reason"),
                "unmerged_by": r.get("unmerged_by"),
                "unmerged_by_name": names.get(r.get("unmerged_by")),
                "unmerged_at": r.get("unmerged_at"),
                "unmerge_reason": r.get("unmerge_reason"),
                "can_unmerge": can_unmerge,
                "can_unmerge_reason": blocked,
                "gc_match_kind": r.get("gc_match_kind"),
                "sender_address": r.get("sender_address"),
                "invitation_method": r.get("invitation_method"),
                "email": email,
            }
        )
    if role not in ACTUAL_BID_VIEWER_ROLES:
        # Breakdowns store date KINDS, never values; this drops the `actual`
        # kind and nulls a date sub-score taken from the actual bid date.
        items = _match().redact_candidates(items, role)
    return items


@router.get("/{project_id}/rfp-matches", dependencies=_RFP_MATCH_DEPS)
def list_project_rfp_matches(
    project_id: str, user: CurrentUser = Depends(require_internal)
) -> list[dict]:
    """The "Merged by System" record: every match row for the project, newest
    decision first. Metadata for every internal role; `email` only for the
    review-queue roles."""
    _project_or_404(project_id)
    return _rfp_match_items(get_supabase(), project_id, user.role)


@router.post(
    "/{project_id}/rfp-matches/{match_id}/unmerge", dependencies=_RFP_MATCH_DEPS
)
def unmerge_project_rfp_match(
    project_id: str,
    match_id: str,
    body: RfpMatchUnmergeIn,
    user: CurrentUser = Depends(require_rfp_unmerge),
) -> list[dict]:
    """Reverse a system merge (docs/RFP_MATCHING.md 3.7): removes exactly the
    link the matcher added (by link id, never one a person added), closes the
    match row with who and why, excludes the project on the email and returns
    it to `match` for the next sweep. Refused with 409
    rfp_match_gc_already_sent once a proposal to that GC is sent or sending.
    The service audits the action; a retry after a crash finishes it."""
    sb = get_supabase()
    _project_or_404(project_id)
    _rfp_match_or_404(sb, project_id, match_id)
    try:
        _ingest().unmerge(sb, match_id, body.reason, user.id)
    except proposal_send.ProposalSendError as exc:
        raise _rfp_already_sent(exc) from exc
    except LookupError as exc:
        raise _rfp_match_error(exc) from exc
    return _rfp_match_items(sb, project_id, user.role)


@router.post(
    "/{project_id}/rfp-matches/{match_id}/acknowledge", dependencies=_RFP_MATCH_DEPS
)
def acknowledge_project_rfp_match(
    project_id: str,
    match_id: str,
    body: RfpMatchAcknowledgeIn | None = None,
    user: CurrentUser = Depends(require_writer),
) -> list[dict]:
    """"No proposal needed" (docs/RFP_MATCHING.md 3.7b): stamps the open merge
    as acknowledged, which clears the project's "New RFP" pill. The GC stays,
    the email stays at `merged`, and a later Send proposal or unmerge (until
    sent) is still allowed. 409 once the GC has a sent or sending proposal
    (nothing to acknowledge) or the row is closed or already acknowledged."""
    sb = get_supabase()
    _project_or_404(project_id)
    _rfp_match_or_404(sb, project_id, match_id)
    reason = body.reason if body else None
    try:
        _ingest().acknowledge_match(sb, match_id, reason, user.id)
    except LookupError as exc:
        raise _rfp_match_error(exc) from exc
    return _rfp_match_items(sb, project_id, user.role)

