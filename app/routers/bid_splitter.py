"""Bid File Splitter - experimental AI file triage and PDF splitting.

Standalone tool (deliberately NOT wired into the bidding pipeline): the user
uploads the raw PDFs a GC sent; a vision model first triages each FILE
(specifications, RFP and addendum files are identified and left intact),
then classifies every page of drawing sets and mixed packages and splits
those along the contiguous category runs, drawings by trade (see
services/bid_split.py). The results come back out as a nested ZIP - a folder
per uploaded PDF, a folder per category inside it - so a whole batch lands on
disk already organised. History and throughput analytics live here too, so the
dev can judge whether the model is good enough before connecting it to intake.

Access: every route 404s while BID_FILE_SPLITTER_ENABLED is false (router-level
dependency, sub-app precedent). While on, writers run splits and internal roles
can read results. The flag is off outside dev for now.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.deps import CurrentUser, require_internal, require_writer
from app.core.error_codes import ErrorCode, RateLimitScope
from app.core.features import require_bid_file_splitter
from app.core.ratelimit import ai_rate_limit, export_rate_limit, upload_rate_limit
from app.core.supabase_client import get_supabase
from app.routers.files import _export_lock, _read_capped, handoff_locked
from app.services import (
    bid_split,
    bid_split_training,
    export_names,
    file_export,
    llm,
    llm_queue,
    pdf_split,
    rfp_split,
    storage,
)
from app.services.notifications import audit

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/bid-splitter",
    tags=["bid-splitter"],
    dependencies=[Depends(require_bid_file_splitter)],
)

_COMPANY_TZ = ZoneInfo("America/Los_Angeles")

_NOT_CONFIGURED_MESSAGE = (
    "The AI model for the Bid File Splitter is not configured on the server. "
    "Set BID_SPLIT_LLM_PROVIDER (anthropic | openai | self_hosted) and the "
    "matching model var (SELF_HOSTED_BID_SPLIT_MODEL while self-hosted "
    "routing applies)."
)


def _job_or_404(sb, job_id: str) -> dict:
    rows = (
        sb.table("bid_split_jobs").select("*").eq("id", job_id).limit(1).execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return rows[0]


# Every bid_split_files column EXCEPT the two heavy training jsonbs
# (input_snapshot / model_output, 0113): they exist for the dev Training page
# and would otherwise ride along on every list/detail/poll response (~75KB for
# a 600-page file). user_corrected stays IN - the FE badges corrected files.
_FILE_COLUMNS = (
    "id, job_id, filename, storage_path, size_bytes, page_count, status, error, "
    "llm_calls, llm_ms, file_kind, file_kind_label, file_kind_confidence, "
    "file_kind_confidence_reason, "
    "user_corrected, started_at, finished_at, created_at, updated_at, "
    "rfp_sandbox_file_id, source_format, classified_from"
)


def _file_or_404(sb, file_id: str) -> dict:
    rows = (
        sb.table("bid_split_files")
        .select(_FILE_COLUMNS)
        .eq("id", file_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return rows[0]


def _training_io(sb, file_id: str) -> dict:
    """The two heavy training jsonbs, fetched only where the capture needs
    them (every response select excludes them - see _FILE_COLUMNS)."""
    rows = (
        sb.table("bid_split_files")
        .select("input_snapshot, model_output")
        .eq("id", file_id)
        .limit(1)
        .execute()
    ).data or []
    return rows[0] if rows else {}


def _is_pdf_row(frow: dict) -> bool:
    """A row the splitter can cut: uploaded by hand, or staged from a PDF.
    Non-PDF rows from the RFP split step (`classified_from` converted_pdf /
    name) were identified, never split."""
    return frow.get("classified_from") in (None, rfp_split.CLASSIFIED_PAGES)


def _project_for_job(sb, job_id: str) -> str | None:
    rows = (
        sb.table("bid_split_jobs").select("id, project_id").eq("id", job_id).limit(1).execute()
    ).data or []
    return (rows[0].get("project_id") if rows else None) or None


def _project_guard(sb, frow: dict) -> str | None:
    """The correction lock (docs/RFP_SPLIT.md 3.3): a file whose job serves
    an RFP-created project may be corrected only while that project's
    hand-off package has not sent. Returns the project id (None on a manual
    job) or raises 409 with the sentence."""
    project_id = _project_for_job(sb, frow["job_id"])
    if project_id and handoff_locked(str(project_id)):
        raise HTTPException(status.HTTP_409_CONFLICT, rfp_split.MSG_PACKAGE_SENT)
    return project_id


def _resync(sb, project_id: str | None, file_id: str, user_id: str) -> dict | None:
    """After a correction on a project-linked file: re-file the project's
    rows (best effort; the correction stands and the response says what
    the resync did or why it did not)."""
    if not project_id:
        return None
    try:
        result = rfp_split.resync_project_files(sb, file_id, actor_id=user_id)
    except Exception as exc:  # noqa: BLE001 - the correction stands
        logger.exception("bid_split: project resync failed for %s", file_id)
        return {"project_id": project_id, "ok": False, "error": str(exc)[:300]}
    if result is None:
        return {"project_id": project_id, "ok": False, "error": "nothing to re-file"}
    return {
        "project_id": project_id, "ok": True, "documents": result.documents,
        "inserted": result.inserted, "replaced": result.replaced, "skipped": result.skipped[:20],
    }


def _dispatch(
    background: BackgroundTasks,
    file_id: str,
    user_id: str,
    forced_kind: str | None = None,
) -> None:
    """Queue one file run, degrading to inline dispatch exactly like BOQ.
    `forced_kind` pins a user verdict on the run (reprocess): triage is
    skipped and the split pass runs as that kind."""
    if get_settings().llm_queue_enabled:
        try:
            payload = {"file_id": file_id}
            if forced_kind is not None:
                payload["forced_kind"] = forced_kind
            llm_queue.enqueue(
                llm_queue.JOB_BID_SPLIT,
                target_id=file_id,
                payload=payload,
                created_by=user_id,
            )
            return
        except Exception:  # noqa: BLE001 - queue outage degrades to inline dispatch
            logger.exception("bid_split enqueue failed; falling back to BackgroundTasks")
    background.add_task(bid_split.run_file, file_id, forced_kind)


# ── Status / create / read ───────────────────────────────────────────────


@router.get("/status")
def splitter_status(_: CurrentUser = Depends(require_internal)):
    """Light config readout for the page header: whether the AI route is
    live, which model answers, and the upload guardrails."""
    s = get_settings()
    return {
        "configured": llm.is_configured("bid_split", s),
        "model": llm.active_model("bid_split", s),
        "max_files_per_job": s.bid_split_max_files_per_job,
        "max_pages_per_file": s.bid_split_max_pages_per_file,
        "upload_max_mb": s.upload_max_bytes // (1024 * 1024),
    }


class BidSplitJobIn(BaseModel):
    """Planned size of the batch; the files then arrive one request each."""

    file_count: int = Field(ge=1)


@router.post(
    "/jobs",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(ai_rate_limit)],
)
def create_job(body: BidSplitJobIn, user: CurrentUser = Depends(require_writer)):
    """Open a job for a batch of PDFs.

    The files are NOT part of this request: each arrives individually via
    POST /jobs/{id}/files, because the global request-body cap
    (max_request_body_bytes, 460 MB) is per REQUEST while the 450 MB upload
    cap is per FILE - a legitimate multi-file bid set easily totals past the
    body cap, so a single multipart batch would 413 in the middleware before
    this handler ever ran. One file per request mirrors every other upload
    surface (New Bid, drafts). file_count here is the plan (validated against
    the per-job limit up front); it is re-stamped with reality as uploads land.
    """
    s = get_settings()
    if not llm.is_configured("bid_split", s):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _NOT_CONFIGURED_MESSAGE)
    if body.file_count > s.bid_split_max_files_per_job:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Too many files (limit {s.bid_split_max_files_per_job} per job).",
        )
    job = (
        get_supabase()
        .table("bid_split_jobs")
        .insert(
            {
                "status": "processing",
                "file_count": body.file_count,
                "model": llm.active_model("bid_split", s),
                "created_by": user.id,
            }
        )
        .execute()
    ).data[0]
    audit(user.id, "bid_split.create", "bid_split_job", job["id"], {"files": body.file_count})
    job["files"] = []
    return job


@router.post(
    "/jobs/{job_id}/files",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(upload_rate_limit)],
)
async def upload_job_file(
    job_id: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_writer),
):
    """Add one PDF to a job and start its split run immediately.

    Validation is per file (extension AND magic bytes, must open, page cap),
    so a bad file rejects only itself and the batch's other files keep
    processing. The handler is async only for the streamed multipart read;
    every sync Supabase/storage call rides run_in_threadpool (event-loop
    rule; see app/core/deps.py)."""
    s = get_settings()
    sb = get_supabase()
    job = await run_in_threadpool(_job_or_404, sb, job_id)
    filename = file.filename or "upload.pdf"
    content = await _read_capped(file, s.upload_max_bytes)
    if not filename.lower().endswith(".pdf") or content[:5] != b"%PDF-":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"{filename}: the splitter accepts PDF files only",
        )
    try:
        pages = await run_in_threadpool(pdf_split.page_count, content)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"{filename}: {exc}"
        ) from exc
    if pages > s.bid_split_max_pages_per_file:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"{filename}: {pages} pages exceeds the "
            f"{s.bid_split_max_pages_per_file}-page limit per file",
        )

    def _stage() -> dict:
        existing = (
            sb.table("bid_split_files").select("id").eq("job_id", job["id"]).execute()
        ).data or []
        if len(existing) >= s.bid_split_max_files_per_job:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"This job already holds {s.bid_split_max_files_per_job} files.",
            )
        path = storage.build_bid_split_source_path(job["id"], filename)
        storage.upload_file(path, content, "application/pdf")
        try:
            frow = (
                sb.table("bid_split_files")
                .insert(
                    {
                        "job_id": job["id"],
                        "filename": filename,
                        "storage_path": path,
                        "size_bytes": len(content),
                        "page_count": pages,
                        "status": "pending",
                    }
                )
                .execute()
            ).data[0]
        except Exception:
            # The object is already stored; drop it so a failed insert leaves
            # no orphan (submittals precedent).
            storage.delete_file(path)
            raise
        # Re-stamp file_count with reality: the create's value was the plan,
        # and a browser closed mid-batch must not overstate it forever.
        sb.table("bid_split_jobs").update({"file_count": len(existing) + 1}).eq(
            "id", job["id"]
        ).execute()
        return frow

    frow = await run_in_threadpool(_stage)
    await run_in_threadpool(_dispatch, background, frow["id"], user.id)
    return frow


_JOB_STATUSES = ("processing", "done", "done_with_errors", "failed")


@router.get("/jobs")
def list_jobs(
    limit: int = 25,
    offset: int = 0,
    status_filter: str | None = Query(default=None, alias="status"),
    _: CurrentUser = Depends(require_internal),
):
    """Job history, newest first, with per-status file counts and page totals
    so the list renders without a per-row fetch. `status` narrows to one job
    status: the app shell polls `?status=processing` for its in-flight
    marker, which must stay a cheap read."""
    limit = max(1, min(limit, 250))
    if status_filter is not None and status_filter not in _JOB_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"status must be one of {', '.join(_JOB_STATUSES)}",
        )
    sb = get_supabase()
    query = sb.table("bid_split_jobs").select("*")
    if status_filter is not None:
        query = query.eq("status", status_filter)
    jobs = (
        query.order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    ).data or []
    if not jobs:
        return jobs
    files = (
        sb.table("bid_split_files")
        .select("job_id, status, page_count")
        .in_("job_id", [j["id"] for j in jobs])
        .execute()
    ).data or []
    by_job: dict[str, list[dict]] = {}
    for f in files:
        by_job.setdefault(f["job_id"], []).append(f)
    for job in jobs:
        rows = by_job.get(job["id"], [])
        job["pages"] = sum(r["page_count"] or 0 for r in rows)
        job["files_done"] = sum(1 for r in rows if r["status"] == "done")
        job["files_failed"] = sum(1 for r in rows if r["status"] == "failed")
    _attach_projects(sb, jobs)
    return jobs


def _attach_projects(sb, jobs: list[dict]) -> None:
    """The project an rfp job serves (docs/RFP_SPLIT.md 3.3): number, name
    and whether its hand-off package has sent (corrections then answer
    409). One projects query for the page; the lock is read once per
    distinct project. Manual jobs carry nulls."""
    project_ids = sorted({str(j["project_id"]) for j in jobs if j.get("project_id")})
    projects: dict[str, dict] = {}
    for i in range(0, len(project_ids), 200):
        for p in (
            sb.table("projects").select("id, number, name").in_("id", project_ids[i:i + 200]).execute()
        ).data or []:
            projects[str(p["id"])] = p
    sent: dict[str, bool] = {}
    for job in jobs:
        pid = str(job.get("project_id") or "")
        proj = projects.get(pid)
        job.setdefault("source", rfp_split.SOURCE_MANUAL)
        job["project_number"] = proj.get("number") if proj else None
        job["project_name"] = proj.get("name") if proj else None
        if proj and pid not in sent:
            sent[pid] = handoff_locked(pid)
        job["package_sent"] = bool(sent.get(pid)) if proj else False


def _attach_segments(sb, files: list[dict]) -> list[dict]:
    """Hang each file's segments (in sort order) off the file row, in place."""
    segments = (
        sb.table("bid_split_segments")
        .select("*")
        .in_("file_id", [f["id"] for f in files] or ["-"])
        .order("sort_order")
        .execute()
    ).data or []
    by_file: dict[str, list[dict]] = {}
    for seg in segments:
        by_file.setdefault(seg["file_id"], []).append(seg)
    for f in files:
        f["segments"] = by_file.get(f["id"], [])
    return files


