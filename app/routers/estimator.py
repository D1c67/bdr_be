"""Estimator hand-off (steps 3-4): assignments, file-package/update emails, the
send-batch record, and the minimal estimator-facing endpoints.

The estimator is an external/untrusted user. Their access is gated everywhere
by `require_project_assignment` and they see only assigned projects plus the
package files (drawings, specifications, and the Changes/Revisions, Additional
and Addendum files that were actually sent to them).

Assigning ONE estimator emails them the full branded package immediately and
records a send batch (`file_sends.claim_batch`) BEFORE the email goes out — so a
double-click can't double-send the initial package and a failed email leaves a
clean retry with nothing recorded as sent. Graph must be configured (503
otherwise), which removes the old unrecoverable "assigned but never sent" state.
From then on the initial drawing/spec blocks are locked (files.py) and new
material flows through `revision`/`additional`/`addendum` files sent via
/send-file-updates as their own batch. Every outbound send goes one email per
recipient — never a single to=[all], which would leak every estimator's address
to the others (graph_email has no BCC path).
"""

import logging
from datetime import date, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.core.deps import (
    CurrentUser,
    get_current_user,
    require_project_assignment,
    require_writer,
)
from app.core.file_categories import (
    SECTION_NOTE_KEYS,
    SECTION_NOTE_MAX_CHARS,
    SECTION_NOTE_REQUIRED_KEYS,
    SENT_GATED_CATEGORIES,
    section_key,
)
from app.core.ratelimit import estimator_rate_limit, outbound_email_rate_limit
from app.core.roles import CHANGE_REVIEW_ROLES, Role
from app.core.supabase_client import get_supabase
from app.services import (
    estimator_email,
    estimator_rounds,
    file_sends,
    general_material,
    revision_email,
)
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


# Includes the addendum metadata (number + issue date) and size so both the
# email renderer and the send-batch summary can be built from one read.
_FILE_FIELDS = (
    "id, category, doc_type, filename, storage_path, note, sent_to_estimators_at, "
    "addendum_number, addendum_issued_on, size_bytes, created_at"
)


def _package_files(project_id: str) -> list[dict]:
    """Everything the estimators work from: the initial drawings/specifications
    plus the updates (revisions, additional files, addenda) that were actually
    sent (an unsent update is still a draft — it goes out via /send-file-updates,
    not with a package)."""
    rows = (
        get_supabase()
        .table("project_files")
        .select(_FILE_FIELDS)
        .eq("project_id", project_id)
        .in_("category", ["drawing", "specification", "addendum", "revision", "additional"])
        .order("created_at")
        .execute()
    ).data or []
    return [
        r
        for r in rows
        if r["category"] in ("drawing", "specification") or r.get("sent_to_estimators_at")
    ]


