"""Send Out (step 10): BOQ → LLM scope lines → reviewed/approved → per-GC
proposal .docx → individually emailed to the GCs the PA chooses. Skipping a
GC (never sending to them) is how "decided not to bid to them" is recorded;
the stage ends only via the explicit complete-send-out call ("Done sending").

Replaces the old one-email-no-attachment send-out stub. Writes are PA/PM
(+ IT admin per convention); reads are any internal role. The estimator never
reaches these routes (_internal) and never sees 'proposal' files (files.py
whitelists)."""

import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.roles import INTERNAL_ROLES, Role
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    ProposalAmountsIn,
    ProposalGenerateIn,
    ProposalLinesIn,
    ProposalSendIn,
)
from app.services import office_preview, proposal_scope, proposal_send
from app.services.notifications import audit
from app.services.proposal_send import ProposalSendError

router = APIRouter(prefix="/projects/{project_id}", tags=["proposals"])
_PA_PM = require_role(Role.PA, Role.PM, Role.IT_ADMIN)


def _internal(user: CurrentUser) -> None:
    if user.role not in INTERNAL_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not permitted")


def _draft_or_404(project_id: str, draft_id: str) -> dict:
    row = (
        get_supabase()
        .table("proposal_drafts")
        .select("*")
        .eq("id", draft_id)
        .eq("project_id", project_id)
        .execute()
    ).data
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Draft not found")
    return row[0]


def _wire_shape(draft: dict | None) -> dict | None:
    """lines = the editable value (seeded from the LLM result at completion);
    result_json stays the immutable audit record of what the model said."""
    if draft is None:
        return None
    return {**draft, "lines": draft.get("lines_json") or []}


# ── scope lines (LLM job, boq_analyses pattern) ───────────────────────────


@router.post("/proposal-lines", status_code=status.HTTP_201_CREATED)
async def start_lines_generation(
    project_id: str,
    body: ProposalGenerateIn,
    background: BackgroundTasks,
    user: CurrentUser = Depends(_PA_PM),
):
    sb = get_supabase()
    boq_file_id = body.boq_file_id
    if not boq_file_id:
        latest = (
            sb.table("project_files")
            .select("id")
            .eq("project_id", project_id)
            .eq("category", "boq")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        ).data
        if not latest:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "No BOQ file uploaded for this project"
            )
        boq_file_id = latest[0]["id"]

    row = (
        sb.table("proposal_drafts")
        .insert(
            {
                "project_id": project_id,
                "boq_file_id": boq_file_id,
                "status": "pending",
                "created_by": user.id,
            }
        )
        .execute()
    ).data[0]
    background.add_task(proposal_scope.run_generation, row["id"])
    audit(user.id, "proposal.lines_generate", "proposal_draft", row["id"],
          {"boq_file_id": boq_file_id})
    return _wire_shape(row)