def _job_files(sb, job_id: str) -> list[dict]:
    """A job's source files in upload order, each with its segments attached."""
    files = (
        sb.table("bid_split_files")
        .select(_FILE_COLUMNS)
        .eq("job_id", job_id)
        .order("created_at")
        .execute()
    ).data or []
    return _attach_segments(sb, files)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, _: CurrentUser = Depends(require_internal)):
    """One job with its files and their segments. Active files carry the queue
    detail (position, attempts, retry state) for the polling panel."""
    sb = get_supabase()
    job = _job_or_404(sb, job_id)
    files = _job_files(sb, job_id)
    for f in files:
        if f["status"] in ("pending", "running"):
            try:
                f["queue"] = llm_queue.poll_info(llm_queue.JOB_BID_SPLIT, f["id"])
            except Exception:  # noqa: BLE001 - detail is optional, polling must not break
                logger.exception("bid_split queue poll_info failed")
    job["files"] = files
    _attach_projects(sb, [job])
    return job


# ── Downloads ────────────────────────────────────────────────────────────


@router.get("/files/{file_id}/download")
def download_source(file_id: str, _: CurrentUser = Depends(require_internal)):
    rec = _file_or_404(get_supabase(), file_id)
    name = rec["filename"] or True
    if rec.get("classified_from") == rfp_split.CLASSIFIED_CONVERTED and isinstance(name, str):
        # The staged object is the sandbox's converted PDF, not the .docx.
        name = f"{name.rsplit('.', 1)[0] if '.' in name else name}.pdf"
    return {"url": storage.signed_url(rec["storage_path"], download=name)}


