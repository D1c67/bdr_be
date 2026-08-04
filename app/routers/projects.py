"""Projects + intake (step 1) and the dashboard list."""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_internal, require_writer
from app.core.features import SubApp, require_feature
from app.core.roles import (
    ACTUAL_BID_EDITOR_ROLES,
    ACTUAL_BID_VIEWER_ROLES,
    INTERNAL_ROLES,
    WRITER_ROLES,
    Role,
)
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    AbandonIn,
    BidsTodayProjectOut,
    ProjectCreate,
    ProjectGCIn,
    ProjectGCUpdate,
    ProjectOut,
    ProjectUpdate,
)
from app.services import email_ingest, estimator_lifecycle, pm, proposal_send, workflow
from app.services.bid_invitations import REPORT_TZ, _day_start
from app.services.notifications import (
    ESTIMATOR_NOTIFICATION_TYPES,
    audit,
    dismiss_notifications,
    notify_role,
)
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
#
# GET "", GET /{id} and PATCH /{id} deliberately stay open: they are how a PM or
# CP deployment reads and renames the project rows it owns.
_BIDDING_ONLY = [Depends(require_feature(SubApp.BIDDING))]

# The projects.number unique index (migration 0052) retires every number ever
# used — they can't be re-used, even by an abandoned project. A collision surfaces
# from PostgREST as a 23505; translate it into a clean 409 instead of a raw 500.
_NUMBER_TAKEN = "That project number is already in use — numbers can't be re-used."


def _is_duplicate_number(exc: Exception) -> bool:
    msg = str(exc)
    return "projects_number_unique_idx" in msg or (
        "23505" in msg and "number" in msg
    )


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


def _present(project: dict, role: Role, cat_state: dict[str, dict] | None = None) -> dict:
    """Attach the derived lifecycle `status` (from the embedded bid outcome, if
    any) plus the per-category `category_state`, and redact. Pass every returned
    project row through here so the API `status` field stays consistent with the
    dashboard/analytics derivation."""
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
    return redact_for_role(project, role)


def _fetch_project_with_outcome(project_id: str) -> dict:
    """Load a project plus its (0-or-1) bid outcome so `status` is fully derivable."""
    resp = (
        get_supabase()
        .table("projects")
        .select("*, bid_outcomes(result)")
        .eq("id", project_id)
        .single()
        .execute()
    )
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
    states = workflow.load_category_states([p["id"] for p in rows])
    return [_present(p, user.role, states.get(p["id"])) for p in rows]


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


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED,
             dependencies=_BIDDING_ONLY)
def create_project(
    body: ProjectCreate,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_writer),
):
    """Create a project (typically the Estimating Admin). Starts in `intake`."""
    sb = get_supabase()
    payload = body.model_dump(exclude={"gcs"}, mode="json")
    payload["created_by"] = user.id
    payload["current_stage"] = "intake"
    payload["current_owner_role"] = Role.ESTIMATING_ADMIN.value
    try:
        created = sb.table("projects").insert(payload).execute().data[0]
    except Exception as exc:  # noqa: BLE001 — unique violation → number re-use
        if _is_duplicate_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _NUMBER_TAKEN) from exc
        raise

    if body.gcs:
        sb.table("project_gcs").insert(
            [
                {"project_id": created["id"], "gc_id": g.gc_id,
                 "needs_by": g.needs_by.isoformat() if g.needs_by else None}
                for g in body.gcs
            ]
        ).execute()

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
    # Learn-back: re-scan Unknown emails against the new project (bid invites
    # often arrive before the project exists). Best-effort, never raises.
    background.add_task(email_ingest.rescan_unknown_for_project, created["id"])
    return _present(created, user.role, workflow.load_category_state(created["id"]))


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, user: CurrentUser = Depends(get_current_user)):
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")
    return _present(
        _fetch_project_with_outcome(project_id),
        user.role,
        workflow.load_category_state(project_id),
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
    "number": _OPEN,
    "invitation_at": _OPEN,
    "notes": _OPEN,
    "is_ngem": _OPEN,
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
    if patch.get("name", "") is None or patch.get("number", "") is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "name and number cannot be cleared")
    _apply_bidding_url_rules(patch)
    denied = sorted(f for f in patch if user.role not in _FIELD_EDITORS.get(f, frozenset()))
    if denied:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Your role may not edit: {', '.join(denied)}",
        )
    try:
        updated = (
            get_supabase().table("projects").update(patch).eq("id", project_id).execute()
        ).data
    except Exception as exc:  # noqa: BLE001 — unique violation → number re-use
        if _is_duplicate_number(exc):
            raise HTTPException(status.HTTP_409_CONFLICT, _NUMBER_TAKEN) from exc
        raise
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    audit(user.id, "project.update", "project", project_id, patch)
    return _present(updated[0], user.role)


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
        .select("id, name, current_stage")
        .eq("id", project_id)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return row[0]