def _unsent_updates(project_id: str) -> list[dict]:
    """The not-yet-emailed Changes/Revisions, Additional files AND addenda. An
    addendum is sent-gated like the update categories, so it must be selectable
    here or it would be invisible in the portal forever."""
    return (
        get_supabase()
        .table("project_files")
        .select(_FILE_FIELDS)
        .eq("project_id", project_id)
        .in_("category", list(SENT_GATED_CATEGORIES))
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


def _recipient_dicts(assigns: list[dict]) -> list[dict]:
    """Turn `_active_assignments` rows into `file_sends.claim_batch` recipient
    dicts, dropping any with no email (nothing to deliver to). claim_batch
    de-dupes on email, so a doubled person can't trip the (batch_id, email) PK."""
    out: list[dict] = []
    for a in assigns:
        prof = a.get("profiles") or {}
        email = (prof.get("email") or "").strip()
        if email:
            out.append(
                {
                    "estimator_id": a.get("estimator_id"),
                    "email": email,
                    "full_name": prof.get("full_name"),
                }
            )
    return out


def _category_counts(files: list[dict]) -> dict[str, int]:
    """Plain {category: count} for a set of files — the audit `counts` payload."""
    counts: dict[str, int] = {}
    for f in files:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
    return counts


def _batch_summary(files: list[dict]) -> dict:
    """The at-send-time snapshot stored on the batch: category counts plus the
    addendum numbers. Rendered as the log headline instead of counting the live
    join, so a later file delete can't retroactively rewrite what was sent."""
    summary: dict = dict(_category_counts(files))
    numbers = [
        str(f["addendum_number"])
        for f in files
        if f["category"] == "addendum" and f.get("addendum_number")
    ]
    if numbers:
        summary["addendum_numbers"] = numbers
    return summary


class AssignIn(BaseModel):
    estimator_id: str
    due_at: datetime | None = None                    # per-assignment turnaround benchmark
    expires_at: datetime | None = None                # None == unlimited access
    due_from_estimator_at: datetime | None = None     # writes projects.due_from_estimator_at
    message: str | None = Field(default=None, max_length=4000)
    revoke_assignment_ids: list[str] = Field(default_factory=list, max_length=20)


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
    """Assign ONE estimator and email them the package in a single step.

    The first assign on a project is the one-shot INITIAL hand-off (a
    kind='initial' send batch, unique per project via the partial index). Every
    later assign is a 'reassign': the new estimator receives the full current
    package plus an "Update history" catch-up of every change already issued, and
    any pre-existing assignees are actually EMAILED (not merely belled) the
    ride-along drafts as their own 'revision' batch.

    The batch row is claimed BEFORE the email, so a double-click can't double-send
    the initial package and a failed email leaves a clean retry with
    `package_sent_at` still null. Graph must be configured (503 otherwise), which
    removes the old unrecoverable "assigned but nothing sent -> drawings frozen"
    state.
    """
    if not project_has_drawing(project_id):
        raise HTTPException(status.HTTP_409_CONFLICT, NO_DRAWING_MESSAGE)
    # Never create an assignment that can never be emailed. Mirrors
    # send_to_estimator; this is what deletes the assign-without-email state.
    if not estimator_email.graph_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Email sending is not configured"
        )
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
    # filter server-side so a stale/handcrafted id can't hand project files to a
    # deactivated or non-estimator account. (.limit(1), not .single(): a missing
    # row must be a clean 404, not an APIError 500.)
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

    # An estimator already actively assigned must not be added twice — that
    # doubles the recipient and the card row. Active = not revoked AND not
    # expired (same predicate as _active_assignments / require_project_assignment).
    active_dupe = (
        sb.table("estimator_assignments")
        .select("id")
        .eq("project_id", project_id)
        .eq("estimator_id", body.estimator_id)
        .is_("revoked_at", "null")
        .or_("expires_at.is.null,expires_at.gt.now()")
        .limit(1)
        .execute()
    ).data or []
    if active_dupe:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That estimator is already assigned to this project"
        )

    # A new due-back date must be persisted BEFORE the email so the email prints
    # it (estimator_email reads proj["due_from_estimator_at"]). Update the local
    # dict too.
    if body.due_from_estimator_at is not None:
        due_iso = body.due_from_estimator_at.isoformat()
        sb.table("projects").update({"due_from_estimator_at": due_iso}).eq(
            "id", project_id
        ).execute()
        proj["due_from_estimator_at"] = due_iso

    # Reactivate an expired-but-unrevoked row rather than pile up a second one;
    # otherwise INSERT. No active row exists (the dupe check passed), so any
    # remaining unrevoked row for this estimator is expired.
    reusable = (
        sb.table("estimator_assignments")
        .select("id")
        .eq("project_id", project_id)
        .eq("estimator_id", body.estimator_id)
        .is_("revoked_at", "null")
        .limit(1)
        .execute()
    ).data or []
    inserted = False
    if reusable:
        row = (
            sb.table("estimator_assignments")
            .update(
                {
                    "assigned_by": user.id,
                    "due_at": body.due_at.isoformat() if body.due_at else None,
                    "expires_at": body.expires_at.isoformat() if body.expires_at else None,
                    "revoked_at": None,
                }
            )
            .eq("id", reusable[0]["id"])
            .execute()
        ).data[0]
    else:
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
        inserted = True

    # The full current package (initial drawings/specs + every revision/addendum
    # already sent) plus the not-yet-sent drafts, which ride along with this send.
    pending = _unsent_updates(project_id)
    files = _package_files(project_id) + pending
    kind = "initial" if not file_sends.has_initial_send(project_id) else "reassign"

    # Claim the batch (row + recipients + file links) BEFORE the email. The unique
    # partial index turns two racing initial sends into a 409 here. Roll back an
    # assignment THIS request inserted if the claim fails, then re-raise unchanged.
    try:
        batch = file_sends.claim_batch(
            project_id=project_id,
            kind=kind,
            sent_by=user.id,
            message=body.message,
            recipients=[
                {
                    "estimator_id": body.estimator_id,
                    "email": est["email"],
                    "full_name": est.get("full_name"),
                }
            ],
            file_ids=[f["id"] for f in files],
            summary=_batch_summary(files),
        )
    except Exception:
        if inserted:
            sb.table("estimator_assignments").delete().eq("id", row["id"]).execute()
        raise

    try:
        log = estimator_email.send_package(
            proj=proj,
            to=[est["email"]],
            files=files,
            recipient_name=est.get("full_name"),
            sent_by=user.id,
            message=body.message,
            kind=kind,
            prior=file_sends.prior_batches(project_id) if kind == "reassign" else None,
        )
    except Exception as exc:  # noqa: BLE001 — a failed email must leave a clean retry
        file_sends.abandon_batch(batch["id"])
        # Only undo an assignment THIS request inserted — never a row we merely
        # reactivated (recover that via Re-send to active assignees).
        if inserted:
            sb.table("estimator_assignments").delete().eq("id", row["id"]).execute()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Could not email the file package — the assignment was rolled back; try again",
        ) from exc

    # Post-send, all best-effort (the mail is out; a lost write must not 500 or
    # tempt a rollback of a delivered email):
    file_sends.attach_email_log(batch["id"], est["email"], log["id"])
    # Start this assignee's turnaround clock, NULL-guarded so a reactivated row's
    # original send timestamp — and its honest turnaround — survives.
    try:
        sb.table("estimator_assignments").update({"sent_to_estimator_at": "now()"}).eq(
            "id", row["id"]
        ).is_("sent_to_estimator_at", "null").execute()
    except Exception:  # noqa: BLE001
        logger.warning("assign_estimator: sent_to_estimator_at stamp failed", exc_info=True)
    # The drafts just rode along in the package — stamp them sent (first-send-wins,
    # NULL-guarded inside stamp_sent).
    file_sends.stamp_sent([f["id"] for f in pending])

    # Ride-along: the pre-existing assignees never received these drafts. Send
    # them their own 'revision' batch — actually emailed, not merely belled, so
    # the files are reachable from their log. Best-effort as a whole: the primary
    # package is delivered, so a hiccup here must not 502 the assign.
    if pending:
        others = [
            a
            for a in _active_assignments(project_id)
            if a.get("estimator_id") != body.estimator_id
            and (a.get("profiles") or {}).get("email")
        ]
        if others:
            try:
                ride_batch = file_sends.claim_batch(
                    project_id=project_id,
                    kind="revision",
                    sent_by=user.id,
                    message=body.message,
                    recipients=_recipient_dicts(others),
                    file_ids=[f["id"] for f in pending],
                    summary=_batch_summary(pending),
                )
            except Exception:  # noqa: BLE001
                ride_batch = None
                logger.warning(
                    "assign_estimator: ride-along batch claim failed", exc_info=True
                )
            if ride_batch:
                for a in others:
                    prof = a["profiles"]
                    try:
                        lg = estimator_email.send_updates(
                            proj=proj,
                            to=[prof["email"]],
                            files=pending,
                            message=body.message,
                            recipient_name=prof.get("full_name"),
                            sent_by=user.id,
                        )
                        file_sends.attach_email_log(ride_batch["id"], prof["email"], lg["id"])
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "assign_estimator: ride-along update send failed", exc_info=True
                        )
            label = f"{proj.get('number') or ''} {proj.get('name') or ''}".strip() or "a project"
            ride_msg = (
                f"{estimator_email.updates_label(pending)} sent for {label} — "
                "review before continuing your estimate."
            )
            for a in others:
                try:
                    notify_user(a["estimator_id"], project_id, "files_updated", ride_msg)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "assign_estimator: ride-along notify failed", exc_info=True
                    )

    # Revocations apply only AFTER a successful send, so a failed send never
    # strands a project with no assignee. Each reuses the revoke semantics.
    revoked: list[str] = []
    for aid in body.revoke_assignment_ids:
        rev = (
            sb.table("estimator_assignments")
            .update({"revoked_at": "now()"})
            .eq("id", aid)
            .eq("project_id", project_id)
            .execute()
        ).data or []
        if rev:
            revoked.append(aid)
            rev_est = rev[0].get("estimator_id")
            if rev_est:
                dismiss_notifications(
                    project_id=project_id, types=["assigned"], user_id=rev_est
                )
            audit(
                user.id,
                "estimator.revoke",
                "project",
                project_id,
                {"assignment_id": aid, "estimator_id": rev_est},
            )

    audit(
        user.id,
        "estimator.assign",
        "project",
        project_id,
        {
            "estimator_id": body.estimator_id,
            "kind": kind,
            "batch_id": batch["id"],
            "package_sent": True,
            "pending_updates_sent": len(pending),
            "revoked": revoked,
        },
    )
    # One uniform line per batch-producing send — keeps the activity feed
    # continuous now that this action's old writer (send-to-estimator) records a
    # batch of its own instead of being the sole source.
    audit(
        user.id,
        "estimator.email_sent",
        "project",
        project_id,
        {
            "batch_id": batch["id"],
            "kind": kind,
            "to": [est["email"]],
            "counts": _category_counts(files),
        },
    )
    notify_user(body.estimator_id, project_id, "assigned", "You were assigned to a project")
    return {
        **row,
        "package_sent": True,
        "batch_id": batch["id"],
        "kind": kind,
        "revoked": revoked,
        "pending_updates_sent": len(pending),
    }