@router.get("/segments/{segment_id}/download")
def download_segment(segment_id: str, _: CurrentUser = Depends(require_internal)):
    rows = (
        get_supabase()
        .table("bid_split_segments")
        .select("storage_path, filename")
        .eq("id", segment_id)
        .limit(1)
        .execute()
    ).data or []
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Segment not found")
    rec = rows[0]
    return {"url": storage.signed_url(rec["storage_path"], download=rec["filename"] or True)}


# ── Folder export ────────────────────────────────────────────────────────


_EXPORT_TITLE = "BDR Bid File Splitter export"


def _short_job_no(sb, job_id: str) -> str:
    """The last 4 digits of the job number for export zip names: the linked
    project's number (26.9.7126 -> 7126), else the last 4 of the job id for a
    manual upload with no project."""
    project_id = _project_for_job(sb, job_id)
    if project_id:
        rows = (
            sb.table("projects").select("number").eq("id", project_id).limit(1).execute()
        ).data or []
        short = export_names.short_job_number(rows[0].get("number") if rows else None)
        if short:
            return short
    return job_id.replace("-", "")[-4:]


def _folder_for(filename: str) -> str:
    """Folder name for a source PDF: the name it was uploaded under, minus the
    .pdf extension (a folder called "BID SET.pdf" reads as a file)."""
    name = (filename or "file").rsplit("/", 1)[-1]
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    return name.strip() or "file"


def _unique(taken: set[str], name: str) -> str:
    """Case-insensitive dedupe for the top-level folders: two uploads sharing a
    filename must land in two folders, not pour their sections into one.
    Case-insensitive because Windows and default macOS would merge them on
    extract even when the names differ as Python strings."""
    candidate, i = name, 2
    while candidate.casefold() in taken:
        candidate, i = f"{name} ({i})", i + 1
    taken.add(candidate.casefold())
    return candidate


