"""Certified Payroll — CPR generation router: generate the weekly certified
report files, preview paper-report data, and browse/download the generation
history (cp_records + cp_record_files, bytes in storage).

Generation is the gated mutation of the CP pipeline: the report must be
finalized (processed/submitted with finalized_at), must not have gone stale
since finalization, and must still pass the finalize checks. Paper-type
projects additionally require the CALLER's completed signer profile and
per-project report metadata — the generating user signs the Statement of
Compliance, never a stored user. Every caller-fixable precondition inside the
generator raises ValueError and surfaces as a 400 here.
"""

import io
import zipfile

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.deps import CurrentUser, require_cp_read, require_cp_write
from app.core.ratelimit import export_rate_limit
from app.core.supabase_client import get_supabase
from app.models.schemas import CpGenerateBody
from app.services import cpr_generation, payroll_reports, storage
from app.services.file_export import _dedupe, _safe_name
from app.services.notifications import audit

router = APIRouter(prefix="/payroll/reports/{report_id}/cpr", tags=["payroll"])


def _get_record(report_id: str, record_id: str) -> dict:
    # Scoped to the report so a foreign/unknown id is a clean 404.
    rows = (
        get_supabase()
        .table("cp_records")
        .select("*")
        .eq("id", record_id)
        .eq("payroll_report_id", report_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CPR record not found")
    return rows[0]


def _get_record_files(record_id: str) -> list[dict]:
    return (
        get_supabase()
        .table("cp_record_files")
        .select("*")
        .eq("record_id", record_id)
        .order("filename")
        .execute()
    ).data or []


@router.post("/generate", dependencies=[Depends(export_rate_limit)])
def generate_cpr(
    report_id: str,
    body: CpGenerateBody | None = None,
    user: CurrentUser = Depends(require_cp_write),
):
    """Generate the certified payroll files for a finalized week and persist
    them as the next revision. Returns the record, file metadata, flags, and
    the non-CP hours summary — the FE reviews flags before downloading."""
    report = payroll_reports.require_cp_report(report_id)
    if report.get("status") not in ("processed", "submitted") or not report.get("finalized_at"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Please finalize the report before processing certified payroll.",
        )
    stale = payroll_reports.check_stale_since_finalization(report)
    if stale:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Data has changed since finalization: {', '.join(stale)}. "
            "Please re-finalize before processing.",
        )
    # Re-verify the finalize gate: registry/matching edits after finalization
    # (e.g. a deleted ignored-project row) must not slip into a certified file.
    issues = payroll_reports.finalize_gate_issues(report)
    if issues:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Report no longer passes the finalize checks: {'; '.join(issues)}",
        )

    paper_reports = body.paper_reports if body else None
    try:
        files, flags, non_cp_hours, file_projects = cpr_generation.generate_all(
            report_id, user.id, paper_reports=paper_reports
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if not files:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "No certified payroll files were generated for this week",
        )

    # Serialize paper_reports metadata for storage on the record
    paper_metadata = None
    if paper_reports:
        paper_metadata = [
            {
                "project_id": pr.project_id,
                "report_number": pr.report_number,
                "report_type": pr.report_type,
                "notes": pr.notes,
            }
            for pr in paper_reports
        ]

    result = cpr_generation.persist_record(
        report_id, files, flags, paper_metadata, user.id, file_projects=file_projects
    )
    audit(
        user.id,
        "cp.generate",
        "cp_payroll_report",
        report_id,
        {
            "record_id": result["record"]["id"],
            "revision_number": result["record"]["revision_number"],
            "files": [f["filename"] for f in result["files"]],
            "error_count": sum(1 for f in flags if f.get("severity") == "error"),
            "warning_count": sum(1 for f in flags if f.get("severity") == "warning"),
        },
    )
    return {
        "record": result["record"],
        "files": result["files"],
        "flags": flags,
        "non_cp_hours": non_cp_hours,
    }


@router.post("/paper-data")
def paper_report_data(
    report_id: str,
    body: CpGenerateBody,
    user: CurrentUser = Depends(require_cp_read),
):
    """Return structured paper report data as JSON for inline viewing (the
    pre-generation preview — nothing is persisted)."""
    payroll_reports.require_cp_report(report_id)
    if not body.paper_reports:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No paper reports specified")
    try:
        reports = cpr_generation.get_paper_report_data(report_id, body.paper_reports, user.id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"reports": reports}


@router.get("/records")
def list_records(report_id: str, user: CurrentUser = Depends(require_cp_read)):
    """The generation history for a report, newest first, with file metadata."""
    payroll_reports.require_cp_report(report_id)
    rows = (
        get_supabase()
        .table("cp_records")
        .select("*, cp_record_files(*)")
        .eq("payroll_report_id", report_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []
    for r in rows:
        r["files"] = r.pop("cp_record_files", None) or []
    return rows


@router.get("/records/{record_id}/download", dependencies=[Depends(export_rate_limit)])
def download_record_zip(
    report_id: str,
    record_id: str,
    user: CurrentUser = Depends(require_cp_read),
):
    """Download all files from a CPR record as a ZIP (bytes pulled from
    storage, bundled in memory)."""
    record = _get_record(report_id, record_id)
    files = _get_record_files(record_id)
    if not files:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No files in this record")

    # Sanitise each stored filename to a safe single path component and de-dupe
    # collisions before using it as a ZIP arcname — the arcname is the only
    # zip-slip defence on the extracting side (shared with the documents export).
    taken: set[str] = set()
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            arcname = _dedupe(taken, _safe_name(f["filename"]))
            zf.writestr(arcname, storage.download_file(f["storage_path"]))
    zip_buffer.seek(0)

    prefix = cpr_generation.revision_prefix(record.get("revision_number") or 0)
    zip_name = f"{prefix}Certified Payroll Reports.zip" if prefix else "Certified Payroll Reports.zip"

    audit(
        user.id,
        "cp.cpr_download",
        "cp_record",
        record_id,
        {"report_id": report_id, "files": len(files)},
    )
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@router.get("/records/{record_id}/files/{file_id}/download")
def download_record_file(
    report_id: str,
    record_id: str,
    file_id: str,
    user: CurrentUser = Depends(require_cp_read),
):
    """A single generated file, served via a short-TTL signed URL. Attachment
    disposition so a top-level open downloads instead of rendering inline."""
    _get_record(report_id, record_id)
    rows = (
        get_supabase()
        .table("cp_record_files")
        .select("*")
        .eq("id", file_id)
        .eq("record_id", record_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    rec = rows[0]
    url = storage.signed_url(rec["storage_path"], download=rec["filename"] or True)
    audit(
        user.id,
        "cp.cpr_download",
        "cp_record",
        record_id,
        {"file_id": file_id, "filename": rec["filename"]},
    )
    return {"url": url, "filename": rec["filename"]}