class HandoffAssigneeOut(BaseModel):
    assignment_id: str
    estimator_id: str
    full_name: str | None = None
    email: str | None = None          # ALWAYS None for Role.ESTIMATOR
    due_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    sent_to_estimator_at: datetime | None = None


class LatestAddendumOut(BaseModel):
    number: str
    issued_on: date


class HandoffOut(BaseModel):
    # SOURCE OF TRUTH for the FE button predicates. sent_at of the kind='initial'
    # batch; None => the initial package was never emailed (NOT the same as locked).
    package_sent_at: datetime | None = None
    last_sent_at: datetime | None = None
    batch_count: int = 0
    due_back_at: datetime | None = None        # projects.due_from_estimator_at
    # Internal: every assignment, revoked included, newest first.
    # ESTIMATOR: EXACTLY the caller's own row, email blanked.
    assignees: list[HandoffAssigneeOut]
    # Uploaded, never emailed. Internal only; {} for the estimator.
    staged: dict[str, int]
    # Cumulative distinct files across the caller's visible batches.
    sent: dict[str, int]
    latest_addendum: LatestAddendumOut | None = None
    locked: bool                               # mirrors GET /files/lock
    # ESTIMATOR only: their own assignment window. Never another's.
    my_access_expires_at: datetime | None = None
    my_due_at: datetime | None = None