def _tree_rows(
    files: list[dict], *, top_folder: bool = True
) -> tuple[list[dict], list[str]]:
    """Shape source files (with their segments attached) into nested-ZIP rows.

    One folder per source file, a category folder inside it, and for a
    free-text 'other' section a further folder carrying the model's label:

        Bid Set A/Electrical Drawings/E-Sheets (pages 12-40).pdf
        Bid Set A/Other/Geotechnical Report/Soils (pages 41-60).pdf

    A file the triage identified and left intact contributes its own original
    object under its category folder (segments carry is_original and point at
    the source path) - that IS the document the user wants out of it. Files
    with nothing to hand back (still running, failed, no sections) become
    manifest notes instead, so the archive explains its own gaps.

    `top_folder=False` drops the per-source folder (the one-file export): the
    zip is already named after that file and Windows "Extract All" makes a
    folder from the zip name, so keeping it would nest the same name twice.
    """
    taken: set[str] = set()
    rows: list[dict] = []
    notes: list[str] = []
    for f in files:
        segments = f.get("segments") or []
        if not segments:
            note = f"{f['filename']}: {f['status']}, no sections to export"
            if f.get("error"):
                note += f" ({f['error']})"
            notes.append(note)
            continue
        folder = _unique(taken, _folder_for(f["filename"]))
        for seg in segments:
            folders = [bid_split.CATEGORY_LABELS.get(seg["category"], "Other")]
            if top_folder:
                folders.insert(0, folder)
            if seg["category"] == "other" and seg.get("other_type"):
                folders.append(seg["other_type"])
            rows.append(
                {
                    "folders": folders,
                    "filename": seg["filename"],
                    "storage_path": seg["storage_path"],
                    "size_bytes": seg["size_bytes"],
                }
            )
    return rows, notes


async def _stream_tree_zip(
    rows: list[dict],
    notes: list[str],
    *,
    filename: str,
    flat: bool = False,
    user: CurrentUser,
    target: str,
    target_id: str,
) -> StreamingResponse:
    """Build the nested archive and stream it out.

    Sizing, the single-build lock and the streaming shape all follow the
    project-files export (routers/files.py): the total is checked against
    export_max_total_bytes up front, the archive is built into a spooled temp
    file so it is never fully resident in RAM, and only one build runs per
    process at a time (concurrent builds are the OOM vector).
    """
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No split files to export")
    # size_bytes is NOT NULL on segments, but coalesce anyway so a surprise
    # null can't silently defeat the guard (files-export precedent).
    total = sum((r.get("size_bytes") or 0) for r in rows)
    max_bytes = get_settings().export_max_total_bytes
    if total > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Too large to export at once (~{total // (1024 * 1024)} MB); "
            "download one source file's folder at a time.",
        )
    if not _export_lock.acquire(blocking=False):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=ErrorCode.RATE_LIMITED,
            headers={"Retry-After": "10", "X-RateLimit-Scope": RateLimitScope.FILE_EXPORT},
        )
    try:
        spool, manifest, size = await run_in_threadpool(
            file_export.build_tree_export_spooled,
            rows,
            title=_EXPORT_TITLE,
            notes=notes,
            flat=flat,
        )
    finally:
        _export_lock.release()

    ok_count = sum(1 for m in manifest if m["status"] == "ok")
    if ok_count == 0:
        # Every object was missing from storage - nothing to hand back.
        spool.close()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No split files to export")
    await run_in_threadpool(
        audit, user.id, "bid_split.export", target, target_id, {"count": ok_count}
    )

    def _stream_zip():
        try:
            while chunk := spool.read(262144):
                yield chunk
        finally:
            spool.close()

    return StreamingResponse(
        _stream_zip(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Content-Length": str(size),
            "Cache-Control": "no-store",
            "X-Export-File-Count": str(ok_count),
        },
    )


@router.get("/jobs/{job_id}/export", dependencies=[Depends(export_rate_limit)])
async def export_job(
    job_id: str,
    user: CurrentUser = Depends(require_internal),
    flat: bool = False,
):
    """Every split file of a job as one nested `.zip`.

    The tree mirrors what the splitter did: a folder per uploaded PDF, the
    categories it was broken into inside it, and the split PDFs in those. A
    job still processing exports what is finished and notes the rest in
    MANIFEST.txt.

    Guarded end to end so any failure surfaces as an HTTPException (which
    keeps its CORS headers) rather than a raw 500 the browser reports as the
    opaque "Failed to fetch".
    """
    try:
        sb = get_supabase()
        await run_in_threadpool(_job_or_404, sb, job_id)
        files = await run_in_threadpool(_job_files, sb, job_id)
        rows, notes = _tree_rows(files)
        job_no = await run_in_threadpool(_short_job_no, sb, job_id)
        return await _stream_tree_zip(
            rows,
            notes,
            filename=file_export.zip_filename(job_no, "split"),
            flat=flat,
            user=user,
            target="bid_split_job",
            target_id=job_id,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - keep CORS headers; never a raw 500
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Export failed"
        ) from exc


@router.get("/files/{file_id}/export", dependencies=[Depends(export_rate_limit)])
async def export_file(
    file_id: str,
    user: CurrentUser = Depends(require_internal),
    flat: bool = False,
):
    """One source PDF's split files as a nested `.zip` (same tree, one top
    folder). This is also the way out of a job whose total trips the export
    size cap."""
    try:
        sb = get_supabase()
        rec = await run_in_threadpool(_file_or_404, sb, file_id)
        files = await run_in_threadpool(_attach_segments, sb, [rec])
        rows, notes = _tree_rows(files, top_folder=False)
        job_no = await run_in_threadpool(_short_job_no, sb, rec["job_id"])
        label = export_names.fit(
            f"{job_no} {export_names.short_name(_folder_for(rec['filename']))}",
            export_names.FOLDER_MAX,
        )
        return await _stream_tree_zip(
            rows,
            notes,
            filename=file_export.zip_filename(label, "split"),
            flat=flat,
            user=user,
            target="bid_split_file",
            target_id=file_id,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - keep CORS headers; never a raw 500
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Export failed"
        ) from exc


# ── Retry / delete ───────────────────────────────────────────────────────


