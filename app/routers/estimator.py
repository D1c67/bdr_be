"""Estimator hand-off (steps 3-4): assignments, file-package/update emails, and
the minimal estimator-facing endpoints.

The estimator is an external/untrusted user. Their access is gated everywhere
by `require_project_assignment` and they see only assigned projects plus the
package files (drawings, specifications, and the Changes/Revisions & Additional
files that were actually sent to them).

Assigning an estimator emails them the full branded package immediately; from
then on the initial drawing/spec blocks are locked (files.py) and new material
flows through `revision`/`additional` files sent via /send-file-updates.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.deps import (
    CurrentUser,
    get_current_user,
    require_project_assignment,
    require_writer,
)
from app.core.ratelimit import estimator_rate_limit, outbound_email_rate_limit
from app.core.roles import Role
from app.core.supabase_client import get_supabase
from app.core.roles import CHANGE_REVIEW_ROLES
from app.services import estimator_email, estimator_rounds, general_material, revision_email
from app.services.notifications import audit, dismiss_notifications, notify_role, notify_user

router = APIRouter(tags=["estimator"])

logger = logging.getLogger(__name__)

# A project must have at least one electrical drawing before it can be handed to
# the estimator — enforced here (not just in the UI) because it's a hard rule:
# you can't assign or email an estimator a package with no drawings.
NO_DRAWING_MESSAGE = "Upload at least one electrical drawing/plan first"


def project_has_drawing(project_id: str) -> bool:
    rows = (
        get_supabase()
        .table("project_files")
        .select("id")
        .eq("project_id", project_id)
        .eq("category", "drawing")
        .limit(1)
        .execute()
    ).data or []
    return bool(rows)


_FILE_FIELDS = "id, category, filename, storage_path, note, sent_to_estimators_at"


def _package_files(project_id: str) -> list[dict]:
    """Everything the estimators work from: the initial drawings/specifications
    plus the updates that were actually sent (an unsent update is still a
    draft — it goes out via /send-file-updates, not with a package)."""
    rows = (
        get_supabase()
        .table("project_files")
        .select(_FILE_FIELDS)
        .eq("project_id", project_id)
        .in_("category", ["drawing", "specification", "revision", "additional"])
        .order("created_at")
        .execute()
    ).data or []
    return [
        r
        for r in rows
        if r["category"] in ("drawing", "specification") or r.get("sent_to_estimators_at")
    ]


def _unsent_updates(project_id: str) -> list[dict]:
    return (
        get_supabase()
        .table("project_files")
        .select(_FILE_FIELDS)
        .eq("project_id", project_id)
        .in_("category", ["revision", "additional"])
        .is_("sent_to_estimators_at", "null")
        .order("created_at")
        .execute()
    ).data or []


def _active_assignments(project_id: str) -> list[dict]:
    """Active assignees with their profile email/name. Active = not revoked AND
    not expired — the same definition `require_project_assignment` enforces, so
    an estimator whose access window lapsed never receives another file email."""
    return (
        get_supabase()
        .table("estimator_assignments")
        .select("estimator_id, profiles!estimator_assignments_estimator_id_fkey(email, full_name)")
        .eq("project_id", project_id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .execute()
    ).data or []


class AssignIn(BaseModel):
    estimator_id: str
    due_at: datetime | None = None
    expires_at: datetime | None = None


# ── Picking an estimator (any writer role) ────────────────────────────────


@router.get("/estimators")
def list_estimators(_: CurrentUser = Depends(require_writer)):
    # Estimator-role profiles plus dev accounts (is_dev). Dev accounts can switch
    # their own role and bypass the estimator assignment gates (see deps.py), so
    # they're selectable here to test/run the estimator flow themselves.
    return (
        get_supabase()
        .table("profiles")
        .select("id, full_name, email, role, is_dev")
        .or_("role.eq.estimator,is_dev.eq.true")
        .eq("is_active", True)
        .order("full_name")
        .execute()
    ).data or []


# ── Assignment management ─────────────────────────────────────────────────


@router.post(
    "/projects/{project_id}/assign-estimator",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(outbound_email_rate_limit)],
)
def assign_estimator(
    project_id: str,
    body: AssignIn,
    user: CurrentUser = Depends(require_writer),
):
    if not project_has_drawing(project_id):
        raise HTTPException(status.HTTP_409_CONFLICT, NO_DRAWING_MESSAGE)
    sb = get_supabase()
    proj = (
        sb.table("projects")
        .select("id, name, number, due_from_estimator_at")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    # Only the profiles the /estimators picker offers are assignable — same
    # filter server-side so a stale/handcrafted id can't hand project files to
    # a deactivated or non-estimator account. (.limit(1), not .single(): a
    # missing row must be a clean 404, not an APIError 500.)
    est_rows = (
        sb.table("profiles")
        .select("email, full_name, role, is_dev, is_active")
        .eq("id", body.estimator_id)
        .limit(1)
        .execute()
    ).data or []
    est = est_rows[0] if est_rows else None
    if (
        not est
        or not est.get("email")
        or not est.get("is_active")
        or not (est.get("role") == Role.ESTIMATOR.value or est.get("is_dev"))
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Estimator not found")
    row = (
        sb.table("estimator_assignments")
        .insert(
            {
                "project_id": project_id,
                "estimator_id": body.estimator_id,
                "assigned_by": user.id,
                "due_at": body.due_at.isoformat() if body.due_at else None,
                "expires_at": body.expires_at.isoformat() if body.expires_at else None,
            }
        )
        .execute()
    ).data[0]

    # The new assignee immediately receives the full branded package — initial
    # drawings/specs plus every update already sent to the earlier estimators,
    # grouped so they can tell which is which. No separate send click. If the
    # email can't go out, the assignment rolls back so "assigned" always
    # implies "has the files". (Skipped when Graph isn't configured — local/dev.)
    #
    # Draft updates that were uploaded but never sent ride along too: assigning
    # is a send event. Without this, an estimator assigned after changes were
    # uploaded (but before anyone pressed "Send file updates") would price off
    # the stale initial set and never know — the exact failure the update flow
    # exists to prevent.
    pending = _unsent_updates(project_id)
    package_sent = False
    if estimator_email.graph_configured():
        try:
            estimator_email.send_package(
                proj=proj,
                to=[est["email"]],
                files=_package_files(project_id) + pending,
                recipient_name=est.get("full_name"),
                sent_by=user.id,
            )
            package_sent = True
        except Exception as exc:
            sb.table("estimator_assignments").delete().eq("id", row["id"]).execute()
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "Could not email the file package — the assignment was rolled back; try again",
            ) from exc
        # Start the turnaround clock for this assignee now that they have the
        # files. Best-effort: the email is already out, so a failed stamp must
        # not 500 the assign (or worse, tempt a rollback of a delivered email).
        try:
            sb.table("estimator_assignments").update({"sent_to_estimator_at": "now()"}).eq(
                "id", row["id"]
            ).execute()
        except Exception:  # noqa: BLE001
            logger.warning("assign_estimator: sent_to_estimator_at stamp failed", exc_info=True)
        # The pending updates just went out in the package, so stamp them sent —
        # visible in every assignee's portal, undeletable, and excluded from the
        # next "Send file updates". Best-effort like the stamp above: the email
        # is already delivered, so a failed stamp must not 500 the assign.
        if pending:
            try:
                sb.table("project_files").update({"sent_to_estimators_at": "now()"}).in_(
                    "id", [f["id"] for f in pending]
                ).execute()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "assign_estimator: sent_to_estimators_at stamp failed", exc_info=True
                )
            else:
                # Earlier assignees never got these files in an email — ping
                # their bell so the newly-visible updates don't appear silently.
                try:
                    label = f"{proj.get('number') or ''} {proj.get('name') or ''}".strip() or (
                        "a project"
                    )
                    msg = (
                        f"{estimator_email.updates_label(pending)} sent for {label} — "
                        "review before continuing your estimate."
                    )
                    for a in _active_assignments(project_id):
                        if a["estimator_id"] != body.estimator_id:
                            notify_user(a["estimator_id"], project_id, "files_updated", msg)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "assign_estimator: files_updated notifications failed", exc_info=True
                    )

    audit(
        user.id,
        "estimator.assign",
        "project",
        project_id,
        {
            "estimator_id": body.estimator_id,
            "package_sent": package_sent,
            "pending_updates_sent": len(pending) if package_sent else 0,
        },
    )
    notify_user(body.estimator_id, project_id, "assigned", "You were assigned to a project")
    return {**row, "package_sent": package_sent}


@router.get("/projects/{project_id}/assignments")
def list_assignments(
    project_id: str, _: CurrentUser = Depends(require_writer)
):
    return (
        get_supabase()
        .table("estimator_assignments")
        .select("*, profiles!estimator_assignments_estimator_id_fkey(full_name, email)")
        .eq("project_id", project_id)
        .execute()
    ).data or []


@router.post("/projects/{project_id}/assignments/{assignment_id}/revoke")
def revoke_assignment(
    project_id: str,
    assignment_id: str,
    user: CurrentUser = Depends(require_writer),
):
    rows = (
        get_supabase()
        .table("estimator_assignments")
        .update({"revoked_at": "now()"})
        .eq("id", assignment_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    audit(user.id, "estimator.revoke", "project", project_id, {"assignment_id": assignment_id})
    # The estimator no longer has the project, so their "assigned" ping is stale.
    estimator_id = rows[0]["estimator_id"] if rows else None
    if estimator_id:
        dismiss_notifications(project_id=project_id, types=["assigned"], user_id=estimator_id)
    return {"revoked": True}


# ── Send to estimator (step 3): email the file package / file updates ─────


@router.post(
    "/projects/{project_id}/send-to-estimator",
    dependencies=[Depends(outbound_email_rate_limit)],
)
def send_to_estimator(
    project_id: str,
    user: CurrentUser = Depends(require_writer),
):
    """Manually (re-)email the full branded file package to every active
    assignee. New assignees already get the package at assign time — this is
    the re-send path (bounced address, estimator lost the mail, etc.)."""
    if not estimator_email.graph_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email sending is not configured")
    sb = get_supabase()
    proj = (
        sb.table("projects")
        .select("id, name, number, due_from_estimator_at")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    assigns = _active_assignments(project_id)
    if not assigns:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assign an estimator first")
    recipients = [a["profiles"]["email"] for a in assigns if a.get("profiles")]

    # Never email an estimator a package with no drawings.
    files = _package_files(project_id)
    if not any(f["category"] == "drawing" for f in files):
        raise HTTPException(status.HTTP_409_CONFLICT, NO_DRAWING_MESSAGE)

    log = estimator_email.send_package(proj=proj, to=recipients, files=files, sent_by=user.id)
    # Start the turnaround clock once, on the first send. Re-sends (e.g. to add a
    # recipient) leave the original timestamp so the measured time stays honest.
    sb.table("estimator_assignments").update({"sent_to_estimator_at": "now()"}).eq(
        "project_id", project_id
    ).is_("revoked_at", "null").is_("sent_to_estimator_at", "null").execute()
    audit(user.id, "estimator.email_sent", "project", project_id, {"to": recipients})
    return {"sent_to": recipients, "email_log_id": log["id"]}


class UpdatesIn(BaseModel):
    # Optional overall message included at the top of the updates email, above
    # the per-file notes.
    message: str | None = None


@router.post(
    "/projects/{project_id}/send-file-updates",
    dependencies=[Depends(outbound_email_rate_limit)],
)
def send_file_updates(
    project_id: str,
    body: UpdatesIn | None = None,
    user: CurrentUser = Depends(require_writer),
):
    """Email the not-yet-sent Changes/Revisions & Additional files (each with
    its required note, plus an optional overall message) to every active
    assignee, then stamp them sent — which makes them visible in the estimator
    portal and undeletable here."""
    if not estimator_email.graph_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Email sending is not configured")
    sb = get_supabase()
    proj = (
        sb.table("projects")
        .select("id, name, number, due_from_estimator_at")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    assigns = _active_assignments(project_id)
    if not assigns:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assign an estimator first")
    recipients = [a["profiles"]["email"] for a in assigns if a.get("profiles")]

    pending = _unsent_updates(project_id)
    if not pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No new Changes/Revisions or Additional files to send"
        )

    message = (body.message or "").strip() if body else ""
    log = estimator_email.send_updates(
        proj=proj, to=recipients, files=pending, message=message or None, sent_by=user.id
    )
    sent_ids = [f["id"] for f in pending]
    sb.table("project_files").update({"sent_to_estimators_at": "now()"}).in_(
        "id", sent_ids
    ).execute()

    counts: dict[str, int] = {}
    for f in pending:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    audit(
        user.id,
        "estimator.updates_sent",
        "project",
        project_id,
        {"to": recipients, "counts": counts},
    )
    label = f"{proj.get('number') or ''} {proj.get('name') or ''}".strip() or "a project"
    msg = (
        f"{estimator_email.updates_label(pending)} sent for {label} — "
        "review before continuing your estimate."
    )
    for estimator_id in {a["estimator_id"] for a in assigns}:
        notify_user(estimator_id, project_id, "files_updated", msg)
    return {
        "sent_to": recipients,
        "sent_file_ids": sent_ids,
        "counts": counts,
        "email_log_id": log["id"],
    }


# ── Estimator-facing minimal endpoints ────────────────────────────────────


@router.get("/estimator/projects", dependencies=[Depends(estimator_rate_limit)])
def my_assigned_projects(user: CurrentUser = Depends(get_current_user)):
    """An estimator's assigned projects — minimal fields only."""
    if user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators only")
    sb = get_supabase()
    assigns = (
        sb.table("estimator_assignments")
        .select("project_id, due_at, expires_at")
        .eq("estimator_id", user.id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .execute()
    ).data or []
    if not assigns:
        return []
    ids = [a["project_id"] for a in assigns]
    projs = (
        sb.table("projects").select("id, name, number, current_stage").in_("id", ids).execute()
    ).data or []
    due_by = {a["project_id"]: a["due_at"] for a in assigns}
    return [{**p, "due_at": due_by.get(p["id"])} for p in projs]


def _before_receive_quotes(stage: str) -> bool:
    """True while nothing downstream has consumed the estimate figure yet —
    the window where a revised estimate may silently refresh the extraction
    (no badge, no human step) instead of demanding a manual reprocess."""
    from app.services.workflow import STAGES

    defn = STAGES.get(stage)
    return bool(defn and defn.order < STAGES["receive_quotes"].order)


@router.post("/estimator/projects/{project_id}/submit", dependencies=[Depends(estimator_rate_limit)])
def submit_deliverables(
    project_id: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_project_assignment),
):
    """The estimator hands their deliverables back to the team.

    Files are already uploaded as drafts; this seals them into a numbered
    submission round (estimator_rounds). Round 1 is the original hand-off;
    every later round is "Changes/Revisions & Additional files" — those alert
    the whole review team (high-importance email + bell + per-user banner)
    instead of the round-1 estimate_submitted notification.
    """
    if user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators only")
    sb = get_supabase()
    proj = (
        sb.table("projects")
        .select("name, number, current_stage")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    if not proj:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    submission, sealed = estimator_rounds.create_submission_round(project_id, user.id)
    round_no = submission["round"]
    counts: dict[str, int] = submission["summary"] or {}
    summary = ", ".join(f"{n} {c}" for c, n in counts.items())

    # Stamp the return so analytics can measure received → returned turnaround.
    # First submit wins — revision rounds carry their own submitted_at on
    # estimator_submissions, so they must not stretch the measured turnaround.
    sb.table("estimator_assignments").update({"returned_at": "now()"}).eq(
        "project_id", project_id
    ).eq("estimator_id", user.id).is_("revoked_at", "null").is_(
        "returned_at", "null"
    ).execute()

    if round_no == 1:
        audit(user.id, "estimator.submit", "project", project_id, {"counts": counts})
        msg = f"Estimator submitted deliverables for {proj['name']} ({proj['number']}): {summary}"
        notify_role(Role.ESTIMATING_ADMIN, project_id, "estimate_submitted", msg)
        notify_role(Role.ESTIMATING_ENGINEER, project_id, "estimate_submitted", msg)
        # Pull the general-material (wiring) price from the estimate in the background.
        if counts.get("estimate"):
            background.add_task(general_material.run_extraction, project_id)
        return {"submitted": True, "round": round_no, "counts": counts}

    # Round ≥ 2 — the round stands even if alerting hiccups, so notifications
    # and the email are each isolated. Bell rows skip the generic email mirror;
    # revision_email sends the one high-importance alert instead.
    audit(
        user.id,
        "estimator.revision_submit",
        "project",
        project_id,
        {"round": round_no, "counts": counts},
    )
    msg = (
        f"Estimator sent changes/revisions (round {round_no}) for "
        f"{proj['name']} ({proj['number']}): {summary}"
    )
    try:
        for role in sorted(CHANGE_REVIEW_ROLES):
            notify_role(role, project_id, "estimate_revised", msg, mirror_email=False)
    except Exception:  # noqa: BLE001
        logger.exception("submit_deliverables: revision bell notifications failed")
    revision_email.queue_revision_alert(project_id, round_no, sealed)

    # A revised estimate before anything consumed the figure just refreshes the
    # extraction silently; at/after Receive Quotes the team reprocesses
    # deliberately via the stale-file badge (never yank verified numbers).
    if counts.get("estimate") and _before_receive_quotes(proj["current_stage"]):
        background.add_task(general_material.run_extraction, project_id)
    return {"submitted": True, "round": round_no, "counts": counts}


@router.get(
    "/estimator/projects/{project_id}", dependencies=[Depends(estimator_rate_limit)]
)
def estimator_project_detail(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    if user.role != Role.ESTIMATOR:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Estimators only")
    # Minimal projection — never pricing/markup/quotes.
    proj = (
        get_supabase()
        .table("projects")
        .select("id, name, number, current_stage, due_from_estimator_at, notes")
        .eq("id", project_id)
        .single()
        .execute()
    ).data
    # Sent rounds, oldest first — drives the post-submit portal UI (locked
    # round history + the Changes/Revisions and Additional Files boxes).
    submissions = (
        get_supabase()
        .table("estimator_submissions")
        .select("round, submitted_at, summary")
        .eq("project_id", project_id)
        .order("round")
        .execute()
    ).data or []
    return {**(proj or {}), "submissions": submissions}