@router.get(
    "/projects/{project_id}/handoff",
    response_model=HandoffOut,
    dependencies=[Depends(estimator_rate_limit)],
)
def project_handoff(
    project_id: str, user: CurrentUser = Depends(require_project_assignment)
):
    """The compact hand-off summary + the source of truth for the FE button
    predicates.

    Served to BOTH roles as a server-side discriminated union
    (`file_sends.build_handoff`): the estimator's payload is scoped to their own
    batches/assignment, their `assignees` list holds exactly their own row with
    the email blanked, and `staged` is empty — so no other estimator's identity,
    nor the project-wide send count, can leak. `require_project_assignment` (not
    `require_writer`) so the read-only accountant and the external estimator both
    reach it; the estimator's own assignment gate still applies.
    """
    return file_sends.build_handoff(project_id, user)


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
    # The estimator no longer has the project, so their "assigned" ping is stale.
    estimator_id = rows[0]["estimator_id"] if rows else None
    audit(
        user.id,
        "estimator.revoke",
        "project",
        project_id,
        {"assignment_id": assignment_id, "estimator_id": estimator_id},
    )
    if estimator_id:
        dismiss_notifications(project_id=project_id, types=["assigned"], user_id=estimator_id)
    return {"revoked": True}


# ── Re-send to estimator: re-email the full package to active assignees ────