@router.post("/files/{file_id}/retry", dependencies=[Depends(ai_rate_limit)])
def retry_file(
    file_id: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_writer),
):
    """Re-run one failed file (a tuning loop staple). Refused while a run is
    genuinely still in flight."""
    s = get_settings()
    if not llm.is_configured("bid_split", s):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _NOT_CONFIGURED_MESSAGE)
    sb = get_supabase()
    rec = _file_or_404(sb, file_id)
    if rec["status"] != "failed":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a failed file can be retried."
        )
    try:
        if llm_queue.active_job(llm_queue.JOB_BID_SPLIT, file_id):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A run is already in flight for this file."
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - queue lookup trouble must not block retries
        logger.exception("bid_split active-job lookup failed")
    # A failed file carrying a user-corrected split kind can only have gotten
    # there through a forced reprocess (corrections need a done file; the only
    # done-to-pending path is /reprocess), so the retry keeps forcing that
    # verdict instead of letting triage overturn it.
    forced_kind = None
    if rec.get("user_corrected") and rec.get("file_kind") in bid_split.SPLIT_KINDS:
        forced_kind = rec["file_kind"]
    bid_split._mark(file_id, status="pending", error=None)
    _dispatch(background, file_id, user.id, forced_kind=forced_kind)
    audit(user.id, "bid_split.retry", "bid_split_file", file_id, {"job_id": rec["job_id"]})
    return {"status": "pending"}


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: str, user: CurrentUser = Depends(require_writer)):
    """Remove a job, its rows (cascade) and every stored object. Test runs
    pile up fast while tuning; this keeps the history and the bucket clean.
    Refused while any of its files still has a live queue run."""
    sb = get_supabase()
    job = _job_or_404(sb, job_id)
    if job.get("source") == rfp_split.SOURCE_RFP and job.get("project_id"):
        raise HTTPException(status.HTTP_409_CONFLICT, rfp_split.MSG_JOB_HAS_PROJECT)
    active = (
        sb.table("bid_split_files")
        .select("id, status")
        .eq("job_id", job_id)
        .in_("status", ["pending", "running"])
        .execute()
    ).data or []
    for rec in active:
        try:
            if llm_queue.active_job(llm_queue.JOB_BID_SPLIT, rec["id"]):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This job is still processing. Wait for it to finish (or fail) first.",
                )
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - stale rows without a live job stay deletable
            logger.exception("bid_split active-job lookup failed during delete")
    try:
        storage.delete_bid_split_prefix(job_id)
    except Exception:  # noqa: BLE001 - the row delete stands; objects are retried never
        logger.exception("bid_split: storage sweep failed for job %s", job_id)
    sb.table("bid_split_jobs").delete().eq("id", job_id).execute()
    audit(user.id, "bid_split.delete", "bid_split_job", job_id, {})


# ── User corrections (0113) ──────────────────────────────────────────────
#
# The tool exists to judge the model; that requires knowing exactly where it
# is wrong. Both endpoints fix a FINISHED file in place and capture the
# correction as a training example (services/bid_split_training) best-effort:
# a capture bug must never fail the correction that feeds it.


def _file_with_segments(sb, file_id: str) -> dict:
    """One file row + its segments, in the shape get_job serves per file."""
    return _attach_segments(sb, [_file_or_404(sb, file_id)])[0]


def _require_done(frow: dict) -> None:
    if frow["status"] != "done":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only a finished file can be corrected."
        )


def _capture(sb, before: dict, segments_before: list[dict], updated: dict, user_id: str) -> None:
    """Best-effort training capture around a correction."""
    try:
        bid_split_training.capture_correction(
            sb, before, segments_before, updated, updated["segments"], user_id
        )
    except Exception:  # noqa: BLE001 - capture must never fail the correction
        logger.exception("bid_split training capture failed for %s", before.get("id"))


class BidSplitFileKindIn(BaseModel):
    """A user verdict on what the FILE is. The vocabulary is the service's
    FILE_KINDS constant - never a hand-copied list."""

    file_kind: Literal[bid_split.FILE_KINDS]  # type: ignore[valid-type]
    file_kind_label: str | None = Field(None, max_length=200)

    @model_validator(mode="after")
    def _label_only_for_other(self):
        label = (self.file_kind_label or "").strip()
        if self.file_kind == "other":
            if not label:
                raise ValueError(
                    "file_kind_label is required when file_kind is 'other'"
                )
            self.file_kind_label = label
        else:
            self.file_kind_label = None
        return self