def _project_gc_rows(project_id: str) -> list[dict]:
    """Wire shape shared by the GET and returned from every membership write
    (the panel swaps its whole list for the response)."""
    rows = (
        get_supabase()
        .table("project_gcs")
        .select("needs_by, general_contractors(id, name, gc_contacts(id, name, email, phone))")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    out = []
    for r in rows:
        gc = r.get("general_contractors")
        if not gc:
            continue
        contacts = sorted(gc.get("gc_contacts") or [], key=lambda c: (c.get("name") or "").lower())
        out.append(
            {"id": gc["id"], "name": gc["name"], "needs_by": r.get("needs_by"),
             "contacts": contacts}
        )
    return sorted(out, key=lambda g: g["name"].lower())


def _block_if_sending(project_id: str, gc_id: str) -> None:
    """Dropping a GC mid-send would trip the isolation assertions and mark the
    send failed — make the user resolve the in-flight send first."""
    sending = (
        get_supabase()
        .table("proposal_sends")
        .select("id")
        .eq("project_id", project_id)
        .eq("gc_id", gc_id)
        .eq("status", "sending")
        .limit(1)
        .execute()
    ).data
    if sending:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A proposal send to this GC is in progress or unresolved — wait or retry it first.",
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
    _project_or_404(project_id)
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
    sb.table("project_gcs").insert(
        {"project_id": project_id, "gc_id": body.gc_id,
         "needs_by": body.needs_by.isoformat() if body.needs_by else None}
    ).execute()
    audit(user.id, "project.gc_add", "project", project_id,
          {"gc_id": body.gc_id, "gc_name": gc[0]["name"],
           "needs_by": body.needs_by.isoformat() if body.needs_by else None})
    return _project_gc_rows(project_id)


@router.patch("/{project_id}/gcs/{gc_id}", dependencies=_BIDDING_ONLY)
def update_project_gc(
    project_id: str,
    gc_id: str,
    body: ProjectGCUpdate,
    user: CurrentUser = Depends(require_writer),
):
    """Edit the per-GC needs-by date (the only mutable field on the link —
    membership itself is add/remove)."""
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
    needs_by = body.needs_by.isoformat() if body.needs_by else None
    sb.table("project_gcs").update({"needs_by": needs_by}).eq(
        "project_id", project_id
    ).eq("gc_id", gc_id).execute()
    audit(user.id, "project.gc_update", "project", project_id,
          {"gc_id": gc_id, "needs_by": needs_by})
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
    _block_if_sending(project_id, gc_id)
    sb.table("project_gcs").delete().eq("project_id", project_id).eq("gc_id", gc_id).execute()
    # Sent history stays in proposal_sends; never-sent rows are retired so the
    # Send Out panel stops offering them.
    proposal_send.retire_unsent_proposals(project_id, gc_id)
    audit(user.id, "project.gc_remove", "project", project_id, {"gc_id": gc_id})
    return _project_gc_rows(project_id)