@router.post(
    "/projects/{project_id}/send-to-estimator",
    dependencies=[Depends(outbound_email_rate_limit)],
)
def send_to_estimator(
    project_id: str,
    user: CurrentUser = Depends(require_writer),
):
    """Re-email the full branded package to every active assignee — the
    bounced-address / lost-mail recovery path, surfaced as "Re-send" on a log
    batch. New assignees already get the package at assign time; this re-sends the
    current package unchanged, one email per recipient (never BCC), and records it
    as its own send batch so the re-send is visible in the Plans & Specs Log.
    """
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
    recipients = _recipient_dicts(assigns)
    if not recipients:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assign an estimator first")

    # Never email an estimator a package with no drawings.
    files = _package_files(project_id)
    if not any(f["category"] == "drawing" for f in files):
        raise HTTPException(status.HTTP_409_CONFLICT, NO_DRAWING_MESSAGE)

    kind = "initial" if not file_sends.has_initial_send(project_id) else "reassign"
    batch = file_sends.claim_batch(
        project_id=project_id,
        kind=kind,
        sent_by=user.id,
        message=None,
        recipients=recipients,
        file_ids=[f["id"] for f in files],
        summary=_batch_summary(files),
    )

    # One email per recipient — a single to=[all] would leak every estimator's
    # address to the others. On the FIRST failure (nothing delivered) abandon the
    # batch and 502 for a clean retry; a later failure after a delivery leaves the
    # batch standing (some recipients did receive it).
    first_log_id: str | None = None
    delivered: list[str] = []
    for r in recipients:
        try:
            lg = estimator_email.send_package(
                proj=proj,
                to=[r["email"]],
                files=files,
                recipient_name=r.get("full_name"),
                sent_by=user.id,
            )
        except Exception as exc:  # noqa: BLE001
            if not delivered:
                file_sends.abandon_batch(batch["id"])
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    "Could not email the file package — try again",
                ) from exc
            logger.warning(
                "send_to_estimator: re-send to a later recipient failed", exc_info=True
            )
            continue
        delivered.append(r["email"])
        file_sends.attach_email_log(batch["id"], r["email"], lg["id"])
        if first_log_id is None:
            first_log_id = lg["id"]

    # Start the turnaround clock once, on the first send. Re-sends leave the
    # original timestamp (NULL-guarded) so the measured time stays honest.
    sb.table("estimator_assignments").update({"sent_to_estimator_at": "now()"}).eq(
        "project_id", project_id
    ).is_("revoked_at", "null").is_("sent_to_estimator_at", "null").execute()
    audit(
        user.id,
        "estimator.email_sent",
        "project",
        project_id,
        {
            "batch_id": batch["id"],
            "kind": kind,
            "to": [r["email"] for r in recipients],
            "counts": _category_counts(files),
        },
    )
    return {
        "sent_to": [r["email"] for r in recipients],
        "email_log_id": first_log_id,
        "batch_id": batch["id"],
    }


class UpdatesIn(BaseModel):
    # Optional overall message included at the top of the updates email, above
    # the per-file notes.
    message: str | None = Field(default=None, max_length=4000)
    # Send exactly this staged subset. `None` keeps the legacy "everything unsent"
    # behaviour; an explicit list stops the modal from sweeping a colleague's
    # in-progress draft into this batch (and is the double-click guard).
    file_ids: list[str] | None = Field(default=None, max_length=200)
    # 0077 — one "what changed" note per SECTION of the Revisions modal, keyed by
    # file_categories.section_key(): "revision:drawing" ("what changed in the
    # plans"), "revision:specification", "addendum", "additional". Sits between
    # the batch-wide `message` and each file's own `note`; validated in the
    # handler against SECTION_NOTE_KEYS so a typo can't store a note nothing
    # renders. `max_length` on a dict bounds its KEY COUNT.
    section_notes: dict[str, str] | None = Field(default=None, max_length=16)


def _clean_section_notes(raw: dict[str, str] | None, files: list[dict]) -> dict[str, str]:
    """Validate + normalise the per-section notes against the batch's actual
    contents.

    Rejects (400) an unknown key, an over-long note, and a note for a section
    this batch has no files in — a note nothing renders is a note the author
    believes was delivered. Requires one for every revision section present, the
    same rule the per-file note already enforces for revisions at upload time.
    Blank/whitespace values are dropped, so "" never counts as an answer.

    A section is "present" under its exact key AND under its bare category: the
    modal keeps addenda in ONE box whose files may be tagged plans or specs
    individually, so its single note arrives keyed "addendum" while its files key
    to "addendum:drawing"/"addendum:specification". The renderer resolves the
    same way round (exact key, else bare category).
    """
    present = {section_key(f["category"], f.get("doc_type")) for f in files}
    present |= {f["category"] for f in files}
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if key not in SECTION_NOTE_KEYS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown section note '{key}'")
        text = (value or "").strip()
        if not text:
            continue
        if len(text) > SECTION_NOTE_MAX_CHARS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A section note is too long")
        if key not in present:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "A section note was given for a section with no files in this send",
            )
        out[key] = text
    missing = sorted((present & SECTION_NOTE_REQUIRED_KEYS) - out.keys())
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Say what changed in each revised section before sending",
        )
    return out