@router.patch("/files/{file_id}")
def correct_file_kind(
    file_id: str,
    body: BidSplitFileKindIn,
    user: CurrentUser = Depends(require_writer),
):
    """Correct the triage verdict on a finished file.

    An intact kind (specifications/rfp/addendum/other) collapses the file to
    the shape the intact-triage path produces: one is_original segment
    pointing at the untouched source object (any cut outputs are swept; the
    source object never is). drawing_set/mixed leave the segments alone - the
    FE offers Edit splits for those, and a previously intact file keeps its
    single original segment until the user edits the splits.
    """
    sb = get_supabase()
    frow = _file_or_404(sb, file_id)
    _require_done(frow)
    if (
        frow.get("file_kind") == body.file_kind
        and (frow.get("file_kind_label") or None) == body.file_kind_label
    ):
        # No-op: nothing changed, nothing to capture or audit.
        return _attach_segments(sb, [frow])[0]
    project_id = _project_guard(sb, frow)
    pdf_row = _is_pdf_row(frow)
    if not pdf_row and body.file_kind in bid_split.SPLIT_KINDS:
        raise HTTPException(status.HTTP_409_CONFLICT, rfp_split.MSG_NOT_A_PDF)
    if body.file_kind in bid_split.INTACT_KINDS and not frow.get("page_count") and pdf_row:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This file has no recorded page count; re-run it first.",
        )

    # Baseline BEFORE any mutation - the pre-mutation state is the model's
    # answer as the user saw it (first capture freezes it).
    before = {**frow, **_training_io(sb, file_id)}
    segments_before = _attach_segments(sb, [dict(frow)])[0]["segments"]

    if body.file_kind in bid_split.INTACT_KINDS:
        category = bid_split._KIND_TO_CATEGORY[body.file_kind]
        other_type = body.file_kind_label if category == "other" else None
        # A user verdict is not a model estimate: no score, marked Edited.
        # The score it overrode is the model's file-level verdict; older rows
        # without one fall back to the best-overlap segment score.
        # A non-PDF row was never opened page by page: its one segment spans
        # what the identification recorded (1..1 when nothing was opened).
        page_end = frow.get("page_count") or max(
            [int(sg.get("page_end") or 1) for sg in segments_before] or [1]
        )
        prior = frow.get("file_kind_confidence")
        if prior is None:
            prior = _prior_confidence(segments_before, 1, page_end)
        bid_split._delete_segments(file_id)
        sb.table("bid_split_segments").insert(
            {
                "file_id": file_id,
                "sort_order": 0,
                "category": category,
                "other_type": other_type,
                "name": bid_split._fallback_name(
                    {"category": category, "other_type": other_type}
                ),
                "description": "Reclassified by the user; left intact.",
                "confidence": None,
                "confidence_reason": None,
                "user_edited": True,
                "prior_confidence": prior,
                "page_start": 1,
                "page_end": page_end,
                "storage_path": frow["storage_path"],
                "filename": frow["filename"],
                "size_bytes": frow["size_bytes"],
                "is_original": True,
            }
        ).execute()

    sb.table("bid_split_files").update(
        {
            "file_kind": body.file_kind,
            "file_kind_label": body.file_kind_label,
            # A user verdict is not a model estimate.
            "file_kind_confidence": None,
            "file_kind_confidence_reason": None,
            "user_corrected": True,
        }
    ).eq("id", file_id).execute()

    updated = _file_with_segments(sb, file_id)
    _capture(sb, before, segments_before, updated, user.id)
    audit(
        user.id,
        "bid_split.correct_kind",
        "bid_split_file",
        file_id,
        {"from": frow.get("file_kind"), "to": body.file_kind, "project_id": project_id},
    )
    updated["project_resync"] = _resync(sb, project_id, file_id, user.id)
    return updated


@router.post("/files/{file_id}/reprocess", dependencies=[Depends(ai_rate_limit)])
def reprocess_file(
    file_id: str,
    background: BackgroundTasks,
    user: CurrentUser = Depends(require_writer),
):
    """Re-run the split on a finished file the user re-typed as a split kind
    (drawing set / mixed). Triage is skipped and the user's verdict is
    forced, so the per-page pass rebuilds the trade segments instead of the
    model getting a chance to re-triage the file back to intact. The verdict
    comes from the file row (the PATCH above lands first), never a body."""
    s = get_settings()
    if not llm.is_configured("bid_split", s):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _NOT_CONFIGURED_MESSAGE)
    sb = get_supabase()
    rec = _file_or_404(sb, file_id)
    _require_done(rec)
    if not _is_pdf_row(rec):
        raise HTTPException(status.HTTP_409_CONFLICT, rfp_split.MSG_NOT_A_PDF)
    if rec.get("file_kind") not in bid_split.SPLIT_KINDS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only a file marked as a drawing set or mixed package can be "
            "reprocessed for splitting. Change the file type first.",
        )
    # The project's rows are re-filed when the run ends (bid_split._after_done),
    # so the lock is checked here, before the run is queued.
    project_id = _project_guard(sb, rec)
    try:
        if llm_queue.active_job(llm_queue.JOB_BID_SPLIT, file_id):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A run is already in flight for this file."
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - queue lookup trouble must not block reprocessing
        logger.exception("bid_split active-job lookup failed")
    bid_split._mark(file_id, status="pending", error=None)
    _dispatch(background, file_id, user.id, forced_kind=rec["file_kind"])
    audit(
        user.id,
        "bid_split.reprocess",
        "bid_split_file",
        file_id,
        {"job_id": rec["job_id"], "file_kind": rec["file_kind"], "project_id": project_id},
    )
    return {"status": "pending"}


class BidSplitSegmentIn(BaseModel):
    category: Literal[bid_split.CATEGORIES]  # type: ignore[valid-type]
    other_type: str | None = Field(None, max_length=200)
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _label_only_for_other(self):
        label = (self.other_type or "").strip()
        if self.category == "other":
            if not label:
                raise ValueError("other_type is required when category is 'other'")
            self.other_type = label
        else:
            self.other_type = None
        return self


class BidSplitSegmentsIn(BaseModel):
    """Full replacement of a file's segment list, in document order."""

    segments: list[BidSplitSegmentIn] = Field(..., min_length=1, max_length=200)


def _prior_confidence(segments_before: list[dict], page_start: int, page_end: int) -> float | None:
    """The model's score on the old segment an edited range mostly came from:
    the old row with the greatest page overlap. An old row that was itself a
    user edit has no score of its own - fall back to ITS prior_confidence so
    the model's original verdict survives repeated edits."""
    best: dict | None = None
    best_overlap = 0
    for old in segments_before:
        overlap = min(page_end, old["page_end"]) - max(page_start, old["page_start"]) + 1
        if overlap > best_overlap:
            best, best_overlap = old, overlap
    if best is None:
        return None
    conf = best.get("confidence")
    return conf if conf is not None else best.get("prior_confidence")


def _validate_coverage(segments: list[dict], total_pages: int) -> None:
    """The new segments must cover the file exactly once, in order: first
    starts at 1, each next starts where the previous ended, last ends at the
    page count. 422 names the failing row."""
    prev_end = 0
    for i, seg in enumerate(segments, start=1):
        if seg["page_start"] > seg["page_end"]:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Segment {i}: page_start {seg['page_start']} is after "
                f"page_end {seg['page_end']}.",
            )
        if seg["page_start"] != prev_end + 1:
            if i == 1:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"Segment 1 must start at page 1 (got {seg['page_start']}).",
                )
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Segment {i} starts at page {seg['page_start']} but the "
                f"previous segment ends at page {prev_end}; segments must "
                "cover the file contiguously, in order.",
            )
        prev_end = seg["page_end"]
    if prev_end != total_pages:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"The last segment ends at page {prev_end} but the file has "
            f"{total_pages} pages.",
        )


