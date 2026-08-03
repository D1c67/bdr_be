"""Project submittals — per-project submittal REQUESTS to vendors (migration 0073).

Under a PM project's Submittals page the team requests product submittals from
the vendors of each material category (like the RFQ step): pick materials, pick
that category's vendor contacts, and email each one a request. Coverage tracks
which materials have had submittals requested (and which never have); vendor
replies are matched back by the email-ingestion pipeline (see submittal_ingest).

Reads are any PM-read role (accountant included, external estimator never);
create-and-send is PM-write. Every row lookup is scoped to the project.
"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_pm_read, require_pm_write
from app.core.ratelimit import ai_rate_limit, bulk_send_rate_limit, upload_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import (
    PmAddToBankIn,
    PmBankPullIn,
    SubmittalApprovalIn,
    SubmittalRequestIn,
    SubmittalVerdictIn,
)
from app.services import pm_submittal_bank, submittal_approval, submittal_sending
from app.services.notifications import audit
from app.services.pm import require_pm_project

router = APIRouter(prefix="/pm/projects/{project_id}/submittals", tags=["pm-submittals"])


@router.get("/coverage")
def coverage(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    """Per-material submittal coverage for the page: which of the project's
    materials have had submittals requested (in any request), plus the ad-hoc
    extras that were requested but aren't project materials."""
    require_pm_project(project_id)
    sb = get_supabase()
    requests = (
        sb.table("submittal_requests")
        .select("id, created_at")
        .eq("project_id", project_id)
        .execute()
    ).data or []
    if not requests:
        return {"materials": [], "adhoc_items": []}
    req_created = {r["id"]: r.get("created_at") for r in requests}

    items = (
        sb.table("submittal_request_items")
        .select(
            "request_id, pm_material_id, material_category_id, category_label, "
            "description, source, created_at"
        )
        .in_("request_id", list(req_created))
        .execute()
    ).data or []

    by_material: dict[str, dict] = {}
    adhoc: list[dict] = []
    for it in items:
        created = req_created.get(it["request_id"])
        mid = it.get("pm_material_id")
        if mid:
            cur = by_material.setdefault(mid, {"requests": set(), "last": None})
            cur["requests"].add(it["request_id"])
            if created and (cur["last"] is None or created > cur["last"]):
                cur["last"] = created
        else:
            adhoc.append(
                {
                    "material_category_id": it.get("material_category_id"),
                    "category_label": it.get("category_label"),
                    "description": it.get("description"),
                    "request_id": it["request_id"],
                    "created_at": created,
                }
            )
    materials = [
        {
            "pm_material_id": mid,
            "requested": True,
            "request_count": len(v["requests"]),
            "last_requested_at": v["last"],
        }
        for mid, v in by_material.items()
    ]
    return {"materials": materials, "adhoc_items": adhoc}