@router.post(
    "/projects/{project_id}/send-file-updates",
    dependencies=[Depends(outbound_email_rate_limit)],
)
def send_file_updates(
    project_id: str,
    body: UpdatesIn | None = None,
    user: CurrentUser = Depends(require_writer),
):
    """Email the not-yet-sent Changes/Revisions, Additional files and addenda
    (each revision/additional with its required note, plus an optional overall
    message) to every active assignee as one 'revision' send batch, then stamp
    them sent — which makes them visible in the estimator portal and undeletable.

    The batch is claimed BEFORE any email, one email is sent per recipient (never
    BCC), and the send exactly follows the staged `file_ids` when given.
    """
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
    recipients = _recipient_dicts(assigns)
    if not recipients:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assign an estimator first")

    # The Revisions batch only exists relative to an initial hand-off — the button
    # only appears post-send, so enforce it server-side too.
    if not file_sends.has_initial_send(project_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Send the initial plans and specs package first"
        )

    pending = _unsent_updates(project_id)
    if body and body.file_ids is not None:
        # Send EXACTLY the staged subset. A requested id that isn't an unsent
        # update of this project is rejected — the double-click guard too.
        by_id = {f["id"]: f for f in pending}
        selected: list[dict] = []
        for fid in body.file_ids:
            rec = by_id.get(fid)
            if rec is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Some of those files were already sent or don't belong to this project",
                )
            selected.append(rec)
        pending = selected
    if not pending:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No new Changes/Revisions or Additional files to send"
        )

    message = (body.message or "").strip() if body else ""
    section_notes = _clean_section_notes(body.section_notes if body else None, pending)
    sent_ids = [f["id"] for f in pending]

    # Claim the batch (row + recipients + file links) BEFORE any email.
    batch = file_sends.claim_batch(
        project_id=project_id,
        kind="revision",
        sent_by=user.id,
        message=message or None,
        recipients=recipients,
        file_ids=sent_ids,
        summary=_batch_summary(pending),
        section_notes=section_notes,
    )

    first_log_id: str | None = None
    delivered: list[str] = []
    for r in recipients:
        try:
            lg = estimator_email.send_updates(
                proj=proj,
                to=[r["email"]],
                files=pending,
                message=message or None,
                recipient_name=r.get("full_name"),
                sent_by=user.id,
                section_notes=section_notes,
            )
        except Exception as exc:  # noqa: BLE001
            if not delivered:
                file_sends.abandon_batch(batch["id"])
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    "Could not email the file updates — try again",
                ) from exc
            logger.warning(
                "send_file_updates: update to a later recipient failed", exc_info=True
            )
            continue
        delivered.append(r["email"])
        file_sends.attach_email_log(batch["id"], r["email"], lg["id"])
        if first_log_id is None:
            first_log_id = lg["id"]

    file_sends.stamp_sent(sent_ids)

    counts = _category_counts(pending)
    audit(
        user.id,
        "estimator.updates_sent",
        "project",
        project_id,
        {"batch_id": batch["id"], "to": [r["email"] for r in recipients], "counts": counts},
    )
    label = f"{proj.get('number') or ''} {proj.get('name') or ''}".strip() or "a project"
    msg = (
        f"{estimator_email.updates_label(pending)} sent for {label} — "
        "review before continuing your estimate."
    )
    for estimator_id in {a["estimator_id"] for a in assigns}:
        notify_user(estimator_id, project_id, "files_updated", msg)
    return {
        "sent_to": [r["email"] for r in recipients],
        "sent_file_ids": sent_ids,
        "counts": counts,
        "email_log_id": first_log_id,
        "batch_id": batch["id"],
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


def _before_receive_quotes(project_id: str) -> bool:
    """True while nothing downstream has consumed the estimate figure yet —
    the window where a revised estimate may silently refresh the extraction
    (no badge, no human step) instead of demanding a manual reprocess. Keyed to
    the MATERIAL category (independent of labor): true until material reaches
    Receive Quotes."""
    from app.services import workflow

    state = workflow.load_category_state(project_id)
    return not workflow.category_reached(state, "material_numbers", "receive_quotes")


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
    if counts.get("estimate") and _before_receive_quotes(project_id):
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
    # Sent rounds, oldest first — drives the post-submit portal UI (locked round
    # history + the Changes/Revisions and Additional Files boxes). Scoped to the
    # caller's own submissions: with more than one active assignee, estimator B
    # must never read A's round count, timestamps or per-category summary (which
    # would also expose A's filenames). Round numbering stays project-global; the
    # portal labels from the caller's own index.
    submissions = (
        get_supabase()
        .table("estimator_submissions")
        .select("round, submitted_at, summary")
        .eq("project_id", project_id)
        .eq("estimator_id", user.id)
        .order("round")
        .execute()
    ).data or []
    return {**(proj or {}), "submissions": submissions}