@router.put("/files/{file_id}/segments")
def correct_segments(
    file_id: str,
    body: BidSplitSegmentsIn,
    user: CurrentUser = Depends(require_writer),
):
    """Replace a finished file's segments with the user's page ranges.

    The whole list comes in at once (full replacement), is validated for
    exact coverage, then re-cut through the same cut_segment path execute()
    uses - so the cover-sheet prepend and the whole-file is_original
    passthrough behave identically. Rows the user left untouched (same
    category, label and range) keep their LLM name/description; new or moved
    rows get the deterministic fallback name. The file's kind is re-derived
    from the new segments.
    """
    sb = get_supabase()
    frow = _file_or_404(sb, file_id)
    _require_done(frow)
    if not _is_pdf_row(frow):
        raise HTTPException(status.HTTP_409_CONFLICT, rfp_split.MSG_NOT_A_PDF)
    try:
        if llm_queue.active_job(llm_queue.JOB_BID_SPLIT, file_id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A run is in flight for this file; wait for it to finish first.",
            )
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - queue lookup trouble must not block the edit
        logger.exception("bid_split active-job lookup failed")
    project_id = _project_guard(sb, frow)
    total_pages = frow.get("page_count")
    if not total_pages:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This file has no recorded page count; re-run it first.",
        )
    segs = [s.model_dump() for s in body.segments]
    _validate_coverage(segs, total_pages)

    # Baseline BEFORE any mutation (see correct_file_kind).
    before = {**frow, **_training_io(sb, file_id)}
    segments_before = _attach_segments(sb, [dict(frow)])[0]["segments"]

    # Untouched rows (same category, label and range) keep the LLM's
    # name/description AND its confidence - one edit must not wipe the scores
    # off rows the user never touched. Anything new or moved gets the
    # deterministic name, an Edited marker instead of a score, and the score
    # of the old row it mostly came from (shown on hover in the FE).
    # An untouched row also keeps its stored OBJECT (no re-cut, no re-upload;
    # a whole-set edit re-uploading hundreds of MB is what these transfers
    # keep dying on) - except a trade segment whose baked-in cover pages
    # changed, whose content is stale even though its own range didn't move.
    general_pages = bid_split.general_cover_pages(segs)
    covers_changed = general_pages != bid_split.general_cover_pages(segments_before)
    by_identity = {
        (
            old["category"],
            (old.get("other_type") or "").casefold(),
            old["page_start"],
            old["page_end"],
        ): old
        for old in segments_before
    }
    reuse_by_index: dict[int, dict] = {}
    for i, seg in enumerate(segs):
        old = by_identity.get(
            (
                seg["category"],
                (seg["other_type"] or "").casefold(),
                seg["page_start"],
                seg["page_end"],
            )
        )
        if old is not None:
            seg["name"] = old["name"]
            seg["description"] = old.get("description")
            seg["confidence"] = old.get("confidence")
            seg["confidence_reason"] = old.get("confidence_reason")
            seg["user_edited"] = old.get("user_edited", False)
            seg["prior_confidence"] = old.get("prior_confidence")
            if not (
                covers_changed
                and seg["category"] in bid_split.COVER_PREFIX_CATEGORIES
            ):
                reuse_by_index[i] = old
        else:
            seg["name"] = bid_split._fallback_name(seg)
            seg["description"] = "Reassigned by the user."
            seg["confidence"] = None
            seg["confidence_reason"] = None
            seg["user_edited"] = True
            seg["prior_confidence"] = _prior_confidence(
                segments_before, seg["page_start"], seg["page_end"]
            )

    # Cut the new outputs first, then swap the rows: a failure mid-cut leaves
    # the old segments standing. _delete_segments sweeps the old cut objects
    # (minus the reused ones) and already guards is_original rows - the
    # source object never goes. The source download only happens if some
    # segment actually needs cutting (whole-file segments never do).
    needs_cut = any(
        i not in reuse_by_index
        and not (seg["page_start"] == 1 and seg["page_end"] == total_pages)
        for i, seg in enumerate(segs)
    )
    pdf = storage.download_file(frow["storage_path"]) if needs_cut else b""
    inserts = []
    for order, seg in enumerate(segs):
        reused = reuse_by_index.get(order)
        if reused is not None:
            path, out_name, size, is_original = (
                reused["storage_path"],
                reused["filename"],
                reused["size_bytes"],
                reused["is_original"],
            )
        else:
            path, out_name, size, is_original = bid_split.cut_segment(
                pdf, frow, seg, general_pages, total_pages
            )
        inserts.append(
            {
                "file_id": file_id,
                "sort_order": order,
                "category": seg["category"],
                "other_type": seg["other_type"],
                "name": seg["name"],
                "description": seg["description"],
                "confidence": seg["confidence"],
                "confidence_reason": seg["confidence_reason"],
                "user_edited": seg["user_edited"],
                "prior_confidence": seg["prior_confidence"],
                "page_start": seg["page_start"],
                "page_end": seg["page_end"],
                "storage_path": path,
                "filename": out_name,
                "size_bytes": size,
                "is_original": is_original,
            }
        )
    bid_split._delete_segments(
        file_id, keep_paths={row["storage_path"] for row in inserts}
    )
    sb.table("bid_split_segments").insert(inserts).execute()

    # The user's segments are ground truth now; re-derive what the file is.
    kind, kind_label = bid_split._derive_kind(segs)
    sb.table("bid_split_files").update(
        {
            "file_kind": kind,
            "file_kind_label": kind_label,
            "file_kind_confidence": None,
            "file_kind_confidence_reason": None,
            "user_corrected": True,
        }
    ).eq("id", file_id).execute()

    updated = _file_with_segments(sb, file_id)
    _capture(sb, before, segments_before, updated, user.id)
    audit(
        user.id,
        "bid_split.correct_segments",
        "bid_split_file",
        file_id,
        {"segments": len(segs), "was": len(segments_before), "project_id": project_id},
    )
    updated["project_resync"] = _resync(sb, project_id, file_id, user.id)
    return updated