@router.get("/requests")
def list_requests(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    """Submittal-request history: each request with its snapshot items and its
    per-contact sends (status + vendor + response state + linked reply email)."""
    require_pm_project(project_id)
    return (
        get_supabase()
        .table("submittal_requests")
        .select(
            "id, status, include_specs, spec_document_keys, drawings_delivery, "
            "deselected_material_ids, created_at, created_by, "
            "submittal_request_items(id, material_category_id, category_label, "
            "pm_material_id, description, source), "
            "submittal_request_sends(id, material_category_id, vendor_contact_id, "
            "status, error, response_received_at, response_count, sent_at, "
            "conversation_id, vendor_contacts(name, email, vendors(name)), "
            "submittal_response_emails(email_id))"
        )
        .eq("project_id", project_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []


@router.post("/requests", dependencies=[Depends(bulk_send_rate_limit)])
def create_request(
    project_id: str,
    body: SubmittalRequestIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Create a submittal request and email each group's selected contacts — one
    email per contact. Per-contact failures are reported in `results`, not
    raised; a bad config or an id outside the project is a 400."""
    require_pm_project(project_id)
    if not any(g.vendor_contact_ids for g in body.groups):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No recipients selected")
    try:
        result = submittal_sending.create_and_send(
            project_id, body.model_dump(), user.id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    audit(
        user.id,
        "submittal.request_endpoint",
        "project",
        project_id,
        {
            "request_id": result["request_id"],
            "sent": sum(1 for r in result["results"] if r["status"] == "sent"),
            "failed": sum(1 for r in result["results"] if r["status"] == "failed"),
        },
    )
    return result


# ── Submittal approval packages: send to the GC (migration 0081) ─────────────
#
# The other direction from the vendor requests above: we package the submittals
# we've collected and email them to the general contractor for approval. One
# email per package (To + CC), logged per file so the approval verdicts the next
# feature records have something to hang off.


@router.get("/approval/available")
def approval_available(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    """Every submittal file on file for the project, grouped by material
    category — what the Request Submittal Approval modal offers to send."""
    require_pm_project(project_id)
    return submittal_approval.available(project_id)


@router.get("/approval/packages")
def approval_packages(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    """Approval-package history, newest first, with each package's files and
    their recorded approval status."""
    require_pm_project(project_id)
    return submittal_approval.list_packages(project_id)


@router.post("/approval/packages/{package_id}/verdict")
def approval_verdict(
    project_id: str,
    package_id: str,
    body: SubmittalVerdictIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Record the GC's response to a package, per file (migration 0082).

    The GC answers by email, by returning the marked-up transmittal, or by phone;
    a human logs it here. The package's own status is derived from its files, not
    accepted from the client. A package outside this project, a file outside the
    package, or a package that was never delivered is a 400.
    """
    require_pm_project(project_id)
    try:
        return submittal_approval.record_verdicts(
            project_id, package_id, body.model_dump(), user.id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.get("/approval/packages/{package_id}/resend-options")
def approval_resend_options(
    project_id: str,
    package_id: str,
    _: CurrentUser = Depends(require_pm_read),
):
    """What the resend modal offers: the project's available submittals merged
    with the files this package contains, each annotated with the verdict it came
    back with so the caller can pre-tick the rejected ones."""
    require_pm_project(project_id)
    try:
        return submittal_approval.resend_options(project_id, package_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@router.post(
    "/approval/packages/{package_id}/resend", dependencies=[Depends(bulk_send_rate_limit)]
)
def approval_resend(
    project_id: str,
    package_id: str,
    body: SubmittalApprovalIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Resubmit selected files in answer to a GC's review — a NEW package, with
    its own number and verdicts, linked back to the one it answers. Same body and
    same failure semantics as the plain send above."""
    require_pm_project(project_id)
    try:
        result = submittal_approval.create_and_send(
            project_id, body.model_dump(), user.id, supersedes_package_id=package_id
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    audit(
        user.id,
        "submittal_approval.resend_endpoint",
        "project",
        project_id,
        {
            "package_id": result["package_id"],
            "number": result["number"],
            "supersedes_package_id": package_id,
            "send_status": result["send_status"],
            "files": result["file_count"],
        },
    )
    return result


@router.post(
    "/approval/uploads",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def approval_upload(
    project_id: str,
    material_category_id: str | None = Form(None),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_pm_write),
):
    """Stage a submittal the sender is adding to a package. The file is archived
    into the Documents hub immediately and comes back as a `pm:<id>` key the
    modal includes in its selection — so the send endpoint stays JSON.

    PDF-only, enforced by extension AND magic bytes (the extension alone is
    spoofable), matching the bank-upload route above.
    """
    require_pm_project(project_id)
    filename = file.filename or "submittal.pdf"
    content = await _read_capped(file, get_settings().upload_max_bytes)
    if not filename.lower().endswith(".pdf") or content[:5] != b"%PDF-":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Submittals must be PDF files")
    return await run_in_threadpool(
        submittal_approval.stage_upload,
        project_id,
        material_category_id or None,
        filename,
        content,
        user.id,
    )


@router.post("/approval/packages", dependencies=[Depends(bulk_send_rate_limit)])
def approval_send(
    project_id: str,
    body: SubmittalApprovalIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Build a submittal approval package and email it to the selected GC
    contacts. A bad selection or an id outside the project is a 400; a delivery
    failure comes back as send_status='failed' in the body (the package row still
    exists, so the attempt stays in the log)."""
    require_pm_project(project_id)
    try:
        result = submittal_approval.create_and_send(project_id, body.model_dump(), user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    audit(
        user.id,
        "submittal_approval.endpoint",
        "project",
        project_id,
        {
            "package_id": result["package_id"],
            "number": result["number"],
            "send_status": result["send_status"],
            "files": result["file_count"],
        },
    )
    return result


# ── Other submittals: fill from the Submittal Bank (migration 0074) ──────────
#
# For materials NOT requested from a vendor, the team either PULLS a matching
# bank submittal or UPLOADS a PDF for one the bank doesn't cover (archived into
# the Documents hub), which can then be pushed into the global bank.


@router.get("/bank")
def bank_links(project_id: str, _: CurrentUser = Depends(require_pm_read)):
    """Persisted bank/uploaded submittal links for the project, grouped by
    material and resolved with names + previewable files."""
    require_pm_project(project_id)
    return pm_submittal_bank.list_links(project_id)


@router.post("/bank/pull")
def bank_pull(
    project_id: str,
    body: PmBankPullIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Fuzzy-match the given materials against the bank and link every
    file-bearing hit for each — a product can carry several submittals — skipping
    bank items already linked to the material."""
    require_pm_project(project_id)
    return pm_submittal_bank.pull(project_id, body.material_ids, user.id)


@router.post("/bank/upload", status_code=status.HTTP_201_CREATED, dependencies=[Depends(upload_rate_limit)])
async def bank_upload(
    project_id: str,
    pm_material_id: str = Form(...),
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_pm_write),
):
    """Upload a PDF submittal for a material the bank doesn't cover. PDF-only is
    enforced by extension AND magic bytes (extension alone is spoofable)."""
    require_pm_project(project_id)
    filename = file.filename or "submittal.pdf"
    content = await _read_capped(file, get_settings().upload_max_bytes)
    if not filename.lower().endswith(".pdf") or content[:5] != b"%PDF-":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Submittals must be PDF files")
    return await run_in_threadpool(
        pm_submittal_bank.upload, project_id, pm_material_id, filename, content, user.id
    )


@router.post("/bank/{link_id}/add-to-bank", dependencies=[Depends(ai_rate_limit)])
def bank_add(
    project_id: str,
    link_id: str,
    body: PmAddToBankIn,
    user: CurrentUser = Depends(require_pm_write),
):
    """Push an uploaded submittal PDF into the global Submittal Bank (metadata
    optional — an unset name defaults to the material's description)."""
    require_pm_project(project_id)
    return pm_submittal_bank.add_to_bank(project_id, link_id, body.model_dump(exclude_unset=True), user.id)


@router.delete("/bank/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def bank_unlink(
    project_id: str,
    link_id: str,
    user: CurrentUser = Depends(require_pm_write),
):
    """Remove a link. A bank match is a pure unlink; an uploaded PDF is also
    removed from the Documents hub (any bank copy already pushed stays)."""
    require_pm_project(project_id)
    pm_submittal_bank.delete_link(project_id, link_id, user.id)


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    """Read the upload into memory, aborting past `max_bytes` (mirrors
    submittals._read_capped)."""
    limit_mb = max_bytes // (1024 * 1024)
    if upload.size is not None and upload.size > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is too large (limit {limit_mb} MB)."
        )
    buf = bytearray()
    while chunk := await upload.read(1024 * 1024):
        buf += chunk
        if len(buf) > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is too large (limit {limit_mb} MB)."
            )
    return bytes(buf)