@router.get("/proposal-lines/latest")
async def latest_draft(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    rows = (
        get_supabase()
        .table("proposal_drafts")
        .select("*")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return None
    # A server restart mid-generation strands rows at pending/running; release
    # them here so the UI's generate button comes back.
    return _wire_shape(proposal_scope.fail_if_stale(rows[0]))


@router.put("/proposal-lines/{draft_id}/lines")
async def save_lines(
    project_id: str,
    draft_id: str,
    body: ProposalLinesIn,
    user: CurrentUser = Depends(_PA_PM),
):
    sb = get_supabase()
    _draft_or_404(project_id, draft_id)
    blocked = (
        sb.table("proposal_sends")
        .select("id")
        .eq("project_id", project_id)
        .in_("status", ["sent", "sending"])
        .limit(1)
        .execute()
    ).data
    if blocked:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Proposals have already been sent (or a send is in progress) — lines are locked.",
        )
    updated = (
        sb.table("proposal_drafts")
        .update({"lines_json": body.lines, "approved_at": None, "approved_by": None})
        .eq("id", draft_id)
        .execute()
    ).data[0]
    audit(user.id, "proposal.lines_save", "proposal_draft", draft_id,
          {"line_count": len(body.lines)})
    return _wire_shape(updated)


@router.post("/proposal-lines/{draft_id}/approve")
async def approve_lines(
    project_id: str, draft_id: str, user: CurrentUser = Depends(_PA_PM)
):
    from datetime import datetime, timezone

    draft = _draft_or_404(project_id, draft_id)
    if draft["status"] != "done" or not draft.get("lines_json"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Lines are not ready to approve — generate them first."
        )
    updated = (
        get_supabase()
        .table("proposal_drafts")
        .update(
            {
                "approved_at": datetime.now(timezone.utc).isoformat(),
                "approved_by": user.id,
            }
        )
        .eq("id", draft_id)
        .execute()
    ).data[0]
    audit(user.id, "proposal.approve", "proposal_draft", draft_id,
          {"line_count": len(draft["lines_json"])})
    return _wire_shape(updated)


# GET /projects/{id}/gcs (membership read + writes) lives in routers/projects.py.


def _sorted_contacts(gc_row: dict) -> list[dict]:
    return sorted(gc_row.get("gc_contacts") or [], key=lambda c: (c.get("name") or "").lower())


# ── proposals (per-GC docx) ────────────────────────────────────────────────


def _proposal_rows(project_id: str) -> list[dict]:
    rows = (
        get_supabase()
        .table("proposal_sends")
        .select(
            "*, project_files(id, filename, preview_status),"
            " general_contractors(id, name, gc_contacts(id, name, email, phone))"
        )
        .eq("project_id", project_id)
        .neq("status", "superseded")
        .order("gc_name")
        .execute()
    ).data or []
    out = []
    for r in rows:
        file = r.pop("project_files", None) or {}
        gc = r.pop("general_contractors", None) or None
        if gc:
            gc = {"id": gc["id"], "name": gc["name"], "contacts": _sorted_contacts(gc)}
        out.append(
            {
                **r,
                "gc": gc,
                "filename": file.get("filename"),
                "preview_status": file.get("preview_status"),
                # Generation is all-or-nothing and validated before storing, so a
                # row with a file is a reviewed-and-ready document; a null file_id
                # means the file row was deleted out from under us.
                "validated": r.get("file_id") is not None,
                "send_status": {
                    "generated": "unsent",
                    "sending": "sending",
                    "sent": "sent",
                    "failed": "failed",
                }[r["status"]],
            }
        )
    return out


@router.get("/proposals")
async def list_proposals(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    return _proposal_rows(project_id)


# ── per-GC amounts (numbers editor, before documents are generated) ───────


@router.get("/proposals/amounts")
async def get_proposal_amounts(
    project_id: str, user: CurrentUser = Depends(get_current_user)
):
    _internal(user)
    return await asyncio.to_thread(proposal_send.amounts_overview, project_id)


@router.put("/proposals/amounts/{gc_id}")
async def set_proposal_amounts(
    project_id: str,
    gc_id: str,
    body: ProposalAmountsIn,
    user: CurrentUser = Depends(_PA_PM),
):
    try:
        return await asyncio.to_thread(
            proposal_send.set_gc_amounts,
            project_id,
            gc_id,
            body.material_amount,
            body.labor_amount,
            user.id,
        )
    except ProposalSendError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/proposals/generate")
async def generate_proposals(
    project_id: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(_PA_PM),
):
    sb = get_supabase()
    drafts = (
        sb.table("proposal_drafts")
        .select("id, approved_at")
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data
    if not drafts or not drafts[0].get("approved_at"):
        raise HTTPException(status.HTTP_409_CONFLICT, "Proposal lines must be approved first")
    try:
        created = await asyncio.to_thread(
            proposal_send.generate_documents, project_id, drafts[0]["id"], user.id
        )
    except ProposalSendError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    for row in created:
        file = row.pop("_file", None)
        if file and office_preview.is_convertible(file["filename"], "proposal"):
            background.add_task(office_preview.generate_preview, file["id"])
    return _proposal_rows(project_id)


@router.get("/proposals/email-preview")
async def email_preview(project_id: str, user: CurrentUser = Depends(get_current_user)):
    _internal(user)
    project = (
        get_supabase().table("projects").select("name, number").eq("id", project_id)
        .single().execute()
    ).data
    if not project:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    subject, body = proposal_send.build_cover_email(project)
    return {"subject": subject, "body": body, "gc_name_token": proposal_send.GC_NAME_TOKEN}


@router.post("/proposals/send")
async def send_proposals(
    project_id: str,
    body: ProposalSendIn,
    user: CurrentUser = Depends(_PA_PM),
):
    try:
        return await asyncio.to_thread(
            proposal_send.send_proposals,
            project_id,
            user.id,
            body.proposal_ids,
            body.email_body,
            body.force,
            body.contacts,
        )
    except ProposalSendError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.post("/proposals/complete-send-out")
async def complete_send_out(project_id: str, user: CurrentUser = Depends(_PA_PM)):
    """The PA's explicit "Done sending": flips the project to Submitted.
    Requires at least one sent proposal; GCs never sent are recorded in the
    stage-event note as skipped (= decided not to bid to them)."""
    try:
        return await asyncio.to_thread(proposal_send.complete_send_out, project_id, user.id)
    except ProposalSendError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