# ── Analytics ────────────────────────────────────────────────────────────


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("/analytics")
def analytics(days: int = 30, _: CurrentUser = Depends(require_internal)):
    """Throughput/quality readout over the last `days` (0 = everything):
    volumes, durations, per-page speed, category and confidence distributions,
    a per-day trend (LA calendar, company convention) and recent failures.
    Aggregated in Python - dev-tool volumes, not a reporting warehouse."""
    days = max(0, min(days, 365))
    sb = get_supabase()
    jobs_q = sb.table("bid_split_jobs").select("id, status, created_at")
    files_q = sb.table("bid_split_files").select(
        "id, job_id, filename, status, error, page_count, llm_calls, llm_ms, "
        "file_kind, started_at, finished_at, created_at"
    )
    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        jobs_q = jobs_q.gte("created_at", cutoff)
        files_q = files_q.gte("created_at", cutoff)
    jobs = jobs_q.execute().data or []
    files = files_q.execute().data or []
    segments = (
        sb.table("bid_split_segments")
        .select("file_id, category, page_start, page_end, confidence, is_original")
        .in_("file_id", [f["id"] for f in files] or ["-"])
        .execute()
    ).data or []

    done = [f for f in files if f["status"] == "done"]
    failed = [f for f in files if f["status"] == "failed"]
    durations: list[float] = []
    for f in done:
        start, end = _parse_ts(f["started_at"]), _parse_ts(f["finished_at"])
        if start and end and end > start:
            durations.append((end - start).total_seconds())
    done_pages = sum(f["page_count"] or 0 for f in done)
    total_seconds = sum(durations)
    durations_sorted = sorted(durations)

    # Triage outcomes: what the files turned out to BE, and how many were
    # identified and left intact (every segment points at the source object).
    kinds: dict[str, int] = {}
    for f in files:
        if f.get("file_kind"):
            kinds[f["file_kind"]] = kinds.get(f["file_kind"], 0) + 1
    segs_by_file: dict[str, list[dict]] = {}
    for seg in segments:
        segs_by_file.setdefault(seg["file_id"], []).append(seg)
    files_intact = sum(
        1
        for f in done
        if segs_by_file.get(f["id"])
        and all(s.get("is_original") for s in segs_by_file[f["id"]])
    )

    categories: dict[str, dict[str, int]] = {}
    confidences: list[float] = []
    buckets = {"lt50": 0, "50to70": 0, "70to90": 0, "gte90": 0}
    for seg in segments:
        cat = categories.setdefault(seg["category"], {"segments": 0, "pages": 0})
        cat["segments"] += 1
        cat["pages"] += seg["page_end"] - seg["page_start"] + 1
        conf = seg.get("confidence")
        if conf is not None:
            confidences.append(conf)
            if conf < 0.5:
                buckets["lt50"] += 1
            elif conf < 0.7:
                buckets["50to70"] += 1
            elif conf < 0.9:
                buckets["70to90"] += 1
            else:
                buckets["gte90"] += 1

    daily: dict[str, dict] = {}
    for f in files:
        created = _parse_ts(f["created_at"])
        if not created:
            continue
        day = created.astimezone(_COMPANY_TZ).date().isoformat()
        row = daily.setdefault(day, {"day": day, "files": 0, "pages": 0, "seconds": 0.0, "timed": 0})
        row["files"] += 1
        row["pages"] += f["page_count"] or 0
        start, end = _parse_ts(f["started_at"]), _parse_ts(f["finished_at"])
        if f["status"] == "done" and start and end and end > start:
            row["seconds"] += (end - start).total_seconds()
            row["timed"] += 1
    trend = []
    for day in sorted(daily):
        row = daily[day]
        trend.append(
            {
                "day": day,
                "files": row["files"],
                "pages": row["pages"],
                "avg_file_seconds": round(row["seconds"] / row["timed"], 1) if row["timed"] else None,
            }
        )

    failures = sorted(failed, key=lambda f: f.get("finished_at") or "", reverse=True)[:10]
    llm_calls = sum(f["llm_calls"] or 0 for f in files)
    llm_ms = sum(f["llm_ms"] or 0 for f in files)
    return {
        "range_days": days,
        "totals": {
            "jobs": len(jobs),
            "files": len(files),
            "pages": sum(f["page_count"] or 0 for f in files),
            "segments": len(segments),
            "files_done": len(done),
            "files_failed": len(failed),
            "files_intact": files_intact,
            "llm_calls": llm_calls,
        },
        "durations": {
            "avg_file_seconds": round(total_seconds / len(durations), 1) if durations else None,
            "p90_file_seconds": round(durations_sorted[int(len(durations_sorted) * 0.9) - 1], 1)
            if len(durations_sorted) >= 2
            else (round(durations_sorted[0], 1) if durations_sorted else None),
            "avg_seconds_per_page": round(total_seconds / done_pages, 2)
            if done_pages and total_seconds
            else None,
            "pages_per_minute": round(done_pages / (total_seconds / 60), 1)
            if total_seconds
            else None,
            "avg_llm_ms_per_call": int(llm_ms / llm_calls) if llm_calls else None,
        },
        "kinds": [
            {"kind": name, "files": count} for name, count in sorted(kinds.items())
        ],
        "categories": [
            {"category": name, **counts} for name, counts in sorted(categories.items())
        ],
        "confidence": {
            "avg": round(sum(confidences) / len(confidences), 3) if confidences else None,
            "buckets": buckets,
        },
        "daily": trend,
        "recent_failures": [
            {
                "id": f["id"],
                "job_id": f["job_id"],
                "filename": f["filename"],
                "error": f["error"],
                "finished_at": f["finished_at"],
            }
            for f in failures
        ],
    }
